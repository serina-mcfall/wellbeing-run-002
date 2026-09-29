"""C-05.2: browser finding classification, clearing, and truthful wording.

Two separate things the Watchdog has to get right about a browser finding.

CLASSIFICATION decides when it may be FORGOTTEN. A finding clears only
when its own resource class reports a complete scan and the finding is
absent from it. An unmapped check_id can never clear at all -
ORPHAN_CLASS.get returns None, scan_ok.get(None) is falsy - so a missing
mapping is not a harmless omission: it pins a resolved finding to the
state document for the rest of the run. All four browser check_ids must
therefore map, and clearing must stay strictly per-class in both
directions - a clean browser scan may not clear a worktree, and a clean
worktree scan may not clear a browser.

WORDING decides what a human is told. Three of the four browser findings
say ownership could not be DETERMINED; only ORPHAN_BROWSER_PROCESS says a
resource HAS NO OWNER. Those are different claims, and the notification
has to make the difference. A detector that reports unreadable evidence
as a confirmed leak trains its reader to ignore it, and then the real
orphan goes unread too - which is the failure this whole class of check
exists to prevent.

Both halves of a notification carry the claim, so both are asserted here.
Changing only the subject line and leaving "has no durable owner" in the
body would still be a lie, in the part a reader acts on.

The five pre-existing non-browser notifications are pinned verbatim: this
work must not have altered a single one of them.
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
    ledger as ledger_mod,
    notify,
    reconcile,
    state,
    watchdog,
)

TZ = "Pacific/Auckland"

BROWSER_CHECKS = (
    "BROWSER_EVIDENCE_UNREADABLE",
    "BROWSER_IDENTITY_UNVERIFIABLE",
    "BROWSER_TERMINALITY_UNKNOWN",
    "ORPHAN_BROWSER_PROCESS",
)
UNCERTAIN_CHECKS = BROWSER_CHECKS[:3]

# The five that existed before C-05.2, with the wording they had.
LEGACY_CHECKS = ("ORPHAN_WORKTREE", "ORPHAN_WORKER_PROCESS",
                 "ORPHAN_AGENT_PROCESS", "FOREIGN_OR_ORPHAN_LISTENER",
                 "PORT_ASSIGNMENT_CONFLICT")


class OrphanClassMappingCase(unittest.TestCase):
    """Pure table checks - no state, no ledger."""

    def test_every_browser_check_id_maps_to_the_browser_class(self):
        for check_id in BROWSER_CHECKS:
            with self.subTest(check_id=check_id):
                self.assertEqual(watchdog.ORPHAN_CLASS.get(check_id), "browser")

    def test_stale_sidecar_is_not_a_finding_and_is_not_mapped(self):
        # reconcile deliberately emits no such finding: a verified-dead
        # sidecar is benign leftover evidence, not a resource problem.
        self.assertNotIn("BROWSER_SIDECAR_STALE", watchdog.ORPHAN_CLASS)
        self.assertNotIn("BROWSER_SIDECAR_STALE", watchdog.ORPHAN_CLAIM_CHECKS)

    def test_no_browser_finding_can_be_left_unclearable(self):
        # The real risk is a check_id reconcile can emit that the table
        # does not know: it would never clear, and the occurrence would
        # outlive the condition for the rest of the run.
        mapped = {c for c, cls in watchdog.ORPHAN_CLASS.items()
                  if cls == "browser"}
        self.assertEqual(mapped, set(BROWSER_CHECKS))

    def test_only_the_orphan_browser_check_claims_ownerlessness(self):
        self.assertIn("ORPHAN_BROWSER_PROCESS", watchdog.ORPHAN_CLAIM_CHECKS)
        for check_id in UNCERTAIN_CHECKS:
            with self.subTest(check_id=check_id):
                self.assertNotIn(check_id, watchdog.ORPHAN_CLAIM_CHECKS)

    def test_an_unlisted_check_id_defaults_to_observation_wording(self):
        """The allow-list must fail toward understating a claim. A finding
        nobody remembered to classify is described as a reconciliation
        finding, never promoted to an orphan."""
        subject, body = watchdog._annunciation({
            "check_id": "SOME_FUTURE_CHECK", "resource_id": "r",
            "occurrence_id": "ORP-1"})
        self.assertTrue(subject.startswith("Resource reconciliation finding:"))
        self.assertNotIn("has no durable owner", body)


class BrowserAnnunciationCase(unittest.TestCase):
    """annunciate_orphans over a mocked detect_orphans."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = state.Store(path=root / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.notifier = mock.Mock(spec=notify.Notifier)
        self.notifier.send.return_value = {"ok": True}
        self.cfg = SimpleNamespace(timezone=TZ)
        self.all_ok = {"worktree": True, "process": True, "port": True,
                       "browser": True}

    @staticmethod
    def finding(check_id, resource_id="2222:777"):
        return reconcile.OrphanFinding(check_id, resource_id, "evidence text")

    def run_pass(self, findings, scan_ok=None):
        with mock.patch.object(watchdog.reconcile, "detect_orphans",
                               return_value=(findings, scan_ok or self.all_ok)):
            watchdog.annunciate_orphans(self.cfg, self.ledger, self.notifier,
                                        store=self.store)

    def entry(self, fingerprint):
        return self.store.read().get("orphan_annunciations", {}).get(fingerprint)

    def events(self, event_type):
        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line and
                json.loads(line).get("event_type") == event_type]

    def sent(self):
        """(subject, body) of the most recent notification."""
        args = self.notifier.send.call_args
        return args.args[1], args.args[2]

    # ------------------------------------------------------------- clearing

    def test_browser_finding_is_retained_while_the_browser_scan_is_incomplete(self):
        finding = self.finding("ORPHAN_BROWSER_PROCESS")
        self.run_pass([finding])
        fingerprint = "ORPHAN_BROWSER_PROCESS:2222:777"
        self.assertIsNotNone(self.entry(fingerprint))
        self.run_pass([], scan_ok=dict(self.all_ok, browser=False))
        self.assertIsNotNone(self.entry(fingerprint))

    def test_browser_finding_clears_on_a_complete_scan_that_omits_it(self):
        self.run_pass([self.finding("ORPHAN_BROWSER_PROCESS")])
        fingerprint = "ORPHAN_BROWSER_PROCESS:2222:777"
        self.assertIsNotNone(self.entry(fingerprint))
        self.run_pass([])
        self.assertIsNone(self.entry(fingerprint))

    def test_every_browser_check_id_can_actually_clear(self):
        for check_id in BROWSER_CHECKS:
            with self.subTest(check_id=check_id):
                self.setUp()
                self.run_pass([self.finding(check_id)])
                fingerprint = f"{check_id}:2222:777"
                self.assertIsNotNone(self.entry(fingerprint))
                self.run_pass([])
                self.assertIsNone(self.entry(fingerprint))

    def test_a_clean_scan_of_another_class_never_clears_a_browser_finding(self):
        self.run_pass([self.finding("ORPHAN_BROWSER_PROCESS")])
        self.run_pass([], scan_ok={"worktree": True, "process": True,
                                   "port": True, "browser": False})
        self.assertIsNotNone(self.entry("ORPHAN_BROWSER_PROCESS:2222:777"))

    def test_a_clean_browser_scan_never_clears_another_class(self):
        self.run_pass([self.finding("ORPHAN_WORKTREE", "/wt/orphan")])
        self.run_pass([], scan_ok={"worktree": False, "process": False,
                                   "port": False, "browser": True})
        self.assertIsNotNone(self.entry("ORPHAN_WORKTREE:/wt/orphan"))

    def test_recurrence_after_clearing_mints_a_new_occurrence(self):
        finding = self.finding("ORPHAN_BROWSER_PROCESS")
        self.run_pass([finding])
        first = self.entry("ORPHAN_BROWSER_PROCESS:2222:777")["occurrence_id"]
        self.run_pass([])
        self.run_pass([finding])
        self.assertNotEqual(
            self.entry("ORPHAN_BROWSER_PROCESS:2222:777")["occurrence_id"], first)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(len(self.events("ORPHAN_DETECTED")), 2)

    # -------------------------------------------------------------- wording

    def test_a_leaked_browser_is_announced_as_an_orphan(self):
        self.run_pass([self.finding("ORPHAN_BROWSER_PROCESS")])
        subject, body = self.sent()
        self.assertEqual(subject,
                         "Orphan resource detected: ORPHAN_BROWSER_PROCESS")
        self.assertIn("2222:777 has no durable owner", body)

    def test_uncertainty_findings_never_claim_an_orphan(self):
        for check_id in UNCERTAIN_CHECKS:
            with self.subTest(check_id=check_id):
                self.setUp()
                self.run_pass([self.finding(check_id)])
                subject, body = self.sent()
                self.assertEqual(
                    subject, f"Resource reconciliation finding: {check_id}")
                self.assertNotIn("Orphan resource detected", subject)
                # The body is the part a reader acts on. Fixing only the
                # subject would still tell them a leak was confirmed.
                self.assertNotIn("has no durable owner", body)
                self.assertNotIn("orphan", body.lower())
                self.assertIn("Ownership is UNKNOWN, not disproven", body)

    def test_uncertainty_findings_are_still_annunciated(self):
        # Neutral wording is not suppression: an observation gap is a real
        # condition and still reaches a human.
        for check_id in UNCERTAIN_CHECKS:
            with self.subTest(check_id=check_id):
                self.setUp()
                self.run_pass([self.finding(check_id)])
                self.assertEqual(self.notifier.send.call_count, 1)
                self.assertEqual(len(self.events("ORPHAN_DETECTED")), 1)

    def test_the_five_pre_existing_notifications_are_unchanged(self):
        for check_id in LEGACY_CHECKS:
            with self.subTest(check_id=check_id):
                self.setUp()
                self.run_pass([self.finding(check_id, "res-1")])
                subject, body = self.sent()
                occurrence = self.entry(f"{check_id}:res-1")["occurrence_id"]
                self.assertEqual(subject,
                                 f"Orphan resource detected: {check_id}")
                self.assertEqual(
                    body,
                    f"res-1 has no durable owner (occurrence {occurrence}). "
                    "Detection only - no automatic removal, kill, or repair "
                    "is performed.")

    # ------------------------------------------------------------- evidence

    def test_browser_detection_metadata_keeps_its_finite_shape(self):
        self.run_pass([self.finding("BROWSER_TERMINALITY_UNKNOWN")])
        meta = self.events("ORPHAN_DETECTED")[0]["metadata_redacted"]
        self.assertEqual(set(meta), {"fingerprint", "occurrence_id",
                                     "check_id", "resource_id",
                                     "first_observed_at"})
        self.assertEqual(meta["check_id"], "BROWSER_TERMINALITY_UNKNOWN")
        self.assertEqual(meta["resource_id"], "2222:777")


if __name__ == "__main__":
    unittest.main()
