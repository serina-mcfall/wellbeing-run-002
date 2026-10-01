"""A required check that is RED, through the REAL merge path.

WHY THIS FILE EXISTS, AND WHY IT IS SHORT.

`MERGE_CI_NOT_SATISFIED` is well proved as a GATE condition - at the unit
level in tests/test_c04a_runtime_merge_lifecycle.py (OrderingCase
test_failing_ci_still_blocks_with_complete_evidence) and in
tests/test_c04b_merge_gate_differential.py (MergeStateDifferenceCase). All
of those call `routing.evaluate_merge` directly.

Nothing proved the three facts that matter about the SUPERVISOR when a
required check is red: that `gh.merge` is not called, that the finite
condition reaches the durable record where a human can act on it, and that
the task is not completed and its dependents are not unblocked. Those are
different claims from "the pure function returns a denial", and the
difference is exactly the C-04a lesson - `review_gate_fires` was correct
and called by nothing for an entire phase.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT. There is no escalation for a
check that goes red AFTER an approving review: the task holds in REVIEW and
`MERGE_BLOCKED` repeats every tick with no counter and no bound. That shape
is the one CONTRADICTION-AUDIT row C-20(d) describes verbatim ("logging
MERGE_BLOCKED every tick forever with no escalation"), and the operator's
recorded decision on it was diagnostics - the finite condition code - not
escalation. Asserting a bound here would invent a requirement nobody took a
decision about; asserting the stall would be a test of a decision rather
than of the code. So this file asserts the diagnostic, which IS what was
decided, and the gap is reported rather than silently closed.

The repair path for a red check that exists BEFORE review is not here
either, and is not missing: `control/evidence.py::_ci_for_sha` puts the
check runs for the exact head in front of the reviewer, so a red check
reaches the verdict, and a REVIEW_FAIL takes the fixer path proved in
tests/test_fixer_dispatch.py and tests/test_c18_stage6_fixer.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import routing  # noqa: E402
from test_merge_boundary import (  # noqa: E402
    BRANCH_FMT, MergeBoundaryCase, _open_pr)

TASK = "TASK-001"
PR = 100


def _red_ci_pr(conclusion="FAILURE"):
    pr = _open_pr(PR, BRANCH_FMT.format(TASK.lower()))
    pr["statusCheckRollup"] = [
        {"name": "ci", "status": "COMPLETED", "conclusion": conclusion}]
    return pr


def _no_ci_pr():
    pr = _open_pr(PR, BRANCH_FMT.format(TASK.lower()))
    pr["statusCheckRollup"] = []
    return pr


class ARedRequiredCheckStopsTheRealMergeCase(MergeBoundaryCase):
    """Everything else about this pull request is merge-ready."""

    def test_the_baseline_with_green_ci_really_does_merge(self):
        """The control. Without it every refusal below could be refusing
        for a reason that has nothing to do with CI."""
        self.seed()
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [PR])

    def test_a_red_required_check_never_reaches_gh_merge(self):
        self.seed()
        self.run_tick(open_prs=[_red_ci_pr()])
        self.assertEqual(self.merged_numbers(), [],
                         "a pull request whose required check is red merged")

    def test_the_ci_condition_reaches_the_durable_record(self):
        """The finite code, not only the prose.

        C-20a decision D is that a reader of the ledger can tell which
        condition held without parsing English, and a red check is the one
        a human can act on directly.
        """
        self.seed()
        self.run_tick(open_prs=[_red_ci_pr()])
        blocked = [fields for name, fields in self.events
                   if name == "MERGE_BLOCKED"]
        self.assertTrue(blocked, "the refusal was not recorded at all")
        self.assertEqual(blocked[-1]["metadata_redacted"]["condition"],
                         routing.MERGE_CI_NOT_SATISFIED)

    def test_a_required_check_nobody_reported_is_refused_the_same_way(self):
        """Absence is a denial, not an exemption - and it must land on the
        SAME condition, so "red" and "never ran" are not two diagnostics a
        reader has to know are the same fault."""
        self.seed()
        self.run_tick(open_prs=[_no_ci_pr()])
        self.assertEqual(self.merged_numbers(), [])
        blocked = [fields for name, fields in self.events
                   if name == "MERGE_BLOCKED"]
        self.assertEqual(blocked[-1]["metadata_redacted"]["condition"],
                         routing.MERGE_CI_NOT_SATISFIED)

    def test_the_task_is_not_completed_and_the_approval_is_kept(self):
        """A red check is not evidence the review was wrong.

        Invalidating would send the task round another review cycle for a
        fault the reviewer cannot fix, and completing it would unblock
        dependents on a pull request that never merged.
        """
        self.seed()
        self.run_tick(open_prs=[_red_ci_pr()])
        self.assertEqual(self.task_state(TASK), "REVIEW")
        record = self.record(PR)
        self.assertFalse(record.get("merged", False))
        self.assertIsNone(record.get("merged_sha"))
        self.assertTrue(record["approval_current"])
        self.assertEqual(record["review_verdict"], routing.REVIEW_PASS)
        self.assertNotIn("MERGED", self.event_types())

    def test_a_check_still_running_is_not_a_passing_one(self):
        """IN_PROGRESS with no conclusion. "Not finished" must never read
        as "finished green" - the same fail-open shape the merge-state
        branch refuses."""
        pr = _open_pr(PR, BRANCH_FMT.format(TASK.lower()))
        pr["statusCheckRollup"] = [
            {"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
        self.seed()
        self.run_tick(open_prs=[pr])
        self.assertEqual(self.merged_numbers(), [])


if __name__ == "__main__":
    unittest.main()
