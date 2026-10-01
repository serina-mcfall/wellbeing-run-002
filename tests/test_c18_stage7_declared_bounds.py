"""C-18 stage 7: every post-T1 phase declares a bound, derived from its own
external timeouts.

WHAT STAGE 7 IS FOR. The heartbeat is written inside T1.
`watchdog.heartbeat_fresh` is an OR - fresh if the last beat is younger
than `heartbeat_stale_seconds` (120), OR if now is inside a declared
`busy_until`. `declare_busy` does not refresh `at`, it only adds the
window. So everything after T1 shares one 120 s budget of ordinary
freshness: dispatch execution, security execution, accessibility
execution, the merges, both drains, Jev and the Observer. A phase that can
outlast what is left and does not declare is a healthy supervisor that
looks dead to the Watchdog.

THE CORRECTION THIS FILE PINS DOWN. An earlier proposal priced each bound
at the WORKER'S LEASE - builder execute at `timeouts.builder` (3600).
That is wrong, and `test_a_dispatch_bound_is_not_the_worker_lease` is the
regression guard. `_execute_builder_dispatch` writes a prompt, makes a
worktree, probes ports, writes a job file and calls `workers.start_job`,
which `Popen`s and returns; it never waits for the worker. A 3660 s bound
would let a wedged `workmux add` look healthy for an hour against a 120 s
threshold - exactly the failure declare_busy exists to prevent.

Nothing here reaches the network, spawns a process or writes a real
heartbeat outside its own temporary directory.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import (  # noqa: E402
    clock,
    config,
    gh,
    state as state_mod,
    supervisor as supervisor_mod,
    watchdog,
    workers,
)

TZ = "Pacific/Auckland"
STALE = 120


# ===================================================== the derivation


class BoundDerivation(unittest.TestCase):
    """The numbers, and where they come from."""

    def test_the_dispatch_bound_is_the_sum_of_its_own_external_timeouts(self):
        # Derived, not transcribed. Raising any of these raises the bound
        # with it, so the two cannot drift apart.
        self.assertEqual(
            supervisor_mod.DISPATCH_EXECUTE_BOUND_PER_PLAN,
            gh.TIMEOUT
            + workers.WORKMUX_TIMEOUT
            + workers.WORKMUX_PATH_TIMEOUT
            + 2 * workers.TMUX_TIMEOUT)

    def test_a_dispatch_bound_is_not_the_worker_lease(self):
        """The withdrawn proposal's defect, pinned so it cannot return.

        `timeouts.builder` is how long the WORKER may run. The execute
        phase spawns and returns. Bounding the phase at the lease would
        make a wedged worktree creation invisible for an hour.
        """
        lease = config.load().extra["timeouts"]["builder"]
        bound = supervisor_mod.batch_bound(
            supervisor_mod.DISPATCH_EXECUTE_BOUND_PER_PLAN, 1)
        self.assertLess(bound, lease)
        # And not marginally so - the phase is a different order of work.
        self.assertLess(bound * 2, lease)

    def test_the_merge_bound_covers_every_gh_call_a_candidate_makes(self):
        # route_prs' pr_view, then attempt_merge's update_branch, pr_view,
        # merge and the post-merge pr_view.
        self.assertEqual(supervisor_mod.MERGE_EXECUTE_BOUND_PER_CANDIDATE,
                         6 * gh.TIMEOUT)

    def test_the_drain_bound_admits_the_in_flight_send(self):
        """`drain_notifications`' docstring states the honest bound as the
        budget plus at most one in-flight send, because http.post_json's
        timeout is per socket operation. The bound says the same."""
        self.assertEqual(
            supervisor_mod.DRAIN_BOUND_SECONDS,
            supervisor_mod.Supervisor.DRAIN_BUDGET_SECONDS + 20.0)

    def test_no_bound_is_open_ended(self):
        for name in ("DISPATCH_EXECUTE_BOUND_PER_PLAN",
                     "MERGE_EXECUTE_BOUND_PER_CANDIDATE",
                     "DRAIN_BOUND_SECONDS",
                     "JEV_BOUND_SECONDS", "BUSY_MARGIN_SECONDS"):
            value = getattr(supervisor_mod, name)
            self.assertIsInstance(value, (int, float), name)
            self.assertGreater(value, 0, name)
            self.assertLess(value, 3600, name)


class BatchScaling(unittest.TestCase):
    """Every declared phase loops. One fixed number would under-bound two
    items, which is how a legitimate second dispatch becomes a dead
    supervisor."""

    def test_the_bound_grows_with_the_batch(self):
        one = supervisor_mod.batch_bound(100, 1)
        two = supervisor_mod.batch_bound(100, 2)
        self.assertEqual(one, 160)
        self.assertEqual(two, 260)

    def test_the_margin_is_added_once_not_per_item(self):
        self.assertEqual(
            supervisor_mod.batch_bound(100, 3),
            300 + supervisor_mod.BUSY_MARGIN_SECONDS)

    def test_an_empty_batch_still_yields_a_usable_window(self):
        # The call sites guard on a non-empty batch, so this is belt and
        # braces: a zero-width window would be instantly expired and read
        # as a wedge.
        self.assertGreater(supervisor_mod.batch_bound(100, 0), 0)


# ============================================== run_declared semantics


class RunDeclaredSemantics(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "heartbeat.json"
        self.sup = supervisor_mod.Supervisor.__new__(supervisor_mod.Supervisor)
        self.sup.now = mock.Mock(
            return_value=datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo(TZ)))
        self.patcher = mock.patch.object(config, "HEARTBEAT_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.path.write_text(json.dumps({"pid": 1, "at": clock.iso(
            self.sup.now())}), encoding="utf-8")

    def beat(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_it_returns_what_the_action_returned(self):
        """Stage 7 needs this: a phase whose result feeds a commit step
        must be declarable without being split in two."""
        self.assertEqual(
            self.sup.run_declared("dispatch", 100, lambda: ["a result"]),
            ["a result"])

    def test_the_window_is_open_while_the_action_runs(self):
        seen = {}

        def action():
            seen.update(self.beat())
        self.sup.run_declared("accessibility", 1140, action)
        self.assertEqual(seen.get("busy_with"), "accessibility")
        self.assertEqual(
            seen.get("busy_until"),
            clock.iso(self.sup.now() + timedelta(seconds=1140)))

    def test_the_window_is_closed_afterwards(self):
        self.sup.run_declared("merges", 780, lambda: None)
        self.assertNotIn("busy_until", self.beat())

    def test_the_window_is_closed_even_when_the_action_raises(self):
        """Otherwise one failed phase would leave a window open across
        everything after it, and a genuinely wedged supervisor would be
        covered by a declaration nobody is inside."""
        with self.assertRaises(RuntimeError):
            self.sup.run_declared("merges", 780, self._boom)
        self.assertNotIn("busy_until", self.beat())
        self.assertNotIn("busy_with", self.beat())

    @staticmethod
    def _boom():
        raise RuntimeError("phase failed")


# ====================================== expiry and recovery, for real


class ExpiryAndRecovery(unittest.TestCase):
    """The Watchdog's side, against a real heartbeat file written by the
    real `declare_busy`. This is the half that proves a bound does not
    blunt detection."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "heartbeat.json"
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo(TZ))
        self.sup = supervisor_mod.Supervisor.__new__(supervisor_mod.Supervisor)
        self.sup.now = mock.Mock(return_value=self.now)
        for target in (config, watchdog.config):
            patcher = mock.patch.object(target, "HEARTBEAT_PATH", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_beat(self, age_seconds: int) -> None:
        self.path.write_text(json.dumps(
            {"pid": 1, "at": clock.iso(self.now - timedelta(seconds=age_seconds))}),
            encoding="utf-8")

    def at(self, offset_seconds: int):
        """Freeze the watchdog's clock at now + offset."""
        moment = self.now + timedelta(seconds=offset_seconds)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment
        return mock.patch.object(watchdog, "datetime", FrozenDatetime)

    def test_a_stale_beat_inside_a_declared_window_is_healthy(self):
        self.write_beat(age_seconds=600)          # far past 120 s
        with self.at(0):
            self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE))
        self.sup.declare_busy("accessibility", 1140)
        with self.at(0):
            self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
            self.assertEqual(watchdog.busy_with(TZ), "accessibility")

    def test_the_window_expires_and_the_supervisor_is_stale_again(self):
        """A bound is not an exemption. Overrun it and detection returns."""
        self.write_beat(age_seconds=600)
        self.sup.declare_busy("accessibility", 1140)
        with self.at(1139):
            self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
        with self.at(1141):
            self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE))
            self.assertIsNone(watchdog.busy_with(TZ))

    def test_finishing_the_work_refreshes_the_beat_and_drops_the_window(self):
        """Recovery, as `clear_busy` actually defines it.

        Checked against the code rather than assumed - this case was
        written the other way round first and went red. `clear_busy` pops
        the three busy keys AND rewrites `at` (supervisor.py:806). That is
        right: the phase returned, so the process is demonstrably alive and
        has earned a full ordinary window. What matters is that the LONG
        window does not survive the work it was declared for.
        """
        self.write_beat(age_seconds=600)
        self.sup.declare_busy("accessibility", 1140)
        self.sup.clear_busy()
        beat = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("busy_until", beat)
        self.assertEqual(beat["at"], clock.iso(self.now))
        with self.at(0):
            self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))

    def test_a_cleared_window_cannot_outlive_ordinary_staleness(self):
        """The property the case above exists to protect. After a 1140 s
        window is cleared, a supervisor that then wedges must be caught at
        120 s - not at 1140."""
        self.write_beat(age_seconds=600)
        self.sup.declare_busy("accessibility", 1140)
        self.sup.clear_busy()
        with self.at(STALE - 1):
            self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))
        with self.at(STALE + 1):
            self.assertFalse(watchdog.heartbeat_fresh(TZ, STALE),
                             "the cleared declaration is still covering it")

    def test_a_declaration_never_shortens_ordinary_freshness(self):
        """`heartbeat_fresh` tests the beat age FIRST, so declaring can only
        extend. This is why adding declarations is safe in the direction
        that matters - a short bound cannot cause a false kill."""
        self.write_beat(age_seconds=5)
        self.sup.declare_busy("notifications", 1)   # deliberately tiny
        with self.at(10):
            self.assertTrue(watchdog.heartbeat_fresh(TZ, STALE))


# ================================ the tick actually declares its phases


class TheTickDeclaresEverySlowPhase(unittest.TestCase):
    """Not the constants - the wiring. Each phase is observed from inside
    its own declaration, through a real `Supervisor.tick`."""

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sup.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.sup.ledger = mock.Mock()
        self.sup.notifier = mock.Mock()
        self.sup.log = mock.Mock()
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        self.sup.store._write(doc)

        self.declared: list[tuple[str, float]] = []
        real = self.sup.run_declared

        def spy(what, bound_seconds, action):
            self.declared.append((what, bound_seconds))
            if what in ("jev", "observer"):
                return None            # slow provider work, not this test
            return real(what, bound_seconds, action)
        self.sup.run_declared = spy

    def run_tick(self):
        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=None), \
                mock.patch.object(config, "HEARTBEAT_PATH",
                                  self.root / "heartbeat.json"):
            self.sup.tick()

    def bounds_for(self, what: str) -> list[float]:
        return [b for name, b in self.declared if name == what]

    def test_both_notification_drains_are_declared(self):
        """Two drain points per tick, with Jev and the Observer between
        them - so the second needs its own window, not the first's."""
        self.run_tick()
        self.assertEqual(self.bounds_for("notifications"),
                         [supervisor_mod.DRAIN_BOUND_SECONDS] * 2)

    def test_a_quiet_tick_declares_nothing_it_did_not_do(self):
        """No plans, no candidates - so no dispatch, security,
        accessibility or merge window. A declaration for work that is not
        happening is a window a wedge could hide in."""
        self.run_tick()
        for phase in ("dispatch", "security", "accessibility", "merges"):
            self.assertEqual(self.bounds_for(phase), [], phase)

    def test_the_accessibility_phase_runs_inside_its_own_window(self):
        """The one that was always mandatory. A single attempt may use the
        whole governed G3 budget - 1080 s, nine times the staleness
        threshold - so this proves the window is OPEN while it runs, not
        merely that a number exists.
        """
        budget = self.sup._accessibility_budget()
        seen = {}

        def fake_route_evidence(doc, observations):
            self.sup._accessibility_plans.append(("TASK-001", "a plan"))
            return []

        def fake_execute(plans):
            seen["busy_with"] = watchdog.busy_with(TZ)
            return []

        with mock.patch.object(watchdog.config, "HEARTBEAT_PATH",
                               self.root / "heartbeat.json"), \
                mock.patch.object(self.sup, "route_evidence",
                                  side_effect=fake_route_evidence), \
                mock.patch.object(self.sup, "execute_accessibility",
                                  side_effect=fake_execute), \
                mock.patch.object(self.sup, "commit_accessibility"):
            self.run_tick()

        self.assertEqual(seen.get("busy_with"), "accessibility")
        self.assertEqual(self.bounds_for("accessibility"),
                         [budget.total + supervisor_mod.BUSY_MARGIN_SECONDS])

    def test_the_merge_phase_runs_inside_its_own_window(self):
        """Where a false-positive kill is least acceptable, because
        gh.merge is irreversible."""
        seen = {}

        def fake_route_prs(doc, cs, open_prs, pr_obs, review_obs):
            return [("TASK-001", 7)]

        def fake_execute_merges(candidates):
            seen["busy_with"] = watchdog.busy_with(TZ)

        with mock.patch.object(watchdog.config, "HEARTBEAT_PATH",
                               self.root / "heartbeat.json"), \
                mock.patch.object(self.sup, "route_prs",
                                  side_effect=fake_route_prs), \
                mock.patch.object(self.sup, "execute_merges",
                                  side_effect=fake_execute_merges):
            self.run_tick()

        self.assertEqual(seen.get("busy_with"), "merges")
        self.assertEqual(
            self.bounds_for("merges"),
            [supervisor_mod.MERGE_EXECUTE_BOUND_PER_CANDIDATE
             + supervisor_mod.BUSY_MARGIN_SECONDS])

    def test_the_merge_window_scales_with_the_number_of_candidates(self):
        def fake_route_prs(doc, cs, open_prs, pr_obs, review_obs):
            return [("TASK-001", 7), ("TASK-002", 8), ("TASK-003", 9)]

        with mock.patch.object(watchdog.config, "HEARTBEAT_PATH",
                               self.root / "heartbeat.json"), \
                mock.patch.object(self.sup, "route_prs",
                                  side_effect=fake_route_prs), \
                mock.patch.object(self.sup, "execute_merges"):
            self.run_tick()

        self.assertEqual(
            self.bounds_for("merges"),
            [3 * supervisor_mod.MERGE_EXECUTE_BOUND_PER_CANDIDATE
             + supervisor_mod.BUSY_MARGIN_SECONDS])


if __name__ == "__main__":
    unittest.main()
