"""C-05.2: browser orphan reconciliation and scan_ok["browser"].

A browser that is still running after every attempt that launched it has
finished has no durable owner. Detecting that is this suite's subject -
and so is the far larger set of situations where it must NOT be claimed.

Three claims are separable here, and the suite keeps them apart:

  * OWNERSHIP. Only a browser proven alive, whose every naming attempt is
    proven terminal, is an orphan. A RUNNING marker beats task state and
    is never overridden by it - a task can be COMPLETE while its last
    attempt's browser winds down, and preferring the task there reports a
    working browser as a leak. Task state is a fallback only where no
    marker exists, and only in the direction that proves termination.
  * ORDER INDEPENDENCE. One process can be named by several attempts that
    disagree about whether they finished. Ownership is aggregated over
    every record naming an identity, so directory order cannot pick the
    answer - deciding per record would silence a leak or invent one from
    identical evidence depending on which sorted first.
  * IDENTITY COVERAGE. Orphanhood is a claim about the ABSENCE of an
    owner, and absence cannot be proven from a partial roll-call. Any
    evidence that could hide another attempt's browser identity - an
    unwalkable tree, an unreadable sidecar, an OPEN sidecar naming no
    process - suppresses every orphan claim, even one otherwise fully
    proven. Uncertainty about a DIFFERENT but KNOWN identity does not: it
    hides nobody, so it spoils completeness without silencing a real leak.

A verified-dead sidecar is benign stale evidence: no finding, and it
spoils nothing. Every OrphanFinding reaches a human as an orphan alert,
and a leftover file is not a resource problem.

Detection only. Nothing here kills a process, closes a browser or removes
evidence, and one test holds the evidence tree to that byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_evidence, reconcile, state  # noqa: E402

TZ = "Pacific/Auckland"
SHA = "a" * 40
LIVE = (2222, 777)
OTHER = (3333, 888)


class BrowserReconcileCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.repo_root = "/repo"

    # ------------------------------------------------------------ fixtures

    def doc(self, task_state="ACTIVE", task_id="TASK001"):
        d = state.initial_document("run-002", "v2.0")
        if task_state is not None:
            task = state.add_task(d, task_id, "T", [], "feature", False, TZ)
            task["state"] = task_state
        return d

    @staticmethod
    def sidecar(pid=LIVE[0], ticks=LIVE[1], state_="OPEN"):
        return json.dumps({"pid": pid, "start_ticks": ticks, "state": state_,
                           "opened_at": "2026-09-29T00:00:00Z",
                           "closed_at": None})

    def attempt(self, *, task="TASK001", ordinal=1, sidecar=None, marker=None):
        path = self.evidence / task / SHA / f"attempt-{ordinal:04d}"
        (path / "accessibility").mkdir(parents=True)
        if sidecar is not None:
            (path / "accessibility" / "browser.json").write_text(
                sidecar, encoding="utf-8")
        if marker is not None:
            (path / marker).write_text("", encoding="utf-8")
        return path

    def run_detect(self, doc, *, alive_pairs=frozenset({LIVE}),
                   unverifiable=frozenset(), records=None):
        """detect_orphans with browser liveness resolved per identity.

        The C-09 harness patches verified_alive with one return value for
        every call; browser ownership needs a different answer per
        (pid, ticks), including the third answer None.
        """
        def _verified(pid, ticks):
            if (pid, ticks) in unverifiable:
                return None
            return (pid, ticks) in alive_pairs

        git_result = mock.Mock(ok=True, stdout=f"worktree {self.repo_root}")
        scan = (mock.patch.object(reconcile.gate_evidence, "scan_sidecars",
                                  lambda root=None: records)
                if records is not None
                else mock.patch.object(reconcile.config, "EVIDENCE_DIR",
                                       self.evidence))
        with mock.patch.object(reconcile.gh, "git", return_value=git_result), \
                mock.patch.object(reconcile.proc, "worker_entry_processes",
                                  return_value={}), \
                mock.patch.object(reconcile.proc, "verified_alive", _verified), \
                mock.patch.object(reconcile.proc, "listening_ports",
                                  return_value=set()), \
                mock.patch.object(reconcile.hostcheck,
                                  "read_candidate_port_range",
                                  return_value=(3200, 3299)), \
                scan:
            return reconcile.detect_orphans(doc, repo_root=self.repo_root)

    @staticmethod
    def ids(findings):
        return sorted((f.check_id, f.resource_id) for f in findings)

    @staticmethod
    def kinds(findings):
        return {f.check_id for f in findings}

    def synthetic(self, *, pid=LIVE[0], ticks=LIVE[1], lifecycle=None,
                  task="TASK001", attempt="a", status=None, state_="OPEN"):
        """One scan record, built directly so record ORDER is controllable -
        a real walk is always sorted, so order-independence cannot be
        tested through the filesystem."""
        return {"sidecar_status": status or gate_evidence.SIDECAR_OK,
                "state": state_, "pid": pid, "start_ticks": ticks,
                "attempt_dir": f"{self.evidence}/{task}/{SHA}/attempt-{attempt}",
                "sidecar_path": f"{self.evidence}/{task}/x/browser.json",
                "lifecycle": lifecycle, "task_id": task, "sha": SHA}

    # ------------------------------------------------- single-identity rules

    def test_closed_sidecar_is_no_finding_and_a_complete_scan(self):
        self.attempt(sidecar=self.sidecar(state_="CLOSED"))
        findings, scan_ok = self.run_detect(self.doc())
        self.assertEqual(self.kinds(findings), set())
        self.assertTrue(scan_ok["browser"])

    def test_running_attempt_with_a_live_browser_is_not_an_orphan(self):
        self.attempt(sidecar=self.sidecar(),
                     marker=gate_evidence.RUNNING_MARKER)
        findings, scan_ok = self.run_detect(self.doc())
        self.assertEqual(self.kinds(findings), set())
        self.assertTrue(scan_ok["browser"])

    def test_terminal_attempt_with_a_live_browser_is_an_orphan(self):
        self.attempt(sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        findings, scan_ok = self.run_detect(self.doc())
        self.assertEqual(self.ids(findings),
                         [("ORPHAN_BROWSER_PROCESS", "2222:777")])
        self.assertTrue(scan_ok["browser"])

    def test_no_marker_with_a_terminal_task_proves_terminality(self):
        for task_state in ("COMPLETE", "FAILED"):
            with self.subTest(task_state=task_state):
                self.setUp()
                self.attempt(sidecar=self.sidecar())
                findings, scan_ok = self.run_detect(self.doc(task_state))
                self.assertEqual(self.ids(findings),
                                 [("ORPHAN_BROWSER_PROCESS", "2222:777")])
                self.assertTrue(scan_ok["browser"])

    def test_no_marker_with_a_nonterminal_task_is_unknown_not_an_orphan(self):
        # ACTIVE does not prove the ATTEMPT is still running, so it proves
        # nothing in either direction.
        self.attempt(sidecar=self.sidecar())
        findings, scan_ok = self.run_detect(self.doc("ACTIVE"))
        self.assertEqual(self.ids(findings),
                         [("BROWSER_TERMINALITY_UNKNOWN", "2222:777")])
        self.assertFalse(scan_ok["browser"])

    def test_no_marker_with_an_absent_task_is_unknown_not_an_orphan(self):
        # Evidence outliving its task is exactly where guessing is least
        # safe; an absent task never proves an attempt finished.
        self.attempt(sidecar=self.sidecar())
        findings, scan_ok = self.run_detect(self.doc(task_state=None))
        self.assertEqual(self.ids(findings),
                         [("BROWSER_TERMINALITY_UNKNOWN", "2222:777")])
        self.assertFalse(scan_ok["browser"])

    def test_running_marker_is_not_overridden_by_a_complete_task(self):
        self.attempt(sidecar=self.sidecar(),
                     marker=gate_evidence.RUNNING_MARKER)
        findings, scan_ok = self.run_detect(self.doc("COMPLETE"))
        self.assertEqual(self.kinds(findings), set())
        self.assertTrue(scan_ok["browser"])

    # ------------------------------------------------------- dead and stale

    def test_verified_dead_browser_is_benign_stale_evidence(self):
        self.attempt(sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        findings, scan_ok = self.run_detect(self.doc(), alive_pairs=frozenset())
        self.assertEqual(self.kinds(findings), set())
        self.assertTrue(scan_ok["browser"])

    def test_duplicate_dead_records_stay_benign(self):
        self.attempt(ordinal=1, sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        self.attempt(ordinal=2, sidecar=self.sidecar())
        findings, scan_ok = self.run_detect(self.doc(), alive_pairs=frozenset())
        self.assertEqual(self.kinds(findings), set())
        self.assertTrue(scan_ok["browser"])

    # -------------------------------------------------- unverifiable inputs

    def test_unverifiable_liveness_is_reported_against_the_identity(self):
        self.attempt(sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        findings, scan_ok = self.run_detect(self.doc(), unverifiable={LIVE})
        self.assertEqual(self.ids(findings),
                         [("BROWSER_IDENTITY_UNVERIFIABLE", "2222:777")])
        self.assertFalse(scan_ok["browser"])

    def test_open_sidecar_naming_no_process_is_reported_against_the_attempt(self):
        # There is no process to name, so the attempt is the only honest
        # resource identifier available.
        path = self.attempt(sidecar=self.sidecar(pid=None),
                            marker=gate_evidence.TERMINAL_MARKER)
        findings, scan_ok = self.run_detect(self.doc())
        self.assertEqual(self.ids(findings),
                         [("BROWSER_IDENTITY_UNVERIFIABLE", str(path))])
        self.assertFalse(scan_ok["browser"])

    def test_unreadable_sidecars_are_reported_against_the_attempt(self):
        for label in ("missing", "directory", "malformed"):
            with self.subTest(sidecar=label):
                self.setUp()
                path = self.attempt(
                    sidecar=None if label == "missing"
                    else ('{"state": "OPEN"' if label == "malformed"
                          else self.sidecar()))
                if label == "directory":
                    target = path / "accessibility" / "browser.json"
                    target.unlink()
                    target.mkdir()
                findings, scan_ok = self.run_detect(self.doc())
                self.assertEqual(self.ids(findings),
                                 [("BROWSER_EVIDENCE_UNREADABLE", str(path))])
                self.assertFalse(scan_ok["browser"])

    def test_an_unwalkable_tree_is_reported_against_the_evidence_root(self):
        self.attempt(sidecar=self.sidecar())
        (self.evidence / "TASK002").symlink_to(self.evidence / "TASK001")
        findings, scan_ok = self.run_detect(self.doc())
        self.assertIn(("BROWSER_EVIDENCE_UNREADABLE", str(self.evidence)),
                      self.ids(findings))
        self.assertFalse(scan_ok["browser"])

    # ------------------------------------------------- identity coverage

    def test_evidence_that_could_hide_an_owner_suppresses_a_proven_orphan(self):
        """An attempt whose browser identity we never learned may be a
        CURRENT owner of this very process, so "no attempt owns it" is not
        proven. The coverage failure is reported; the orphan is withheld."""
        for label in ("missing", "directory", "malformed", "no-pid"):
            with self.subTest(hidden_by=label):
                self.setUp()
                self.attempt(ordinal=1, sidecar=self.sidecar(),
                             marker=gate_evidence.TERMINAL_MARKER)
                path = self.attempt(
                    ordinal=2,
                    sidecar={"missing": None,
                             "malformed": '{"state": "OPEN"',
                             "directory": self.sidecar(),
                             "no-pid": self.sidecar(pid=None)}[label])
                if label == "directory":
                    target = path / "accessibility" / "browser.json"
                    target.unlink()
                    target.mkdir()
                findings, scan_ok = self.run_detect(self.doc())
                self.assertTrue(
                    {"BROWSER_EVIDENCE_UNREADABLE",
                     "BROWSER_IDENTITY_UNVERIFIABLE"} & self.kinds(findings))
                self.assertNotIn("ORPHAN_BROWSER_PROCESS", self.kinds(findings))
                self.assertFalse(scan_ok["browser"])

    def test_an_unverifiable_sibling_identity_does_not_suppress_an_orphan(self):
        """A DIFFERENT but KNOWN identity being unverifiable poisons
        completeness - it hides nobody, so withholding the proven orphan
        would lose a real leak."""
        self.attempt(ordinal=1, sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        self.attempt(ordinal=2, sidecar=self.sidecar(*OTHER),
                     marker=gate_evidence.TERMINAL_MARKER)
        findings, scan_ok = self.run_detect(
            self.doc(), alive_pairs={LIVE, OTHER}, unverifiable={OTHER})
        self.assertIn(("ORPHAN_BROWSER_PROCESS", "2222:777"), self.ids(findings))
        self.assertIn(("BROWSER_IDENTITY_UNVERIFIABLE", "3333:888"),
                      self.ids(findings))
        self.assertFalse(scan_ok["browser"])

    def test_an_unsettled_sibling_identity_does_not_suppress_an_orphan(self):
        self.attempt(ordinal=1, sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        self.attempt(ordinal=2, sidecar=self.sidecar(*OTHER))  # no marker
        findings, scan_ok = self.run_detect(
            self.doc("ACTIVE"), alive_pairs={LIVE, OTHER})
        self.assertIn(("ORPHAN_BROWSER_PROCESS", "2222:777"), self.ids(findings))
        self.assertIn(("BROWSER_TERMINALITY_UNKNOWN", "3333:888"),
                      self.ids(findings))
        self.assertFalse(scan_ok["browser"])

    # --------------------------------- duplicate identity aggregation (A-D)

    def _both_orders(self, records, doc, **kwargs):
        forward = self.run_detect(doc, records=(list(records), True), **kwargs)
        reverse = self.run_detect(doc, records=(list(reversed(records)), True),
                                  **kwargs)
        return forward, reverse

    def test_a_running_and_terminal_naming_one_browser_is_owned(self):
        records = [self.synthetic(lifecycle=gate_evidence.RUNNING, attempt="1"),
                   self.synthetic(lifecycle=gate_evidence.TERMINAL, attempt="2")]
        for label, (findings, scan_ok) in zip(
                ("forward", "reverse"), self._both_orders(records, self.doc())):
            with self.subTest(order=label):
                self.assertEqual(self.kinds(findings), set())
                self.assertTrue(scan_ok["browser"])

    def test_b_terminal_and_unknown_naming_one_browser_is_not_an_orphan(self):
        records = [self.synthetic(lifecycle=gate_evidence.TERMINAL, attempt="1"),
                   self.synthetic(lifecycle=None, attempt="2")]
        for label, (findings, scan_ok) in zip(
                ("forward", "reverse"),
                self._both_orders(records, self.doc("ACTIVE"))):
            with self.subTest(order=label):
                self.assertEqual(self.ids(findings),
                                 [("BROWSER_TERMINALITY_UNKNOWN", "2222:777")])
                self.assertFalse(scan_ok["browser"])

    def test_c_two_terminal_attempts_yield_exactly_one_orphan(self):
        records = [self.synthetic(lifecycle=gate_evidence.TERMINAL, attempt="1"),
                   self.synthetic(lifecycle=gate_evidence.TERMINAL, attempt="2")]
        for label, (findings, scan_ok) in zip(
                ("forward", "reverse"), self._both_orders(records, self.doc())):
            with self.subTest(order=label):
                self.assertEqual(self.ids(findings),
                                 [("ORPHAN_BROWSER_PROCESS", "2222:777")])
                self.assertEqual(
                    len([f for f in findings
                         if f.check_id == "ORPHAN_BROWSER_PROCESS"]), 1)
                self.assertTrue(scan_ok["browser"])

    def test_d_reversing_duplicate_records_changes_nothing(self):
        cases = {
            "running+terminal": ([gate_evidence.RUNNING,
                                  gate_evidence.TERMINAL], "COMPLETE"),
            "terminal+unknown": ([gate_evidence.TERMINAL, None], "ACTIVE"),
            "terminal+terminal": ([gate_evidence.TERMINAL,
                                   gate_evidence.TERMINAL], "COMPLETE"),
            "unknown+running": ([None, gate_evidence.RUNNING], "ACTIVE"),
        }
        for label, (lifecycles, task_state) in cases.items():
            with self.subTest(case=label):
                records = [self.synthetic(lifecycle=lc, attempt=str(i))
                           for i, lc in enumerate(lifecycles)]
                (f_find, f_ok), (r_find, r_ok) = self._both_orders(
                    records, self.doc(task_state))
                self.assertEqual(self.ids(f_find), self.ids(r_find))
                self.assertEqual(f_ok, r_ok)

    # --------------------------------------------------- detection only

    def test_detection_never_touches_a_browser_or_its_evidence(self):
        self.attempt(ordinal=1, sidecar=self.sidecar(),
                     marker=gate_evidence.TERMINAL_MARKER)
        self.attempt(ordinal=2, sidecar=self.sidecar(state_="CLOSED"))

        def digest():
            return sorted(
                (str(p.relative_to(self.evidence)),
                 hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(self.evidence.rglob("*")) if p.is_file())

        before = digest()
        findings, _ = self.run_detect(self.doc())
        self.assertEqual(self.ids(findings),
                         [("ORPHAN_BROWSER_PROCESS", "2222:777")])
        self.assertEqual(digest(), before)
        # Annunciation policy is the Watchdog's; reconciliation states
        # facts. No termination primitive is reachable from this module.
        for banned in ("kill", "terminate", "unlink", "rmtree", "close"):
            self.assertFalse(hasattr(reconcile, banned), banned)


if __name__ == "__main__":
    unittest.main()
