"""Regression tests for the watchdog/supervisor liveness interaction (DEV-006).

The failure reproduced twice in production. A bounded Grok Observer job runs
outside the state lock but still inside the tick, so no heartbeat is written for
minutes - longer than the 120s staleness threshold. The watchdog declared a
healthy supervisor dead, the DEV-001 singleton guard correctly refused the
redundant restart, the watchdog read that refusal as a failed recovery, and it
exited. Run 001 then had no liveness protection at all.

Required proofs:
  1. a long but bounded Observer operation does not falsely classify the
     supervisor as dead
  2. a refused duplicate start with a healthy incumbent does not terminate
     watchdog protection
  3. a genuinely dead supervisor is still detected and recovery attempted
  4. genuinely failed recovery still escalates

These tests drive the real heartbeat file through a temporary path, so they
exercise the same reader the watchdog uses in production.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import config, watchdog  # noqa: E402

TZ = "Pacific/Auckland"
STALE = 120


def now() -> datetime:
    return datetime.now(ZoneInfo(TZ))


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class HeartbeatCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "supervisor-heartbeat.json"
        patcher = mock.patch.object(config, "HEARTBEAT_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def write(self, *, age_seconds: float, pid: int = 4242,
              busy_with: str | None = None, busy_in: float | None = None) -> None:
        payload = {"pid": pid, "at": iso(now() - timedelta(seconds=age_seconds)),
                   "phase": "NORMAL"}
        if busy_with is not None:
            payload["busy_with"] = busy_with
            payload["busy_until"] = iso(now() + timedelta(seconds=busy_in))
        self.path.write_text(json.dumps(payload), encoding="utf-8")


class TestBoundedObserverIsNotDeath(HeartbeatCase):
    """Proof 1: declared, bounded slow work is not staleness."""

    def test_a_stale_heartbeat_inside_a_declared_window_is_fresh(self):
        # Exactly the production case: observer running, heartbeat 200s old.
        self.write(age_seconds=200, busy_with="observer", busy_in=300)
        self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
        self.assertEqual(watchdog.busy_with(TZ), "observer")

    def test_the_same_staleness_without_a_declaration_is_death(self):
        self.write(age_seconds=200)
        self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE))
        self.assertIsNone(watchdog.busy_with(TZ))

    def test_overrunning_the_declared_window_is_stale_again(self):
        self.write(age_seconds=400, busy_with="observer", busy_in=-1)
        self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE),
                         "a declared window must have a deadline, not be open-ended")
        self.assertIsNone(watchdog.busy_with(TZ))

    def test_a_recent_heartbeat_is_fresh_with_or_without_a_declaration(self):
        self.write(age_seconds=5)
        self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
        self.write(age_seconds=5, busy_with="jev", busy_in=60)
        self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))

    def test_a_corrupt_declaration_does_not_grant_freshness(self):
        self.path.write_text(json.dumps(
            {"pid": 1, "at": iso(now() - timedelta(seconds=400)),
             "busy_until": "not-a-timestamp"}), encoding="utf-8")
        self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE))

    def test_a_missing_heartbeat_is_never_fresh(self):
        self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE))


class WatchdogLoopCase(HeartbeatCase):
    """Drives one pass of the watchdog loop with everything external stubbed."""

    def run_one_pass(self, *, incumbent_alive: bool, start_returns: int | None,
                     recovery_confirmed: bool, fresh):
        """Run exactly one iteration of the watchdog loop.

        The loop is bounded by supervisor_pid, which is called once at the top of
        every iteration: the second call ends the pass. Bounding on time.sleep
        would not work, because the recovery and stand-down paths `continue` past
        it.
        """
        ledger = mock.Mock()
        notifier = mock.Mock()
        notifier.send.return_value = {"ok": True}

        class SecondPass(RuntimeError):
            pass

        iterations = {"n": 0}

        def one_iteration():
            # reap_children runs exactly once at the top of each loop pass, which
            # supervisor_pid does not - the stand-down path re-reads it.
            iterations["n"] += 1
            if iterations["n"] > 1:
                raise SecondPass()

        with mock.patch.object(watchdog, "supervisor_pid", return_value=4242), \
                mock.patch.object(watchdog, "alive", return_value=incumbent_alive), \
                mock.patch.object(watchdog, "start_supervisor",
                                  return_value=start_returns) as start, \
                mock.patch.object(watchdog, "heartbeat_wait",
                                  return_value=recovery_confirmed), \
                mock.patch.object(watchdog, "heartbeat_fresh", side_effect=fresh), \
                mock.patch.object(watchdog, "ledger_mod") as led_mod, \
                mock.patch.object(watchdog, "notify") as notify_mod, \
                mock.patch.object(watchdog.proc, "reap_children",
                                  side_effect=one_iteration), \
                mock.patch.object(watchdog.config, "WATCHDOG_PID_PATH",
                                  Path(self.dir.name) / "wd.pid"), \
                mock.patch.object(watchdog.time, "sleep"):
            led_mod.Ledger.return_value = ledger
            notify_mod.Notifier.return_value = notifier
            notify_mod.HUMAN_REQUIRED = "HUMAN_REQUIRED"
            notify_mod.ATTENTION = "ATTENTION"
            try:
                code = watchdog.main()
            except SecondPass:
                code = None  # survived the pass and looped again
        events = [c.args[0] for c in ledger.append.call_args_list]
        return code, events, start


class TestRefusalDoesNotKillTheWatchdog(WatchdogLoopCase):
    """Proof 2: a refused duplicate start with a healthy incumbent stands down."""

    def test_watchdog_survives_a_refused_start_when_the_incumbent_is_healthy(self):
        # The production false alarm: the heartbeat looks stale at the top of the
        # loop, the start is refused because the incumbent is alive, and the
        # re-check finds the incumbent healthy.
        self.write(age_seconds=200)
        code, events, _ = self.run_one_pass(
            incumbent_alive=True, start_returns=None, recovery_confirmed=False,
            fresh=[False, True])
        self.assertIsNone(code, "the watchdog must not exit on a false alarm")
        self.assertIn("SUPERVISOR_STAND_DOWN", events)
        self.assertNotIn("SUPERVISOR_RESTART_FAILED", events)

    def test_stand_down_is_recorded_with_the_incumbent(self):
        self.write(age_seconds=200)
        code, events, _ = self.run_one_pass(
            incumbent_alive=True, start_returns=None, recovery_confirmed=False,
            fresh=[False, True])
        self.assertEqual(events.count("SUPERVISOR_STAND_DOWN"), 1)


class TestGenuineDeathStillDetected(WatchdogLoopCase):
    """Proof 3: a real dead supervisor is still detected and restarted."""

    def test_dead_supervisor_triggers_a_restart_attempt(self):
        self.write(age_seconds=500)
        code, events, start = self.run_one_pass(
            incumbent_alive=False, start_returns=9999, recovery_confirmed=True,
            fresh=lambda *a: False)
        start.assert_called_once()
        self.assertIn("SUPERVISOR_UNHEALTHY", events)
        self.assertIn("SUPERVISOR_RESTARTED", events)
        self.assertIsNone(code, "a successful recovery keeps the watchdog running")

    def test_a_dead_process_is_detected_even_inside_a_declared_window(self):
        # A supervisor that dies mid-observation must not be shielded by the
        # window it declared before dying: liveness is judged by the PID.
        self.write(age_seconds=30, busy_with="observer", busy_in=300)
        self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
        code, events, start = self.run_one_pass(
            incumbent_alive=False, start_returns=9999, recovery_confirmed=True,
            fresh=lambda *a: True)
        start.assert_called_once()
        self.assertIn("SUPERVISOR_UNHEALTHY", events)


class TestGenuineFailureStillEscalates(WatchdogLoopCase):
    """Proof 4: when no healthy supervisor exists, escalation is unchanged."""

    def test_failed_recovery_with_no_incumbent_escalates_and_exits(self):
        self.write(age_seconds=500)
        code, events, _ = self.run_one_pass(
            incumbent_alive=False, start_returns=None, recovery_confirmed=False,
            fresh=lambda *a: False)
        self.assertEqual(code, 1, "a genuine failure must still exit non-zero")
        self.assertIn("SUPERVISOR_RESTART_FAILED", events)
        self.assertNotIn("SUPERVISOR_STAND_DOWN", events)

    def test_crash_loop_backstop_is_unchanged(self):
        self.assertEqual(watchdog.CRASH_LOOP_LIMIT, 3)
        self.assertEqual(watchdog.CRASH_LOOP_WINDOW_SECONDS, 600)


class TestSingletonInvariantPreserved(unittest.TestCase):
    """The repair must not weaken the guard that DEV-001 introduced."""

    def test_supervisor_still_refuses_to_start_beside_a_live_incumbent(self):
        """The DEV-001 guard must not be weakened.

        It was strengthened instead. The original check-then-write - read the
        pid file, test liveness, then write - left a window in which two
        starting processes could both find no incumbent and both proceed;
        measured at 16 duplicate starts in 40 trials. Exclusion is now a
        non-blocking flock held for the process lifetime, so this pins the
        atomic mechanism rather than the sequence it replaced.

        Behavioural proof that two concurrent starts cannot both win lives in
        tests/test_c05_3_step6b_repairs.py, which races real processes. A
        source grep can only show the mechanism is present.
        """
        from control import supervisor as supervisor_mod
        source = Path(supervisor_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("SUPERVISOR_START_REFUSED", source)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", source)
        self.assertNotIn("proc.is_running(incumbent)", source,
                         "the racy check-then-write guard must not return")


if __name__ == "__main__":
    unittest.main()
