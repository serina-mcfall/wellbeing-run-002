"""Complete, passing evidence for one PR record at one head — a FIXTURE.

`routing.evaluate_merge` requires every Protocol v2 evidence class to be a
completed pass for the head GitHub reports, through
`routing.review_gate_fires`. Five test modules build records that are
expected to MERGE, and each needs the same three legs.

Defining them once matters for a specific reason: a fixture that drifts
from the evidence shape stops exercising the gate and starts exercising
nothing, and five copies drift independently. If the required classes
change, this file fails and every caller follows.

NOT a factory the control plane uses. `routing.security_claim` is the real
builder for the security leg and is used here so that leg is genuinely
canonical; the two accessibility legs have no builder yet (that is handover
§36.5 D4's tripwire), so their shapes follow §31.5 and §31.7 and match
`tests/test_c05_3b_review_gate.py`, which is where the gate itself is
proved.

Callers that want a record which must NOT merge should leave the legs off,
or override one — absence and staleness are exactly what the gate refuses.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, routing  # noqa: E402

_CLAIMED = clock.now("UTC").replace(microsecond=0)
CLAIMED_AT = clock.iso(_CLAIMED)
EXPIRES_AT = clock.iso(_CLAIMED + timedelta(seconds=1800))


def security_leg(sha: str, task_id: str = "task-001") -> dict:
    """A COMPLETE, passing security claim bound to `sha`.

    Built through the real `routing.security_claim`, so this leg satisfies
    `security_claim_is_valid` for the same reasons a live one would —
    including the SHA-bound worker name. A hand-written dict could drift
    from the validator and quietly stop proving anything.
    """
    claim = routing.security_claim(
        task_id=task_id, sha=sha, ordinal=1,
        claimed_at=CLAIMED_AT, lease_expires_at=EXPIRES_AT)
    claim["claim_state"] = "COMPLETE"
    claim["verdict"] = routing.SECURITY_PASS
    claim["reason"] = ""
    return claim


def accessibility_auto_leg(sha: str) -> dict:
    """A COMPLETE, passing accessibility_auto claim — §31.5's seven keys."""
    return {
        "sha": sha,
        "attempt_id": "attempt-0001",
        "claim_state": "COMPLETE",
        "claimed_at": CLAIMED_AT,
        "port": 3200,
        # G2: the scan has finished AND its product server has been torn
        # down with the listener observed gone. A mergeable record is one
        # whose port is back in the pool.
        "port_released": True,
        "verdict": routing.ACCESSIBILITY_AUTO_PASS,
        "reason": "",
    }


def accessibility_review_leg(sha: str, task_id: str = "task-001") -> dict:
    """A COMPLETE, passing accessibility_review claim — §31.7's nine keys."""
    return {
        "sha": sha,
        "ordinal": 1,
        "attempt_id": "a11y-attempt-0001",
        "worker": f"{task_id}-a11y-{sha}-0001",
        "claim_state": "COMPLETE",
        "claimed_at": CLAIMED_AT,
        "lease_expires_at": EXPIRES_AT,
        "verdict": routing.ACCESSIBILITY_PASS,
        "reason": "",
    }


def complete_evidence(sha: str, task_id: str = "task-001") -> dict:
    """Every required evidence leg, complete and passing at `sha`.

    Spread into a PR record: `record.update(complete_evidence(HEAD))`.
    """
    return {
        "security_evidence": security_leg(sha, task_id),
        "accessibility_auto": accessibility_auto_leg(sha),
        "accessibility_review": accessibility_review_leg(sha, task_id),
    }


def with_complete_evidence(record: dict, sha: str,
                           task_id: str = "task-001") -> dict:
    """`record`, updated in place with complete evidence at `sha`."""
    record.update(complete_evidence(sha, task_id))
    return record


# ---------------------------------------------------------------- C-04c
#
# THE OTHER HALF OF THE SAME FIXTURE. `complete_evidence` seeds the
# MUTABLE record. A real run cannot reach that record without ALSO having
# written four events to the durable ledger, one as each leg completed -
# `commit_review`, `commit_security`, `commit_accessibility` and
# `ingest_accessibility_review` each log their RESULT with the head they
# judged.
#
# Since C-04c, `attempt_merge` cross-checks the two sources, so a fixture
# that seeds only the record is modelling a state production cannot
# produce: evidence that passed with nothing in the evidence record saying
# so. Seeding both is what keeps these fixtures faithful rather than what
# makes them pass.
#
# They live next to `complete_evidence` for the reason that file's own
# docstring gives: copies drift independently, and a fixture that drifts
# from the shape under test stops exercising anything.

def review_worker(task_id: str = "TASK-001", cycle: int = 1) -> str:
    """The reviewer worker name `supervisor.dispatch_reviewer` would mint.

    `f"{task['id'].lower()}-review-{cycle}"`, reproduced here rather than
    imported because the Supervisor builds it inline. It is NOT SHA-bound -
    see `routing._review_worker_attests` for what that costs.
    """
    return f"{task_id.lower()}-review-{cycle}"


def attestation_events(sha: str, task_id: str = "TASK-001",
                       pr_id: int = 100, cycle: int = 1) -> tuple[dict, ...]:
    """The ledger events a real run writes alongside the record.

    `task_id` here is the LEDGER's spelling - `self.log(task_id=task["id"])`
    uses the uppercase task identifier, while `complete_evidence` takes the
    lowercase form the SHA-bound worker names are built from. They are
    genuinely different strings and conflating them is how an attestation
    silently stops matching.

    The head is carried in `metadata_redacted`, which is where
    `Ledger.append` puts it for all four results: `head_sha` is not in
    `ledger.FIELDS`, and the three accessibility/security legs pass
    `metadata_redacted={"head": ...}` directly.

    THE REVIEW PAIR. `REVIEW_DISPATCHED` is emitted too, and both review
    events carry `agent_id`, because `ledger_attests_merge` now requires a
    dispatch bound to this head and requires the verdict to name a worker
    that dispatch names. A real run writes exactly this pair
    (`_commit_reviewer_dispatch` and `on_reviewer_finished`), so a fixture
    without it was describing a run that cannot happen.
    """
    worker = review_worker(task_id, cycle)
    events = [
        {"event_type": event_type, "task_id": task_id, "pr_id": pr_id,
         "outcome": outcome, "metadata_redacted": {"head": sha},
         **({"agent_id": worker}
            if event_type == routing.REVIEW_RESULT_EVENT else {})}
        for event_type, outcome in routing.MERGE_ATTESTATIONS
    ]
    events.append({"event_type": routing.REVIEW_DISPATCH_EVENT,
                   "task_id": task_id, "pr_id": pr_id, "outcome": "DISPATCHED",
                   "agent_id": worker, "metadata_redacted": {"head": sha}})
    return tuple(events)


def attest(ledger, sha: str, task_id: str = "TASK-001",
           pr_id: int = 100, cycle: int = 1) -> None:
    """Append those events to a real `Ledger`.

    Uses `Ledger.append` rather than writing lines directly, so the events
    go through the same stamping, redaction and locking a live one would -
    a fixture that bypassed `append` could pass while the real writer was
    broken.
    """
    for event in attestation_events(sha, task_id, pr_id, cycle):
        extra = {"agent_id": event["agent_id"]} if "agent_id" in event else {}
        ledger.append(event["event_type"], task_id=event["task_id"],
                      pr_id=event["pr_id"], outcome=event["outcome"],
                      metadata_redacted=dict(event["metadata_redacted"]),
                      **extra)
