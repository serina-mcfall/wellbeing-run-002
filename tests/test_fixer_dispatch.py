"""Regression tests for the approved review/fixer deadlock repair (DEV-003).

Scope is deliberately narrow: the Codex REVIEW_FAIL -> fresh Claude Fixer ->
same PR -> Codex re-review path, and the dispatch-failure handling that stops a
task being stranded in REVIEW.

They exist because a Fixer worktree was created on the Builder's PR branch,
which git already had checked out elsewhere. No worktree appeared, the dispatch
returned without logging anything, and TASK-001 sat in REVIEW with no worker
while stale detection - which covers only ASSIGNED and ACTIVE - could not see it.

Required proofs:
  1. a failed first review dispatches a fresh Fixer against the existing PR branch
  2. dispatch failure cannot silently leave a task indefinitely in REVIEW
  3. the repaired path routes back to independent Codex re-review
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, config, routing, state, supervisor as supervisor_mod  # noqa: E402

TZ = "Pacific/Auckland"
PR = 3
BRANCH = "task/task-001"
BUILDER_WORKTREE = Path("/tmp/run-002-test/task-001-builder")

FINDINGS = [
    {"id": "F1", "severity": "P1", "category": "SECURITY", "summary": "one"},
    {"id": "F2", "severity": "P1", "category": "ACCESSIBILITY", "summary": "two"},
]


def doc_in_review(verdict: str = routing.REVIEW_FAIL, with_findings: bool = True) -> dict:
    doc = state.initial_document("run-001", "v1.0")
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task["state"] = "REVIEW"
    task["branch"] = BRANCH
    task["pr"] = PR
    task["worker"] = None
    task["last_progress_at"] = clock.iso(clock.now(TZ))
    record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
    record["review_verdict"] = verdict
    record["review_cycles"] = 1
    if with_findings:
        record["pending_findings"] = list(FINDINGS)
    doc["prs"][str(PR)] = record
    return doc


class FixerDispatchCase(unittest.TestCase):
    def setUp(self):
        cfg = config.load()
        self.cfg = cfg
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        self.events: list[tuple] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def event_types(self) -> list[str]:
        return [name for name, _ in self.events]

    def dispatch(self, doc, *, existing_worktree: Path | None, start_ok: bool = True,
                 create_ok: bool = True, created_path: Path | None = None):
        with mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                               return_value=existing_worktree) as by_branch, \
                mock.patch.object(supervisor_mod.workers, "create_worker") as create, \
                mock.patch.object(supervisor_mod.workers, "worktree_path",
                                  return_value=created_path), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job") as start, \
                mock.patch.object(supervisor_mod.prompts, "fixer", return_value="p"), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")):
            create.return_value = mock.Mock(ok=create_ok, stderr="", stdout="")
            start.return_value = mock.Mock(ok=start_ok, stderr="boom", stdout="")
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR,
                                    doc["prs"][str(PR)]["pending_findings"])
        return by_branch, create, start


class TestFreshFixerOnExistingBranch(FixerDispatchCase):
    """Proof 1: a failed review dispatches a Fixer against the existing PR branch."""

    def test_fixer_reuses_the_worktree_holding_the_pr_branch(self):
        doc = doc_in_review()
        by_branch, create, start = self.dispatch(doc, existing_worktree=BUILDER_WORKTREE)

        by_branch.assert_called_once_with(BRANCH)
        create.assert_not_called()          # never tries to check the branch out twice
        start.assert_called_once()
        self.assertEqual(start.call_args.args[2], BUILDER_WORKTREE)

        task = doc["tasks"]["TASK-001"]
        self.assertEqual(task["state"], "FIX_REQUIRED")
        self.assertIn("FIX_DISPATCHED", self.event_types())
        self.assertEqual(doc["prs"][str(PR)]["repair_cycles"], 1)
        self.assertEqual(doc["prs"][str(PR)]["open_finding_ids"], ["F1", "F2"])

    def test_the_fixer_is_a_new_worker_not_the_builder(self):
        doc = doc_in_review()
        self.dispatch(doc, existing_worktree=BUILDER_WORKTREE)
        worker = doc["tasks"]["TASK-001"]["worker"]
        self.assertEqual(worker, "task-001-fixer-1")
        self.assertEqual(doc["workers"][worker]["role"], "fixer")
        self.assertNotIn("builder", worker)

    def test_only_the_reported_findings_are_passed_to_the_fixer(self):
        doc = doc_in_review()
        with mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                               return_value=BUILDER_WORKTREE), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=mock.Mock(ok=True, stderr="")), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")), \
                mock.patch.object(supervisor_mod.prompts, "fixer",
                                  return_value="p") as fixer_prompt:
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertEqual(fixer_prompt.call_args.args[4], FINDINGS)

    def test_repair_cycle_limit_is_unchanged_and_still_escalates(self):
        doc = doc_in_review()
        doc["prs"][str(PR)]["repair_cycles"] = self.cfg.max_repair_cycles
        self.dispatch(doc, existing_worktree=BUILDER_WORKTREE)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.sup.notify_out.assert_called_once()


class TestDispatchFailureIsNeverSilent(FixerDispatchCase):
    """Proof 2: a dispatch failure cannot strand a task in REVIEW."""

    def test_missing_worktree_logs_and_counts_instead_of_returning_silently(self):
        doc = doc_in_review()
        self.dispatch(doc, existing_worktree=None, create_ok=True, created_path=None)

        self.assertIn("DISPATCH_FAILED", self.event_types())
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"], 1)
        self.assertEqual(doc["prs"][str(PR)]["repair_cycles"], 0)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")

    def test_a_stranded_review_is_retried_by_the_routing_loop(self):
        doc = doc_in_review()
        self.dispatch(doc, existing_worktree=None, create_ok=True, created_path=None)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertEqual(doc["workers"], {})

        cs = clock.ClockState(clock.now(TZ), clock.now(TZ), 24)
        pr = {"number": PR, "state": "OPEN", "isDraft": False,
              "headRefName": BRANCH, "mergeStateStatus": "CLEAN"}
        with mock.patch.object(self.sup, "dispatch_fixer") as retry:
            self.sup.route_prs(doc, cs, [pr])

        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[2], PR)
        self.assertEqual(retry.call_args.args[3], FINDINGS)
        # DEV-004 generalised FIX_DISPATCH_RETRY into DISPATCH_RETRY, which carries
        # the role. The behaviour asserted above is unchanged.
        self.assertIn("DISPATCH_RETRY", self.event_types())

    def test_repeated_dispatch_failures_escalate_to_a_human(self):
        doc = doc_in_review()
        for _ in range(self.cfg.max_repair_cycles):
            self.dispatch(doc, existing_worktree=None, create_ok=False,
                          created_path=None)
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"],
                         self.cfg.max_repair_cycles)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.sup.notify_out.assert_called_once()

    def test_a_failed_worker_start_is_also_recorded(self):
        doc = doc_in_review()
        self.dispatch(doc, existing_worktree=BUILDER_WORKTREE, start_ok=False)
        self.assertIn("DISPATCH_FAILED", self.event_types())
        self.assertEqual(doc["prs"][str(PR)]["repair_cycles"], 0)
        self.assertEqual(doc["workers"], {})

    def test_retry_does_not_fire_while_a_worker_is_already_live(self):
        doc = doc_in_review()
        doc["workers"]["task-001-fixer-1"] = {"role": "fixer", "task_id": "TASK-001",
                                              "pr": PR}
        cs = clock.ClockState(clock.now(TZ), clock.now(TZ), 24)
        pr = {"number": PR, "state": "OPEN", "isDraft": False,
              "headRefName": BRANCH, "mergeStateStatus": "CLEAN"}
        with mock.patch.object(self.sup, "dispatch_fixer") as retry:
            self.sup.route_prs(doc, cs, [pr])
        retry.assert_not_called()


class TestRoutesBackToIndependentReview(FixerDispatchCase):
    """Proof 3: a completed fix returns to Codex, never to self-review."""

    def test_finished_fixer_invalidates_approval_and_re_dispatches_codex(self):
        doc = doc_in_review()
        doc["tasks"]["TASK-001"]["state"] = "FIX_REQUIRED"
        record = doc["prs"][str(PR)]
        record["approval_current"] = True          # must not survive a repair
        meta = {"role": "fixer", "task_id": "TASK-001", "pr": PR}

        with mock.patch.object(self.sup, "dispatch_reviewer") as review:
            self.sup.on_fixer_finished(doc, "task-001-fixer-1", meta,
                                       {"outcome": "SUCCESS"})

        review.assert_called_once()
        self.assertEqual(review.call_args.args[2], PR)
        self.assertFalse(record["approval_current"])
        self.assertIsNone(record["review_verdict"])

    def test_a_review_pass_is_never_inherited_across_a_repair(self):
        doc = doc_in_review(verdict=routing.REVIEW_PASS)
        doc["tasks"]["TASK-001"]["state"] = "FIX_REQUIRED"
        record = doc["prs"][str(PR)]
        record["approval_current"] = True
        with mock.patch.object(self.sup, "dispatch_reviewer"):
            self.sup.on_fixer_finished(doc, "task-001-fixer-1",
                                       {"role": "fixer", "task_id": "TASK-001", "pr": PR},
                                       {"outcome": "SUCCESS"})
        self.assertNotEqual(record["review_verdict"], routing.REVIEW_PASS)
        self.assertFalse(record["approval_current"])


if __name__ == "__main__":
    unittest.main()
