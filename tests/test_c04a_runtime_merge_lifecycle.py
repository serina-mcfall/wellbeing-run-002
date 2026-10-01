"""C-04a through the gate the SUPERVISOR ACTUALLY USES.

`apparatus/fixture-preflight/production-lifecycle.test.js` drives the same
lifecycle through `apparatus/pr-evidence/live-gate.js`. That proves the
composition layer — but **nothing in `control/` calls it**. The Supervisor
calls `routing.evaluate_merge` (`supervisor.py:3728`), so a fixture that
only exercises the JavaScript proves a gate the runtime never reaches.

This file is the runtime half: the same multi-cycle shape, decided by
`routing.evaluate_merge`.

THE HOLE THIS CLOSES, reproduced before it was fixed. `evaluate_merge`
consulted no evidence at all. A record carrying REVIEW_PASS, a current
approval, a matching head and diff, green CI and a CLEAN merge state —
but **no security evidence and neither accessibility leg** — returned
`MergeDecision(allowed=True, "all merge gates satisfied")`.

It was reachable in ordinary routing, not only in theory:
`route_awaiting_dispatch` dispatches a reviewer whenever the task is in
`ROUTING_STATES`, which includes `PR_OPEN`, and that transitions straight
to `REVIEW`. `WAITING_EVIDENCE` is entered only from
`EVIDENCE_STATES = ("PR_OPEN", "WAITING_EVIDENCE")`, and `REVIEW` is not
one of them — so a task reviewed before its evidence was claimed never
enters the evidence state, and nothing downstream noticed.

WHAT THIS FILE DOES NOT CLAIM. `evaluate_merge` gates on evidence
CLAIMS — is each required class a completed pass at this head. It does
NOT apply `control/severity.py::apply_severity_policy` to findings; that
function still has no caller in `control/`, so the deterministic P0/P1
floor and the requirement-registry validity check run only in
`apparatus/`, which the runtime does not invoke. See handover §38.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import routing  # noqa: E402
from mergeable_evidence import complete_evidence  # noqa: E402

SHA1 = "1" * 40
SHA2 = "2" * 40
DIFF1 = "d1" * 20
DIFF2 = "d2" * 20


def a_pr(head=SHA1, **overrides):
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    pr.update(overrides)
    return pr


def reviewed_record(head=SHA1, diff=DIFF1, *, evidence_at=None, **overrides):
    """A PR record whose code review passed at `head`.

    `evidence_at` is the head the evidence legs are bound to, defaulting to
    the reviewed head. Passing a different value is how staleness is
    expressed; passing None leaves the record with no evidence at all.
    """
    record = routing.blank_pr_record(7, "TASK-001", "run-002/task-001")
    record.update({
        "review_verdict": routing.REVIEW_PASS,
        "approval_current": True,
        "reviewed_head": head,
        "reviewed_diff_hash": diff,
        "review_cycles": 1,
    })
    if evidence_at is not None:
        record.update(complete_evidence(evidence_at))
    record.update(overrides)
    return record


def decide(pr, record, diff):
    return routing.evaluate_merge(pr, record, ("ci",), False, diff)


class TheHoleCase(unittest.TestCase):
    """A merge with no evidence at all. This is the regression."""

    def test_a_fully_approved_pr_with_no_evidence_is_refused(self):
        record = reviewed_record(evidence_at=None)
        self.assertIsNone(record.get("security_evidence"))
        self.assertIsNone(record.get("accessibility_auto"))
        self.assertIsNone(record.get("accessibility_review"))

        decision = decide(a_pr(), record, DIFF1)
        self.assertFalse(
            decision.allowed,
            "a PR with no security or accessibility evidence was allowed to "
            "merge; this is the defect this file exists for")
        self.assertEqual(decision.condition, routing.MERGE_EVIDENCE_INCOMPLETE)

    def test_the_same_record_WITH_evidence_merges(self):
        # The control. Without it the refusal above could be caused by
        # anything in the fixture rather than by the missing evidence.
        decision = decide(a_pr(), reviewed_record(evidence_at=SHA1), DIFF1)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.condition, routing.MERGE_OK)

    def test_each_required_leg_is_independently_load_bearing(self):
        for leg in ("security_evidence", "accessibility_auto",
                    "accessibility_review"):
            with self.subTest(missing=leg):
                record = reviewed_record(evidence_at=SHA1)
                del record[leg]
                decision = decide(a_pr(), record, DIFF1)
                self.assertFalse(decision.allowed, f"{leg} was not required")
                self.assertEqual(decision.condition,
                                 routing.MERGE_EVIDENCE_INCOMPLETE)

    def test_an_incomplete_leg_is_not_a_pass(self):
        for leg in ("security_evidence", "accessibility_auto",
                    "accessibility_review"):
            with self.subTest(planned=leg):
                record = reviewed_record(evidence_at=SHA1)
                record[leg]["claim_state"] = "PLANNED"
                record[leg]["verdict"] = None
                self.assertFalse(decide(a_pr(), record, DIFF1).allowed)

    def test_a_failed_leg_is_not_a_pass(self):
        record = reviewed_record(evidence_at=SHA1)
        record["security_evidence"]["verdict"] = routing.SECURITY_FAIL
        self.assertFalse(decide(a_pr(), record, DIFF1).allowed)


class ExactShaBindingCase(unittest.TestCase):
    """Evidence is bound to the head GitHub reports, not to the record."""

    def test_evidence_from_the_previous_head_is_refused(self):
        # Everything else is current: the review is at SHA2, the diff
        # matches, CI is green. Only the evidence belongs to SHA1.
        record = reviewed_record(head=SHA2, diff=DIFF2, evidence_at=SHA1)
        decision = decide(a_pr(head=SHA2), record, DIFF2)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_EVIDENCE_INCOMPLETE)

    def test_regenerating_the_evidence_at_the_new_head_clears_it(self):
        record = reviewed_record(head=SHA2, diff=DIFF2, evidence_at=SHA2)
        self.assertTrue(decide(a_pr(head=SHA2), record, DIFF2).allowed)

    def test_evidence_is_compared_to_the_OBSERVED_head_not_the_claimed_one(self):
        # A record that lies about which head was reviewed must not also get
        # to choose the head its evidence is checked against. Here the record
        # claims SHA1 and carries SHA1 evidence, but GitHub reports SHA2.
        record = reviewed_record(head=SHA1, diff=DIFF1, evidence_at=SHA1)
        decision = decide(a_pr(head=SHA2), record, DIFF1)
        self.assertFalse(decision.allowed)
        # The head check fires first, which is correct: the review itself is
        # stale, and that is a stronger statement than stale evidence.
        self.assertEqual(decision.condition, routing.MERGE_HEAD_CHANGED)
        self.assertTrue(decision.invalidate_approval)


class MultiCycleCase(unittest.TestCase):
    """Builder -> review FAIL -> fix -> re-review -> merge, at two heads."""

    def test_cycle_one_fails_and_does_not_merge(self):
        record = reviewed_record(evidence_at=SHA1,
                                 review_verdict=routing.REVIEW_FAIL)
        decision = decide(a_pr(), record, DIFF1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_NO_REVIEW_PASS)

    def test_the_cycle_one_evidence_set_cannot_carry_the_cycle_two_head(self):
        # The fixer moved the head to SHA2 and a fresh review passed there,
        # but the evidence was never regenerated.
        record = reviewed_record(head=SHA2, diff=DIFF2, evidence_at=SHA1,
                                 review_cycles=2)
        self.assertFalse(decide(a_pr(head=SHA2), record, DIFF2).allowed)

    def test_the_second_cycle_merges_once_everything_is_regenerated(self):
        record = reviewed_record(head=SHA2, diff=DIFF2, evidence_at=SHA2,
                                 review_cycles=2)
        decision = decide(a_pr(head=SHA2), record, DIFF2)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(record["review_cycles"], 2)


class OrderingCase(unittest.TestCase):
    """Evidence does not substitute for review, and review does not
    substitute for evidence."""

    def test_complete_evidence_cannot_merge_without_a_code_review_pass(self):
        record = reviewed_record(evidence_at=SHA1, review_verdict=None,
                                 approval_current=False)
        decision = decide(a_pr(), record, DIFF1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_NO_REVIEW_PASS)

    def test_a_draft_still_blocks_even_with_complete_evidence(self):
        decision = decide(a_pr(isDraft=True), reviewed_record(evidence_at=SHA1),
                          DIFF1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_PR_IS_DRAFT)

    def test_the_red_guardrail_still_wins_over_everything(self):
        decision = routing.evaluate_merge(
            a_pr(), reviewed_record(evidence_at=SHA1), ("ci",), True, DIFF1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_GUARDRAIL_RED)

    def test_failing_ci_still_blocks_with_complete_evidence(self):
        pr = a_pr(statusCheckRollup=[
            {"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}])
        decision = decide(pr, reviewed_record(evidence_at=SHA1), DIFF1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_CI_NOT_SATISFIED)


class TheGateIsTheOneTheSupervisorCallsCase(unittest.TestCase):
    """The point of this file, asserted rather than assumed."""

    def test_evaluate_merge_consults_the_c05_3b_review_gate(self):
        # `review_gate_fires` was built, proved, and called by nothing.
        # If this stops being true, the runtime has silently stopped
        # requiring evidence again.
        record = reviewed_record(evidence_at=SHA1)
        self.assertTrue(routing.review_gate_fires(record, SHA1))
        self.assertTrue(decide(a_pr(), record, DIFF1).allowed)

        del record["accessibility_auto"]
        self.assertFalse(routing.review_gate_fires(record, SHA1))
        self.assertFalse(
            decide(a_pr(), record, DIFF1).allowed,
            "review_gate_fires refuses but evaluate_merge allows — the "
            "runtime is no longer using the gate C-05.3b proved")

    def test_the_condition_code_is_finite_and_distinct(self):
        # Durable evidence must not have to be parsed as English, and
        # "no evidence" must be distinguishable from every other refusal.
        self.assertEqual(routing.MERGE_EVIDENCE_INCOMPLETE,
                         "EVIDENCE_INCOMPLETE")
        others = {v for k, v in vars(routing).items()
                  if k.startswith("MERGE_") and isinstance(v, str)
                  and k != "MERGE_EVIDENCE_INCOMPLETE"}
        self.assertNotIn(routing.MERGE_EVIDENCE_INCOMPLETE, others)


if __name__ == "__main__":
    unittest.main()
