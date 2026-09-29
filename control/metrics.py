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

browser_count comes from the C-05 evidence tree: persisted sidecars plus
(pid, start_ticks) verification. It is all-or-nothing - null with
browser_observed=false unless EVERY attempt was observable - because a
verified-live subset is not a smaller true count, it is an unknown count
with some known members, and publishing it would understate real browser
pressure exactly when observation is degraded. Never record a fabricated
zero; an observed zero is only ever a complete one.

Peaks and averages are deliberately absent: the Protocol says "track",
and min/max/mean are computable at report time from the append-only
samples. Contention OCCURRENCES are not re-recorded here either - the
existing durable events (PORT_ALLOCATION_FAILED, PORT_ASSIGNMENT_CONFLICT,
FOREIGN_OR_ORPHAN_LISTENER) remain authoritative; this sample records
only the periodic levels.
"""

from __future__ import annotations

from pathlib import Path

from . import config, gate_evidence, hostcheck, proc, reconcile
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


def _browser_levels() -> tuple[int | None, bool]:
    """(browser_count, browser_observed) from the persisted C-05 evidence.

    Browsers are launched entirely by the Node accessibility runner, so
    this is an INFERENCE - sidecars plus identity verification - and it is
    published only when every step of that inference held. Anything
    unknown collapses the whole sample, never a partial count.

    The count is of BROWSER PROCESSES, not of sidecar records. One browser
    can be described by more than one OPEN sidecar - a rerun that observed
    the same child, evidence copied between attempts - so identities are
    deduplicated by the exact (pid, start_ticks) pair C-09 uses. Counting
    records instead would inflate browser pressure out of bookkeeping
    rather than out of anything actually running.

    CLOSED is the one benign case: run.js writes it only after
    browser.close() has returned, so it is positive evidence of no live
    browser. It contributes zero, needs no identity, and poisons nothing.

    Terminality is deliberately not consulted. Whether an OPEN sidecar
    under a finished attempt is a LEAK is orphan classification, which
    belongs to reconcile.py; this is simply how many browsers are
    verifiably alive right now.

    No exception containment here on purpose: every reader in this chain
    is already total, and watchdog.record_resource_sample owns the
    top-level containment that turns any surprise into the finite
    METRICS_SAMPLE_ERROR pair. Catching here would hide it from that.
    """
    records, walk_ok = gate_evidence.scan_sidecars()
    if not walk_ok:
        return None, False
    live = 0
    seen: set[tuple[int, int]] = set()
    for record in records:
        if record["sidecar_status"] != gate_evidence.SIDECAR_OK:
            return None, False   # absent, unreachable or garbled: unknown
        if record["state"] == gate_evidence.CLOSED:
            continue             # proven shut; no identity needed
        if record["pid"] is None or record["start_ticks"] is None:
            return None, False   # OPEN with nothing to verify against
        identity = (record["pid"], record["start_ticks"])
        if identity in seen:
            continue             # one process, however many records name it
        alive = proc.verified_alive(*identity)
        if alive is None:
            return None, False   # running but unverifiable - never claimed
        seen.add(identity)
        if alive:
            live += 1            # False is provably gone, and counts zero
    return live, True


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

    # Browsers (C-05.2). See _browser_levels: all-or-nothing by design, and
    # counted by distinct (pid, start_ticks) identity rather than by record.
    out["browser_count"], out["browser_observed"] = _browser_levels()
    return out
