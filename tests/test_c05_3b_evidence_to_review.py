"""C-20(a): WAITING_EVIDENCE finally has an exit, and it is governed.

Before this, `supervisor.ingest_security` carried a comment saying a
SECURITY_PASS "deliberately does NOT advance to REVIEW ... so the task
holds in WAITING_EVIDENCE". That hold was correct about accessibility
being required, but it was UNCONDITIONAL and the state had no exit at all:
every product PR deadlocked there forever, with no diagnostic saying why.

The hold is now a gate. `routing.review_gate_fires` answers the question
the comment was standing in for - is every required evidence class a
completed pass at THIS head - and the task advances exactly when that is
true.

Driven through `Supervisor.route_evidence` and `ingest_security`, the
functions a tick actually calls. External services are injected.

WHAT THIS FILE DOES NOT CLAIM: nothing dispatches the accessibility legs
yet, so in a real run today the gate cannot fire. What is proved here is
that the exit exists, is governed by evidence rather than by a comment,
and refuses every way evidence can be incomplete.
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

from control import ledger as ledger_mod  # noqa: E402
from control import notify, providers, routing  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as sv_mod  # noqa: E402
from mergeable_evidence import complete_evidence  # noqa: E402

TZ = "Pacific/Auckland"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZoneInfo(TZ))


class EvidenceToReviewCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        self.sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r", max_security=1,
            extra={"timeouts": {"waiting_evidence_total_seconds": 18000}})
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
        # Separate concerns with their own suites. This file is about the
        # ADVANCE decision, so worktree ownership and new-attempt planning
        # are stubbed rather than re-tested here.
        self.sv._own_security_worktree = mock.Mock()
        self.sv.plan_security = mock.Mock(return_value=None)

    # ------------------------------------------------------------ fixtures

    def doc_with(self, *, head=HEAD, evidence_at=HEAD, state="WAITING_EVIDENCE"):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": state, "pr": 7, "branch": "run-002/task-001"})
        record = routing.blank_pr_record(7, "TASK-001", "run-002/task-001")
        if evidence_at is not None:
            record.update(complete_evidence(evidence_at))
        doc["prs"]["7"] = record
        return doc, task, record

    def route(self, doc, head=HEAD):
        # outcome None: the security result landed earlier, and this tick
        # is simply re-examining whether every class now passes.
        return self.sv.route_evidence(doc, {7: (head, {}, None, {})})

    def kinds(self):
        return [event for event, _ in self.logged]

    # ------------------------------------------------- the exit exists

    def test_complete_evidence_advances_to_review(self):
        doc, task, _ = self.doc_with()
        self.route(doc)
        self.assertEqual(task["state"], "REVIEW")

    def test_the_advance_is_durably_evidenced(self):
        doc, _, _ = self.doc_with()
        self.route(doc)
        self.assertIn("EVIDENCE_COMPLETE", self.kinds())
        payload = next(k for e, k in self.logged if e == "EVIDENCE_COMPLETE")
        self.assertEqual(payload["outcome"], "REVIEW")
        self.assertEqual(payload["metadata_redacted"]["head"], HEAD)
        self.assertEqual(sorted(payload["metadata_redacted"]["classes"]),
                         ["accessibility_auto", "accessibility_review",
                          "security_evidence"])

    def test_the_evidence_wait_interval_is_closed_on_the_way_out(self):
        doc, task, _ = self.doc_with()
        task[state_mod.EVIDENCE_WAIT_SINCE] = NOW.isoformat(timespec="seconds")
        self.route(doc)
        self.assertIsNone(task[state_mod.EVIDENCE_WAIT_SINCE])

    # ------------------------------------------- failed evidence blocks

    def test_no_evidence_at_all_holds(self):
        doc, task, _ = self.doc_with(evidence_at=None)
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")
        self.assertNotIn("EVIDENCE_COMPLETE", self.kinds())

    def test_each_missing_class_independently_holds(self):
        for leg in ("security_evidence", "accessibility_auto",
                    "accessibility_review"):
            with self.subTest(missing=leg):
                self.setUp()
                doc, task, record = self.doc_with()
                del record[leg]
                self.route(doc)
                self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_a_failed_class_holds(self):
        doc, task, record = self.doc_with()
        record["accessibility_auto"]["verdict"] = routing.ACCESSIBILITY_AUTO_FAIL
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_an_incomplete_class_holds(self):
        doc, task, record = self.doc_with()
        record["accessibility_review"]["claim_state"] = "SPAWNED"
        record["accessibility_review"]["verdict"] = None
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_a_class_its_own_validator_refuses_holds(self):
        # Structurally plausible, bound to this head, but the worker name
        # addresses another commit's artefacts.
        doc, task, record = self.doc_with()
        record["accessibility_review"]["worker"] = \
            f"task-001-a11y-{NEW_HEAD}-0001"
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    # ------------------------------------ changed-head invalidation

    def test_evidence_for_a_superseded_commit_does_not_advance(self):
        doc, task, _ = self.doc_with(evidence_at=HEAD)
        self.route(doc, head=NEW_HEAD)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_a_unanimously_stale_set_does_not_advance(self):
        # Three claims agreeing perfectly with EACH OTHER about a commit
        # that has since been superseded. Pairwise agreement is not
        # freshness; every leg is compared to the head observed this tick.
        doc, task, record = self.doc_with(evidence_at=NEW_HEAD)
        self.route(doc, head=HEAD)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_regenerating_at_the_new_head_then_advances(self):
        doc, task, record = self.doc_with(evidence_at=HEAD)
        self.route(doc, head=NEW_HEAD)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")
        record.update(complete_evidence(NEW_HEAD))
        self.route(doc, head=NEW_HEAD)
        self.assertEqual(task["state"], "REVIEW")

    # ------------------------------------------------ restart recovery

    def test_the_decision_survives_a_restart(self):
        # A Supervisor restart is a round trip through durable state. The
        # advance must be re-derivable from the document alone, with no
        # in-memory carry-over.
        doc, task, _ = self.doc_with()
        self.sv.store._write(doc)
        reloaded = self.sv.store.read()
        self.route(reloaded)
        self.assertEqual(reloaded["tasks"]["TASK-001"]["state"], "REVIEW")

    # -------------------------------------------- it only fires once

    def test_a_task_already_in_review_is_not_advanced_again(self):
        doc, task, _ = self.doc_with(state="REVIEW")
        self.route(doc)
        self.assertEqual(task["state"], "REVIEW")
        self.assertNotIn("EVIDENCE_COMPLETE", self.kinds())

    def test_a_missing_pr_record_holds_rather_than_raising(self):
        doc, task, _ = self.doc_with()
        del doc["prs"]["7"]
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    # ------------------------------- the ingest path advances too

    def test_a_security_pass_ingest_advances_when_the_rest_already_passed(self):
        doc, task, record = self.doc_with()
        # The security claim is mid-flight; the accessibility legs are done.
        record["security_evidence"]["claim_state"] = "SPAWNED"
        record["security_evidence"]["verdict"] = None
        self.sv.ingest_security(
            doc, task, 7, HEAD,
            {"status": sv_mod.gate_evidence.COMPLETED,
             "attempt_id": record["security_evidence"]["attempt_id"],
             "verdict": routing.SECURITY_PASS, "findings": []})
        self.assertEqual(task["state"], "REVIEW")

    def test_a_security_pass_still_holds_when_accessibility_is_absent(self):
        # The old unconditional hold, now reached for the right reason.
        doc, task, record = self.doc_with()
        del record["accessibility_auto"]
        record["security_evidence"]["claim_state"] = "SPAWNED"
        record["security_evidence"]["verdict"] = None
        self.sv.ingest_security(
            doc, task, 7, HEAD,
            {"status": sv_mod.gate_evidence.COMPLETED,
             "attempt_id": record["security_evidence"]["attempt_id"],
             "verdict": routing.SECURITY_PASS, "findings": []})
        self.assertEqual(task["state"], "WAITING_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
