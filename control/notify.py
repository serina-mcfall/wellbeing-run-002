"""Discord escalation channel.

Three severities, as defined in experiment/TIMELINE.md:
  INFO           checkpoints and completions
  ATTENTION      cooldown, recovery, queue pressure - labelled when no human action is needed
  HUMAN_REQUIRED credentials, repeated repair failure, privacy or security concern,
                 apparatus failure, protocol decision

The webhook URL is read from the environment at call time and never logged.
"""

from __future__ import annotations

import os

from . import http, redact

INFO = "INFO"
ATTENTION = "ATTENTION"
HUMAN_REQUIRED = "HUMAN_REQUIRED"

_PREFIX = {
    INFO: "INFO",
    ATTENTION: "ATTENTION",
    HUMAN_REQUIRED: "HUMAN REQUIRED",
}

MAX_CONTENT = 1800


class Notifier:
    def __init__(self, experiment_id: str = "run-001", dry_run: bool = False) -> None:
        self.experiment_id = experiment_id
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("DISCORD_WEBHOOK_URL"))

    def send(self, severity: str, title: str, body: str = "",
             no_human_action_needed: bool = False, clock_label: str | None = None) -> dict:
        """Deliver one notification. Returns a result dict; never raises."""
        header = f"[{_PREFIX.get(severity, severity)}] [{self.experiment_id}]"
        if clock_label:
            header += f" [{clock_label}]"
        lines = [f"{header} {title}"]
        if no_human_action_needed and severity == ATTENTION:
            lines.append("_No human action needed._")
        if body:
            lines.append(body)
        content = redact.scrub("\n".join(lines))[:MAX_CONTENT]

        if redact.contains_secret(content):
            return {"ok": False, "reason": "BLOCKED_SECRET_IN_NOTIFICATION"}
        if self.dry_run:
            return {"ok": True, "dry_run": True, "content_length": len(content)}

        url = os.environ.get("DISCORD_WEBHOOK_URL")
        if not url:
            return {"ok": False, "reason": "DISCORD_WEBHOOK_URL_NOT_SET"}

        response = http.post_json(url, {"content": content, "username": "Run 001 Supervisor"})
        # Discord returns 204 No Content on success.
        ok = response.ok or response.status in (200, 204)
        return {"ok": ok, "status": response.status, "error": response.error}
