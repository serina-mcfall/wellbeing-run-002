"""C-10.2: the accepted non-blocking debt register.

Two halves, matching the two trust boundaries control/debt.py guards:

  * retain_accepted() sees fresh reviewer output and may refuse. Its refusals
    happen at the approving review, while the PR is not yet merge-eligible —
    the only moment at which refusing a malformed finding identity is free.
  * accept() sees stored state after gh.merge() has already run. It may never
    raise, because raising would roll back the transaction, lose
    record["merged"], and leave the next tick staring at a closed pull request
    it can never complete. The all-or-nothing and never-raises properties below
    are what make that safe.
"""

from __future__ import annotations

import copy
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    config,
    debt,
    providers,
    routing,
    state,
    supervisor as supervisor_mod,
)

TZ = "Pacific/Auckland"
PR = 7
BRANCH = "task/task-001"
TASK = "TASK-001"
SHA = "a" * 40
AT = "2026-09-26T12:00:00+12:00"

# Long enough to trip control/redact.py's \bgh[pousr]_[A-Za-z0-9]{16,} and short
# enough NOT to trip scripts/check_no_secrets.py's stricter {30,}. This file is
# tracked and that CI guardrail scans tracked files; lengthening this breaks CI.
SECRET_SHAPED = "ghp_" + "A" * 20

UI_PASS = {"OVERENGINEERING": "PASS", "COGNITIVE_LOAD": "PASS", "SENSORY_LOAD": "PASS"}


def _finding(fid="F1", severity="P2", category="TESTS", summary="a small thing", **extra):
    out = {"id": fid, "severity": severity, "category": category, "summary": summary}
    out.update(extra)
    return out


def _payload(findings=None, gates=None, verdict="REVIEW_PASS"):
    return {"verdict": verdict, "gates": gates or dict(UI_PASS),
            "findings": findings if findings is not None else []}


def _review(findings=None, gates=None, verdict="REVIEW_PASS"):
    text = "```json\n" + json.dumps(_payload(findings, gates, verdict)) + "\n```"
    return routing.parse_review(text)


def _entry(fid="F1", severity="P2", category="TESTS", summary="a small thing"):
    return {"id": fid, "severity": severity, "category": category, "summary": summary}


def _doc():
    return state.initial_document("run-002", "2.0")


# ------------------------------------------------------------------ retention


class TestRetainAccepted(unittest.TestCase):
    def test_valid_p2_is_retained(self):
        result = debt.retain_accepted(_review([_finding(severity="P2")]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.entries[0]["severity"], "P2")

    def test_valid_p3_is_retained(self):
        result = debt.retain_accepted(_review([_finding(severity="P3")]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.entries[0]["severity"], "P3")

    def test_multiple_are_retained_in_order(self):
        result = debt.retain_accepted(_review([
            _finding("F1", "P2", "TESTS"), _finding("F2", "P3", "SCOPE"),
            _finding("F3", "P2", "PRIVACY")]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual([e["id"] for e in result.entries], ["F1", "F2", "F3"])

    def test_entry_carries_exactly_the_four_reduced_fields(self):
        result = debt.retain_accepted(_review([
            _finding(file="app/page.tsx", evidence="lots of it",
                     required_change="do the thing", extra="unexpected")]))
        self.assertEqual(set(result.entries[0]), {"id", "severity", "category", "summary"})

    def test_summary_is_scrubbed(self):
        result = debt.retain_accepted(_review([
            _finding(summary=f"token {SECRET_SHAPED} was committed")]))
        self.assertTrue(result.ok, result.reason)
        self.assertNotIn(SECRET_SHAPED, result.entries[0]["summary"])
        self.assertIn("redacted", result.entries[0]["summary"])

    def test_a_scrubbed_summary_is_still_valid_evidence(self):
        """Scrubbing must never be the reason a merge is refused."""
        result = debt.retain_accepted(_review([_finding(summary=SECRET_SHAPED)]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(result.entries), 1)

    def test_summary_is_bounded(self):
        result = debt.retain_accepted(_review([_finding(summary="x" * 5000)]))
        self.assertEqual(len(result.entries[0]["summary"]), debt.SUMMARY_MAX)

    def test_absent_or_non_string_summary_is_coerced(self):
        for summary in (None, 42, ["a", "b"], {"k": "v"}):
            with self.subTest(summary=summary):
                finding = _finding()
                if summary is None:
                    finding.pop("summary")
                else:
                    finding["summary"] = summary
                result = debt.retain_accepted(_review([finding]))
                self.assertTrue(result.ok, result.reason)
                self.assertIsInstance(result.entries[0]["summary"], str)

    def test_duplicate_id_is_refused(self):
        result = debt.retain_accepted(_review([
            _finding("F1", "P2", "TESTS"), _finding("F1", "P3", "SCOPE")]))
        self.assertFalse(result.ok)
        self.assertIn("duplicate finding id F1", result.reason)
        self.assertEqual(result.entries, ())

    def test_blank_or_unusable_id_is_refused(self):
        for bad in ("", "   ", None, 7, [], {}):
            with self.subTest(id=bad):
                finding = _finding()
                finding["id"] = bad
                result = debt.retain_accepted(_review([finding]))
                self.assertFalse(result.ok)
                self.assertIn("id", result.reason)

    def test_an_absent_id_key_is_filled_by_the_parser_before_retention(self):
        """routing.parse_review setdefaults a positional id, so retention never
        sees an absent key — only an explicitly unusable value. Asserted here so
        the division of labour is recorded rather than assumed."""
        finding = _finding()
        finding.pop("id")
        review = _review([finding])
        self.assertEqual(review.findings[0]["id"], "F1")
        self.assertTrue(debt.retain_accepted(review).ok)

    def test_blocking_severity_is_refused_never_dropped(self):
        for severity in ("P0", "P1"):
            with self.subTest(severity=severity):
                result = debt.retain_accepted(_review([_finding(severity=severity)]))
                self.assertFalse(result.ok)
                self.assertIn("cannot be accepted", result.reason)
                self.assertEqual(result.entries, ())

    def test_unknown_category_is_refused(self):
        result = debt.retain_accepted(_review([_finding(category="VIBES")]))
        self.assertFalse(result.ok)
        self.assertIn("unrecognised category", result.reason)

    def test_empty_findings_is_an_empty_accepted_set(self):
        result = debt.retain_accepted(_review([]))
        self.assertTrue(result.ok)
        self.assertEqual(result.entries, ())


# ----------------------------------------------------------------- acceptance


class TestAccept(unittest.TestCase):
    def accept(self, doc, entries, *, task_id=TASK, pr_number=PR,
               merged_sha=SHA, at=AT):
        return debt.accept(doc, task_id=task_id, pr_number=pr_number,
                           accepted_findings=entries, merged_sha=merged_sha, at=at)

    def test_one_accepted_finding_becomes_one_record(self):
        doc = _doc()
        result = self.accept(doc, [_entry()])
        self.assertEqual(result.status, debt.RECORDED)
        self.assertIsNone(result.error)
        self.assertEqual(result.debt_ids, (f"{TASK}-PR{PR}-F1",))
        self.assertEqual(doc["debt"][f"{TASK}-PR{PR}-F1"], {
            "id": f"{TASK}-PR{PR}-F1", "finding_id": "F1", "task_id": TASK,
            "pr_number": PR, "severity": "P2", "category": "TESTS",
            "summary": "a small thing", "status": "ACCEPTED_NONBLOCKING",
            "accepted_at": AT, "merged_sha": SHA})

    def test_multiple_become_separate_records(self):
        doc = _doc()
        result = self.accept(doc, [_entry("F1"), _entry("F2", "P3", "SCOPE")])
        self.assertEqual(result.status, debt.RECORDED)
        self.assertEqual(len(doc["debt"]), 2)
        self.assertEqual(set(result.debt_ids),
                         {f"{TASK}-PR{PR}-F1", f"{TASK}-PR{PR}-F2"})

    def test_absent_accepted_findings_is_not_required(self):
        doc = _doc()
        result = self.accept(doc, None)
        self.assertEqual(result.status, debt.NOT_REQUIRED)
        self.assertIsNone(result.error)
        self.assertEqual(doc["debt"], {})

    def test_empty_accepted_findings_is_not_required(self):
        doc = _doc()
        self.assertEqual(self.accept(doc, []).status, debt.NOT_REQUIRED)
        self.assertEqual(doc["debt"], {})

    def test_rerun_is_idempotent_and_preserves_the_original_timestamp(self):
        doc = _doc()
        self.accept(doc, [_entry()], at=AT)
        before = copy.deepcopy(doc["debt"])

        again = self.accept(doc, [_entry()], at="2026-09-26T23:59:59+12:00")
        self.assertEqual(again.status, debt.RECORDED)
        self.assertEqual(again.debt_ids, (f"{TASK}-PR{PR}-F1",))
        self.assertEqual(doc["debt"], before)
        self.assertEqual(len(doc["debt"]), 1)

    def test_two_prs_reporting_f1_produce_two_records(self):
        doc = _doc()
        self.accept(doc, [_entry()], pr_number=7)
        self.accept(doc, [_entry()], pr_number=8)
        self.assertEqual(len(doc["debt"]), 2)

    def test_missing_merged_sha_is_recorded_as_none(self):
        doc = _doc()
        result = self.accept(doc, [_entry()], merged_sha=None)
        self.assertEqual(result.status, debt.RECORDED)
        self.assertIsNone(doc["debt"][f"{TASK}-PR{PR}-F1"]["merged_sha"])

    # ------------------------------------------------------------ error codes
    def test_malformed_retained_set(self):
        for bad in ({"a": 1}, "F1", 7):
            with self.subTest(value=bad):
                doc = _doc()
                result = self.accept(doc, bad)
                self.assertEqual((result.status, result.error),
                                 (debt.FAILED, "MALFORMED_RETAINED_SET"))
                self.assertEqual(doc["debt"], {})

    def test_malformed_retained_entry_is_a_malformed_set(self):
        doc = _doc()
        result = self.accept(doc, ["not a dict"])
        self.assertEqual(result.error, "MALFORMED_RETAINED_SET")

    def test_invalid_retained_entry(self):
        cases = [
            {"severity": "P2", "category": "TESTS", "summary": "s"},          # no id
            _entry(severity="P1"),
            _entry(severity="NOPE"),
            _entry(category="VIBES"),
            {"id": "F1", "severity": "P2", "category": "TESTS", "summary": 9},
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                doc = _doc()
                result = self.accept(doc, [entry])
                self.assertEqual((result.status, result.error),
                                 (debt.FAILED, "INVALID_RETAINED_ENTRY"))
                self.assertEqual(doc["debt"], {})

    def test_duplicate_finding_id(self):
        doc = _doc()
        result = self.accept(doc, [_entry("F1"), _entry("F1", "P3", "SCOPE")])
        self.assertEqual((result.status, result.error),
                         (debt.FAILED, "DUPLICATE_FINDING_ID"))
        self.assertEqual(doc["debt"], {})

    def test_debt_key_conflict(self):
        doc = _doc()
        self.accept(doc, [_entry(summary="original")])
        before = copy.deepcopy(doc["debt"])

        result = self.accept(doc, [_entry(summary="something else entirely")])
        self.assertEqual((result.status, result.error),
                         (debt.FAILED, "DEBT_KEY_CONFLICT"))
        self.assertEqual(doc["debt"], before)

    def test_missing_merge_context(self):
        for kwargs in ({"task_id": ""}, {"task_id": "   "}, {"task_id": None},
                       {"pr_number": None}, {"pr_number": "7"}):
            with self.subTest(**kwargs):
                doc = _doc()
                result = self.accept(doc, [_entry()], **kwargs)
                self.assertEqual((result.status, result.error),
                                 (debt.FAILED, "MISSING_MERGE_CONTEXT"))
                self.assertEqual(doc["debt"], {})

    def test_unexpected_is_caught_and_never_raises(self):
        doc = _doc()
        with mock.patch.object(debt, "_record", side_effect=RuntimeError("boom")):
            result = self.accept(doc, [_entry()])
        self.assertEqual((result.status, result.error), (debt.FAILED, "UNEXPECTED"))
        self.assertEqual(doc["debt"], {})

    def test_every_error_is_from_the_finite_vocabulary(self):
        doc = _doc()
        for entries in (["x"], [_entry(severity="P1")], [_entry(), _entry()], 7):
            result = self.accept(doc, entries)
            if result.error is not None:
                self.assertIn(result.error, debt.ERROR_CODES)

    # ------------------------------------------------------------ invariants
    def test_all_or_nothing_writes_no_partial_set(self):
        doc = _doc()
        before = copy.deepcopy(doc["debt"])
        result = self.accept(doc, [_entry("F1"), _entry("F2", severity="P1"),
                                   _entry("F3")])
        self.assertEqual(result.status, debt.FAILED)
        self.assertEqual(doc["debt"], before)
        self.assertEqual(doc["debt"], {})

    def test_all_or_nothing_leaves_prior_debt_untouched(self):
        doc = _doc()
        self.accept(doc, [_entry("F1")], pr_number=1)
        before = copy.deepcopy(doc["debt"])
        self.accept(doc, [_entry("F9"), _entry("F9")], pr_number=2)
        self.assertEqual(doc["debt"], before)

    def test_never_raises_on_hostile_input(self):
        doc = _doc()
        for entries in (7, "text", {"a": 1}, [None], [[]], [{"id": {}}],
                        [{"id": "F1", "severity": None, "category": None,
                          "summary": None}]):
            with self.subTest(entries=entries):
                result = debt.accept(doc, task_id=TASK, pr_number=PR,
                                     accepted_findings=entries,
                                     merged_sha=SHA, at=AT)
                self.assertIn(result.status, (debt.FAILED, debt.NOT_REQUIRED))

    def test_legacy_document_without_a_debt_key(self):
        doc = _doc()
        del doc["debt"]
        result = self.accept(doc, [_entry()])
        self.assertEqual(result.status, debt.RECORDED)
        self.assertEqual(len(doc["debt"]), 1)

    def test_legacy_document_with_no_accepted_findings_does_not_crash(self):
        doc = _doc()
        del doc["debt"]
        self.assertEqual(self.accept(doc, None).status, debt.NOT_REQUIRED)
        self.assertNotIn("debt", doc)

    def test_nothing_outside_the_register_is_mutated(self):
        doc = _doc()
        before = copy.deepcopy({k: v for k, v in doc.items() if k != "debt"})
        self.accept(doc, [_entry()])
        after = {k: v for k, v in doc.items() if k != "debt"}
        self.assertEqual(after, before)

    def test_debt_is_not_written_into_interventions(self):
        doc = _doc()
        self.accept(doc, [_entry()])
        self.assertEqual(doc["interventions"], {})


# ---------------------------------------------------------------- integration


class SupervisorCase(unittest.TestCase):
    """Mirrors tests/test_dispatch_invariant.py's harness: a real Supervisor
    with its outbound edges stubbed."""

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)
        self.events: list[tuple[str, dict]] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def event(self, name):
        for event, fields in self.events:
            if event == name:
                return fields
        return None

    def doc_at_review(self, *, review_cycles=1, task_state="REVIEW"):
        doc = state.initial_document("run-002", "2.0")
        providers.ensure(doc)
        task = state.add_task(doc, TASK, "Foundation", [], "feature", False, TZ)
        task.update({"state": task_state, "branch": BRANCH, "pr": PR, "worker": "w1"})
        record = routing.blank_pr_record(PR, TASK, BRANCH)
        record["review_cycles"] = review_cycles
        doc["prs"][str(PR)] = record
        return doc

    def run_review(self, doc, payload):
        text = "```json\n" + json.dumps(payload) + "\n```"
        with mock.patch.object(supervisor_mod.workers, "worker_output",
                               return_value=text), \
                mock.patch.object(supervisor_mod.routing, "touches_ui",
                                  return_value=False), \
                mock.patch.object(supervisor_mod.config, "WORKER_LOG_DIR",
                                  Path("/nonexistent-worker-log-dir")), \
                mock.patch.object(self.sup, "release_review_worktree"), \
                mock.patch.object(self.sup, "dispatch_fixer"):
            self.sup.on_reviewer_finished(doc, "w1", {"task_id": TASK, "pr": PR},
                                          {"outcome": "SUCCESS"})

    def run_merge(self, doc, *, allowed=True, merge_ok=True, merged_sha=SHA,
                  pr_view=True):
        task = doc["tasks"][TASK]
        record = doc["prs"][str(PR)]
        pr = {"number": PR, "state": "OPEN", "isDraft": False,
              "mergeStateStatus": "CLEAN"}
        view = {"mergeCommit": {"oid": merged_sha}} if pr_view else None
        with mock.patch.object(supervisor_mod.providers, "may", return_value=True), \
                mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                  return_value="h"), \
                mock.patch.object(supervisor_mod.routing, "evaluate_merge",
                                  return_value=routing.MergeDecision(allowed, "reason")), \
                mock.patch.object(supervisor_mod.gh, "merge",
                                  return_value=types.SimpleNamespace(
                                      ok=merge_ok, stderr="", stdout="")), \
                mock.patch.object(supervisor_mod.gh, "pr_view", return_value=view), \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            self.sup.attempt_merge(doc, task, pr, record)


class TestPassPathRetention(SupervisorCase):
    def test_valid_pass_stores_the_accepted_set(self):
        doc = self.doc_at_review()
        self.run_review(doc, _payload([_finding("F1", "P2", "TESTS")]))
        record = doc["prs"][str(PR)]
        self.assertEqual(record["review_verdict"], routing.REVIEW_PASS)
        self.assertTrue(record["approval_current"])
        self.assertEqual(record["accepted_findings"], [_entry("F1", "P2", "TESTS")])

    def test_pass_with_no_findings_stores_an_empty_set(self):
        doc = self.doc_at_review()
        self.run_review(doc, _payload([]))
        self.assertEqual(doc["prs"][str(PR)]["accepted_findings"], [])

    def test_duplicate_id_pass_is_rejected_before_merge_eligibility(self):
        doc = self.doc_at_review()
        self.run_review(doc, _payload([_finding("F1", "P2", "TESTS"),
                                       _finding("F1", "P3", "SCOPE")]))
        record = doc["prs"][str(PR)]
        self.assertFalse(record["approval_current"])
        self.assertEqual(record["review_verdict"], routing.REVIEW_FAIL)
        self.assertNotIn("accepted_findings", record)
        self.assertEqual(doc["tasks"][TASK]["state"], "PR_OPEN")
        self.assertIn("duplicate finding id F1",
                      self.event("REVIEW_REJECTED_BY_SUPERVISOR")["metadata_redacted"]["reason"])

    def test_duplicate_id_pass_at_the_cycle_limit_escalates(self):
        doc = self.doc_at_review(review_cycles=self.cfg.max_repair_cycles)
        self.run_review(doc, _payload([_finding("F1"), _finding("F1")]))
        self.assertEqual(doc["tasks"][TASK]["state"], "HUMAN_REQUIRED")
        self.assertFalse(doc["prs"][str(PR)]["approval_current"])

    def test_a_retention_rejected_pr_is_not_merge_eligible(self):
        doc = self.doc_at_review()
        self.run_review(doc, _payload([_finding("F1"), _finding("F1")]))
        decision = routing.evaluate_merge(
            {"number": PR, "state": "OPEN", "isDraft": False},
            doc["prs"][str(PR)], ("ci",), False, "h")
        self.assertFalse(decision.allowed)
        self.assertIn("REVIEW_PASS", decision.reason)

    def test_stale_pending_findings_are_not_mixed_into_the_accepted_set(self):
        doc = self.doc_at_review()
        doc["prs"][str(PR)]["pending_findings"] = [
            {"id": "F9", "severity": "P1", "category": "SECURITY", "summary": "old"}]
        self.run_review(doc, _payload([_finding("F1", "P2", "TESTS")]))
        self.assertEqual([e["id"] for e in doc["prs"][str(PR)]["accepted_findings"]],
                         ["F1"])

    def test_review_pass_alone_creates_no_debt(self):
        doc = self.doc_at_review()
        self.run_review(doc, _payload([_finding("F1", "P2", "TESTS")]))
        self.assertEqual(doc["debt"], {})


class TestMergePathDebt(SupervisorCase):
    def merged_doc(self, findings=None):
        doc = self.doc_at_review()
        self.run_review(doc, _payload(findings if findings is not None
                                      else [_finding("F1", "P2", "TESTS")]))
        self.events.clear()
        return doc

    def test_merge_success_records_debt(self):
        doc = self.merged_doc()
        self.run_merge(doc)
        record = doc["prs"][str(PR)]
        self.assertEqual(record["debt_recording_status"], debt.RECORDED)
        self.assertNotIn("debt_recording_error", record)
        self.assertEqual(list(doc["debt"]), [f"{TASK}-PR{PR}-F1"])
        stored = doc["debt"][f"{TASK}-PR{PR}-F1"]
        self.assertEqual(stored["task_id"], TASK)
        self.assertEqual(stored["pr_number"], PR)
        self.assertEqual(stored["severity"], "P2")
        self.assertEqual(stored["category"], "TESTS")
        self.assertEqual(stored["status"], "ACCEPTED_NONBLOCKING")
        self.assertEqual(stored["merged_sha"], SHA)
        self.assertTrue(stored["accepted_at"])

    def test_merged_event_carries_status_and_ids(self):
        doc = self.merged_doc()
        self.run_merge(doc)
        metadata = self.event("MERGED")["metadata_redacted"]
        self.assertEqual(metadata["debt_recording_status"], debt.RECORDED)
        self.assertEqual(metadata["debt_ids"], [f"{TASK}-PR{PR}-F1"])

    def test_debt_exists_by_the_time_the_task_is_complete(self):
        doc = self.merged_doc()
        self.run_merge(doc)
        self.assertEqual(doc["tasks"][TASK]["state"], "COMPLETE")
        self.assertEqual(len(doc["debt"]), 1)

    def test_merge_failure_records_no_debt(self):
        doc = self.merged_doc()
        self.run_merge(doc, merge_ok=False)
        self.assertEqual(doc["debt"], {})
        self.assertFalse(doc["prs"][str(PR)]["merged"])
        self.assertNotIn("debt_recording_status", doc["prs"][str(PR)])

    def test_blocked_merge_records_no_debt(self):
        doc = self.merged_doc()
        self.run_merge(doc, allowed=False)
        self.assertEqual(doc["debt"], {})
        self.assertNotIn("debt_recording_status", doc["prs"][str(PR)])

    def test_no_accepted_findings_is_not_required_and_is_silent(self):
        doc = self.merged_doc(findings=[])
        self.run_merge(doc)
        record = doc["prs"][str(PR)]
        self.assertEqual(record["debt_recording_status"], debt.NOT_REQUIRED)
        self.assertNotIn("debt_recording_error", record)
        self.assertEqual(doc["debt"], {})
        self.assertIsNone(self.event("DEBT_RECORDING_FAILED"))
        self.assertEqual(self.event("MERGED")["metadata_redacted"]["debt_ids"], [])

    def test_missing_merged_sha_still_records_debt(self):
        doc = self.merged_doc()
        self.run_merge(doc, pr_view=False)
        self.assertEqual(doc["prs"][str(PR)]["debt_recording_status"], debt.RECORDED)
        self.assertIsNone(doc["debt"][f"{TASK}-PR{PR}-F1"]["merged_sha"])

    # ------------------------------------------------------ post-merge failure
    def corrupt_retained(self, doc):
        """Retained data that only a hand-edit or an older writer could produce,
        since retention already refuses this shape."""
        doc["prs"][str(PR)]["accepted_findings"] = [
            _entry("F1"), _entry("F1", "P3", "SCOPE")]

    def test_post_merge_failure_still_completes_the_task(self):
        doc = self.merged_doc()
        self.corrupt_retained(doc)
        self.run_merge(doc)
        self.assertEqual(doc["tasks"][TASK]["state"], "COMPLETE")
        self.assertTrue(doc["prs"][str(PR)]["merged"])

    def test_post_merge_failure_writes_no_partial_debt(self):
        doc = self.merged_doc()
        self.corrupt_retained(doc)
        self.run_merge(doc)
        self.assertEqual(doc["debt"], {})

    def test_post_merge_failure_records_status_and_finite_code(self):
        doc = self.merged_doc()
        self.corrupt_retained(doc)
        self.run_merge(doc)
        record = doc["prs"][str(PR)]
        self.assertEqual(record["debt_recording_status"], debt.FAILED)
        self.assertEqual(record["debt_recording_error"], "DUPLICATE_FINDING_ID")
        self.assertIn(record["debt_recording_error"], debt.ERROR_CODES)

    def test_post_merge_failure_emits_the_escalation_event(self):
        doc = self.merged_doc()
        self.corrupt_retained(doc)
        self.run_merge(doc)
        fields = self.event("DEBT_RECORDING_FAILED")
        self.assertIsNotNone(fields)
        self.assertEqual(fields["activity_class"], "ESCALATION")
        self.assertFalse(fields["human_intervention"])
        self.assertEqual(fields["metadata_redacted"]["code"], "DUPLICATE_FINDING_ID")
        self.assertEqual(fields["metadata_redacted"]["retained_count"], 2)

    def test_post_merge_failure_never_claims_debt_ids_in_merged(self):
        doc = self.merged_doc()
        self.corrupt_retained(doc)
        self.run_merge(doc)
        metadata = self.event("MERGED")["metadata_redacted"]
        self.assertEqual(metadata["debt_ids"], [])
        self.assertEqual(metadata["debt_recording_status"], debt.FAILED)

    def test_no_exception_text_reaches_state_or_the_ledger(self):
        doc = self.merged_doc()
        with mock.patch.object(debt, "_record", side_effect=RuntimeError("boom-xyz")):
            self.run_merge(doc)
        record = doc["prs"][str(PR)]
        self.assertEqual(record["debt_recording_error"], "UNEXPECTED")
        self.assertNotIn("boom-xyz", json.dumps(doc, default=str))
        self.assertNotIn("boom-xyz", json.dumps(self.events, default=str))

    def test_a_later_success_clears_a_stale_error(self):
        doc = self.merged_doc()
        doc["prs"][str(PR)]["debt_recording_error"] = "DUPLICATE_FINDING_ID"
        doc["prs"][str(PR)]["debt_recording_status"] = debt.FAILED
        self.run_merge(doc)
        record = doc["prs"][str(PR)]
        self.assertEqual(record["debt_recording_status"], debt.RECORDED)
        self.assertNotIn("debt_recording_error", record)


if __name__ == "__main__":
    unittest.main()
