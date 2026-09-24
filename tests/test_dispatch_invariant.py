"""Regression tests for the general dispatch/worktree invariant (DEV-004).

Three deadlocks shared one cause: a role dispatch needed a git worktree, could
not obtain one because the branch was already checked out elsewhere, and
returned without leaving the task recoverable. DEV-003 fixed the Fixer instance.
The Reviewer then hit the same wall on review cycle 2, because every cycle used
the one branch `review/<branch>`.

The invariant these tests hold:

    A role either works ON an existing branch and reuses the worktree already
    holding it, or needs its own checkout and is given a branch nobody holds.
    A dispatch that cannot obtain a worktree always returns a reason, and the
    task is always re-routed on a later tick.

Required proofs:
  1. a second review cycle launches in a fresh Codex context against the
     updated PR head
  2. obsolete review worktree or branch state cannot prevent later cycles
  3. a reviewer dispatch failure cannot indefinitely strand a task in REVIEW
  4. the recovered path still requires independent Codex review before merge
  5. the invariant is general, not a per-role patch
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    clock,
    config,
    routing,
    state,
    supervisor as supervisor_mod,
    workers,
)

TZ = "Pacific/Auckland"
PR = 3
BRANCH = "task/task-001"
HEAD_AFTER_FIX = "54ab0f3e680f1111222233334444555566667777"
BUILDER_WORKTREE = Path("/tmp/run-002-test/task-001-builder")
REVIEW_WORKTREE = Path("/tmp/run-002-test/task-001-review-2")

FINDINGS = [{"id": "F1", "severity": "P1", "category": "SECURITY", "summary": "one"}]


def doc_after_fix() -> dict:
    """The exact live state that blocked: fix pushed, verdict cleared, no worker."""
    doc = state.initial_document("run-001", "v1.0")
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task.update({"state": "REVIEW", "branch": BRANCH, "pr": PR,
                 "worker": "task-001-fixer-1"})
    record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
    record.update({"review_verdict": None, "approval_current": False,
                   "review_cycles": 1, "repair_cycles": 1, "dispatch_failures": 1,
                   "pending_findings": list(FINDINGS)})
    doc["prs"][str(PR)] = record
    return doc


class InvariantCase(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)
        self.events: list[str] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append(e))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def open_pr(self) -> dict:
        return {"number": PR, "state": "OPEN", "isDraft": False, "headRefName": BRANCH,
                "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
                "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                                       "conclusion": "SUCCESS"}]}


class TestSecondReviewCycle(InvariantCase):
    """Proof 1: cycle 2 is a fresh Codex context against the updated PR head."""

    def test_cycle_two_uses_its_own_branch_based_on_the_current_head(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["pending_findings"] = None
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                               return_value=HEAD_AFTER_FIX), \
                mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                  return_value="hash-after-fix"), \
                mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                                  return_value=(REVIEW_WORKTREE, "")) as acquire, \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=mock.Mock(ok=True, stderr="")), \
                mock.patch.object(supervisor_mod.prompts, "reviewer", return_value="p"), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")):
            self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR)

        name, branch, base = acquire.call_args.args[0], acquire.call_args.args[1], \
            acquire.call_args.args[2]
        self.assertEqual(name, "task-001-review-2")        # fresh Codex worker
        self.assertEqual(branch, f"review/c2/{BRANCH}")    # cycle-scoped branch
        self.assertEqual(base, HEAD_AFTER_FIX)             # the CURRENT PR head
        self.assertFalse(acquire.call_args.kwargs["reuse_if_checked_out"])
        self.assertEqual(doc["prs"][str(PR)]["reviewed_head"], HEAD_AFTER_FIX)
        self.assertEqual(doc["prs"][str(PR)]["review_cycles"], 2)

    def test_an_unresolvable_head_fails_loudly_rather_than_reviewing_stale_code(self):
        doc = doc_after_fix()
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha", return_value=None), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")):
            self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR)
        self.assertIn("DISPATCH_FAILED", self.events)
        self.assertEqual(doc["prs"][str(PR)]["review_cycles"], 1)


class TestObsoleteWorktreeState(InvariantCase):
    """Proof 2: leftover review state cannot block a later cycle."""

    def test_cycle_branches_are_distinct_so_they_cannot_collide(self):
        seen = set()
        for cycle in (1, 2, 3):
            doc = doc_after_fix()
            doc["prs"][str(PR)]["review_cycles"] = cycle - 1
            doc["prs"][str(PR)]["pending_findings"] = None
            with mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                                   return_value=HEAD_AFTER_FIX), \
                    mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                      return_value="h"), \
                    mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                                      return_value=(REVIEW_WORKTREE, "")) as acquire, \
                    mock.patch.object(supervisor_mod.workers, "write_job",
                                      return_value=Path("/tmp/job.json")), \
                    mock.patch.object(supervisor_mod.workers, "start_job",
                                      return_value=mock.Mock(ok=True, stderr="")), \
                    mock.patch.object(supervisor_mod.prompts, "reviewer",
                                      return_value="p"), \
                    mock.patch.object(supervisor_mod.prompts, "write",
                                      return_value=Path("/tmp/p.md")):
                self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR)
            seen.add(acquire.call_args.args[1])
        self.assertEqual(seen, {f"review/c1/{BRANCH}", f"review/c2/{BRANCH}",
                                f"review/c3/{BRANCH}"})

    def test_a_finished_reviewer_worktree_is_released(self):
        with mock.patch.object(supervisor_mod.workers, "read_status",
                               return_value={"phase": "DONE", "agent_pid": 1}), \
                mock.patch.object(supervisor_mod.workers, "process_alive",
                                  return_value=False), \
                mock.patch.object(supervisor_mod.workers, "remove_worker",
                                  return_value=mock.Mock(ok=True, stderr="")) as remove:
            self.sup.release_review_worktree("task-001-review-1")
        remove.assert_called_once_with("task-001-review-1")
        self.assertIn("REVIEW_WORKTREE_RELEASED", self.events)

    def test_a_live_reviewer_worktree_is_never_destroyed(self):
        for status, alive in (({"phase": "RUNNING", "agent_pid": 1}, True),
                              ({"phase": "DONE", "agent_pid": 1}, True)):
            with self.subTest(status=status, alive=alive):
                with mock.patch.object(supervisor_mod.workers, "read_status",
                                       return_value=status), \
                        mock.patch.object(supervisor_mod.workers, "process_alive",
                                          return_value=alive), \
                        mock.patch.object(supervisor_mod.workers,
                                          "remove_worker") as remove:
                    self.sup.release_review_worktree("task-001-review-1")
                remove.assert_not_called()


class TestReviewerDispatchCannotStrand(InvariantCase):
    """Proof 3: the exact live failure recovers instead of stranding."""

    def test_the_blocked_state_is_rerouted_to_a_reviewer(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["pending_findings"] = None   # fix done, verdict cleared
        with mock.patch.object(self.sup, "dispatch_reviewer") as review, \
                mock.patch.object(self.sup, "dispatch_fixer") as fix:
            self.sup.route_prs(doc, clock.ClockState(clock.now(TZ), clock.now(TZ), 24),
                               [self.open_pr()])
        review.assert_called_once()
        fix.assert_not_called()
        self.assertIn("DISPATCH_RETRY", self.events)

    def test_a_failed_review_still_routes_to_the_fixer_not_another_review(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["review_verdict"] = routing.REVIEW_FAIL
        with mock.patch.object(self.sup, "dispatch_reviewer") as review, \
                mock.patch.object(self.sup, "dispatch_fixer") as fix:
            self.sup.route_prs(doc, clock.ClockState(clock.now(TZ), clock.now(TZ), 24),
                               [self.open_pr()])
        fix.assert_called_once()
        review.assert_not_called()

    def test_recovery_does_not_fire_while_a_worker_is_live(self):
        doc = doc_after_fix()
        doc["workers"]["task-001-review-2"] = {"role": "reviewer",
                                               "task_id": "TASK-001", "pr": PR}
        with mock.patch.object(self.sup, "dispatch_reviewer") as review, \
                mock.patch.object(self.sup, "dispatch_fixer") as fix:
            self.sup.route_prs(doc, clock.ClockState(clock.now(TZ), clock.now(TZ), 24),
                               [self.open_pr()])
        review.assert_not_called()
        fix.assert_not_called()

    def test_repeated_reviewer_dispatch_failures_escalate_to_a_human(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["dispatch_failures"] = 0
        for _ in range(self.cfg.max_repair_cycles):
            self.sup.on_dispatch_failure(doc, doc["tasks"]["TASK-001"], PR, "reviewer",
                                         "no worktree")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.sup.notify_out.assert_called_once()


class TestIndependentReviewStillRequired(InvariantCase):
    """Proof 4: recovery never becomes a shortcut to merge."""

    def test_recovery_cannot_produce_an_approval(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["pending_findings"] = None
        with mock.patch.object(self.sup, "dispatch_reviewer"):
            self.sup.route_prs(doc, clock.ClockState(clock.now(TZ), clock.now(TZ), 24),
                               [self.open_pr()])
        record = doc["prs"][str(PR)]
        self.assertIsNone(record["review_verdict"])
        self.assertFalse(record["approval_current"])

    def test_merge_gate_still_refuses_without_a_current_codex_pass(self):
        record = doc_after_fix()["prs"][str(PR)]
        decision = routing.evaluate_merge(self.open_pr(), record, ("ci",), False, "h")
        self.assertFalse(decision.allowed)
        self.assertIn("REVIEW_PASS", decision.reason)

    def test_merge_is_not_attempted_from_a_recovered_review_state(self):
        doc = doc_after_fix()
        doc["prs"][str(PR)]["pending_findings"] = None
        with mock.patch.object(self.sup, "attempt_merge") as merge, \
                mock.patch.object(self.sup, "dispatch_reviewer"):
            self.sup.route_prs(doc, clock.ClockState(clock.now(TZ), clock.now(TZ), 24),
                               [self.open_pr()])
        merge.assert_not_called()


class TestTheInvariantIsGeneral(InvariantCase):
    """Proof 5: one rule, not three role-specific patches."""

    def test_every_routing_state_with_no_worker_is_recovered(self):
        for task_state in supervisor_mod.Supervisor.ROUTING_STATES:
            with self.subTest(state=task_state):
                doc = doc_after_fix()
                doc["tasks"]["TASK-001"]["state"] = task_state
                doc["prs"][str(PR)]["pending_findings"] = None
                with mock.patch.object(self.sup, "dispatch_reviewer") as review:
                    self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], PR,
                                                     doc["prs"][str(PR)])
                review.assert_called_once()

    def test_terminal_and_pre_pr_states_are_left_alone(self):
        for task_state in ("QUEUED", "READY", "ASSIGNED", "ACTIVE", "MERGED",
                           "COMPLETE", "HUMAN_REQUIRED"):
            with self.subTest(state=task_state):
                doc = doc_after_fix()
                doc["tasks"]["TASK-001"]["state"] = task_state
                with mock.patch.object(self.sup, "dispatch_reviewer") as review, \
                        mock.patch.object(self.sup, "dispatch_fixer") as fix:
                    self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], PR,
                                                     doc["prs"][str(PR)])
                review.assert_not_called()
                fix.assert_not_called()

    def test_acquire_worktree_always_returns_a_path_or_a_reason(self):
        cases = [
            ("reuse hit", True, BUILDER_WORKTREE, None, BUILDER_WORKTREE),
            ("reuse miss then create", True, None, REVIEW_WORKTREE, REVIEW_WORKTREE),
            ("no reuse, collision", False, BUILDER_WORKTREE, None, None),
            ("no reuse, create fails", False, None, None, None),
        ]
        for label, reuse, holder, created_path, expected in cases:
            with self.subTest(case=label):
                with mock.patch.object(workers, "worktree_for_branch",
                                       return_value=holder), \
                        mock.patch.object(workers, "create_worker",
                                          return_value=mock.Mock(ok=created_path
                                                                 is not None,
                                                                 stderr="")), \
                        mock.patch.object(workers, "worktree_path",
                                          return_value=created_path):
                    path, why = workers.acquire_worktree(
                        "w", "b", "base", "s", Path("/tmp/p.md"),
                        reuse_if_checked_out=reuse)
                self.assertEqual(path, expected)
                # Exactly one of path or reason, never neither.
                self.assertEqual(path is None, bool(why))

    def test_a_collision_reason_names_the_holding_worktree(self):
        with mock.patch.object(workers, "worktree_for_branch",
                               return_value=BUILDER_WORKTREE):
            path, why = workers.acquire_worktree("w", BRANCH, "base", "s",
                                                 Path("/tmp/p.md"),
                                                 reuse_if_checked_out=False)
        self.assertIsNone(path)
        self.assertIn(str(BUILDER_WORKTREE), why)
        self.assertIn("two worktrees", why)


if __name__ == "__main__":
    unittest.main()
