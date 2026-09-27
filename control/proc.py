"""Process liveness that a zombie cannot fake.

`os.kill(pid, 0)` succeeds for a zombie: a killed child whose parent has not
reaped it keeps its PID table entry. A watchdog relying on that signal reports a
dead supervisor as healthy. This reads the process state instead, and treats
`Z` (zombie) and `X` (dead) as not running.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        state = _state(pid)
    except (OSError, ValueError):
        return False
    if state is None:
        # No procfs: fall back to the signal probe, which is better than nothing.
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    return state not in ("Z", "X")


def _state(pid: int) -> str | None:
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        if Path("/proc/self").exists():
            return "X"  # procfs exists and the pid does not
        return None
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return None


def _ticks_from_stat(text: str) -> int | None:
    """starttime (field 22) from /proc/<pid>/stat content.

    comm (field 2) may contain spaces and parentheses, so fields are taken
    after the LAST ')' - field 3 (state) is then index 0.
    """
    _, _, rest = text.rpartition(")")
    fields = rest.split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def start_ticks(pid: int | None) -> int | None:
    """The process's start time in clock ticks since boot - (pid, start_ticks)
    identifies one process for the lifetime of a boot, which a reused PID
    cannot fake. None when unreadable."""
    if not pid or pid <= 0:
        return None
    try:
        return _ticks_from_stat(
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verified_alive(pid: int | None, recorded_ticks) -> bool | None:
    """Is the recorded process itself still running?

    True  - running, and current start ticks equal the recorded ticks;
    False - not running, or running with provably different start ticks
            (a reused PID is NOT the recorded process);
    None  - running but unverifiable (no recorded ticks, or /proc stat
            unreadable). Callers decide the fail-closed direction: orphan
            DETECTION never claims an ambiguous process; governed
            RESOLUTION never treats one as absent.
    """
    if not is_running(pid):
        return False
    if recorded_ticks is None:
        return None
    current = start_ticks(pid)
    if current is None:
        return None
    return current == int(recorded_ticks)


def worker_entry_processes(job_dir) -> dict[str, int] | None:
    """Live worker-entry processes, identified by the run-unique absolute
    job-file path each carries in its own cmdline (both the tmux and the
    detached launch mode exec the same `worker_entry.py <job.json>`).

    Returns {worker_name: pid}. None means /proc itself could not be
    enumerated - callers must fail closed, never treat that as "none live".
    """
    root = Path("/proc")
    if not root.exists():
        return None
    marker = str(job_dir)
    found: dict[str, int] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # process vanished mid-scan - not a scan failure
        for arg in raw.decode("utf-8", errors="replace").split("\0"):
            if arg.startswith(marker) and arg.endswith(".job.json"):
                found[Path(arg).name[: -len(".job.json")]] = int(entry.name)
    return found


def _parse_listen_ports(text: str, lo: int, hi: int) -> set[int]:
    """LISTEN-state local ports in [lo, hi] from /proc/net/tcp{,6} content."""
    ports: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        try:
            port = int(fields[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if lo <= port <= hi:
            ports.add(port)
    return ports


def listening_ports(lo: int, hi: int) -> set[int] | None:
    """LISTEN ports in [lo, hi] across IPv4 AND IPv6. A dual-stack [::]
    listener occupies the IPv4 wildcard too (bindv6only=0 default), so
    reading /proc/net/tcp alone under-reports. None means the observation
    itself failed - callers must fail closed."""
    found: set[int] = set()
    read_any = False
    for name in ("tcp", "tcp6"):
        path = Path(f"/proc/net/{name}")
        if not path.exists():
            continue
        try:
            found |= _parse_listen_ports(path.read_text(encoding="utf-8"), lo, hi)
            read_any = True
        except OSError:
            return None
    return found if read_any else None


def reap_children() -> None:
    """Collect finished children so they cannot linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return
