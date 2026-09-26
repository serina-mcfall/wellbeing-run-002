"""D2 (part 3): control/watchdog.py's reconcile_worker_state - the
deterministic-policy actor over control/reconcile.py's facts.

Uses a real state.Store against a temp file (real transaction/lock
mechanics) and a real Ledger against a temp file, with only the raw
observation primitives (read_status/is_running/git) mocked - the same
style as tests/test_reconcile.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, ledger as ledger_mod, notify, reconcile, state, watchdog  # noqa: E402

TZ = "Pacific/Auckland"


class WatchdogReconcileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)

        self.store = state.Store(path=tmp_path / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=tmp_path / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.notifier = mock.Mock(spec=notify.Notifier)
        self.cfg = SimpleNamespace(timezone=TZ)

    def _add_active_task_with_dead_pid(self):
        with self.store.transaction() as doc:
            task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
            task["state"] = "ACTIVE"
            task["worker"] = "task-001-builder"
            doc["workers"]["task-001-builder"] = state.new_worker_record(
                "builder", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            )

    def _ledger_lines(self):
        return [json.loads(line) for line in
               Path(self.ledger.path).read_text(encoding="utf-8").splitlines() if line]

    def run_reconcile(self, *, pid_alive=False, status=None):
        if status is None:
            status = {"phase": "RUNNING", "agent_pid": 4242}
        with mock.patch.object(reconcile.workers_mod, "read_status", return_value=status), \
                mock.patch.object(reconcile.proc, "is_running", return_value=pid_alive):
            watchdog.reconcile_worker_state(self.cfg, self.ledger, self.notifier,
                                            store=self.store)

    def test_a_dangerous_finding_freezes_the_task(self):
        self._add_active_task_with_dead_pid()
        self.run_reconcile(pid_alive=False)
        doc = self.store.read()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")

    def test_freeze_event_is_not_recorded_as_human_intervention(self):
        """The Watchdog independently detecting and freezing a task is its
        own autonomous control-plane action, not a human doing anything."""
        self._add_active_task_with_dead_pid()
        self.run_reconcile(pid_alive=False)
        events = [e for e in self._ledger_lines() if e["event_type"] == "STATE_INVARIANT_VIOLATION"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["human_intervention"], False)

    def test_freeze_event_carries_check_id_worker_and_evidence(self):
        self._add_active_task_with_dead_pid()
        self.run_reconcile(pid_alive=False)
        events = [e for e in self._ledger_lines() if e["event_type"] == "STATE_INVARIANT_VIOLATION"]
        meta = events[0]["metadata_redacted"]
        self.assertEqual(meta["check_id"], "DEAD_PID_CLAIMED_ALIVE")
        self.assertEqual(meta["worker"], "task-001-builder")
        self.assertIn("agent_pid", meta["evidence"])
        self.assertEqual(events[0]["activity_class"], "ESCALATION")
        self.assertEqual(events[0]["outcome"], "FROZEN")

    def test_critical_notification_is_sent(self):
        self._add_active_task_with_dead_pid()
        self.run_reconcile(pid_alive=False)
        self.notifier.send.assert_called_once()
        args, _ = self.notifier.send.call_args
        self.assertEqual(args[0], notify.CRITICAL)

    def test_a_healthy_worker_is_not_frozen_and_nothing_is_logged(self):
        self._add_active_task_with_dead_pid()
        self.run_reconcile(pid_alive=True, status={
            "phase": "RUNNING", "agent_pid": 4242, "heartbeat_at": clock.iso(clock.now(TZ)),
        })
        doc = self.store.read()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ACTIVE")
        self.assertEqual(
            [e for e in self._ledger_lines() if e["event_type"] == "STATE_INVARIANT_VIOLATION"],
            [],
        )
        self.notifier.send.assert_not_called()

    def test_no_state_file_yet_is_a_silent_no_op(self):
        """Pre-T+00: reconciliation must not create or require state that
        does not exist yet."""
        missing_store = state.Store(path=Path(self.tmp.name) / "nonexistent.json", tz=TZ)
        watchdog.reconcile_worker_state(self.cfg, self.ledger, self.notifier,
                                        store=missing_store)
        self.assertFalse(missing_store.path.exists())
        self.notifier.send.assert_not_called()

    def test_never_transitions_a_task_outside_the_reconcilable_states(self):
        """A finding naming a task that has already moved on (e.g. to
        PR_OPEN) since reconcile() read it must not be acted on."""
        with self.store.transaction() as doc:
            task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
            task["state"] = "PR_OPEN"
            task["worker"] = "task-001-builder"
        # No worker record at all for a PR_OPEN task is normal (see
        # control/reconcile.py's RECONCILABLE_STATES gate) - this proves
        # the policy layer respects that gate too, not just reconcile().
        self.run_reconcile(pid_alive=False)
        doc = self.store.read()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")
        self.notifier.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()


# ====================================================================== C-14.2
# Independent detection: the Watchdog must reach the same verdicts without the
# Supervisor being involved at all. Behavioural - these drive the existing
# reconcile entry point and assert observable outcomes, not module names.

from datetime import timedelta  # noqa: E402

from control import gh, routing  # noqa: E402

MERGED_SHA = "abc123def456abc123def456abc123def456abcd"


class WatchdogMergeInvariantCase(WatchdogReconcileCase):

    def seed_pr_task(self, *, task_state="REVIEW", merged=False, debt=None,
                     last_review_hours_ago=1):
        with self.store.transaction() as doc:
            task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
            task.update({"state": task_state, "branch": "task/task-001", "pr": 100,
                         "worker": "task-001-review-1"})
            # A healthy worker, so control/reconcile.py's worker checks produce
            # nothing: every freeze observed in these tests is then
            # attributable to the merge-invariant pass and nothing else.
            doc["workers"]["task-001-review-1"] = state.new_worker_record(
                "reviewer", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)))
            task["history"].append(
                {"at": clock.iso(clock.now(TZ) - timedelta(hours=3)),
                 "from": "ACTIVE", "to": "PR_OPEN", "reason": "opened"})
            record = routing.blank_pr_record(100, "TASK-001", "task/task-001")
            record.update({"review_verdict": routing.REVIEW_PASS,
                           "approval_current": True, "merged": merged,
                           "last_review_at": clock.iso(
                               clock.now(TZ) - timedelta(hours=last_review_hours_ago))})
            doc["prs"]["100"] = record
            if debt:
                doc["debt"] = dict(debt)

    def local_merged_event(self, *, debt_ids=(), status="NOT_REQUIRED"):
        self.ledger.append("MERGED", task_id="TASK-001", pr_id=100,
                           branch="task/task-001", outcome="MERGED",
                           activity_class="ORCHESTRATION",
                           metadata_redacted={"merged_sha": MERGED_SHA,
                                              "detected_externally": False,
                                              "debt_recording_status": status,
                                              "debt_ids": list(debt_ids)})

    def run_watchdog(self, *, pr_state="MERGED"):
        """The Watchdog makes its OWN GitHub observation - the Supervisor is
        never called anywhere in this path."""
        view = None if pr_state is None else {
            "number": 100, "state": pr_state, "isDraft": False,
            "mergeCommit": {"oid": MERGED_SHA}}
        healthy = {"phase": "RUNNING", "agent_pid": 4242,
                   "heartbeat_at": clock.iso(clock.now(TZ))}
        with mock.patch.object(reconcile.workers_mod, "read_status", return_value=healthy), \
                mock.patch.object(reconcile.proc, "is_running", return_value=True), \
                mock.patch.object(gh, "pr_view", return_value=view) as pr_view:
            watchdog.reconcile_worker_state(self.cfg, self.ledger, self.notifier,
                                            store=self.store)
        self.pr_view = pr_view

    def violations(self):
        """Tolerant of a deliberately corrupt line - the point of some of
        these tests is that the ledger cannot be fully parsed."""
        out = []
        for line in Path(self.ledger.path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") == "STATE_INVARIANT_VIOLATION":
                out.append(event)
        return out

    def doc(self):
        return self.store.read()


class TestWatchdogIndependentDetection(WatchdogMergeInvariantCase):

    def test_f5_watchdog_detects_lost_local_commit_without_the_supervisor(self):
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog()

        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "FROZEN")
        found = self.violations()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["metadata_redacted"]["verdict"], "LOST_LOCAL_COMMIT")
        self.assertEqual(found[0]["metadata_redacted"]["detector"], "watchdog")

    def test_watchdog_makes_its_own_github_observation(self):
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog()
        self.pr_view.assert_called()

    def test_f2_debt_divergence_on_a_completed_task_is_annunciated(self):
        """F2. State merged, task COMPLETE, ledger claims debt ids state lacks.
        The Supervisor's external branch skips merged records entirely, so only
        the independent Watchdog pass can see this."""
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        self.run_watchdog()

        found = self.violations()
        self.assertEqual(len(found), 1)
        meta = found[0]["metadata_redacted"]
        self.assertEqual(meta["verdict"], "DEBT_DIVERGENCE")
        self.assertEqual(meta["missing_debt_ids"], ["TASK-001-PR100-F1"])
        self.assertFalse(meta["freezable"])
        self.assertFalse(meta["froze"])
        # COMPLETE is terminal; no illegal transition may be attempted.
        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "COMPLETE")
        self.assertEqual(found[0]["state_after"], "COMPLETE")

    def test_f17_consistent_merged_pull_request_is_not_a_finding(self):
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True,
                          debt={"TASK-001-PR100-F1": {"id": "TASK-001-PR100-F1"}})
        self.run_watchdog()
        self.assertEqual(self.violations(), [])
        self.notifier.send.assert_not_called()

    def test_debt_status_not_required_is_not_a_divergence(self):
        self.local_merged_event(status="NOT_REQUIRED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        self.run_watchdog()
        self.assertEqual(self.violations(), [])

    def test_f9_absent_accepted_findings_is_never_read_as_no_debt_owed(self):
        """The C-10.2 trap: accepted_findings is gone from state, but the
        ledger proves debt was recorded. Absence must not read as healthy."""
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        doc = self.store.read()
        self.assertNotIn("accepted_findings", doc["prs"]["100"])
        self.run_watchdog()
        self.assertEqual(self.violations()[0]["metadata_redacted"]["verdict"],
                         "DEBT_DIVERGENCE")


class TestWatchdogAnnunciationAccounting(WatchdogMergeInvariantCase):

    def test_f10_repeated_loops_annunciate_once(self):
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        for _ in range(3):
            self.run_watchdog()
        self.assertEqual(len(self.violations()), 1)
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.doc()["counters"]["human_interventions"], 1)

    def test_f11_a_changed_verdict_reannunciates(self):
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        self.run_watchdog()
        self.assertEqual(len(self.violations()), 1)

        with self.store.transaction() as doc:          # evidence changes
            doc["prs"]["100"]["merged"] = False
            doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        self.run_watchdog()
        verdicts = [v["metadata_redacted"]["verdict"] for v in self.violations()]
        self.assertEqual(len(verdicts), 2)
        self.assertNotEqual(verdicts[0], verdicts[1])

    def test_human_required_notification_is_sent_once_per_finding(self):
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog()
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.notifier.send.call_args.args[0], notify.HUMAN_REQUIRED)

    def test_violation_event_is_not_marked_as_human_intervention(self):
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog()
        self.assertFalse(self.violations()[0]["human_intervention"])

    def test_no_intervention_record_is_created(self):
        """C-08b.2 production wiring is out of scope for C-14.2."""
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog()
        self.assertEqual(self.doc()["interventions"], {})


class TestWatchdogWorkerReconcileUnaffected(WatchdogMergeInvariantCase):

    def test_no_pr_bearing_task_makes_no_github_call(self):
        self._add_active_task_with_dead_pid()
        with mock.patch.object(gh, "pr_view") as pr_view:
            self.run_reconcile(pid_alive=False)
        pr_view.assert_not_called()


class TestWatchdogUnobservableGithub(WatchdogMergeInvariantCase):
    """The row-1 narrowing, proved on the independent detector too: both
    components must derive the same classification from equivalent evidence."""

    def test_unobservable_without_contradiction_defers(self):
        self.seed_pr_task()
        self.run_watchdog(pr_state=None)

        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertEqual(self.violations(), [])
        self.notifier.send.assert_not_called()
        self.assertEqual(self.doc()["counters"]["human_interventions"], 0)

    def test_unobservable_with_a_durable_local_claim_is_unprovable(self):
        self.local_merged_event()
        self.seed_pr_task()
        self.run_watchdog(pr_state=None)

        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violations()[0]["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertTrue(meta["froze"])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.doc()["counters"]["human_interventions"], 1)

    def test_unobservable_does_not_hide_a_provable_debt_divergence(self):
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        self.seed_pr_task(task_state="COMPLETE", merged=True, debt={})
        self.run_watchdog(pr_state=None)

        meta = self.violations()[0]["metadata_redacted"]
        self.assertEqual(meta["verdict"], "DEBT_DIVERGENCE")
        self.assertEqual(meta["missing_debt_ids"], ["TASK-001-PR100-F1"])

    def test_unreadable_ledger_still_fails_closed_when_github_is_unobservable(self):
        with open(self.ledger.path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "MERGED", "task_id": "TASK-0\n')
        self.seed_pr_task()
        self.run_watchdog(pr_state=None)

        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violations()[0]["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertFalse(meta["ledger_readable"])
