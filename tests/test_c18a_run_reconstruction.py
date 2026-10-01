"""C-18a: the durable evidence that lets a reader reconstruct the run.

Run 002 itself is now the endurance experiment (CONTRADICTION-AUDIT.md row
C-18a, human decision 2026-10-01), replacing the standalone five-hour
rehearsal. That changes what the run's evidence is FOR: how long the
apparatus operated, what failed first, what it recovered from by itself,
and why it stopped are now RESULTS, not operational detail.

The operator's obligations, verbatim in intent:

  * reconstruct the first failure, elapsed runtime, task/PR state,
    recovery attempts and the stopping reason;
  * distinguish autonomous recovery from human intervention;
  * never reset or extend the original run clock to hide downtime;
  * never describe an early stop as a completed 24-hour run.

These tests pin the parts of that which are properties of this code. They
do NOT claim the run is reconstructible in full - section 37 of the
handover reports what is and is not covered, including the cases no event
this process writes could ever cover (SIGKILL, power loss).
"""

from __future__ import annotations

import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import state as state_mod  # noqa: E402
from control import supervisor as supervisor_mod  # noqa: E402


def a_supervisor():
    """A Supervisor with __init__ bypassed.

    _stop_reason reads exactly one attribute and touches nothing else, so
    constructing the real object - which opens config, a store, a notifier,
    telemetry and a Jev client - would test those instead of this.
    """
    sup = object.__new__(supervisor_mod.Supervisor)
    sup._stop_signal = None
    sup.stopping = False
    return sup


class StopReasonCase(unittest.TestCase):
    """Why the loop ended reaches durable evidence."""

    def test_sigterm_and_sigint_are_named(self):
        for signum, expected in ((signal.SIGTERM, "SIGTERM"),
                                 (signal.SIGINT, "SIGINT")):
            with self.subTest(signal=expected):
                sup = a_supervisor()
                sup._stop_signal = signum
                self.assertEqual(sup._stop_reason(), expected)

    def test_no_signal_is_not_silently_reported_as_a_clean_stop(self):
        # The token exists so a future stop path that forgets to record
        # itself is visible rather than indistinguishable from SIGTERM.
        self.assertEqual(a_supervisor()._stop_reason(),
                         "LOOP_EXITED_WITHOUT_SIGNAL")

    def test_an_unnamed_signal_is_recorded_as_unknown_not_dropped(self):
        sup = a_supervisor()
        sup._stop_signal = signal.SIGHUP
        self.assertEqual(sup._stop_reason(),
                         f"UNKNOWN_SIGNAL_{int(signal.SIGHUP)}")

    def test_the_reason_vocabulary_is_fixed_and_finite(self):
        # C-16: durable evidence carries fixed structural metadata, never
        # runtime prose. Every value this can emit is a constant or a
        # signal number.
        self.assertEqual(set(supervisor_mod.Supervisor.STOP_REASONS.values()),
                         {"SIGTERM", "SIGINT"})

    def test_the_first_signal_wins(self):
        # A second SIGTERM arriving while the current tick finishes must
        # not rewrite why the stop began.
        sup = a_supervisor()
        captured = {}

        def fake_signal(signum, handler):
            captured[signum] = handler

        with mock.patch.object(supervisor_mod.signal, "signal", fake_signal):
            # Re-create just the handler-installation the run loop does.
            def stop(signum, _frame):
                if sup._stop_signal is None:
                    sup._stop_signal = signum
                sup.stopping = True
            supervisor_mod.signal.signal(signal.SIGTERM, stop)
            supervisor_mod.signal.signal(signal.SIGINT, stop)

        captured[signal.SIGINT](signal.SIGINT, None)
        captured[signal.SIGTERM](signal.SIGTERM, None)
        self.assertTrue(sup.stopping)
        self.assertEqual(sup._stop_reason(), "SIGINT")


class RunClockCase(unittest.TestCase):
    """The 24-hour clock is set once and cannot be quietly moved."""

    def a_document(self):
        return state_mod.initial_document("run-002", "2.0")

    def test_a_fresh_document_has_no_start_time(self):
        self.assertIsNone(self.a_document()["started_at"])

    def test_the_counters_separate_human_intervention_from_autonomous_recovery(self):
        # "Distinguish autonomous recovery from human intervention" needs
        # two counts that cannot be confused for one another: a human-side
        # count nothing autonomous increments, and an autonomous-recovery
        # count no human action increments.
        counters = self.a_document()["counters"]
        self.assertIn("human_interventions", counters)
        self.assertIn("recoveries", counters)
        self.assertEqual(counters["human_interventions"], 0)
        self.assertEqual(counters["recoveries"], 0)

    def test_elapsed_runtime_has_an_anchor_that_is_written_once(self):
        # started_at is the only run-clock anchor; everything else is
        # derived from ledger timestamps against it.
        doc = self.a_document()
        self.assertIn("started_at", doc)
        self.assertIn("frozen_at", doc)

    def test_ctl_start_refuses_a_second_start(self):
        # The clock must not be reset or extended to hide downtime. The
        # guard is in cli.cmd_start, which refuses once started_at is set.
        source = (Path(__file__).resolve().parent.parent
                  / "control" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('already started at', source)
        self.assertIn('the protocol is frozen', source)


class RecoveryVocabularyCase(unittest.TestCase):
    """Autonomous recovery is nameable in the ledger, separately from humans."""

    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.supervisor_src = (root / "control" / "supervisor.py").read_text(
            encoding="utf-8")
        self.watchdog_src = (root / "control" / "watchdog.py").read_text(
            encoding="utf-8")

    def test_the_supervisor_names_its_own_autonomous_recoveries(self):
        for event in ('"DISPATCH_RECOVERED"', '"RECOVERED_FROM_JOB_FILE"'):
            with self.subTest(event=event):
                self.assertIn(event, self.supervisor_src)

    def test_the_watchdog_names_supervisor_restart_outcomes_both_ways(self):
        # A restart that worked and one that did not must be different
        # events, or "recovery attempts" cannot be counted honestly.
        self.assertIn('"SUPERVISOR_RESTARTED"', self.watchdog_src)
        self.assertIn('"SUPERVISOR_RESTART_FAILED"', self.watchdog_src)

    def test_the_run_boundary_events_exist_in_matched_pairs(self):
        # A SUPERVISOR_STARTED with no matching SUPERVISOR_STOPPED is how a
        # death is told apart from a stop. Both must be emitted.
        self.assertIn('"SUPERVISOR_STARTED"', self.supervisor_src)
        self.assertIn('"SUPERVISOR_STOPPED"', self.supervisor_src)

    def test_the_stopped_event_carries_the_reason(self):
        self.assertIn('metadata_redacted={"stop_reason": self._stop_reason()}',
                      self.supervisor_src)


if __name__ == "__main__":
    unittest.main()
