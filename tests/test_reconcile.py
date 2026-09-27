"""D2: independent Watchdog reconciliation (control/reconcile.py).

Every test here proves either a genuine disagreement is caught, or that a
healthy/absent-but-normal case produces no finding - and, per the two
observation-integrity corrections, that missing/failed EVIDENCE is never
silently treated as healthy nor upgraded into a stronger claim than the
evidence supports.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, reconcile, state  # noqa: E402

TZ = "Pacific/Auckland"
AGENT_PID = 4242


def _doc(task_state="ACTIVE", worker_id="task-001-builder", *, with_worker=True,
         worktree="/worktrees/task-001-builder", task_worker_override=None):
    doc = state.initial_document("run-002", "v2.0")
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task["state"] = task_state
    if task_worker_override is not None:
        task["worker"] = task_worker_override
    elif with_worker:
        task["worker"] = worker_id
    if with_worker:
        doc["workers"][worker_id] = state.new_worker_record(
            "builder", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            worktree=worktree,
        )
    return doc


def _status(**overrides):
    base = {"phase": "RUNNING", "prompt_accepted": True, "output_bytes": 100,
            "agent_pid": AGENT_PID, "heartbeat_at": clock.iso(clock.now(TZ))}
    base.update(overrides)
    return base


def _run(doc, *, status=None, pid_alive=True, git_ok=True, git_worktrees=None):
    git_worktrees = git_worktrees if git_worktrees is not None else {
        "/worktrees/task-001-builder"
    }
    git_result = mock.Mock(ok=git_ok, stdout="\n".join(
        f"worktree {p}" for p in git_worktrees
    ))
    with mock.patch.object(reconcile.workers_mod, "read_status", return_value=status), \
            mock.patch.object(reconcile.proc, "is_running", return_value=pid_alive), \
            mock.patch.object(reconcile.gh, "git", return_value=git_result):
        return reconcile.reconcile(doc, tz=TZ)


def _ids(findings):
    return [f.check_id for f in findings]


class TestGateScope(unittest.TestCase):
    def test_states_outside_the_gate_are_never_checked(self):
        for task_state in ("QUEUED", "READY", "PR_OPEN", "WAITING_CI", "WAITING_EVIDENCE",
                           "MERGE_READY", "MERGED", "COMPLETE", "STALE", "FAILED",
                           "HUMAN_REQUIRED"):
            with self.subTest(state=task_state):
                doc = _doc(task_state, with_worker=False)
                findings = _run(doc, status=None)
                self.assertEqual(findings, [])

    def test_a_fully_healthy_worker_produces_no_findings(self):
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status())
        self.assertEqual(findings, [])


class TestMissingTaskWorkerRef(unittest.TestCase):
    def test_reconcilable_state_with_no_task_worker_is_dangerous(self):
        doc = _doc("ACTIVE", with_worker=False)
        findings = _run(doc, status=None)
        self.assertEqual(_ids(findings), ["MISSING_TASK_WORKER_REF"])
        self.assertTrue(findings[0].dangerous)


class TestOrphanedAndBackref(unittest.TestCase):
    def test_task_worker_with_no_worker_record_is_dangerous(self):
        doc = _doc("ACTIVE", with_worker=False, task_worker_override="ghost-worker")
        findings = _run(doc, status=None)
        self.assertEqual(_ids(findings), ["ORPHANED_TASK_WORKER_REF"])

    def test_worker_record_pointing_at_a_different_task_is_dangerous(self):
        doc = _doc("ACTIVE")
        doc["workers"]["task-001-builder"]["task_id"] = "TASK-999"
        findings = _run(doc, status=_status())
        self.assertIn("WORKER_TASK_BACKREF_MISMATCH", _ids(findings))


class TestMissingWorkerStatus(unittest.TestCase):
    def test_missing_status_fails_closed_without_claiming_a_dead_pid(self):
        """A missing status file is not evidence the PID is dead - it is
        evidence of nothing about the PID at all."""
        doc = _doc("ACTIVE")
        findings = _run(doc, status=None)
        ids = _ids(findings)
        self.assertIn("MISSING_WORKER_STATUS", ids)
        self.assertNotIn("DEAD_PID_CLAIMED_ALIVE", ids)
        self.assertTrue(all(f.dangerous for f in findings if f.check_id == "MISSING_WORKER_STATUS"))


class TestDeadPid(unittest.TestCase):
    def test_status_present_but_pid_dead_is_dangerous(self):
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(), pid_alive=False)
        self.assertIn("DEAD_PID_CLAIMED_ALIVE", _ids(findings))


class TestHeartbeatCases(unittest.TestCase):
    def test_missing_heartbeat_while_pid_alive_is_dangerous(self):
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(heartbeat_at=None), pid_alive=True)
        self.assertIn("MISSING_HEARTBEAT_WHILE_PID_ALIVE", _ids(findings))

    def test_malformed_heartbeat_while_pid_alive_is_dangerous(self):
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(heartbeat_at="not-a-timestamp"), pid_alive=True)
        self.assertIn("MALFORMED_HEARTBEAT_WHILE_PID_ALIVE", _ids(findings))

    def test_stale_heartbeat_beyond_the_bound_while_pid_alive_is_dangerous(self):
        old = clock.iso(clock.now(TZ) - timedelta(seconds=reconcile.HEARTBEAT_DANGER_SECONDS + 5))
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(heartbeat_at=old), pid_alive=True)
        self.assertIn("HEARTBEAT_STALE_WHILE_PID_ALIVE", _ids(findings))

    def test_fresh_heartbeat_while_pid_alive_is_not_flagged(self):
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(), pid_alive=True)
        self.assertEqual([f for f in findings if "HEARTBEAT" in f.check_id], [])

    def test_heartbeat_is_not_evaluated_when_pid_is_already_dead(self):
        """Dead PID already explains everything; do not also compound it
        with a redundant heartbeat finding for the same underlying fact."""
        doc = _doc("ACTIVE")
        findings = _run(doc, status=_status(heartbeat_at=None), pid_alive=False)
        ids = _ids(findings)
        self.assertIn("DEAD_PID_CLAIMED_ALIVE", ids)
        self.assertEqual([i for i in ids if "HEARTBEAT" in i], [])


class TestWorktreeObservation(unittest.TestCase):
    def test_failed_git_observation_is_not_reported_as_not_registered(self):
        doc = _doc("ACTIVE", worktree="/worktrees/task-001-builder")
        findings = _run(doc, status=_status(), git_ok=False)
        ids = _ids(findings)
        self.assertIn("WORKTREE_OBSERVATION_FAILED", ids)
        self.assertNotIn("WORKTREE_NOT_REGISTERED", ids)

    def test_successful_observation_with_genuinely_absent_worktree_is_not_registered(self):
        doc = _doc("ACTIVE", worktree="/worktrees/task-001-builder")
        findings = _run(doc, status=_status(), git_ok=True, git_worktrees=set())
        self.assertIn("WORKTREE_NOT_REGISTERED", _ids(findings))

    def test_successful_observation_with_the_claimed_worktree_present_is_not_flagged(self):
        doc = _doc("ACTIVE", worktree="/worktrees/task-001-builder")
        findings = _run(doc, status=_status(), git_ok=True,
                        git_worktrees={"/worktrees/task-001-builder"})
        self.assertEqual([f for f in findings if "WORKTREE" in f.check_id], [])

    def test_no_claimed_worktree_never_calls_git_at_all(self):
        doc = _doc("ACTIVE", worktree=None)
        with mock.patch.object(reconcile, "registered_worktrees") as rw:
            _run(doc, status=_status())
            rw.assert_not_called()

    def test_git_is_called_at_most_once_per_reconcile_call(self):
        doc = state.initial_document("run-002", "v2.0")
        for n in (1, 2):
            task_id = f"TASK-00{n}"
            worker_id = f"task-00{n}-builder"
            state.add_task(doc, task_id, "T", [], "feature", False, TZ)
            doc["tasks"][task_id]["state"] = "ACTIVE"
            doc["tasks"][task_id]["worker"] = worker_id
            doc["workers"][worker_id] = state.new_worker_record(
                "builder", task_id, f"task/{task_id.lower()}", clock.iso(clock.now(TZ)),
                worktree=f"/worktrees/{worker_id}",
            )
        with mock.patch.object(reconcile.workers_mod, "read_status", return_value=_status()), \
                mock.patch.object(reconcile.proc, "is_running", return_value=True), \
                mock.patch.object(reconcile, "registered_worktrees",
                                  return_value=(True, {"/worktrees/task-001-builder",
                                                       "/worktrees/task-002-builder"})) as rw:
            reconcile.reconcile(doc, tz=TZ)
        rw.assert_called_once()


class TestDormantProgressCheck(unittest.TestCase):
    """last_meaningful_progress_at is never populated by supervisor.py
    today - these tests prove the comparison logic works against
    synthetic input, not that it is active in live Run 002 operation."""

    def test_absent_progress_claim_is_not_itself_a_mismatch(self):
        doc = _doc("ACTIVE")
        self.assertIsNone(doc["workers"]["task-001-builder"]["last_meaningful_progress_at"])
        findings = _run(doc, status=_status())
        self.assertEqual(
            [f for f in findings if f.check_id == "PROGRESS_CLAIM_UNSUPPORTED_BY_OUTPUT_FILE"],
            [],
        )

    def test_a_present_claim_with_no_real_output_file_yet_is_handled_not_flagged(self):
        """No output file at all is not itself a mismatch - only a present
        file whose mtime contradicts the claim is. The positive case (a
        real file that does contradict the claim) is the next test."""
        doc = _doc("ACTIVE")
        future = clock.iso(clock.now(TZ) + timedelta(days=1))
        doc["workers"]["task-001-builder"]["last_meaningful_progress_at"] = future
        with mock.patch.object(reconcile.config, "WORKER_LOG_DIR", Path("/nonexistent-dir")):
            findings = _run(doc, status=_status())
        self.assertEqual(
            [f for f in findings if f.check_id == "PROGRESS_CLAIM_UNSUPPORTED_BY_OUTPUT_FILE"],
            [],
        )

    def test_comparison_logic_flags_a_claim_newer_than_a_real_file_mtime(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmp:
            worker_id = "task-001-builder"
            output_path = Path(tmp) / f"{worker_id}.out"
            output_path.write_text("some output")
            old_mtime = clock.now(TZ).timestamp() - 3600
            os.utime(output_path, (old_mtime, old_mtime))

            doc = _doc("ACTIVE", worker_id=worker_id)
            doc["workers"][worker_id]["last_meaningful_progress_at"] = clock.iso(clock.now(TZ))

            with mock.patch.object(reconcile.config, "WORKER_LOG_DIR", Path(tmp)):
                findings = _run(doc, status=_status())
        self.assertIn("PROGRESS_CLAIM_UNSUPPORTED_BY_OUTPUT_FILE", _ids(findings))


class TestWaitingRoutingStates(unittest.TestCase):
    """D2/C-14 hardening: REVIEW and FIX_REQUIRED may legitimately exist
    between Supervisor ticks with no live worker record - routing's own
    recovery rule (route_awaiting_dispatch) waits there and redispatches by
    verdict, checking liveness from worker records, never task["worker"].
    A stale task["worker"] there is historical identity, not an
    active-ownership claim, so neither MISSING_TASK_WORKER_REF nor
    ORPHANED_TASK_WORKER_REF may fire - a false dangerous finding would
    freeze a healthy task, unrecoverably before C-14.3."""

    REF_CHECKS = ("MISSING_TASK_WORKER_REF", "ORPHANED_TASK_WORKER_REF")

    def ref_findings(self, findings):
        return [f.check_id for f in findings if f.check_id in self.REF_CHECKS]

    def test_waiting_with_stale_previous_worker_is_not_a_contradiction(self):
        for state_name in ("FIX_REQUIRED", "REVIEW"):
            with self.subTest(state=state_name):
                doc = _doc(state_name, with_worker=False,
                           task_worker_override="old-fixer")
                findings = _run(doc, status=None)
                self.assertEqual(self.ref_findings(findings), [])

    def test_waiting_with_no_worker_reference_is_not_a_contradiction(self):
        for state_name in ("FIX_REQUIRED", "REVIEW"):
            with self.subTest(state=state_name):
                doc = _doc(state_name, with_worker=False)
                findings = _run(doc, status=None)
                self.assertEqual(self.ref_findings(findings), [])

    def test_assigned_and_active_still_fail_closed_on_both_checks(self):
        for state_name in ("ASSIGNED", "ACTIVE"):
            with self.subTest(state=state_name, check="missing"):
                doc = _doc(state_name, with_worker=False)
                findings = _run(doc, status=None)
                self.assertEqual(self.ref_findings(findings),
                                 ["MISSING_TASK_WORKER_REF"])
                self.assertTrue(all(f.dangerous for f in findings))
            with self.subTest(state=state_name, check="orphaned"):
                doc = _doc(state_name, with_worker=False,
                           task_worker_override="ghost-worker")
                findings = _run(doc, status=None)
                self.assertEqual(self.ref_findings(findings),
                                 ["ORPHANED_TASK_WORKER_REF"])
                self.assertTrue(all(f.dangerous for f in findings))

    def test_record_present_integrity_checks_still_operate_while_waiting(self):
        """The waiting-state allowance covers ONLY the absent-record case;
        a record that does exist keeps every reality check."""
        for state_name in ("FIX_REQUIRED", "REVIEW"):
            with self.subTest(state=state_name, check="backref"):
                doc = _doc(state_name)
                doc["workers"]["task-001-builder"]["task_id"] = "TASK-999"
                findings = _run(doc, status=_status())
                self.assertIn("WORKER_TASK_BACKREF_MISMATCH", _ids(findings))
            with self.subTest(state=state_name, check="missing_status"):
                doc = _doc(state_name)
                findings = _run(doc, status=None)
                self.assertIn("MISSING_WORKER_STATUS", _ids(findings))
            with self.subTest(state=state_name, check="dead_pid"):
                doc = _doc(state_name)
                findings = _run(doc, status=_status(), pid_alive=False)
                self.assertIn("DEAD_PID_CLAIMED_ALIVE", _ids(findings))
            with self.subTest(state=state_name, check="missing_heartbeat"):
                doc = _doc(state_name)
                findings = _run(doc, status=_status(heartbeat_at=None))
                self.assertIn("MISSING_HEARTBEAT_WHILE_PID_ALIVE", _ids(findings))
                self.assertTrue(all(
                    f.dangerous for f in findings
                    if f.check_id == "MISSING_HEARTBEAT_WHILE_PID_ALIVE"))
            with self.subTest(state=state_name, check="stale_heartbeat"):
                doc = _doc(state_name)
                old = clock.iso(clock.now(TZ) - timedelta(seconds=600))
                findings = _run(doc, status=_status(heartbeat_at=old))
                self.assertIn("HEARTBEAT_STALE_WHILE_PID_ALIVE", _ids(findings))
            with self.subTest(state=state_name, check="worktree"):
                doc = _doc(state_name)
                findings = _run(doc, status=_status(), git_worktrees=set())
                self.assertIn("WORKTREE_NOT_REGISTERED", _ids(findings))


if __name__ == "__main__":
    unittest.main()
