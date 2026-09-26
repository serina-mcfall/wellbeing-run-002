"""C-08b.1: the human-intervention lifecycle foundation.

Protocol v2's "Human intervention taxonomy" names six intervention types and
requires recording requested_at, acknowledged_at, resolved_at and active
human minutes, plus visibility into simultaneous open HUMAN_REQUIRED events.
This module is the durable record and the OPEN -> ACKNOWLEDGED -> RESOLVED
state machine for one intervention. Its lifecycle primitives store and
validate a human's decision only - they never execute one. Clearing a
guardrail, retrying a task, or any other effect of a resolution outcome is
executed by the integration/CLI layer (control/cli.py's human-resolve,
C-08b.2), never here.

Records live in doc["interventions"], inside the existing durable state
document (control/state.py's JSON doc via control/state.py::Store) - no new
file, no new store. A document created before this module existed simply
has no "interventions" key; every function here treats that as "zero
interventions" rather than raising, so an old on-disk state document never
breaks.

Mirrors control/state.py::transition()'s own shape deliberately: these are
plain functions that mutate the passed-in `doc` and return, with `tz`
threaded explicitly. Callers append ledger events themselves, in the same
two-step pattern control/supervisor.py already uses everywhere else
(self.transition(...) then self.log(...)) - this module never writes to the
ledger and never calls control/notify.py. That is what makes "durable
record before Discord notification" true structurally, without this module
needing to know Discord exists.
"""

from __future__ import annotations

import re
import secrets

from . import clock, redact

INTERVENTION_TYPES = frozenset({
    "HUMAN_PRIVILEGED_ACTION",
    "HUMAN_APPARATUS_AUTHORISATION",
    "HUMAN_VERIFICATION",
    "HUMAN_GOVERNANCE_DECISION",
    "HUMAN_PRODUCT_DECISION",
    "HUMAN_PRODUCT_IMPLEMENTATION",
})

RESOLUTION_OUTCOMES = frozenset({
    "RETRY",
    "FAIL",
    "RESUME",
    "CLEAR_GUARDRAIL",
    "FREEZE",
    "NO_ACTION",
})

SCOPES = frozenset({"task", "systemic"})

VALID_STATUSES = frozenset({"OPEN", "ACKNOWLEDGED", "RESOLVED"})

CONDITION_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# C-08b.2: production reasons are structurally generated from fixed control-plane
# templates plus canonical identifiers and bounded integers, so the secret
# detector firing on one is supposed to be impossible. If it fires anyway, the
# TEXT is rejected from persistence - never the intervention occurrence. The
# matched value is not echoed, not stored, and not excerpted; this fixed
# placeholder is stored instead and the record carries reason_withheld=True.
WITHHELD_REASON = "reason withheld: the generated reason matched the secret detector"


class InterventionError(RuntimeError):
    """A lifecycle rule or input invariant was violated. The document is
    left unchanged whenever this is raised."""


def new_intervention_id() -> str:
    return f"INT-{secrets.token_hex(8)}"


def _validate_request_inputs(type_: str, scope: str, task_id: str | None,
                             reason: str, condition_code: str) -> None:
    if type_ not in INTERVENTION_TYPES:
        raise InterventionError(
            f"{type_!r} is not one of Protocol v2's six intervention types: "
            f"{sorted(INTERVENTION_TYPES)}"
        )
    if scope not in SCOPES:
        raise InterventionError(f"scope must be 'task' or 'systemic', got {scope!r}")
    if scope == "task" and not (task_id and task_id.strip()):
        raise InterventionError("scope='task' requires a non-empty task_id")
    if scope == "systemic" and task_id is not None:
        raise InterventionError(
            f"scope='systemic' requires task_id=None, got {task_id!r}"
        )
    if not (reason and reason.strip()):
        raise InterventionError("reason must be non-empty")
    if not condition_code or not CONDITION_CODE_RE.match(condition_code):
        raise InterventionError(
            f"condition_code {condition_code!r} must match {CONDITION_CODE_RE.pattern}"
        )


def _dedup_key(scope: str, task_id: str | None, condition_code: str) -> str:
    if scope == "task":
        return f"task:{task_id}:{condition_code}"
    return f"system:{condition_code}"


def request(doc: dict, *, type_: str, scope: str, task_id: str | None,
           reason: str, condition_code: str, tz: str) -> tuple[dict, bool]:
    """Creates a new OPEN intervention, or reuses an existing OPEN/
    ACKNOWLEDGED one sharing the same (scope, task_id, condition_code)
    dedup key. Returns (record, is_new) - is_new is False when an existing
    record was reused, so the caller can decide whether a fresh ledger
    event is warranted (a deduped reuse must not re-log a new request).

    Validation, the dedup search, and ID collision-checking are all
    read-only against doc.get("interventions", {}) - the document is never
    mutated (not even by creating an empty "interventions" key) until a
    complete, valid, uniquely-keyed record is ready to insert. A validation
    failure or a deduped reuse therefore leaves the whole document,
    including an old one with no "interventions" key at all, completely
    untouched.
    """
    _validate_request_inputs(type_, scope, task_id, reason, condition_code)

    existing = doc.get("interventions", {})
    dedup_key = _dedup_key(scope, task_id, condition_code)
    for record in existing.values():
        if record["dedup_key"] == dedup_key and record["status"] != "RESOLVED":
            return record, False

    intervention_id = new_intervention_id()
    while intervention_id in existing:
        intervention_id = new_intervention_id()

    reason_withheld = redact.contains_secret(reason)
    record = {
        "id": intervention_id,
        "type": type_,
        "scope": scope,
        "task_id": task_id,
        "reason": WITHHELD_REASON if reason_withheld else reason,
        "reason_withheld": reason_withheld,
        "condition_code": condition_code,
        "dedup_key": dedup_key,
        "status": "OPEN",
        "requested_at": clock.iso(clock.now(tz)),
        "acknowledged_at": None,
        "acknowledged_by": None,
        "resolved_at": None,
        "resolved_by": None,
        "resolution": None,
        "resolution_note": None,
        "active_human_seconds": None,
    }

    interventions = doc.setdefault("interventions", {})
    interventions[intervention_id] = record
    return record, True


def _get_or_raise(doc: dict, intervention_id: str) -> dict:
    record = doc.get("interventions", {}).get(intervention_id)
    if record is None:
        raise InterventionError(f"unknown intervention {intervention_id!r}")
    return record


def _require_nonblank(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise InterventionError(f"{field_name} must be non-empty")
    return value


def _validated_status(record: dict) -> str:
    status = record["status"]
    if status not in VALID_STATUSES:
        raise InterventionError(
            f"{record.get('id', '?')} has an unrecognised status {status!r}; "
            "refusing to act on corrupt state"
        )
    return status


def acknowledge(doc: dict, intervention_id: str, *, by: str, tz: str) -> tuple[dict, bool]:
    """OPEN -> ACKNOWLEDGED. Raises InterventionError, leaving the document
    unchanged, for an unknown ID or a blank `by`.

    Idempotent for an already-ACKNOWLEDGED intervention: returns the
    existing record UNCHANGED (acknowledged_at/acknowledged_by are never
    reset) and (record, False) - the caller must not emit a second
    HUMAN_INTERVENTION_ACKNOWLEDGED ledger event for that case. Returns
    (record, True) only on a genuine first acknowledgement. Rejects
    RESOLVED explicitly, and rejects any other status as corrupt state
    rather than silently treating it as OPEN.
    """
    _require_nonblank(by, "by")
    record = _get_or_raise(doc, intervention_id)
    status = _validated_status(record)

    if status == "OPEN":
        record["status"] = "ACKNOWLEDGED"
        record["acknowledged_at"] = clock.iso(clock.now(tz))
        record["acknowledged_by"] = by
        return record, True
    if status == "ACKNOWLEDGED":
        return record, False
    # status == "RESOLVED"
    raise InterventionError(
        f"{intervention_id} is already RESOLVED; acknowledgement no longer applies"
    )


def resolve(doc: dict, intervention_id: str, *, outcome: str, by: str, tz: str,
           note: str | None = None) -> dict:
    """ACKNOWLEDGED -> RESOLVED only - acknowledgement is mandatory first,
    so direct OPEN -> RESOLVED is not permitted. Raises InterventionError,
    leaving the document unchanged, if the intervention is unknown, `by` is
    blank, `outcome` is not a governed RESOLUTION_OUTCOMES value, the
    intervention is still OPEN, it is already RESOLVED (no double-resolve),
    or its status is anything other than the three known values.

    active_human_seconds is the exact non-negative integer second count
    between acknowledged_at and resolved_at - never a rounded float. This
    is always computable, because acknowledgement is now mandatory before
    resolution. Protocol v2's "active human minutes" is a presentation-time
    view of this value (see active_human_minutes() below); rounding for
    display happens only at that reporting boundary, never in this stored
    field.

    Stores the decision only. Never mutates doc["tasks"], doc["red_guardrail"],
    doc["budget"], or anything else - executing a resolution outcome is
    C-08b.2's job, not this function's.
    """
    if outcome not in RESOLUTION_OUTCOMES:
        raise InterventionError(
            f"{outcome!r} is not a governed resolution outcome: {sorted(RESOLUTION_OUTCOMES)}"
        )
    _require_nonblank(by, "by")
    record = _get_or_raise(doc, intervention_id)
    status = _validated_status(record)

    if status == "OPEN":
        raise InterventionError(
            f"{intervention_id} must be acknowledged before it can be resolved"
        )
    if status == "RESOLVED":
        raise InterventionError(f"{intervention_id} is already RESOLVED")
    # status == "ACKNOWLEDGED"

    resolved_at = clock.now(tz)
    acknowledged_at = clock.parse(record["acknowledged_at"])
    active_human_seconds = int((resolved_at - acknowledged_at).total_seconds())
    if active_human_seconds < 0:
        raise InterventionError(
            f"{intervention_id}: resolved_at precedes acknowledged_at - refusing to "
            "store a negative duration"
        )

    record["status"] = "RESOLVED"
    record["resolved_at"] = clock.iso(resolved_at)
    record["resolved_by"] = by
    record["resolution"] = outcome
    record["resolution_note"] = note
    record["active_human_seconds"] = active_human_seconds
    return record


def active_human_minutes(record: dict) -> float | None:
    """Presentation-only view of active_human_seconds, rounded to one
    decimal place. None until the intervention is resolved. This rounding
    happens only here, at the reporting boundary - never in stored state."""
    seconds = record.get("active_human_seconds")
    if seconds is None:
        return None
    return round(seconds / 60.0, 1)


def open_interventions(doc: dict) -> list[dict]:
    """Every intervention whose status != RESOLVED (OPEN + ACKNOWLEDGED).
    Raises InterventionError if any record's status is not one of the
    three known values - a corrupt record anywhere makes this count
    untrustworthy, so it is never silently included or silently skipped."""
    return [r for r in doc.get("interventions", {}).values() if _validated_status(r) != "RESOLVED"]


def simultaneous_open_count(doc: dict) -> int:
    return len(open_interventions(doc))


def notification_suffix(record: dict) -> str:
    """The intervention identity/action line every escalation notification
    carries (C-08b.2). One home, because the Supervisor, the Watchdog and the
    merge-invariant annunciator all append the same sentence."""
    return (f" Intervention {record['id']} ({record['type']}) recorded: "
            f"acknowledge with ctl human-acknowledge {record['id']}, then "
            f"resolve with ctl human-resolve.")
