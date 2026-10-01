"""C-05.3a: the security-evidence claim, and the transition it unlocks.

Two halves of one change.

THE TRANSITION. `WAITING_EVIDENCE -> FIX_REQUIRED` is added by C-05.3
governance (2026-09-30). A specialized evidence FAIL on the current head
means that head needs remediation before a reviewer spends a cycle on it.
The loop is WAITING_EVIDENCE -> FIX_REQUIRED -> PR_OPEN ->
WAITING_EVIDENCE, with required evidence rerun for the NEW head; a prior
SHA's pass never carries forward. `WAITING_EVIDENCE -> REVIEW` stays
exactly as it was, because REVIEW is still where a head goes once every
required current-SHA evidence class has passed, and the reviewer remains
the acceptance authority. Evidence is not merge authority.

THE CLAIM. It lives on the PR record and is written ONLY inside the
Supervisor's state-only transaction. The ordinal arrives ALREADY CHOSEN -
allocated under the exclusive state lock by the caller - and
security_claim turns it into a canonical record. It never picks an
ordinal, never increments one, never searches the filesystem for one.
Doing any of those would move the choice outside the lock and let two
ticks claim the same attempt.

attempt_id and worker are DERIVED, not accepted, so a caller cannot
supply an attempt_id that disagrees with its ordinal or a worker name
that is not a safe path component.

The claim's diagnostics are deliberately NOT the eleven
SECURITY_FAILURE_REASONS. Those say why an ATTEMPT is not a completed
review; these say why a claim RECORD is malformed. Filing a bookkeeping
bug as a security finding would put a control-plane mistake into the
record as evidence about the product.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import clock, routing, security_contract, state  # noqa: E402

TZ = "Pacific/Auckland"
SHA40 = "a" * 40


def moments(gap_seconds=1800):
    now = clock.now(TZ)
    return clock.iso(now), clock.iso(now + timedelta(seconds=gap_seconds))


def a_claim(**over):
    claimed_at, lease_expires_at = moments()
    claim = routing.security_claim(
        task_id="TASK-001", sha=SHA40, ordinal=1,
        claimed_at=claimed_at, lease_expires_at=lease_expires_at)
    claim.update(over)
    return claim


class TransitionTableCase(unittest.TestCase):

    def test_waiting_evidence_may_now_reach_fix_required(self):
        self.assertIn("FIX_REQUIRED",
                      state.ALLOWED_TRANSITIONS["WAITING_EVIDENCE"])

    def test_waiting_evidence_still_reaches_review(self):
        # The pass path is untouched: REVIEW is where a head goes once all
        # required current-SHA evidence has passed.
        self.assertIn("REVIEW", state.ALLOWED_TRANSITIONS["WAITING_EVIDENCE"])

    def test_evidence_is_not_merge_authority(self):
        # Nothing may go from evidence straight to merge-eligible.
        for forbidden in ("MERGE_READY", "MERGED", "COMPLETE"):
            with self.subTest(target=forbidden):
                self.assertNotIn(
                    forbidden, state.ALLOWED_TRANSITIONS["WAITING_EVIDENCE"])

    def test_no_other_row_changed(self):
        expected = {
            "PR_OPEN": {"REVIEW", "WAITING_CI", "WAITING_EVIDENCE", "STALE",
                        "FAILED", "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"},
            "WAITING_CI": {"REVIEW", "WAITING_EVIDENCE", "STALE", "FAILED",
                           "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET"},
            "REVIEW": {"FIX_REQUIRED", "MERGE_READY", "MERGED", "PR_OPEN",
                       "STALE", "FAILED", "HUMAN_REQUIRED",
                       "WAITING_PROVIDER_RESET", "FROZEN"},
            "FIX_REQUIRED": {"REVIEW", "PR_OPEN", "STALE", "FAILED",
                             "HUMAN_REQUIRED", "WAITING_PROVIDER_RESET",
                             "FROZEN"},
        }
        for origin, targets in expected.items():
            with self.subTest(origin=origin):
                self.assertEqual(set(state.ALLOWED_TRANSITIONS[origin]), targets)

    def test_the_remediation_loop_walks_legally(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task["state"] = "PR_OPEN"
        for target in ("WAITING_EVIDENCE", "FIX_REQUIRED", "PR_OPEN",
                       "WAITING_EVIDENCE", "REVIEW"):
            state.transition(doc, "TASK-001", target, "c05.3a loop", TZ)
            self.assertEqual(doc["tasks"]["TASK-001"]["state"], target)

    def test_an_illegal_transition_still_raises(self):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task["state"] = "WAITING_EVIDENCE"
        with self.assertRaises(state.TransitionError):
            state.transition(doc, "TASK-001", "MERGE_READY", "no", TZ)


class PrRecordCase(unittest.TestCase):

    def test_a_new_pr_record_carries_an_empty_claim(self):
        record = routing.blank_pr_record(12, "TASK-001", "feat/x")
        self.assertIn("security_evidence", record)
        self.assertIsNone(record["security_evidence"])

    def test_the_existing_baseline_fields_are_untouched(self):
        record = routing.blank_pr_record(12, "TASK-001", "feat/x")
        self.assertEqual(
            {k: v for k, v in record.items() if k != "security_evidence"},
            {"number": 12, "task_id": "TASK-001", "branch": "feat/x",
             "review_verdict": None, "approval_current": False,
             "reviewed_head": None,
             "reviewed_diff_hash": None, "review_cycles": 0,
             "repair_cycles": 0, "open_finding_ids": [],
             "last_review_at": None, "reconciled": False, "merged": False})

    def test_a_legacy_record_without_the_key_is_still_readable(self):
        # Records written before C-05.3a simply lack the field. Absent and
        # None both mean "no claim"; a reader must use .get, never [].
        legacy = routing.blank_pr_record(12, "TASK-001", "feat/x")
        del legacy["security_evidence"]
        self.assertIsNone(legacy.get("security_evidence"))


class ClaimConstructionCase(unittest.TestCase):

    def test_a_claim_is_planned_with_no_outcome(self):
        claim = a_claim()
        self.assertEqual(claim["claim_state"], "PLANNED")
        self.assertIsNone(claim["verdict"])
        self.assertEqual(claim["reason"], "")
        self.assertEqual(routing.security_claim_is_valid(claim), (True, ""))

    def test_the_shape_is_closed_at_nine_keys(self):
        self.assertEqual(set(a_claim()), set(routing.SECURITY_CLAIM_KEYS))
        self.assertEqual(len(routing.SECURITY_CLAIM_KEYS), 9)

    def test_attempt_id_and_worker_are_derived_from_the_ordinal(self):
        claimed_at, lease = moments()
        for ordinal, attempt_id in ((1, "security-attempt-0001"),
                                    (42, "security-attempt-0042"),
                                    (9999, "security-attempt-9999")):
            with self.subTest(ordinal=ordinal):
                claim = routing.security_claim(
                    task_id="TASK-001", sha=SHA40, ordinal=ordinal,
                    claimed_at=claimed_at, lease_expires_at=lease)
                self.assertEqual(claim["attempt_id"], attempt_id)
                # The worker name carries the FULL head SHA as well as the
                # ordinal. Without a SHA at all, two attempts for different
                # commits share a name, and every worker artefact is keyed on
                # that name - which is how one commit's output became
                # another's durable verdict. With only a 12-character prefix
                # the same collision returns for any two commits sharing it,
                # so the whole SHA is used: as unique as the attempt
                # directory it addresses.
                self.assertEqual(claim["worker"],
                                 f"task-001-security-{SHA40}-{ordinal:04d}")

    def test_it_never_allocates_or_searches_for_an_ordinal(self):
        # The same inputs give the same claim, every time, with no state
        # anywhere. An allocator would have to remember something.
        claimed_at, lease = moments()
        kwargs = dict(task_id="TASK-001", sha=SHA40, ordinal=7,
                      claimed_at=claimed_at, lease_expires_at=lease)
        first = routing.security_claim(**kwargs)
        second = routing.security_claim(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["ordinal"], 7)

    def test_a_non_canonical_input_refuses_to_build_a_claim(self):
        claimed_at, lease = moments()
        base = dict(task_id="TASK-001", sha=SHA40, ordinal=1,
                    claimed_at=claimed_at, lease_expires_at=lease)
        for label, over in [
            ("ordinal zero", {"ordinal": 0}),
            ("ordinal negative", {"ordinal": -1}),
            ("ordinal past the namespace", {"ordinal": 10000}),
            ("ordinal bool", {"ordinal": True}),
            ("ordinal str", {"ordinal": "1"}),
            ("ordinal float", {"ordinal": 1.0}),
            ("sha uppercase", {"sha": "A" * 40}),
            ("sha short", {"sha": "a" * 39}),
            ("sha empty", {"sha": ""}),
            ("task_id traversing", {"task_id": "../x"}),
            ("task_id separator", {"task_id": "a/b"}),
            ("task_id empty", {"task_id": ""}),
            ("task_id leading dot", {"task_id": ".hidden"}),
            ("task_id not a string", {"task_id": 1}),
            ("lease equals claim", {"lease_expires_at": claimed_at}),
        ]:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    routing.security_claim(**{**base, **over})

    def test_a_lease_before_its_claim_is_refused(self):
        now = clock.now(TZ)
        with self.assertRaises(ValueError):
            routing.security_claim(
                task_id="TASK-001", sha=SHA40, ordinal=1,
                claimed_at=clock.iso(now),
                lease_expires_at=clock.iso(now - timedelta(seconds=1)))


class ClaimTimestampCase(unittest.TestCase):
    """Canonical means round-trips AND is tz-aware. The second is not
    implied by the first - a naive timestamp round-trips perfectly, and
    would then raise TypeError at the ordering comparison instead of being
    refused."""

    def test_a_canonical_pair_is_accepted(self):
        self.assertEqual(routing.security_claim_is_valid(a_claim()), (True, ""))

    def test_non_canonical_forms_are_refused(self):
        for label, value in [
            ("microseconds", "2026-09-30T08:16:07.123456+13:00"),
            ("zulu suffix", "2026-09-30T08:16:07Z"),
            ("space separator", "2026-09-30 08:16:07+13:00"),
            ("minute precision", "2026-09-30T08:16+13:00"),
            ("garbage", "not-a-time"),
            ("empty", ""),
            ("not a string", 1759190167),
            ("naive - no offset", "2026-09-30T08:16:07"),
        ]:
            with self.subTest(case=label):
                ok, reason = routing.security_claim_is_valid(
                    a_claim(claimed_at=value))
                self.assertFalse(ok)
                self.assertEqual(reason, "CLAIM_TIMESTAMP_INVALID")

    def test_a_stored_claim_with_an_inverted_lease_is_refused(self):
        # Not just at construction. A record read back from state must be
        # refused too: Step 6a proves a prior attempt is not running partly
        # via now >= lease_expires_at, and a lease that expires at or
        # before its claim makes that true immediately - which would permit
        # a duplicate provider review of the same head.
        now = clock.now(TZ)
        claimed_at = clock.iso(now)
        for label, lease in (("equal", claimed_at),
                             ("one second earlier",
                              clock.iso(now - timedelta(seconds=1))),
                             ("an hour earlier",
                              clock.iso(now - timedelta(hours=1)))):
            with self.subTest(lease=label):
                ok, why = routing.security_claim_is_valid(
                    a_claim(claimed_at=claimed_at, lease_expires_at=lease))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_LEASE_NOT_AFTER_CLAIM")

    def test_a_naive_timestamp_never_reaches_the_ordering_comparison(self):
        # The regression that matters: it round-trips, so only the explicit
        # tz-awareness check stops it, and without that check comparing it
        # with an aware value raises TypeError rather than returning False.
        self.assertTrue(
            clock.iso(clock.parse("2026-09-30T08:16:07")) == "2026-09-30T08:16:07")
        ok, reason = routing.security_claim_is_valid(
            a_claim(claimed_at="2026-09-30T08:16:07"))
        self.assertFalse(ok)
        self.assertEqual(reason, "CLAIM_TIMESTAMP_INVALID")


class ClaimInvariantCase(unittest.TestCase):

    def test_planned_and_spawned_carry_no_outcome(self):
        for claim_state in ("PLANNED", "SPAWNED"):
            with self.subTest(claim_state=claim_state):
                self.assertEqual(
                    routing.security_claim_is_valid(
                        a_claim(claim_state=claim_state)), (True, ""))

    def test_complete_with_a_verdict_is_valid(self):
        for verdict in (security_contract.SECURITY_PASS,
                        security_contract.SECURITY_FAIL):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    routing.security_claim_is_valid(
                        a_claim(claim_state="COMPLETE", verdict=verdict)),
                    (True, ""))

    def test_complete_with_a_finite_reason_is_valid(self):
        for reason in sorted(security_contract.SECURITY_FAILURE_REASONS):
            with self.subTest(reason=reason):
                self.assertEqual(
                    routing.security_claim_is_valid(
                        a_claim(claim_state="COMPLETE", reason=reason)),
                    (True, ""))

    def test_an_outcome_before_completion_is_refused(self):
        for claim_state in ("PLANNED", "SPAWNED"):
            with self.subTest(claim_state=claim_state, field="verdict"):
                ok, why = routing.security_claim_is_valid(a_claim(
                    claim_state=claim_state,
                    verdict=security_contract.SECURITY_PASS))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_VERDICT_BEFORE_COMPLETE")
            with self.subTest(claim_state=claim_state, field="reason"):
                ok, why = routing.security_claim_is_valid(a_claim(
                    claim_state=claim_state,
                    reason=security_contract.TIMED_OUT))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_REASON_BEFORE_COMPLETE")

    def test_complete_with_both_is_a_contradiction(self):
        ok, why = routing.security_claim_is_valid(a_claim(
            claim_state="COMPLETE", verdict=security_contract.SECURITY_PASS,
            reason=security_contract.TIMED_OUT))
        self.assertFalse(ok)
        self.assertEqual(why, "CLAIM_VERDICT_AND_REASON")

    def test_complete_with_neither_says_nothing(self):
        ok, why = routing.security_claim_is_valid(
            a_claim(claim_state="COMPLETE"))
        self.assertFalse(ok)
        self.assertEqual(why, "CLAIM_COMPLETE_WITHOUT_OUTCOME")

    def test_an_unrecognised_verdict_or_reason_is_refused(self):
        ok, why = routing.security_claim_is_valid(
            a_claim(claim_state="COMPLETE", verdict="SECURITY_MAYBE"))
        self.assertFalse(ok)
        self.assertEqual(why, "CLAIM_VERDICT_UNRECOGNISED")
        for bad in ("PASS_WITH_FINDINGS", "BROKEN", "timed_out"):
            with self.subTest(reason=bad):
                ok, why = routing.security_claim_is_valid(
                    a_claim(claim_state="COMPLETE", reason=bad))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_REASON_UNRECOGNISED")

    def test_unparseable_is_never_a_claim_verdict(self):
        ok, why = routing.security_claim_is_valid(a_claim(
            claim_state="COMPLETE",
            verdict=security_contract.SECURITY_UNPARSEABLE))
        self.assertFalse(ok)
        self.assertEqual(why, "CLAIM_VERDICT_UNRECOGNISED")


class ClaimShapeCase(unittest.TestCase):

    def test_a_foreign_or_missing_key_is_refused(self):
        self.assertEqual(
            routing.security_claim_is_valid(a_claim(extra="x"))[1],
            "CLAIM_KEYS_INVALID")
        short = a_claim()
        del short["worker"]
        self.assertEqual(routing.security_claim_is_valid(short)[1],
                         "CLAIM_KEYS_INVALID")

    def test_a_non_object_claim_is_refused(self):
        for value in (None, [], "PLANNED", 7):
            with self.subTest(claim=repr(value)):
                self.assertEqual(routing.security_claim_is_valid(value),
                                 (False, "CLAIM_NOT_AN_OBJECT"))

    def test_an_attempt_id_that_disagrees_with_its_ordinal_is_refused(self):
        for attempt_id in ("security-attempt-0002", "security-attempt-1",
                           "attempt-0001", "security-attempt-00001", ""):
            with self.subTest(attempt_id=attempt_id):
                ok, why = routing.security_claim_is_valid(
                    a_claim(attempt_id=attempt_id))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_ATTEMPT_ID_MISMATCH")

    def test_an_unsafe_worker_name_is_refused(self):
        for worker in ("../escape", "a/b", "UPPER", "-leading", ".hidden",
                       "", "x" * 65, "has space"):
            with self.subTest(worker=worker):
                ok, why = routing.security_claim_is_valid(a_claim(worker=worker))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_WORKER_INVALID")

    def test_the_ordinal_ceiling_is_the_namespace_width(self):
        # security-attempt-NNNN is four digits, so 9999 is the last ordinal
        # that can be NAMED. Past it the claim must fail here rather than
        # mint a five-digit id no reader would resolve.
        self.assertEqual(routing.SECURITY_ORDINAL_MAX, 9999)
        for ordinal in (0, -1, 10000, True, "1", 1.0, None):
            with self.subTest(ordinal=repr(ordinal)):
                ok, why = routing.security_claim_is_valid(
                    a_claim(ordinal=ordinal))
                self.assertFalse(ok)
                self.assertIn(why, {"CLAIM_ORDINAL_INVALID",
                                    "CLAIM_ATTEMPT_ID_MISMATCH"})

    def test_a_non_canonical_sha_is_refused(self):
        for sha in ("A" * 40, "a" * 39, "a" * 41, "", "../" + "a" * 37, None):
            with self.subTest(sha=repr(sha)):
                ok, why = routing.security_claim_is_valid(a_claim(sha=sha))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_SHA_INVALID")

    def test_an_unknown_claim_state_is_refused(self):
        for claim_state in ("RUNNING", "planned", "", None, "DONE"):
            with self.subTest(claim_state=repr(claim_state)):
                ok, why = routing.security_claim_is_valid(
                    a_claim(claim_state=claim_state))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_STATE_INVALID")

    def test_an_unhashable_claim_state_is_refused_not_raised(self):
        # A membership test hashes its left operand. Durable JSON can carry
        # an object or an array in any field, so a corrupt PR record must
        # still produce a finite diagnostic rather than crash the tick.
        for claim_state in ({}, [], {"a": 1}, [1, 2], set()):
            with self.subTest(claim_state=repr(claim_state)):
                ok, why = routing.security_claim_is_valid(
                    a_claim(claim_state=claim_state))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_STATE_INVALID")

    def test_an_unhashable_verdict_is_refused_not_raised(self):
        # The second of the two sites, and the one C-05.3b's D1 reported.
        # A COMPLETE claim whose verdict holds an object or an array is an
        # unrecognised verdict, which is what it is refused as.
        for verdict in ({}, [], {"verdict": "SECURITY_PASS"}, ["x"], set()):
            with self.subTest(verdict=repr(verdict)):
                ok, why = routing.security_claim_is_valid(
                    a_claim(claim_state="COMPLETE", verdict=verdict))
                self.assertFalse(ok)
                self.assertEqual(why, "CLAIM_VERDICT_UNRECOGNISED")

    def test_no_claim_field_can_raise_out_of_the_validator(self):
        # The guards above are per-field, so this is the standing property
        # they serve: whatever durable JSON puts in any field, the validator
        # ANSWERS. It never raises, and a refusal always carries a non-empty
        # diagnostic. A new membership test added later without a guard
        # fails here even if nobody thinks to extend the two cases above.
        hostile = ({}, [], {"k": [1]}, [{"k": 1}], set(), 0.5, b"bytes")
        for field in sorted(routing.SECURITY_CLAIM_KEYS):
            for value in hostile:
                with self.subTest(field=field, value=repr(value)):
                    try:
                        ok, why = routing.security_claim_is_valid(
                            a_claim(**{field: value}))
                    except Exception as exc:  # noqa: BLE001 - that is the point
                        self.fail(f"{field}={value!r} raised "
                                  f"{type(exc).__name__}: {exc}")
                    self.assertFalse(ok)
                    self.assertTrue(why)


class DiagnosticSeparationCase(unittest.TestCase):
    """Claim diagnostics and attempt failure reasons are different
    vocabularies answering different questions."""

    def test_no_claim_diagnostic_is_an_attempt_failure_reason(self):
        seen = set()
        for broken in (None, a_claim(sha="nope"), a_claim(ordinal=0),
                       a_claim(worker="../x"), a_claim(claim_state="NOPE"),
                       a_claim(claimed_at="bad"), a_claim(extra=1),
                       a_claim(claim_state="COMPLETE"),
                       a_claim(claim_state="COMPLETE", reason="BROKEN"),
                       a_claim(verdict="SECURITY_PASS")):
            ok, why = routing.security_claim_is_valid(broken)
            self.assertFalse(ok)
            seen.add(why)
        self.assertTrue(seen)
        self.assertEqual(
            seen & set(security_contract.SECURITY_FAILURE_REASONS), set(),
            "a bookkeeping diagnostic leaked into the attempt vocabulary")
        for why in seen:
            self.assertTrue(why.startswith("CLAIM_"), why)


class NoSideEffectCase(unittest.TestCase):

    def test_the_claim_api_touches_nothing_outside_itself(self):
        import control.routing as mod
        for banned in ("os", "subprocess", "socket", "shutil", "requests"):
            with self.subTest(module=banned):
                self.assertIsNone(getattr(mod, banned, None))

    def test_building_a_claim_creates_no_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(Path(tmp).rglob("*"))
            a_claim()
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
