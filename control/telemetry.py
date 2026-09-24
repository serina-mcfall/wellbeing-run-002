"""Langfuse telemetry over OTLP/HTTP.

Preflight established that the real-time OTel path works and that legacy
ingestion must not be relied on for Run 001, so this speaks OTLP directly with
the standard library: no SDK, no background exporter, no extra dependency.

Every attribute value is scrubbed. Prompt and product content is never sent -
only control-plane facts (role, task, provider, model, outcome, token counts).
"""

from __future__ import annotations

import base64
import os
import secrets
import time

from . import http, redact

OTLP_PATH = "/api/public/otel/v1/traces"

STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": redact.scrub(str(value))}}


class Telemetry:
    def __init__(self, service_name: str = "run-001-control-plane",
                 experiment_id: str = "run-001", enabled: bool = True) -> None:
        self.service_name = service_name
        self.experiment_id = experiment_id
        self.enabled = enabled

    @property
    def configured(self) -> bool:
        return all(
            os.environ.get(name)
            for name in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
        )

    def _endpoint(self) -> str:
        base = (os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
        return base + OTLP_PATH

    def _auth_header(self) -> dict[str, str]:
        public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def span(self, name: str, attributes: dict | None = None, *,
             trace_id: str | None = None, parent_span_id: str | None = None,
             start_ns: int | None = None, duration_ms: float | None = None,
             error: bool = False) -> dict:
        """Emit one span. Returns a result dict; never raises."""
        if not self.enabled:
            return {"ok": True, "skipped": "disabled"}
        if not self.configured:
            return {"ok": False, "reason": "LANGFUSE_NOT_CONFIGURED"}

        trace = trace_id or new_trace_id()
        span_id = new_span_id()
        end_ns = time.time_ns()
        begin_ns = start_ns if start_ns is not None else (
            end_ns - int((duration_ms or 0) * 1_000_000)
        )

        merged = {
            "langfuse.session.id": self.experiment_id,
            "experiment.id": self.experiment_id,
        }
        merged.update(attributes or {})

        span: dict = {
            "traceId": trace,
            "spanId": span_id,
            "name": name,
            "kind": 1,
            "startTimeUnixNano": str(begin_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": [_attr(k, v) for k, v in merged.items()],
            "status": {"code": STATUS_ERROR if error else STATUS_OK},
        }
        if parent_span_id:
            span["parentSpanId"] = parent_span_id

        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            _attr("service.name", self.service_name),
                            _attr("deployment.environment", "experiment"),
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "run-001.supervisor"}, "spans": [span]}
                    ],
                }
            ]
        }

        response = http.post_json(
            self._endpoint(), payload, headers=self._auth_header(), timeout=15.0
        )
        return {
            "ok": response.ok,
            "status": response.status,
            "trace_id": trace,
            "span_id": span_id,
            "error": response.error,
            "body": response.body[:300] if not response.ok else "",
        }
