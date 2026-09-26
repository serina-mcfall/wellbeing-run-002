"""C-08b.2: ctl human-resolve executes governed, condition-specific effects.

The lifecycle mutation (C-08b.1) stays untouched; what this file pins is the
execution layer around it: which (condition_code, outcome) pairs are governed,
that a refused pair leaves the intervention and the task exactly as they were,
that each accepted effect is the smallest legal state mutation, and that the
RESOLVED ledger-repair retry never re-executes an effect.

Fixture style follows tests/test_intervention.py's CliCase: an isolated state
file and ledger, a stub config, no contact with .runtime.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    cli,
    clock,
    intervention,
    ledger as ledger_mod,
    migration_lock,
    routing,
    state,
)

TZ = "Pacific/Auckland"
PR = 7
MAX = 3

TYPE_FOR = {
    "repair_cycle_limit": "HUMAN_PRODUCT_DECISION",
    "dispatch_failure_limit": "HUMAN_APPARATUS_AUTHORISATION",
    "builder_attempts_exhausted": "HUMAN_PRODUCT_DECISION",
    "fixer_failed": "HUMAN_PRODUCT_DECISION",
    "review_unusable": "HUMAN_APPARATUS_AUTHORISATION",
    "p0_finding": "HUMAN_PRODUCT_DECISION",
    "red_guardrail": "HUMAN_GOVERNANCE_DECISION",
    "budget_hard_stop": "HUMAN_GOVERNANCE_DECISION",
    "supervisor_crash_loop": "HUMAN_APPARATUS_AUTHORISATION",
    "supervisor_restart_failed": "HUMAN_APPARATUS_AUTHORISATION",
    "merge_invariant_violation": "HUMAN_VERIFICATION",
    "worker_state_invariant": "HUMAN_VERIFICATION",
}

SYSTEMIC = {"red_guardrail", "budget_hard_stop", "supervisor_crash_loop",
            "supervisor_restart_failed"}


def _fake_config() -> types.SimpleNamespace:
    return types.SimpleNamespace(timezone=TZ, experiment_id="run-002",
                                 max_repair_cycles=MAX)


class ResolutionCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state_path = root / "state.json"
        self.ledger_path = root / "ledger.jsonl"
        for patcher in (
            mock.patch.object(cli.config, "STATE_PATH", self.state_path),
            mock.patch.object(cli.config, "LEDGER_PATH", self.ledger_path),
            mock.patch.object(cli.config, "load", _fake_config),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ------------------------------------------------------------- fixtures
    def seed(self, condition_code: str, *, task_state: str = "HUMAN_REQUIRED",
             repair_cycles: int = MAX, dispatch_failures: int = 0,
             review_verdict: str | None = routing.REVIEW_FAIL,
             attempts: int = 0, red_guardrail: bool = False,
             hard_stop: bool = False, lock_owner: bool = False) -> str:
        doc = state.initial_document("run-002", "2.0")
        if condition_code not in SYSTEMIC:
            task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
            task.update({"state": task_state, "branch": "task/task-001", "pr": PR})
            task["attempts"] = attempts
            record = routing.blank_pr_record(PR, "TASK-001", "task/task-001")
            record["repair_cycles"] = repair_cycles
            record["dispatch_failures"] = dispatch_failures
            record["review_verdict"] = review_verdict
            record["pending_findings"] = [{"id": "F1"}]
            doc["prs"][str(PR)] = record
            task_id = "TASK-001"
        else:
            task_id = None
        if red_guardrail:
            doc["red_guardrail"] = {"guardrail": "PRIVACY", "detail": "d", "at": "x"}
        if hard_stop:
            doc["budget"]["hard_stop"] = True
            doc["budget"]["thresholds_crossed"] = ["SOFT_75", "HARD_STOP"]
        if lock_owner:
            doc["migration_lock"].update({"state": "HELD", "owner_task": "TASK-001",
                                          "owner_pr": PR})
        record, _ = intervention.request(
            doc, type_=TYPE_FOR[condition_code],
            scope="systemic" if task_id is None else "task",
            task_id=task_id, reason="seeded", condition_code=condition_code, tz=TZ)
        intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return record["id"]

    def doc(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def task(self) -> dict:
        return self.doc()["tasks"]["TASK-001"]

    def pr(self) -> dict:
        return self.doc()["prs"][str(PR)]

    def stored(self, iid: str) -> dict:
        return self.doc()["interventions"][iid]

    def resolve(self, iid: str, outcome: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["human-resolve", iid, "--outcome", outcome,
                             "--by", "serina"])
        return code, buffer.getvalue()

    def assert_refused(self, iid: str, outcome: str, *, out_contains: str = "refusing"):
        before = self.doc()
        code, out = self.resolve(iid, outcome)
        self.assertEqual(code, 1)
        self.assertIn(out_contains, out)
        self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")
        self.assertEqual(self.doc(), before)


class TestS1Resolution(ResolutionCase):
    def test_retry_grants_exactly_one_attempt_via_max_minus_one(self):
        iid = self.seed("repair_cycle_limit")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FIX_REQUIRED")
        self.assertEqual(self.pr()["repair_cycles"], MAX - 1)
        self.assertEqual(self.stored(iid)["status"], "RESOLVED")

    def test_retry_never_touches_dispatch_failures(self):
        iid = self.seed("repair_cycle_limit", dispatch_failures=2)
        self.resolve(iid, "RETRY")
        self.assertEqual(self.pr()["dispatch_failures"], 2)

    def test_fail_moves_the_task_to_failed(self):
        iid = self.seed("repair_cycle_limit")
        code, _ = self.resolve(iid, "FAIL")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FAILED")

    def test_no_action_resolves_without_touching_the_task(self):
        iid = self.seed("repair_cycle_limit")
        code, _ = self.resolve(iid, "NO_ACTION")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "HUMAN_REQUIRED")
        self.assertEqual(self.pr()["repair_cycles"], MAX)

    def test_ungoverned_outcomes_are_refused_before_any_mutation(self):
        for outcome in ("RESUME", "CLEAR_GUARDRAIL", "FREEZE"):
            with self.subTest(outcome=outcome):
                iid = self.seed("repair_cycle_limit")
                self.assert_refused(iid, outcome)

    def test_retry_in_the_wrong_task_state_is_refused_whole(self):
        iid = self.seed("repair_cycle_limit", task_state="MERGED")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 1)
        self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")
        self.assertEqual(self.task()["state"], "MERGED")
        self.assertEqual(self.pr()["repair_cycles"], MAX)


class TestS2Resolution(ResolutionCase):
    def test_retry_resets_dispatch_failures_and_returns_to_routing(self):
        iid = self.seed("dispatch_failure_limit", dispatch_failures=MAX)
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "PR_OPEN")
        self.assertEqual(self.pr()["dispatch_failures"], 0)
        self.assertEqual(self.pr()["repair_cycles"], MAX)  # untouched


class TestS3Resolution(ResolutionCase):
    def test_retry_uses_the_documented_exception_exit_to_ready(self):
        iid = self.seed("builder_attempts_exhausted", task_state="FAILED",
                        attempts=MAX)
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "READY")
        history = [h["to"] for h in self.task()["history"]]
        self.assertEqual(history[-2:], ["HUMAN_REQUIRED", "READY"])

    def test_fail_is_refused_because_the_task_is_already_failed(self):
        iid = self.seed("builder_attempts_exhausted", task_state="FAILED")
        self.assert_refused(iid, "FAIL")


class TestS4S5S6Resolution(ResolutionCase):
    def test_fixer_failed_retry_returns_to_fix_required(self):
        iid = self.seed("fixer_failed")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FIX_REQUIRED")
        self.assertEqual(self.pr()["pending_findings"], [{"id": "F1"}])

    def test_review_unusable_retry_clears_only_the_stale_verdict(self):
        iid = self.seed("review_unusable")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "PR_OPEN")
        self.assertIsNone(self.pr()["review_verdict"])
        self.assertFalse(self.pr()["approval_current"])
        self.assertEqual(self.pr()["review_cycles"], 0)  # counter untouched

    def test_p0_retry_means_fresh_review(self):
        iid = self.seed("p0_finding")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "PR_OPEN")
        self.assertIsNone(self.pr()["review_verdict"])


class TestS7Resolution(ResolutionCase):
    def test_clear_guardrail_clears_only_the_state_field(self):
        iid = self.seed("red_guardrail", red_guardrail=True)
        code, _ = self.resolve(iid, "CLEAR_GUARDRAIL")
        self.assertEqual(code, 0)
        self.assertIsNone(self.doc()["red_guardrail"])
        self.assertEqual(self.stored(iid)["status"], "RESOLVED")

    def test_no_action_leaves_the_guardrail_raised(self):
        iid = self.seed("red_guardrail", red_guardrail=True)
        self.resolve(iid, "NO_ACTION")
        self.assertTrue(self.doc()["red_guardrail"])

    def test_freeze_is_refused(self):
        iid = self.seed("red_guardrail", red_guardrail=True)
        self.assert_refused(iid, "FREEZE")


class TestS8Resolution(ResolutionCase):
    def test_no_action_resolves_and_budget_stays_fail_closed(self):
        iid = self.seed("budget_hard_stop", hard_stop=True)
        code, _ = self.resolve(iid, "NO_ACTION")
        self.assertEqual(code, 0)
        self.assertTrue(self.doc()["budget"]["hard_stop"])
        self.assertEqual(self.doc()["budget"]["thresholds_crossed"],
                         ["SOFT_75", "HARD_STOP"])

    def test_resume_is_refused_and_budget_untouched(self):
        iid = self.seed("budget_hard_stop", hard_stop=True)
        # The refusal must come from the governance table itself, not fall out
        # of a downstream accident - RESUME is simply not governed for S8.
        self.assert_refused(iid, "RESUME", out_contains="not governed")
        self.assertTrue(self.doc()["budget"]["hard_stop"])


class TestS9S10Resolution(ResolutionCase):
    def test_only_no_action_is_governed(self):
        for condition in ("supervisor_crash_loop", "supervisor_restart_failed"):
            with self.subTest(condition=condition):
                iid = self.seed(condition)
                self.assert_refused(iid, "RETRY")
                code, _ = self.resolve(iid, "NO_ACTION")
                self.assertEqual(code, 0)


class TestS11Resolution(ResolutionCase):
    def test_no_action_resolves_without_repair(self):
        iid = self.seed("merge_invariant_violation", task_state="FROZEN")
        code, _ = self.resolve(iid, "NO_ACTION")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FROZEN")

    def test_fail_from_frozen_uses_the_legal_two_step_path(self):
        iid = self.seed("merge_invariant_violation", task_state="FROZEN")
        code, _ = self.resolve(iid, "FAIL")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FAILED")
        history = [h["to"] for h in self.task()["history"]]
        self.assertEqual(history[-2:], ["HUMAN_REQUIRED", "FAILED"])

    def test_fail_never_falsifies_merged_or_complete_work(self):
        for terminal in ("MERGED", "COMPLETE"):
            with self.subTest(state=terminal):
                iid = self.seed("merge_invariant_violation", task_state=terminal)
                code, out = self.resolve(iid, "FAIL")
                self.assertEqual(code, 1)
                # The explicit falsification guard must refuse, not merely the
                # transition table happening to lack an edge.
                self.assertIn("falsify", out)
                self.assertEqual(self.task()["state"], terminal)
                self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")

    def test_retry_and_resume_are_refused_before_c14_3(self):
        iid = self.seed("merge_invariant_violation", task_state="FROZEN")
        self.assert_refused(iid, "RETRY")
        self.assert_refused(iid, "RESUME")


class TestS12Resolution(ResolutionCase):
    def test_no_action_means_deliberately_left_frozen_for_the_run(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN")
        code, _ = self.resolve(iid, "NO_ACTION")
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        self.assertEqual(intervention.simultaneous_open_count(doc), 0)

    def test_fail_uses_frozen_human_required_failed(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN")
        code, _ = self.resolve(iid, "FAIL")
        self.assertEqual(code, 0)
        self.assertEqual(self.task()["state"], "FAILED")

    def test_retry_and_resume_are_refused(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN")
        self.assert_refused(iid, "RETRY")
        self.assert_refused(iid, "RESUME")


class TestMigrationLock(ResolutionCase):
    def test_human_fail_releases_a_lock_the_task_owns(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN",
                        lock_owner=True)
        code, _ = self.resolve(iid, "FAIL")
        self.assertEqual(code, 0)
        lock = self.doc()["migration_lock"]
        self.assertEqual(lock["state"], "FREE")
        self.assertIsNone(lock["owner_task"])
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertIn("MIGRATION_LOCK_RELEASED",
                      [e["event_type"] for e in events])

    def test_lock_release_evidence_survives_a_failed_append_and_repair_rerun(self):
        """Review point 4: state commits (task FAILED, lock FREE, intervention
        RESOLVED), then the MIGRATION_LOCK_RELEASED append fails. The rerun of
        the identical command must restore the missed evidence WITHOUT
        re-executing any state effect."""
        iid = self.seed("worker_state_invariant", task_state="FROZEN",
                        lock_owner=True)
        real_append = ledger_mod.Ledger.append

        def refuse_lock_event(ledger_self, event_type, **fields):
            if event_type == "MIGRATION_LOCK_RELEASED":
                raise OSError("no space left on device")
            return real_append(ledger_self, event_type, **fields)

        with mock.patch.object(ledger_mod.Ledger, "append", refuse_lock_event):
            first, out = self.resolve(iid, "FAIL")
        self.assertEqual(first, 1)
        self.assertIn("LEDGER EVIDENCE FAILED", out)
        # State effects are all durable already...
        self.assertEqual(self.task()["state"], "FAILED")
        self.assertEqual(self.doc()["migration_lock"]["state"], "FREE")
        self.assertEqual(self.stored(iid)["status"], "RESOLVED")
        # ...but the lock-release evidence is missing from the ledger.
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertNotIn("MIGRATION_LOCK_RELEASED",
                         [e["event_type"] for e in events])

        history_before = list(self.task()["history"])
        second, _ = self.resolve(iid, "FAIL")
        self.assertEqual(second, 0)
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        types_ = [e["event_type"] for e in events]
        self.assertEqual(types_.count("MIGRATION_LOCK_RELEASED"), 1)
        self.assertEqual(types_.count("HUMAN_INTERVENTION_RESOLVED"), 1)
        # No state effect ran twice: history and lock are exactly as committed.
        self.assertEqual(self.task()["history"], history_before)
        self.assertEqual(self.doc()["migration_lock"]["state"], "FREE")

    def test_repair_survives_unrelated_lock_activity_before_the_rerun(self):
        """Adversarial durability case: the lock's last_release_reason is
        mutable shared state, so a different task legitimately acquiring and
        releasing the lock between the failed append and the rerun must not
        destroy the repair evidence. The evidence has to belong to the
        intervention record itself."""
        iid = self.seed("worker_state_invariant", task_state="FROZEN",
                        lock_owner=True)
        real_append = ledger_mod.Ledger.append

        def refuse_lock_event(ledger_self, event_type, **fields):
            if event_type == "MIGRATION_LOCK_RELEASED":
                raise OSError("no space left on device")
            return real_append(ledger_self, event_type, **fields)

        with mock.patch.object(ledger_mod.Ledger, "append", refuse_lock_event):
            first, _ = self.resolve(iid, "FAIL")
        self.assertEqual(first, 1)

        # A different task acquires and releases the lock through the real
        # lock functions, overwriting last_release_reason.
        doc = self.doc()
        self.assertTrue(migration_lock.acquire(doc, "TASK-002", TZ))
        self.assertTrue(migration_lock.release(doc, "TASK-002", "migration merged"))
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        history_before = list(self.task()["history"])
        second, _ = self.resolve(iid, "FAIL")
        self.assertEqual(second, 0)
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        lock_events = [e for e in events
                       if e["event_type"] == "MIGRATION_LOCK_RELEASED"]
        # The original evidence is reconstructed exactly once, for the
        # original intervention, despite the intervening lock activity.
        self.assertEqual(len(lock_events), 1)
        self.assertEqual(lock_events[0]["metadata_redacted"]["intervention_id"],
                         iid)
        self.assertEqual(
            len([e for e in events
                 if e["event_type"] == "HUMAN_INTERVENTION_RESOLVED"]), 1)
        # No state effect re-executed, and the unrelated lock activity's own
        # record is untouched by the repair.
        self.assertEqual(self.task()["history"], history_before)
        lock = self.doc()["migration_lock"]
        self.assertEqual(lock["state"], "FREE")
        self.assertEqual(lock["last_release_reason"], "migration merged")

    def test_repair_rerun_with_both_events_present_appends_nothing(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN",
                        lock_owner=True)
        first, _ = self.resolve(iid, "FAIL")
        self.assertEqual(first, 0)
        second, out = self.resolve(iid, "FAIL")
        self.assertEqual(second, 0)
        self.assertIn("already resolved", out)
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        types_ = [e["event_type"] for e in events]
        self.assertEqual(types_.count("MIGRATION_LOCK_RELEASED"), 1)
        self.assertEqual(types_.count("HUMAN_INTERVENTION_RESOLVED"), 1)

    def test_a_lock_owned_elsewhere_is_never_touched(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN")
        before = self.doc()["migration_lock"]
        with_owner = dict(before, state="HELD", owner_task="TASK-999")
        doc = self.doc()
        doc["migration_lock"] = with_owner
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        code, _ = self.resolve(iid, "FAIL")
        self.assertEqual(code, 0)
        self.assertEqual(self.doc()["migration_lock"]["owner_task"], "TASK-999")


class TestGovernanceBoundaries(ResolutionCase):
    def test_unknown_condition_codes_permit_no_action_only(self):
        doc = state.initial_document("run-002", "2.0")
        record, _ = intervention.request(
            doc, type_="HUMAN_PRIVILEGED_ACTION", scope="task", task_id="T-01",
            reason="seeded", condition_code="gh_auth_expired", tz=TZ)
        intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        code, out = self.resolve(record["id"], "RETRY")
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)
        code, _ = self.resolve(record["id"], "NO_ACTION")
        self.assertEqual(code, 0)

    def test_refusal_happens_before_the_lifecycle_mutation(self):
        iid = self.seed("budget_hard_stop", hard_stop=True)
        self.resolve(iid, "RESUME")
        rec = self.stored(iid)
        self.assertEqual(rec["status"], "ACKNOWLEDGED")
        self.assertIsNone(rec["resolved_at"])
        self.assertIsNone(rec["resolution"])

    def test_resolved_repair_retry_never_re_executes_effects(self):
        iid = self.seed("repair_cycle_limit")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.pr()["repair_cycles"], MAX - 1)
        # The effect already ran; move the world on, then replay the command.
        doc = self.doc()
        doc["prs"][str(PR)]["repair_cycles"] = MAX
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.pr()["repair_cycles"], MAX)
        self.assertEqual(self.task()["state"], "REVIEW")

    def test_resolution_ledger_event_carries_the_outcome(self):
        iid = self.seed("worker_state_invariant", task_state="FROZEN")
        self.resolve(iid, "NO_ACTION")
        events = [json.loads(line) for line in
                  self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
        resolved = [e for e in events
                    if e["event_type"] == "HUMAN_INTERVENTION_RESOLVED"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["outcome"], "NO_ACTION")


if __name__ == "__main__":
    unittest.main()
