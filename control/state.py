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
TASK_STATES = (
    "QUEUED",
    "READY",
    "ASSIGNED",
    "ACTIVE",
    "PR_OPEN",
    "REVIEW",
    "FIX_REQUIRED",
    "MERGED",
    "COMPLETE",
    "BLOCKED",
    "STALE",
    "FAILED",
    "WAITING_PROVIDER_RESET",
    "WAITING_DB_LOCK",
    "HUMAN_REQUIRED",
)

TERMINAL_STATES = frozenset({"COMPLETE", "FAILED"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"READY", "BLOCKED", "HUMAN_REQUIRED", "FAILED"}),
    "READY": frozenset(
        {"ASSIGNED", "QUEUED", "BLOCKED", "WAITING_PROVIDER_RESET", "WAITING_DB_LOCK",
         "HUMAN_REQUIRED", "FAILED"}
    ),
    "ASSIGNED": frozenset({"ACTIVE", "READY", "STALE", "FAILED", "HUMAN_REQUIRED"}),
    "ACTIVE": frozenset(
        {"PR_OPEN", "STALE", "FAILED", "BLOCKED", "READY", "WAITING_PROVIDER_RESET",
         "WAITING_DB_LOCK", "HUMAN_REQUIRED"}
    ),
    "PR_OPEN": frozenset({"REVIEW", "STALE", "FAILED", "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"}),
    "REVIEW": frozenset(
        {"FIX_REQUIRED", "MERGED", "PR_OPEN", "STALE", "FAILED", "HUMAN_REQUIRED",
         "WAITING_PROVIDER_RESET"}
    ),
    "FIX_REQUIRED": frozenset(
        {"REVIEW", "PR_OPEN", "STALE", "FAILED", "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"}
    ),
    "MERGED": frozenset({"COMPLETE", "HUMAN_REQUIRED"}),
    "COMPLETE": frozenset(),
    "BLOCKED": frozenset({"READY", "QUEUED", "HUMAN_REQUIRED", "FAILED"}),
    "STALE": frozenset({"READY", "ASSIGNED", "ACTIVE", "FAILED", "HUMAN_REQUIRED"}),
    "FAILED": frozenset({"HUMAN_REQUIRED"}),
    "WAITING_PROVIDER_RESET": frozenset({"READY", "ACTIVE", "REVIEW", "FIX_REQUIRED",
                                         "HUMAN_REQUIRED", "FAILED"}),
    "WAITING_DB_LOCK": frozenset({"READY", "ACTIVE", "HUMAN_REQUIRED", "FAILED"}),
    "HUMAN_REQUIRED": frozenset(set(TASK_STATES)),
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
