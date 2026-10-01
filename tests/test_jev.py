"""C1: the five Run 002 Jev decision kinds (control/jev.py), each bounded,
each failing closed on an invalid/unrecognised value. No Run 001 kinds."""

import json
import os
import unittest
from unittest import mock

from control import jev


def _safe_json(body):
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return None


def _fake_response(status=200, body=""):
    return mock.Mock(ok=(status == 200), status=status, body=body,
                     error=None, json=lambda: _safe_json(body))


class TestExactlyFiveRun002Kinds(unittest.TestCase):
    def test_exactly_five_top_level_kinds(self):
        self.assertEqual(set(jev.DECISIONS.keys()), {
            "finding_severity", "worker_health", "queue_priority",
            "model_routing", "incident_classification",
        })

    def test_no_run_001_kinds_present(self):
        for forbidden in ("task_health", "event_significance", "next_attention"):
            self.assertNotIn(forbidden, jev.DECISIONS)

    def test_every_fallback_is_a_member_of_its_own_options(self):
        for kind, spec in jev.DECISIONS.items():
            self.assertIn(spec["fallback"], spec["options"], kind)

    def test_incident_classification_enum_is_exactly_the_frozen_set(self):
        self.assertEqual(
            jev.DECISIONS["incident_classification"]["options"],
            ["NONE", "WORKER", "PROVIDER", "CI", "EVIDENCE", "SECURITY",
             "PRIVACY", "BUDGET", "STATE_INVARIANT", "APPARATUS", "CREDENTIAL"],
        )


class TestFallbackWhenNotAllowed(unittest.TestCase):
    """decide(allowed=False) must never call out; always the fallback."""

    def _service(self):
        return jev.DecisionService(model="test-model", enabled=True)

    def test_each_kind_falls_back_when_not_allowed(self):
        service = self._service()
        for kind, spec in jev.DECISIONS.items():
            decision = service.decide(kind, {"evidence": "x"}, allowed=False)
            self.assertEqual(decision.source, "fallback")
            self.assertEqual(decision.choice, spec["fallback"])
            self.assertIn(decision.choice, spec["options"])

    def test_incident_classification_fallback_is_none_but_marked_uncertain(self):
        """NONE from the fallback path means 'Jev unreachable', not
        'evidence confirmed no incident' — the caller must check source."""
        decision = self._service().decide("incident_classification", {}, allowed=False)
        self.assertEqual(decision.choice, "NONE")
        self.assertEqual(decision.source, "fallback")

    def test_unknown_kind_is_not_silently_handled(self):
        with self.assertRaises(KeyError):
            self._service().decide("made_up_kind", {}, allowed=False)


def _answer_body(kind, choice, **extra):
    """A Decisions API response body (C-19), not a chat completion."""
    body = {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {kind: {"type": "choice", "choice": choice, "confidence": 0.8}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }
    body.update(extra)
    return json.dumps(body)


class TestFallbackOnOffSchemaResponse(unittest.TestCase):
    """A real Jev response that names a value outside the permitted options
    must fail closed to the fallback, never be accepted as-is."""

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    @mock.patch("control.jev.http.post_json")
    def test_off_schema_choice_falls_back(self, mock_post):
        mock_post.return_value = _fake_response(
            200, _answer_body("worker_health", "MADE_UP_CLASS"))
        decision = jev.DecisionService(model="test-model").decide(
            "worker_health", {"evidence": "x"})
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.choice, "HEALTHY")
        self.assertIn("off-schema", decision.error)

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    @mock.patch("control.jev.http.post_json")
    def test_valid_answer_is_accepted_for_the_approved_kind(self, mock_post):
        for valid_choice in jev.DECISIONS["worker_health"]["options"]:
            mock_post.return_value = _fake_response(
                200, _answer_body("worker_health", valid_choice))
            decision = jev.DecisionService(model="test-model").decide(
                "worker_health", {"evidence": "x"})
            self.assertEqual(decision.source, "jev")
            self.assertEqual(decision.choice, valid_choice)

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    @mock.patch("control.jev.http.post_json")
    def test_kinds_without_approved_criteria_never_reach_the_wire(self, mock_post):
        """C-19 decision 5: the vocabulary is preserved, the call is not made."""
        for kind, spec in jev.DECISIONS.items():
            if kind == "worker_health":
                continue
            mock_post.reset_mock()
            decision = jev.DecisionService(model="test-model").decide(
                kind, {"evidence": "x"})
            mock_post.assert_not_called()
            self.assertEqual(decision.source, "fallback")
            self.assertEqual(decision.choice, spec["fallback"])
            self.assertEqual(decision.error, jev.UNAPPROVED_KIND)

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    @mock.patch("control.jev.http.post_json")
    def test_unparseable_response_falls_back_instead_of_raising(self, mock_post):
        mock_post.return_value = _fake_response(200, "not json at all")
        decision = jev.DecisionService(model="test-model").decide(
            "worker_health", {"evidence": "x"})
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.choice, "HEALTHY")


if __name__ == "__main__":
    unittest.main()
