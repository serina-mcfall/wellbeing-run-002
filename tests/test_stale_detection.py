"""Regression tests for the approved stale-detection behaviour (DEV-002).

Scope is deliberately narrow: these cover `Supervisor.detect_stale` only. They
exist because the original rule measured progress solely as growth in the worker
output file, which a buffered agent can never produce - so a healthy builder was
classified as stale and would have been recycled mid-build.

The approved definition is:
  STALE = the worker's agent process vanished without reporting a terminal
  status, OR a worker that had already begun producing output made no progress
  for longer than stale_task_minutes. A worker that has not yet produced any
  output is not stale; it is bounded by its own hard timeout.

Everything the supervisor would reach outside its own state is stubbed: no
Discord message, no workmux call, no ledger write, no real worker.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, config, state, supervisor as supervisor_mod  # noqa: E402

TZ = "Pacific/Auckland"
AGENT_PID = 4242


def build_doc(last_progress_minutes_ago: float) -> dict:
    doc = state.initial_document("run-001", "v1.0")
    doc["counters"].setdefault("guardrail_activations", 0)
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task["state"] = "ASSIGNED"
    task["worker"] = "task-001-builder"
    task["branch"] = "task/task-001"
    moment = clock.now(TZ) - timedelta(minutes=last_progress_minutes_ago)
    task["assigned_at"] = clock.iso(moment)
    task["last_progress_at"] = clock.iso(moment)
    task["progress_marker"] = 0
    return doc


class StaleDetectionCase(unittest.TestCase):
    def setUp(self):
        cfg = config.load()
        self.assertEqual(cfg.stale_task_minutes, 25,
                         "these tests assume the T+00 stale window of 25 minutes")
        self.cfg = cfg
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(cfg)
        self.sup.log = mock.Mock()
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def run_detect(self, doc: dict, status: dict | None, process_alive: bool):
        with mock.patch.object(supervisor_mod.workers, "read_status",
                               return_value=status), \
                mock.patch.object(supervisor_mod.workers, "process_alive",
                                  return_value=process_alive), \
                mock.patch.object(supervisor_mod.workers, "remove_worker") as remove:
            self.sup.detect_stale(doc)
        return remove


class TestNotYetProducingOutput(StaleDetectionCase):
    """The defect DEV-002 fixed: a buffered agent cannot emit a progress marker."""

    def test_silent_but_live_worker_is_not_stale_even_past_the_window(self):
        doc = build_doc(last_progress_minutes_ago=40)
        status = {"phase": "RUNNING", "prompt_accepted": False, "output_bytes": 0,
                  "agent_pid": AGENT_PID}
        remove = self.run_detect(doc, status, process_alive=True)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ASSIGNED")
        remove.assert_not_called()
        self.sup.notify_out.assert_not_called()


class TestStreamingThenStalled(StaleDetectionCase):
    """A worker that produced output and then stopped progressing has hung."""

    def test_stalled_streaming_worker_is_recycled(self):
        doc = build_doc(last_progress_minutes_ago=40)
        status = {"phase": "RUNNING", "prompt_accepted": True, "output_bytes": 8192,
                  "agent_pid": AGENT_PID}
        remove = self.run_detect(doc, status, process_alive=True)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "READY")
        remove.assert_called_once_with("task-001-builder")
        history = doc["tasks"]["TASK-001"]["history"]
        self.assertIn("no meaningful progress", history[0]["reason"])

    def test_streaming_worker_inside_the_window_is_left_alone(self):
        doc = build_doc(last_progress_minutes_ago=5)
        status = {"phase": "RUNNING", "prompt_accepted": True, "output_bytes": 8192,
                  "agent_pid": AGENT_PID}
        remove = self.run_detect(doc, status, process_alive=True)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ASSIGNED")
        remove.assert_not_called()


class TestProcessVanished(StaleDetectionCase):
    """A dead agent with no terminal status is stale immediately, window or not."""

    def test_vanished_process_is_stale_even_inside_the_window(self):
        doc = build_doc(last_progress_minutes_ago=2)
        status = {"phase": "RUNNING", "prompt_accepted": True, "output_bytes": 100,
                  "agent_pid": AGENT_PID}
        remove = self.run_detect(doc, status, process_alive=False)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "READY")
        remove.assert_called_once_with("task-001-builder")
        self.assertIn("vanished", doc["tasks"]["TASK-001"]["history"][0]["reason"])

    def test_vanished_process_before_any_output_is_still_stale(self):
        doc = build_doc(last_progress_minutes_ago=2)
        status = {"phase": "RUNNING", "prompt_accepted": False, "output_bytes": 0,
                  "agent_pid": AGENT_PID}
        remove = self.run_detect(doc, status, process_alive=False)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "READY")
        remove.assert_called_once_with("task-001-builder")


class TestTerminalAndMissingStatus(StaleDetectionCase):
    """Finished workers belong to reaping, not to stale detection."""

    def test_terminal_phase_is_left_for_the_reaper(self):
        for phase in ("DONE", "FAILED", "TIMEOUT"):
            with self.subTest(phase=phase):
                doc = build_doc(last_progress_minutes_ago=90)
                status = {"phase": phase, "prompt_accepted": True, "output_bytes": 10,
                          "agent_pid": AGENT_PID}
                remove = self.run_detect(doc, status, process_alive=False)
                self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ASSIGNED")
                remove.assert_not_called()

    def test_worker_with_no_status_file_yet_is_not_stale(self):
        doc = build_doc(last_progress_minutes_ago=40)
        remove = self.run_detect(doc, None, process_alive=False)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ASSIGNED")
        remove.assert_not_called()


class TestScope(StaleDetectionCase):
    """Stale detection applies only to tasks that own a running worker."""

    def test_states_without_a_worker_are_ignored(self):
        for task_state in ("READY", "PR_OPEN", "REVIEW", "COMPLETE"):
            with self.subTest(state=task_state):
                doc = build_doc(last_progress_minutes_ago=90)
                doc["tasks"]["TASK-001"]["state"] = task_state
                status = {"phase": "RUNNING", "prompt_accepted": True,
                          "output_bytes": 10, "agent_pid": AGENT_PID}
                self.run_detect(doc, status, process_alive=False)
                self.assertEqual(doc["tasks"]["TASK-001"]["state"], task_state)


if __name__ == "__main__":
    unittest.main()
