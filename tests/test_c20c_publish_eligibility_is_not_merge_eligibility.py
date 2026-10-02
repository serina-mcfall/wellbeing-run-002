"""The pending-review flag's contract, stated and tested.

WHAT THE FLAG MEANS. `blockedOnlyByPendingIndependentReview` establishes
ONE thing: that this gate is ELIGIBLE TO PUBLISH its own independent-review
result for this head. It exists to break the F5 deadlock - once
`run-002/independent-review` is a required context, GitHub reports BLOCKED
until something publishes it, and the thing that publishes it is this gate.

WHAT IT DOES NOT MEAN, AND THE DOCUMENT USED TO SAY OTHERWISE. It is NOT a
claim that every branch-protection condition is satisfied. GitHub collapses
every unsatisfied protection rule into one `mergeStateStatus: BLOCKED`, and
`live-gate.js` raises exactly one reason code for it, so the flag cannot
tell its own missing context from a second required check, an unresolved
conversation, or a restored approving-review requirement.

THE SAFETY ARGUMENT IS NOT "THERE ARE ONLY TWO CONTEXTS." That was the
earlier reasoning and it rested on a branch-protection shape nothing
enforces - anyone adding a third requirement would have silently invalidated
it. The argument that holds is structural and is what this file tests:

  * PUBLISHING is a side effect on a status context. It merges nothing.
  * MERGING requires `routing.evaluate_merge` to allow, and that function
    denies on `mergeStateStatus == BLOCKED` without ever reading the flag -
    it cannot read it; the flag is not among its arguments.

So an unrelated unmet requirement leaves the pull request unmerged no
matter what was published, for any number of required contexts.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import publisher, routing
from mergeable_evidence import with_complete_evidence  # noqa: E402

HEAD = "a" * 40
ON = {publisher.ENABLE_ENV: "1", publisher.TOKEN_ENV: "simulated-not-a-token"}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, context, state, sha):
        self.calls.append((context, state, sha))
        return True


def pending_decision():
    """What the gate emits when only a protection rule holds the PR back."""
    return {"decision": "DENIED", "trustedHeadSha": HEAD,
            "blockedOnlyByPendingIndependentReview": True,
            "reasons": [{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"}]}


def blocked_pr():
    return {"number": 100, "state": "OPEN", "isDraft": False,
            "headRefOid": HEAD, "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci",
                                   "status": "COMPLETED",
                                   "conclusion": "SUCCESS"}]}


def approved_record():
    """Merge-ready in EVERY respect except the protection state.

    The evidence legs matter: without them `evaluate_merge` denies
    EVIDENCE_INCOMPLETE before it ever reaches `mergeStateStatus`, and a
    test asserting the protection refusal would be passing for the wrong
    reason entirely.
    """
    record = {"reviewed_head": HEAD, "approval_current": True,
              "review_verdict": routing.REVIEW_PASS,
              "reviewed_diff_hash": "d1", "review_cycles": 1}
    return with_complete_evidence(record, HEAD)


class ThePublishSideCase(unittest.TestCase):
    """The flag does what it is for: it authorises a publication."""

    def test_the_flag_earns_a_success_status(self):
        poster = Recorder()
        result = publisher.publish(pending_decision(), HEAD, poster=poster,
                                   environ=ON)
        self.assertTrue(result.posted)
        self.assertEqual(result.state, publisher.SUCCESS)
        self.assertEqual(poster.calls[0][0], publisher.CONTEXT)

    def test_it_publishes_only_against_the_head_the_gate_judged(self):
        poster = Recorder()
        publisher.publish(pending_decision(), "b" * 40, poster=poster,
                          environ=ON)
        self.assertEqual(poster.calls, [])


class TheMergeSideCase(unittest.TestCase):
    """The same pull request, asked the question that actually merges."""

    def decide(self, pr=None):
        return routing.evaluate_merge(
            pr or blocked_pr(), approved_record(), ("ci",),
            red_guardrail_active=False, current_diff_hash="d1")

    def test_a_blocked_pull_request_does_not_merge(self):
        decision = self.decide()
        self.assertFalse(decision.allowed,
                         "a pull request GitHub reports as BLOCKED merged")
        self.assertEqual(decision.condition, routing.MERGE_STATE_BLOCKED)

    def test_publishing_first_does_not_change_the_merge_answer(self):
        """The sequence that matters: publish, then ask to merge."""
        poster = Recorder()
        published = publisher.publish(pending_decision(), HEAD,
                                      poster=poster, environ=ON)
        self.assertTrue(published.posted)
        self.assertFalse(self.decide().allowed,
                         "publishing an independent-review pass made a "
                         "blocked pull request mergeable")

    def test_the_merge_gate_cannot_read_the_flag_at_all(self):
        """Structural, not behavioural - the strongest form of this claim.

        `evaluate_merge` takes a pull-request observation, a record, the
        required checks, the guardrail and a diff hash. The flag is not
        among them and no caller supplies it, so no change to the flag can
        change a merge decision.
        """
        import inspect
        params = set(inspect.signature(routing.evaluate_merge).parameters)
        self.assertNotIn("blockedOnlyByPendingIndependentReview", params)
        source = Path(routing.__file__).read_text(encoding="utf-8")
        self.assertNotIn("blockedOnlyByPendingIndependentReview", source,
                         "the merge authority now references the publish "
                         "flag; publish eligibility and merge eligibility "
                         "have been joined")


class AnyNumberOfUnrelatedRequirementsCase(unittest.TestCase):
    """The claim the old wording leaned on, replaced by one that holds.

    The earlier argument was "the AFTER protection leaves exactly two
    required contexts, so there is no third rule for the flag to hide".
    Nothing enforces that shape. These cases add unrelated unmet
    requirements and show the merge is refused regardless - the refusal
    does not depend on how many contexts exist.
    """

    def decide(self, pr):
        return routing.evaluate_merge(
            pr, approved_record(), ("ci",),
            red_guardrail_active=False, current_diff_hash="d1")

    def test_blocked_still_refuses_with_many_extra_contexts_present(self):
        pr = blocked_pr()
        for n in range(5):
            pr["statusCheckRollup"].append(
                {"__typename": "CheckRun", "name": f"extra-{n}",
                 "status": "COMPLETED", "conclusion": "SUCCESS"})
        self.assertFalse(self.decide(pr).allowed)

    def test_an_unmet_extra_required_check_refuses_on_its_own(self):
        """Not via BLOCKED - directly, because `ci` is not the only rule
        a deployment may declare required."""
        pr = blocked_pr()
        pr["mergeStateStatus"] = "CLEAN"
        decision = routing.evaluate_merge(
            pr, approved_record(), ("ci", "security-scan"),
            red_guardrail_active=False, current_diff_hash="d1")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_CI_NOT_SATISFIED)

    def test_the_flag_being_true_is_irrelevant_to_every_case_above(self):
        """One assertion, stated once, so it cannot be read as incidental."""
        poster = Recorder()
        publisher.publish(pending_decision(), HEAD, poster=poster, environ=ON)
        self.assertTrue(poster.calls, "precondition: a status was published")
        pr = blocked_pr()
        pr["mergeStateStatus"] = "BLOCKED"
        self.assertFalse(self.decide(pr).allowed)


if __name__ == "__main__":
    unittest.main()
