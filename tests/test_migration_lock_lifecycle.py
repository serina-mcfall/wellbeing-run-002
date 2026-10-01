"""C-15: migration-lock ownership lifecycle on abandonment.

Pins the two dispatch-failure subcases and the auto-release evidence
durability:

  * FRESH owner - this dispatch newly acquired the lock and failed at a proven
    pre-builder-execution point, so no schema mutation is attributable to the
    ownership: the lock is released automatically, atomically with the BLOCKED
    transition and a task-owned durable marker.
  * RE-ENTRANT owner - a builder previously ran under this ownership, so
    shared-schema mutation is possible: never auto-released; the task goes to
    HUMAN_REQUIRED with a builder_dispatch_failed intervention instead of
    invisible BLOCKED.

Evidence reconciliation converges the task-owned marker to exactly one
MIGRATION_LOCK_RELEASED ledger event without ever touching current lock
ownership, surviving ledger failures, lost evidenced-flag commits and
unrelated later lock activity.

Supervisor fixture style follows tests/test_intervention_integration.py; the
ledger is real (temp file) because evidence reconciliation reads it.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    budget,
    clock,
    config,
    intervention,
    ledger as ledger_mod,
    migration_lock,
    providers,
    state,
    supervisor as supervisor_mod,
)

TZ = "Pacific/Auckland"
AUTO_WHY = "DISPATCH_FAILED_BEFORE_BUILDER_STARTED"
AUTO_BY = "dispatch_failed_before_builder_started"


def base_doc() -> dict:
    doc = state.initial_document("run-002", "v2.0")
    providers.ensure(doc)
    budget.ensure(doc, 25.0)
    migration_lock.ensure(doc)
    for tid in ("TASK-002", "TASK-003"):
        task = state.add_task(doc, tid, "schema", [], "migration", True, TZ)
        task["state"] = "READY"
    return doc


class LockLifecycleCase(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)
        self.ledger = ledger_mod.Ledger(path=Path(tmp.name) / "ledger.jsonl",
                                        tz=TZ, experiment_id="run-002")
        self.sup.ledger = self.ledger
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    # ------------------------------------------------------------- helpers
    def dispatch_fail(self, doc, task_id="TASK-002", site="create"):
        """Drive a whole builder dispatch to the chosen pre-execution failure.

        C-18 stage 4: `dispatch_builder` only claims now, so the helper runs
        the same three phases `tick()` runs - plan inside the transaction,
        execute with no lock held, commit the outcome. `apply_dispatch_result`
        is used rather than `confirm_dispatches` so the assertions can read
        the same `doc` the plan was made against, which is what every test in
        this file already does.
        """
        create_ok = site != "create"
        path = None if site == "path" else Path("/tmp/run-002-test/wt")
        start_ok = site != "start"
        self.sup._dispatch_plans = []
        with mock.patch.object(supervisor_mod.prompts, "builder", return_value="p"), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p.md")), \
                mock.patch.object(supervisor_mod.workers, "create_worker",
                                  return_value=mock.Mock(ok=create_ok, stderr="", stdout="")), \
                mock.patch.object(supervisor_mod.workers, "worktree_path",
                                  return_value=path), \
                mock.patch.object(supervisor_mod.workers, "probe_port",
                                  return_value=True), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/job.json")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=mock.Mock(ok=start_ok, stderr="boom", stdout="")):
            self.sup.dispatch_builder(doc, doc["tasks"][task_id])
            results = self.sup.execute_dispatches(self.sup._dispatch_plans,
                                                  snapshot=doc)
        for result in results:
            self.sup.apply_dispatch_result(doc, result)

    def lock_events(self, task_id=None):
        out = []
        for line in Path(self.ledger.path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event_type") != "MIGRATION_LOCK_RELEASED":
                continue
            if task_id and event.get("task_id") != task_id:
                continue
            out.append(event)
        return out

    def marker(self, doc, task_id="TASK-002"):
        return doc["tasks"][task_id].get("migration_lock_auto_release")


class TestFreshOwnerDispatchFailure(LockLifecycleCase):
    def test_each_pre_execution_failure_releases_and_marks(self):
        for site in ("create", "path", "start"):
            with self.subTest(site=site):
                doc = base_doc()
                self.dispatch_fail(doc, site=site)
                task = doc["tasks"]["TASK-002"]
                self.assertEqual(task["state"], "BLOCKED")
                self.assertEqual(migration_lock.state(doc), migration_lock.FREE)
                self.assertIsNone(migration_lock.owner(doc))
                marker = self.marker(doc)
                self.assertIsNotNone(marker)
                self.assertEqual(marker["why"], AUTO_WHY)
                self.assertFalse(marker["evidenced"])
                self.assertTrue(marker["at"])
                # No human intervention is created merely for the release.
                self.assertEqual(doc["interventions"], {})

    def test_next_schema_task_can_acquire_after_the_release(self):
        doc = base_doc()
        self.dispatch_fail(doc)
        self.assertTrue(migration_lock.acquire(doc, "TASK-003", TZ))

    def test_marker_never_written_without_a_real_release(self):
        doc = base_doc()
        with mock.patch.object(supervisor_mod.migration_lock, "release",
                               return_value=False):
            with self.assertRaises(RuntimeError):
                self.dispatch_fail(doc)
        # fail closed: no marker claiming a release that did not happen.
        self.assertIsNone(self.marker(doc))

    def test_pre_existing_unevidenced_marker_fails_closed(self):
        doc = base_doc()
        doc["tasks"]["TASK-002"]["migration_lock_auto_release"] = {
            "at": "x", "why": AUTO_WHY, "evidenced": False}
        with self.assertRaises(RuntimeError):
            self.dispatch_fail(doc)

    def test_non_schema_task_failure_is_unchanged(self):
        doc = base_doc()
        task = state.add_task(doc, "TASK-006", "ui", [], "feature", False, TZ)
        task["state"] = "READY"
        self.dispatch_fail(doc, task_id="TASK-006")
        self.assertEqual(doc["tasks"]["TASK-006"]["state"], "BLOCKED")
        self.assertIsNone(self.marker(doc, "TASK-006"))
        self.assertEqual(doc["interventions"], {})


class TestDispatchFailureHistoryShape(LockLifecycleCase):
    """The helper is the single owner of the failure-state decision: exactly
    one transition per failure, recorded through the REAL state machinery.
    A reintroduced caller-side BLOCKED transition would either add a
    BLOCKED -> BLOCKED no-op (fresh) or an intermediate BLOCKED edge
    (re-entrant) - both pinned here."""

    def edges(self, doc, task_id="TASK-002"):
        return [(h["from"], h["to"]) for h in doc["tasks"][task_id]["history"]]

    def test_fresh_owner_history_is_exactly_one_edge_into_blocked(self):
        for site in ("create", "path", "start"):
            with self.subTest(site=site):
                doc = base_doc()
                self.dispatch_fail(doc, site=site)
                edges = self.edges(doc)
                self.assertEqual(edges, [("READY", "BLOCKED")])
                self.assertNotIn(("BLOCKED", "BLOCKED"), edges)
                self.assertEqual(migration_lock.state(doc), migration_lock.FREE)
                self.assertFalse(self.marker(doc)["evidenced"])

    def test_reentrant_owner_goes_directly_to_human_required(self):
        doc = base_doc()
        self.assertTrue(migration_lock.acquire(doc, "TASK-002", TZ))
        doc["tasks"]["TASK-002"]["attempts"] = 1
        self.dispatch_fail(doc)
        edges = self.edges(doc)
        self.assertEqual(edges, [("READY", "HUMAN_REQUIRED")])
        self.assertNotIn("BLOCKED", [e[1] for e in edges])
        self.assertEqual(migration_lock.owner(doc), "TASK-002")

    def test_non_owner_history_is_exactly_one_edge_into_blocked(self):
        doc = base_doc()
        task = state.add_task(doc, "TASK-006", "ui", [], "feature", False, TZ)
        task["state"] = "READY"
        self.dispatch_fail(doc, task_id="TASK-006")
        self.assertEqual(self.edges(doc, "TASK-006"), [("READY", "BLOCKED")])


class TestReentrantOwnerDispatchFailure(LockLifecycleCase):
    def seed_reentrant(self, doc):
        """A previous builder attempt ran under this ownership."""
        self.assertTrue(migration_lock.acquire(doc, "TASK-002", TZ))
        doc["tasks"]["TASK-002"]["attempts"] = 1

    def test_never_auto_released_and_escalates_to_a_human(self):
        doc = base_doc()
        self.seed_reentrant(doc)
        self.dispatch_fail(doc)
        task = doc["tasks"]["TASK-002"]
        self.assertEqual(task["state"], "HUMAN_REQUIRED")
        self.assertEqual(migration_lock.owner(doc), "TASK-002")
        self.assertIsNone(self.marker(doc))
        records = intervention.open_interventions(doc)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["condition_code"], "builder_dispatch_failed")
        self.assertEqual(rec["type"], "HUMAN_APPARATUS_AUTHORISATION")
        self.assertEqual(rec["task_id"], "TASK-002")
        self.assertEqual(self.sup.notify_out.call_count, 1)
        self.assertIn(rec["id"], self.sup.notify_out.call_args.args[3])

    def test_recurrence_deduplicates(self):
        doc = base_doc()
        self.seed_reentrant(doc)
        self.dispatch_fail(doc)
        doc["tasks"]["TASK-002"]["state"] = "READY"
        self.dispatch_fail(doc)
        self.assertEqual(len(intervention.open_interventions(doc)), 1)
        self.assertEqual(self.sup.notify_out.call_count, 1)

    def test_free_text_stderr_stays_out_of_the_reason(self):
        doc = base_doc()
        self.seed_reentrant(doc)
        self.dispatch_fail(doc, site="start")
        rec = intervention.open_interventions(doc)[0]
        self.assertEqual(rec["reason"],
                         "Builder dispatch failed for TASK-002 while it owns "
                         "the migration lock")


class TestAutoReleaseEvidence(LockLifecycleCase):
    def released_doc(self):
        doc = base_doc()
        self.dispatch_fail(doc)
        return doc

    def test_reconciliation_appends_exactly_once_and_flips_the_bit(self):
        doc = self.released_doc()
        self.sup.reconcile_auto_release_evidence(doc)
        events = self.lock_events("TASK-002")
        self.assertEqual(len(events), 1)
        meta = events[0]["metadata_redacted"]
        self.assertEqual(meta["released_by"], AUTO_BY)
        self.assertEqual(meta["released_at"], self.marker(doc)["at"])
        self.assertTrue(self.marker(doc)["evidenced"])
        # A second pass appends nothing.
        self.sup.reconcile_auto_release_evidence(doc)
        self.assertEqual(len(self.lock_events("TASK-002")), 1)

    def test_unrelated_lock_activity_cannot_destroy_repairability(self):
        doc = self.released_doc()
        # A different task legitimately acquires and releases before repair,
        # overwriting every mutable global lock field.
        self.assertTrue(migration_lock.acquire(doc, "TASK-003", TZ))
        self.assertTrue(migration_lock.release(doc, "TASK-003", "migration merged"))
        self.sup.reconcile_auto_release_evidence(doc)
        self.assertEqual(len(self.lock_events("TASK-002")), 1)
        # Repair never touches current ownership or the other task's record.
        self.assertEqual(migration_lock.state(doc), migration_lock.FREE)
        self.assertEqual(doc["migration_lock"]["last_release_reason"],
                         "migration merged")

    def test_ledger_failure_leaves_the_marker_unevidenced_for_retry(self):
        doc = self.released_doc()
        with mock.patch.object(self.sup, "log",
                               side_effect=OSError("no space left on device")):
            with self.assertRaises(OSError):
                self.sup.reconcile_auto_release_evidence(doc)
        self.assertFalse(self.marker(doc)["evidenced"])
        self.assertEqual(self.lock_events("TASK-002"), [])
        self.sup.reconcile_auto_release_evidence(doc)   # later tick converges
        self.assertEqual(len(self.lock_events("TASK-002")), 1)
        self.assertTrue(self.marker(doc)["evidenced"])

    def test_lost_evidenced_commit_converges_without_duplicate(self):
        doc = self.released_doc()
        self.sup.reconcile_auto_release_evidence(doc)
        # Simulate the evidenced=True state commit being lost after the append.
        self.marker(doc)["evidenced"] = False
        self.sup.reconcile_auto_release_evidence(doc)
        self.assertEqual(len(self.lock_events("TASK-002")), 1)
        self.assertTrue(self.marker(doc)["evidenced"])

    def test_repair_never_releases_or_transitions_anything(self):
        doc = self.released_doc()
        state_before = doc["tasks"]["TASK-002"]["state"]
        history_before = list(doc["tasks"]["TASK-002"]["history"])
        with mock.patch.object(supervisor_mod.migration_lock, "release") as rel:
            self.sup.reconcile_auto_release_evidence(doc)
        rel.assert_not_called()
        self.assertEqual(doc["tasks"]["TASK-002"]["state"], state_before)
        self.assertEqual(doc["tasks"]["TASK-002"]["history"], history_before)

    def test_no_marker_means_no_work(self):
        doc = base_doc()
        self.sup.reconcile_auto_release_evidence(doc)
        self.assertEqual(self.lock_events(), [])

    def test_the_tick_itself_performs_the_reconciliation(self):
        """The evidence pass must run from the ordinary tick transaction -
        pinning the call site, not just the method."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = state.Store(path=Path(tmp.name) / "state.json", tz=TZ)
        store.initialise("run-002", "v2.0")
        with store.transaction() as doc:
            for key, value in base_doc().items():
                doc[key] = value
            self.dispatch_fail(doc)
        self.sup.store = store
        with mock.patch.object(supervisor_mod.gh, "list_open_prs",
                               return_value=[]), \
                mock.patch.object(self.sup, "_write_heartbeat"):
            self.sup.tick()
        after = store.read()
        marker = after["tasks"]["TASK-002"]["migration_lock_auto_release"]
        self.assertTrue(marker["evidenced"])
        self.assertEqual(len(self.lock_events("TASK-002")), 1)


if __name__ == "__main__":
    unittest.main()
