"""Regression tests for evidence-aware review (DEV-005).

Codex reviews with no network and no installed dependencies, so it cannot read
CI and cannot observe keyboard or 375px behaviour. DEV-005 supplies what it
cannot reach, labelled by source. That is only safe if evidence can never
authorise acceptance of code it does not describe.

Required proofs:
  1. evidence is tied to the exact PR head under review
  2. stale CI or human evidence from another SHA cannot authorise acceptance
  3. evidence source and provenance are explicit
  4. reviewer independence and the existing merge gate are unchanged
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import evidence, prompts, routing  # noqa: E402

HEAD = "1db53051dd04c7b154b9a38c54cdc861663b660f"
OTHER = "54ab0f3e680fcb407545d704e3244bcebc1c718d"
REPO = "owner/repo"


class FakeLedger:
    def __init__(self, events):
        self._events = events

    def events(self, event_type=None):
        return [e for e in self._events
                if event_type is None or e.get("event_type") == event_type]


def human_event(head: str) -> dict:
    return {"event_type": "HUMAN_VERIFICATION",
            "metadata_redacted": {
                "verified_by": "human (experiment authority)", "verified_head": head,
                "method": "production build at the PR head",
                "mobile_375px": {"result": "PASS", "items": "M1-M8"},
                "keyboard_focus": {"result": "PASS", "items": "K1-K10"}}}


def ci_stdout(name: str, conclusion: str, sha: str) -> str:
    return f"{name}|completed|{conclusion}|{sha}"


class TestBoundToExactHead(unittest.TestCase):
    """Proof 1: evidence is tied to the exact head under review."""

    def test_ci_and_human_evidence_for_this_head_apply(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout=ci_stdout("ci", "success",
                                                                       HEAD))):
            items = evidence.collect(REPO, HEAD, FakeLedger([human_event(HEAD)]))
        self.assertTrue(all(i.applies for i in items))
        self.assertEqual({i.source for i in items}, {evidence.GITHUB, evidence.HUMAN})

    def test_the_head_is_queried_by_commit_not_by_pull_request(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout="")) as run:
            evidence.collect(REPO, HEAD, FakeLedger([]))
        called = " ".join(run.call_args.args[0])
        self.assertIn(f"commits/{HEAD}/check-runs", called)

    def test_rendered_prompt_names_the_commit(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout=ci_stdout("ci", "success",
                                                                       HEAD))):
            items = evidence.collect(REPO, HEAD, FakeLedger([human_event(HEAD)]))
        self.assertIn(HEAD, evidence.render(items, HEAD))

    def test_abbreviated_sha_matches_the_same_commit(self):
        self.assertTrue(evidence.same_commit("1db53051dd04", HEAD))
        self.assertTrue(evidence.same_commit(HEAD, "1db53051dd04"))

    def test_too_short_a_prefix_is_not_a_match(self):
        self.assertFalse(evidence.same_commit("1db53", HEAD))
        self.assertFalse(evidence.same_commit("", HEAD))
        self.assertFalse(evidence.same_commit(None, HEAD))


class TestStaleEvidenceCannotAuthorise(unittest.TestCase):
    """Proof 2: evidence from another SHA cannot authorise acceptance."""

    def test_ci_from_a_different_commit_is_not_applicable(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout=ci_stdout("ci", "success",
                                                                       OTHER))):
            items = evidence.collect(REPO, HEAD, FakeLedger([]))
        self.assertTrue(items)
        self.assertFalse(any(i.applies for i in items))

    def test_human_verification_of_a_different_commit_is_not_applicable(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="", stdout="")):
            items = evidence.collect(REPO, HEAD, FakeLedger([human_event(OTHER)]))
        human = [i for i in items if i.source == evidence.HUMAN]
        self.assertEqual(len(human), 1)
        self.assertFalse(human[0].applies)

    def test_mismatched_evidence_is_rendered_under_an_explicit_warning(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout=ci_stdout("ci", "success",
                                                                       OTHER))):
            items = evidence.collect(REPO, HEAD, FakeLedger([human_event(OTHER)]))
        text = evidence.render(items, HEAD)
        self.assertIn("Not applicable", text)
        self.assertIn("do not rely on these", text)
        self.assertIn(OTHER, text)
        self.assertNotIn("### Independent evidence for this exact commit\n- **", text)

    def test_unreadable_ci_is_reported_as_unavailable_not_as_success(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=False, stderr="no network",
                                                      stdout="")):
            items = evidence.collect(REPO, HEAD, FakeLedger([]))
        self.assertFalse(any(i.applies for i in items))
        self.assertIn("could not be read", items[0].detail)

    def test_absent_evidence_never_reads_as_positive(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="", stdout="")):
            items = evidence.collect(REPO, HEAD, FakeLedger([]))
        text = evidence.render(items, HEAD)
        self.assertIn("unverified is not the same as acceptable", text)


class TestProvenanceIsExplicit(unittest.TestCase):
    """Proof 3: every item names its source, and agents are never a source."""

    def test_each_item_is_labelled_with_its_source(self):
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="",
                                                      stdout=ci_stdout("ci", "success",
                                                                       HEAD))):
            items = evidence.collect(REPO, HEAD, FakeLedger([human_event(HEAD)]))
        text = evidence.render(items, HEAD)
        for item in items:
            self.assertIn(f"**[{item.source}]**", text)

    def test_only_machine_and_human_authority_sources_exist(self):
        self.assertEqual({evidence.GITHUB, evidence.HUMAN},
                         {"github-actions", "human-authority"})

    def test_agent_assertions_are_not_collected_as_evidence(self):
        noise = [
            {"event_type": "WORKER_FINISHED",
             "metadata_redacted": {"claim": "builder says all checks pass"}},
            {"event_type": "FIX_DISPATCHED",
             "metadata_redacted": {"claim": "fixer says F1 is not a code defect"}},
        ]
        with mock.patch.object(evidence.gh, "run",
                               return_value=mock.Mock(ok=True, stderr="", stdout="")):
            items = evidence.collect(REPO, HEAD, FakeLedger(noise))
        text = evidence.render(items, HEAD)
        self.assertNotIn("builder says", text)
        self.assertNotIn("fixer says", text)
        self.assertIn("No Builder, Fixer or other Claude assertion", text)

    def test_the_prompt_states_that_evidence_does_not_decide_the_verdict(self):
        rendered = prompts.reviewer(
            {"id": "TASK-001", "title": "Foundation"}, 3, "task/task-001", REPO, 5,
            evidence="- **[github-actions]** (ci) check 'ci': completed/success",
        )
        self.assertIn("sole authority for the verdict", rendered)
        self.assertIn("does not decide it", rendered)
        self.assertIn("github-actions", rendered)


class TestIndependenceAndMergeGateUnchanged(unittest.TestCase):
    """Proof 4: nothing here relaxes review independence or the merge gate."""

    def setUp(self):
        self.pr = {"number": 3, "state": "OPEN", "isDraft": False,
                   "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                   "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                                          "conclusion": "SUCCESS"}]}
        self.record = routing.blank_pr_record(3, "TASK-001", "task/task-001")

    def test_evidence_alone_cannot_merge_without_a_codex_pass(self):
        self.record.update({"review_verdict": None, "approval_current": False,
                            "reviewed_diff_hash": "h"})
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, "h")
        self.assertFalse(decision.allowed)
        self.assertIn("REVIEW_PASS", decision.reason)

    def test_a_review_fail_still_blocks_however_strong_the_evidence(self):
        self.record.update({"review_verdict": routing.REVIEW_FAIL,
                            "approval_current": True, "reviewed_diff_hash": "h"})
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "h").allowed)

    def test_the_six_merge_conditions_are_untouched(self):
        self.record.update({"review_verdict": routing.REVIEW_PASS,
                            "approval_current": True, "reviewed_diff_hash": "h"})
        self.assertTrue(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "h").allowed)
        # each condition still independently blocks
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), True, "h").allowed)
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "moved").allowed)

    def test_pass_with_blocking_findings_is_still_rejected(self):
        review = routing.parse_review(
            '```json\n{"verdict":"REVIEW_PASS","gates":{"OVERENGINEERING":"PASS",'
            '"COGNITIVE_LOAD":"PASS","SENSORY_LOAD":"PASS"},'
            '"findings":[{"severity":"P1","summary":"still broken"}]}\n```')
        consistent, why = routing.review_is_consistent(review, touches_ui=True)
        self.assertFalse(consistent)
        self.assertIn("P0/P1", why)


if __name__ == "__main__":
    unittest.main()
