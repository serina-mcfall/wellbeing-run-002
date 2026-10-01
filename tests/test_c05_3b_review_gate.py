"""C-05.3b foundations: the composite REVIEW gate predicate.

The defect this closes is stated at control/supervisor.py's end of
ingest_security: accessibility is a required evidence class, so
SECURITY_PASS deliberately does not advance to REVIEW and a passing task
holds in WAITING_EVIDENCE forever. The predicate here is the decision
that edge needs - PURE, over (record, head), reading no state.

It is NOT yet wired. These tests drive it with synthetic PR records,
which is the whole point of building it as a leaf: it can be proved
before the claims it reads exist.

THE RULE UNDER TEST, and the one worth stating twice: every leg is
compared to the head observed this tick, never to another leg. Pairwise
agreement would accept three claims that agree perfectly with one another
and all describe a commit that has since been superseded.
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
STALE = "b" * 40

_CLAIMED = clock.now("UTC").replace(microsecond=0)
CLAIMED_AT = clock.iso(_CLAIMED)
EXPIRES_AT = clock.iso(_CLAIMED + timedelta(seconds=1800))


# A sentinel, not None: `verdict=None` is a case these tests must be able
# to express - a COMPLETE claim that says nothing is one of the shapes the
# gate has to refuse - so "not supplied" cannot also be spelled None.
DEFAULT = object()


def security_leg(sha=HEAD, *, state="COMPLETE", verdict=DEFAULT, reason=""):
    """A security claim in exactly the nine-key C-05.3a shape."""
    if verdict is DEFAULT:
        verdict = routing.SECURITY_PASS if state == "COMPLETE" else None
    return {
        "sha": sha,
        "ordinal": 1,
        "attempt_id": routing.SECURITY_ATTEMPT_FMT.format(1),
        "worker": routing.security_worker_name("task-001", sha, 1),
        "claim_state": state,
        "claimed_at": CLAIMED_AT,
        "lease_expires_at": EXPIRES_AT,
        "verdict": verdict,
        "reason": reason,
    }


def auto_leg(sha=HEAD, *, state="COMPLETE", verdict=DEFAULT, reason=""):
    """An accessibility_auto claim in the seven-key shape section 31.5
    specifies. Its own validator belongs with the claim builder, which is
    a later stage; the gate must still refuse a malformed one."""
    if verdict is DEFAULT:
        verdict = routing.ACCESSIBILITY_AUTO_PASS if state == "COMPLETE" else None
    return {
        "sha": sha,
        "attempt_id": "attempt-0001",
        "claim_state": state,
        "claimed_at": CLAIMED_AT,
        "port": 3200,
        "verdict": verdict,
        "reason": reason,
    }


def review_leg(sha=HEAD, *, state="COMPLETE", verdict=DEFAULT, reason=""):
    """An accessibility_review claim in the nine-key shape section 31.7
    specifies."""
    if verdict is DEFAULT:
        verdict = routing.ACCESSIBILITY_PASS if state == "COMPLETE" else None
    return {
        "sha": sha,
        "ordinal": 1,
        "attempt_id": "a11y-attempt-0001",
        "worker": f"task-001-a11y-{sha}-0001",
        "claim_state": state,
        "claimed_at": CLAIMED_AT,
        "lease_expires_at": EXPIRES_AT,
        "verdict": verdict,
        "reason": reason,
    }


LEG_BUILDERS = {
    "security_evidence": security_leg,
    "accessibility_auto": auto_leg,
    "accessibility_review": review_leg,
}


def record(**overrides):
    """A PR record with all three legs complete and passing at HEAD,
    unless overridden. `None` as an override removes the leg."""
    doc = {key: build() for key, build in LEG_BUILDERS.items()}
    doc["number"] = 7
    for key, value in overrides.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return doc


class GateFiresCase(unittest.TestCase):

    def test_all_three_legs_complete_and_passing_at_the_head_advances(self):
        self.assertTrue(routing.review_gate_fires(record(), HEAD))

    def test_the_three_legs_are_exactly_these(self):
        self.assertEqual([key for key, _ in routing.REVIEW_GATE_LEGS],
                         list(LEG_BUILDERS))
        self.assertEqual([verdict for _, verdict in routing.REVIEW_GATE_LEGS],
                         [routing.SECURITY_PASS,
                          routing.ACCESSIBILITY_AUTO_PASS,
                          routing.ACCESSIBILITY_PASS])


class MissingLegCase(unittest.TestCase):
    """An absent leg is absent evidence. It is never a pass."""

    def test_any_single_missing_leg_holds_the_gate(self):
        for key in LEG_BUILDERS:
            with self.subTest(missing=key):
                self.assertFalse(
                    routing.review_gate_fires(record(**{key: None}), HEAD))

    def test_security_pass_alone_does_not_advance(self):
        # The exact defect C-05.3b exists to close, from the other side:
        # it must stay closed for a record carrying only security.
        doc = record(accessibility_auto=None, accessibility_review=None)
        self.assertFalse(routing.review_gate_fires(doc, HEAD))

    def test_an_empty_record_holds(self):
        self.assertFalse(routing.review_gate_fires({}, HEAD))

    def test_a_record_that_is_not_an_object_holds(self):
        for value in (None, [], "", 0, object()):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(routing.review_gate_fires(value, HEAD))


class HeadBindingCase(unittest.TestCase):
    """Every leg is compared to the head observed this tick."""

    def test_any_single_leg_at_another_commit_holds_the_gate(self):
        for key, build in LEG_BUILDERS.items():
            with self.subTest(stale=key):
                self.assertFalse(
                    routing.review_gate_fires(record(**{key: build(STALE)}),
                                              HEAD))

    def test_a_unanimously_stale_evidence_set_holds_the_gate(self):
        # Three claims that agree perfectly with one another and all
        # describe a superseded commit. Pairwise comparison would advance
        # this; comparison to the head does not.
        doc = {key: build(STALE) for key, build in LEG_BUILDERS.items()}
        self.assertFalse(routing.review_gate_fires(doc, HEAD))
        # And it is genuinely self-consistent - the mutation is the
        # comparison, not the data.
        self.assertEqual({doc[key]["sha"] for key in LEG_BUILDERS}, {STALE})

    def test_a_head_move_needs_no_reset_path(self):
        doc = record()
        self.assertTrue(routing.review_gate_fires(doc, HEAD))
        moved = "c" * 40
        self.assertFalse(routing.review_gate_fires(doc, moved))
        # Nothing was cleared or rewritten by asking.
        self.assertEqual(doc, record())

    def test_an_unknown_or_malformed_head_never_fires(self):
        for head in (None, "", "deadbeef", HEAD.upper(), 40 * "z", 7):
            with self.subTest(head=head):
                doc = {key: build(head if isinstance(head, str) else HEAD)
                       for key, build in LEG_BUILDERS.items()}
                self.assertFalse(routing.review_gate_fires(doc, head))


class IncompleteLegCase(unittest.TestCase):
    """A leg that has not finished is not a pass."""

    def test_any_leg_not_complete_holds_the_gate(self):
        for key, build in LEG_BUILDERS.items():
            for state in ("PLANNED", "SPAWNED", "RUNNING", "", None):
                with self.subTest(leg=key, state=state):
                    leg = build(state=state, verdict=None)
                    self.assertFalse(
                        routing.review_gate_fires(record(**{key: leg}), HEAD))

    def test_a_leg_carrying_a_failure_reason_holds_the_gate(self):
        for key, build in LEG_BUILDERS.items():
            with self.subTest(leg=key):
                leg = build(reason=ac.TIMED_OUT)
                self.assertFalse(
                    routing.review_gate_fires(record(**{key: leg}), HEAD))

    def test_a_leg_that_is_not_an_object_holds_the_gate(self):
        for key in LEG_BUILDERS:
            for value in ("COMPLETE", [], 1, True):
                with self.subTest(leg=key, value=value):
                    self.assertFalse(
                        routing.review_gate_fires(record(**{key: value}), HEAD))


class VerdictCase(unittest.TestCase):
    """Each leg must carry ITS OWN passing verdict, and no other."""

    def test_a_failing_verdict_on_any_leg_holds_the_gate(self):
        failing = {
            "security_evidence": routing.SECURITY_FAIL,
            "accessibility_auto": routing.ACCESSIBILITY_AUTO_FAIL,
            "accessibility_review": routing.ACCESSIBILITY_FAIL,
        }
        for key, verdict in failing.items():
            with self.subTest(leg=key):
                leg = LEG_BUILDERS[key](verdict=verdict)
                self.assertFalse(
                    routing.review_gate_fires(record(**{key: leg}), HEAD))

    def test_an_unparseable_verdict_is_not_a_pass(self):
        for key, token in (("security_evidence", routing.SECURITY_UNPARSEABLE),
                           ("accessibility_auto",
                            routing.ACCESSIBILITY_UNPARSEABLE),
                           ("accessibility_review",
                            routing.ACCESSIBILITY_UNPARSEABLE)):
            with self.subTest(leg=key):
                leg = LEG_BUILDERS[key](verdict=token)
                self.assertFalse(
                    routing.review_gate_fires(record(**{key: leg}), HEAD))

    def test_an_absent_or_junk_verdict_is_not_a_pass(self):
        for key in LEG_BUILDERS:
            for verdict in (None, "", "PASS", "OK", True, {}):
                with self.subTest(leg=key, verdict=verdict):
                    leg = LEG_BUILDERS[key](verdict=verdict)
                    self.assertFalse(
                        routing.review_gate_fires(record(**{key: leg}), HEAD))

    def test_one_legs_verdict_cannot_satisfy_another_leg(self):
        # The two accessibility halves have deliberately distinct tokens.
        # An automated pass standing in for the qualitative review is the
        # collapse agents/ACCESSIBILITY.md:8 forbids.
        doc = record(accessibility_review=review_leg(
            verdict=routing.ACCESSIBILITY_AUTO_PASS))
        self.assertFalse(routing.review_gate_fires(doc, HEAD))
        doc = record(accessibility_auto=auto_leg(
            verdict=routing.ACCESSIBILITY_PASS))
        self.assertFalse(routing.review_gate_fires(doc, HEAD))


class SecurityClaimValidityCase(unittest.TestCase):
    """The one leg whose validator exists gets it."""

    def test_a_structurally_plausible_but_invalid_security_claim_holds(self):
        # Four visible fields read perfectly; the worker name addresses a
        # different attempt's artefacts, which security_claim_is_valid
        # refuses as CLAIM_WORKER_PROVENANCE_MISMATCH.
        leg = security_leg()
        leg["worker"] = "someone-elses-worker"
        self.assertFalse(routing.security_claim_is_valid(leg)[0])
        self.assertFalse(
            routing.review_gate_fires(record(security_evidence=leg), HEAD))

    def test_an_unhashable_verdict_holds_the_gate_rather_than_raising(self):
        # security_claim_is_valid raises TypeError on `verdict in
        # SECURITY_VERDICTS` when the verdict is a dict or a list, both of
        # which durable JSON can hold. That is a C-05.3a defect, recorded
        # rather than patched from here. This gate must still DECIDE: a
        # crash mid-tick is not a hold, it is an unhandled exception in
        # the supervisor's own transaction.
        for verdict in ({}, [], {"verdict": "SECURITY_PASS"}):
            with self.subTest(verdict=verdict):
                leg = security_leg(verdict=verdict)
                with self.assertRaises(TypeError):
                    routing.security_claim_is_valid(leg)
                self.assertFalse(
                    routing.review_gate_fires(record(security_evidence=leg),
                                              HEAD))

    def test_the_valid_security_claim_used_by_these_tests_really_is_valid(self):
        # Otherwise every assertion above would pass for the wrong reason.
        ok, diagnostic = routing.security_claim_is_valid(security_leg())
        self.assertTrue(ok, diagnostic)


class PurityCase(unittest.TestCase):

    def test_asking_twice_gives_the_same_answer_and_mutates_nothing(self):
        doc = record()
        before = repr(doc)
        self.assertTrue(routing.review_gate_fires(doc, HEAD))
        self.assertTrue(routing.review_gate_fires(doc, HEAD))
        self.assertEqual(repr(doc), before)

    def test_unrelated_record_keys_are_ignored(self):
        doc = record()
        doc["approval"] = {"verdict": "REVIEW_FAIL"}
        doc["merged"] = False
        self.assertTrue(routing.review_gate_fires(doc, HEAD))


if __name__ == "__main__":
    unittest.main()
