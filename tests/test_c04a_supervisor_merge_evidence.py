"""The evidence requirement through the REAL Supervisor, end to end.

`test_c04a_runtime_merge_lifecycle.py` proves `routing.evaluate_merge`
refuses a PR with no evidence. That is the gate in isolation. The operator's
requirement is stronger: *"Verify the real Supervisor orchestration and
merge boundary, not only evaluate_merge in isolation."*

So this file reuses `test_merge_boundary.MergeBoundaryCase` — a real
`Supervisor` against a real `Store` with a genuine state file, driving a
real `tick()`, with only the outbound edges stubbed — and asks the question
that matters: **does a pull request with missing or stale evidence actually
fail to reach `gh.merge`?**

A gate that refuses while the orchestration merges anyway would be the
worst of both: a passing unit test and a merged commit. `gh.merge` call
records are the evidence here, not the decision object.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import routing  # noqa: E402
from mergeable_evidence import complete_evidence  # noqa: E402
from test_merge_boundary import (  # noqa: E402
    REVIEWED_HEAD, MergeBoundaryCase)

STALE_HEAD = "9" * 40


class SupervisorRefusesWithoutEvidenceCase(MergeBoundaryCase):

    def _record(self, doc, number=100):
        return doc["prs"][str(number)]

    def test_the_seeded_baseline_really_does_merge(self):
        # The control. Every refusal below is only meaningful if this
        # fixture reaches gh.merge when its evidence is complete.
        self.seed()
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [100])

    def test_a_pr_with_NO_evidence_never_reaches_gh_merge(self):
        doc = self.seed()
        record = self._record(doc)
        for leg in ("security_evidence", "accessibility_auto",
                    "accessibility_review"):
            record.pop(leg, None)
        self.store._write(doc)

        self.run_tick()

        self.assertEqual(
            self.merged_numbers(), [],
            "the Supervisor merged a pull request carrying no accessibility "
            "or security evidence")

    def test_each_missing_leg_independently_stops_the_merge(self):
        for leg in ("security_evidence", "accessibility_auto",
                    "accessibility_review"):
            with self.subTest(missing=leg):
                self.setUp()
                doc = self.seed()
                del self._record(doc)[leg]
                self.store._write(doc)
                self.run_tick()
                self.assertEqual(self.merged_numbers(), [],
                                 f"{leg} was not required by the orchestration")

    def test_evidence_bound_to_a_superseded_commit_stops_the_merge(self):
        doc = self.seed()
        self._record(doc).update(complete_evidence(STALE_HEAD))
        self.store._write(doc)

        self.run_tick()

        self.assertEqual(self.merged_numbers(), [])

    def test_an_incomplete_claim_stops_the_merge(self):
        doc = self.seed()
        claim = self._record(doc)["accessibility_review"]
        claim["claim_state"] = "SPAWNED"
        claim["verdict"] = None
        self.store._write(doc)

        self.run_tick()

        self.assertEqual(self.merged_numbers(), [])

    def test_a_claim_its_own_validator_refuses_stops_the_merge(self):
        # Structurally plausible: COMPLETE, passing, bound to this head.
        # The worker name addresses another commit's artefacts, which the
        # newly registered accessibility validator refuses.
        doc = self.seed()
        claim = self._record(doc)["accessibility_review"]
        claim["worker"] = f"task-001-a11y-{STALE_HEAD}-0001"
        self.store._write(doc)

        self.assertFalse(routing.accessibility_review_claim_is_valid(claim)[0])
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [])


class TheRefusalIsDurablyRecordedCase(MergeBoundaryCase):
    """A refusal nobody can read afterwards is not evidence."""

    def _conditions(self):
        return [kwargs.get("metadata_redacted", {}).get("condition")
                for event, kwargs in self.events if event == "MERGE_BLOCKED"]

    def test_the_missing_evidence_condition_reaches_the_ledger(self):
        doc = self.seed()
        del doc["prs"]["100"]["accessibility_auto"]
        self.store._write(doc)

        self.run_tick()

        self.assertEqual(self.merged_numbers(), [])
        self.assertIn(
            routing.MERGE_EVIDENCE_INCOMPLETE, self._conditions(),
            "the merge was blocked but durable evidence does not say it was "
            "for missing evidence; MERGE_BLOCKED conditions seen: "
            f"{self._conditions()}")

    def test_a_complete_set_emits_no_evidence_refusal(self):
        self.seed()
        self.run_tick()
        self.assertNotIn(routing.MERGE_EVIDENCE_INCOMPLETE, self._conditions())


class TheTaskIsNotCompletedWithoutAMergeCase(MergeBoundaryCase):
    """Blocking the merge must also block everything downstream of it."""

    def test_no_merge_means_no_completion_and_no_merged_sha(self):
        doc = self.seed()
        del doc["prs"]["100"]["security_evidence"]
        self.store._write(doc)

        self.run_tick()

        after = self.store.read()
        self.assertEqual(self.merged_numbers(), [])
        self.assertFalse(after["prs"]["100"].get("merged", False))
        self.assertIsNone(after["prs"]["100"].get("merge_sha_observed"))
        self.assertNotEqual(after["tasks"]["TASK-001"]["state"], "MERGED")


if __name__ == "__main__":
    unittest.main()
