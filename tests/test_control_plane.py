"""Control-plane unit tests.

These cover the decisions that can silently go wrong and cost the experiment:
backpressure arithmetic, the merge gate, state transitions, budget thresholds,
the migration lock, deadline phases, review parsing and secret redaction.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    budget,
    clock,
    migration_lock,
    proc,
    providers,
    redact,
    routing,
    state,
)

TZ = "Pacific/Auckland"


def fresh_doc() -> dict:
    doc = state.initial_document("run-001", "v1.0")
    providers.ensure(doc)
    budget.ensure(doc, 25.0)
    migration_lock.ensure(doc)
    return doc


class TestBackpressure(unittest.TestCase):
    def test_limits_follow_the_protocol_table(self):
        self.assertEqual(state.builder_limit(0, 3), 3)
        self.assertEqual(state.builder_limit(1, 3), 3)
        self.assertEqual(state.builder_limit(2, 3), 2)
        self.assertEqual(state.builder_limit(3, 3), 0)
        self.assertEqual(state.builder_limit(9, 3), 0)

    def test_configured_maximum_is_never_exceeded(self):
        self.assertEqual(state.builder_limit(0, 2), 2)
        self.assertEqual(state.builder_limit(2, 1), 1)

    def test_review_queue_counts_prs_awaiting_review(self):
        doc = fresh_doc()
        for index, task_state in enumerate(("PR_OPEN", "REVIEW", "ACTIVE", "COMPLETE")):
            task = state.add_task(doc, f"T-{index}", "t", [], "feature", False, TZ)
            task["state"] = task_state
        self.assertEqual(state.review_queue_depth(doc), 2)


class TestTransitions(unittest.TestCase):
    def test_illegal_transition_is_refused(self):
        doc = fresh_doc()
        state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "MERGED", "skipping review", TZ)

    def test_legal_path_records_history(self):
        doc = fresh_doc()
        state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        for target in ("READY", "ASSIGNED", "ACTIVE", "PR_OPEN", "REVIEW", "MERGED",
                       "COMPLETE"):
            state.transition(doc, "TASK-001", target, "test", TZ)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        self.assertEqual(len(doc["tasks"]["TASK-001"]["history"]), 7)

    def test_dependencies_must_be_complete(self):
        doc = fresh_doc()
        state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
        child = state.add_task(doc, "TASK-002", "Check-in", ["TASK-001"], "feature",
                               True, TZ)
        self.assertFalse(state.dependencies_met(doc, child))
        doc["tasks"]["TASK-001"]["state"] = "COMPLETE"
        self.assertTrue(state.dependencies_met(doc, child))


class TestMergeGate(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 7, "state": "OPEN", "isDraft": False,
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                                   "conclusion": "SUCCESS"}],
        }
        self.record = routing.blank_pr_record(7, "TASK-001", "task/task-001")
        self.record.update({"review_verdict": routing.REVIEW_PASS,
                            "approval_current": True,
                            "reviewed_diff_hash": "abc123"})

    def test_all_gates_satisfied_allows_merge(self):
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123")
        self.assertTrue(decision.allowed, decision.reason)

    def test_changed_material_diff_invalidates_approval(self):
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, "different")
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.invalidate_approval)

    def test_missing_required_check_blocks(self):
        self.pr["statusCheckRollup"] = []
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123")
        self.assertFalse(decision.allowed)
        self.assertIn("not reported", decision.reason)

    def test_failing_required_check_blocks(self):
        self.pr["statusCheckRollup"] = [{"name": "ci", "status": "COMPLETED",
                                         "conclusion": "FAILURE"}]
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123")
        self.assertFalse(decision.allowed)

    def test_stale_approval_blocks(self):
        self.record["approval_current"] = False
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123").allowed
        )

    def test_no_review_pass_blocks(self):
        self.record["review_verdict"] = None
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123").allowed
        )

    def test_red_guardrail_blocks(self):
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), True, "abc123").allowed
        )

    def test_branch_behind_main_blocks_until_reconciled(self):
        self.pr["mergeStateStatus"] = "BEHIND"
        self.assertFalse(
            routing.evaluate_merge(self.pr, self.record, ("ci",), False, "abc123").allowed
        )

    def test_unverifiable_diff_invalidates_rather_than_passing(self):
        decision = routing.evaluate_merge(self.pr, self.record, ("ci",), False, None)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.invalidate_approval)


class TestReviewParsing(unittest.TestCase):
    def test_parses_pass_verdict(self):
        review = routing.parse_review(
            'prose\n```json\n{"verdict":"REVIEW_PASS","gates":{"SCOPE":"PASS"},'
            '"findings":[],"summary":"clean"}\n```\n'
        )
        self.assertEqual(review.verdict, routing.REVIEW_PASS)
        self.assertEqual(review.findings, [])

    def test_missing_block_is_not_a_pass(self):
        self.assertEqual(routing.parse_review("Looks good to me!").verdict,
                         routing.REVIEW_UNPARSEABLE)

    def test_pass_with_blocking_findings_is_inconsistent(self):
        review = routing.parse_review(
            '```json\n{"verdict":"REVIEW_PASS","gates":{},'
            '"findings":[{"severity":"P1","summary":"no focus style"}]}\n```'
        )
        consistent, why = routing.review_is_consistent(review, touches_ui=True)
        self.assertFalse(consistent)
        self.assertIn("P0/P1", why)

    def test_ui_review_must_grade_the_named_gates(self):
        review = routing.parse_review(
            '```json\n{"verdict":"REVIEW_PASS","gates":{"SCOPE":"PASS"},"findings":[]}\n```'
        )
        consistent, why = routing.review_is_consistent(review, touches_ui=True)
        self.assertFalse(consistent)
        self.assertIn("OVERENGINEERING", why)

    def test_failing_gate_contradicts_a_pass(self):
        review = routing.parse_review(
            '```json\n{"verdict":"REVIEW_PASS",'
            '"gates":{"OVERENGINEERING":"FAIL","COGNITIVE_LOAD":"PASS",'
            '"SENSORY_LOAD":"PASS"},"findings":[]}\n```'
        )
        consistent, _ = routing.review_is_consistent(review, touches_ui=True)
        self.assertFalse(consistent)

    def test_findings_get_ids_when_absent(self):
        review = routing.parse_review(
            '```json\n{"verdict":"REVIEW_FAIL","findings":[{"severity":"P1"},'
            '{"severity":"P2"}]}\n```'
        )
        self.assertEqual([f["id"] for f in review.findings], ["F1", "F2"])


class TestBudget(unittest.TestCase):
    def test_thresholds_cross_once_each(self):
        doc = fresh_doc()
        self.assertEqual(budget.record(doc, "openrouter", 12.5, None), ["ATTENTION"])
        self.assertEqual(budget.record(doc, "openrouter", 0.0, None), [])
        self.assertEqual(budget.record(doc, "openrouter", 6.25, None),
                         ["RESTRICT_ESCALATION"])
        self.assertEqual(budget.record(doc, "openrouter", 6.25, None),
                         ["RELEASE_CRITICAL_ONLY", "HARD_STOP"])
        self.assertTrue(doc["budget"]["hard_stop"])
        self.assertFalse(budget.metered_call_allowed(doc))

    def test_subscription_providers_do_not_consume_the_metered_ceiling(self):
        doc = fresh_doc()
        budget.record(doc, "claude", 100.0, None)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)

    def test_estimates_never_count_as_actual_spend(self):
        doc = fresh_doc()
        budget.record(doc, "openrouter", None, 10.0)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        self.assertEqual(doc["budget"]["estimated_usd"], 10.0)

    def test_escalation_restricted_above_seventy_five_percent(self):
        doc = fresh_doc()
        budget.record(doc, "openrouter", 19.0, None)
        self.assertFalse(budget.escalation_allowed(doc))
        self.assertTrue(budget.escalation_allowed(doc, release_critical=True))


class TestProviders(unittest.TestCase):
    def test_limit_error_opens_a_cooldown(self):
        doc = fresh_doc()
        before, after = providers.record_failure(doc, "claude", "429 rate limit exceeded",
                                                 900, TZ)
        self.assertEqual((before, after), ("AVAILABLE", "COOLDOWN"))
        self.assertIsNotNone(doc["providers"]["claude"]["cooldown_until"])

    def test_ordinary_errors_degrade_then_open_the_breaker(self):
        doc = fresh_doc()
        for _ in range(providers.DEGRADED_AFTER):
            providers.record_failure(doc, "codex", "connection reset", 900, TZ)
        self.assertEqual(doc["providers"]["codex"]["state"], "DEGRADED")
        for _ in range(providers.UNAVAILABLE_AFTER - providers.DEGRADED_AFTER):
            providers.record_failure(doc, "codex", "connection reset", 900, TZ)
        self.assertEqual(doc["providers"]["codex"]["state"], "UNAVAILABLE")

    def test_claude_cooldown_still_allows_review_and_merge(self):
        doc = fresh_doc()
        providers.record_failure(doc, "claude", "usage limit reached", 900, TZ)
        self.assertFalse(providers.may(doc, "new_builds"))
        self.assertTrue(providers.may(doc, "review"))
        self.assertTrue(providers.may(doc, "merge"))

    def test_codex_cooldown_allows_building_but_never_review(self):
        doc = fresh_doc()
        providers.record_failure(doc, "codex", "429 too many requests", 900, TZ)
        self.assertTrue(providers.may(doc, "new_builds"))
        self.assertFalse(providers.may(doc, "review"))
        self.assertFalse(providers.may(doc, "merge"))

    def test_grok_cooldown_does_not_stop_development(self):
        doc = fresh_doc()
        providers.record_failure(doc, "grok", "quota exceeded", 900, TZ)
        self.assertTrue(providers.may(doc, "new_builds"))
        self.assertTrue(providers.may(doc, "review"))
        self.assertFalse(providers.may(doc, "observation"))

    def test_two_development_providers_down_is_a_safe_hold(self):
        doc = fresh_doc()
        providers.record_failure(doc, "claude", "rate limit", 900, TZ)
        providers.record_failure(doc, "codex", "rate limit", 900, TZ)
        self.assertTrue(providers.safe_hold(doc))


class TestMigrationLock(unittest.TestCase):
    def test_single_writer_only(self):
        doc = fresh_doc()
        self.assertTrue(migration_lock.acquire(doc, "TASK-002", TZ))
        self.assertFalse(migration_lock.acquire(doc, "TASK-003", TZ))
        self.assertEqual(migration_lock.owner(doc), "TASK-002")
        self.assertIn("TASK-003", doc["migration_lock"]["waiters"])
        self.assertEqual(doc["counters"]["migration_lock_waits"], 1)

    def test_reacquire_by_the_owner_is_idempotent(self):
        doc = fresh_doc()
        migration_lock.acquire(doc, "TASK-002", TZ)
        self.assertTrue(migration_lock.acquire(doc, "TASK-002", TZ))

    def test_only_the_owner_can_release(self):
        doc = fresh_doc()
        migration_lock.acquire(doc, "TASK-002", TZ)
        self.assertFalse(migration_lock.release(doc, "TASK-003", "wrong owner"))
        self.assertTrue(migration_lock.release(doc, "TASK-002", "merged"))
        self.assertEqual(migration_lock.state(doc), migration_lock.FREE)


class TestClock(unittest.TestCase):
    def test_phase_boundaries(self):
        start = clock.parse("2026-09-24T10:00:00+12:00")
        cases = {0.0: "NORMAL", 15.9: "NORMAL", 16.0: "NO_OPTIONAL_WORK",
                 18.0: "FEATURE_FREEZE", 21.0: "STABILISATION_P0_P1",
                 23.0: "RELEASE_BLOCKERS_ONLY", 24.0: "FROZEN", 30.0: "FROZEN"}
        for hours, expected in cases.items():
            now = start + __import__("datetime").timedelta(hours=hours)
            self.assertEqual(clock.ClockState(start, now, 24).phase, expected, hours)

    def test_feature_work_stops_at_feature_freeze(self):
        self.assertTrue(clock.phase_allows("NORMAL", "feature"))
        self.assertFalse(clock.phase_allows("FEATURE_FREEZE", "feature"))
        self.assertTrue(clock.phase_allows("FEATURE_FREEZE", "stabilisation"))
        self.assertFalse(clock.phase_allows("RELEASE_BLOCKERS_ONLY", "stabilisation"))
        self.assertTrue(clock.phase_allows("RELEASE_BLOCKERS_ONLY", "release_blocker"))
        self.assertFalse(clock.phase_allows("FROZEN", "release_blocker"))

    def test_optional_work_stops_first(self):
        self.assertFalse(clock.phase_allows("NO_OPTIONAL_WORK", "optional"))
        self.assertTrue(clock.phase_allows("NO_OPTIONAL_WORK", "feature"))


class TestProcessLiveness(unittest.TestCase):
    """A zombie child keeps its PID entry, so `os.kill(pid, 0)` still succeeds.

    The watchdog trusted that signal and reported a killed supervisor as healthy.
    """

    def test_a_zombie_child_is_not_running(self):
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = zombie.pid
        for _ in range(200):
            status = Path(f"/proc/{pid}/status")
            if status.exists() and "State:\tZ" in status.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            zombie.wait(timeout=5)
            self.skipTest("could not observe a zombie on this platform")

        try:
            os.kill(pid, 0)
            signal_says_alive = True
        except (ProcessLookupError, PermissionError):
            signal_says_alive = False

        try:
            self.assertTrue(signal_says_alive,
                            "expected a zombie to still answer signal 0")
            self.assertFalse(proc.is_running(pid),
                             "a zombie must not count as a running process")
        finally:
            zombie.wait(timeout=5)

    def test_a_live_process_is_running(self):
        self.assertTrue(proc.is_running(os.getpid()))

    def test_absent_and_invalid_pids_are_not_running(self):
        self.assertFalse(proc.is_running(None))
        self.assertFalse(proc.is_running(0))
        self.assertFalse(proc.is_running(-1))


class TestRedaction(unittest.TestCase):
    def test_environment_secret_is_replaced(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-thisisafakevalueforthetest"
        try:
            text = "calling with sk-or-v1-thisisafakevalueforthetest now"
            scrubbed = redact.scrub(text)
            self.assertNotIn("thisisafakevalueforthetest", scrubbed)
            self.assertFalse(redact.contains_secret(scrubbed))
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)

    def test_webhook_url_shape_is_replaced_even_when_unknown(self):
        text = "posting to https://discord.com/api/webhooks/123456/abcDEF-ghi_jkl"
        self.assertNotIn("abcDEF", redact.scrub(text))

    def test_nested_structures_are_scrubbed(self):
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-0000-1111-2222-3333"
        try:
            payload = {"headers": {"auth": "sk-lf-0000-1111-2222-3333"},
                       "list": ["sk-lf-0000-1111-2222-3333"]}
            scrubbed = redact.scrub(payload)
            self.assertNotIn("0000-1111", str(scrubbed))
        finally:
            os.environ.pop("LANGFUSE_SECRET_KEY", None)

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(redact.scrub("mood check-in saved"), "mood check-in saved")


if __name__ == "__main__":
    unittest.main()
