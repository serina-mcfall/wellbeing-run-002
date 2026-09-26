"""Append-only JSONL experiment ledger.

The raw ledger is authoritative. Events are appended under an exclusive file
lock, flushed and fsynced, and never rewritten. Every event is scrubbed before
it is written; a line that still looks like it holds a secret is refused and a
`GUARDRAIL_RED` marker is written in its place.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import config, redact

ACTIVITY_CLASSES = frozenset(
    {
        "BUILD",
        "REVIEW",
        "FIX",
        "DEBUG",
        "TEST",
        "ORCHESTRATION",
        "OBSERVATION",
        "DOCUMENTATION",
        "ESCALATION",
        "FAILED_WORK",
    }
)

FIELDS = (
    "timestamp",
    "experiment_id",
    "event_type",
    "activity_class",
    "agent_id",
    "role",
    "provider",
    "model",
    "task_id",
    "pr_id",
    "branch",
    "state_before",
    "state_after",
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "actual_cost_usd",
    "estimated_cost_usd",
    "cost_source",
    "duration_ms",
    "outcome",
    "guardrail",
    "human_intervention",
    "metadata_redacted",
)


# Finite error vocabulary for the invariant inspection path. An OS exception
# string is never persisted.
LEDGER_FILE_MISSING = "LEDGER_FILE_MISSING"
LEDGER_UNREADABLE = "LEDGER_UNREADABLE"


@dataclass(frozen=True)
class Inspection:
    """Result of a bounded invariant lookup. `complete` is False whenever any
    line could not be parsed, so callers can refuse to treat an absence as
    proof. Matched events that DID parse remain valid evidence."""
    events: tuple
    complete: bool
    unreadable_lines: int
    error: str | None


class Ledger:
    def __init__(self, path: Path | None = None, tz: str = "Pacific/Auckland",
                 experiment_id: str = "run-002") -> None:
        self.path = path or config.LEDGER_PATH
        self.tz = tz
        self.experiment_id = experiment_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event_type: str, **fields: Any) -> dict:
        event: dict[str, Any] = {
            "timestamp": datetime.now(ZoneInfo(self.tz)).isoformat(timespec="milliseconds"),
            "experiment_id": self.experiment_id,
            "event_type": event_type,
        }
        for key in FIELDS:
            if key in fields:
                event[key] = fields.pop(key)
        if fields:
            merged = dict(event.get("metadata_redacted") or {})
            merged.update(fields)
            event["metadata_redacted"] = merged

        event = redact.scrub(event)
        line = json.dumps(event, ensure_ascii=False, sort_keys=False, default=str)

        if redact.contains_secret(line):
            # Defence in depth: never write a line we cannot prove is clean.
            event = {
                "timestamp": event["timestamp"],
                "experiment_id": self.experiment_id,
                "event_type": "GUARDRAIL_RED",
                "guardrail": "SECRET_IN_LEDGER_EVENT_BLOCKED",
                "outcome": "BLOCKED",
                "metadata_redacted": {"blocked_event_type": event_type},
            }
            line = json.dumps(event, ensure_ascii=False)

        self._write(line)
        return event

    def _write(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> Iterator[dict]:
        if not self.path.exists():
            return iter(())
        return self._iter()

    def _iter(self) -> Iterator[dict]:
        with open(self.path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    def events(self, event_type: str | None = None) -> list[dict]:
        return [e for e in self.read() if event_type is None or e.get("event_type") == event_type]

    def last(self, event_type: str) -> dict | None:
        found = None
        for event in self.read():
            if event.get("event_type") == event_type:
                found = event
        return found

    def count(self) -> int:
        return sum(1 for _ in self.read())

    def inspect(self, *, task_id: str | None = None, pr_id=None,
                event_types: tuple[str, ...] = ()) -> "Inspection":
        """C-14.2: a bounded, fail-closed lookup for invariant checking.

        read()/events()/last()/count() silently skip a line that will not
        parse, which is right for reporting and wrong here: for invariant work
        "no matching event" and "the matching event was truncated" are
        different answers and must not collapse into the same one. A crash
        mid-append - exactly the fault C-14 is about - leaves a truncated
        final line, so this path reports completeness instead of hiding it.

        Deliberately separate from the existing readers so no other consumer
        changes behaviour.
        """
        matched: list[dict] = []
        unreadable = 0
        error: str | None = None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        unreadable += 1
                        continue
                    if event_types and event.get("event_type") not in event_types:
                        continue
                    if task_id is not None and event.get("task_id") != task_id:
                        continue
                    if pr_id is not None and event.get("pr_id") != pr_id:
                        continue
                    matched.append(event)
        except FileNotFoundError:
            # A missing ledger during invariant reconciliation is the loss of a
            # durable evidence source, not proof that no events occurred. It is
            # reported incomplete here even though read() and the ordinary
            # reporting path legitimately treat absence as empty.
            return Inspection((), False, 0, LEDGER_FILE_MISSING)
        except OSError:
            # Unreadable file is not an empty file. Never report completeness
            # we cannot demonstrate.
            return Inspection((), False, 0, LEDGER_UNREADABLE)
        return Inspection(tuple(matched), unreadable == 0, unreadable, error)
