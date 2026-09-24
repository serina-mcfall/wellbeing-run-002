"""Control-plane Jev adapter - advisory only.

Jev answers typed, predefined questions through the OpenRouter Decisions
pattern: a constrained JSON-schema response whose value must be one of a fixed
enum, with provider usage and cost returned alongside. The supervisor validates
every answer and acts on its own authority. Jev never merges, kills workers,
changes scope or providers, or contacts the human.

If Jev is unavailable, over budget, or returns anything off-schema, the caller
gets the deterministic fallback and the run continues.
"""

from __future__ import annotations

import os
import time

from . import http, redact

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# The only questions the control plane may ask, with their permitted answers
# and the deterministic answer used when Jev cannot be reached. This is the
# Run 002 vocabulary (protocol/JEV-CONTROL-PLANE-V2.md). It replaces Run
# 001's own control-plane vocabulary (task_health/event_significance/
# next_attention), which product/AI.md documents as historical only; Run 002
# code must not implement those Run 001 labels.
DECISIONS: dict[str, dict] = {
    "finding_severity": {
        "options": ["P0", "P1", "P2", "P3"],
        "fallback": "P1",
        "question": "Classify the severity of this finding from the evidence.",
    },
    "worker_health": {
        "options": ["HEALTHY", "SLOW", "STALLED", "WAITING"],
        "fallback": "HEALTHY",
        "question": "Classify the health of this worker from the evidence.",
    },
    "queue_priority": {
        "options": ["LOWEST", "LOW", "NORMAL", "HIGH", "HIGHEST"],
        "fallback": "NORMAL",
        "question": "Classify the dispatch priority of this already-legal task from the evidence.",
    },
    "model_routing": {
        "options": ["STAY_TIER", "ESCALATE_ONE_TIER", "HUMAN_REQUIRED"],
        "fallback": "STAY_TIER",
        "question": "Recommend a model-tier action from the evidence.",
    },
    "incident_classification": {
        "options": ["NONE", "WORKER", "PROVIDER", "CI", "EVIDENCE", "SECURITY",
                    "PRIVACY", "BUDGET", "STATE_INVARIANT", "APPARATUS",
                    "CREDENTIAL"],
        "fallback": "NONE",
        "question": "Classify what class of incident, if any, this evidence shows.",
    },
}

SYSTEM_PROMPT = (
    "You are an advisory classifier for an autonomous software delivery run. "
    "Answer only with one of the permitted options and a short factual reason. "
    "You have no authority: you never merge, stop workers, change scope, or contact humans. "
    "If the evidence is insufficient, choose the most conservative option and say so."
)


class Decision:
    __slots__ = ("kind", "choice", "confidence", "reason", "source", "actual_cost_usd",
                 "input_tokens", "output_tokens", "model", "duration_ms", "error")

    def __init__(self, kind: str, choice: str, source: str, **kwargs) -> None:
        self.kind = kind
        self.choice = choice
        self.source = source  # "jev" or "fallback"
        self.confidence = kwargs.get("confidence")
        self.reason = kwargs.get("reason", "")
        self.actual_cost_usd = kwargs.get("actual_cost_usd")
        self.input_tokens = kwargs.get("input_tokens")
        self.output_tokens = kwargs.get("output_tokens")
        self.model = kwargs.get("model")
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
        """Ask one typed question. Never raises; always returns a Decision."""
        spec = DECISIONS[kind]
        if not allowed or not self.enabled or not self.configured:
            return Decision(kind, spec["fallback"], "fallback",
                            reason="jev disabled, unconfigured, or budget-blocked")

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["choice", "confidence", "reason"],
            "properties": {
                "choice": {"type": "string", "enum": spec["options"]},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
        }
        payload = {
            "model": self.model,
            "usage": {"include": True},
            "temperature": 0,
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": redact.scrub(
                        f"{spec['question']}\n"
                        f"Permitted options: {', '.join(spec['options'])}\n"
                        f"Evidence:\n{_render(evidence)}"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": kind, "strict": True, "schema": schema},
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
            return Decision(kind, spec["fallback"], "fallback", duration_ms=duration_ms,
                            error=f"http {response.status}: {response.body[:200]}")

        body = response.json() or {}
        try:
            import json as _json

            content = body["choices"][0]["message"]["content"]
            parsed = _json.loads(content)
            choice = parsed["choice"]
        except Exception as exc:  # noqa: BLE001 - any malformed answer falls back
            return Decision(kind, spec["fallback"], "fallback", duration_ms=duration_ms,
                            error=f"unparseable response: {exc}")

        if choice not in spec["options"]:
            return Decision(kind, spec["fallback"], "fallback", duration_ms=duration_ms,
                            error=f"off-schema choice: {choice!r}")

        usage = body.get("usage") or {}
        return Decision(
            kind,
            choice,
            "jev",
            confidence=parsed.get("confidence"),
            reason=redact.scrub(str(parsed.get("reason", "")))[:300],
            actual_cost_usd=usage.get("cost"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            model=body.get("model", self.model),
            duration_ms=duration_ms,
        )


def _render(evidence: dict) -> str:
    return "\n".join(f"- {k}: {redact.scrub(str(v))}" for k, v in evidence.items())
