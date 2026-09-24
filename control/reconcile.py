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
FIX_REQUIRED - the only states where supervisor.py's own dispatch code
(verified directly, not assumed) guarantees a live doc["workers"] record
exists for the task's entire time in that state. Every other in-flight
state routinely has no live worker record as normal behaviour, so nothing
is checked there; see control/state.py's FROZEN comment for the full
per-state accounting.

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
from zoneinfo import ZoneInfo

from . import config, gh, proc
from . import workers as workers_mod

RECONCILABLE_STATES = frozenset({"ASSIGNED", "ACTIVE", "REVIEW", "FIX_REQUIRED"})

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
            # Dispatch construction guarantees task["worker"] is set the
            # moment a task enters one of these states (verified against
            # supervisor.py directly - see control/state.py's FROZEN
            # comment). Its absence here is itself a contradiction, not
            # an ordinary "nothing dispatched yet" case.
            findings.append(Disagreement(
                check_id="MISSING_TASK_WORKER_REF", worker=None, task_id=task_id,
                dangerous=True,
                evidence=(f'{task_id} is "{task["state"]}", which guarantees a worker was '
                          "dispatched, but task[\"worker\"] is missing or empty."),
            ))
            continue  # no worker-specific observation is possible without a worker_id

        worker = doc.get("workers", {}).get(worker_id)
        if worker is None:
            findings.append(Disagreement(
                check_id="ORPHANED_TASK_WORKER_REF", worker=worker_id, task_id=task_id,
                dangerous=True,
                evidence=(f'{task_id} is "{task["state"]}" and claims worker "{worker_id}", '
                          'but no such worker record exists in doc["workers"].'),
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
