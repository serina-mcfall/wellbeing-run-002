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


if __name__ == "__main__":
    unittest.main()
