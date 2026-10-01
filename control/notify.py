"""Discord escalation channel.

Four severities, per Protocol v2 ("Discord"):
  INFO           checkpoints and completions
  ATTENTION      cooldown, recovery, queue pressure - labelled when no human action is needed
  HUMAN_REQUIRED credentials, repeated repair failure, privacy or security concern,
                 apparatus failure, protocol decision
  CRITICAL       privacy/security/experiment-integrity emergency

The webhook URL is read from the environment at call time and never logged.
"""

from __future__ import annotations

import os

from . import clock, http, redact

INFO = "INFO"
ATTENTION = "ATTENTION"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
CRITICAL = "CRITICAL"

_PREFIX = {
    INFO: "INFO",
    ATTENTION: "ATTENTION",
    HUMAN_REQUIRED: "HUMAN REQUIRED",
    CRITICAL: "CRITICAL",
}

MAX_CONTENT = 1800


class Notifier:
    def __init__(self, experiment_id: str = "run-002", dry_run: bool = False) -> None:
        self.experiment_id = experiment_id
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("DISCORD_WEBHOOK_URL"))

    def send(self, severity: str, title: str, body: str = "",
             no_human_action_needed: bool = False, clock_label: str | None = None,
             timeout: float | None = None) -> dict:
        """Deliver one notification. Returns a result dict; never raises.

        `timeout` bounds this single HTTP call. The C-18 drain passes the
        budget it has left, because a per-send default longer than the whole
        drain budget would make that budget decorative.
        """
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

        payload = {"content": content, "username": "Run 002 Supervisor"}
        if timeout is None:
            response = http.post_json(url, payload)
        else:
            response = http.post_json(url, payload, timeout=timeout)
        # Discord returns 204 No Content on success.
        ok = response.ok or response.status in (200, 204)
        # `ambiguous` travels with the result because the caller cannot
        # reconstruct it: a timeout and a rejection both arrive as ok=False,
        # and only the transport knows which one happened. Never ambiguous
        # once ok - a delivered message is a known outcome.
        return {"ok": ok, "status": response.status, "error": response.error,
                "ambiguous": bool(response.ambiguous) and not ok}


# --------------------------------------------------- C-18 stage 2: the queue
#
# Delivery is an external effect and must not happen inside a state
# transaction. A notification is therefore recorded as a durable INTENT,
# committed atomically with the state change that produced it, and delivered
# afterwards by a bounded drain holding no lock.
#
# The lifecycle is the one watchdog.annunciate_orphans already establishes:
#
#   PENDING -> ATTEMPTING -> DELIVERED
#
#   * the ATTEMPTING fence commits BEFORE any external effect, so at most one
#     send exists per durably authorised attempt;
#   * delivery evidence is an immediate intent-scoped ledger append, which
#     survives any number of lost state commits - it, not the state flag, is
#     what suppresses a resend after a success;
#   * a new attempt exists only after a DURABLY RECORDED failure. An
#     ambiguous outcome - the send began and its result is unknown - leaves
#     the intent ATTEMPTING and therefore suppressed, because delivering
#     twice is worse than stale bookkeeping;
#   * backoff after a known failure keeps one unreachable destination from
#     starving every newer notification of the drain budget.
#
# This is AT MOST ONCE per authorised attempt. It is deliberately NOT
# exactly-once: a Discord webhook accepts no idempotency key, so no
# implementation on this side could honour that claim.

QUEUE_KEY = "notifications"

PENDING = "PENDING"
ATTEMPTING = "ATTEMPTING"
DELIVERED = "DELIVERED"

# Bounds on what becomes DURABLE. Before this queue existed a notification
# body was composed, scrubbed and posted without ever being written to
# state.json; now it is persisted, so it is bounded and scrubbed on the way
# in rather than only on the way out.
MAX_TITLE = 300
MAX_BODY = MAX_CONTENT

BACKOFF_BASE_SECONDS = 30      # one supervisor poll interval
BACKOFF_MAX_SECONDS = 1800     # the provider-cooldown scale already in use


def backoff_seconds(attempt: int) -> int:
    """How long to wait after `attempt` durably-known failures."""
    if attempt < 1:
        return 0
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)


def queue(doc: dict) -> dict:
    """The intent queue, created on demand.

    setdefault, not indexing: a state document written before this stage has
    no such key, and must keep working untouched.
    """
    return doc.setdefault(QUEUE_KEY, {})


def new_intent(*, severity: str, title: str, body: str, no_human_action_needed: bool,
               clock_label: str | None, created_at: str) -> dict:
    """One validated, scrubbed intent record.

    Raises ValueError when a secret survives scrubbing. The send path already
    refuses to transmit such a message; this refuses to PERSIST it, which the
    send path never had to consider because nothing was stored.
    """
    if severity not in _PREFIX:
        raise ValueError(f"unknown severity {severity!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    clean_title = redact.scrub(title)[:MAX_TITLE]
    clean_body = redact.scrub(body or "")[:MAX_BODY]
    if redact.contains_secret(clean_title) or redact.contains_secret(clean_body):
        raise ValueError("BLOCKED_SECRET_IN_NOTIFICATION")
    return {
        "severity": severity,
        "title": clean_title,
        "body": clean_body,
        "no_human_action_needed": bool(no_human_action_needed),
        "clock_label": clock_label,
        "created_at": created_at,
        "status": PENDING,
        "attempt": 0,
        "next_attempt_at": None,
        "last_outcome": None,
        "delivered_at": None,
    }


def is_due(intent: dict, now) -> bool:
    """Whether a PENDING intent's backoff has elapsed."""
    if intent.get("status") != PENDING:
        return False
    when = intent.get("next_attempt_at")
    if not when:
        return True
    try:
        return clock.parse(when) <= now
    except (ValueError, TypeError):
        # An unparseable gate must not suppress a notification forever.
        return True


def due_intents(doc: dict, now) -> list[tuple[str, dict]]:
    """Eligible intents in creation order - dicts preserve insertion order,
    so the oldest notification is always attempted first."""
    return [(key, intent) for key, intent in queue(doc).items()
            if is_due(intent, now)]


def backlog(doc: dict, now) -> dict:
    """Pending count and oldest pending age, for operator reporting.

    Nothing is trimmed in this stage, so the queue can only grow while the
    destination is unreachable. These two numbers are how that shows up
    before it becomes a surprise.
    """
    pending = [intent for intent in queue(doc).values()
               if intent.get("status") in (PENDING, ATTEMPTING)]
    oldest = None
    for intent in pending:
        created = intent.get("created_at")
        if not created:
            continue
        try:
            age = (now - clock.parse(created)).total_seconds()
        except (ValueError, TypeError):
            continue
        oldest = age if oldest is None else max(oldest, age)
    return {"pending": len(pending),
            "oldest_pending_age_seconds": None if oldest is None else round(oldest, 1)}
