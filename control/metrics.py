"""C-08a: runtime resource sampling - measurement only.

Protocol v2 ("Concurrency metrics") says "Track ... CPU/RAM/disk,
process/worktree/browser counts, inotify usage, port contention". This
module is the bounded sample builder for exactly that subset: it composes
the existing C-08c host readers (control/hostcheck.py) and C-09 resource
observation primitives (control/proc.py, control/reconcile.py,
control/workers.py) into one fixed-shape dict of finite scalars.

It is pure: it never mutates state, never writes the ledger, never
notifies, and never evaluates a threshold - thresholds belong to the
C-08c preflight gate, and consequences (freezes, escalations) belong to
the reconciliation/annunciation paths that already own them. The Watchdog
owns the governed sampling cadence, the RESOURCE_SAMPLE append and
top-level failure containment (control/watchdog.py).

Observation semantics: an unavailable observation is null plus an
explicit *_observed=false - never zero, never a guess. State-derived
counts (worker records, owned ports, ACTIVE/RETAINED worktrees) remain
recorded when a physical observation fails, because the state document in
hand still mechanically supports them. load1 is the system 1-minute load
average from /proc/loadavg - a run-queue length, NOT a CPU-utilisation
percentage; per-process CPU is not measured anywhere and not claimed.

browser_count is null with browser_observed=false until C-05 gives the
control plane actual browser visibility: browsers are launched entirely
by the Node accessibility runner today, so any number here would be
fabricated. Never record 0.

Peaks and averages are deliberately absent: the Protocol says "track",
and min/max/mean are computable at report time from the append-only
samples. Contention OCCURRENCES are not re-recorded here either - the
existing durable events (PORT_ALLOCATION_FAILED, PORT_ASSIGNMENT_CONFLICT,
FOREIGN_OR_ORPHAN_LISTENER) remain authoritative; this sample records
only the periodic levels.
"""

from __future__ import annotations

from pathlib import Path

from . import config, hostcheck, proc, reconcile
from . import workers as workers_mod

# Governed for Run 002 (human decision, 2026-09-28): sample immediately on
# Watchdog start, then no more often than every 300 monotonic seconds.
SAMPLE_INTERVAL_SECONDS = 300

# The complete finite RESOURCE_SAMPLE metadata key set. sample() returns
# exactly these keys, always - a class that could not be observed carries
# null values and *_observed=false, never a missing key and never zero.
METADATA_KEYS = frozenset({
    "load1", "logical_cpus", "cpu_observed",
    "mem_available_bytes", "ram_observed",
    "disk_free_bytes", "disk_observed",
    "inotify_instances_used", "inotify_watches_used",
    "inotify_max_instances", "inotify_max_watches", "inotify_observed",
    "ports_owned", "ports_listening_in_range",
    "port_range_lo", "port_range_hi", "ports_observed",
    "worker_records", "live_worker_entries", "verified_agents",
    "processes_observed",
    "worktrees_registered_managed", "worktrees_active",
    "worktrees_retained", "worktrees_orphan", "worktrees_observed",
    "browser_count", "browser_observed",
})


def sample(doc: dict, *, repo_root=None) -> dict:
    """One fixed-shape resource sample. Read-only over doc and the host."""
    repo_root = Path(repo_root) if repo_root is not None else config.REPO_ROOT
    out: dict = {}

    try:
        out["load1"] = hostcheck.read_load1()
        out["logical_cpus"] = hostcheck.logical_cpu_count()
        out["cpu_observed"] = True
    except hostcheck.HostCheckError:
        out.update(load1=None, logical_cpus=None, cpu_observed=False)

    try:
        out["mem_available_bytes"] = hostcheck.read_mem_available_bytes()
        out["ram_observed"] = True
    except hostcheck.HostCheckError:
        out.update(mem_available_bytes=None, ram_observed=False)

    try:
        out["disk_free_bytes"] = hostcheck.read_disk_free_bytes(repo_root)
        out["disk_observed"] = True
    except hostcheck.HostCheckError:
        out.update(disk_free_bytes=None, disk_observed=False)

    try:
        instances_used, watches_used = hostcheck.current_real_uid_inotify_usage()
        max_watches, max_instances = hostcheck.read_inotify_ceilings()
        out.update(inotify_instances_used=instances_used,
                   inotify_watches_used=watches_used,
                   inotify_max_instances=max_instances,
                   inotify_max_watches=max_watches, inotify_observed=True)
    except hostcheck.HostCheckError:
        out.update(inotify_instances_used=None, inotify_watches_used=None,
                   inotify_max_instances=None, inotify_max_watches=None,
                   inotify_observed=False)

    # Ports: ownership is state-derived and always countable; the level of
    # LISTEN sockets in the governed range comes from proc.listening_ports,
    # the C-09 primitive whose IPv4+IPv6 dual-stack merge is already pinned
    # by its own tests. Contention occurrences stay with their existing
    # durable events; this is the periodic level only.
    owned_ports = {meta.get("port")
                   for meta in doc.get("workers", {}).values()
                   if isinstance(meta.get("port"), int)}
    out["ports_owned"] = len(owned_ports)
    listening = None
    lo = hi = None
    try:
        lo, hi = hostcheck.read_candidate_port_range()
        listening = proc.listening_ports(lo, hi)
    except hostcheck.HostCheckError:
        lo = hi = None
    out.update(port_range_lo=lo, port_range_hi=hi,
               ports_listening_in_range=(len(listening)
                                         if listening is not None else None),
               ports_observed=listening is not None)

    # Processes: three deliberately separate truthful counts - committed
    # worker records (state-derived), live worker-entry processes
    # (cmdline-identified), and identity-verified surviving agents
    # ((agent_pid, agent_start_ticks), so a reused PID never counts). One
    # worker can appear in all three; summing them would count it thrice.
    out["worker_records"] = len(doc.get("workers", {}))
    entries = proc.worker_entry_processes(config.WORKER_LOG_DIR)
    out["live_worker_entries"] = len(entries) if entries is not None else None
    verified: int | None = 0
    try:
        status_paths = sorted(config.WORKER_LOG_DIR.glob("*.status.json"))
    except OSError:
        status_paths = None
    if status_paths is None:
        verified = None
    else:
        for status_path in status_paths:
            worker = status_path.name[: -len(".status.json")]
            status = workers_mod.read_status(worker) or {}
            if proc.verified_alive(status.get("agent_pid"),
                                   status.get("agent_start_ticks")) is True:
                verified += 1
    out["verified_agents"] = verified
    out["processes_observed"] = entries is not None and verified is not None

    # Worktrees: ACTIVE (claimed by a live worker record) and RETAINED
    # (claimed by a task's retained_worktrees) are state-derived and always
    # countable. Registered/orphan need the physical git observation; a
    # failed observation leaves them null - an orphan count must never be
    # inferred from evidence that was not actually gathered. Managed-root
    # scoping mirrors reconcile.detect_orphans exactly: the main checkout
    # and any unmanaged/unresolvable worktree are never Run 002 resources.
    active = {meta.get("worktree")
              for meta in doc.get("workers", {}).values()
              if meta.get("worktree")}
    retained: set = set()
    for task in doc.get("tasks", {}).values():
        retained.update(task.get("retained_worktrees") or {})
    out["worktrees_active"] = len(active)
    out["worktrees_retained"] = len(retained)
    ok, paths = reconcile.registered_worktrees(repo_root)
    if ok:
        managed_root = workers_mod.managed_worktree_root(repo_root).resolve()
        managed = []
        for path in paths:
            if path == str(repo_root):
                continue
            try:
                if Path(path).resolve().is_relative_to(managed_root):
                    managed.append(path)
            except OSError:
                continue  # unresolvable path is not provably Run 002-managed
        out["worktrees_registered_managed"] = len(managed)
        out["worktrees_orphan"] = len(
            [p for p in managed if p not in active and p not in retained])
        out["worktrees_observed"] = True
    else:
        out.update(worktrees_registered_managed=None, worktrees_orphan=None,
                   worktrees_observed=False)

    out["browser_count"] = None
    out["browser_observed"] = False
    return out
