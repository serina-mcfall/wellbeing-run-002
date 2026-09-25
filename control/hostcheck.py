"""C-08c: the pre-T+00 host-resource headroom gate.

Protocol v2 requires "host CPU/RAM/disk/fd/inotify/port headroom" checked
before T+00 (§"Preflight"). This module reads six independent host signals
and evaluates each against a governed threshold (human decision, recorded
here as the only place these numbers live - not invented per-call):

  CPU      1-minute load average <= 0.75 x logical CPU count
  RAM      MemAvailable >= 4 GiB
  Disk     free space on the filesystem holding config.REPO_ROOT >= 20 GiB
  fd       system-wide remaining file handles >= 10,000
  inotify  current-real-UID watches <= 50% of max_user_watches,
           current-real-UID instances <= 50% of max_user_instances
  Ports    at least 20 ports in config/isolation.json's declared candidate
           range are bindable on 127.0.0.1 right now

The port check proves only "at least 20 candidate ports are free right
now." It does not reserve them and is not evidence any port will still be
free by the time a worker needs one - allocation/ownership is C-09, not
this module.

Every raw reader raises HostCheckError on an unreadable or malformed
source; headroom_check() catches that per-signal and fails closed,
reporting every failing/unreadable signal, not only the first.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

from . import config

CPU_LOAD_FACTOR = 0.75
RAM_MIN_BYTES = 4 * 1024 ** 3
DISK_MIN_BYTES = 20 * 1024 ** 3
FD_MIN_REMAINING = 10_000
INOTIFY_MAX_FRACTION = 0.5
PORT_MIN_FREE = 20

LOADAVG_PATH = Path("/proc/loadavg")
MEMINFO_PATH = Path("/proc/meminfo")
FILE_NR_PATH = Path("/proc/sys/fs/file-nr")
INOTIFY_MAX_WATCHES_PATH = Path("/proc/sys/fs/inotify/max_user_watches")
INOTIFY_MAX_INSTANCES_PATH = Path("/proc/sys/fs/inotify/max_user_instances")
ISOLATION_CONFIG_PATH = config.REPO_ROOT / "config" / "isolation.json"


class HostCheckError(RuntimeError):
    """A required OS/config source could not be read or parsed."""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str
    fields: dict = field(default_factory=dict)


# --------------------------------------------------------------- raw readers

def read_load1(loadavg_path: Path | None = None) -> float:
    path = loadavg_path or LOADAVG_PATH
    try:
        return float(path.read_text(encoding="utf-8").split()[0])
    except OSError as exc:
        raise HostCheckError(f"could not read {path}: {exc}") from exc
    except (ValueError, IndexError) as exc:
        raise HostCheckError(f"{path} did not contain a parseable load average: {exc}") from exc


def logical_cpu_count() -> int:
    cpus = os.cpu_count()
    if not cpus:
        raise HostCheckError("os.cpu_count() returned no usable logical CPU count")
    return cpus


def read_mem_available_bytes(meminfo_path: Path | None = None) -> int:
    path = meminfo_path or MEMINFO_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HostCheckError(f"could not read {path}: {exc}") from exc
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError) as exc:
                raise HostCheckError(f"{path}'s MemAvailable line is malformed: {line!r}") from exc
    raise HostCheckError(f"{path} has no MemAvailable field")


def read_disk_free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError as exc:
        raise HostCheckError(f"could not read disk usage for {path}: {exc}") from exc


def read_fd_capacity(file_nr_path: Path | None = None) -> tuple[int, int, int]:
    """Returns (allocated_handles, legacy_free_field, max_handles) parsed
    from /proc/sys/fs/file-nr's three whitespace-separated fields.

    Per proc_sys_fs(5): field 1 is the number of allocated (open) file
    handles; field 2 has been an unused legacy value, always zero, since
    Linux 2.6 - it must never be read as "free capacity"; field 3 is the
    system-wide maximum (identical to /proc/sys/fs/file-max). Remaining
    capacity is therefore max_handles - allocated_handles, never field 2.
    """
    path = file_nr_path or FILE_NR_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HostCheckError(f"could not read {path}: {exc}") from exc
    parts = text.split()
    if len(parts) != 3:
        raise HostCheckError(f"{path} did not contain exactly three fields: {parts!r}")
    try:
        allocated_handles, legacy_free_field, max_handles = (int(p) for p in parts)
    except ValueError as exc:
        raise HostCheckError(f"{path} contained a non-integer field: {parts!r}") from exc
    return allocated_handles, legacy_free_field, max_handles


def read_inotify_ceilings(max_watches_path: Path | None = None,
                          max_instances_path: Path | None = None) -> tuple[int, int]:
    watches_path = max_watches_path or INOTIFY_MAX_WATCHES_PATH
    instances_path = max_instances_path or INOTIFY_MAX_INSTANCES_PATH
    try:
        max_watches = int(watches_path.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise HostCheckError(f"could not read {watches_path}: {exc}") from exc
    except ValueError as exc:
        raise HostCheckError(f"{watches_path} was not an integer: {exc}") from exc
    try:
        max_instances = int(instances_path.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise HostCheckError(f"could not read {instances_path}: {exc}") from exc
    except ValueError as exc:
        raise HostCheckError(f"{instances_path} was not an integer: {exc}") from exc
    return max_watches, max_instances


def current_real_uid_inotify_usage(proc_root: Path | None = None,
                                   real_uid: int | None = None) -> tuple[int, int]:
    """Returns (instances_used, watches_used) for one real UID (defaults
    to the current process's), scanned from /proc.

    An inotify fd is identified by os.readlink(/proc/<pid>/fd/<fd>) ==
    "anon_inode:inotify" (confirmed on this host, and documented by
    proc_pid_fd(5)). Each of its watches is one line beginning "inotify
    wd:" in /proc/<pid>/fdinfo/<fd> (proc_pid_fdinfo(5), since Linux 3.8).

    /proc enumeration is inherently racy. A PID, fd, or fdinfo entry that
    disappears mid-scan (FileNotFoundError) is skipped - it no longer
    exists to count. Any other OSError (chiefly PermissionError), or a
    malformed Uid: value, on a directory/file belonging to a process
    ALREADY CONFIRMED to have the target real UID is not a
    vanished-object race: it means this measurement cannot be trusted,
    and fails closed rather than silently undercounting. Processes owned
    by a different real UID are skipped without inspecting their
    fd/fdinfo contents at all.
    """
    proc_root = proc_root or Path("/proc")
    real_uid = os.getuid() if real_uid is None else real_uid

    instances = 0
    watches = 0

    for pid_dir in sorted(proc_root.glob("[0-9]*")):
        try:
            status_text = (pid_dir / "status").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HostCheckError(f"could not read {pid_dir / 'status'}: {exc}") from exc

        pid_real_uid = None
        for line in status_text.splitlines():
            if line.startswith("Uid:"):
                fields_ = line.split()
                if len(fields_) < 2:
                    raise HostCheckError(f"{pid_dir / 'status'} has a malformed Uid: line")
                try:
                    pid_real_uid = int(fields_[1])
                except ValueError as exc:
                    raise HostCheckError(
                        f"{pid_dir / 'status'} has a non-integer Uid: value: {fields_[1]!r}"
                    ) from exc
                break
        if pid_real_uid is None:
            raise HostCheckError(f"{pid_dir / 'status'} has no Uid: line")
        if pid_real_uid != real_uid:
            continue

        fd_dir = pid_dir / "fd"
        try:
            fd_names = os.listdir(fd_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HostCheckError(
                f"could not list {fd_dir} for a process confirmed to have real UID "
                f"{real_uid}: {exc}"
            ) from exc

        for fd_name in fd_names:
            try:
                target = os.readlink(fd_dir / fd_name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise HostCheckError(
                    f"could not read {fd_dir / fd_name} for a process confirmed to have "
                    f"real UID {real_uid}: {exc}"
                ) from exc
            if target != "anon_inode:inotify":
                continue
            instances += 1

            fdinfo_path = pid_dir / "fdinfo" / fd_name
            try:
                fdinfo_text = fdinfo_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise HostCheckError(
                    f"could not read {fdinfo_path} for a process confirmed to have real "
                    f"UID {real_uid}: {exc}"
                ) from exc
            watches += sum(1 for line in fdinfo_text.splitlines()
                          if line.startswith("inotify wd:"))

    return instances, watches


def read_candidate_port_range(isolation_path: Path | None = None) -> tuple[int, int]:
    """Reads and validates config/isolation.json's candidate_tcp_port_range.

    Both endpoints must be genuine ints - not bools. Python's bool is a
    subclass of int, so isinstance(True, int) is True and a JSON `true`
    would silently pass an isinstance check; `type(x) is int` rejects it.
    The range must also be non-reversed and within the valid TCP port
    space (1..65535).
    """
    path = isolation_path or ISOLATION_CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HostCheckError(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise HostCheckError(f"{path} is not valid JSON: {exc}") from exc
    try:
        lo_raw, hi_raw = raw["candidate_tcp_port_range"]
    except (KeyError, ValueError, TypeError) as exc:
        raise HostCheckError(
            f"{path} has no valid two-element candidate_tcp_port_range: {exc}"
        ) from exc
    if type(lo_raw) is not int or type(hi_raw) is not int:
        raise HostCheckError(
            f"{path}'s candidate_tcp_port_range endpoints must both be plain integers "
            f"(not bool or other types), got {lo_raw!r}, {hi_raw!r}"
        )
    lo, hi = lo_raw, hi_raw
    if not (1 <= lo <= hi <= 65535):
        raise HostCheckError(
            f"{path}'s candidate_tcp_port_range [{lo}, {hi}] is not a valid "
            f"1..65535 non-reversed range"
        )
    return lo, hi


def count_bindable_ports(lo: int, hi: int, host: str = "127.0.0.1") -> int:
    """Counts ports in [lo, hi] bindable on `host` right now.

    Every successful bind is closed immediately (the `with` block calls
    close() on both the success and failure path). This proves only that
    these ports are free at this instant - it does not reserve them, and
    a counted port can be taken by another process the moment this
    function returns. Allocation/ownership is C-09, not this function.
    """
    free = 0
    for port in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                free += 1
            except OSError:
                continue
    return free


# ------------------------------------------------------------- pure evaluation

def cpu_ok(load1: float, cpus: int) -> bool:
    return load1 <= CPU_LOAD_FACTOR * cpus


def ram_ok(mem_available_bytes: int) -> bool:
    return mem_available_bytes >= RAM_MIN_BYTES


def disk_ok(free_bytes: int) -> bool:
    return free_bytes >= DISK_MIN_BYTES


def fd_ok(remaining_handles: int) -> bool:
    return remaining_handles >= FD_MIN_REMAINING


def inotify_watches_ok(watches_used: int, max_watches: int) -> bool:
    return watches_used <= INOTIFY_MAX_FRACTION * max_watches


def inotify_instances_ok(instances_used: int, max_instances: int) -> bool:
    return instances_used <= INOTIFY_MAX_FRACTION * max_instances


def ports_ok(free_count: int) -> bool:
    return free_count >= PORT_MIN_FREE


# ---------------------------------------------------------------- combined gate

def headroom_check(root: Path | None = None,
                   isolation_path: Path | None = None) -> CheckResult:
    """Evaluates all six signals. Fails closed per-signal on any
    HostCheckError and names every failing or unreadable signal - never
    stops at the first problem found.
    """
    root = root or config.REPO_ROOT
    problems: list[str] = []
    fields: dict = {}

    try:
        load1 = read_load1()
        cpus = logical_cpu_count()
        ok = cpu_ok(load1, cpus)
        fields["cpu"] = {"load1": load1, "logical_cpus": cpus,
                         "threshold": CPU_LOAD_FACTOR * cpus, "ok": ok}
        if not ok:
            problems.append(f"cpu: load1 {load1} exceeds {CPU_LOAD_FACTOR} x {cpus} cpus")
    except HostCheckError as exc:
        problems.append(f"cpu: {exc}")
        fields["cpu"] = {"error": str(exc)}

    try:
        mem_available = read_mem_available_bytes()
        ok = ram_ok(mem_available)
        fields["ram"] = {"mem_available_bytes": mem_available,
                         "threshold_bytes": RAM_MIN_BYTES, "ok": ok}
        if not ok:
            problems.append(f"ram: {mem_available} bytes available, below {RAM_MIN_BYTES}")
    except HostCheckError as exc:
        problems.append(f"ram: {exc}")
        fields["ram"] = {"error": str(exc)}

    try:
        free_bytes = read_disk_free_bytes(root)
        ok = disk_ok(free_bytes)
        fields["disk"] = {"free_bytes": free_bytes, "threshold_bytes": DISK_MIN_BYTES, "ok": ok}
        if not ok:
            problems.append(f"disk: {free_bytes} bytes free, below {DISK_MIN_BYTES}")
    except HostCheckError as exc:
        problems.append(f"disk: {exc}")
        fields["disk"] = {"error": str(exc)}

    try:
        allocated, legacy_free, max_handles = read_fd_capacity()
        remaining = max_handles - allocated
        ok = fd_ok(remaining)
        fields["fd"] = {"allocated_handles": allocated, "legacy_free_field": legacy_free,
                        "max_handles": max_handles, "remaining_handles": remaining,
                        "threshold": FD_MIN_REMAINING, "ok": ok}
        if not ok:
            problems.append(f"fd: {remaining} remaining, below {FD_MIN_REMAINING}")
    except HostCheckError as exc:
        problems.append(f"fd: {exc}")
        fields["fd"] = {"error": str(exc)}

    try:
        max_watches, max_instances = read_inotify_ceilings()
        instances_used, watches_used = current_real_uid_inotify_usage()
        watches_pass = inotify_watches_ok(watches_used, max_watches)
        instances_pass = inotify_instances_ok(instances_used, max_instances)
        ok = watches_pass and instances_pass
        fields["inotify"] = {"watches_used": watches_used, "max_user_watches": max_watches,
                             "instances_used": instances_used,
                             "max_user_instances": max_instances, "ok": ok}
        if not watches_pass:
            problems.append(
                f"inotify: {watches_used} watches exceed 50% of {max_watches}"
            )
        if not instances_pass:
            problems.append(
                f"inotify: {instances_used} instances exceed 50% of {max_instances}"
            )
    except HostCheckError as exc:
        problems.append(f"inotify: {exc}")
        fields["inotify"] = {"error": str(exc)}

    try:
        lo, hi = read_candidate_port_range(isolation_path)
        free_count = count_bindable_ports(lo, hi)
        ok = ports_ok(free_count)
        fields["ports"] = {"free_count": free_count, "range": [lo, hi],
                           "threshold": PORT_MIN_FREE, "ok": ok}
        if not ok:
            problems.append(f"ports: only {free_count} free in [{lo}, {hi}], need {PORT_MIN_FREE}")
    except HostCheckError as exc:
        problems.append(f"ports: {exc}")
        fields["ports"] = {"error": str(exc)}

    if problems:
        return CheckResult(False, "; ".join(problems), fields)
    return CheckResult(True, "all host-resource headroom checks passed", fields)
