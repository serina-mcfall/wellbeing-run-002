"""Tiny JSON-over-HTTP helper built on urllib.

Errors are returned, never raised, so a failing side-channel (telemetry, Jev,
notification) can never take the supervisor down. Response text is scrubbed
before it is handed back.
"""

from __future__ import annotations

import json
import socket
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
    # Whether the request MAY have reached the server despite this failure.
    #
    # `ok=False` alone cannot tell a caller whether anything happened out
    # there. A 500 means the server answered and rejected the request;
    # a read timeout means the request may have been delivered in full and
    # only the answer was lost. Those are different facts, and a caller that
    # retries on the second one delivers twice.
    #
    # Defaults False, so every existing caller - telemetry, Jev, manifest,
    # reconcile - behaves exactly as before. Only C-18's notification drain
    # reads it.
    ambiguous: bool = False

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


# Failures that prove NOTHING left this machine: the name never resolved, or
# the connection was refused outright. An ALLOW-LIST, deliberately - an
# unrecognised failure is treated as possibly-transmitted, which costs a
# suppressed retry rather than a duplicate delivery. A deny-list would fail
# the other way.
_NOT_TRANSMITTED = (socket.gaierror, ConnectionRefusedError)


def _send(request: urllib.request.Request, timeout: float) -> Response:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return Response(True, response.status, redact.scrub(body))
    except urllib.error.HTTPError as exc:
        # The server answered. Whatever it said, the request arrived and was
        # positively rejected - a known outcome, not an ambiguous one.
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best effort only
            pass
        return Response(False, exc.code, redact.scrub(body), redact.scrub(str(exc)))
    except urllib.error.URLError as exc:
        # Connect-time failures arrive wrapped. Only a name that would not
        # resolve, or a refused connection, prove nothing was sent.
        known = isinstance(exc.reason, _NOT_TRANSMITTED)
        return Response(False, 0, "", redact.scrub(str(exc)), ambiguous=not known)
    except Exception as exc:  # noqa: BLE001 - network failures must not propagate
        # Bare socket timeouts land here, as does anything unforeseen. A
        # timeout is the archetypal ambiguous outcome: the request may have
        # been delivered in full and only the response lost.
        return Response(False, 0, "", redact.scrub(str(exc)), ambiguous=True)
