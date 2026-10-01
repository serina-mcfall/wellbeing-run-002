"""Control-plane unit tests.

These cover the decisions that can silently go wrong and cost the experiment:
backpressure arithmetic, the merge gate, state transitions, budget thresholds,
the migration lock, deadline phases, review parsing and secret redaction.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# This directory too, for the shared merge-evidence fixture.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mergeable_evidence import complete_evidence  # noqa: E402

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


HEAD_SHA = "a" * 40


class TestMergeGate(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 7, "state": "OPEN", "isDraft": False,
            "headRefOid": HEAD_SHA,
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                                   "conclusion": "SUCCESS"}],
        }
        self.record = routing.blank_pr_record(7, "TASK-001", "task/task-001")
        self.record.update({"review_verdict": routing.REVIEW_PASS,
                            "approval_current": True,
                            "reviewed_head": HEAD_SHA,
                            "reviewed_diff_hash": "abc123"})
        # evaluate_merge requires the Protocol v2 evidence classes for the
        # observed head; without them this fixture cannot merge and the
        # "all gates satisfied" case would pass for the wrong reason.
        self.record.update(complete_evidence(HEAD_SHA))

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
            '"findings":[{"severity":"P1","category":"ACCESSIBILITY",'
            '"summary":"no focus style"}]}\n```'
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


def _review(verdict="REVIEW_PASS", gates=None, findings=None):
    payload = {"verdict": verdict, "gates": gates or {}, "findings": findings or []}
    return routing.parse_review("```json\n" + json.dumps(payload) + "\n```")


def _finding(severity="P2", category="TESTS", fid="F1", **extra):
    finding = {"id": fid, "severity": severity, "category": category}
    finding.update(extra)
    return finding


UI_PASS = {"OVERENGINEERING": "PASS", "COGNITIVE_LOAD": "PASS", "SENSORY_LOAD": "PASS"}


class TestGateVocabulary(unittest.TestCase):
    """KNOWN_GATES is a contract shared with prompts/reviewer.md. If the two
    drift, a reviewer can emit a category the code rejects, or grade a gate the
    code has never heard of. This catches a twelfth dimension being added to
    one without the other."""

    def _prompt(self) -> str:
        return (Path(__file__).resolve().parent.parent
                / "prompts" / "reviewer.md").read_text(encoding="utf-8")

    def test_known_gates_match_the_prompt_dimension_list(self):
        block = self._prompt().split("## Review dimensions")[1] \
                              .split("Specifically check for")[0]
        dims = tuple(t.strip() for t in re.split(r"[·\n]", block) if t.strip())
        self.assertEqual(routing.KNOWN_GATES, dims)

    def test_known_gates_match_the_prompt_json_example(self):
        gates = re.search(r'"gates":\s*\{(.*?)\}', self._prompt(), re.DOTALL).group(1)
        self.assertEqual(routing.KNOWN_GATES,
                         tuple(re.findall(r'"([A-Z_]+)"\s*:', gates)))

    def test_required_ui_gates_are_a_subset(self):
        self.assertTrue(set(routing.REQUIRED_UI_GATES) <= set(routing.KNOWN_GATES))

    def test_only_pass_and_fail_are_governed_gate_values(self):
        self.assertEqual(routing.GATE_VALUES, frozenset({"PASS", "FAIL"}))


class TestReviewParserContract(unittest.TestCase):
    """Parser-level guarantees, asserted directly rather than only through
    review_is_consistent."""

    def test_missing_severity_is_not_defaulted_to_p2(self):
        review = _review(findings=[{"id": "F1", "category": "TESTS"}])
        self.assertIsNone(review.findings[0].get("severity"))
        self.assertEqual(review.blocking, [])

    def test_blank_severity_is_preserved_as_invalid(self):
        review = _review(findings=[{"id": "F1", "severity": "  ", "category": "TESTS"}])
        self.assertEqual(review.findings[0]["severity"], "")
        self.assertNotIn(review.findings[0]["severity"], routing.SEVERITIES)

    def test_severity_is_normalised(self):
        review = _review(findings=[_finding(severity=" p1 ", category="TESTS")])
        self.assertEqual(review.findings[0]["severity"], "P1")

    def test_category_is_normalised(self):
        review = _review(findings=[_finding(category=" accessibility ")])
        self.assertEqual(review.findings[0]["category"], "ACCESSIBILITY")

    def test_absent_category_key_stays_absent(self):
        """So consumers calling .get('category', '') keep their own default."""
        review = _review(findings=[{"id": "F1", "severity": "P2"}])
        self.assertNotIn("category", review.findings[0])

    def test_ids_are_still_defaulted_positionally(self):
        review = _review(verdict="REVIEW_FAIL",
                         findings=[{"severity": "P1"}, {"severity": "P2"}])
        self.assertEqual([f["id"] for f in review.findings], ["F1", "F2"])


class TestC10GateAggregation(unittest.TestCase):
    """C-10.1: only P0/P1 independently block a REVIEW_PASS. A gate graded FAIL
    for a correctly-attributed P2/P3 no longer prevents a pass; everything
    malformed or unattributable still fails closed."""

    def assertConsistent(self, review):
        consistent, why = routing.review_is_consistent(review, touches_ui=True)
        self.assertTrue(consistent, f"expected consistent, got: {why}")

    def assertInconsistent(self, review, fragment):
        consistent, why = routing.review_is_consistent(review, touches_ui=True)
        self.assertFalse(consistent, "expected inconsistent")
        self.assertIn(fragment, why)
        return why

    # ---------------------------------------------- the contradiction, fixed
    def test_p2_backed_gate_fail_no_longer_blocks(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "ACCESSIBILITY": "FAIL"},
            findings=[_finding("P2", "ACCESSIBILITY")]))

    def test_p3_backed_gate_fail_no_longer_blocks(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "TESTS": "FAIL"},
            findings=[_finding("P3", "TESTS")]))

    def test_mixed_p2_and_p3_backing_one_gate_does_not_block(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "SCOPE": "FAIL"},
            findings=[_finding("P2", "SCOPE", "F1"), _finding("P3", "SCOPE", "F2")]))

    def test_two_gates_each_backed_by_non_blocking_findings(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "SCOPE": "FAIL", "TESTS": "FAIL"},
            findings=[_finding("P2", "SCOPE", "F1"), _finding("P3", "TESTS", "F2")]))

    # -------------------------------------------------- blocking still blocks
    def test_p0_backed_gate_fail_blocks(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "SECURITY": "FAIL"},
            findings=[_finding("P0", "SECURITY")]), "P0/P1")

    def test_p1_backed_gate_fail_blocks(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "ACCESSIBILITY": "FAIL"},
            findings=[_finding("P1", "ACCESSIBILITY")]), "P0/P1")

    def test_mixed_blocking_and_non_blocking_on_one_gate_blocks(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "TESTS": "FAIL"},
            findings=[_finding("P1", "TESTS", "F1"), _finding("P2", "TESTS", "F2")]),
            "P0/P1")

    def test_blocking_finding_blocks_even_with_every_gate_passing(self):
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[_finding("P1", "PRIVACY")]), "P0/P1")

    # --------------------------------------------------------- fail closed
    def test_unbacked_gate_fail_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "SCOPE": "FAIL"}), "no attributable finding")

    def test_gate_fail_backed_only_by_a_different_category_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "SCOPE": "FAIL"},
            findings=[_finding("P2", "TESTS")]), "no attributable finding")

    def test_unknown_gate_name_graded_fail_is_unbacked(self):
        """No finding may carry a category outside KNOWN_GATES, so a FAIL on an
        invented gate is caught by the attribution rule without needing one."""
        self.assertInconsistent(_review(
            gates={**UI_PASS, "VIBES": "FAIL"},
            findings=[_finding("P2", "TESTS")]), "gate VIBES FAIL")

    def test_missing_severity_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[{"id": "F1", "category": "TESTS"}]),
            "missing or unrecognised severity")

    def test_unknown_severity_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[_finding("CRITICAL", "TESTS")]),
            "missing or unrecognised severity")

    def test_missing_category_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[{"id": "F1", "severity": "P2"}]),
            "missing or unrecognised category")

    def test_unknown_category_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[_finding("P2", "VIBES")]),
            "missing or unrecognised category")

    def test_malformed_category_is_inconsistent_even_when_no_gate_fails(self):
        """Q3: malformed attribution must not enter a validated review at all,
        because an accepted P2/P3 may later become durable debt."""
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[_finding("P2", "NOT_A_DIMENSION")]),
            "missing or unrecognised category")

    def test_unknown_gate_value_is_inconsistent(self):
        self.assertInconsistent(_review(
            gates={**UI_PASS, "TESTS": "SKIPPED"}), "unrecognised gate value")

    def test_lowercase_gate_value_is_normalised_not_rejected(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "TESTS": "pass"}))

    def test_malformed_evidence_is_reported_before_it_is_interpreted(self):
        """A P1 with no category reports the category defect, not the severity
        one — malformed evidence is never read as a lesser finding."""
        self.assertInconsistent(_review(
            gates=UI_PASS, findings=[{"id": "F1", "severity": "P1"}]),
            "missing or unrecognised category")

    # -------------------------------------------------------- normalisation
    def test_category_matching_is_case_insensitive(self):
        self.assertConsistent(_review(
            gates={**UI_PASS, "ACCESSIBILITY": "FAIL"},
            findings=[_finding("p2", " accessibility ")]))

    # ------------------------------------------------- unchanged behaviours
    def test_ui_required_gate_presence_still_enforced(self):
        self.assertInconsistent(_review(gates={"SCOPE": "PASS"}),
                                "missing required gates")

    def test_non_ui_review_need_not_grade_the_ui_gates(self):
        review = _review(gates={"SCOPE": "PASS"})
        consistent, _ = routing.review_is_consistent(review, touches_ui=False)
        self.assertTrue(consistent)

    def test_review_fail_with_only_p2_p3_is_not_upgraded(self):
        review = _review(verdict="REVIEW_FAIL", gates={**UI_PASS, "TESTS": "FAIL"},
                         findings=[_finding("P2", "TESTS")])
        consistent, _ = routing.review_is_consistent(review, touches_ui=True)
        self.assertTrue(consistent, "consistency validation does not judge a FAIL")
        self.assertEqual(review.verdict, routing.REVIEW_FAIL)

    def test_review_fail_is_never_inspected_for_malformed_findings(self):
        review = _review(verdict="REVIEW_FAIL", findings=[{"id": "F1"}])
        consistent, _ = routing.review_is_consistent(review, touches_ui=True)
        self.assertTrue(consistent)
        self.assertEqual(review.verdict, routing.REVIEW_FAIL)

    def test_unparseable_is_still_not_a_pass(self):
        self.assertEqual(routing.parse_review("looks fine").verdict,
                         routing.REVIEW_UNPARSEABLE)

    # --------------------------------------------- RUN001-F1 reproduction pair
    def test_run001_f1_reproduction_is_resolved(self):
        """The exact shape C-10 exists to fix: a PASS verdict with one gate
        graded FAIL for a genuine, correctly-classified P2."""
        self.assertConsistent(_review(
            gates={"SCOPE": "PASS", "OVERENGINEERING": "FAIL",
                   "COGNITIVE_LOAD": "PASS", "SENSORY_LOAD": "PASS"},
            findings=[_finding("P2", "OVERENGINEERING", "F1",
                               summary="helper with a single caller")]))

    def test_run001_f1_fail_closed_counterpart(self):
        """Same failing gate, no finding accounting for it — still refused."""
        self.assertInconsistent(_review(
            gates={"SCOPE": "PASS", "OVERENGINEERING": "FAIL",
                   "COGNITIVE_LOAD": "PASS", "SENSORY_LOAD": "PASS"}),
            "gate OVERENGINEERING FAIL with no attributable finding")


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
