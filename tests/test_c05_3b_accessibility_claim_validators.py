"""The two accessibility claim validators — handover §36.5 D4, closed.

D4 recorded that the accessibility legs had no claim validator, so each was
checked structurally only: strictly weaker than the security leg, and
invisible at the call site because `review_gate_fires` still returned a
confident True. Both validators now exist and are registered.

These tests check the validators THEMSELVES. `test_c05_3b_review_gate.py`
exercises them only incidentally, through records built to pass, which
would not notice a validator that accepted everything.

Two rules inherited from `security_claim_is_valid`, both deliberately
re-proved here rather than assumed:

  * a diagnostic is RETURNED, never raised — durable JSON can carry a dict
    or a list in any field, and that was a real crash in the security
    validator (§36.5 D1), not a hypothesis;
  * these diagnostics describe a malformed claim RECORD and are NOT drawn
    from the accessibility failure-reason vocabularies, which say why an
    ATTEMPT is not a completed review.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_contract as ac  # noqa: E402
from control import clock, routing  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40

_T0 = clock.now("UTC").replace(microsecond=0)
CLAIMED_AT = clock.iso(_T0)
EXPIRES_AT = clock.iso(_T0 + timedelta(seconds=1800))

HOSTILE = ({}, [], {"k": [1]}, [{"k": 1}], set(), 0.5, b"bytes", None)


def auto(**over):
    claim = {
        "sha": HEAD,
        "attempt_id": "attempt-0001",
        "claim_state": "COMPLETE",
        "claimed_at": CLAIMED_AT,
        "port": 3200,
        "port_released": True,
        "verdict": ac.ACCESSIBILITY_AUTO_PASS,
        "reason": "",
    }
    claim.update(over)
    return claim


def review(**over):
    claim = {
        "sha": HEAD,
        "ordinal": 1,
        "attempt_id": "a11y-attempt-0001",
        "worker": f"task-001-a11y-{HEAD}-0001",
        "claim_state": "COMPLETE",
        "claimed_at": CLAIMED_AT,
        "lease_expires_at": EXPIRES_AT,
        "verdict": ac.ACCESSIBILITY_PASS,
        "reason": "",
    }
    claim.update(over)
    return claim


class BaselineCase(unittest.TestCase):
    """Otherwise every refusal below could pass for the wrong reason."""

    def test_the_fixtures_really_are_valid(self):
        self.assertEqual(routing.accessibility_auto_claim_is_valid(auto()),
                         (True, ""))
        self.assertEqual(routing.accessibility_review_claim_is_valid(review()),
                         (True, ""))


class AutoClaimCase(unittest.TestCase):

    def check(self, expected, **over):
        ok, why = routing.accessibility_auto_claim_is_valid(auto(**over))
        self.assertFalse(ok)
        self.assertEqual(why, expected)

    def test_a_non_object_is_refused(self):
        for value in (None, [], "COMPLETE", 7):
            with self.subTest(claim=repr(value)):
                self.assertEqual(
                    routing.accessibility_auto_claim_is_valid(value),
                    (False, "CLAIM_NOT_AN_OBJECT"))

    def test_a_foreign_or_missing_key_is_refused(self):
        self.check("CLAIM_KEYS_INVALID", extra="x")
        short = auto()
        del short["port"]
        self.assertEqual(
            routing.accessibility_auto_claim_is_valid(short)[1],
            "CLAIM_KEYS_INVALID")

    def test_a_non_canonical_sha_is_refused(self):
        for sha in ("A" * 40, "a" * 39, "a" * 41, "", None, 7):
            with self.subTest(sha=repr(sha)):
                self.check("CLAIM_SHA_INVALID", sha=sha)

    def test_an_unknown_claim_state_is_refused(self):
        for state in ("RUNNING", "complete", "", None, "DONE"):
            with self.subTest(state=repr(state)):
                self.check("CLAIM_STATE_INVALID", claim_state=state)

    def test_a_malformed_attempt_id_is_refused(self):
        for attempt in ("attempt-1", "attempt-00001", "a11y-attempt-0001",
                        "", "ATTEMPT-0001", None):
            with self.subTest(attempt_id=repr(attempt)):
                self.check("CLAIM_ATTEMPT_ID_INVALID", attempt_id=attempt)

    def test_an_out_of_range_or_non_integer_port_is_refused(self):
        for port in (0, -1, 1023, 65536, "3200", 3200.0, None, True):
            with self.subTest(port=repr(port)):
                self.check("CLAIM_PORT_INVALID", port=port)

    def test_a_valid_port_at_each_boundary_is_accepted(self):
        for port in (1024, 65535, 3200):
            with self.subTest(port=port):
                self.assertTrue(
                    routing.accessibility_auto_claim_is_valid(auto(port=port))[0])

    def test_a_non_canonical_timestamp_is_refused(self):
        for at in ("2026-10-01", "not a time", "", None,
                   "2026-10-01T00:00:00Z"):
            with self.subTest(claimed_at=repr(at)):
                self.check("CLAIM_TIMESTAMP_INVALID", claimed_at=at)

    def test_an_outcome_before_completion_is_refused(self):
        for state in ("PLANNED", "SPAWNED"):
            with self.subTest(state=state, field="verdict"):
                self.check("CLAIM_VERDICT_BEFORE_COMPLETE",
                           claim_state=state,
                           verdict=ac.ACCESSIBILITY_AUTO_PASS)
            with self.subTest(state=state, field="reason"):
                self.check("CLAIM_REASON_BEFORE_COMPLETE",
                           claim_state=state, verdict=None,
                           reason=ac.TIMED_OUT)

    def test_a_planned_claim_saying_nothing_is_valid(self):
        self.assertTrue(routing.accessibility_auto_claim_is_valid(
            auto(claim_state="PLANNED", verdict=None, reason=""))[0])

    def test_complete_with_both_or_neither_is_refused(self):
        self.check("CLAIM_VERDICT_AND_REASON",
                   verdict=ac.ACCESSIBILITY_AUTO_PASS, reason=ac.TIMED_OUT)
        self.check("CLAIM_COMPLETE_WITHOUT_OUTCOME", verdict=None, reason="")

    def test_an_unrecognised_verdict_is_refused(self):
        for verdict in ("ACCESSIBILITY_MAYBE", ac.ACCESSIBILITY_PASS,
                        ac.ACCESSIBILITY_UNPARSEABLE, "accessibility_auto_pass"):
            with self.subTest(verdict=verdict):
                self.check("CLAIM_VERDICT_UNRECOGNISED", verdict=verdict)

    def test_the_qualitative_verdict_can_never_satisfy_the_automated_leg(self):
        # The two verdict families are deliberately disjoint: a record
        # carrying only the machine half must not be indistinguishable
        # from one carrying both.
        self.check("CLAIM_VERDICT_UNRECOGNISED", verdict=ac.ACCESSIBILITY_PASS)

    def test_a_qualitative_only_reason_is_refused_on_the_automated_leg(self):
        # A machine run must never be recorded as having failed a
        # judgement it never made.
        self.check("CLAIM_REASON_UNRECOGNISED", verdict=None,
                   reason=ac.CLASSIFICATION_INVALID)

    def test_every_automated_failure_reason_is_accepted(self):
        for reason in sorted(ac.ACCESSIBILITY_AUTO_FAILURE_REASONS):
            with self.subTest(reason=reason):
                self.assertTrue(routing.accessibility_auto_claim_is_valid(
                    auto(verdict=None, reason=reason))[0], reason)


class ReviewClaimCase(unittest.TestCase):

    def check(self, expected, **over):
        ok, why = routing.accessibility_review_claim_is_valid(review(**over))
        self.assertFalse(ok)
        self.assertEqual(why, expected)

    def test_a_foreign_or_missing_key_is_refused(self):
        self.check("CLAIM_KEYS_INVALID", extra="x")

    def test_an_ordinal_outside_the_namespace_is_refused(self):
        for ordinal in (0, -1, 10000, True, "1", 1.0, None):
            with self.subTest(ordinal=repr(ordinal)):
                ok, why = routing.accessibility_review_claim_is_valid(
                    review(ordinal=ordinal))
                self.assertFalse(ok)
                self.assertIn(why, {"CLAIM_ORDINAL_INVALID",
                                    "CLAIM_ATTEMPT_ID_MISMATCH"})

    def test_an_attempt_id_disagreeing_with_its_ordinal_is_refused(self):
        self.check("CLAIM_ATTEMPT_ID_MISMATCH", attempt_id="a11y-attempt-0002")
        self.check("CLAIM_ATTEMPT_ID_MISMATCH", attempt_id="attempt-0001")

    def test_an_unsafe_worker_name_is_refused(self):
        for worker in ("../escape", "a/b", "UPPER", "-leading", ".hidden",
                       "", "x" * 65, "has space", None):
            with self.subTest(worker=repr(worker)):
                self.check("CLAIM_WORKER_INVALID", worker=worker)

    def test_a_worker_name_not_bound_to_this_sha_and_ordinal_is_refused(self):
        # THE point of the SHA-bound name. The ordinal restarts at 1 for a
        # new head, so a name built from task and ordinal alone repeats
        # across a head change and the second attempt would read the
        # first attempt's artefacts.
        for worker in (f"task-001-a11y-{OTHER}-0001",     # another commit
                       "task-001-a11y-0001",              # the pre-SHA form
                       f"task-001-a11y-{HEAD[:12]}-0001",  # a prefix
                       f"task-001-a11y-{HEAD}-0002"):     # another attempt
            with self.subTest(worker=worker):
                self.check("CLAIM_WORKER_PROVENANCE_MISMATCH", worker=worker)

    def test_a_lease_that_does_not_outlast_its_claim_is_refused(self):
        self.check("CLAIM_LEASE_NOT_AFTER_CLAIM", lease_expires_at=CLAIMED_AT)
        self.check("CLAIM_LEASE_NOT_AFTER_CLAIM",
                   lease_expires_at=clock.iso(_T0 - timedelta(seconds=1)))

    def test_a_non_canonical_timestamp_is_refused(self):
        self.check("CLAIM_TIMESTAMP_INVALID", claimed_at="2026-10-01")
        self.check("CLAIM_TIMESTAMP_INVALID", lease_expires_at=None)

    def test_the_automated_verdict_can_never_satisfy_the_qualitative_leg(self):
        self.check("CLAIM_VERDICT_UNRECOGNISED",
                   verdict=ac.ACCESSIBILITY_AUTO_PASS)

    def test_unparseable_is_never_a_claim_verdict(self):
        self.check("CLAIM_VERDICT_UNRECOGNISED",
                   verdict=ac.ACCESSIBILITY_UNPARSEABLE)

    def test_every_qualitative_failure_reason_is_accepted(self):
        for reason in sorted(ac.ACCESSIBILITY_FAILURE_REASONS):
            with self.subTest(reason=reason):
                self.assertTrue(routing.accessibility_review_claim_is_valid(
                    review(verdict=None, reason=reason))[0], reason)


class NeverRaisesCase(unittest.TestCase):
    """§36.5 D1's lesson, applied to the new validators up front."""

    def test_no_field_of_either_claim_can_raise_out_of_its_validator(self):
        cases = (
            (routing.accessibility_auto_claim_is_valid, auto,
             routing.ACCESSIBILITY_AUTO_CLAIM_KEYS),
            (routing.accessibility_review_claim_is_valid, review,
             routing.ACCESSIBILITY_REVIEW_CLAIM_KEYS),
        )
        for validate, build, keys in cases:
            for field in sorted(keys):
                for value in HOSTILE:
                    with self.subTest(validator=validate.__name__,
                                      field=field, value=repr(value)):
                        try:
                            ok, why = validate(build(**{field: value}))
                        except Exception as exc:  # noqa: BLE001 - the point
                            self.fail(f"{field}={value!r} raised "
                                      f"{type(exc).__name__}: {exc}")
                        self.assertFalse(ok)
                        self.assertTrue(why)

    def test_an_unhashable_verdict_or_state_is_refused_not_raised(self):
        for value in ({}, [], {"v": 1}):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    routing.accessibility_auto_claim_is_valid(
                        auto(verdict=value))[1], "CLAIM_VERDICT_UNRECOGNISED")
                self.assertEqual(
                    routing.accessibility_review_claim_is_valid(
                        review(claim_state=value))[1], "CLAIM_STATE_INVALID")


class DiagnosticSeparationCase(unittest.TestCase):
    """Claim diagnostics and attempt failure reasons stay distinct."""

    def test_no_claim_diagnostic_is_an_attempt_failure_reason(self):
        seen = set()
        broken = [auto(sha="nope"), auto(port=0), auto(attempt_id="x"),
                  auto(claim_state="NOPE"), auto(verdict=None, reason=""),
                  review(ordinal=0), review(worker="../x"),
                  review(lease_expires_at=CLAIMED_AT)]
        for claim in broken:
            validate = (routing.accessibility_auto_claim_is_valid
                        if set(claim) == routing.ACCESSIBILITY_AUTO_CLAIM_KEYS
                        else routing.accessibility_review_claim_is_valid)
            ok, why = validate(claim)
            self.assertFalse(ok)
            seen.add(why)
        self.assertTrue(seen)
        self.assertFalse(seen & ac.ACCESSIBILITY_FAILURE_REASONS)
        self.assertFalse(seen & ac.ACCESSIBILITY_AUTO_FAILURE_REASONS)


if __name__ == "__main__":
    unittest.main()
