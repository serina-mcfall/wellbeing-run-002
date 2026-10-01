"""Durable supervisor state: tasks, dependencies, workers, PRs, locks.

State lives in a single JSON document written atomically under an exclusive
lock, so the supervisor can be killed at any instant and rebuilt from Git plus
this file plus the ledger. The ledger stays authoritative for *what happened*;
this file holds *where we are now*.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import config

# Task states from agents/SUPERVISOR.md plus the v1.0 migration-lock state.
# Extended per Protocol v2's fuller state machine: PR_OPEN -> WAITING_CI/
# WAITING_EVIDENCE -> REVIEW -> FIX_REQUIRED -> REVIEW -> MERGE_READY ->
# MERGED -> COMPLETE. WAITING_CI closes the Run 001 CI-dispatch race named
# in Protocol v2's audit mapping; WAITING_EVIDENCE is its accessibility/
# security-evidence counterpart. Both are additive only.
TASK_STATES = (
    "QUEUED",
    "READY",
    "ASSIGNED",
    "ACTIVE",
    "PR_OPEN",
    "WAITING_CI",
    "WAITING_EVIDENCE",
    "REVIEW",
    "FIX_REQUIRED",
    "MERGE_READY",
    "MERGED",
    "COMPLETE",
    "BLOCKED",
    "STALE",
    "FAILED",
    "WAITING_PROVIDER_RESET",
    "WAITING_DB_LOCK",
    "HUMAN_REQUIRED",
    # Protocol v2 "State machine" names FROZEN as one of four exception
    # states (HUMAN_REQUIRED, BLOCKED, FAILED, FROZEN), but nothing built
    # it until D2 (Watchdog reconciliation): "Dangerous mismatch freezes
    # the affected task and emits STATE_INVARIANT_VIOLATION." Distinct
    # from the other three: FAILED means the WORK failed; BLOCKED means
    # an ordinary, expected condition (a dependency/lock); HUMAN_REQUIRED
    # is used for many unrelated escalations elsewhere. FROZEN means
    # Supervisor's own bookkeeping was independently caught contradicting
    # observable reality - a control-plane integrity break, regardless of
    # whether the underlying work is good or bad. Terminal for
    # deterministic automation: only FROZEN -> HUMAN_REQUIRED is legal,
    # mirroring FAILED -> HUMAN_REQUIRED; nothing in this codebase moves a
    # task out of FROZEN automatically, even if PID/heartbeat reality
    # later looks healthy again. A human decides the real next state from
    # HUMAN_REQUIRED, same as any other exception state.
    "FROZEN",
)

# Worker-level phase vocabulary frozen by Protocol v2 ("Worker awareness").
# This is the TARGET vocabulary for explicit self-reported phase. No current
# worker (worker_entry.py) reports any of these values — it only knows
# STARTING/RUNNING/PROMPT_ACCEPTED/DONE/FAILED/TIMEOUT, which is process
# activity, not phase. Do not derive a WORKER_PHASES value from role +
# raw phase: that would assert knowledge the Supervisor does not have.
WORKER_PHASES = (
    "STARTING",
    "READING",
    "PLANNING",
    "IMPLEMENTING",
    "TESTING",
    "COMMITTING",
    "PUSHING",
    "WAITING_CI",
    "REVIEWING",
    "FIXING",
    "ACCESSIBILITY_TESTING",
    "SECURITY_REVIEWING",
    "COMPLETED",
    "FAILED",
)

TERMINAL_STATES = frozenset({"COMPLETE", "FAILED"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"READY", "BLOCKED", "HUMAN_REQUIRED", "FAILED"}),
    "READY": frozenset(
        {"ASSIGNED", "QUEUED", "BLOCKED", "WAITING_PROVIDER_RESET", "WAITING_DB_LOCK",
         "HUMAN_REQUIRED", "FAILED"}
    ),
    "ASSIGNED": frozenset({"ACTIVE", "READY", "STALE", "FAILED", "HUMAN_REQUIRED", "FROZEN"}),
    "ACTIVE": frozenset(
        {"PR_OPEN", "STALE", "FAILED", "BLOCKED", "READY", "WAITING_PROVIDER_RESET",
         "WAITING_DB_LOCK", "HUMAN_REQUIRED", "FROZEN"}
    ),
    "PR_OPEN": frozenset({"REVIEW", "WAITING_CI", "WAITING_EVIDENCE", "STALE", "FAILED",
                          "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"}),
    "WAITING_CI": frozenset({"REVIEW", "WAITING_EVIDENCE", "STALE", "FAILED",
                             "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"}),
    # FIX_REQUIRED added by C-05.3 governance (2026-09-30). A specialized
    # evidence FAIL on the current head means that head needs remediation
    # before a reviewer spends a cycle on it. The loop is
    # WAITING_EVIDENCE -> FIX_REQUIRED -> PR_OPEN -> WAITING_EVIDENCE, with
    # the required evidence rerun for the NEW head; a prior SHA's pass
    # never carries forward. REVIEW is reached only once every required
    # current-SHA evidence class has passed. Evidence is not merge
    # authority - the reviewer remains the acceptance authority.
    "WAITING_EVIDENCE": frozenset({"REVIEW", "FIX_REQUIRED", "STALE", "FAILED",
                                   "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"}),
    "REVIEW": frozenset(
        {"FIX_REQUIRED", "MERGE_READY", "MERGED", "PR_OPEN", "STALE", "FAILED",
         "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET", "FROZEN"}
    ),
    "FIX_REQUIRED": frozenset(
        {"REVIEW", "PR_OPEN", "STALE", "FAILED", "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET",
         "FROZEN"}
    ),
    "MERGE_READY": frozenset({"MERGED", "REVIEW", "STALE", "FAILED", "HUMAN_REQUIRED"}),
    "MERGED": frozenset({"COMPLETE", "HUMAN_REQUIRED"}),
    "COMPLETE": frozenset(),
    "BLOCKED": frozenset({"READY", "QUEUED", "HUMAN_REQUIRED", "FAILED"}),
    "STALE": frozenset({"READY", "ASSIGNED", "ACTIVE", "FAILED", "HUMAN_REQUIRED"}),
    "FAILED": frozenset({"HUMAN_REQUIRED"}),
    "WAITING_PROVIDER_RESET": frozenset({"READY", "ACTIVE", "REVIEW", "FIX_REQUIRED",
                                         "HUMAN_REQUIRED", "FAILED"}),
    "WAITING_DB_LOCK": frozenset({"READY", "ACTIVE", "HUMAN_REQUIRED", "FAILED"}),
    "HUMAN_REQUIRED": frozenset(set(TASK_STATES)),
    # Terminal for deterministic automation: only a human-driven escalation
    # out, never a direct recovery back to READY/ACTIVE/etc. Inbound edges
    # are deliberately limited to ASSIGNED/ACTIVE/REVIEW/FIX_REQUIRED - the
    # reconcilable states. They are not alike (D2/C-14 amendment,
    # 2026-09-27): dispatch construction guarantees a live doc["workers"]
    # record only for ASSIGNED and ACTIVE, so only there is a missing or
    # dangling task["worker"] itself a freezable contradiction. REVIEW and
    # FIX_REQUIRED are routing/waiting states that may legitimately have no
    # live worker between Supervisor ticks (route_awaiting_dispatch
    # redispatches next tick), so absence alone never freezes them - but
    # when a worker record DOES exist there, reconciliation still validates
    # its back-reference, status, PID, heartbeat and worktree, and any of
    # those contradictions can freeze. Every other in-flight state (PR_OPEN,
    # WAITING_CI, WAITING_EVIDENCE, MERGE_READY) routinely has no live worker
    # record as NORMAL behaviour - the prior worker has already been reaped
    # and a new one not yet dispatched - so "no live worker" there is not a
    # violation and must not freeze the task. STALE already has its own
    # correct, separate mechanism (DEV-002) that removes the worker as part
    # of the same recycling action, leaving nothing for D2 to independently
    # catch. COMPLETE/MERGED are excluded outright: Protocol v2 does not
    # require post-completion reconciliation, so a completed task is never
    # mutated over stale historical worker/process evidence.
    "FROZEN": frozenset({"HUMAN_REQUIRED"}),
}


class TransitionError(RuntimeError):
    pass


def _now(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


def initial_document(experiment_id: str, protocol_version: str) -> dict:
    return {
        "version": 1,
        "experiment_id": experiment_id,
        "protocol_version": protocol_version,
        "started_at": None,
        "frozen_at": None,
        "baseline_sha": None,
        "tasks": {},
        "providers": {},
        # C-19: `reservations` holds conservative allowances committed before a
        # paid request is sent. Readers use budget.reservations(doc), which
        # setdefaults, so a document written before this key existed still
        # works unchanged.
        "budget": {"total_usd": 0.0, "spent_usd": 0.0, "estimated_usd": 0.0,
                   "thresholds_crossed": [], "hard_stop": False,
                   "reservations": {}},
        "migration_lock": {"state": "FREE", "owner_task": None, "owner_pr": None,
                           "acquired_at": None, "waiters": [], "contention_events": 0},
        "workers": {},
        "prs": {},
        "interventions": {},
        "debt": {},
        "counters": {"human_interventions": 0, "guardrail_activations": 0,
                     "merge_approvals_invalidated": 0, "throttle_events": 0,
                     "recoveries": 0, "migration_lock_waits": 0},
        "review_queue_depth_history": [],
        "checkpoints_sent": [],
        # C-18 stage 2. Notification intents, committed with the state change
        # that produced them and delivered afterwards with no lock held.
        # Readers use notify.queue(doc), which setdefaults, so a document
        # written before this key existed still works unchanged.
        "notifications": {},
    }


class Store:
    """Read-modify-write access to the state document, serialised by a lock file."""

    def __init__(self, path: Path | None = None, tz: str = "Pacific/Auckland") -> None:
        self.path = path or config.STATE_PATH
        self.lock_path = self.path.with_suffix(".lock")
        self.tz = tz
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists()

    def initialise(self, experiment_id: str, protocol_version: str) -> dict:
        doc = initial_document(experiment_id, protocol_version)
        self._write(doc)
        return doc

    def read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    @contextmanager
    def transaction(self) -> Iterator[dict]:
        self.lock_path.touch(exist_ok=True)
        with open(self.lock_path, "r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                doc = self.read()
                yield doc
                self._write(doc)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, doc: dict) -> None:
        directory = self.path.parent
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False
        ) as tmp:
            json.dump(doc, tmp, indent=2, sort_keys=False, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.replace(temp_name, self.path)


def add_task(doc: dict, task_id: str, title: str, depends_on: list[str], kind: str,
             schema_changing: bool, tz: str) -> dict:
    record = {
        "id": task_id,
        "title": title,
        "depends_on": list(depends_on),
        "kind": kind,
        "schema_changing": bool(schema_changing),
        "state": "QUEUED",
        "worker": None,
        "branch": None,
        "pr": None,
        "attempts": 0,
        "repair_cycles": 0,
        "assigned_at": None,
        "last_progress_at": None,
        "progress_marker": None,
        "updated_at": _now(tz),
        "history": [],
    }
    doc["tasks"][task_id] = record
    return record


def new_worker_record(role: str, task_id: str, branch: str, started_at: str, *,
                       pr: int | None = None, worktree: str | None = None,
                       browser_profile: str | None = None, port: int | None = None,
                       lease_expires_at: str | None = None) -> dict:
    """The three Protocol v2 worker-awareness signals, kept distinct:

    - process activity/liveness: last_process_activity_at + process_phase —
      the coarse STARTING/RUNNING/PROMPT_ACCEPTED/DONE/FAILED/TIMEOUT signal
      worker_entry.py's heartbeat/PID detection actually reports. Never
      upgraded into a WORKER_PHASES value.
    - meaningful progress: last_meaningful_progress_at — set only when the
      task's own progress_marker changes (reap_workers()'s existing signal).
    - explicitly reported worker phase: reported_phase — left None here.
      No current worker writes a WORKER_PHASES value; None is the honest
      state, not a guess derived from role.

    browser_profile/port/lease_expires_at exist as fields because Protocol
    v2 requires tracking them; no population logic for them is implemented
    in D1 — they are None until whatever dispatches that resource sets them.
    """
    return {
        "role": role,
        "task_id": task_id,
        "branch": branch,
        "pr": pr,
        "started_at": started_at,
        "last_process_activity_at": started_at,
        "process_phase": "STARTING",
        "last_meaningful_progress_at": None,
        "reported_phase": None,
        "lease_expires_at": lease_expires_at,
        "wait_reason": None,
        "worktree": worktree,
        "browser_profile": browser_profile,
        "port": port,
    }


# ------------------------------------------------ C-18: dispatch claims
#
# A dispatch claim is the durable reservation that lets the slow, external
# half of a dispatch run with NO state lock held. It is the dispatch
# equivalent of C-05.3a's `security_evidence` claim and obeys the same rule:
# the state transaction chooses identity and reserves it; everything that
# touches a file, a subprocess, a socket or the network happens afterwards.
#
# WHERE IT LIVES. `task["dispatch_claims"][role]` - one key per role, so a
# task can hold at most one in-flight dispatch per role and two ticks cannot
# plan the same one twice. Builder claims carry `pr: None`; reviewer and
# fixer claims carry the pull request they were planned for, which is what
# lets `Supervisor.has_worker` see a planned-but-unspawned dispatch and stop
# `route_awaiting_dispatch` re-dispatching it on the next tick.
#
# WHY NOT A TOP-LEVEL MAP. Everything that reads a claim already has the
# task in hand, the claim dies with the task, and keeping it on the task
# means no second place can disagree with `doc["tasks"]` about who owns what.
#
# The accessors below setdefault, so a state document written before this
# key existed keeps working unchanged - the same compatibility rule
# `budget.reservations` and `notify.queue` follow.

DISPATCH_CLAIMS_KEY = "dispatch_claims"
DISPATCH_ROLES = ("builder", "reviewer", "fixer")

# Claim states. PLANNED means the reservation is committed and the external
# work has not been proven to have started. SPAWNED means a worker process
# was started under this claim. Both are ACTIVE: both hold the role's slot,
# and neither may be re-planned. A claim is REMOVED - never left behind in a
# terminal state - once its outcome has been committed, so "active" and
# "present" mean the same thing and there is only one rule to remember.
DISPATCH_PLANNED = "PLANNED"
DISPATCH_SPAWNED = "SPAWNED"
DISPATCH_ACTIVE_CLAIM_STATES = (DISPATCH_PLANNED, DISPATCH_SPAWNED)

# A worker name becomes a file name (`<worker>.job.json`) and a workmux
# window name, so it must be one safe path component and nothing else.
_DISPATCH_WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_DISPATCH_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def dispatch_claims(task: dict) -> dict:
    """Every dispatch claim this task holds, creating the map if absent."""
    claims = task.setdefault(DISPATCH_CLAIMS_KEY, {})
    if not isinstance(claims, dict):
        claims = {}
        task[DISPATCH_CLAIMS_KEY] = claims
    return claims


def dispatch_claim(task: dict, role: str) -> dict | None:
    """This task's claim for one role, or None. Never creates anything."""
    claim = (task.get(DISPATCH_CLAIMS_KEY) or {}).get(role)
    return claim if isinstance(claim, dict) else None


def dispatch_claim_active(claim: Any) -> bool:
    """Whether a claim still reserves its role's slot.

    A malformed claim is deliberately NOT active: it reserves nothing, so a
    later tick is free to replace it. Fail-closed would strand the task
    forever with no way back, and the claim carries no external effect of
    its own - the job file is the evidence that anything was spawned.
    """
    return (isinstance(claim, dict)
            and claim.get("claim_state") in DISPATCH_ACTIVE_CLAIM_STATES)


def _aware_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def new_dispatch_claim(*, role: str, task_id: str, worker: str, branch: str,
                       claimed_at: str, lease_expires_at: str,
                       pr: int | None = None,
                       port_candidates: Any = (),
                       observed_head: str | None = None,
                       context: dict | None = None) -> dict:
    """One PLANNED dispatch claim, for the state transaction.

    Every identifier is ALREADY CHOSEN by the caller, under the exclusive
    state lock, and nothing here derives one: a claim that picked its own
    worker name or port would be making the choice somewhere two ticks
    could make it simultaneously.

    Raises ValueError on any input the rest of the control plane would later
    refuse. A claim that cannot be acted on is worse than no claim, because
    the role's slot would be reserved by something that can never spawn.

    `port_candidates` is the ordered list `workers.select_port_candidates`
    chose under the lock; the probe that proves one of them binds happens
    outside. An empty list is legitimate and means this role needs no port.

    `observed_head` is the commit a pre-lock observation was taken at, for
    roles that have one. Carrying it durably is what lets the commit phase
    refuse a plan whose head has since moved.

    `context` carries the role-specific facts the execute and commit phases
    need and nothing else may interpret - for the builder, the C-15
    migration-lock evidence. It is copied, not referenced, so it cannot
    alias anything in the transaction's document.
    """
    if role not in DISPATCH_ROLES:
        raise ValueError(f"role must be one of {DISPATCH_ROLES}")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty string")
    if not isinstance(worker, str) or not _DISPATCH_WORKER_RE.match(worker):
        raise ValueError("worker must be a safe single path component")
    if not isinstance(branch, str) or not branch:
        raise ValueError("branch must be a non-empty string")
    if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool)
                           or pr <= 0):
        raise ValueError("pr must be a positive int or None")
    candidates = list(port_candidates or [])
    if any(not isinstance(port, int) or isinstance(port, bool) or port <= 0
           for port in candidates):
        raise ValueError("port_candidates must be positive ints")
    if observed_head is not None and (not isinstance(observed_head, str)
                                      or not _DISPATCH_SHA_RE.match(observed_head)):
        raise ValueError(
            "observed_head must be a full 40-character lowercase-hex git SHA")
    claimed = _aware_moment(claimed_at)
    expires = _aware_moment(lease_expires_at)
    if claimed is None or expires is None:
        raise ValueError("timestamps must be timezone-aware ISO-8601")
    if expires <= claimed:
        raise ValueError("lease_expires_at must be strictly after claimed_at")
    if context is not None and not isinstance(context, dict):
        raise ValueError("context must be a dict or None")
    return {
        "role": role,
        "task_id": task_id,
        "pr": pr,
        "worker": worker,
        "branch": branch,
        "port_candidates": candidates,
        "observed_head": observed_head,
        "claim_state": DISPATCH_PLANNED,
        "claimed_at": claimed_at,
        "lease_expires_at": lease_expires_at,
        "context": dict(context or {}),
    }


# =====================================================================
# G4: cumulative evidence wait. Approved 2026-10-01, 18000 s, as an
# explicit Run 002 experiment limit - NOT a guarantee that all valid work
# fits inside it. A maximally contended task can legitimately need about
# 4 h 06 m; the allowance clears that with margin, and at exhaustion the
# governed HUMAN_REQUIRED path is taken with a finite reason.
#
# THE ARITHMETIC, stated because getting it wrong is silent:
#
#   accumulated = sum of CLOSED intervals  +  the OPEN interval, if any
#
# `waiting_evidence_total_seconds` holds only closed intervals.
# `waiting_evidence_since` holds the start of the open one, or None when
# the task is not waiting. Nothing is counted twice because an interval is
# added to the total exactly once, at the moment it closes, and `since` is
# cleared in the same step.
#
# A NEW SHA STARTS A NEW INTERVAL AND NEVER RESETS THE TOTAL. The
# remediation loop runs WAITING_EVIDENCE -> FIX_REQUIRED -> PR_OPEN ->
# WAITING_EVIDENCE, so each cycle simply opens another interval on top of
# the accumulated total. Retries and Supervisor restarts retain it too:
# both fields are durable state, so a restart reloads the total AND the
# open interval's start, rather than beginning from zero - which is what
# would otherwise let a crash loop wait forever.
EVIDENCE_WAIT_STATE = "WAITING_EVIDENCE"
EVIDENCE_WAIT_TOTAL = "waiting_evidence_total_seconds"
EVIDENCE_WAIT_SINCE = "waiting_evidence_since"


def _closed_evidence_wait(task: dict) -> float:
    total = task.get(EVIDENCE_WAIT_TOTAL, 0)
    return float(total) if isinstance(total, (int, float)) \
        and not isinstance(total, bool) and total >= 0 else 0.0


def open_evidence_wait(task: dict, moment: str) -> None:
    """Start an interval, unless one is already open.

    Idempotent on purpose: re-entering the state while already waiting
    must not restart the clock, which would make a task that bounces
    within the state immortal.
    """
    if task.get(EVIDENCE_WAIT_SINCE) is None:
        task[EVIDENCE_WAIT_SINCE] = moment


def close_evidence_wait(task: dict, moment: str) -> None:
    """Close the open interval, adding it to the total exactly once."""
    since = task.get(EVIDENCE_WAIT_SINCE)
    if since is None:
        return
    task[EVIDENCE_WAIT_TOTAL] = _closed_evidence_wait(task) + \
        _interval_seconds(since, moment)
    task[EVIDENCE_WAIT_SINCE] = None


def _interval_seconds(since, until) -> float:
    """One interval in seconds, or `inf` if it cannot be established.

    `inf` rather than 0 for an unreadable timestamp. A broken accounting
    record cannot show a task is INSIDE its allowance, and the safe
    reading of "we cannot tell" is escalation to a human, never an
    unbounded wait.

    A backwards clock contributes 0 rather than a negative number, so the
    accumulated total can never decrease.
    """
    try:
        start = datetime.fromisoformat(since) if isinstance(since, str) else since
        end = datetime.fromisoformat(until) if isinstance(until, str) else until
    except (ValueError, TypeError):
        return float("inf")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return float("inf")
    # A naive timestamp cannot be compared with an aware one, and Run 002
    # runs in Pacific/Auckland where a DST offset change makes any
    # offset-less arithmetic wrong rather than merely imprecise.
    if start.tzinfo is None or end.tzinfo is None:
        return float("inf")
    return max(0.0, (end - start).total_seconds())


def evidence_wait_seconds(task: dict, now) -> float:
    """Total time this task has spent waiting for evidence, ever.

    `now` is injected rather than read here, so the arithmetic is testable
    against a controlled clock and no wall-clock test is needed.
    """
    if not isinstance(task, dict):
        return float("inf")
    accumulated = _closed_evidence_wait(task)
    since = task.get(EVIDENCE_WAIT_SINCE)
    if since is None:
        return accumulated
    return accumulated + _interval_seconds(since, now)


def evidence_wait_exhausted(task: dict, now, allowance) -> bool:
    """Whether the governed cumulative allowance is spent.

    Two rules, in this order, and the order is the point:

    1. A task that has not waited cannot have exhausted its wait. This is
       simply true, and checking it first means a configuration fault
       never escalates a task that has done nothing wrong - the task is
       not the thing that is broken.

    2. An unusable allowance is NOT a licence to wait forever. For a task
       that IS accumulating wait, an ungoverned bound means an unbounded
       one, which is the condition this check exists to prevent, so it
       escalates immediately and loudly rather than silently waiting.
    """
    waited = evidence_wait_seconds(task, now)
    if waited <= 0:
        return False
    if not isinstance(allowance, (int, float)) or isinstance(allowance, bool) \
            or allowance < 0:
        return True
    return waited >= float(allowance)


def transition(doc: dict, task_id: str, new_state: str, reason: str, tz: str) -> tuple[str, str]:
    task = doc["tasks"][task_id]
    old = task["state"]
    if new_state == old:
        return old, old
    if new_state not in TASK_STATES:
        raise TransitionError(f"unknown state {new_state}")
    if new_state not in ALLOWED_TRANSITIONS[old]:
        raise TransitionError(f"{task_id}: {old} -> {new_state} is not permitted")
    task["state"] = new_state
    task["updated_at"] = _now(tz)
    task["history"].append({"at": task["updated_at"], "from": old, "to": new_state,
                            "reason": reason})
    # G4. Hooked HERE, at the one chokepoint every state change passes
    # through, rather than at each call site - an accounting a caller can
    # forget to update is an accounting that silently under-counts, and
    # under-counting is the direction that lets a task wait forever. The
    # same `updated_at` moment is used, so the arithmetic agrees with the
    # history entry above rather than drifting from it by a clock read.
    if old == EVIDENCE_WAIT_STATE:
        close_evidence_wait(task, task["updated_at"])
    if new_state == EVIDENCE_WAIT_STATE:
        open_evidence_wait(task, task["updated_at"])
    return old, new_state


def dependencies_met(doc: dict, task: dict) -> bool:
    return all(
        doc["tasks"].get(dep, {}).get("state") == "COMPLETE" for dep in task["depends_on"]
    )


def tasks_in(doc: dict, *states: str) -> list[dict]:
    wanted = set(states)
    return [t for t in doc["tasks"].values() if t["state"] in wanted]


def review_queue_depth(doc: dict) -> int:
    """PRs awaiting independent review - the backpressure signal."""
    return len(tasks_in(doc, "PR_OPEN", "REVIEW"))


def builder_limit(queue_depth: int, configured_max: int) -> int:
    """Protocol v1.0 review-queue backpressure."""
    if queue_depth >= 3:
        return 0
    if queue_depth == 2:
        return min(2, configured_max)
    return configured_max


def active_builders(doc: dict) -> int:
    return len([t for t in doc["tasks"].values()
                if t["state"] in ("ASSIGNED", "ACTIVE") and t.get("worker")])
