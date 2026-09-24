"""Provider availability, circuit breakers and cooldown policy.

A provider limit pauses that provider only. The 24-hour clock never pauses, and
work that does not depend on the paused provider keeps moving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PROVIDERS = ("claude", "codex", "grok", "openrouter")

AVAILABLE = "AVAILABLE"
COOLDOWN = "COOLDOWN"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
STATES = (AVAILABLE, COOLDOWN, DEGRADED, UNAVAILABLE)

# Consecutive failures before the breaker opens.
DEGRADED_AFTER = 2
UNAVAILABLE_AFTER = 4

# Text that indicates a rate/usage limit rather than an ordinary error.
LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "usage limit",
    "quota",
    "too many requests",
    "overloaded",
    "capacity",
    "insufficient_quota",
    "resets at",
)


@dataclass(frozen=True)
class Policy:
    """What may continue while a given provider is paused."""

    provider: str
    allows_new_builds: bool
    allows_review: bool
    allows_merge: bool
    allows_observation: bool
    note: str


# Cooldown policy is spelled out in BOOTSTRAP.md "Provider cooldowns".
POLICIES: dict[str, Policy] = {
    "claude": Policy(
        "claude",
        allows_new_builds=False,
        allows_review=True,
        allows_merge=True,
        allows_observation=True,
        note="Codex may finish review/merge of Claude-authored PRs; Grok/Jev/CI continue; "
             "new Claude implementation waits.",
    ),
    "codex": Policy(
        "codex",
        allows_new_builds=True,
        allows_review=False,
        allows_merge=False,
        allows_observation=True,
        note="Claude may build; PRs queue for review; no self-review is ever permitted.",
    ),
    "grok": Policy(
        "grok",
        allows_new_builds=True,
        allows_review=True,
        allows_merge=True,
        allows_observation=False,
        note="Observation pauses; telemetry and development continue.",
    ),
    "openrouter": Policy(
        "openrouter",
        allows_new_builds=True,
        allows_review=True,
        allows_merge=True,
        allows_observation=True,
        note="Jev falls back to deterministic defaults; companion reports unavailability.",
    ),
}


def blank(provider: str) -> dict:
    return {
        "provider": provider,
        "state": AVAILABLE,
        "consecutive_failures": 0,
        "cooldown_until": None,
        "last_error_kind": None,
        "last_changed_at": None,
        "cooldown_count": 0,
    }


def ensure(doc: dict) -> None:
    for provider in PROVIDERS:
        doc.setdefault("providers", {}).setdefault(provider, blank(provider))


def is_limit_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in LIMIT_MARKERS)


def refresh(doc: dict, tz: str) -> list[tuple[str, str, str]]:
    """Expire elapsed cooldowns. Returns (provider, old, new) for each change."""
    changes: list[tuple[str, str, str]] = []
    now = datetime.now(ZoneInfo(tz))
    for provider, record in doc.get("providers", {}).items():
        until = record.get("cooldown_until")
        if record["state"] == COOLDOWN and until and datetime.fromisoformat(until) <= now:
            record["state"] = AVAILABLE
            record["cooldown_until"] = None
            record["consecutive_failures"] = 0
            record["last_changed_at"] = now.isoformat(timespec="seconds")
            changes.append((provider, COOLDOWN, AVAILABLE))
    return changes


def record_success(doc: dict, provider: str, tz: str) -> tuple[str, str] | None:
    record = doc["providers"][provider]
    before = record["state"]
    record["consecutive_failures"] = 0
    if before in (DEGRADED,):
        record["state"] = AVAILABLE
        record["last_changed_at"] = datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
        return before, AVAILABLE
    return None


def record_failure(doc: dict, provider: str, error_text: str, cooldown_seconds: int,
                   tz: str) -> tuple[str, str]:
    """Update the breaker for a provider. Returns (old_state, new_state)."""
    record = doc["providers"][provider]
    before = record["state"]
    now = datetime.now(ZoneInfo(tz))
    record["consecutive_failures"] += 1

    if is_limit_error(error_text):
        record["last_error_kind"] = "LIMIT"
        record["state"] = COOLDOWN
        record["cooldown_until"] = (now + timedelta(seconds=cooldown_seconds)).isoformat(
            timespec="seconds"
        )
        record["cooldown_count"] += 1
    else:
        record["last_error_kind"] = "ERROR"
        failures = record["consecutive_failures"]
        if failures >= UNAVAILABLE_AFTER:
            record["state"] = UNAVAILABLE
        elif failures >= DEGRADED_AFTER:
            record["state"] = DEGRADED

    record["last_changed_at"] = now.isoformat(timespec="seconds")
    return before, record["state"]


def usable(doc: dict, provider: str) -> bool:
    return doc["providers"][provider]["state"] in (AVAILABLE, DEGRADED)


def paused(doc: dict) -> list[str]:
    return [p for p, r in doc["providers"].items() if r["state"] in (COOLDOWN, UNAVAILABLE)]


def safe_hold(doc: dict) -> bool:
    """Multiple development-critical providers unavailable -> hold safely."""
    blocked = [p for p in ("claude", "codex")
               if doc["providers"][p]["state"] in (COOLDOWN, UNAVAILABLE)]
    return len(blocked) >= 2


def may(doc: dict, capability: str) -> bool:
    """Whether a capability is permitted given every paused provider's policy.

    capability is one of: new_builds, review, merge, observation.
    """
    attribute = f"allows_{capability}"
    for provider in paused(doc):
        policy = POLICIES.get(provider)
        if policy and not getattr(policy, attribute):
            return False
    return True
