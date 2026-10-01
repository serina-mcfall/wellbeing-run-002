"""C-19: the adapter must actually be Jev, and a paid call must be admitted
before it is made.

Two halves, verified together because they are one repair:

1. TRANSPORT. `product/AI.md` - frozen and hash-pinned - says "Jev via
   OpenRouter Decisions API". The adapter used to call `chat/completions` with
   an inherited general-purpose chat model. These tests pin the endpoint, the
   request shape, the criteria, and defensive parsing of a response schema
   whose optional fields the documentation does not promise.

2. ADMISSION. A cost read out of a response cannot cap a call already made.
   The budget now commits a conservative allowance to durable state BEFORE the
   request leaves, and settles it afterwards against what actually happened.

The transport is mocked BENEATH the real adapter - `urlopen` is replaced, not
`jev.decide` and not `http.post_json` - so the request these tests inspect is
the one `control/http.py` would really have put on the wire.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    budget,
    config,
    http,
    jev,
    preflight,
    proc,
    state,
    supervisor,
)

TZ = "Pacific/Auckland"
MODEL = "typesafe/jev-1.13"
RETURNED_MODEL = "typesafe/jev-1.13-20260917"
ALLOWANCE = 0.002688
EVIDENCE = {"task": "TASK-001", "state": "ACTIVE", "attempts": 1,
            "minutes_since_progress": 12.5, "review_queue_depth": 0}


def answer_body(choice="HEALTHY", *, kind="worker_health", cost=0.000019992,
                confidence=0.67, model=RETURNED_MODEL, usage=None, answers=None):
    body = {}
    if model is not None:
        body["model"] = model
    if answers is None:
        answer = {"type": "choice", "choice": choice}
        if confidence is not None:
            answer["confidence"] = confidence
        answers = {kind: answer}
    body["answers"] = answers
    if usage is None:
        usage = {"input_tokens": 476, "output_tokens": 70}
        if cost is not None:
            usage["cost"] = cost
    body["usage"] = usage
    return json.dumps(body)


class _FakeSocket:
    """Stands in for the object urlopen returns, nothing more."""

    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """Records what would have gone on the wire; raises what we tell it to."""

    def __init__(self, status=200, body="", raises=None):
        self.status = status
        self.body = body
        self.raises = raises
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises
        return _FakeSocket(self.status, self.body)

    @property
    def called(self):
        return bool(self.calls)

    @property
    def payload(self):
        return json.loads(self.calls[-1].data.decode("utf-8"))

    @property
    def url(self):
        return self.calls[-1].full_url


def decide(transport, kind="worker_health", evidence=None, allowed=True,
           model=MODEL):
    with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
            mock.patch.object(http.urllib.request, "urlopen", transport):
        return jev.DecisionService(model).decide(
            kind, EVIDENCE if evidence is None else evidence, allowed=allowed)


def fresh_doc(total=25.0, spent=0.0):
    doc = {"budget": {}}
    budget.ensure(doc, total)
    doc["budget"]["spent_usd"] = spent
    return doc


# --------------------------------------------------------------- transport

class TestTheRequestIsADecisionsRequest(unittest.TestCase):
    def test_endpoint_is_the_decisions_api_not_chat_completions(self):
        transport = FakeTransport(200, answer_body())
        decide(transport)
        self.assertEqual(transport.url, "https://openrouter.ai/api/alpha/decisions")
        self.assertNotIn("chat/completions", transport.url)

    def test_the_configured_model_slug_is_sent_verbatim(self):
        transport = FakeTransport(200, answer_body())
        decide(transport)
        self.assertEqual(transport.payload["model"], MODEL)

    def test_no_alias_is_sent(self):
        """BOOTSTRAP item 6: an alias does not identify a build."""
        transport = FakeTransport(200, answer_body())
        decide(transport)
        self.assertNotIn("~", transport.payload["model"])
        self.assertNotIn("latest", transport.payload["model"])

    def test_the_question_is_a_choice_with_criteria_for_exactly_the_options(self):
        transport = FakeTransport(200, answer_body())
        decide(transport)
        question = transport.payload["questions"]["worker_health"]
        self.assertEqual(question["type"], "choice")
        self.assertTrue(question["instructions"])
        self.assertEqual(sorted(question["criteria"]),
                         sorted(jev.DECISIONS["worker_health"]["options"]))
        for text in question["criteria"].values():
            self.assertTrue(text.strip())

    def test_the_evidence_is_sent_as_state(self):
        transport = FakeTransport(200, answer_body())
        decide(transport)
        self.assertEqual(sorted(transport.payload["state"]), sorted(EVIDENCE))

    def test_no_chat_completion_fields_survive(self):
        transport = FakeTransport(200, answer_body())
        decide(transport)
        for gone in ("messages", "response_format", "max_tokens", "temperature"):
            self.assertNotIn(gone, transport.payload)

    def test_stalled_criteria_never_ask_for_an_unobservable_liveness_claim(self):
        """The evidence carries no liveness observation, so the criteria must
        not invite one. This is the approved wording, pinned."""
        stalled = jev.DECISIONS["worker_health"]["criteria"]["STALLED"]
        self.assertIn("do not conclude that a process has died", stalled)
        self.assertNotIn("has vanished", stalled)


class TestUnapprovedKindsNeverReachHttp(unittest.TestCase):
    def test_every_unapproved_kind_refuses_before_the_transport(self):
        for kind, spec in jev.DECISIONS.items():
            if kind == "worker_health":
                continue
            transport = FakeTransport(200, answer_body())
            decision = decide(transport, kind=kind)
            self.assertFalse(transport.called, kind)
            self.assertEqual(decision.source, "fallback")
            self.assertEqual(decision.choice, spec["fallback"])
            self.assertEqual(decision.error, jev.UNAPPROVED_KIND)
            self.assertEqual(decision.billing_state, budget.NOT_SENT)

    def test_the_frozen_vocabulary_is_preserved_not_removed(self):
        self.assertEqual(set(jev.DECISIONS), {
            "finding_severity", "worker_health", "queue_priority",
            "model_routing", "incident_classification"})


class TestAnswerParsing(unittest.TestCase):
    def test_a_valid_answer_is_accepted(self):
        decision = decide(FakeTransport(200, answer_body("SLOW")))
        self.assertEqual(decision.source, "jev")
        self.assertEqual(decision.choice, "SLOW")
        self.assertEqual(decision.confidence, 0.67)

    def test_off_enum_choice_still_falls_back(self):
        decision = decide(FakeTransport(200, answer_body("SPLENDID")))
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.choice, "HEALTHY")
        self.assertIn("off-schema", decision.error)

    def test_malformed_answers_fall_back_without_raising(self):
        cases = {
            "body is not an object": "[]",
            "body is not json": "<html>502</html>",
            "no answers key": json.dumps({"model": RETURNED_MODEL, "usage": {}}),
            "answers is not an object": json.dumps({"answers": "yes"}),
            "no answer for this kind": answer_body(kind="something_else"),
            "wrong answer type": json.dumps(
                {"answers": {"worker_health": {"type": "score", "score": 3}}}),
            "no choice field": json.dumps(
                {"answers": {"worker_health": {"type": "choice"}}}),
            "choice is not a string": json.dumps(
                {"answers": {"worker_health": {"type": "choice", "choice": 7}}}),
            "answer is not an object": json.dumps({"answers": {"worker_health": "HEALTHY"}}),
        }
        for label, body in cases.items():
            with self.subTest(label):
                decision = decide(FakeTransport(200, body))
                self.assertEqual(decision.source, "fallback")
                self.assertEqual(decision.choice, "HEALTHY")
                self.assertTrue(decision.error)

    def test_malformed_confidence_becomes_unknown_never_a_number(self):
        for value in ("high", True, 1.5, -0.2, None, float("nan")):
            with self.subTest(repr(value)):
                body = json.dumps({
                    "answers": {"worker_health": {
                        "type": "choice", "choice": "HEALTHY", "confidence": value}},
                })
                decision = decide(FakeTransport(200, body))
                self.assertEqual(decision.source, "jev")
                self.assertIsNone(decision.confidence)

    def test_absent_confidence_is_unknown_and_does_not_block_the_decision(self):
        decision = decide(FakeTransport(200, answer_body("STALLED", confidence=None)))
        self.assertEqual(decision.choice, "STALLED")
        self.assertIsNone(decision.confidence)


class TestModelProvenance(unittest.TestCase):
    def test_requested_and_returned_are_recorded_separately(self):
        decision = decide(FakeTransport(200, answer_body()))
        self.assertEqual(decision.requested_model, MODEL)
        self.assertEqual(decision.returned_model, RETURNED_MODEL)
        self.assertNotEqual(decision.requested_model, decision.returned_model)

    def test_a_response_naming_no_model_records_unknown_not_the_request(self):
        decision = decide(FakeTransport(200, answer_body(model=None)))
        self.assertEqual(decision.requested_model, MODEL)
        self.assertIsNone(decision.returned_model)

    def test_a_non_string_model_is_unknown(self):
        body = json.dumps({"model": 113, "answers": {
            "worker_health": {"type": "choice", "choice": "HEALTHY"}}})
        decision = decide(FakeTransport(200, body))
        self.assertIsNone(decision.returned_model)

    def test_provenance_is_recorded_even_when_the_answer_is_unusable(self):
        decision = decide(FakeTransport(200, answer_body("NONSENSE")))
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.returned_model, RETURNED_MODEL)


class TestCostAndBillingState(unittest.TestCase):
    def test_a_reported_cost_is_actual_spend(self):
        decision = decide(FakeTransport(200, answer_body()))
        self.assertEqual(decision.actual_cost_usd, 0.000019992)
        self.assertEqual(decision.billing_state, budget.BILLED)

    def test_a_billed_response_with_an_unusable_answer_is_still_billed(self):
        """The R7 regression: this used to return a fallback carrying no cost,
        so the money was spent and never counted."""
        decision = decide(FakeTransport(200, answer_body("NONSENSE")))
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.actual_cost_usd, 0.000019992)
        self.assertEqual(decision.billing_state, budget.BILLED)

    def test_absent_cost_is_unknown_never_zero(self):
        decision = decide(FakeTransport(200, answer_body(cost=None)))
        self.assertIsNone(decision.actual_cost_usd)
        self.assertEqual(decision.billing_state, budget.UNKNOWN)

    def test_invalid_costs_are_unknown_never_arithmetic(self):
        for value in ("0.0001", True, -0.5, float("inf"), float("nan"), None, {}):
            with self.subTest(repr(value)):
                decision = decide(FakeTransport(
                    200, answer_body(usage={"input_tokens": 1, "output_tokens": 1,
                                            "cost": value})))
                self.assertIsNone(decision.actual_cost_usd)
                self.assertEqual(decision.billing_state, budget.UNKNOWN)

    def test_token_counts_are_read_from_the_decisions_field_names(self):
        decision = decide(FakeTransport(200, answer_body()))
        self.assertEqual(decision.input_tokens, 476)
        self.assertEqual(decision.output_tokens, 70)

    def test_invalid_token_counts_are_unknown(self):
        for value in ("476", True, -1, 4.5, None):
            with self.subTest(repr(value)):
                decision = decide(FakeTransport(
                    200, answer_body(usage={"input_tokens": value, "output_tokens": 1})))
                self.assertIsNone(decision.input_tokens)

    def test_a_server_error_may_have_been_billed(self):
        transport = FakeTransport(raises=urllib.error.HTTPError(
            jev.ENDPOINT, 402, "Payment Required", {}, None))
        decision = decide(transport)
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.billing_state, budget.UNKNOWN)

    def test_a_refused_connection_definitely_was_not_billed(self):
        transport = FakeTransport(raises=urllib.error.URLError(ConnectionRefusedError()))
        decision = decide(transport)
        self.assertEqual(decision.billing_state, budget.NOT_SENT)

    def test_a_timeout_may_have_been_billed(self):
        decision = decide(FakeTransport(raises=socket.timeout("timed out")))
        self.assertEqual(decision.billing_state, budget.UNKNOWN)

    def test_a_refused_call_is_not_sent_at_all(self):
        transport = FakeTransport(200, answer_body())
        decision = decide(transport, allowed=False)
        self.assertFalse(transport.called)
        self.assertEqual(decision.billing_state, budget.NOT_SENT)
        self.assertEqual(decision.source, "fallback")


# ---------------------------------------------------------------- admission

class TestReservationAdmission(unittest.TestCase):
    def test_a_reservation_fits_inside_the_remaining_ceiling(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        self.assertIsNotNone(reservation)
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)

    def test_hard_stop_refuses(self):
        doc = fresh_doc()
        doc["budget"]["hard_stop"] = True
        self.assertIsNone(budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev"))

    def test_spend_plus_exposure_plus_allowance_must_fit(self):
        doc = fresh_doc(total=1.0, spent=0.999)
        self.assertIsNone(budget.reserve(doc, "openrouter", 0.002, purpose="jev"))
        self.assertEqual(budget.reservations(doc), {})

    def test_outstanding_exposure_alone_can_close_the_gate(self):
        """Two calls whose cost never reported, then a third refused. Each is
        settled as UNKNOWN - the realistic way exposure is retained - because
        an unsettled reservation is a different condition entirely."""
        doc = fresh_doc(total=round(ALLOWANCE * 2, 6))
        for purpose in ("a", "b"):
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose=purpose)
            self.assertIsNotNone(reservation, purpose)
            budget.settle(doc, reservation, provider="openrouter",
                          billing_state=budget.UNKNOWN)
        self.assertIsNone(budget.reserve(doc, "openrouter", ALLOWANCE, purpose="c"))
        self.assertFalse(budget.metered_call_allowed(doc))
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)

    def test_an_unreadable_amount_closes_the_gate_rather_than_opening_it(self):
        doc = fresh_doc(total=25.0)
        budget.reservations(doc)["tampered"] = {"provider": "openrouter",
                                               "amount_usd": "lots",
                                               "status": budget.RESERVATION_OPEN}
        self.assertGreaterEqual(budget.outstanding_exposure_usd(doc), 25.0)
        self.assertFalse(budget.metered_call_allowed(doc))

    def test_invalid_allowances_are_refused(self):
        for value in (None, 0, -1, True, "0.002", float("nan"), float("inf")):
            with self.subTest(repr(value)):
                doc = fresh_doc()
                self.assertIsNone(budget.reserve(doc, "openrouter", value, purpose="jev"))

    def test_an_uninitialised_ceiling_refuses(self):
        doc = fresh_doc(total=0.0)
        self.assertIsNone(budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev"))

    def test_exposure_never_moves_percent_thresholds_or_hard_stop(self):
        doc = fresh_doc(total=1.0)
        budget.reserve(doc, "openrouter", 0.9, purpose="jev")
        self.assertEqual(budget.percent(doc), 0.0)
        self.assertEqual(doc["budget"]["thresholds_crossed"], [])
        self.assertFalse(doc["budget"]["hard_stop"])
        self.assertEqual(doc["budget"]["estimated_usd"], 0.0)


class TestReservationSettlement(unittest.TestCase):
    def test_a_known_cost_settles_as_actual_and_is_not_double_counted(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        budget.settle(doc, reservation, provider="openrouter",
                      billing_state=budget.BILLED, actual_cost_usd=0.000019992)
        self.assertEqual(doc["budget"]["spent_usd"], 0.00002)
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)
        self.assertEqual(budget.reservations(doc), {})

    def test_a_billed_but_unusable_answer_still_settles_the_money(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        budget.settle(doc, reservation, provider="openrouter",
                      billing_state=budget.BILLED, actual_cost_usd=0.5)
        self.assertEqual(doc["budget"]["spent_usd"], 0.5)
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)

    def test_a_definitely_unsent_request_releases_its_reservation(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        budget.settle(doc, reservation, provider="openrouter",
                      billing_state=budget.NOT_SENT)
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)

    def test_an_unknown_outcome_keeps_its_exposure(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        budget.settle(doc, reservation, provider="openrouter",
                      billing_state=budget.UNKNOWN)
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        self.assertEqual(budget.reservations(doc)[reservation]["status"],
                         budget.RESERVATION_UNRESOLVED)

    def test_a_billed_state_carrying_an_unusable_cost_keeps_its_exposure(self):
        doc = fresh_doc()
        reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        budget.settle(doc, reservation, provider="openrouter",
                      billing_state=budget.BILLED, actual_cost_usd="free")
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)

    def test_settling_actual_spend_still_crosses_thresholds(self):
        doc = fresh_doc(total=1.0)
        reservation = budget.reserve(doc, "openrouter", 0.5, purpose="jev")
        crossed = budget.settle(doc, reservation, provider="openrouter",
                                billing_state=budget.BILLED, actual_cost_usd=1.0)
        self.assertIn("HARD_STOP", crossed)
        self.assertTrue(doc["budget"]["hard_stop"])


class TestReservationSurvivesACrash(unittest.TestCase):
    """The reservation is committed before the request leaves, so a process
    that dies mid-flight leaves its exposure on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        patch = mock.patch.object(config, "STATE_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = state.Store(path=self.path, tz=TZ)
        self.store.initialise("run-002", "v2.0")
        with self.store.transaction() as doc:
            budget.ensure(doc, 25.0)

    def test_exposure_outlives_the_process_that_reserved_it(self):
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        self.assertIsNotNone(reservation)

        # A new Store, reading the file back exactly as a restarted process
        # would. Nothing settled the reservation, and nothing may release it.
        reopened = state.Store(path=self.path, tz=TZ).read()
        self.assertEqual(budget.outstanding_exposure_usd(reopened), ALLOWANCE)
        self.assertEqual(
            reopened["budget"]["reservations"][reservation]["status"],
            budget.RESERVATION_OPEN)
        self.assertEqual(reopened["budget"]["spent_usd"], 0.0)

    def test_exposure_exhausting_the_ceiling_refuses_the_next_reservation(self):
        """Ceiling arithmetic, which is NOT what makes a crash safe - this
        only bites once exposure fills the whole budget. The crash guard is
        TestCrashDoesNotBuyTheSameAnswerTwice."""
        with self.store.transaction() as doc:
            doc["budget"]["total_usd"] = ALLOWANCE
            self.assertIsNotNone(
                budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev"))

        with state.Store(path=self.path, tz=TZ).transaction() as doc:
            again = budget.reserve(doc, "openrouter", ALLOWANCE, purpose="jev")
        self.assertIsNone(again)


CONCURRENT_RESERVER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from control import budget, state
store = state.Store(path=Path(sys.argv[2]), tz="Pacific/Auckland")
with store.transaction() as doc:
    reservation = budget.reserve(doc, "openrouter", float(sys.argv[3]),
                                 purpose="preflight:concurrent")
print("RESERVED" if reservation else "REFUSED", flush=True)
time.sleep(30)
"""


class TestConsultationIdentity(unittest.TestCase):
    """What makes two consultations the same question (§25.4).

    Too coarse and a crash silences Jev about that task for the rest of the
    run; too fine and a restart re-buys the answer it may already have paid
    for. These pin both edges.
    """

    def task(self, **over):
        record = {"id": "TASK-001", "state": "ACTIVE", "attempts": 1,
                  "worker": "task-001-builder", "progress_marker": 4096,
                  "last_progress_at": "2026-10-01T09:00:00+13:00",
                  "updated_at": "2026-10-01T09:00:00+13:00", "repair_cycles": 0}
        record.update(over)
        return record

    def identity(self, **over):
        return supervisor.jev_consultation_identity(self.task(**over))

    def test_the_same_unresolved_consultation_keeps_its_identity(self):
        self.assertEqual(self.identity(), self.identity())

    def test_time_passing_does_not_make_it_a_new_question(self):
        """minutes_since_progress is recomputed every tick from the clock."""
        for moved in ("last_progress_at", "updated_at"):
            with self.subTest(moved):
                self.assertEqual(
                    self.identity(),
                    self.identity(**{moved: "2026-10-01T23:59:59+13:00"}))

    def test_an_unrelated_queue_depth_is_not_part_of_it(self):
        self.assertNotIn("review_queue_depth",
                         supervisor.jev_consultation_identity(self.task()))

    def test_a_new_builder_attempt_is_a_new_question(self):
        """The builder's worker id is `<task>-builder` every time, so only
        `attempts` distinguishes a re-dispatch."""
        self.assertNotEqual(self.identity(), self.identity(attempts=2))

    def test_a_new_fixer_worker_is_a_new_question(self):
        """A fixer dispatch reassigns `worker` and leaves `attempts` alone."""
        self.assertNotEqual(self.identity(),
                            self.identity(worker="task-001-fixer-1"))

    def test_real_progress_is_a_new_question(self):
        self.assertNotEqual(self.identity(), self.identity(progress_marker=8192))

    def test_a_durable_state_transition_is_a_new_question(self):
        self.assertNotEqual(self.identity(), self.identity(state="REVIEW"))

    def test_a_marker_of_zero_is_not_read_as_absent(self):
        """`progress_marker` is 0 at dispatch, which is falsy and is not the
        same fact as never having had one."""
        self.assertNotEqual(self.identity(progress_marker=0),
                            self.identity(progress_marker=None))

    def test_different_tasks_are_different_questions(self):
        self.assertNotEqual(self.identity(), self.identity(id="TASK-002"))


class TestCrashDoesNotBuyTheSameAnswerTwice(unittest.TestCase):
    """The requirement is two claims, and only one of them is arithmetic.

    Keeping the exposure is arithmetic: an OPEN reservation keeps counting.
    REFUSING THE NEXT CALL IS NOT. Against a $25 ceiling a $0.002688 orphan
    leaves headroom for roughly nine thousand more calls, so exposure alone
    does not stop a restarted Supervisor re-asking the question it may have
    already paid for - and `_last_jev` starts at 0.0, so the 900-second
    interval gate does not suppress the first consultation after a restart
    either.

    What stops it: a reservation nobody can settle is ABANDONED, and THAT
    LOGICAL REQUEST is never sent again. Scoped to the request, not to the
    budget - a different question, about a different task, was never a
    duplicate of anything and still goes ahead.
    """

    # Opaque at this layer: `reserve` compares purposes, it does not parse
    # them. The real shape is pinned in TestConsultationIdentity.
    LOST = "jev:worker_health:TASK-001:a1:wtask-001-builder:p0:ACTIVE"
    OTHER = "jev:worker_health:TASK-002:a1:wtask-002-builder:p0:ACTIVE"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        patch = mock.patch.object(config, "STATE_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = state.Store(path=self.path, tz=TZ)
        self.store.initialise("run-002", "v2.0")
        with self.store.transaction() as doc:
            budget.ensure(doc, 25.0)          # the REAL governed ceiling

    def _orphan(self, purpose=None, **owner):
        """A reservation left OPEN by a call that never settled it."""
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE,
                                         purpose=purpose or self.LOST)
            doc["budget"]["reservations"][reservation].update(owner)
        return reservation

    def _reserve(self, purpose):
        with self.store.transaction() as doc:
            return budget.reserve(doc, "openrouter", ALLOWANCE, purpose=purpose)

    def _dead_pid(self):
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        return done.pid

    def test_the_ceiling_alone_leaves_room_for_thousands_more_calls(self):
        """The fact that makes the rest of this class necessary."""
        self._orphan()
        doc = self.store.read()
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertLess(ALLOWANCE * 100, doc["budget"]["total_usd"])

    def test_an_unsettled_reservation_from_a_dead_process_blocks_a_repeat(self):
        self._orphan(owner_pid=self._dead_pid(), owner_start_ticks=4242)
        self.assertIsNone(self._reserve(self.LOST))

    def test_the_repeat_is_refused_with_the_whole_ceiling_still_free(self):
        """Suppression must not be budget exhaustion wearing a disguise."""
        self._orphan(owner_pid=self._dead_pid(), owner_start_ticks=4242)
        doc = self.store.read()
        self.assertTrue(budget.metered_call_allowed(doc))
        headroom = doc["budget"]["total_usd"] - budget.outstanding_exposure_usd(doc)
        self.assertGreater(headroom / ALLOWANCE, 1000)
        self.assertIsNone(self._reserve(self.LOST))

    def test_a_different_request_is_not_a_duplicate_and_still_goes_ahead(self):
        self._orphan(owner_pid=self._dead_pid(), owner_start_ticks=4242)
        self.assertIsNotNone(self._reserve(self.OTHER))
        self.assertIsNotNone(self._reserve("preflight:jev_minimal_decision"))

    def test_an_unsettled_reservation_from_this_process_blocks_too(self):
        """A crash is not the only way to lose one: an exception between the
        send and the settle loses it inside a process that is still alive."""
        self._orphan()
        self.assertIsNone(self._reserve(self.LOST))

    def test_an_unverifiable_owner_fails_closed(self):
        """Alive, but not provably the process that reserved it."""
        self._orphan(owner_pid=os.getpid(), owner_start_ticks=None)
        self.assertIsNone(self._reserve(self.LOST))

    def test_a_reservation_records_the_process_that_made_it(self):
        """Owner identity is what distinguishes an orphan from a live
        concurrent caller. Without it every reservation looks abandoned."""
        reservation = self._orphan()
        entry = self.store.read()["budget"]["reservations"][reservation]
        self.assertEqual(entry["owner_pid"], os.getpid())
        self.assertEqual(entry["owner_start_ticks"], proc.start_ticks(os.getpid()))

    def test_a_genuinely_concurrent_live_caller_is_not_treated_as_abandoned(self):
        """Preflight running beside the Supervisor must not durably block the
        run. A real second process makes the reservation through the real
        `reserve`, so the identity under test is the one production records.
        Its exposure still counts; it is simply not an orphan."""
        other = subprocess.Popen(
            [sys.executable, "-c", CONCURRENT_RESERVER,
             str(Path(__file__).resolve().parent.parent), str(self.path),
             str(ALLOWANCE)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(other.wait)
        self.addCleanup(other.terminate)
        self.addCleanup(other.stdout.close)
        self.assertTrue(other.stdout.readline().startswith("RESERVED"))

        self.assertIsNotNone(self._reserve(self.LOST),
                             "a live concurrent caller was read as an orphan")
        self.assertEqual(budget.outstanding_exposure_usd(self.store.read()),
                         round(ALLOWANCE * 2, 6))

    def test_the_abandoned_reservation_keeps_its_exposure_and_spends_nothing(self):
        self._orphan(owner_pid=self._dead_pid(), owner_start_ticks=4242)
        self._reserve(self.LOST)
        doc = self.store.read()
        statuses = [r["status"] for r in doc["budget"]["reservations"].values()]
        self.assertEqual(statuses, [budget.RESERVATION_ABANDONED])
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)

    def test_a_settled_call_leaves_nothing_that_could_block_the_next_one(self):
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE,
                                         purpose=self.LOST)
            budget.settle(doc, reservation, provider="openrouter",
                          billing_state=budget.BILLED, actual_cost_usd=0.000019992)
        self.assertIsNotNone(self._reserve(self.LOST))

    def test_an_unresolved_outcome_does_not_block_the_next_call(self):
        """A timeout was SETTLED - the call completed its lifecycle and the
        outcome is recorded. It keeps its exposure; it is not an orphan."""
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE,
                                         purpose=self.LOST)
            budget.settle(doc, reservation, provider="openrouter",
                          billing_state=budget.UNKNOWN)
        self.assertIsNotNone(self._reserve(self.LOST))


# ------------------------------------------------------------- call sites

class CallSiteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        patch = mock.patch.object(config, "STATE_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = state.Store(path=self.path, tz=TZ)
        self.store.initialise("run-002", "v2.0")
        with self.store.transaction() as doc:
            budget.ensure(doc, 25.0)
            doc["tasks"]["TASK-001"] = {
                "id": "TASK-001", "state": "ACTIVE", "attempts": 1,
                "worker": "task-001-builder", "progress_marker": 4096,
                "last_progress_at": None,
            }

    def cfg(self, pricing=None):
        return SimpleNamespace(
            timezone=TZ, experiment_id="run-002", protocol_version="v2.0",
            roles={"jev": SimpleNamespace(model=MODEL)},
            jev_pricing={"reservation_usd_per_call": ALLOWANCE}
            if pricing is None else pricing)


class TestSupervisorPath(CallSiteCase):
    def _supervisor(self):
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.tz = TZ
        sup.cfg = self.cfg()
        sup.store = self.store
        sup.jev = jev.DecisionService(MODEL)
        sup._last_jev = -1e9
        self.events = []
        sup.log = lambda event, **fields: self.events.append((event, fields))
        return sup

    def _consult(self, transport):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", transport):
            self._supervisor().consult_jev()
        return self.store.read()

    def test_a_successful_consultation_settles_the_reservation(self):
        transport = FakeTransport(200, answer_body("SLOW"))
        doc = self._consult(transport)
        self.assertTrue(transport.called)
        self.assertEqual(doc["budget"]["spent_usd"], 0.00002)
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)
        event, fields = self.events[-1]
        self.assertEqual(event, "JEV_DECISION")
        self.assertEqual(fields["model"], MODEL)
        self.assertEqual(fields["metadata_redacted"]["returned_model"], RETURNED_MODEL)
        self.assertEqual(fields["outcome"], "SLOW")

    def test_an_unreported_cost_leaves_exposure_behind(self):
        doc = self._consult(FakeTransport(200, answer_body(cost=None)))
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)

    def test_budget_denial_prevents_the_call(self):
        with self.store.transaction() as doc:
            doc["budget"]["hard_stop"] = True
        transport = FakeTransport(200, answer_body())
        doc = self._consult(transport)
        self.assertFalse(transport.called)
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        _, fields = self.events[-1]
        self.assertEqual(fields["metadata_redacted"]["source"], "fallback")
        self.assertIsNone(fields["metadata_redacted"]["reservation_usd"])

    def _crash_mid_flight(self):
        """The request goes out; the process dies before settling."""
        crashing = FakeTransport(raises=socket.timeout("lost"))
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", crashing):
            dying = self._supervisor()
            dying.log = mock.Mock(side_effect=RuntimeError("process dies here"))
            with self.assertRaises(RuntimeError):
                dying.consult_jev()
        self.assertTrue(crashing.called)

    def test_a_restart_after_a_crash_mid_flight_sends_nothing(self):
        """End to end, at the real $25 ceiling: the request goes out, the
        process dies before settling, the Watchdog restarts the Supervisor -
        and the restarted Supervisor must not buy the same answer again."""
        self._crash_mid_flight()

        restarted = FakeTransport(200, answer_body())
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", restarted):
            self._supervisor().consult_jev()

        self.assertFalse(restarted.called, "the same paid question was re-sent")
        doc = self.store.read()
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertTrue(budget.metered_call_allowed(doc),
                        "suppression must not be budget exhaustion in disguise")
        _, fields = self.events[-1]
        self.assertEqual(fields["metadata_redacted"]["source"], "fallback")
        self.assertTrue(fields["metadata_redacted"]["duplicate_suppressed"],
                        "the ledger must say why nothing was asked")

    def test_the_same_task_may_be_consulted_again_once_the_question_changes(self):
        """The four steps, end to end on one task.

        1. the consultation is sent and the process dies before settling;
        2. a restart with the SAME durable evidence sends nothing;
        3. the task records a genuinely new attempt / real progress;
        4. that consultation is permitted, and the lost one still holds its
           exposure and is still unrepeatable.
        """
        self._crash_mid_flight()
        lost_identity = supervisor.jev_consultation_identity(
            self.store.read()["tasks"]["TASK-001"])

        unchanged = FakeTransport(200, answer_body())
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", unchanged):
            self._supervisor().consult_jev()
        self.assertFalse(unchanged.called, "the same question was re-sent")

        # Wall-clock drift alone must not unlock it either: the Supervisor
        # recomputes minutes_since_progress every tick.
        with self.store.transaction() as doc:
            doc["tasks"]["TASK-001"]["last_progress_at"] = "2026-10-01T23:59:00+13:00"
        still_blocked = FakeTransport(200, answer_body())
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", still_blocked):
            self._supervisor().consult_jev()
        self.assertFalse(still_blocked.called, "time passing unlocked a repeat")

        # A genuinely new worker attempt, written durably as production does.
        with self.store.transaction() as doc:
            task = doc["tasks"]["TASK-001"]
            task["attempts"] += 1
            task["progress_marker"] = 0

        fresh = FakeTransport(200, answer_body("SLOW"))
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", fresh):
            self._supervisor().consult_jev()

        self.assertTrue(fresh.called, "a genuinely new consultation was suppressed")
        doc = self.store.read()
        self.assertEqual(doc["budget"]["spent_usd"], 0.00002)
        # The abandoned reservation is preserved, with its exposure.
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertTrue(budget.duplicate_blocked(doc, lost_identity))
        self.assertEqual(
            [r["status"] for r in doc["budget"]["reservations"].values()],
            [budget.RESERVATION_ABANDONED])

    def test_real_progress_alone_permits_the_next_consultation(self):
        """Step 3's other half: no new attempt, just the worker doing work."""
        self._crash_mid_flight()
        with self.store.transaction() as doc:
            doc["tasks"]["TASK-001"]["progress_marker"] = 9001

        fresh = FakeTransport(200, answer_body())
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", fresh):
            self._supervisor().consult_jev()
        self.assertTrue(fresh.called)
        self.assertEqual(budget.outstanding_exposure_usd(self.store.read()),
                         ALLOWANCE)

    def test_a_consultation_about_a_different_task_still_goes_ahead(self):
        """The guard suppresses a repeat, not Jev. A question about another
        task was never a duplicate of the lost one."""
        self._crash_mid_flight()
        lost_identity = supervisor.jev_consultation_identity(
            self.store.read()["tasks"]["TASK-001"])

        with self.store.transaction() as doc:
            doc["tasks"]["TASK-001"]["state"] = "COMPLETE"
            doc["tasks"]["TASK-002"] = {"id": "TASK-002", "state": "ACTIVE",
                                        "attempts": 1, "last_progress_at": None}

        fresh = FakeTransport(200, answer_body("SLOW"))
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", fresh):
            self._supervisor().consult_jev()

        self.assertTrue(fresh.called, "a genuinely new consultation was suppressed")
        doc = self.store.read()
        self.assertEqual(doc["budget"]["spent_usd"], 0.00002)
        # The lost one still holds its exposure and is still unrepeatable.
        self.assertEqual(budget.outstanding_exposure_usd(doc), ALLOWANCE)
        self.assertTrue(budget.duplicate_blocked(doc, lost_identity))
        _, fields = self.events[-1]
        self.assertEqual(fields["task_id"], "TASK-002")
        self.assertFalse(fields["metadata_redacted"]["duplicate_suppressed"])

    def test_an_unknown_returned_model_is_recorded_as_unknown(self):
        self._consult(FakeTransport(200, answer_body(model=None)))
        _, fields = self.events[-1]
        self.assertEqual(fields["metadata_redacted"]["returned_model"], "unknown")
        self.assertEqual(fields["metadata_redacted"]["requested_model"], MODEL)


class TestPreflightPath(CallSiteCase):
    def _preflight(self, pricing=None):
        pf = preflight.Preflight.__new__(preflight.Preflight)
        pf.cfg = self.cfg(pricing)
        pf.tz = TZ
        pf.ledger = mock.Mock()
        pf.notifier = mock.Mock()
        pf.telemetry = mock.Mock()
        pf.skip = set()
        pf.results = []
        return pf

    def _gate(self, transport, pricing=None):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", transport):
            return self._preflight(pricing).gate_jev()

    def test_the_gate_passes_and_records_both_identifiers(self):
        transport = FakeTransport(200, answer_body("HEALTHY"))
        gate = self._gate(transport)
        self.assertTrue(gate.ok)
        self.assertEqual(gate.evidence["requested_model"], MODEL)
        self.assertEqual(gate.evidence["returned_model"], RETURNED_MODEL)
        self.assertEqual(gate.evidence["billing_state"], budget.BILLED)
        doc = self.store.read()
        self.assertEqual(budget.outstanding_exposure_usd(doc), 0.0)
        self.assertEqual(doc["budget"]["spent_usd"], 0.00002)

    def test_budget_denial_prevents_the_preflight_call_too(self):
        with self.store.transaction() as doc:
            doc["budget"]["hard_stop"] = True
        transport = FakeTransport(200, answer_body())
        gate = self._gate(transport)
        self.assertFalse(transport.called)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.evidence["reason_code"], "BUDGET_REFUSED")
        self.assertTrue(gate.evidence["not_evaluated"])
        self.assertIn("NOT EVALUATED", gate.detail)

    def test_exhausted_exposure_denies_the_preflight_call(self):
        with self.store.transaction() as doc:
            doc["budget"]["total_usd"] = ALLOWANCE
            budget.reserve(doc, "openrouter", ALLOWANCE, purpose="earlier")
        transport = FakeTransport(200, answer_body())
        gate = self._gate(transport)
        self.assertFalse(transport.called)
        self.assertEqual(gate.evidence["reason_code"], "BUDGET_REFUSED")

    def test_re_running_the_probe_after_a_lost_one_sends_nothing(self):
        """The gate always sends the SAME probe, so a second run after one was
        lost in flight is the same logical request. End to end: the first run
        dies between sending and settling, the second must not re-send it."""
        lost = FakeTransport(200, answer_body())
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(http.urllib.request, "urlopen", lost), \
                mock.patch.object(budget, "settle",
                                  side_effect=RuntimeError("process dies here")):
            with self.assertRaises(RuntimeError):
                self._preflight().gate_jev()
        self.assertTrue(lost.called)

        again = FakeTransport(200, answer_body())
        gate = self._gate(again)
        self.assertFalse(again.called, "the same paid probe was re-sent")
        self.assertEqual(gate.evidence["reason_code"], "DUPLICATE_REQUEST_UNRESOLVED")
        self.assertNotIn("budget refused", gate.detail)
        self.assertTrue(gate.evidence["not_evaluated"])
        doc = self.store.read()
        self.assertEqual(doc["budget"]["spent_usd"], 0.0)
        self.assertTrue(budget.metered_call_allowed(doc))

    def test_an_abandoned_probe_does_not_block_a_supervisor_consultation(self):
        """Different logical request, so not a duplicate of the lost probe."""
        consultation = supervisor.jev_consultation_identity(
            self.store.read()["tasks"]["TASK-001"])
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", ALLOWANCE,
                                         purpose=preflight.JEV_GATE_PURPOSE)
            doc["budget"]["reservations"][reservation]["owner_pid"] = 0
        with self.store.transaction() as doc:
            self.assertIsNone(budget.reserve(doc, "openrouter", ALLOWANCE,
                                             purpose=preflight.JEV_GATE_PURPOSE))
            self.assertIsNotNone(budget.reserve(doc, "openrouter", ALLOWANCE,
                                                purpose=consultation))

    def test_no_governed_cost_bound_blocks_the_paid_path(self):
        transport = FakeTransport(200, answer_body())
        gate = self._gate(transport, pricing={})
        self.assertFalse(transport.called)
        self.assertEqual(gate.evidence["reason_code"], "NO_COST_BOUND")

    def test_no_state_document_blocks_the_paid_path(self):
        self.path.unlink()
        transport = FakeTransport(200, answer_body())
        gate = self._gate(transport)
        self.assertFalse(transport.called)
        self.assertEqual(gate.evidence["reason_code"], "NO_STATE_DOCUMENT")

    def test_a_failure_is_reported_as_a_failure_not_as_not_evaluated(self):
        """A refusal and a wrong answer must stay distinguishable."""
        gate = self._gate(FakeTransport(200, answer_body("NONSENSE")))
        self.assertFalse(gate.ok)
        self.assertNotIn("not_evaluated", gate.evidence)
        self.assertIn("Jev unavailable", gate.detail)


class TestGovernedPricingIsConfigured(unittest.TestCase):
    def test_the_configured_allowance_matches_the_documented_basis(self):
        cfg = config.load()
        pricing = cfg.jev_pricing
        self.assertEqual(
            round(pricing["max_billable_input_tokens"]
                  * pricing["input_usd_per_million_tokens"] / 1_000_000, 9),
            round(pricing["reservation_usd_per_call"], 9))
        self.assertTrue(pricing["basis"])

    def test_the_configured_model_is_the_pinned_jev_build(self):
        self.assertEqual(config.load().roles["jev"].model, MODEL)

    def test_an_absent_or_invalid_bound_yields_no_allowance(self):
        for pricing in ({}, {"reservation_usd_per_call": 0},
                        {"reservation_usd_per_call": "cheap"},
                        {"reservation_usd_per_call": True},
                        {"reservation_usd_per_call": float("inf")}):
            with self.subTest(repr(pricing)):
                cfg = SimpleNamespace(jev_pricing=pricing)
                self.assertIsNone(config.jev_reservation_usd(cfg))


if __name__ == "__main__":
    unittest.main()
