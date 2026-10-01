"""Control-plane Jev adapter - advisory only.

Jev is TypeSafe's decision model, served through OpenRouter's Decisions API:
`POST /api/alpha/decisions`, one typed question per request, answered with a
member of a fixed enum plus a probability distribution and a confidence. This
is the API `product/AI.md` names, not a chat model prompted to emit JSON
(C-19).

The supervisor validates every answer and acts on its own authority. Jev never
merges, kills workers, changes scope or providers, or contacts the human. If
Jev is unavailable, over budget, or returns anything off-schema, the caller gets
the deterministic fallback and the run continues.

A kind is callable only when its `criteria` have been approved. `worker_health`
is the only approved kind; the other four keep their frozen vocabulary and are
refused before any HTTP call, so "unavailable" is a mechanism rather than a
note.
"""

from __future__ import annotations

import math
import os
import time

from . import budget, http, redact

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

# The only questions the control plane may ask, with their permitted answers
# and the deterministic answer used when Jev cannot be reached. This is the
# Run 002 vocabulary (protocol/JEV-CONTROL-PLANE-V2.md). It replaces Run
# 001's own control-plane vocabulary (task_health/event_significance/
# next_attention), which product/AI.md documents as historical only; Run 002
# code must not implement those Run 001 labels.
#
# `criteria` is what the Decisions API requires: one description per option.
# Descriptions shape classification and the enums are frozen under C-01a, so
# they are a governance artefact. A kind without approved criteria is not
# callable - see `decide`.
DECISIONS: dict[str, dict] = {
    "finding_severity": {
        "options": ["P0", "P1", "P2", "P3"],
        "fallback": "P1",
        "question": "Classify the severity of this finding from the evidence.",
        # Not a DecisionService call at all: severity reaches the control plane
        # as the `jev_severity` field of the PR evidence record, validated by
        # control/severity.py::apply_severity_policy. Criteria unapproved.
    },
    "worker_health": {
        "options": ["HEALTHY", "SLOW", "STALLED", "WAITING"],
        "fallback": "HEALTHY",
        "question": "Classify the health of this worker from the evidence.",
        # Approved 2026-10-01. Every statement below is answerable from the
        # five fields the evidence actually carries: task, state, attempts,
        # minutes_since_progress, review_queue_depth. Deliberately NOT here:
        # any instruction to infer that a process has died. This evidence
        # contains no liveness observation, and asking for one would invite a
        # confident answer drawn from nothing.
        "criteria": {
            "HEALTHY": (
                "Progress is recent, or so little time has passed since the "
                "last progress signal that no fault is evidenced yet."
            ),
            "SLOW": (
                "Time since the last progress signal is longer than this "
                "task's attempts and the review queue depth would explain, "
                "but progress is still evidenced. Slow is explicitly not "
                "stalled: the control plane's own rule is that stale means no "
                "progress, not merely slow."
            ),
            "STALLED": (
                "Time since the last progress signal is long enough that no "
                "meaningful progress can be credited to this worker. Judge "
                "this from the elapsed time alone: the evidence contains no "
                "observation of any process, so do not conclude that a "
                "process has died or ended."
            ),
            "WAITING": (
                "The task is parked on something outside the worker rather "
                "than being worked on - the state field reports this "
                "directly, for example REVIEW meaning the task is awaiting an "
                "independent review."
            ),
        },
    },
    "queue_priority": {
        "options": ["LOWEST", "LOW", "NORMAL", "HIGH", "HIGHEST"],
        "fallback": "NORMAL",
        "question": "Classify the dispatch priority of this already-legal task from the evidence.",
        # Criteria deliberately not drafted. Nothing in this repository
        # distinguishes HIGHEST from HIGH or LOWEST from LOW, and the kind has
        # no call site; inventing five gradations is exactly what C-01a's
        # no-subtype-taxonomy rule exists to prevent.
    },
    "model_routing": {
        "options": ["STAY_TIER", "ESCALATE_ONE_TIER", "HUMAN_REQUIRED"],
        "fallback": "STAY_TIER",
        "question": "Recommend a model-tier action from the evidence.",
        # Criteria unapproved: Protocol v2 states no precedence between
        # ESCALATE_ONE_TIER and HUMAN_REQUIRED on evidence that reads as both.
    },
    "incident_classification": {
        "options": ["NONE", "WORKER", "PROVIDER", "CI", "EVIDENCE", "SECURITY",
                    "PRIVACY", "BUDGET", "STATE_INVARIANT", "APPARATUS",
                    "CREDENTIAL"],
        "fallback": "NONE",
        "question": "Classify what class of incident, if any, this evidence shows.",
        # Criteria unapproved: C-01a froze the enumeration but set no rule for
        # evidence fitting two classes.
    },
}

# Finite refusal reasons. Never exception prose.
UNAPPROVED_KIND = "kind has no approved criteria and is not callable"
NOT_PERMITTED = "jev disabled, unconfigured, or budget-blocked"


class Decision:
    __slots__ = ("kind", "choice", "confidence", "reason", "source", "actual_cost_usd",
                 "input_tokens", "output_tokens", "requested_model", "returned_model",
                 "billing_state", "duration_ms", "error")

    def __init__(self, kind: str, choice: str, source: str, **kwargs) -> None:
        self.kind = kind
        self.choice = choice
        self.source = source  # "jev" or "fallback"
        self.confidence = kwargs.get("confidence")
        self.reason = kwargs.get("reason", "")
        self.actual_cost_usd = kwargs.get("actual_cost_usd")
        self.input_tokens = kwargs.get("input_tokens")
        self.output_tokens = kwargs.get("output_tokens")
        # Two identifiers, never conflated: what we asked for, and what
        # answered. A response that names no model records `returned_model`
        # as None - unknown provenance - and is never back-filled from the
        # request. Neither identifier proves which weights ran.
        self.requested_model = kwargs.get("requested_model")
        self.returned_model = kwargs.get("returned_model")
        self.billing_state = kwargs.get("billing_state", budget.NOT_SENT)
        self.duration_ms = kwargs.get("duration_ms")
        self.error = kwargs.get("error")

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}


class DecisionService:
    def __init__(self, model: str, enabled: bool = True) -> None:
        self.model = model
        self.enabled = enabled

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("OPENROUTER_API_KEY"))

    def decide(self, kind: str, evidence: dict, *, allowed: bool = True,
               timeout: float = 25.0) -> Decision:
        """Ask one typed question. Never raises; always returns a Decision.

        `allowed` is the budget's answer, decided and durably reserved by the
        caller before this is called. False means nothing is sent.
        """
        spec = DECISIONS[kind]
        if not allowed or not self.enabled or not self.configured:
            return Decision(kind, spec["fallback"], "fallback",
                            requested_model=self.model,
                            billing_state=budget.NOT_SENT, reason=NOT_PERMITTED)
        criteria = spec.get("criteria")
        if not criteria:
            # Before any HTTP call, and before any money: an unapproved kind is
            # refused rather than asked with invented descriptions.
            return Decision(kind, spec["fallback"], "fallback",
                            requested_model=self.model,
                            billing_state=budget.NOT_SENT, error=UNAPPROVED_KIND)

        payload = {
            "model": self.model,
            "state": {str(key): redact.scrub(str(value))
                      for key, value in evidence.items()},
            "questions": {
                kind: {
                    "type": "choice",
                    "instructions": spec["question"],
                    "criteria": dict(criteria),
                },
            },
        }

        started = time.monotonic()
        response = http.post_json(
            ENDPOINT,
            payload,
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "X-Title": "Run 002 Supervisor",
            },
            timeout=timeout,
        )
        duration_ms = round((time.monotonic() - started) * 1000, 1)

        if not response.ok:
            return Decision(kind, spec["fallback"], "fallback",
                            duration_ms=duration_ms, requested_model=self.model,
                            billing_state=_billing_on_failure(response),
                            error=f"http {response.status}: {response.body[:200]}")

        body = response.json()
        body = body if isinstance(body, dict) else {}
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        cost = budget.valid_usd(usage.get("cost"))

        # Settled from the response, not from whether the answer was usable:
        # a billed 200 that returns nonsense still cost money.
        common = {
            "duration_ms": duration_ms,
            "requested_model": self.model,
            "returned_model": _model_string(body.get("model")),
            "actual_cost_usd": cost,
            "input_tokens": _token_count(usage.get("input_tokens")),
            "output_tokens": _token_count(usage.get("output_tokens")),
            "billing_state": budget.BILLED if cost is not None else budget.UNKNOWN,
        }

        choice, error = _read_choice(body, kind, spec)
        if error is not None:
            return Decision(kind, spec["fallback"], "fallback", error=error, **common)

        answer = body["answers"][kind]
        return Decision(
            kind,
            choice,
            "jev",
            confidence=_confidence(answer.get("confidence")),
            reason=redact.scrub(str(answer.get("reason", "")))[:300],
            **common,
        )


def _read_choice(body: dict, kind: str, spec: dict) -> tuple[str | None, str | None]:
    """The permitted choice, or a finite reason why there is not one."""
    answers = body.get("answers")
    if not isinstance(answers, dict):
        return None, "malformed response: no answers object"
    answer = answers.get(kind)
    if not isinstance(answer, dict):
        return None, f"malformed response: no answer for {kind}"
    if answer.get("type") != "choice":
        return None, f"malformed response: answer type {answer.get('type')!r}"
    choice = answer.get("choice")
    if not isinstance(choice, str):
        return None, "malformed response: choice is not a string"
    if choice not in spec["options"]:
        return None, f"off-schema choice: {choice!r}"
    return choice, None


def _billing_on_failure(response) -> str:
    """Only a transport that proves nothing left releases the reservation.

    A server that answered - any HTTP status - reached the provider, and this
    control plane cannot tell from the outside whether that was billed.
    """
    definitely_unsent = (response.status == 0 and not getattr(response, "ambiguous", True))
    return budget.NOT_SENT if definitely_unsent else budget.UNKNOWN


def _model_string(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _token_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _confidence(value) -> float | None:
    """Advisory only. A malformed confidence is unknown, never a number.

    No threshold exists and none is invented here: nothing in the control plane
    acts on this value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number
