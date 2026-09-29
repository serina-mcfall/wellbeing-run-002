"""D2: independent Watchdog reconciliation of Supervisor's claimed
worker/task state against observable reality.

Watchdog's existing supervisor-process liveness check (control/watchdog.py)
proves the Supervisor PROCESS itself is alive. It does not prove the
CONTENT of state.json it maintains is still true. This module is that
second, independent check: it reads state.json's claims and compares
them against real PID liveness, real registered git worktrees, and
internal task<->worker cross-reference consistency, using the same raw
observation primitives already in the codebase (control/proc.py,
control/workers.py's read_status) rather than Supervisor's own derived
belief about a worker's health.

This module reports FACTS ONLY - it never mutates state.json, never
freezes a task, never sends a notification, and never repairs bookkeeping,
kills, or restarts a worker. Applying deterministic policy to these facts
(freezing a task on a dangerous finding) is control/watchdog.py's job, the
same separation Protocol v2 draws for Jev: "Jev classifies/recommends;
deterministic policy acts."

This deliberately does NOT re-implement control/supervisor.py's own
detect_stale (DEV-002), which already correctly distinguishes process
liveness from meaningful progress and is not being second-guessed here.
What is being checked is a different failure mode: whether Supervisor's
OWN bookkeeping is still consistent with reality at all, which a buggy or
drifted Supervisor could violate even while otherwise behaving correctly.

Never infer meaningful progress merely from process liveness (Protocol v2
"Worker awareness"): a live PID is one independent signal among several
here, never itself treated as proof that a claimed worker is doing
anything, and never used to infer a detailed worker phase.

Reconciliation only evaluates tasks in ASSIGNED, ACTIVE, REVIEW or
FIX_REQUIRED. The four are NOT alike (D2/C-14 amendment, 2026-09-27):
dispatch construction guarantees a live doc["workers"] record only for
ASSIGNED and ACTIVE, so only there is a missing/dangling task["worker"]
itself a contradiction. REVIEW and FIX_REQUIRED are routing states -
supervisor.py's route_awaiting_dispatch explicitly treats them with no
live worker as "waiting to redispatch next tick" (human RETRY resolutions
and dispatch failures legitimately commit that shape to disk between
ticks), and routing checks liveness from worker records, never from
task["worker"], whose stale value there is historical identity, not an
active-ownership claim. When a record IS present, every reality check
below still applies in all four states. Every other in-flight state
routinely has no live worker record as normal behaviour, so nothing is
checked there.

Every check below fails CLOSED, not open. Missing or malformed evidence
where evidence is expected - no task["worker"] in a reconcilable state,
no readable worker status, no heartbeat_at, an unparsable heartbeat_at, a
failed git worktree observation - is itself reported as a dangerous
finding, and is never silently treated as "nothing to report" or
converted into a stronger claim the evidence doesn't actually support
(a missing status file is not evidence the PID is dead; a failed git
call is not evidence the worktree is unregistered).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, gate_evidence, gh, hostcheck, proc, state
from . import workers as workers_mod

RECONCILABLE_STATES = frozenset({"ASSIGNED", "ACTIVE", "REVIEW", "FIX_REQUIRED"})

# Only dispatch construction for ASSIGNED/ACTIVE guarantees a live worker
# record; REVIEW/FIX_REQUIRED legitimately wait with none while routing
# redispatches (see the module docstring). Missing/dangling task["worker"]
# is a contradiction only here.
ACTIVE_WORKER_REQUIRED_STATES = frozenset({"ASSIGNED", "ACTIVE"})

# Bounded multiple of workers.HEARTBEAT_STALE_SECONDS, not the raw value,
# so an ordinary tick-timing gap is never flagged as dangerous - only a
# heartbeat that stays stale across several would-be-fresh windows.
HEARTBEAT_DANGER_MULTIPLE = 3
HEARTBEAT_DANGER_SECONDS = workers_mod.HEARTBEAT_STALE_SECONDS * HEARTBEAT_DANGER_MULTIPLE


@dataclass(frozen=True)
class Disagreement:
    check_id: str
    worker: str | None
    task_id: str | None
    dangerous: bool
    evidence: str


def registered_worktrees(repo_root) -> tuple[bool, set[str]]:
    """(observation_ok, paths). observation_ok=False means git itself
    failed to answer - callers must not treat that the same as "git
    answered and found zero worktrees"."""
    result = gh.git(["worktree", "list", "--porcelain"], str(repo_root))
    if not result.ok:
        return False, set()
    paths = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(line.split(" ", 1)[1].strip())
    return True, paths


def reconcile(doc: dict, *, repo_root=None, tz: str = "Pacific/Auckland") -> list[Disagreement]:
    """Read-only. Never mutates doc, never touches the ledger, never
    notifies. Returns every observed disagreement, dangerous or not."""
    repo_root = repo_root or config.REPO_ROOT
    findings: list[Disagreement] = []

    # Lazy, cached per call: at most one `git worktree list` regardless of
    # how many workers claim a worktree, and a failed observation is
    # cached as failed rather than retried into a false empty success.
    worktree_cache: dict = {"computed": False, "ok": False, "paths": set()}

    def worktree_observation() -> tuple[bool, set[str]]:
        if not worktree_cache["computed"]:
            ok, paths = registered_worktrees(repo_root)
            worktree_cache.update(computed=True, ok=ok, paths=paths)
        return worktree_cache["ok"], worktree_cache["paths"]

    for task_id, task in doc.get("tasks", {}).items():
        if task.get("state") not in RECONCILABLE_STATES:
            continue

        worker_id = task.get("worker")
        if not worker_id:
            # Dispatch construction guarantees task["worker"] is set for
            # ASSIGNED/ACTIVE, so its absence there is a contradiction. In
            # REVIEW/FIX_REQUIRED it is ordinary waiting-to-redispatch
            # (see the module docstring) - no worker-specific observation
            # is possible or owed.
            if task["state"] in ACTIVE_WORKER_REQUIRED_STATES:
                findings.append(Disagreement(
                    check_id="MISSING_TASK_WORKER_REF", worker=None, task_id=task_id,
                    dangerous=True,
                    evidence=(f'{task_id} is "{task["state"]}", which guarantees a worker '
                              "was dispatched, but task[\"worker\"] is missing or empty."),
                ))
            continue

        worker = doc.get("workers", {}).get(worker_id)
        if worker is None:
            # Same split: a dangling reference is a contradiction only
            # where an active record is mandatory; in REVIEW/FIX_REQUIRED
            # the stale name is historical identity while routing waits.
            if task["state"] in ACTIVE_WORKER_REQUIRED_STATES:
                findings.append(Disagreement(
                    check_id="ORPHANED_TASK_WORKER_REF", worker=worker_id, task_id=task_id,
                    dangerous=True,
                    evidence=(f'{task_id} is "{task["state"]}" and claims worker '
                              f'"{worker_id}", but no such worker record exists in '
                              'doc["workers"].'),
                ))
            continue  # nothing further is checkable without a worker record

        if worker.get("task_id") != task_id:
            findings.append(Disagreement(
                check_id="WORKER_TASK_BACKREF_MISMATCH", worker=worker_id, task_id=task_id,
                dangerous=True,
                evidence=(f'{task_id} claims worker "{worker_id}", but that worker record '
                          f'claims task_id {worker.get("task_id")!r}.'),
            ))

        # read_status() collapses "no such file" and "file exists but
        # unreadable/malformed" into the same None - it does not expose
        # which happened, so this cannot distinguish them either. Either
        # way: no status means no PID/heartbeat evidence at all, which is
        # its own dangerous finding, never converted into a claim (dead or
        # alive) the missing evidence doesn't support.
        status = workers_mod.read_status(worker_id)
        if status is None:
            findings.append(Disagreement(
                check_id="MISSING_WORKER_STATUS", worker=worker_id, task_id=task_id,
                dangerous=True,
                evidence=(f'{task_id} is "{task["state"]}" with worker "{worker_id}", but no '
                          "readable status file exists for it - PID and heartbeat evidence "
                          "are unavailable, not confirmed healthy."),
            ))
        else:
            pid_alive = proc.is_running(status.get("agent_pid"))
            if not pid_alive:
                findings.append(Disagreement(
                    check_id="DEAD_PID_CLAIMED_ALIVE", worker=worker_id, task_id=task_id,
                    dangerous=True,
                    evidence=(f'{task_id} is "{task["state"]}" with worker "{worker_id}", but '
                              f'its reported agent_pid {status.get("agent_pid")!r} is not a '
                              "running process."),
                ))
            else:
                heartbeat_at = status.get("heartbeat_at")
                if not heartbeat_at:
                    findings.append(Disagreement(
                        check_id="MISSING_HEARTBEAT_WHILE_PID_ALIVE", worker=worker_id,
                        task_id=task_id, dangerous=True,
                        evidence=(f'worker "{worker_id}" PID is alive but its status has no '
                                  "heartbeat_at at all."),
                    ))
                else:
                    try:
                        seen = datetime.fromisoformat(heartbeat_at)
                    except ValueError:
                        findings.append(Disagreement(
                            check_id="MALFORMED_HEARTBEAT_WHILE_PID_ALIVE", worker=worker_id,
                            task_id=task_id, dangerous=True,
                            evidence=(f'worker "{worker_id}" PID is alive but its '
                                      f'heartbeat_at {heartbeat_at!r} is not a parsable '
                                      "timestamp."),
                        ))
                    else:
                        age = (datetime.now(ZoneInfo(tz)) - seen).total_seconds()
                        if age > HEARTBEAT_DANGER_SECONDS:
                            findings.append(Disagreement(
                                check_id="HEARTBEAT_STALE_WHILE_PID_ALIVE", worker=worker_id,
                                task_id=task_id, dangerous=True,
                                evidence=(f'worker "{worker_id}" PID is alive but its '
                                          f'heartbeat is {age:.0f}s old (> '
                                          f'{HEARTBEAT_DANGER_SECONDS}s bound).'),
                            ))

        claimed_worktree = worker.get("worktree")
        if claimed_worktree:
            observation_ok, live_worktrees = worktree_observation()
            if not observation_ok:
                findings.append(Disagreement(
                    check_id="WORKTREE_OBSERVATION_FAILED", worker=worker_id, task_id=task_id,
                    dangerous=True,
                    evidence=(f'could not independently verify worker "{worker_id}"\'s '
                              f'claimed worktree "{claimed_worktree}": git worktree list '
                              "failed."),
                ))
            elif claimed_worktree not in live_worktrees:
                findings.append(Disagreement(
                    check_id="WORKTREE_NOT_REGISTERED", worker=worker_id, task_id=task_id,
                    dangerous=True,
                    evidence=(f'worker "{worker_id}" claims worktree "{claimed_worktree}", '
                              "which git does not list as a registered worktree."),
                ))

        # Currently dormant in normal Run 002 operation: supervisor.py does
        # not yet populate last_meaningful_progress_at anywhere (verified
        # by grep). This branch is a real, tested comparison against
        # whatever value is present - it is not proof that live
        # meaningful-progress reconciliation is operational, and D2 does
        # not populate that field merely to activate it. Absence of a
        # claim is not itself a mismatch; only a present, unsupported
        # claim is.
        last_progress = worker.get("last_meaningful_progress_at")
        if last_progress:
            output_path = config.WORKER_LOG_DIR / f"{worker_id}.out"
            try:
                mtime = output_path.stat().st_mtime
                claimed_epoch = datetime.fromisoformat(last_progress).timestamp()
            except (OSError, ValueError):
                pass  # no output file yet, or an unparsable timestamp - not itself a mismatch
            else:
                if claimed_epoch > mtime + 1:  # small slack for filesystem/clock resolution
                    findings.append(Disagreement(
                        check_id="PROGRESS_CLAIM_UNSUPPORTED_BY_OUTPUT_FILE",
                        worker=worker_id, task_id=task_id, dangerous=True,
                        evidence=(f'worker "{worker_id}" claims meaningful progress at '
                                  f'{last_progress}, newer than its output file\'s real '
                                  "mtime."),
                    ))

    return findings


# ------------------------------------------------------------- C-09 orphans
#
# The reverse (reality -> state) half of resource reconciliation: physical
# resources with no durable owner. Facts only, exactly like reconcile()
# above - detection is not repair, and annunciation policy (dedup, ledger,
# notification) is control/watchdog.py's job. Every scan reports its own
# ok flag so a failed observation is never mistaken for "nothing there",
# and a failed scan never justifies clearing previously observed evidence.


@dataclass(frozen=True)
class OrphanFinding:
    check_id: str
    resource_id: str
    evidence: str


def _attempt_terminal(record: dict, doc: dict) -> bool | None:
    """Has the attempt that launched this browser finished?

    True, False, or None for "cannot tell" - and None is never promoted
    into either answer, because both directions are wrong in a way that
    matters: a false True invents a leak, a false False hides one.

    The RUNNING marker is authoritative and is never overridden by task
    state. A task can be COMPLETE while its last attempt's browser is
    still winding down, and preferring the task there would turn a
    perfectly ordinary in-flight browser into a reported orphan.

    Task state is consulted ONLY as a fallback when no marker exists at
    all, and only in the direction that can prove termination. An absent
    task proves nothing - evidence outliving its task is exactly where
    guessing is least safe - and a merely non-terminal task does not
    prove its attempt is still running either.
    """
    if record["lifecycle"] == gate_evidence.TERMINAL:
        return True
    if record["lifecycle"] == gate_evidence.RUNNING:
        return False
    task = doc.get("tasks", {}).get(record["task_id"])
    if not isinstance(task, dict):
        return None
    if task.get("state") in state.TERMINAL_STATES:
        return True
    return None


def detect_orphans(doc: dict, *, repo_root=None) -> tuple[list[OrphanFinding],
                                                          dict[str, bool]]:
    repo_root = repo_root or config.REPO_ROOT
    findings: list[OrphanFinding] = []
    scan_ok = {"worktree": False, "process": False, "port": False,
               "browser": False}

    # Worktrees: every git-registered worktree under the workmux-managed
    # root (the sibling '<project>__worktrees' directory) must be claimed
    # by a live worker record or a task's retained_worktrees map (ACTIVE /
    # RETAINED / else orphan). The main checkout and any manual/unrelated
    # worktree outside that root are not Run 002 resources and are never
    # classified - detection must not over-claim.
    ok, paths = registered_worktrees(repo_root)
    if ok:
        scan_ok["worktree"] = True
        managed_root = workers_mod.managed_worktree_root(repo_root).resolve()
        active = {meta.get("worktree")
                  for meta in doc.get("workers", {}).values()
                  if meta.get("worktree")}
        retained: set[str] = set()
        for task in doc.get("tasks", {}).values():
            retained.update(task.get("retained_worktrees") or {})
        for path in sorted(paths):
            if path == str(repo_root):
                continue
            try:
                if not Path(path).resolve().is_relative_to(managed_root):
                    continue
            except OSError:
                continue  # unresolvable path is not provably Run 002-managed
            if path in active or path in retained:
                continue
            findings.append(OrphanFinding(
                "ORPHAN_WORKTREE", path,
                f"registered worktree {path} has no live worker owner and "
                f"no task retained_worktrees claim"))

    # Processes. P1: a live worker-entry (identified by the run-unique job
    # path in its own cmdline) with no worker record and a non-terminal
    # status. P2: the entry is gone but the recorded agent survives,
    # claimed ONLY under identity verification by (agent_pid, start ticks)
    # - a reused PID or missing ticks is never claimed.
    entries = proc.worker_entry_processes(config.WORKER_LOG_DIR)
    if entries is not None:
        try:
            status_paths = sorted(config.WORKER_LOG_DIR.glob("*.status.json"))
        except OSError:
            status_paths = None
        if status_paths is not None:
            scan_ok["process"] = True
            recorded = doc.get("workers", {})
            for worker, pid in sorted(entries.items()):
                if worker in recorded:
                    continue
                status = workers_mod.read_status(worker) or {}
                if status.get("phase") in ("DONE", "FAILED", "TIMEOUT"):
                    continue  # finished; the entry is exiting momentarily
                findings.append(OrphanFinding(
                    "ORPHAN_WORKER_PROCESS", worker,
                    f"worker-entry pid {pid} runs job {worker} but no "
                    f"doc[\"workers\"] record exists for it"))
            for status_path in status_paths:
                worker = status_path.name[: -len(".status.json")]
                if worker in recorded or worker in entries:
                    continue
                status = workers_mod.read_status(worker)
                if not status or status.get("phase") in ("DONE", "FAILED",
                                                         "TIMEOUT"):
                    continue
                if proc.verified_alive(status.get("agent_pid"),
                                       status.get("agent_start_ticks")) is True:
                    findings.append(OrphanFinding(
                        "ORPHAN_AGENT_PROCESS", worker,
                        f"agent pid {status.get('agent_pid')} for {worker} is "
                        f"identity-verified alive but its worker-entry and "
                        f"worker record are both gone"))

    # Ports: LISTEN sockets (IPv4 + IPv6 merged - a dual-stack [::]
    # listener occupies the IPv4 wildcard) inside the run's declared
    # exclusive candidate range, minus live worker ownership. Assignment
    # invariants (duplicates) are also surfaced here.
    try:
        lo, hi = hostcheck.read_candidate_port_range()
    except hostcheck.HostCheckError:
        lo = hi = None
    if lo is not None:
        listening = proc.listening_ports(lo, hi)
        if listening is not None:
            scan_ok["port"] = True
            owners: dict[int, list[str]] = {}
            for worker, meta in doc.get("workers", {}).items():
                port = meta.get("port")
                if isinstance(port, int):
                    owners.setdefault(port, []).append(worker)
            for port, names in sorted(owners.items()):
                if len(names) > 1:
                    findings.append(OrphanFinding(
                        "PORT_ASSIGNMENT_CONFLICT", str(port),
                        f"port {port} is assigned to more than one live "
                        f"worker record: {', '.join(sorted(names))}"))
            for port in sorted(listening - set(owners)):
                findings.append(OrphanFinding(
                    "FOREIGN_OR_ORPHAN_LISTENER", str(port),
                    f"port {port} is listening inside the declared candidate "
                    f"range [{lo}, {hi}] but no live worker record owns it"))

    # Browsers (C-05.2). Reality -> state, like the classes above: a
    # browser verifiably alive after every attempt that named it has
    # verifiably finished has no durable owner. Detection only - nothing
    # here kills a process, closes a browser or unlinks evidence.
    #
    # scan_ok["browser"] is what lets the Watchdog CLEAR a previously
    # observed browser finding, so it may only stand when the walk
    # finished, every sidecar parsed, every OPEN identity is usable, every
    # liveness answer was definite, and every live browser's ownership was
    # settled. CLOSED records need none of it - run.js writes CLOSED only
    # after browser.close() returned.
    records, browser_ok = gate_evidence.scan_sidecars()
    # Two distinct questions, and conflating them is how a leak gets
    # reported that is not one. browser_ok asks "is anything about the
    # browsers uncertain?". identity_coverage asks the narrower, harsher
    # question "could an attempt exist whose browser IDENTITY we never
    # learned?" - because orphanhood is a claim about the ABSENCE of an
    # owner, and absence cannot be proven from a partial roll-call.
    identity_coverage = browser_ok
    if not browser_ok:
        findings.append(OrphanFinding(
            "BROWSER_EVIDENCE_UNREADABLE", str(config.EVIDENCE_DIR),
            "the accessibility evidence tree could not be fully walked, so "
            "whether any browser was left behind is unknown"))

    # Grouped by identity BEFORE anything is decided. One process can be
    # named by several attempts - a rerun that observed the same child,
    # evidence copied between attempts - and those records can disagree
    # about whether their attempt finished. Deciding per record would let
    # directory sort order pick the answer: RUNNING first would silence a
    # leak, TERMINAL first would report one, from identical evidence.
    candidates: dict[tuple[int, int], list[dict]] = {}
    for record in records:
        attempt = record["attempt_dir"]
        if record["sidecar_status"] != gate_evidence.SIDECAR_OK:
            browser_ok = identity_coverage = False
            findings.append(OrphanFinding(
                "BROWSER_EVIDENCE_UNREADABLE", attempt,
                f"browser sidecar for {attempt} is "
                f"{record['sidecar_status']}, so whether that attempt left a "
                f"browser running cannot be determined"))
            continue
        if record["state"] == gate_evidence.CLOSED:
            continue
        pid, ticks = record["pid"], record["start_ticks"]
        if pid is None or ticks is None:
            browser_ok = identity_coverage = False
            findings.append(OrphanFinding(
                "BROWSER_IDENTITY_UNVERIFIABLE", attempt,
                f"OPEN browser sidecar for {attempt} records no usable "
                f"(pid, start_ticks), so no process can be identified"))
            continue
        candidates.setdefault((pid, ticks), []).append(record)

    for (pid, ticks), naming in sorted(candidates.items()):
        alive = proc.verified_alive(pid, ticks)
        if alive is None:
            browser_ok = False  # the identity is KNOWN; it hides nobody else
            findings.append(OrphanFinding(
                "BROWSER_IDENTITY_UNVERIFIABLE", f"{pid}:{ticks}",
                f"OPEN browser pid {pid} is running but could not be "
                f"identity-verified against its recorded start ticks"))
            continue
        if not alive:
            # Benign stale evidence: the process this sidecar names is
            # provably gone. Nothing leaked, nothing to annunciate, and
            # nothing here makes the scan less complete. Deliberately NOT
            # a finding - every finding reaches a human as an orphan
            # alert, and a leftover file is not a resource problem.
            continue
        # Ownership, aggregated over every record naming this ONE process,
        # most conservative answer first. any() over the whole list, never
        # a per-record short-circuit, so order cannot reach the decision.
        terminality = [_attempt_terminal(r, doc) for r in naming]
        if any(t is False for t in terminality):
            continue  # an attempt is still in flight and owns it
        if any(t is None for t in terminality):
            browser_ok = False  # known identity, unsettled ownership
            findings.append(OrphanFinding(
                "BROWSER_TERMINALITY_UNKNOWN", f"{pid}:{ticks}",
                f"browser pid {pid} is identity-verified alive but whether "
                f"every attempt naming it has finished could not be "
                f"established, so it is neither claimed nor cleared"))
            continue
        if not identity_coverage:
            # Every attempt we COULD read is terminal - but an attempt we
            # could not read, or one whose sidecar named no process, may
            # be a current owner of this exact browser. "No attempt owns
            # it" is then unproven, and this is the one claim that must
            # never be made on incomplete evidence: a false orphan sends a
            # human hunting a process that is doing its job. The coverage
            # failure already has its own finding and browser_ok is
            # already False, so nothing is added here - the uncertainty is
            # recorded once, where it was observed.
            continue
        findings.append(OrphanFinding(
            "ORPHAN_BROWSER_PROCESS", f"{pid}:{ticks}",
            f"browser pid {pid} is identity-verified alive and every attempt "
            f"naming it is proven terminal (first: "
            f"{min(r['attempt_dir'] for r in naming)}); no attempt owns it"))
    scan_ok["browser"] = browser_ok

    return findings, scan_ok
