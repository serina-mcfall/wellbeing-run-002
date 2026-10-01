"""G4 exhaustion through the LIVE route, not through the predicate alone.

`test_g3_g4_evidence_budget.py` proves the arithmetic. This proves the
arithmetic is actually consulted by `Supervisor.route_evidence`, which is
the function a tick calls, and that exhaustion takes the governed
HUMAN_REQUIRED path with a finite reason rather than failing the task or
waiting forever.

The clock is injected. No wall-clock test, and none is required.

The harness is deliberately standalone rather than inherited from
`test_c05_3_evidence_routing`: subclassing re-runs that file's whole suite
under a Supervisor whose `log` and `request_intervention` this file
replaces, which would silently change what those tests mean.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import ledger as ledger_mod  # noqa: E402
from control import notify, providers  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as sv_mod  # noqa: E402

TZ = "Pacific/Auckland"
ALLOWANCE = 18000
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZoneInfo(TZ))


class EvidenceWaitEscalationCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        self.sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r",
            extra={"timeouts": {"waiting_evidence_total_seconds": ALLOWANCE}})
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

    def doc_with(self, state="WAITING_EVIDENCE", waited=None):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": state, "pr": 7, "branch": "run-002/task-001"})
        doc["prs"]["7"] = {"number": 7}
        if waited is not None:
            task[state_mod.EVIDENCE_WAIT_SINCE] = (
                NOW - timedelta(seconds=waited)).isoformat(timespec="seconds")
        return doc, task

    def route(self, doc):
        # No observation is supplied ON PURPOSE. A task starved of
        # observations is exactly the one that would otherwise wait
        # forever, so the allowance must be checked without one.
        return self.sv.route_evidence(doc, {})

    def kinds(self):
        return [event for event, _ in self.logged]

    # --------------------------------------------------------------- cases

    def test_an_exhausted_task_escalates_to_human_required(self):
        doc, task = self.doc_with(waited=ALLOWANCE + 60)
        self.route(doc)
        self.assertEqual(task["state"], "HUMAN_REQUIRED")

    def test_a_task_inside_its_allowance_is_untouched(self):
        doc, task = self.doc_with(waited=ALLOWANCE - 60)
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")
        self.assertNotIn("EVIDENCE_WAIT_EXHAUSTED", self.kinds())

    def test_a_task_that_never_waited_is_untouched(self):
        doc, task = self.doc_with()
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_the_escalation_is_durably_evidenced(self):
        doc, _ = self.doc_with(waited=ALLOWANCE + 60)
        self.route(doc)
        self.assertIn("EVIDENCE_WAIT_EXHAUSTED", self.kinds())
        payload = next(k for e, k in self.logged
                       if e == "EVIDENCE_WAIT_EXHAUSTED")
        meta = payload["metadata_redacted"]
        self.assertEqual(meta["allowance_seconds"], ALLOWANCE)
        self.assertGreaterEqual(meta["waited_seconds"], ALLOWANCE)
        self.assertIsInstance(meta["waited_seconds"], int)
        self.assertEqual(payload["outcome"], "HUMAN_REQUIRED")

    def test_the_governed_intervention_path_is_used(self):
        doc, _ = self.doc_with(waited=ALLOWANCE + 60)
        self.route(doc)
        self.sv.request_intervention.assert_called_once()
        kwargs = self.sv.request_intervention.call_args.kwargs
        self.assertEqual(kwargs["condition_code"],
                         self.sv.EVIDENCE_WAIT_EXHAUSTED)
        self.assertEqual(kwargs["task_id"], "TASK-001")
        self.assertEqual(kwargs["pr_id"], 7)
        # C-08b.2: the durable reason is structurally generated from a
        # fixed template and canonical identifiers, never free text.
        self.assertIn("TASK-001", kwargs["reason"])
        self.assertNotIn("\n", kwargs["reason"])

    def test_escalating_twice_does_not_transition_twice(self):
        doc, task = self.doc_with(waited=ALLOWANCE + 60)
        self.route(doc)
        history = len(task["history"])
        self.route(doc)
        self.assertEqual(len(task["history"]), history,
                         "a second tick appended another history entry")

    def test_an_ungoverned_allowance_escalates_a_waiting_task(self):
        del self.sv.cfg.extra["timeouts"]["waiting_evidence_total_seconds"]
        doc, task = self.doc_with(waited=60)
        self.route(doc)
        self.assertEqual(task["state"], "HUMAN_REQUIRED",
                         "an ungoverned bound became an unbounded wait")

    def test_an_ungoverned_allowance_spares_a_task_that_never_waited(self):
        # A configuration fault must not escalate a task that has done
        # nothing. The config is what is broken, not the task.
        del self.sv.cfg.extra["timeouts"]["waiting_evidence_total_seconds"]
        doc, task = self.doc_with()
        self.route(doc)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_an_unreadable_wait_record_escalates(self):
        doc, task = self.doc_with(waited=60)
        task[state_mod.EVIDENCE_WAIT_SINCE] = "nonsense"
        self.route(doc)
        self.assertEqual(task["state"], "HUMAN_REQUIRED")

    def test_a_pr_open_task_is_not_subject_to_the_evidence_allowance(self):
        # PR_OPEN is an evidence state but is not WAITING: the allowance
        # governs time spent waiting for evidence, not time before it.
        doc, task = self.doc_with(state="PR_OPEN")
        task[state_mod.EVIDENCE_WAIT_TOTAL] = ALLOWANCE * 2
        self.route(doc)
        self.assertEqual(task["state"], "PR_OPEN")

    def test_the_accumulated_total_is_what_is_judged_not_one_cycle(self):
        # Three closed cycles, none individually over the allowance.
        doc, task = self.doc_with()
        task[state_mod.EVIDENCE_WAIT_TOTAL] = ALLOWANCE + 1
        self.route(doc)
        self.assertEqual(task["state"], "HUMAN_REQUIRED")


if __name__ == "__main__":
    unittest.main()
