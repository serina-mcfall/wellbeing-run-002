"""Supabase reachability check.

Uses the publishable key only. The secret key stays in the environment and is
never used from the control plane.

The PostgREST root (`/rest/v1/`) serves the OpenAPI schema and deliberately
requires a *secret* key, so a publishable key gets 401 there even when the
project is perfectly healthy. Reachability is therefore probed against a table
path: a `PGRST205` 404 ("could not find the table") proves PostgREST answered
and accepted the key, which is exactly what a project with an intentionally
empty database should return.
"""

from __future__ import annotations

import os

from . import http

# A table name no migration will ever create.
PROBE_TABLE = "run001_healthcheck_absent"


def check(timeout: float = 15.0) -> dict:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get(
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"
    )
    if not url or not key:
        return {"ok": False, "reason": "SUPABASE_URL_OR_PUBLISHABLE_KEY_NOT_SET"}

    headers = {"apikey": key, "Accept": "application/json"}
    auth = http.get(f"{url}/auth/v1/health", headers=headers, timeout=timeout)
    rest = http.get(
        f"{url}/rest/v1/{PROBE_TABLE}?select=id&limit=1", headers=headers, timeout=timeout
    )

    auth_ok = auth.status == 200
    body = rest.json() or {}
    rest_ok = rest.status in (200, 206) or (
        rest.status == 404 and str(body.get("code", "")).startswith("PGRST")
    )

    return {
        "ok": bool(auth_ok and rest_ok),
        "auth_status": auth.status,
        "rest_status": rest.status,
        "rest_code": body.get("code"),
        "note": "404/PGRST205 is the expected healthy answer for an empty database",
        "error": None if (auth_ok and rest_ok) else (rest.error or auth.error),
    }
