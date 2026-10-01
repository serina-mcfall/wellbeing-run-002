"""C-08b.2: production escalation sites feed the C-08b.1 intervention lifecycle.

Supervisor sites S1-S8 must create a durable OPEN intervention with the accepted
taxonomy type and a structurally safe reason, append HUMAN_INTERVENTION_REQUESTED
once, and carry the intervention identity in the one existing HUMAN_REQUIRED
notification. A deduplicated recurrence must not re-log or re-notify. The
Watchdog's systemic escalations S9/S10 must record the obligation without ever
suppressing the existing safety alert.

Style follows tests/test_fixer_dispatch.py for the Supervisor (mocked log and
notify_out over a real document) and tests/test_watchdog_reconcile.py for the
Watchdog (a real Store and Ledger in a temp directory).
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

from control import (  # noqa: E402
    budget,
    clock,
    config,
    intervention,
    ledger as ledger_mod,
    notify,
    providers,
    routing,
    state,
    supervisor as supervisor_mod,
    watchdog,
)

TZ = "Pacific/Auckland"
PR = 3
BRANCH = "task/task-001"

FINDINGS = [
    {"id": "F1", "severity": "P1", "category": "SECURITY", "summary": "one"},
]


def base_doc(task_state: str = "REVIEW", *, repair_cycles: int = 0,
             attempts: int = 0, review_cycles: int = 0) -> dict:
    doc = state.initial_document("run-002", "v2.0")
    providers.ensure(doc)
    budget.ensure(doc, 25.0)
    task = state.add_task(doc, "TASK-001", "Foundation", [], "feature", False, TZ)
    task["state"] = task_state
    task["branch"] = BRANCH
    task["pr"] = PR
    task["attempts"] = attempts
    task["last_progress_at"] = clock.iso(clock.now(TZ))
    record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
    record["review_verdict"] = routing.REVIEW_FAIL
    record["review_cycles"] = review_cycles
    record["repair_cycles"] = repair_cycles
    record["pending_findings"] = list(FINDINGS)
    doc["prs"][str(PR)] = record
    return doc


def open_records(doc: dict) -> list[dict]:
    return intervention.open_interventions(doc)


class SupervisorSiteCase(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify") as notify_stub, \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            notify_stub.HUMAN_REQUIRED = notify.HUMAN_REQUIRED
            notify_stub.ATTENTION = notify.ATTENTION
            notify_stub.INFO = notify.INFO
            notify_stub.CRITICAL = notify.CRITICAL
            self.sup = supervisor_mod.Supervisor(self.cfg)
        self.events: list[tuple] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    # ------------------------------------------------------------- helpers
    def requested_events(self):
        return [k for e, k in self.events if e == "HUMAN_INTERVENTION_REQUESTED"]

    def notified_bodies(self):
        return [c.args[3] if len(c.args) > 3 else c.kwargs.get("body", "")
                for c in self.sup.notify_out.call_args_list]

    def assert_single_open(self, doc, *, type_, condition_code, task_id="TASK-001",
                           scope="task"):
        records = open_records(doc)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["status"], "OPEN")
        self.assertEqual(rec["type"], type_)
        self.assertEqual(rec["scope"], scope)
        self.assertEqual(rec["task_id"], task_id)
        self.assertEqual(rec["condition_code"], condition_code)
        self.assertFalse(rec["reason_withheld"])
        requested = self.requested_events()
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["metadata_redacted"]["intervention_id"], rec["id"])
        # EXACTLY one notification total for a new escalation - the enriched
        # HUMAN_REQUIRED one. A second call of any severity is a duplicate.
        self.assertEqual(self.sup.notify_out.call_count, 1)
        only = self.sup.notify_out.call_args
        self.assertEqual(only.args[1], notify.HUMAN_REQUIRED)
        self.assertIn(rec["id"], only.args[3])
        return rec

    def use_real_notify(self):
        """Swap the mocked notify_out for the real method (Notifier mocked),
        so counters['human_interventions'] behaviour is genuinely exercised."""
        self.sup.notifier = mock.Mock()
        self.sup.notifier.send.return_value = {"ok": True}
        self.sup.notify_out = supervisor_mod.Supervisor.notify_out.__get__(self.sup)

    def fire_s1(self, doc):
        self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR,
                                doc["prs"][str(PR)]["pending_findings"])

    def dispatch_ok(self, doc):
        """dispatch_fixer with the worker layer succeeding."""
        with mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                               return_value=(Path("/tmp/wt"), "")), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=mock.Mock(ok=True, stderr="")), \
                mock.patch.object(supervisor_mod.prompts, "fixer", return_value="p"), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")), \
                mock.patch.object(supervisor_mod.state_mod, "new_worker_record",
                                  return_value={"role": "fixer", "pr": PR}):
            self.fire_s1(doc)


class TestS1RepairCycleLimit(SupervisorSiteCase):
    def test_limit_creates_a_product_decision_intervention(self):
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles)
        self.fire_s1(doc)
        rec = self.assert_single_open(doc, type_="HUMAN_PRODUCT_DECISION",
                                      condition_code="repair_cycle_limit")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertEqual(
            rec["reason"],
            f"Repair cycle limit reached for TASK-001 PR #{PR} after "
            f"{self.cfg.max_repair_cycles} repair cycles")

    def test_recurrence_deduplicates_event_and_notification(self):
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles)
        self.fire_s1(doc)
        self.fire_s1(doc)
        self.assertEqual(len(open_records(doc)), 1)
        self.assertEqual(len(self.requested_events()), 1)
        self.assertEqual(self.sup.notify_out.call_count, 1)

    def test_open_count_reflects_the_new_obligation(self):
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles)
        self.assertEqual(intervention.simultaneous_open_count(doc), 0)
        self.fire_s1(doc)
        self.assertEqual(intervention.simultaneous_open_count(doc), 1)

    def test_counter_increments_exactly_once_even_on_recurrence(self):
        self.use_real_notify()
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles)
        self.fire_s1(doc)
        self.assertEqual(doc["counters"]["human_interventions"], 1)
        self.fire_s1(doc)   # deduped recurrence: no second notification
        self.assertEqual(doc["counters"]["human_interventions"], 1)
        # C-18 stage 2: notify_out queues a durable intent and the drain
        # delivers it later, so "no second notification" is now counted at
        # the queue. Exactly one intent means exactly one send can ever
        # happen - the same claim, checked one step earlier.
        self.assertEqual(len(notify.queue(doc)), 1)
        self.assertEqual(self.sup.notifier.send.call_count, 0)


class TestS1CounterSemantics(SupervisorSiteCase):
    """The accepted RETRY mutation is max_repair_cycles - 1, never zero."""

    def test_zero_reset_would_grant_a_full_replenishment(self):
        doc = base_doc(repair_cycles=0)
        dispatched = 0
        for _ in range(self.cfg.max_repair_cycles + 1):
            before = doc["prs"][str(PR)]["repair_cycles"]
            doc["tasks"]["TASK-001"]["state"] = "REVIEW"
            self.dispatch_ok(doc)
            if doc["prs"][str(PR)]["repair_cycles"] == before + 1:
                dispatched += 1
        self.assertEqual(dispatched, self.cfg.max_repair_cycles)

    def test_max_minus_one_grants_exactly_one_fixer_dispatch(self):
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles - 1)
        self.dispatch_ok(doc)
        self.assertEqual(doc["prs"][str(PR)]["repair_cycles"],
                         self.cfg.max_repair_cycles)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FIX_REQUIRED")
        # The one granted attempt is spent; the next dispatch escalates again.
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        self.dispatch_ok(doc)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")

    def test_next_exhaustion_after_resolution_opens_a_new_intervention(self):
        doc = base_doc(repair_cycles=self.cfg.max_repair_cycles)
        self.fire_s1(doc)
        first = open_records(doc)[0]
        intervention.acknowledge(doc, first["id"], by="serina", tz=TZ)
        intervention.resolve(doc, first["id"], outcome="RETRY", by="serina", tz=TZ)
        # The accepted RETRY effect, then the granted attempt fails again.
        doc["prs"][str(PR)]["repair_cycles"] = self.cfg.max_repair_cycles - 1
        doc["tasks"]["TASK-001"]["state"] = "FIX_REQUIRED"
        self.dispatch_ok(doc)
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        self.fire_s1(doc)
        records = open_records(doc)
        self.assertEqual(len(records), 1)
        self.assertNotEqual(records[0]["id"], first["id"])


class TestS2DispatchFailureLimit(SupervisorSiteCase):
    def fire(self, doc, why="worker did not start: RAWSTDERR boom"):
        self.sup.on_dispatch_failure(doc, doc["tasks"]["TASK-001"], PR, "fixer", why)

    def test_limit_creates_an_apparatus_intervention(self):
        doc = base_doc()
        doc["prs"][str(PR)]["dispatch_failures"] = self.cfg.max_repair_cycles - 1
        self.fire(doc)
        rec = self.assert_single_open(doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                                      condition_code="dispatch_failure_limit")
        self.assertEqual(
            rec["reason"],
            f"fixer dispatch failed {self.cfg.max_repair_cycles} times for "
            f"TASK-001 PR #{PR}")

    def test_free_text_failure_reason_never_enters_the_intervention(self):
        doc = base_doc()
        doc["prs"][str(PR)]["dispatch_failures"] = self.cfg.max_repair_cycles - 1
        self.fire(doc, why="worker did not start: RAWSTDERR /very/private/path")
        self.assertNotIn("RAWSTDERR", open_records(doc)[0]["reason"])

    def test_below_the_limit_creates_no_intervention(self):
        doc = base_doc()
        self.fire(doc)
        self.assertEqual(open_records(doc), [])

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc()
        doc["prs"][str(PR)]["dispatch_failures"] = self.cfg.max_repair_cycles - 1
        self.fire(doc)
        self.assertEqual(doc["counters"]["human_interventions"], 1)


class TestS3BuilderAttemptsExhausted(SupervisorSiteCase):
    def test_exhaustion_creates_a_product_decision_and_leaves_the_task_failed(self):
        doc = base_doc("ACTIVE", attempts=self.cfg.max_repair_cycles)
        doc["workers"]["task-001-builder"] = {"role": "builder", "task": "TASK-001"}
        self.sup.on_builder_finished(doc, "task-001-builder",
                                     {"task_id": "TASK-001"}, {"outcome": "FAILED"})
        rec = self.assert_single_open(doc, type_="HUMAN_PRODUCT_DECISION",
                                      condition_code="builder_attempts_exhausted")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FAILED")
        self.assertEqual(
            rec["reason"],
            f"Builder attempts exhausted for TASK-001 after "
            f"{self.cfg.max_repair_cycles} attempts")

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc("ACTIVE", attempts=self.cfg.max_repair_cycles)
        self.sup.on_builder_finished(doc, "task-001-builder",
                                     {"task_id": "TASK-001"}, {"outcome": "FAILED"})
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_a_retryable_failure_creates_no_intervention(self):
        doc = base_doc("ACTIVE", attempts=1)
        self.sup.on_builder_finished(doc, "task-001-builder",
                                     {"task_id": "TASK-001"}, {"outcome": "FAILED"})
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "READY")
        self.assertEqual(open_records(doc), [])


class TestS4FixerFailed(SupervisorSiteCase):
    def test_fixer_failure_creates_a_product_decision(self):
        doc = base_doc("FIX_REQUIRED")
        self.sup.on_fixer_finished(doc, "task-001-fixer-1",
                                   {"task_id": "TASK-001", "pr": PR},
                                   {"outcome": "FAILED"})
        rec = self.assert_single_open(doc, type_="HUMAN_PRODUCT_DECISION",
                                      condition_code="fixer_failed")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertEqual(rec["reason"], f"Fixer failed for TASK-001 PR #{PR}")

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc("FIX_REQUIRED")
        self.sup.on_fixer_finished(doc, "task-001-fixer-1",
                                   {"task_id": "TASK-001", "pr": PR},
                                   {"outcome": "FAILED"})
        self.assertEqual(doc["counters"]["human_interventions"], 1)


class ReviewerFinishedCase(SupervisorSiteCase):
    def finish_review(self, doc, review):
        with mock.patch.object(supervisor_mod.workers, "worker_output",
                               return_value="irrelevant"), \
                mock.patch.object(supervisor_mod.routing, "parse_review",
                                  return_value=review), \
                mock.patch.object(supervisor_mod.routing, "touches_ui",
                                  return_value=False), \
                mock.patch.object(self.sup, "release_review_worktree"), \
                mock.patch.object(supervisor_mod.providers, "record_success"):
            self.sup.on_reviewer_finished(doc, "task-001-review-1",
                                          {"task_id": "TASK-001", "pr": PR},
                                          {"outcome": "SUCCESS"})


class TestS5ReviewUnusable(ReviewerFinishedCase):
    def unparseable(self):
        return routing.parse_review("no verdict here at all")

    def test_unusable_at_the_cycle_limit_creates_an_apparatus_intervention(self):
        doc = base_doc(review_cycles=self.cfg.max_repair_cycles)
        self.finish_review(doc, self.unparseable())
        rec = self.assert_single_open(doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                                      condition_code="review_unusable")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertEqual(
            rec["reason"],
            f"Review unusable for TASK-001 PR #{PR} after "
            f"{self.cfg.max_repair_cycles} review cycles")

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc(review_cycles=self.cfg.max_repair_cycles)
        self.finish_review(doc, self.unparseable())
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_below_the_limit_re_reviews_without_an_intervention(self):
        doc = base_doc(review_cycles=1)
        self.finish_review(doc, self.unparseable())
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")
        self.assertEqual(open_records(doc), [])


class TestS6P0Finding(ReviewerFinishedCase):
    def p0_review(self):
        return SimpleNamespace(
            verdict=routing.REVIEW_FAIL,
            critical=[{"id": "F9", "severity": "P0", "summary": "data loss"}],
            blocking=[], findings=[], as_dict=lambda: {"verdict": "REVIEW_FAIL"})

    def test_p0_creates_a_product_decision(self):
        doc = base_doc(review_cycles=1)
        with mock.patch.object(supervisor_mod.routing, "review_is_consistent",
                               return_value=(True, "")):
            self.finish_review(doc, self.p0_review())
        rec = self.assert_single_open(doc, type_="HUMAN_PRODUCT_DECISION",
                                      condition_code="p0_finding")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertEqual(
            rec["reason"],
            f"P0 finding requires a human product decision for TASK-001 PR #{PR}")
        # Reviewer prose stays in the notification, never in the durable reason.
        self.assertNotIn("data loss", rec["reason"])

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc(review_cycles=1)
        with mock.patch.object(supervisor_mod.routing, "review_is_consistent",
                               return_value=(True, "")):
            self.finish_review(doc, self.p0_review())
        self.assertEqual(doc["counters"]["human_interventions"], 1)


class TestS7RedGuardrail(SupervisorSiteCase):
    def test_raise_red_records_a_systemic_governance_intervention(self):
        doc = base_doc()
        self.sup.raise_red(doc, "PRIVACY", "free-text detail stays out of the reason")
        rec = open_records(doc)[0]
        self.assertEqual(rec["type"], "HUMAN_GOVERNANCE_DECISION")
        self.assertEqual(rec["scope"], "systemic")
        self.assertIsNone(rec["task_id"])
        self.assertEqual(rec["condition_code"], "red_guardrail")
        # Constant text: the guardrail name is caller-supplied free text and
        # must never enter the durable reason.
        self.assertEqual(rec["reason"],
                         "Experiment-wide RED guardrail requires a human "
                         "governance decision")
        self.assertNotIn("PRIVACY", rec["reason"])
        self.assertEqual(len(self.requested_events()), 1)
        self.assertTrue(doc["red_guardrail"])
        # Exactly one RED alert for one invocation - never two.
        self.assertEqual(self.sup.notify_out.call_count, 1)
        self.assertIn(rec["id"], self.sup.notify_out.call_args.args[3])

    def test_a_second_red_still_alerts_but_reuses_the_open_intervention(self):
        doc = base_doc()
        self.sup.raise_red(doc, "PRIVACY", "first")
        self.sup.raise_red(doc, "SECURITY", "second")
        self.assertEqual(len(open_records(doc)), 1)
        self.assertEqual(len(self.requested_events()), 1)
        # One RED alert per invocation - unconditional, never doubled and
        # never rate-limited by intervention dedup.
        self.assertEqual(self.sup.notify_out.call_count, 2)

    def test_counter_follows_the_unconditional_alert(self):
        """Pre-existing semantics: every RED HUMAN_REQUIRED alert increments
        the lifetime counter, once per invocation, even when the intervention
        record is deduplicated."""
        self.use_real_notify()
        doc = base_doc()
        self.sup.raise_red(doc, "PRIVACY", "first")
        self.assertEqual(doc["counters"]["human_interventions"], 1)
        self.sup.raise_red(doc, "SECURITY", "second")
        self.assertEqual(doc["counters"]["human_interventions"], 2)
        self.assertEqual(len(open_records(doc)), 1)
        self.assertEqual(len(self.requested_events()), 1)


class TestS8BudgetHardStop(SupervisorSiteCase):
    def test_hard_stop_records_a_systemic_governance_intervention(self):
        doc = base_doc()
        doc["budget"]["hard_stop"] = True
        self.sup.on_budget_threshold(doc, "HARD_STOP")
        rec = self.assert_single_open(doc, type_="HUMAN_GOVERNANCE_DECISION",
                                      condition_code="budget_hard_stop",
                                      task_id=None, scope="systemic")
        self.assertEqual(
            rec["reason"],
            "Metered budget hard stop reached; further paid model calls require "
            "a human governance decision")
        # Recording the obligation never touches the fail-closed budget state.
        self.assertTrue(doc["budget"]["hard_stop"])

    def test_counter_increments_exactly_once(self):
        self.use_real_notify()
        doc = base_doc()
        doc["budget"]["hard_stop"] = True
        self.sup.on_budget_threshold(doc, "HARD_STOP")
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_soft_thresholds_create_no_intervention(self):
        doc = base_doc()
        self.sup.on_budget_threshold(doc, "SOFT_75")
        self.assertEqual(open_records(doc), [])


class TestSecretBackstop(SupervisorSiteCase):
    CANARY = "sk-canary0000000000000000"

    def test_secret_shaped_reason_is_withheld_but_the_intervention_survives(self):
        doc = base_doc()
        record, is_new = intervention.request(
            doc, type_="HUMAN_GOVERNANCE_DECISION", scope="systemic", task_id=None,
            reason=f"impossible reason carrying {self.CANARY}",
            condition_code="red_guardrail", tz=TZ)
        self.assertTrue(is_new)
        self.assertTrue(record["reason_withheld"])
        self.assertEqual(record["reason"], intervention.WITHHELD_REASON)
        self.assertNotIn(self.CANARY, json.dumps(doc))

    def test_helper_keeps_the_canary_out_of_ledger_and_notification(self):
        doc = base_doc()
        self.sup.request_intervention(
            doc, type_="HUMAN_GOVERNANCE_DECISION", condition_code="red_guardrail",
            reason=f"impossible reason carrying {self.CANARY}",
            title="RED guardrail: TEST", body="fixed body")
        rec = open_records(doc)[0]
        self.assertTrue(rec["reason_withheld"])
        requested = self.requested_events()[0]
        self.assertNotIn(self.CANARY, json.dumps(requested, default=str))
        self.assertTrue(requested["metadata_redacted"]["reason_withheld"])
        for call in self.sup.notify_out.call_args_list:
            self.assertNotIn(self.CANARY, json.dumps(list(call.args), default=str))


# --------------------------------------------------------------- S9/S10


class LedgerRefusingRequested:
    """A real ledger that fails exactly the HUMAN_INTERVENTION_REQUESTED append."""

    def __init__(self, inner):
        self.inner = inner

    def append(self, event_type, **fields):
        if event_type == "HUMAN_INTERVENTION_REQUESTED":
            raise OSError("no space left on device")
        return self.inner.append(event_type, **fields)


class WatchdogEscalationCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = state.Store(path=root / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.notifier = mock.Mock(spec=notify.Notifier)
        self.cfg = SimpleNamespace(timezone=TZ)

    def ledger_events(self, event_type=None):
        out = []
        for line in Path(self.ledger.path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                if event_type is None or event.get("event_type") == event_type:
                    out.append(event)
        return out

    def crash_loop(self, **kw):
        watchdog.escalate_crash_loop(self.cfg, self.ledger, self.notifier, 3,
                                     store=kw.pop("store", self.store), **kw)

    def restart_failed(self, **kw):
        watchdog.escalate_restart_failed(self.cfg, self.ledger, self.notifier, None,
                                         store=kw.pop("store", self.store), **kw)


class TestS9CrashLoop(WatchdogEscalationCase):
    def test_records_intervention_then_event_then_alert(self):
        self.crash_loop()
        doc = self.store.read()
        records = list(doc["interventions"].values())
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["type"], "HUMAN_APPARATUS_AUTHORISATION")
        self.assertEqual(rec["scope"], "systemic")
        self.assertEqual(rec["condition_code"], "supervisor_crash_loop")
        requested = self.ledger_events("HUMAN_INTERVENTION_REQUESTED")
        self.assertEqual(len(requested), 1)
        crash = self.ledger_events("SUPERVISOR_CRASH_LOOP")
        self.assertEqual(len(crash), 1)
        # The bookkeeping outcome rides on the EXISTING failure event.
        self.assertEqual(crash[0]["metadata_redacted"]["intervention_bookkeeping"],
                         "OK")
        self.assertEqual(crash[0]["metadata_redacted"]["intervention_id"], rec["id"])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.notifier.send.call_args.args[0], notify.HUMAN_REQUIRED)
        self.assertIn(rec["id"], self.notifier.send.call_args.args[2])

    def test_state_absent_still_alerts(self):
        absent = state.Store(path=Path(self.tmp_path()) / "missing.json", tz=TZ)
        self.crash_loop(store=absent)
        crash = self.ledger_events("SUPERVISOR_CRASH_LOOP")
        self.assertEqual(len(crash), 1)
        meta = crash[0]["metadata_redacted"]
        self.assertEqual(meta["intervention_bookkeeping"], "STATE_ABSENT")
        self.assertIsNone(meta["intervention_id"])
        self.assertEqual(self.notifier.send.call_count, 1)

    def tmp_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_state_write_failure_still_alerts_without_exception_text(self):
        broken = mock.Mock()
        broken.exists.return_value = True
        broken.transaction.side_effect = RuntimeError("EXPLOSION-XYZ")
        self.crash_loop(store=broken)
        crash = self.ledger_events("SUPERVISOR_CRASH_LOOP")
        self.assertEqual(len(crash), 1)
        meta = crash[0]["metadata_redacted"]
        self.assertEqual(meta["intervention_bookkeeping"], "STATE_WRITE_FAILED")
        self.assertIsNone(meta["intervention_id"])
        self.assertEqual(self.notifier.send.call_count, 1)
        raw = Path(self.ledger.path).read_text(encoding="utf-8")
        self.assertNotIn("EXPLOSION-XYZ", raw)
        self.assertNotIn("EXPLOSION-XYZ",
                         json.dumps(list(self.notifier.send.call_args.args)))

    def test_requested_ledger_failure_still_alerts(self):
        watchdog.escalate_crash_loop(self.cfg, LedgerRefusingRequested(self.ledger),
                                     self.notifier, 3, store=self.store)
        self.assertEqual(self.ledger_events("HUMAN_INTERVENTION_REQUESTED"), [])
        crash = self.ledger_events("SUPERVISOR_CRASH_LOOP")
        self.assertEqual(len(crash), 1)
        meta = crash[0]["metadata_redacted"]
        self.assertEqual(meta["intervention_bookkeeping"], "REQUESTED_EVENT_FAILED")
        self.assertEqual(self.notifier.send.call_count, 1)
        # The durable intervention itself was not lost, and the failure event
        # still names it.
        records = list(self.store.read()["interventions"].values())
        self.assertEqual(len(records), 1)
        self.assertEqual(meta["intervention_id"], records[0]["id"])

    def test_duplicate_recurrence_alerts_again_but_records_once(self):
        self.crash_loop()
        self.crash_loop()
        self.assertEqual(len(self.store.read()["interventions"]), 1)
        self.assertEqual(len(self.ledger_events("HUMAN_INTERVENTION_REQUESTED")), 1)
        self.assertEqual(len(self.ledger_events("SUPERVISOR_CRASH_LOOP")), 2)
        self.assertEqual(self.notifier.send.call_count, 2)


class TestS10RestartFailed(WatchdogEscalationCase):
    def test_records_intervention_and_alerts(self):
        self.restart_failed()
        doc = self.store.read()
        rec = list(doc["interventions"].values())[0]
        self.assertEqual(rec["type"], "HUMAN_APPARATUS_AUTHORISATION")
        self.assertEqual(rec["condition_code"], "supervisor_restart_failed")
        failed = self.ledger_events("SUPERVISOR_RESTART_FAILED")
        self.assertEqual(len(failed), 1)
        meta = failed[0]["metadata_redacted"]
        self.assertEqual(meta["intervention_bookkeeping"], "OK")
        self.assertEqual(meta["intervention_id"], rec["id"])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertIn(rec["id"], self.notifier.send.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
