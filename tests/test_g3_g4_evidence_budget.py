"""G3's attempt budget and G4's cumulative evidence wait. Approved 2026-10-01.

G3 — the phase bounds of ONE automated accessibility attempt:

    install + build        600
    server readiness       120   (pre-existing, separate from install/build)
    scan                   300   (pre-existing hard deadline; 120 soft)
    teardown + confirm      60
    ------------------------------
    attempt total         1080

The total is asserted to EQUAL the sum of its parts, so the five numbers
cannot drift into disagreeing about the same attempt.

G4 — 18 000 s cumulative evidence wait, an explicit Run 002 experiment
limit and NOT a guarantee that all valid work fits inside it. A maximally
contended task can legitimately need about 4 h 06 m.

THE ARITHMETIC THESE TESTS EXIST FOR:

    accumulated = closed intervals + the open one, if any

A new SHA opens a new interval and NEVER resets the total; retries and
restarts retain it; nothing is counted twice. Every case here drives an
INJECTED clock — there is no wall-clock test, and none is required.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import config, state as state_mod  # noqa: E402

TZ = "Pacific/Auckland"
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=ZoneInfo(TZ))


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(seconds: float) -> str:
    return at(seconds).isoformat(timespec="seconds")


def a_task(**over):
    doc = state_mod.initial_document("run-002", "2.0")
    task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
    task.update(over)
    return doc, task


class G3AttemptBudgetCase(unittest.TestCase):

    def setUp(self):
        self.t = config.load().extra["timeouts"]

    def test_each_phase_bound_is_the_approved_value(self):
        self.assertEqual(self.t["accessibility_install_build_seconds"], 600)
        self.assertEqual(self.t["product_server_readiness_seconds"], 120)
        self.assertEqual(self.t["accessibility_soft_seconds"], 120)
        self.assertEqual(self.t["accessibility_hard_seconds"], 300)
        self.assertEqual(self.t["accessibility_teardown_seconds"], 60)
        self.assertEqual(self.t["accessibility_attempt_seconds"], 1080)

    def test_the_total_equals_the_sum_of_its_parts(self):
        # The one invariant that keeps the five numbers describing the same
        # attempt. Change any phase without changing the total and this is
        # where it surfaces.
        parts = (self.t["accessibility_install_build_seconds"],
                 self.t["product_server_readiness_seconds"],
                 self.t["accessibility_hard_seconds"],
                 self.t["accessibility_teardown_seconds"])
        self.assertEqual(sum(parts), self.t["accessibility_attempt_seconds"])

    def test_readiness_is_separate_from_install_and_build(self):
        # The distinction that was wrong in an earlier draft: 120 s bounds
        # readiness AFTER the server process starts. npm ci plus next build
        # routinely exceeds it, and conflating them would make every scan
        # time out and look like a product failure.
        self.assertNotEqual(self.t["product_server_readiness_seconds"],
                            self.t["accessibility_install_build_seconds"])

    def test_the_scan_deadline_is_not_the_attempt_deadline(self):
        # accessibility_hard_seconds bounds the SCAN. The attempt is the
        # whole sequence and needs its own, larger bound.
        self.assertLess(self.t["accessibility_hard_seconds"],
                        self.t["accessibility_attempt_seconds"])

    def test_teardown_is_inside_the_attempt_budget(self):
        self.assertLess(self.t["accessibility_teardown_seconds"],
                        self.t["accessibility_attempt_seconds"])


class G4ArithmeticCase(unittest.TestCase):
    """Controlled clock throughout."""

    def test_a_task_that_never_waited_has_waited_nothing(self):
        _, task = a_task()
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(0)), 0.0)

    def test_an_open_interval_accrues(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(600)), 600.0)

    def test_a_closed_interval_stops_accruing(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        state_mod.close_evidence_wait(task, stamp(1000))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(99_000)),
                         1000.0)

    def test_a_second_cycle_ACCUMULATES_and_never_resets(self):
        # The sentence that was ambiguous: a new SHA starts a new evidence
        # cycle but must NEVER reset accumulated time.
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        state_mod.close_evidence_wait(task, stamp(1000))
        state_mod.open_evidence_wait(task, stamp(2000))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(2500)),
                         1500.0)

    def test_reopening_while_already_open_does_not_restart_or_double_count(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        state_mod.open_evidence_wait(task, stamp(400))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(600)), 600.0)

    def test_closing_twice_counts_the_interval_once(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        state_mod.close_evidence_wait(task, stamp(500))
        state_mod.close_evidence_wait(task, stamp(900))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(9000)), 500.0)

    def test_a_restart_retains_both_the_total_and_the_open_interval(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        state_mod.close_evidence_wait(task, stamp(1000))
        state_mod.open_evidence_wait(task, stamp(2000))
        # A Supervisor restart is a JSON round trip through durable state.
        reloaded = json.loads(json.dumps(task))
        self.assertEqual(state_mod.evidence_wait_seconds(reloaded, at(2500)),
                         1500.0)

    def test_a_backwards_clock_never_decreases_the_total(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(900))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(0)), 0.0)
        state_mod.close_evidence_wait(task, stamp(0))
        self.assertEqual(task[state_mod.EVIDENCE_WAIT_TOTAL], 0.0)

    def test_an_unreadable_interval_is_infinite_not_zero(self):
        # A broken accounting record cannot show a task is INSIDE its
        # allowance. "We cannot tell" must escalate, never wait forever.
        for since in ("nonsense", "", 7, [], {}, "2026-10-01"):
            with self.subTest(since=repr(since)):
                task = {state_mod.EVIDENCE_WAIT_SINCE: since}
                self.assertEqual(
                    state_mod.evidence_wait_seconds(task, at(0)),
                    float("inf"))

    def test_a_naive_timestamp_is_refused_rather_than_guessed(self):
        # Pacific/Auckland changes offset; offset-less arithmetic is wrong
        # rather than merely imprecise.
        task = {state_mod.EVIDENCE_WAIT_SINCE: "2026-10-01T09:00:00"}
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(0)),
                         float("inf"))


class G4ExhaustionCase(unittest.TestCase):

    def test_the_allowance_is_the_approved_18000(self):
        self.assertEqual(
            config.load().extra["timeouts"]["waiting_evidence_total_seconds"],
            18000)

    def test_the_allowance_clears_the_worst_contended_case(self):
        # The arithmetic behind the number: one uncontended pass is about
        # 4920 s, and one slot per evidence role against three builders
        # puts a task third in three queues. The allowance must clear that,
        # or it would cut off work that is merely queued.
        worst_contended = 3 * 4920
        self.assertGreater(18000, worst_contended)

    def test_a_task_that_has_not_waited_is_never_exhausted(self):
        # A configuration fault must not escalate a task that has done
        # nothing. The task is not the thing that is broken.
        _, task = a_task()
        for allowance in (18000, None, -1, "x", True):
            with self.subTest(allowance=repr(allowance)):
                self.assertFalse(
                    state_mod.evidence_wait_exhausted(task, at(0), allowance))

    def test_exhaustion_at_the_boundary(self):
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        self.assertFalse(
            state_mod.evidence_wait_exhausted(task, at(17_999), 18000))
        self.assertTrue(
            state_mod.evidence_wait_exhausted(task, at(18_000), 18000))

    def test_an_ungoverned_allowance_escalates_a_waiting_task(self):
        # Fail closed: with no governed bound the wait is unbounded, which
        # is the condition this check exists to prevent.
        _, task = a_task()
        state_mod.open_evidence_wait(task, stamp(0))
        for allowance in (None, "x", -1, True, {}):
            with self.subTest(allowance=repr(allowance)):
                self.assertTrue(
                    state_mod.evidence_wait_exhausted(task, at(1), allowance))

    def test_accumulated_wait_across_cycles_exhausts(self):
        # Three cycles of 7000 s each: no single cycle exceeds the
        # allowance, the total does. This is what "retries retain it" buys.
        _, task = a_task()
        for cycle in range(3):
            state_mod.open_evidence_wait(task, stamp(cycle * 10_000))
            state_mod.close_evidence_wait(task, stamp(cycle * 10_000 + 7000))
        self.assertEqual(state_mod.evidence_wait_seconds(task, at(99_000)),
                         21_000.0)
        self.assertTrue(
            state_mod.evidence_wait_exhausted(task, at(99_000), 18000))


class TransitionHookCase(unittest.TestCase):
    """The accounting is driven by the one chokepoint, not by call sites."""

    def test_entering_the_state_opens_an_interval(self):
        doc, task = a_task(state="PR_OPEN")
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        self.assertIsNotNone(task[state_mod.EVIDENCE_WAIT_SINCE])

    def test_leaving_the_state_closes_it(self):
        doc, task = a_task(state="PR_OPEN")
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        state_mod.transition(doc, "TASK-001", "REVIEW", "r", TZ)
        self.assertIsNone(task[state_mod.EVIDENCE_WAIT_SINCE])
        self.assertGreaterEqual(task[state_mod.EVIDENCE_WAIT_TOTAL], 0)

    def test_the_remediation_loop_accumulates_rather_than_resetting(self):
        # WAITING_EVIDENCE -> FIX_REQUIRED -> PR_OPEN -> WAITING_EVIDENCE.
        # Each pass opens a new interval on top of the accumulated total,
        # which is what makes a new SHA a new CYCLE and not a new CLOCK.
        doc, task = a_task(state="PR_OPEN")
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        task[state_mod.EVIDENCE_WAIT_SINCE] = stamp(0)
        state_mod.transition(doc, "TASK-001", "FIX_REQUIRED", "r", TZ)
        banked = task[state_mod.EVIDENCE_WAIT_TOTAL]
        self.assertGreater(banked, 0)

        state_mod.transition(doc, "TASK-001", "PR_OPEN", "r", TZ)
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        self.assertEqual(task[state_mod.EVIDENCE_WAIT_TOTAL], banked,
                         "a new evidence cycle reset the accumulated total")
        self.assertIsNotNone(task[state_mod.EVIDENCE_WAIT_SINCE])

    def test_a_no_op_transition_does_not_touch_the_accounting(self):
        doc, task = a_task(state="PR_OPEN")
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        opened = task[state_mod.EVIDENCE_WAIT_SINCE]
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "r", TZ)
        self.assertEqual(task[state_mod.EVIDENCE_WAIT_SINCE], opened)


if __name__ == "__main__":
    unittest.main()
