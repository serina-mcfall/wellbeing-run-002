"""C-05.3b end to end: plan -> execute -> commit, through the real Supervisor.

Not the helpers in isolation. `Supervisor.plan_accessibility_auto` writes
the claim in T1, `accessibility_evidence.run_attempt` runs the attempt with
every external service injected, and `Supervisor.ingest_accessibility_auto`
commits the answer after re-verifying the claim and the head.

The demonstrations required of this work, each with its own case:

    failed evidence blocks the merge
    repair, then fresh evidence, succeeds
    a changed head invalidates what was gathered for the old one
    the decision survives a restart
    the owned server is cleaned up and its port returned
    the task reaches REVIEW only when every class passes

Nothing here installs, builds, binds, spawns or scans. The services are
injected; what is exercised is the Supervisor's own sequencing.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import accessibility_contract as ac  # noqa: E402
from control import accessibility_evidence as ae  # noqa: E402
from control import config, notify, providers, routing, workers  # noqa: E402
from control import ledger as ledger_mod  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as sv_mod  # noqa: E402
from mergeable_evidence import (  # noqa: E402
    accessibility_review_leg, security_leg)

TZ = "Pacific/Auckland"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZoneInfo(TZ))
TIMEOUTS = config.load().extra["timeouts"]


def checks(result="PASS", sha=HEAD, only=None, detail=None):
    out = []
    for check_id in routing.AUTOMATED_CHECK_IDS:
        entry = {"check_id": check_id, "sha": sha,
                 "artifact_reference": "evidence/a11y",
                 "result": result if (only is None or check_id in only) else "PASS"}
        if detail is not None and (only is None or check_id in only):
            entry["detail"] = detail
        out.append(entry)
    return out


class Services:
    def __init__(self, **over):
        self.stopped = 0
        self.cfg = dict(entrypoint=True, lockfile=True, install=(True, None),
                        server="handle", ready=True, scan=checks(),
                        listening=set())
        self.cfg.update(over)

    def exists(self, path):
        return self.cfg["entrypoint"] if path == ae.PRODUCT_ENTRYPOINT \
            else self.cfg["lockfile"]

    def install_build(self, bound):
        return self.cfg["install"]

    def start_server(self, port, bound):
        return self.cfg["server"]

    def await_ready(self, port, bound):
        return self.cfg["ready"]

    def scan(self, port, sha, bound):
        return self.cfg["scan"]

    def stop_server(self, handle, bound):
        self.stopped += 1

    def listening_ports(self):
        return self.cfg["listening"]


class ConnectedLifecycleCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        self.sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r", max_security=1,
            max_accessibility_auto=1, max_accessibility_review=1,
            extra={"timeouts": dict(TIMEOUTS)})
        self.sv.tz = TZ
        self.sv.stopping = False
        self.sv.store = state_mod.Store(path=root / "state.json", tz=TZ)
        self.sv.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                           experiment_id="run-002")
        self.sv.notifier = mock.Mock(spec=notify.Notifier)
        self.sv.now = mock.Mock(return_value=NOW)
        self.sv.request_intervention = mock.Mock(return_value={"id": "I-1"})
        self.logged = []
        self.sv.log = mock.Mock(
            side_effect=lambda e, **k: self.logged.append((e, k)))

    # ------------------------------------------------------------ fixtures

    def doc_with(self, *, head=HEAD, security=True, review=True):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": "WAITING_EVIDENCE", "pr": 7,
                     "branch": "run-002/task-001"})
        record = routing.blank_pr_record(7, "TASK-001", "run-002/task-001")
        if security:
            record["security_evidence"] = security_leg(head, "task-001")
        if review:
            record["accessibility_review"] = accessibility_review_leg(
                head, "task-001")
        doc["prs"]["7"] = record
        return doc, task, record

    def cycle(self, doc, task, services, head=HEAD):
        """plan (T1) -> execute (outside) -> commit, exactly as a tick does."""
        plan = self.sv.plan_accessibility_auto(doc, task, 7, head)
        if plan is None:
            return None, None
        outcome = ae.run_attempt(plan, self.sv._accessibility_budget(),
                                 services, lambda: 0.0)
        claim = doc["prs"]["7"]["accessibility_auto"]
        outcome = ae.confirm_release(outcome, claim, services)
        self.sv.ingest_accessibility_auto(doc, task, 7, head, plan, outcome)
        return plan, outcome

    def kinds(self):
        return [e for e, _ in self.logged]

    # ------------------------------------------------- the whole path

    def test_a_clean_attempt_reaches_review(self):
        doc, task, _ = self.doc_with()
        svc = Services()
        _plan, outcome = self.cycle(doc, task, svc)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_PASS)
        self.assertEqual(task["state"], "REVIEW")
        self.assertIn("EVIDENCE_COMPLETE", self.kinds())

    def test_the_server_is_cleaned_up_and_the_port_returned(self):
        doc, task, record = self.doc_with()
        svc = Services()
        plan, _ = self.cycle(doc, task, svc)
        self.assertEqual(svc.stopped, 1)
        self.assertTrue(record["accessibility_auto"]["port_released"])
        self.assertEqual(
            workers.claimed_product_server_ports(doc), {},
            "the port is still claimed after a confirmed teardown")

    def test_the_claimed_port_is_withheld_from_builders_while_in_flight(self):
        doc, task, _ = self.doc_with()
        plan = self.sv.plan_accessibility_auto(doc, task, 7, HEAD)
        with mock.patch.object(workers, "_reserved_job_file_ports",
                               return_value=set()):
            candidates, _ = workers.select_port_candidates(doc)
        self.assertNotIn(plan.port, candidates)

    # --------------------------------------- failed evidence blocks

    def test_an_accessibility_failure_routes_to_fix_required(self):
        doc, task, record = self.doc_with()
        svc = Services(scan=checks(result="FAIL", only={"TOUCH_TARGETS"}))
        _plan, outcome = self.cycle(doc, task, svc)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_FAIL)
        self.assertEqual(task["state"], "FIX_REQUIRED")
        self.assertEqual(
            [f["unmet_requirement"] for f in record["pending_findings"]],
            ["ACC-DOD-TOUCH_TARGETS"])

    def test_an_unmapped_failure_still_blocks(self):
        doc, task, record = self.doc_with()
        svc = Services(scan=checks(result="FAIL", only={"AXE_SCAN"},
                                   detail={"violations": [{"id": "image-alt"}]}))
        self.cycle(doc, task, svc)
        self.assertEqual(task["state"], "FIX_REQUIRED")
        self.assertTrue(record["pending_findings"])
        self.assertIsNone(record["pending_findings"][0]["unmet_requirement"])

    def test_every_blocking_product_state_holds_without_a_verdict(self):
        # G3: no NOT_APPLICABLE route. Each is an apparatus failure, so
        # none routes to FIX_REQUIRED - a run that did not happen is not
        # evidence that the page is inaccessible.
        for label, over, reason in (
                ("entrypoint", dict(entrypoint=False),
                 ac.PRODUCT_ENTRYPOINT_MISSING),
                ("lockfile", dict(lockfile=False), ac.PRODUCT_LOCKFILE_MISSING),
                ("build", dict(install=(False, ac.PRODUCT_BUILD_FAILED)),
                 ac.PRODUCT_BUILD_FAILED),
                ("server", dict(server=None), ac.PRODUCT_SERVER_UNREADY)):
            with self.subTest(blocked_on=label):
                self.setUp()
                doc, task, record = self.doc_with()
                _plan, outcome = self.cycle(doc, task, Services(**over))
                self.assertEqual(outcome.reason, reason)
                self.assertEqual(task["state"], "WAITING_EVIDENCE")
                self.assertIsNone(record["accessibility_auto"]["verdict"])
                self.assertEqual(record["accessibility_auto"]["reason"], reason)

    def test_an_unconfirmed_teardown_keeps_the_port_and_does_not_pass(self):
        doc, task, record = self.doc_with()
        svc = Services(listening={3200})
        _plan, outcome = self.cycle(doc, task, svc)
        self.assertEqual(outcome.reason, ac.PRODUCT_SERVER_NOT_RELEASED)
        self.assertFalse(record["accessibility_auto"]["port_released"])
        self.assertEqual(task["state"], "WAITING_EVIDENCE")
        self.assertIn(record["accessibility_auto"]["port"],
                      workers.claimed_product_server_ports(doc))

    def test_a_held_port_stops_the_next_attempt_being_planned(self):
        doc, task, _ = self.doc_with()
        self.cycle(doc, task, Services(listening={3200}))
        self.assertIsNone(self.sv.plan_accessibility_auto(doc, task, 7, HEAD))

    # ------------------------------- repair, then fresh evidence

    def test_repair_then_fresh_evidence_at_the_new_head_succeeds(self):
        doc, task, record = self.doc_with()
        self.cycle(doc, task,
                   Services(scan=checks(result="FAIL", only={"TOUCH_TARGETS"})))
        self.assertEqual(task["state"], "FIX_REQUIRED")

        # The fixer moves the head; the other classes are regenerated.
        state_mod.transition(doc, "TASK-001", "PR_OPEN", "fixed", TZ)
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "evidence", TZ)
        record["security_evidence"] = security_leg(NEW_HEAD, "task-001")
        record["accessibility_review"] = accessibility_review_leg(
            NEW_HEAD, "task-001")

        _plan, outcome = self.cycle(doc, task, Services(scan=checks(sha=NEW_HEAD)),
                                    head=NEW_HEAD)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_PASS)
        self.assertEqual(task["state"], "REVIEW")

    def test_a_second_attempt_takes_the_next_ordinal(self):
        doc, task, record = self.doc_with()
        first, _ = self.cycle(doc, task, Services(server=None))
        second, _ = self.cycle(doc, task, Services())
        self.assertEqual(first.attempt_id, "attempt-0001")
        self.assertEqual(second.attempt_id, "attempt-0002")

    # ------------------------------- changed-head invalidation

    def test_evidence_for_the_old_head_does_not_satisfy_the_new_one(self):
        doc, task, _ = self.doc_with()
        self.cycle(doc, task, Services())
        self.assertEqual(task["state"], "REVIEW")

        state_mod.transition(doc, "TASK-001", "FIX_REQUIRED", "x", TZ)
        state_mod.transition(doc, "TASK-001", "PR_OPEN", "x", TZ)
        state_mod.transition(doc, "TASK-001", "WAITING_EVIDENCE", "x", TZ)
        self.assertFalse(self.sv.advance_if_evidence_complete(
            doc, task, 7, NEW_HEAD))
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_a_result_for_a_superseded_claim_is_discarded(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_auto(doc, task, 7, HEAD)
        # The world moves on: the claim is replaced while the attempt runs.
        record["accessibility_auto"] = dict(record["accessibility_auto"],
                                            attempt_id="attempt-0009")
        outcome = ae.run_attempt(plan, self.sv._accessibility_budget(),
                                 Services(), lambda: 0.0)
        self.sv.ingest_accessibility_auto(doc, task, 7, HEAD, plan, outcome)
        self.assertIn("ACCESSIBILITY_RESULT_DISCARDED", self.kinds())
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    # ------------------------------------------- restart recovery

    def test_the_whole_decision_survives_a_restart(self):
        doc, task, _ = self.doc_with()
        plan = self.sv.plan_accessibility_auto(doc, task, 7, HEAD)
        self.sv.store._write(doc)

        # A Supervisor restart: nothing in memory carries over.
        reloaded = self.sv.store.read()
        task2 = reloaded["tasks"]["TASK-001"]
        svc = Services()
        outcome = ae.run_attempt(plan, self.sv._accessibility_budget(),
                                 svc, lambda: 0.0)
        claim = reloaded["prs"]["7"]["accessibility_auto"]
        outcome = ae.confirm_release(outcome, claim, svc)
        self.sv.ingest_accessibility_auto(reloaded, task2, 7, HEAD, plan,
                                          outcome)
        self.assertEqual(task2["state"], "REVIEW")

    def test_a_claim_in_flight_is_not_re_planned_after_a_restart(self):
        # The window a restart opens: the claim is durable, the attempt is
        # not. Re-planning would start a second server on a claimed port.
        doc, task, _ = self.doc_with()
        self.sv.plan_accessibility_auto(doc, task, 7, HEAD)
        self.sv.store._write(doc)
        reloaded = self.sv.store.read()
        self.assertIsNone(self.sv.plan_accessibility_auto(
            reloaded, reloaded["tasks"]["TASK-001"], 7, HEAD))

    # ----------------------------------------------- idempotence

    def test_committing_twice_does_not_duplicate_findings(self):
        doc, task, record = self.doc_with()
        svc = Services(scan=checks(result="FAIL", only={"TOUCH_TARGETS"}))
        plan, outcome = self.cycle(doc, task, svc)
        first = list(record["pending_findings"])
        self.sv.ingest_accessibility_auto(doc, task, 7, HEAD, plan, outcome)
        self.assertEqual(record["pending_findings"], first)

    def test_concurrency_is_bounded_at_the_governed_one(self):
        doc, task, _ = self.doc_with()
        self.sv.plan_accessibility_auto(doc, task, 7, HEAD)
        # A second PR cannot claim while the first is unreleased.
        doc["prs"]["8"] = routing.blank_pr_record(8, "TASK-002", "b")
        other = state_mod.add_task(doc, "TASK-002", "T2", [], "feature", False, TZ)
        other.update({"state": "WAITING_EVIDENCE", "pr": 8})
        self.assertIsNone(self.sv.plan_accessibility_auto(doc, other, 8, HEAD))


if __name__ == "__main__":
    unittest.main()
