"""C-04c — the second source, and the threat model that made it necessary.

WHY THIS FILE EXISTS. C-04b compared the two merge gates and declined to
port live-gate.js's REVIEW_PROVENANCE_* checks, on this reasoning:

    "live-gate.js judges an untrusted SUBMITTED PACKAGE while
     evaluate_merge judges the control plane's OWN records written under
     its own lock - a different question, not a redundant one."

THAT SENTENCE DESCRIBES A BOUNDARY THAT DOES NOT EXIST. It is true about
who writes those records when nothing is attacking them, and false as a
security property. `ThreatModelCase` below measures the difference and
reproduces the consequence.

WHAT THIS FILE PROVES, IN ORDER:

  1. the forgery is real - six field writes merge an unreviewed PR;
  2. the attestation predicate refuses every shape of missing, stale,
     unreadable and contradicted evidence;
  3. the Supervisor actually calls it, before the gate, and a forged
     record without matching ledger events no longer merges;
  4. it is NOT prevention - the same writer can forge the ledger too.

Point 4 is as important as the other three. C-22 stays open.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import config, ledger as ledger_mod, routing
from mergeable_evidence import attest, complete_evidence
from test_merge_boundary import MergeBoundaryCase

HEAD = "a" * 40
OTHER = "b" * 40
DIFF = "d" * 40
TASK = "TASK-001"
PR = 100
TZ = "Pacific/Auckland"


def a_pr(**over):
    pr = {"number": PR, "state": "OPEN", "isDraft": False, "headRefOid": HEAD,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                                 "conclusion": "SUCCESS"}]}
    pr.update(over)
    return pr


class ThreatModelCase(unittest.TestCase):
    """What a worker can reach today. Measured, not assumed."""

    def test_workers_run_as_the_uid_that_owns_the_runtime_directory(self):
        """The premise. If this ever stops being true, say so loudly.

        `control/worker_entry.py` spawns with `subprocess.Popen` and no
        setuid, setgid, user= or container, so a worker inherits this
        process's UID. That is the same UID that owns `.runtime/`.
        """
        source = (config.REPO_ROOT / "control" / "worker_entry.py").read_text(
            encoding="utf-8")
        for isolating in ("setuid", "setgid", "preexec_fn", "user="):
            self.assertNotIn(
                isolating, source,
                f"worker_entry.py now contains {isolating!r} - if worker "
                "isolation has landed, C-22 and this file's premise must be "
                "revisited rather than left standing")

        runtime = config.REPO_ROOT / ".runtime"
        if not runtime.exists():
            self.skipTest(".runtime/ does not exist on this checkout")
        self.assertEqual(
            runtime.stat().st_uid, os.getuid(),
            "the runtime directory is owned by a different user - isolation "
            "may have landed; re-check C-22 before trusting this file")

    def test_six_field_writes_merge_a_pull_request_nobody_reviewed(self):
        """THE REPRODUCTION. This is why C-04c exists.

        Everything written below lives in `.runtime/state.json`. Nothing
        here touches GitHub, the diff hash, or CI - those come from
        outside and a worker cannot forge them. It does not need to.
        """
        honest = routing.blank_pr_record(PR, TASK, "task/task-001")
        before = routing.evaluate_merge(a_pr(), honest, ("ci",), False, DIFF)
        self.assertFalse(before.allowed)
        self.assertEqual(before.condition, routing.MERGE_NO_REVIEW_PASS)

        forged = dict(honest)
        forged.update({
            "review_verdict": routing.REVIEW_PASS,   # 1
            "approval_current": True,                # 2
            "reviewed_head": HEAD,                   # 3
            "reviewed_diff_hash": DIFF,              # 4
        })
        forged.update(complete_evidence(HEAD))       # 5, 6 and the third leg

        after = routing.evaluate_merge(a_pr(), forged, ("ci",), False, DIFF)
        self.assertTrue(
            after.allowed,
            "if this no longer merges, the gate has gained a defence this "
            "file does not know about - find it and rewrite this test rather "
            "than deleting it")
        self.assertEqual(after.condition, routing.MERGE_OK)


class _Inspection:
    """The shape control/ledger.py's `inspect` returns."""

    def __init__(self, events=(), complete=True):
        self.events = tuple(events)
        self.complete = complete
        self.unreadable_lines = 0
        self.error = None


def event(event_type, outcome, head=HEAD):
    meta = {} if head is None else {"head": head}
    return {"event_type": event_type, "task_id": TASK, "pr_id": PR,
            "outcome": outcome, "metadata_redacted": meta}


def all_four(head=HEAD):
    return [event(e, o, head) for e, o in routing.MERGE_ATTESTATIONS]


class PredicateCase(unittest.TestCase):
    """`ledger_attests_merge`, every refusal shape."""

    def test_a_complete_set_at_this_head_attests(self):
        ok, why = routing.ledger_attests_merge(_Inspection(all_four()), HEAD)
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_each_missing_leg_refuses_on_its_own(self):
        """One at a time, so no leg is being carried by another."""
        for dropped, _ in routing.MERGE_ATTESTATIONS:
            with self.subTest(dropped=dropped):
                events = [e for e in all_four()
                          if e["event_type"] != dropped]
                ok, why = routing.ledger_attests_merge(
                    _Inspection(events), HEAD)
                self.assertFalse(ok)
                self.assertEqual(why, routing.MERGE_ATTESTATION_MISSING)

    def test_attestations_for_another_commit_do_not_count(self):
        ok, why = routing.ledger_attests_merge(
            _Inspection(all_four(OTHER)), HEAD)
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_MISSING)

    def test_an_attestation_naming_no_head_does_not_count(self):
        ok, why = routing.ledger_attests_merge(
            _Inspection(all_four(None)), HEAD)
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_MISSING)

    def test_a_failing_outcome_at_this_head_does_not_attest(self):
        events = [e for e in all_four() if e["event_type"] != "SECURITY_RESULT"]
        events.append(event("SECURITY_RESULT", "SECURITY_FAIL"))
        ok, why = routing.ledger_attests_merge(_Inspection(events), HEAD)
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_MISSING)

    def test_a_pass_and_a_fail_for_one_leg_is_a_contradiction_not_a_pass(self):
        """Two disagreeing attestations about one commit.

        Taking the convenient one is how a re-run that FAILED becomes a
        merge, so the pair is refused even though a PASS is present.
        """
        events = all_four() + [event("SECURITY_RESULT", "SECURITY_FAIL")]
        ok, why = routing.ledger_attests_merge(_Inspection(events), HEAD)
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_CONTRADICTED)

    def test_an_incomplete_inspection_refuses_even_with_every_leg_present(self):
        """A truncated line is not an absent event.

        `complete` False means some line would not parse. Reading that as
        "so the rest is the whole story" is how a crash mid-append, or a
        deliberately corrupted ledger, reads as whatever the caller hoped.
        """
        ok, why = routing.ledger_attests_merge(
            _Inspection(all_four(), complete=False), HEAD)
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_UNREADABLE)

    def test_an_unusable_head_can_attest_nothing(self):
        for head in (None, "", "not-a-sha", 7, HEAD[:-1]):
            with self.subTest(head=head):
                ok, why = routing.ledger_attests_merge(
                    _Inspection(all_four()), head)
                self.assertFalse(ok)

    def test_a_stubbed_or_broken_inspection_refuses_without_raising(self):
        """It returns a finite reason. It never raises one.

        The same lesson security_claim_is_valid learned: a predicate that
        raises mid-tick is worse than one that holds, because a crash is
        not a decision.
        """
        for inspection in (None, _Inspection(complete=True),
                           mock.Mock(), object(),
                           mock.Mock(complete=True, events=mock.Mock())):
            with self.subTest(inspection=type(inspection).__name__):
                ok, why = routing.ledger_attests_merge(inspection, HEAD)
                self.assertFalse(ok)
                self.assertIsInstance(why, str)
                self.assertTrue(why)

    def test_a_non_dict_event_is_skipped_not_crashed_on(self):
        ok, why = routing.ledger_attests_merge(
            _Inspection(["not an event", None] + all_four()), HEAD)
        self.assertTrue(ok, why)


class AgainstARealLedgerCase(unittest.TestCase):
    """Through the real `Ledger.append` and the real `inspect`.

    The predicate tests above build event dicts by hand. If `Ledger.append`
    stored the head somewhere `_attested_head` does not look, every one of
    them would still pass while production attested nothing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ledger_mod.Ledger(
            path=Path(self.tmp.name) / "ledger.jsonl", tz=TZ,
            experiment_id="run-002")

    def _attests(self, head=HEAD):
        return routing.ledger_attests_merge(
            self.ledger.inspect(task_id=TASK, pr_id=PR,
                                event_types=routing.MERGE_ATTESTATION_EVENTS),
            head)

    def test_an_empty_ledger_attests_nothing(self):
        ok, why = self._attests()
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_MISSING)

    def test_events_written_by_the_real_append_are_found(self):
        attest(self.ledger, HEAD, TASK, PR)
        ok, why = self._attests()
        self.assertTrue(ok, why)

    def test_the_head_sha_spelling_is_also_found(self):
        """REVIEW_RESULT passes `head_sha=`, the other three `head`.

        `head_sha` is not in ledger.FIELDS, so append merges it into
        metadata_redacted - which is where the other three put theirs.
        Both spellings must resolve or the code-review leg silently stops
        attesting.
        """
        for event_type, outcome in routing.MERGE_ATTESTATIONS:
            if event_type == "REVIEW_RESULT":
                self.ledger.append(event_type, task_id=TASK, pr_id=PR,
                                   outcome=outcome, head_sha=HEAD)
            else:
                self.ledger.append(event_type, task_id=TASK, pr_id=PR,
                                   outcome=outcome,
                                   metadata_redacted={"head": HEAD})
        ok, why = self._attests()
        self.assertTrue(ok, why)

    def test_another_pull_requests_attestations_do_not_count(self):
        attest(self.ledger, HEAD, TASK, PR + 1)
        ok, _ = self._attests()
        self.assertFalse(ok)

    def test_another_tasks_attestations_do_not_count(self):
        attest(self.ledger, HEAD, "TASK-009", PR)
        ok, _ = self._attests()
        self.assertFalse(ok)

    def test_a_truncated_final_line_makes_the_whole_answer_unreadable(self):
        attest(self.ledger, HEAD, TASK, PR)
        with open(self.ledger.path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "REVIEW_RESU')
        ok, why = self._attests()
        self.assertFalse(ok)
        self.assertEqual(why, routing.MERGE_ATTESTATION_UNREADABLE)


class ThroughTheRealSupervisorCase(MergeBoundaryCase):
    """The check is REACHED, before the gate, by the real merge path.

    A predicate nothing calls is documentation. These drive the real
    `execute_merges` over a real `Store` and a real `Ledger`.
    """

    def test_the_seeded_baseline_still_merges(self):
        """Or nothing below means anything."""
        self.seed()
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [100])

    def test_a_record_with_no_ledger_attestation_does_not_merge(self):
        """THE FORGERY, through the real Supervisor.

        The record is exactly the one that merges above - complete
        evidence, REVIEW_PASS, current approval, matching head and diff.
        Only the durable attestations are gone, which is the state a
        worker produces by writing state.json and nothing else.
        """
        self.seed()
        self.ledger_path.write_text("", encoding="utf-8")
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [],
                         "a record with no durable attestation merged")

    def test_the_refusal_names_its_condition_durably(self):
        self.seed()
        self.ledger_path.write_text("", encoding="utf-8")
        self.run_tick()
        blocked = [fields for name, fields in self.events
                   if name == "MERGE_BLOCKED"]
        self.assertTrue(blocked, "the refusal was not recorded at all")
        self.assertEqual(blocked[-1]["metadata_redacted"]["condition"],
                         routing.MERGE_ATTESTATION_MISSING)

    def test_the_refusal_does_not_invalidate_the_approval(self):
        """A bookkeeping fault is not evidence the review was wrong.

        Invalidating would send the task back round a whole review cycle
        for a ledger that could not be read.
        """
        self.seed()
        self.ledger_path.write_text("", encoding="utf-8")
        self.run_tick()
        record = self.store.read()["prs"]["100"]
        self.assertTrue(record["approval_current"])
        self.assertEqual(record["review_verdict"], routing.REVIEW_PASS)

    def test_attestations_for_a_different_head_do_not_merge(self):
        """The attestation is bound to the head GitHub reports now."""
        self.seed()
        self.ledger_path.write_text("", encoding="utf-8")
        attest(self.sup.ledger, OTHER, "TASK-001", 100)
        self.run_tick()
        self.assertEqual(self.merged_numbers(), [])

    def test_the_check_runs_before_the_gate(self):
        """Ordering is the contract: an unattested merge must never reach
        `evaluate_merge` at all, so a gate defect cannot be the thing that
        saves it."""
        self.seed()
        self.ledger_path.write_text("", encoding="utf-8")
        with mock.patch.object(routing, "evaluate_merge") as gate:
            self.run_tick()
        gate.assert_not_called()


class WhatThisIsNotCase(unittest.TestCase):
    """The limit, asserted rather than left to a comment.

    If any of these starts failing, the ledger has gained an integrity
    property and C-22's assessment must be rewritten rather than assumed
    still true.
    """

    def test_the_ledger_has_no_hash_chain_or_signature(self):
        source = (config.REPO_ROOT / "control" / "ledger.py").read_text(
            encoding="utf-8")
        for integrity in ("prev_hash", "hmac", "signature", "sha256"):
            self.assertNotIn(
                integrity, source,
                f"ledger.py now mentions {integrity!r} - if the ledger has "
                "gained integrity protection, this check is tamper "
                "PREVENTION rather than tamper evidence, and C-22 should "
                "be re-assessed")

    def test_the_same_writer_can_forge_the_attestations_too(self):
        """The honest statement of what C-04c bought.

        Nothing here stops a writer who can edit state.json from also
        appending four events. What it buys is that the forgery must now
        appear in the durable evidence record, permanently, where a human
        or the Watchdog can see it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ledger_mod.Ledger(path=Path(tmp) / "ledger.jsonl",
                                       tz=TZ, experiment_id="run-002")
            attest(ledger, HEAD, TASK, PR)
            ok, _ = routing.ledger_attests_merge(
                ledger.inspect(task_id=TASK, pr_id=PR,
                               event_types=routing.MERGE_ATTESTATION_EVENTS),
                HEAD)
        self.assertTrue(
            ok,
            "a forged set of attestations is accepted - which is the point "
            "being recorded, not a defect in this test. C-22 stays open "
            "until workers cannot write .runtime/")


if __name__ == "__main__":
    unittest.main()
