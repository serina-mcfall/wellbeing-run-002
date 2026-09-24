"""Secret redaction.

Every string that leaves the control plane - ledger line, Langfuse span, Discord
message, Observer prompt - passes through `scrub`. Secret values are read from the
environment once, held in memory, and never printed; they are only ever used as
needles to replace with `<redacted:NAME>`.
"""

from __future__ import annotations

import os
import re

# Environment variables whose values must never appear in any output.
SECRET_ENV_NAMES: tuple[str, ...] = (
    "OPENROUTER_API_KEY",
    "DISCORD_WEBHOOK_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "SUPABASE_PUBLISHABLE_KEY",
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
)

# Shapes that are secrets regardless of whether we hold the value.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+"), "<redacted:discord-webhook>"),
    (re.compile(r"\bsk-[A-Za-z0-9._-]{16,}"), "<redacted:api-key>"),
    (re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9._-]{8,}"), "<redacted:supabase-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "<redacted:github-token>"),
    (re.compile(r"\bxai-[A-Za-z0-9._-]{16,}"), "<redacted:xai-key>"),
    (re.compile(r"\bpk-lf-[A-Za-z0-9-]{8,}"), "<redacted:langfuse-public-key>"),
    (re.compile(r"\bsk-lf-[A-Za-z0-9-]{8,}"), "<redacted:langfuse-secret-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<redacted:jwt>"),
)

# Shortest secret we will substring-match. Anything shorter risks mangling
# ordinary text without protecting anything meaningful.
_MIN_NEEDLE = 8


def _needles() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= _MIN_NEEDLE:
            found.append((value, f"<redacted:{name}>"))
    # Longest first so a value that contains another is replaced whole.
    found.sort(key=lambda pair: len(pair[0]), reverse=True)
    return found


def scrub(value):
    """Recursively redact secrets from a string, mapping, or sequence."""
    if isinstance(value, str):
        out = value
        for needle, label in _needles():
            if needle in out:
                out = out.replace(needle, label)
        for pattern, label in _PATTERNS:
            out = pattern.sub(label, out)
        return out
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v) for v in value)
    return value


def contains_secret(value: str) -> bool:
    """True when the text still holds a known secret value or secret-shaped token."""
    if not isinstance(value, str):
        return False
    if any(needle in value for needle, _ in _needles()):
        return True
    return any(pattern.search(value) for pattern, _ in _PATTERNS)


def present(name: str) -> bool:
    """Whether a secret is configured. Never reveals the value."""
    return bool(os.environ.get(name))
