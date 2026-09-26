"""C-08b.1: the human-intervention lifecycle foundation.

Pins the contract of control/intervention.py: the OPEN -> ACKNOWLEDGED ->
RESOLVED state machine, its input invariants, its dedup behaviour, and the
boundary that makes it a record rather than an executor.

Two properties get disproportionate attention here because they are what the
module promises structurally rather than incidentally:

  * A rejected operation leaves the document deep-equal to what it was. Every
    rejection test snapshots with copy.deepcopy and compares the whole
    document, not just the field under test.
  * Resolving an intervention stores a decision and executes nothing. The
    no-execution tests compare every top-level key except "interventions",
    so a future edit that quietly clears a guardrail from here fails them.

Time is frozen with mock.patch.object over intervention.clock.now so durations
are exact integers rather than whatever the wall clock happened to do.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import cli, clock, intervention, ledger as ledger_mod, state  # noqa: E402

TZ = "Pacific/Auckland"

ID_RE = re.compile(r"^INT-[0-9a-f]{16}$")

VALID_REQUEST = {
    "type_": "HUMAN_PRIVILEGED_ACTION",
    "scope": "task",
    "task_id": "T-01",
    "reason": "gh auth refresh needs a human at the browser",
    "condition_code": "gh_auth_expired",
}


def _moment(hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    """A fixed, microsecond-free instant, so clock.iso() round-trips exactly."""
    return datetime(2026, 9, 26, hour, minute, second, tzinfo=ZoneInfo(TZ))


@contextlib.contextmanager
def _clock_at(moment: datetime):
    """Freeze the clock intervention.py reads, for the duration of the block."""
    with mock.patch.object(intervention.clock, "now", return_value=moment):
        yield


def _fresh_doc() -> dict:
    return state.initial_document("run-002", "2.0")


def _legacy_doc() -> dict:
    """A state document written before C-08b.1 existed: no "interventions" key
    at all. Every function must treat this as zero interventions."""
    doc = state.initial_document("run-002", "2.0")
    del doc["interventions"]
    return doc


def _request(doc: dict, *, at: datetime | None = None, **overrides) -> tuple[dict, bool]:
    payload = dict(VALID_REQUEST)
    payload.update(overrides)
    with _clock_at(at or _moment()):
        return intervention.request(doc, tz=TZ, **payload)


def _acknowledged(doc: dict, *, at: datetime | None = None, by: str = "serina",
                  **overrides) -> dict:
    record, _ = _request(doc, **overrides)
    with _clock_at(at or _moment(12, 0, 0)):
        intervention.acknowledge(doc, record["id"], by=by, tz=TZ)
    return record


# --------------------------------------------------------------------- request


class TestRequest(unittest.TestCase):
    def test_valid_task_scoped_request_creates_open_record(self):
        doc = _fresh_doc()
        record, is_new = _request(doc)
        self.assertTrue(is_new)
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(record["scope"], "task")
        self.assertEqual(record["task_id"], "T-01")
        self.assertEqual(record["type"], "HUMAN_PRIVILEGED_ACTION")
        self.assertEqual(record["condition_code"], "gh_auth_expired")
        self.assertEqual(doc["interventions"][record["id"]], record)

    def test_new_record_has_the_full_field_set_with_lifecycle_fields_empty(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        self.assertEqual(
            set(record),
            {"id", "type", "scope", "task_id", "reason", "condition_code",
             "dedup_key", "status", "requested_at", "acknowledged_at",
             "acknowledged_by", "resolved_at", "resolved_by", "resolution",
             "resolution_note", "active_human_seconds"},
        )
        for field in ("acknowledged_at", "acknowledged_by", "resolved_at",
                      "resolved_by", "resolution", "resolution_note",
                      "active_human_seconds"):
            self.assertIsNone(record[field], field)

    def test_valid_systemic_request_creates_open_record(self):
        doc = _fresh_doc()
        record, is_new = _request(
            doc, scope="systemic", task_id=None,
            type_="HUMAN_GOVERNANCE_DECISION", condition_code="budget_exhausted",
            reason="spend crossed the hard stop and needs a governance call",
        )
        self.assertTrue(is_new)
        self.assertEqual(record["status"], "OPEN")
        self.assertEqual(record["scope"], "systemic")
        self.assertIsNone(record["task_id"])

    def test_id_is_int_prefix_plus_sixteen_hex_characters(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        self.assertRegex(record["id"], ID_RE)

    def test_generated_ids_match_the_format_and_differ(self):
        first = intervention.new_intervention_id()
        second = intervention.new_intervention_id()
        self.assertRegex(first, ID_RE)
        self.assertRegex(second, ID_RE)
        self.assertNotEqual(first, second)

    def test_requested_at_is_populated_and_round_trips(self):
        doc = _fresh_doc()
        moment = _moment(9, 30, 15)
        record, _ = _request(doc, at=moment)
        self.assertEqual(record["requested_at"], clock.iso(moment))
        self.assertEqual(clock.parse(record["requested_at"]), moment)

    def test_task_scoped_dedup_key(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        self.assertEqual(record["dedup_key"], "task:T-01:gh_auth_expired")

    def test_systemic_dedup_key_omits_task(self):
        doc = _fresh_doc()
        record, _ = _request(
            doc, scope="systemic", task_id=None, condition_code="budget_exhausted",
        )
        self.assertEqual(record["dedup_key"], "system:budget_exhausted")

    def test_same_condition_on_different_tasks_are_separate_interventions(self):
        doc = _fresh_doc()
        first, _ = _request(doc, task_id="T-01")
        second, is_new = _request(doc, task_id="T-02")
        self.assertTrue(is_new)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(doc["interventions"]), 2)

    def test_open_duplicate_reuses_existing_record(self):
        doc = _fresh_doc()
        first, _ = _request(doc)
        second, is_new = _request(doc, at=_moment(13, 0, 0))
        self.assertFalse(is_new)
        self.assertIs(second, first)
        self.assertEqual(len(doc["interventions"]), 1)
        self.assertEqual(second["requested_at"], clock.iso(_moment()))

    def test_acknowledged_duplicate_reuses_existing_record(self):
        doc = _fresh_doc()
        first = _acknowledged(doc)
        second, is_new = _request(doc, at=_moment(14, 0, 0))
        self.assertFalse(is_new)
        self.assertIs(second, first)
        self.assertEqual(second["status"], "ACKNOWLEDGED")
        self.assertEqual(len(doc["interventions"]), 1)

    def test_recurrence_after_resolution_creates_a_new_intervention(self):
        doc = _fresh_doc()
        first = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, first["id"], outcome="RETRY", by="serina", tz=TZ)

        second, is_new = _request(doc, at=_moment(15, 0, 0))
        self.assertTrue(is_new)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "OPEN")
        self.assertEqual(second["dedup_key"], first["dedup_key"])
        self.assertEqual(len(doc["interventions"]), 2)
        self.assertEqual(doc["interventions"][first["id"]]["status"], "RESOLVED")

    def test_id_collision_regenerates_without_overwriting_the_existing_record(self):
        doc = _fresh_doc()
        first, _ = _request(doc, condition_code="gh_auth_expired")
        snapshot = copy.deepcopy(first)
        replacement = "INT-" + "a" * 16

        with mock.patch.object(intervention, "new_intervention_id",
                               side_effect=[first["id"], replacement]) as gen:
            second, is_new = _request(doc, condition_code="host_unreachable")

        self.assertEqual(gen.call_count, 2)
        self.assertTrue(is_new)
        self.assertEqual(second["id"], replacement)
        self.assertEqual(len(doc["interventions"]), 2)
        self.assertEqual(doc["interventions"][first["id"]], snapshot)

    def test_request_against_a_document_with_no_interventions_key(self):
        doc = _legacy_doc()
        record, is_new = _request(doc)
        self.assertTrue(is_new)
        self.assertEqual(doc["interventions"], {record["id"]: record})

    def test_invalid_request_leaves_a_legacy_document_deep_equal_unchanged(self):
        doc = _legacy_doc()
        before = copy.deepcopy(doc)
        with self.assertRaises(intervention.InterventionError):
            _request(doc, type_="HUMAN_SOMETHING_ELSE")
        self.assertEqual(doc, before)
        self.assertNotIn("interventions", doc)


# ----------------------------------------------------------- input invariants


class TestRequestInputInvariants(unittest.TestCase):
    """Each invariant is rejected on its own, and the rejection leaves the whole
    document untouched — including an already-present valid intervention."""

    def _doc_with_one_record(self) -> dict:
        doc = _fresh_doc()
        _request(doc, condition_code="pre_existing")
        return doc

    def _assert_rejected(self, **overrides):
        doc = self._doc_with_one_record()
        before = copy.deepcopy(doc)
        with self.assertRaises(intervention.InterventionError):
            _request(doc, **overrides)
        self.assertEqual(doc, before)
        self.assertEqual(len(doc["interventions"]), 1)

    def test_unknown_taxonomy_type_rejected(self):
        self._assert_rejected(type_="HUMAN_SOMETHING_ELSE")

    def test_lowercase_variant_of_a_real_type_rejected(self):
        self._assert_rejected(type_="human_privileged_action")

    def test_invalid_scope_rejected(self):
        for scope in ("global", "TASK", "", None):
            with self.subTest(scope=scope):
                self._assert_rejected(scope=scope)

    def test_task_scope_without_task_id_rejected(self):
        for task_id in (None, "", "   "):
            with self.subTest(task_id=task_id):
                self._assert_rejected(scope="task", task_id=task_id)

    def test_systemic_scope_with_a_task_id_rejected(self):
        self._assert_rejected(scope="systemic", task_id="T-01")

    def test_blank_reason_rejected(self):
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                self._assert_rejected(reason=reason)

    def test_malformed_condition_code_rejected(self):
        for code in ("", None, "   ", "Bad_Code", "has space", "_leading",
                     "-leading", "trailing!", "unicodeé"):
            with self.subTest(condition_code=code):
                self._assert_rejected(condition_code=code)

    def test_well_formed_condition_codes_accepted(self):
        for code in ("a", "9", "gh_auth_expired", "host-unreachable", "code9"):
            with self.subTest(condition_code=code):
                doc = _fresh_doc()
                record, _ = _request(doc, condition_code=code)
                self.assertEqual(record["condition_code"], code)


# ----------------------------------------------------------------- acknowledge


class TestAcknowledge(unittest.TestCase):
    def test_open_becomes_acknowledged(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        with _clock_at(_moment(12, 5, 0)):
            returned, is_first = intervention.acknowledge(
                doc, record["id"], by="serina", tz=TZ)
        self.assertIs(returned, record)
        self.assertTrue(is_first)
        self.assertEqual(record["status"], "ACKNOWLEDGED")

    def test_acknowledged_timestamp_and_actor_are_stored(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        moment = _moment(12, 5, 30)
        with _clock_at(moment):
            intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.assertEqual(record["acknowledged_at"], clock.iso(moment))
        self.assertEqual(record["acknowledged_by"], "serina")

    def test_repeat_acknowledgement_reports_it_is_not_the_first(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(18, 0, 0)):
            returned, is_first = intervention.acknowledge(
                doc, record["id"], by="someone-else", tz=TZ)
        self.assertIs(returned, record)
        self.assertFalse(is_first)

    def test_repeat_acknowledgement_preserves_the_original_stamp_exactly(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 5, 0), by="serina")
        snapshot = copy.deepcopy(record)

        with _clock_at(_moment(23, 59, 59)):
            intervention.acknowledge(doc, record["id"], by="someone-else", tz=TZ)

        self.assertEqual(record, snapshot)
        self.assertEqual(record["acknowledged_at"], clock.iso(_moment(12, 5, 0)))
        self.assertEqual(record["acknowledged_by"], "serina")

    def test_unknown_id_rejected_without_mutation(self):
        doc = _fresh_doc()
        _request(doc)
        before = copy.deepcopy(doc)
        with self.assertRaises(intervention.InterventionError):
            intervention.acknowledge(doc, "INT-" + "0" * 16, by="serina", tz=TZ)
        self.assertEqual(doc, before)

    def test_unknown_id_rejected_on_a_legacy_document(self):
        doc = _legacy_doc()
        before = copy.deepcopy(doc)
        with self.assertRaises(intervention.InterventionError):
            intervention.acknowledge(doc, "INT-" + "0" * 16, by="serina", tz=TZ)
        self.assertEqual(doc, before)

    def test_resolved_intervention_cannot_be_acknowledged(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome="FAIL", by="serina", tz=TZ)
        before = copy.deepcopy(doc)

        with self.assertRaises(intervention.InterventionError):
            intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.assertEqual(doc, before)

    def test_blank_actor_rejected_without_mutation(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        before = copy.deepcopy(doc)
        for by in ("", "   "):
            with self.subTest(by=by):
                with self.assertRaises(intervention.InterventionError):
                    intervention.acknowledge(doc, record["id"], by=by, tz=TZ)
                self.assertEqual(doc, before)

    def test_corrupt_status_rejected_without_mutation(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        record["status"] = "PENDING"
        before = copy.deepcopy(doc)

        with self.assertRaises(intervention.InterventionError):
            intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.assertEqual(doc, before)
        self.assertEqual(record["status"], "PENDING")


# --------------------------------------------------------------------- resolve


class TestResolve(unittest.TestCase):
    def test_open_cannot_be_resolved_directly(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        before = copy.deepcopy(doc)

        with self.assertRaises(intervention.InterventionError):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertEqual(doc, before)
        self.assertEqual(record["status"], "OPEN")

    def test_acknowledged_becomes_resolved(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 30)):
            returned = intervention.resolve(
                doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertIs(returned, record)
        self.assertEqual(record["status"], "RESOLVED")

    def test_resolution_fields_are_stored(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        moment = _moment(12, 1, 30)
        with _clock_at(moment):
            intervention.resolve(doc, record["id"], outcome="CLEAR_GUARDRAIL",
                                 by="serina", tz=TZ, note="host headroom restored")
        self.assertEqual(record["resolved_at"], clock.iso(moment))
        self.assertEqual(record["resolved_by"], "serina")
        self.assertEqual(record["resolution"], "CLEAR_GUARDRAIL")
        self.assertEqual(record["resolution_note"], "host headroom restored")

    def test_note_defaults_to_none(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome="NO_ACTION",
                                 by="serina", tz=TZ)
        self.assertIsNone(record["resolution_note"])

    def test_every_governed_outcome_is_accepted(self):
        self.assertEqual(
            intervention.RESOLUTION_OUTCOMES,
            frozenset({"RETRY", "FAIL", "RESUME", "CLEAR_GUARDRAIL", "FREEZE",
                       "NO_ACTION"}),
        )
        for outcome in sorted(intervention.RESOLUTION_OUTCOMES):
            with self.subTest(outcome=outcome):
                doc = _fresh_doc()
                record = _acknowledged(doc)
                with _clock_at(_moment(12, 1, 0)):
                    intervention.resolve(doc, record["id"], outcome=outcome,
                                         by="serina", tz=TZ)
                self.assertEqual(record["status"], "RESOLVED")
                self.assertEqual(record["resolution"], outcome)

    def test_ungoverned_outcome_rejected_without_mutation(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        before = copy.deepcopy(doc)
        for outcome in ("APPROVE", "retry", "", None, "CLEAR"):
            with self.subTest(outcome=outcome):
                with self.assertRaises(intervention.InterventionError):
                    intervention.resolve(doc, record["id"], outcome=outcome,
                                         by="serina", tz=TZ)
                self.assertEqual(doc, before)

    def test_blank_actor_rejected_without_mutation(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        before = copy.deepcopy(doc)
        for by in ("", "   "):
            with self.subTest(by=by):
                with self.assertRaises(intervention.InterventionError):
                    intervention.resolve(doc, record["id"], outcome="RETRY",
                                         by=by, tz=TZ)
                self.assertEqual(doc, before)

    def test_double_resolution_rejected_without_mutation(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        before = copy.deepcopy(doc)

        with _clock_at(_moment(13, 0, 0)):
            with self.assertRaises(intervention.InterventionError):
                intervention.resolve(doc, record["id"], outcome="FAIL",
                                     by="someone-else", tz=TZ)
        self.assertEqual(doc, before)

    def test_unknown_id_rejected_without_mutation(self):
        doc = _fresh_doc()
        _acknowledged(doc)
        before = copy.deepcopy(doc)
        with self.assertRaises(intervention.InterventionError):
            intervention.resolve(doc, "INT-" + "0" * 16, outcome="RETRY",
                                 by="serina", tz=TZ)
        self.assertEqual(doc, before)

    def test_corrupt_status_rejected_without_mutation(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        record["status"] = "IN_PROGRESS"
        before = copy.deepcopy(doc)

        with self.assertRaises(intervention.InterventionError):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertEqual(doc, before)
        self.assertEqual(record["status"], "IN_PROGRESS")

    def test_negative_duration_rejected_and_record_left_unchanged(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 5, 0))
        before = copy.deepcopy(doc)

        with _clock_at(_moment(12, 0, 0)):
            with self.assertRaises(intervention.InterventionError):
                intervention.resolve(doc, record["id"], outcome="RETRY",
                                     by="serina", tz=TZ)

        self.assertEqual(doc, before)
        self.assertEqual(record["status"], "ACKNOWLEDGED")
        self.assertIsNone(record["resolved_at"])
        self.assertIsNone(record["resolution"])
        self.assertIsNone(record["active_human_seconds"])

    def test_active_human_seconds_is_an_exact_integer_duration(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        with _clock_at(_moment(12, 1, 30)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)

        self.assertEqual(record["active_human_seconds"], 90)
        self.assertIsInstance(record["active_human_seconds"], int)
        self.assertNotIsInstance(record["active_human_seconds"], float)

    def test_zero_second_turnaround_is_stored_as_zero_not_none(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        with _clock_at(_moment(12, 0, 0)):
            intervention.resolve(doc, record["id"], outcome="NO_ACTION",
                                 by="serina", tz=TZ)
        self.assertEqual(record["active_human_seconds"], 0)


class TestActiveHumanMinutes(unittest.TestCase):
    def test_none_until_resolved(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        self.assertIsNone(intervention.active_human_minutes(record))

    def test_derives_the_presentation_value(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        with _clock_at(_moment(12, 1, 30)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertEqual(intervention.active_human_minutes(record), 1.5)

    def test_rounding_happens_only_at_the_reporting_boundary(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        with _clock_at(_moment(12, 1, 35)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)

        self.assertEqual(record["active_human_seconds"], 95)
        self.assertEqual(intervention.active_human_minutes(record), 1.6)

    def test_does_not_mutate_the_durable_record(self):
        doc = _fresh_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        with _clock_at(_moment(12, 1, 35)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)

        before = copy.deepcopy(doc)
        intervention.active_human_minutes(record)
        self.assertEqual(doc, before)


# -------------------------------------------------------- no outcome execution


class TestResolutionExecutesNothing(unittest.TestCase):
    """C-08b.1 records a human's decision. Executing it is C-08b.2's job.

    Each test resolves with an outcome whose name describes an action, then
    proves nothing but doc["interventions"] moved.
    """

    def _loaded_doc(self) -> dict:
        """A document with real, non-default values in every subtree a
        resolution might be tempted to touch — so "unchanged" is a meaningful
        assertion rather than a comparison of empty dicts."""
        doc = state.initial_document("run-002", "2.0")
        doc["started_at"] = "2026-09-26T00:00:00+12:00"
        doc["frozen_at"] = None
        doc["baseline_sha"] = "a" * 40
        state.add_task(doc, "T-01", "build the thing", [], "feature", False, TZ)
        doc["tasks"]["T-01"]["state"] = "BLOCKED"
        doc["tasks"]["T-01"]["attempts"] = 2
        doc["red_guardrail"] = {"guardrail": "HOST_HEADROOM",
                                "detail": "free disk below floor",
                                "at": "2026-09-26T11:00:00+12:00"}
        doc["budget"]["total_usd"] = 100.0
        doc["budget"]["spent_usd"] = 42.5
        doc["budget"]["hard_stop"] = True
        doc["counters"]["human_interventions"] = 3
        doc["counters"]["guardrail_activations"] = 1
        return doc

    def _resolve_with(self, outcome: str) -> tuple[dict, dict]:
        doc = self._loaded_doc()
        record = _acknowledged(doc, at=_moment(12, 0, 0))
        before = copy.deepcopy({k: v for k, v in doc.items() if k != "interventions"})
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome=outcome,
                                 by="serina", tz=TZ)
        after = {k: v for k, v in doc.items() if k != "interventions"}
        return before, after

    def test_clear_guardrail_does_not_clear_the_guardrail(self):
        before, after = self._resolve_with("CLEAR_GUARDRAIL")
        self.assertEqual(after["red_guardrail"], before["red_guardrail"])
        self.assertEqual(after, before)

    def test_retry_does_not_touch_tasks(self):
        before, after = self._resolve_with("RETRY")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["tasks"]["T-01"]["state"], "BLOCKED")
        self.assertEqual(after["tasks"]["T-01"]["attempts"], 2)
        self.assertEqual(after, before)

    def test_freeze_does_not_freeze_the_experiment(self):
        before, after = self._resolve_with("FREEZE")
        self.assertIsNone(after["frozen_at"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after, before)

    def test_resume_does_not_touch_the_budget(self):
        before, after = self._resolve_with("RESUME")
        self.assertEqual(after["budget"], before["budget"])
        self.assertTrue(after["budget"]["hard_stop"])
        self.assertEqual(after, before)

    def test_no_outcome_increments_the_counters(self):
        """Counter and ledger bookkeeping belong to the caller, in the same
        two-step pattern supervisor.py already uses. This module never does it
        on the caller's behalf, or it would be counted twice."""
        for outcome in sorted(intervention.RESOLUTION_OUTCOMES):
            with self.subTest(outcome=outcome):
                before, after = self._resolve_with(outcome)
                self.assertEqual(after["counters"], before["counters"])
                self.assertEqual(after["counters"]["human_interventions"], 3)

    def test_request_and_acknowledge_execute_nothing_either(self):
        doc = self._loaded_doc()
        before = copy.deepcopy({k: v for k, v in doc.items() if k != "interventions"})
        record, _ = _request(doc)
        with _clock_at(_moment(12, 0, 0)):
            intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        after = {k: v for k, v in doc.items() if k != "interventions"}
        self.assertEqual(after, before)


# ----------------------------------------------------------- simultaneous open


class TestSimultaneousOpen(unittest.TestCase):
    def test_open_intervention_counts(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        self.assertEqual(intervention.simultaneous_open_count(doc), 1)
        self.assertEqual(intervention.open_interventions(doc), [record])

    def test_acknowledged_intervention_still_counts(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        self.assertEqual(record["status"], "ACKNOWLEDGED")
        self.assertEqual(intervention.simultaneous_open_count(doc), 1)

    def test_resolved_intervention_does_not_count(self):
        doc = _fresh_doc()
        record = _acknowledged(doc)
        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertEqual(intervention.simultaneous_open_count(doc), 0)
        self.assertEqual(intervention.open_interventions(doc), [])

    def test_count_across_the_full_lifecycle_is_one_one_zero(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        self.assertEqual(intervention.simultaneous_open_count(doc), 1)

        with _clock_at(_moment(12, 0, 0)):
            intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.assertEqual(intervention.simultaneous_open_count(doc), 1)

        with _clock_at(_moment(12, 1, 0)):
            intervention.resolve(doc, record["id"], outcome="RETRY", by="serina", tz=TZ)
        self.assertEqual(intervention.simultaneous_open_count(doc), 0)

    def test_counts_several_simultaneous_interventions(self):
        doc = _fresh_doc()
        _request(doc, task_id="T-01")
        _request(doc, task_id="T-02")
        _request(doc, scope="systemic", task_id=None, condition_code="budget_exhausted")
        self.assertEqual(intervention.simultaneous_open_count(doc), 3)

    def test_legacy_document_with_no_interventions_key_returns_zero(self):
        doc = _legacy_doc()
        self.assertEqual(intervention.simultaneous_open_count(doc), 0)
        self.assertEqual(intervention.open_interventions(doc), [])
        self.assertNotIn("interventions", doc)

    def test_empty_interventions_map_returns_zero(self):
        self.assertEqual(intervention.simultaneous_open_count(_fresh_doc()), 0)

    def test_corrupt_status_fails_closed_rather_than_miscounting(self):
        doc = _fresh_doc()
        record, _ = _request(doc)
        record["status"] = "OPENED"
        with self.assertRaises(intervention.InterventionError):
            intervention.simultaneous_open_count(doc)

    def test_one_corrupt_record_among_healthy_ones_still_fails_closed(self):
        doc = _fresh_doc()
        _request(doc, task_id="T-01")
        broken, _ = _request(doc, task_id="T-02")
        _request(doc, task_id="T-03")
        broken["status"] = None

        with self.assertRaises(intervention.InterventionError):
            intervention.simultaneous_open_count(doc)


# ------------------------------------------------------------------- CLI layer
#
# These drive control/cli.py's ctl human-* commands end to end against an
# isolated state file and ledger. The property they exist to pin is the
# ORDERING: the state transaction commits first, the ledger append happens
# after it closes. That is what makes a ledger failure a repairable lag rather
# than a lost acknowledgement, and several tests below assert it directly by
# reading the state file from disk at the moment append is called.


def _fake_config() -> types.SimpleNamespace:
    """Only the two fields the human-* commands read. Avoids depending on the
    real config/experiment.json, and keeps the secrets file out of the test
    process entirely."""
    return types.SimpleNamespace(timezone=TZ, experiment_id="run-002")


class CliCase(unittest.TestCase):
    """Isolated state file and ledger, stub config, no contact with .runtime."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state_path = root / "state.json"
        self.ledger_path = root / "ledger.jsonl"

        for patcher in (
            mock.patch.object(cli.config, "STATE_PATH", self.state_path),
            mock.patch.object(cli.config, "LEDGER_PATH", self.ledger_path),
            mock.patch.object(cli.config, "load", _fake_config),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ------------------------------------------------------------- fixtures
    def write_doc(self, doc: dict) -> None:
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def seed(self, *, status: str = "OPEN", outcome: str = "RETRY",
             note: str | None = "host restarted", by: str = "serina",
             **request_kw) -> str:
        """One intervention on disk, driven to the requested status through the
        real lifecycle functions under a frozen clock."""
        doc = state.initial_document("run-002", "2.0")
        record, _ = _request(doc, **request_kw)
        if status in ("ACKNOWLEDGED", "RESOLVED"):
            with _clock_at(_moment(12, 0, 0)):
                intervention.acknowledge(doc, record["id"], by=by, tz=TZ)
        if status == "RESOLVED":
            with _clock_at(_moment(12, 1, 30)):
                intervention.resolve(doc, record["id"], outcome=outcome, by=by,
                                     tz=TZ, note=note)
        self.write_doc(doc)
        return record["id"]

    def corrupt_status(self, intervention_id: str, status) -> None:
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        doc["interventions"][intervention_id]["status"] = status
        self.write_doc(doc)

    # ---------------------------------------------------------- inspection
    def stored(self, intervention_id: str) -> dict:
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        return doc["interventions"][intervention_id]

    def events(self, event_type: str | None = None) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return ledger_mod.Ledger(path=self.ledger_path, tz=TZ,
                                 experiment_id="run-002").events(event_type)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    # -------------------------------------------------------------- doubles
    def exploding_ledger(self):
        """Ledger.append failing the way a full or read-only disk fails —
        after the state transaction has already committed."""
        return mock.patch.object(ledger_mod.Ledger, "append",
                                 side_effect=OSError("no space left on device"))

    def forbidden_getuser(self):
        """getpass.getuser() wired to blow up. A repair path that consults it
        fails the test loudly instead of silently rewriting the actor."""
        return mock.patch.object(
            cli.getpass, "getuser",
            side_effect=AssertionError("getpass.getuser() consulted on a repair"))


class TestCliHumanList(CliCase):
    def seed_three(self) -> dict[str, str]:
        doc = state.initial_document("run-002", "2.0")
        open_rec, _ = _request(doc, task_id="T-01", condition_code="open_one")
        ack_rec, _ = _request(doc, task_id="T-02", condition_code="ack_one")
        res_rec, _ = _request(doc, task_id="T-03", condition_code="res_one")
        with _clock_at(_moment(12, 0, 0)):
            intervention.acknowledge(doc, ack_rec["id"], by="serina", tz=TZ)
            intervention.acknowledge(doc, res_rec["id"], by="serina", tz=TZ)
        with _clock_at(_moment(12, 1, 30)):
            intervention.resolve(doc, res_rec["id"], outcome="RETRY", by="serina",
                                 tz=TZ, note="done")
        self.write_doc(doc)
        return {"OPEN": open_rec["id"], "ACKNOWLEDGED": ack_rec["id"],
                "RESOLVED": res_rec["id"]}

    def test_default_filter_lists_only_open(self):
        ids = self.seed_three()
        code, out = self.run_cli(["human-list"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status_filter"], "OPEN")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["interventions"][0]["id"], ids["OPEN"])

    def test_each_status_filter_selects_only_that_status(self):
        ids = self.seed_three()
        for status, expected_id in ids.items():
            with self.subTest(status=status):
                code, out = self.run_cli(["human-list", "--status", status])
                payload = json.loads(out)
                self.assertEqual(code, 0)
                self.assertEqual(payload["count"], 1)
                self.assertEqual(payload["interventions"][0]["id"], expected_id)
                self.assertEqual(payload["interventions"][0]["status"], status)

    def test_all_lists_every_status(self):
        ids = self.seed_three()
        code, out = self.run_cli(["human-list", "--status", "ALL"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 3)
        self.assertEqual({r["id"] for r in payload["interventions"]}, set(ids.values()))

    def test_presentation_only_minutes_are_added_without_touching_state(self):
        ids = self.seed_three()
        code, out = self.run_cli(["human-list", "--status", "RESOLVED"])
        record = json.loads(out)["interventions"][0]
        self.assertEqual(code, 0)
        self.assertEqual(record["active_human_seconds"], 90)
        self.assertEqual(record["active_human_minutes"], 1.5)
        # The durable record still carries seconds only.
        self.assertNotIn("active_human_minutes", self.stored(ids["RESOLVED"]))

    def test_open_and_acknowledged_report_null_minutes(self):
        self.seed_three()
        for status in ("OPEN", "ACKNOWLEDGED"):
            with self.subTest(status=status):
                _, out = self.run_cli(["human-list", "--status", status])
                self.assertIsNone(
                    json.loads(out)["interventions"][0]["active_human_minutes"])

    def test_listing_mutates_neither_state_nor_ledger(self):
        self.seed_three()
        before = self.state_path.read_bytes()
        for argv in (["human-list"], ["human-list", "--status", "ALL"]):
            self.run_cli(argv)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse(self.ledger_path.exists())

    def test_empty_state_lists_nothing(self):
        self.write_doc(state.initial_document("run-002", "2.0"))
        code, out = self.run_cli(["human-list", "--status", "ALL"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 0)


class TestCliHumanAcknowledge(CliCase):
    def test_fresh_acknowledgement_defaults_the_actor_to_the_invoking_user(self):
        iid = self.seed(status="OPEN")
        with mock.patch.object(cli.getpass, "getuser", return_value="ci-bot"):
            with _clock_at(_moment(12, 5, 0)):
                code, _ = self.run_cli(["human-acknowledge", iid])
        self.assertEqual(code, 0)
        record = self.stored(iid)
        self.assertEqual(record["status"], "ACKNOWLEDGED")
        self.assertEqual(record["acknowledged_by"], "ci-bot")
        self.assertEqual(record["acknowledged_at"], clock.iso(_moment(12, 5, 0)))

    def test_explicit_actor_overrides_the_default(self):
        iid = self.seed(status="OPEN")
        with self.forbidden_getuser():
            code, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        self.assertEqual(code, 0)
        self.assertEqual(self.stored(iid)["acknowledged_by"], "serina")

    def test_ledger_append_happens_after_the_state_transaction_commits(self):
        """The ordering the whole design rests on. At the moment append is
        called, the acknowledgement must already be readable from the state
        file on disk — not merely present in the in-memory document."""
        iid = self.seed(status="OPEN")
        seen: dict = {}
        real_append = ledger_mod.Ledger.append

        def spy(ledger_self, event_type, **fields):
            seen["record_on_disk"] = self.stored(iid)
            return real_append(ledger_self, event_type, **fields)

        with mock.patch.object(ledger_mod.Ledger, "append", spy):
            code, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])

        self.assertEqual(code, 0)
        self.assertEqual(seen["record_on_disk"]["status"], "ACKNOWLEDGED")
        self.assertEqual(seen["record_on_disk"]["acknowledged_by"], "serina")
        self.assertIsNotNone(seen["record_on_disk"]["acknowledged_at"])

    def test_one_event_appended_with_canonical_values(self):
        iid = self.seed(status="OPEN")
        with _clock_at(_moment(12, 5, 0)):
            code, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        events = self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")
        self.assertEqual(code, 0)
        self.assertEqual(len(events), 1)
        metadata = events[0]["metadata_redacted"]
        record = self.stored(iid)
        self.assertEqual(metadata["intervention_id"], iid)
        self.assertEqual(metadata["acknowledged_at"], record["acknowledged_at"])
        self.assertEqual(metadata["acknowledged_by"], record["acknowledged_by"])
        self.assertEqual(metadata["condition_code"], record["condition_code"])
        self.assertEqual(events[0]["human_intervention"], True)
        self.assertEqual(events[0]["outcome"], "ACKNOWLEDGED")
        self.assertEqual(events[0]["task_id"], record["task_id"])

    def test_ledger_failure_returns_one_and_says_state_committed(self):
        iid = self.seed(status="OPEN")
        with self.exploding_ledger():
            code, out = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        self.assertEqual(code, 1)
        self.assertIn("STATE COMMITTED BUT LEDGER EVIDENCE FAILED", out)
        self.assertIn("Rerun", out)
        # State is durable; evidence is not yet written.
        self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")
        self.assertEqual(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED"), [])

    def test_retry_after_ledger_failure_repairs_exactly_one_event(self):
        iid = self.seed(status="OPEN")
        with self.exploding_ledger():
            first, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        second, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")), 1)

    def test_repeated_acknowledgement_with_evidence_present_appends_nothing(self):
        iid = self.seed(status="OPEN")
        self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        self.assertEqual(code, 0)
        self.assertIn("already present", out)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")), 1)
        self.assertEqual(self.stored(iid), before)

    def test_repair_without_by_uses_the_stored_actor_and_never_asks_the_os(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        with self.forbidden_getuser():
            code, _ = self.run_cli(["human-acknowledge", iid])

        self.assertEqual(code, 0)
        events = self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata_redacted"]["acknowledged_by"], "serina")
        self.assertEqual(self.stored(iid)["acknowledged_by"], "serina")

    def test_repair_with_matching_actor_is_allowed(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, _ = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")), 1)
        self.assertEqual(self.stored(iid), before)

    def test_repair_with_a_different_actor_is_rejected(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-acknowledge", iid, "--by", "mallory"])
        self.assertEqual(code, 1)
        self.assertIn("already acknowledged by serina", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED"), [])

    def test_resolved_intervention_is_rejected(self):
        iid = self.seed(status="RESOLVED")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
        self.assertEqual(code, 1)
        self.assertIn("already RESOLVED", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED"), [])

    def test_unknown_id_fails_closed(self):
        self.seed(status="OPEN")
        code, out = self.run_cli(["human-acknowledge", "INT-" + "0" * 16])
        self.assertEqual(code, 1)
        self.assertIn("unknown intervention", out)
        self.assertEqual(self.events(), [])

    def test_corrupt_status_fails_closed(self):
        for bad in ("PENDING", None, "", "open"):
            with self.subTest(status=bad):
                iid = self.seed(status="OPEN")
                self.corrupt_status(iid, bad)
                before = copy.deepcopy(self.stored(iid))

                code, out = self.run_cli(["human-acknowledge", iid, "--by", "serina"])
                self.assertEqual(code, 1)
                self.assertIn("unrecognised status", out)
                self.assertEqual(self.stored(iid), before)
                self.assertEqual(self.events(), [])


class TestCliHumanResolve(CliCase):
    def test_open_cannot_be_resolved(self):
        iid = self.seed(status="OPEN")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY"])
        self.assertEqual(code, 1)
        self.assertIn("must be acknowledged", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events(), [])

    def test_fresh_resolution_stores_the_decision_and_logs_it_once(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        with _clock_at(_moment(12, 1, 30)):
            code, _ = self.run_cli(["human-resolve", iid, "--outcome", "CLEAR_GUARDRAIL",
                                    "--note", "headroom restored", "--by", "serina"])

        record = self.stored(iid)
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "RESOLVED")
        self.assertEqual(record["resolution"], "CLEAR_GUARDRAIL")
        self.assertEqual(record["resolution_note"], "headroom restored")
        self.assertEqual(record["resolved_by"], "serina")
        self.assertEqual(record["resolved_at"], clock.iso(_moment(12, 1, 30)))
        self.assertEqual(record["active_human_seconds"], 90)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_RESOLVED")), 1)

    def test_fresh_resolution_defaults_the_actor_to_the_invoking_user(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        with mock.patch.object(cli.getpass, "getuser", return_value="ci-bot"):
            with _clock_at(_moment(12, 1, 30)):
                code, _ = self.run_cli(["human-resolve", iid, "--outcome", "RETRY"])
        self.assertEqual(code, 0)
        self.assertEqual(self.stored(iid)["resolved_by"], "ci-bot")

    def test_ledger_append_happens_after_the_state_transaction_commits(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        seen: dict = {}
        real_append = ledger_mod.Ledger.append

        def spy(ledger_self, event_type, **fields):
            seen["record_on_disk"] = self.stored(iid)
            return real_append(ledger_self, event_type, **fields)

        with mock.patch.object(ledger_mod.Ledger, "append", spy):
            with _clock_at(_moment(12, 1, 30)):
                code, _ = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                        "--by", "serina"])

        self.assertEqual(code, 0)
        self.assertEqual(seen["record_on_disk"]["status"], "RESOLVED")
        self.assertEqual(seen["record_on_disk"]["resolution"], "RETRY")
        self.assertEqual(seen["record_on_disk"]["active_human_seconds"], 90)

    def test_ledger_failure_then_identical_retry_repairs_exactly_one_event(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        argv = ["human-resolve", iid, "--outcome", "RETRY", "--note", "host restarted",
                "--by", "serina"]

        with self.exploding_ledger():
            with _clock_at(_moment(12, 1, 30)):
                first, out = self.run_cli(argv)

        self.assertEqual(first, 1)
        self.assertIn("STATE COMMITTED BUT LEDGER EVIDENCE FAILED", out)
        self.assertIn("Rerun", out)
        self.assertEqual(self.stored(iid)["status"], "RESOLVED")
        self.assertEqual(self.events("HUMAN_INTERVENTION_RESOLVED"), [])

        after_failure = copy.deepcopy(self.stored(iid))
        second, _ = self.run_cli(argv)

        self.assertEqual(second, 0)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_RESOLVED")), 1)
        self.assertEqual(self.stored(iid), after_failure)

    def test_repair_payload_is_derived_from_stored_state_not_from_the_arguments(self):
        """The retry supplies only --outcome. Everything else in the event must
        come from the committed record.

        The free-text note is deliberately NOT carried into the ledger: a note
        holding a secret-shaped string would be rewritten to GUARDRAIL_RED by
        control/redact.py, which carries no intervention_id, so _ledger_has_event
        would never find it and every rerun would append another line. The note
        stays in durable state, where it is not subject to that guard.
        """
        iid = self.seed(status="RESOLVED", outcome="FREEZE", by="serina",
                        note="operator decided to stop the run")
        record = self.stored(iid)

        code, _ = self.run_cli(["human-resolve", iid, "--outcome", "FREEZE"])
        self.assertEqual(code, 0)

        events = self.events("HUMAN_INTERVENTION_RESOLVED")
        self.assertEqual(len(events), 1)
        metadata = events[0]["metadata_redacted"]
        self.assertEqual(metadata["intervention_id"], iid)
        self.assertEqual(metadata["resolved_by"], record["resolved_by"])
        self.assertEqual(metadata["resolved_at"], record["resolved_at"])
        self.assertEqual(metadata["resolution"], record["resolution"])
        self.assertEqual(metadata["acknowledged_by"], record["acknowledged_by"])
        self.assertEqual(metadata["active_human_seconds"],
                         record["active_human_seconds"])
        self.assertEqual(metadata["active_human_minutes"],
                         intervention.active_human_minutes(record))
        self.assertEqual(events[0]["outcome"], record["resolution"])
        self.assertNotIn("resolution_note", metadata)

    def test_repair_with_a_different_outcome_is_rejected(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "FAIL"])
        self.assertEqual(code, 1)
        self.assertIn("already RESOLVED as RETRY", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events("HUMAN_INTERVENTION_RESOLVED"), [])

    def test_repair_with_a_different_actor_is_rejected(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--by", "mallory"])
        self.assertEqual(code, 1)
        self.assertIn("already resolved by serina", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events("HUMAN_INTERVENTION_RESOLVED"), [])

    def test_repair_with_a_different_note_is_rejected(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY", note="host restarted")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--note", "something else entirely"])
        self.assertEqual(code, 1)
        self.assertIn("note differs", out)
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events("HUMAN_INTERVENTION_RESOLVED"), [])

    def test_repair_without_by_uses_the_stored_actor_and_never_asks_the_os(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY", by="serina")
        with self.forbidden_getuser():
            code, _ = self.run_cli(["human-resolve", iid, "--outcome", "RETRY"])

        self.assertEqual(code, 0)
        events = self.events("HUMAN_INTERVENTION_RESOLVED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata_redacted"]["resolved_by"], "serina")
        self.assertEqual(self.stored(iid)["resolved_by"], "serina")

    def test_existing_resolution_evidence_is_never_duplicated(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY", by="serina")
        argv = ["human-resolve", iid, "--outcome", "RETRY"]

        first, _ = self.run_cli(argv)
        before = copy.deepcopy(self.stored(iid))
        second, out = self.run_cli(argv)
        third, _ = self.run_cli(argv)

        self.assertEqual((first, second, third), (0, 0, 0))
        self.assertIn("already resolved", out)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_RESOLVED")), 1)
        self.assertEqual(self.stored(iid), before)

    def test_unknown_id_fails_closed(self):
        self.seed(status="ACKNOWLEDGED")
        code, out = self.run_cli(["human-resolve", "INT-" + "0" * 16,
                                  "--outcome", "RETRY"])
        self.assertEqual(code, 1)
        self.assertIn("unknown intervention", out)
        self.assertEqual(self.events(), [])

    def test_corrupt_status_fails_closed(self):
        for bad in ("IN_PROGRESS", None, "", "resolved"):
            with self.subTest(status=bad):
                iid = self.seed(status="ACKNOWLEDGED")
                self.corrupt_status(iid, bad)
                before = copy.deepcopy(self.stored(iid))

                code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY"])
                self.assertEqual(code, 1)
                self.assertIn("unrecognised status", out)
                self.assertEqual(self.stored(iid), before)
                self.assertEqual(self.events(), [])


class TestCliSecretShapedInput(CliCase):
    """C-08b.1 Option A: operator-supplied free text is refused at the CLI
    boundary if it looks like a secret, before any transaction opens.

    SECRET_SHAPED is built by concatenation on purpose. It has to be long
    enough to trip control/redact.py's `\\bgh[pousr]_[A-Za-z0-9]{16,}` and short
    enough NOT to trip scripts/check_no_secrets.py's stricter `{30,}` — this
    file is destined to be tracked, and that CI guardrail scans tracked files.
    Building it from parts means the literal never appears in the source at
    all, which is a second, independent reason the scanner stays quiet. If you
    lengthen it, you will fail CI.
    """

    SECRET_SHAPED = "ghp_" + "A" * 20

    def assert_untouched(self, iid: str, before: dict, out: str) -> None:
        """A refusal must leave state and ledger alone and must never echo the
        supplied value back to the terminal."""
        self.assertEqual(self.stored(iid), before)
        self.assertEqual(self.events(), [])
        self.assertNotIn(self.SECRET_SHAPED, out)
        self.assertIn("secret", out)

    # ------------------------------------------------------------------ note
    def test_secret_shaped_note_is_rejected_with_no_side_effects(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--note", self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--note appears to", out)
        self.assert_untouched(iid, before, out)
        self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")

    def test_secret_embedded_in_a_longer_note_is_still_rejected(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))
        note = f"re-authenticated the CLI, token was {self.SECRET_SHAPED} - fixed now"

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--note", note])
        self.assertEqual(code, 1)
        self.assert_untouched(iid, before, out)

    # ------------------------------------------------------------------- by
    def test_secret_shaped_by_on_acknowledge_is_rejected(self):
        iid = self.seed(status="OPEN")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-acknowledge", iid, "--by",
                                  self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--by appears to", out)
        self.assert_untouched(iid, before, out)
        self.assertEqual(self.stored(iid)["status"], "OPEN")

    def test_secret_shaped_by_on_resolve_is_rejected(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--by", self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--by appears to", out)
        self.assert_untouched(iid, before, out)

    # ---------------------------------------------------------- repair paths
    def test_secret_shaped_by_on_acknowledge_repair_is_rejected_first(self):
        """Ordering matters here, not just the exit code.

        Without the guard this would reach the stored-actor equality check,
        which refuses too — but whose message interpolates args.by and would
        therefore print the secret to the terminal. Asserting the value is
        absent from the output is what proves the guard ran first.
        """
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-acknowledge", iid, "--by",
                                  self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--by appears to", out)
        self.assertNotIn("already acknowledged by", out)
        self.assert_untouched(iid, before, out)

    def test_secret_shaped_by_on_resolve_repair_is_rejected_first(self):
        iid = self.seed(status="RESOLVED", outcome="RETRY", by="serina")
        before = copy.deepcopy(self.stored(iid))

        code, out = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                  "--by", self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--by appears to", out)
        self.assertNotIn("already resolved by", out)
        self.assert_untouched(iid, before, out)

    def test_guard_precedes_even_the_unknown_id_lookup(self):
        """The guard sits outside the transaction, so it fires before the
        record is ever read."""
        self.seed(status="OPEN")
        code, out = self.run_cli(["human-acknowledge", "INT-" + "0" * 16,
                                  "--by", self.SECRET_SHAPED])
        self.assertEqual(code, 1)
        self.assertIn("--by appears to", out)
        self.assertNotIn("unknown intervention", out)
        self.assertNotIn(self.SECRET_SHAPED, out)
        self.assertEqual(self.events(), [])

    # --------------------------------------------------------- non-regression
    def test_the_os_derived_user_is_never_rejected(self):
        """getpass.getuser() is the OS's answer, not operator free text. Even a
        username that happens to match a broad pattern must be accepted, or the
        command becomes unusable on a host with an awkward account name."""
        iid = self.seed(status="OPEN")
        with mock.patch.object(cli.getpass, "getuser",
                               return_value=self.SECRET_SHAPED):
            code, _ = self.run_cli(["human-acknowledge", iid])

        self.assertEqual(code, 0)
        self.assertEqual(self.stored(iid)["status"], "ACKNOWLEDGED")
        self.assertEqual(self.stored(iid)["acknowledged_by"], self.SECRET_SHAPED)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_ACKNOWLEDGED")), 1)

    def test_ordinary_notes_and_actors_are_unaffected(self):
        iid = self.seed(status="ACKNOWLEDGED", by="serina")
        with _clock_at(_moment(12, 1, 30)):
            code, _ = self.run_cli(["human-resolve", iid, "--outcome", "RETRY",
                                    "--note", "re-ran the gate after the host "
                                              "came back; nothing else changed",
                                    "--by", "serina"])
        record = self.stored(iid)
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "RESOLVED")
        self.assertEqual(record["resolved_by"], "serina")
        self.assertEqual(record["active_human_seconds"], 90)
        self.assertEqual(len(self.events("HUMAN_INTERVENTION_RESOLVED")), 1)

    def test_omitted_note_and_by_are_never_checked(self):
        iid = self.seed(status="OPEN")
        with mock.patch.object(cli.getpass, "getuser", return_value="serina"):
            # Both lifecycle stamps must be frozen, not just the resolve: an
            # acknowledgement taken from the wall clock drifts past the fixed
            # resolve instant and makes the human duration negative.
            with _clock_at(_moment(12, 0, 0)):
                ack, _ = self.run_cli(["human-acknowledge", iid])
            with _clock_at(_moment(12, 1, 30)):
                res, _ = self.run_cli(["human-resolve", iid, "--outcome", "FAIL"])
        self.assertEqual((ack, res), (0, 0))
        self.assertEqual(self.stored(iid)["status"], "RESOLVED")


if __name__ == "__main__":
    unittest.main()
