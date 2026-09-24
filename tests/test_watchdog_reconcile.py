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
