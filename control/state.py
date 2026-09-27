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
    "WAITING_EVIDENCE": frozenset({"REVIEW", "STALE", "FAILED", "HUMAN_REQUIRED",
                                   "WAITING_PROVIDER_RESET"}),
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
        "budget": {"total_usd": 0.0, "spent_usd": 0.0, "estimated_usd": 0.0,
                   "thresholds_crossed": [], "hard_stop": False},
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
