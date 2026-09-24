"""Tiny JSON-over-HTTP helper built on urllib.

Errors are returned, never raised, so a failing side-channel (telemetry, Jev,
notification) can never take the supervisor down. Response text is scrubbed
before it is handed back.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import redact

USER_AGENT = "run-002-control-plane/1.0"


@dataclass(frozen=True)
class Response:
    ok: bool
    status: int
    body: str
    error: str | None = None

    def json(self) -> dict | list | None:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, TypeError):
            return None


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None,
              timeout: float = 20.0) -> Response:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return _send(request, timeout)


def get(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Response:
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return _send(request, timeout)


def _send(request: urllib.request.Request, timeout: float) -> Response:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return Response(True, response.status, redact.scrub(body))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best effort only
            pass
        return Response(False, exc.code, redact.scrub(body), redact.scrub(str(exc)))
    except Exception as exc:  # noqa: BLE001 - network failures must not propagate
        return Response(False, 0, "", redact.scrub(str(exc)))
