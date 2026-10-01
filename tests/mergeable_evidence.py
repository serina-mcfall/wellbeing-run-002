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
