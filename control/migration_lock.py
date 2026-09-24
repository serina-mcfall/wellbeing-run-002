"""Deterministic single-writer database migration lock.

Only one unmerged migration owner may exist at a time. A task blocked solely on
schema ownership waits in `WAITING_DB_LOCK` while independent UI or domain work
continues.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

FREE = "FREE"
HELD = "HELD"


def ensure(doc: dict) -> None:
    doc.setdefault(
        "migration_lock",
        {"state": FREE, "owner_task": None, "owner_pr": None, "acquired_at": None,
         "waiters": [], "contention_events": 0},
    )


def state(doc: dict) -> str:
    return doc["migration_lock"]["state"]


def owner(doc: dict) -> str | None:
    return doc["migration_lock"]["owner_task"]


def acquire(doc: dict, task_id: str, tz: str) -> bool:
    """Take the lock if free, or confirm the caller already holds it."""
    lock = doc["migration_lock"]
    if lock["state"] == HELD:
        if lock["owner_task"] == task_id:
            return True
        if task_id not in lock["waiters"]:
            lock["waiters"].append(task_id)
            lock["contention_events"] += 1
            doc["counters"]["migration_lock_waits"] += 1
        return False
    lock["state"] = HELD
    lock["owner_task"] = task_id
    lock["acquired_at"] = datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
    if task_id in lock["waiters"]:
        lock["waiters"].remove(task_id)
    return True


def attach_pr(doc: dict, task_id: str, pr_number: int) -> None:
    lock = doc["migration_lock"]
    if lock["owner_task"] == task_id:
        lock["owner_pr"] = pr_number


def release(doc: dict, task_id: str, reason: str) -> bool:
    """Release only after the migration is merged, abandoned, or rolled back."""
    lock = doc["migration_lock"]
    if lock["owner_task"] != task_id:
        return False
    lock.update({"state": FREE, "owner_task": None, "owner_pr": None,
                 "acquired_at": None, "last_release_reason": reason})
    return True


def next_waiter(doc: dict) -> str | None:
    waiters = doc["migration_lock"]["waiters"]
    return waiters[0] if waiters else None
