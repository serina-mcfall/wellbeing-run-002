"""D1: Protocol v2 task-state additions (WAITING_CI, WAITING_EVIDENCE,
MERGE_READY) and the three-way worker-awareness distinction (process
activity vs. meaningful progress vs. explicitly reported phase)."""

import unittest

from control import state


def _doc_with_task(initial_state="PR_OPEN"):
    doc = state.initial_document("run-002", "v2.0")
    state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, "Pacific/Auckland")
    if initial_state != "QUEUED":
        doc["tasks"]["TASK-001"]["state"] = initial_state
    return doc


class TestWaitingCiTransitions(unittest.TestCase):
    def test_pr_open_to_waiting_ci_is_legal(self):
        doc = _doc_with_task("PR_OPEN")
        old, new = state.transition(doc, "TASK-001", "WAITING_CI", "CI dispatched", "Pacific/Auckland")
        self.assertEqual((old, new), ("PR_OPEN", "WAITING_CI"))

    def test_waiting_ci_to_review_is_legal(self):
        doc = _doc_with_task("WAITING_CI")
        old, new = state.transition(doc, "TASK-001", "REVIEW", "CI passed", "Pacific/Auckland")
        self.assertEqual((old, new), ("WAITING_CI", "REVIEW"))

    def test_waiting_ci_to_waiting_evidence_is_legal(self):
        doc = _doc_with_task("WAITING_CI")
        old, new = state.transition(doc, "TASK-001", "WAITING_EVIDENCE",
                                    "CI passed, evidence still pending", "Pacific/Auckland")
        self.assertEqual((old, new), ("WAITING_CI", "WAITING_EVIDENCE"))

    def test_waiting_ci_to_merged_is_not_legal(self):
        doc = _doc_with_task("WAITING_CI")
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "MERGED", "skip review", "Pacific/Auckland")


class TestWaitingEvidenceTransitions(unittest.TestCase):
    def test_pr_open_to_waiting_evidence_is_legal(self):
        doc = _doc_with_task("PR_OPEN")
        old, new = state.transition(doc, "TASK-001", "WAITING_EVIDENCE",
                                    "evidence pending", "Pacific/Auckland")
        self.assertEqual((old, new), ("PR_OPEN", "WAITING_EVIDENCE"))

    def test_waiting_evidence_to_review_is_legal(self):
        doc = _doc_with_task("WAITING_EVIDENCE")
        old, new = state.transition(doc, "TASK-001", "REVIEW",
                                    "evidence current", "Pacific/Auckland")
        self.assertEqual((old, new), ("WAITING_EVIDENCE", "REVIEW"))

    def test_waiting_evidence_to_merge_ready_is_not_legal(self):
        doc = _doc_with_task("WAITING_EVIDENCE")
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "MERGE_READY", "skip review",
                             "Pacific/Auckland")


class TestMergeReadyTransitions(unittest.TestCase):
    def test_review_to_merge_ready_is_legal(self):
        doc = _doc_with_task("REVIEW")
        old, new = state.transition(doc, "TASK-001", "MERGE_READY",
                                    "review passed, evidence current", "Pacific/Auckland")
        self.assertEqual((old, new), ("REVIEW", "MERGE_READY"))

    def test_merge_ready_to_merged_is_legal(self):
        doc = _doc_with_task("MERGE_READY")
        old, new = state.transition(doc, "TASK-001", "MERGED", "merged", "Pacific/Auckland")
        self.assertEqual((old, new), ("MERGE_READY", "MERGED"))

    def test_merge_ready_falls_back_to_review_on_material_change(self):
        doc = _doc_with_task("MERGE_READY")
        old, new = state.transition(doc, "TASK-001", "REVIEW",
                                    "material diff changed post-approval", "Pacific/Auckland")
        self.assertEqual((old, new), ("MERGE_READY", "REVIEW"))

    def test_review_to_merged_directly_is_still_legal(self):
        """Existing REVIEW -> MERGED edge is preserved (additive change only)."""
        doc = _doc_with_task("REVIEW")
        old, new = state.transition(doc, "TASK-001", "MERGED", "merged", "Pacific/Auckland")
        self.assertEqual((old, new), ("REVIEW", "MERGED"))


class TestWorkerAwarenessSignalsStayDistinct(unittest.TestCase):
    def test_process_activity_and_meaningful_progress_are_separate_fields(self):
        record = state.new_worker_record("builder", "TASK-001", "task/task-001",
                                          "2026-09-25T10:00:00+13:00")
        self.assertIn("last_process_activity_at", record)
        self.assertIn("last_meaningful_progress_at", record)
        # Distinct at creation: activity is known (dispatch just happened),
        # progress is not yet known (nothing has been produced yet).
        self.assertEqual(record["last_process_activity_at"], "2026-09-25T10:00:00+13:00")
        self.assertIsNone(record["last_meaningful_progress_at"])

    def test_reported_phase_defaults_to_none(self):
        record = state.new_worker_record("builder", "TASK-001", "task/task-001",
                                          "2026-09-25T10:00:00+13:00")
        self.assertIsNone(record["reported_phase"])

    def test_unpopulated_resource_fields_are_none_not_invented(self):
        record = state.new_worker_record("builder", "TASK-001", "task/task-001",
                                          "2026-09-25T10:00:00+13:00")
        self.assertIsNone(record["worktree"])
        self.assertIsNone(record["browser_profile"])
        self.assertIsNone(record["port"])
        self.assertIsNone(record["lease_expires_at"])
        self.assertIsNone(record["wait_reason"])

    def test_process_phase_is_the_same_generic_value_for_every_role(self):
        """No role-based inference: process_phase at dispatch is always the
        same honest 'STARTING' signal, whatever the role, never a guessed
        IMPLEMENTING/FIXING/REVIEWING/ACCESSIBILITY_TESTING/SECURITY_REVIEWING."""
        for role in ("builder", "fixer", "reviewer", "accessibility", "security"):
            record = state.new_worker_record(role, "TASK-001", "task/task-001",
                                              "2026-09-25T10:00:00+13:00")
            self.assertEqual(record["process_phase"], "STARTING")
            self.assertNotIn(record["process_phase"], state.WORKER_PHASES[1:-2])

    def test_no_phase_inference_helper_exists(self):
        """Regression guard: a role+RUNNING -> detailed-phase inference
        function must not be reintroduced into this module."""
        self.assertFalse(hasattr(state, "worker_display_phase"))


class TestFrozenTransitions(unittest.TestCase):
    """D2: FROZEN is legal only from the states where supervisor.py's own
    dispatch code guarantees a live worker record exists for the task's
    entire time in that state (ASSIGNED, ACTIVE, REVIEW, FIX_REQUIRED),
    never from a state where "no live worker" is normal (PR_OPEN,
    WAITING_CI, WAITING_EVIDENCE, MERGE_READY), and it only ever escalates
    to HUMAN_REQUIRED - never a direct, automatic recovery."""

    def test_assigned_to_frozen_is_legal(self):
        doc = _doc_with_task("ASSIGNED")
        old, new = state.transition(doc, "TASK-001", "FROZEN",
                                    "dead PID claimed alive", "Pacific/Auckland")
        self.assertEqual((old, new), ("ASSIGNED", "FROZEN"))

    def test_active_to_frozen_is_legal(self):
        doc = _doc_with_task("ACTIVE")
        old, new = state.transition(doc, "TASK-001", "FROZEN",
                                    "worktree not registered", "Pacific/Auckland")
        self.assertEqual((old, new), ("ACTIVE", "FROZEN"))

    def test_review_to_frozen_is_legal(self):
        doc = _doc_with_task("REVIEW")
        old, new = state.transition(doc, "TASK-001", "FROZEN",
                                    "worker/task backref mismatch", "Pacific/Auckland")
        self.assertEqual((old, new), ("REVIEW", "FROZEN"))

    def test_fix_required_to_frozen_is_legal(self):
        doc = _doc_with_task("FIX_REQUIRED")
        old, new = state.transition(doc, "TASK-001", "FROZEN",
                                    "dead PID claimed alive", "Pacific/Auckland")
        self.assertEqual((old, new), ("FIX_REQUIRED", "FROZEN"))

    def test_pr_open_to_frozen_is_not_legal(self):
        """PR_OPEN routinely has no live worker record - that is normal,
        not a violation, so it must not be a FROZEN source."""
        doc = _doc_with_task("PR_OPEN")
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "FROZEN", "spurious", "Pacific/Auckland")

    def test_waiting_evidence_to_frozen_is_not_legal(self):
        doc = _doc_with_task("WAITING_EVIDENCE")
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "FROZEN", "spurious", "Pacific/Auckland")

    def test_merge_ready_to_frozen_is_not_legal(self):
        doc = _doc_with_task("MERGE_READY")
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "FROZEN", "spurious", "Pacific/Auckland")

    def test_frozen_escalates_only_to_human_required(self):
        doc = _doc_with_task("ACTIVE")
        state.transition(doc, "TASK-001", "FROZEN", "dead PID claimed alive",
                         "Pacific/Auckland")
        old, new = state.transition(doc, "TASK-001", "HUMAN_REQUIRED",
                                    "human resolving the freeze", "Pacific/Auckland")
        self.assertEqual((old, new), ("FROZEN", "HUMAN_REQUIRED"))

    def test_frozen_cannot_be_automatically_unfrozen(self):
        """No deterministic path recovers a frozen task directly - PID/
        heartbeat looking healthy again later does not matter."""
        doc = _doc_with_task("ACTIVE")
        state.transition(doc, "TASK-001", "FROZEN", "dead PID claimed alive",
                         "Pacific/Auckland")
        for target in ("READY", "ACTIVE", "ASSIGNED", "COMPLETE", "MERGED"):
            with self.subTest(target=target):
                with self.assertRaises(state.TransitionError):
                    state.transition(doc, "TASK-001", target,
                                     "reality looks healthy again", "Pacific/Auckland")


if __name__ == "__main__":
    unittest.main()
