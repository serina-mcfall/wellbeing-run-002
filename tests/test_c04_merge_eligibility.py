"""C-04: SHA-bound merge eligibility and the draft condition.

Two defects on the Python half of the merge-eligibility path, both proved
here against the behaviour that preceded the fix:

  1. evaluate_merge compared only the material DIFF hash. Protocol v2
     "Evidence provenance" says "new SHA => regenerate required automated
     evidence", and the head SHA is the thing that rule names. A force-push
     that reproduces a byte-identical diff from a different commit therefore
     merged a commit nothing had reviewed - record["reviewed_head"] was
     recorded at dispatch and never once compared at merge time.

  2. A closed pull request and a draft one shared a single refusal sentence,
     so MERGE_BLOCKED could not distinguish them. A draft could stall a task
     with nothing in durable evidence saying it was a draft that did it.

Nothing here marks a pull request ready for review. Draft status is not
governed by protocol/RUN-002-PROTOCOL-v2.0.md - the word does not appear in
it - so automating it would amend a frozen specification. The gate reports
the condition and changes nothing. See handover section 34.
"""

import unittest

from control import routing

HEAD = "a" * 40
MOVED = "b" * 40
DIFF = "d" * 40


def a_pr(**overrides) -> dict:
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    pr.update(overrides)
    return pr


def a_record(**overrides) -> dict:
    record = routing.blank_pr_record(7, "TASK-001", "task/task-001")
    record.update({
        "review_verdict": routing.REVIEW_PASS,
        "approval_current": True,
        "reviewed_head": HEAD,
        "reviewed_diff_hash": DIFF,
    })
    record.update(overrides)
    return record


def evaluate(pr=None, record=None, diff_hash=DIFF):
    return routing.evaluate_merge(
        pr if pr is not None else a_pr(),
        record if record is not None else a_record(),
        ("ci",), False, diff_hash)


class HeadShaBindingCase(unittest.TestCase):
    """The head SHA, not only the diff, binds an approval to a commit."""

    def test_the_baseline_fixture_merges(self):
        # Without this, every denial below could be passing for the wrong
        # reason - a fixture that can never merge proves nothing.
        decision = evaluate()
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.condition, routing.MERGE_OK)

    def test_a_force_push_with_an_identical_diff_is_refused(self):
        # THE defect. The diff hash is unchanged - a force-push can rewrite
        # history and still produce byte-identical output - so the old gate
        # saw nothing wrong and merged a commit no reviewer ever saw.
        decision = evaluate(pr=a_pr(headRefOid=MOVED), diff_hash=DIFF)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_HEAD_CHANGED)
        self.assertTrue(decision.invalidate_approval)

    def test_a_head_that_moved_invalidates_the_approval(self):
        # Not merely blocked: the approval must be withdrawn, or the next tick
        # would re-evaluate the same stale approval against the new head.
        self.assertTrue(evaluate(pr=a_pr(headRefOid=MOVED)).invalidate_approval)

    def test_a_record_with_no_reviewed_head_is_refused(self):
        decision = evaluate(record=a_record(reviewed_head=None))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_HEAD_UNVERIFIABLE)
        self.assertTrue(decision.invalidate_approval)

    def test_an_observation_with_no_head_is_refused(self):
        pr = a_pr()
        del pr["headRefOid"]
        decision = evaluate(pr=pr)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_HEAD_UNVERIFIABLE)

    def test_two_absent_heads_do_not_cancel_out(self):
        # The fail-open shape this guard exists to prevent: None == None is
        # True, so "nothing to compare" must never read as "nothing changed".
        pr = a_pr()
        del pr["headRefOid"]
        decision = evaluate(pr=pr, record=a_record(reviewed_head=None))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_HEAD_UNVERIFIABLE)

    def test_the_head_check_runs_before_the_diff_check(self):
        # Both are wrong; the head is the stronger and more specific fact, and
        # is the one Protocol v2 names, so it must be the reported condition.
        decision = evaluate(pr=a_pr(headRefOid=MOVED), diff_hash="different")
        self.assertEqual(decision.condition, routing.MERGE_HEAD_CHANGED)

    def test_the_diff_check_still_blocks_on_its_own(self):
        # The head check does not replace the diff check. A reconciliation
        # merge moves the head legitimately; a changed diff under an unchanged
        # head is a different fault, and both must still be caught.
        decision = evaluate(diff_hash="different")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_DIFF_CHANGED)

    def test_blank_pr_record_declares_reviewed_head_as_explicit_absence(self):
        self.assertIsNone(routing.blank_pr_record(1, "TASK-001", "b")["reviewed_head"])


class DraftConditionCase(unittest.TestCase):
    """A draft pull request is a reported condition, never a silent stall."""

    def test_a_draft_is_refused_with_its_own_condition_code(self):
        decision = evaluate(pr=a_pr(isDraft=True))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_PR_IS_DRAFT)

    def test_a_closed_pull_request_is_a_different_condition(self):
        decision = evaluate(pr=a_pr(state="CLOSED"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_PR_NOT_OPEN)

    def test_draft_and_closed_do_not_share_a_reason(self):
        draft = evaluate(pr=a_pr(isDraft=True))
        closed = evaluate(pr=a_pr(state="CLOSED"))
        self.assertNotEqual(draft.condition, closed.condition)
        self.assertNotEqual(draft.reason, closed.reason)

    def test_the_draft_reason_names_the_action_a_human_would_take(self):
        self.assertIn("ready for review", evaluate(pr=a_pr(isDraft=True)).reason)

    def test_a_draft_does_not_invalidate_an_otherwise_current_approval(self):
        # Being in draft says nothing about whether the review is still valid.
        # Withdrawing the approval would force a pointless re-review once the
        # pull request is marked ready.
        self.assertFalse(evaluate(pr=a_pr(isDraft=True)).invalidate_approval)

    def test_nothing_in_routing_marks_a_pull_request_ready(self):
        # Draft status is ungoverned; the smallest honest behaviour is to
        # report it. If an amendment later authorises the transition, this
        # test is the thing that should be changed deliberately.
        source = (routing.__file__,)
        with open(source[0], encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("ready-for-review", text)
        self.assertNotIn("pr ready", text.lower())


class ConditionVocabularyCase(unittest.TestCase):
    """Every refusal carries a finite code, so durable evidence need not be
    read as English."""

    def test_every_refusal_path_sets_a_condition(self):
        cases = [
            (a_pr(), a_record(review_verdict=None), DIFF, routing.MERGE_NO_REVIEW_PASS),
            (a_pr(), a_record(approval_current=False), DIFF, routing.MERGE_APPROVAL_STALE),
            (a_pr(state="CLOSED"), a_record(), DIFF, routing.MERGE_PR_NOT_OPEN),
            (a_pr(isDraft=True), a_record(), DIFF, routing.MERGE_PR_IS_DRAFT),
            (a_pr(headRefOid=MOVED), a_record(), DIFF, routing.MERGE_HEAD_CHANGED),
            (a_pr(), a_record(), None, routing.MERGE_DIFF_UNVERIFIABLE),
            (a_pr(), a_record(), "other", routing.MERGE_DIFF_CHANGED),
            (a_pr(statusCheckRollup=[]), a_record(), DIFF, routing.MERGE_CI_NOT_SATISFIED),
            (a_pr(mergeable="CONFLICTING"), a_record(), DIFF, routing.MERGE_CONFLICTING),
            (a_pr(mergeStateStatus="BEHIND"), a_record(), DIFF, routing.MERGE_BEHIND),
            (a_pr(mergeStateStatus="BLOCKED"), a_record(), DIFF, routing.MERGE_STATE_BLOCKED),
        ]
        for pr, record, diff, expected in cases:
            with self.subTest(expected=expected):
                decision = routing.evaluate_merge(pr, record, ("ci",), False, diff)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.condition, expected)

    def test_the_red_guardrail_condition(self):
        decision = routing.evaluate_merge(a_pr(), a_record(), ("ci",), True, DIFF)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_GUARDRAIL_RED)

    def test_the_condition_codes_are_distinct(self):
        codes = [
            routing.MERGE_OK, routing.MERGE_GUARDRAIL_RED, routing.MERGE_NO_REVIEW_PASS,
            routing.MERGE_APPROVAL_STALE, routing.MERGE_PR_NOT_OPEN,
            routing.MERGE_PR_IS_DRAFT, routing.MERGE_HEAD_UNVERIFIABLE,
            routing.MERGE_HEAD_CHANGED, routing.MERGE_DIFF_UNVERIFIABLE,
            routing.MERGE_DIFF_CHANGED, routing.MERGE_CI_NOT_SATISFIED,
            routing.MERGE_CONFLICTING, routing.MERGE_BEHIND, routing.MERGE_STATE_BLOCKED,
        ]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main()
