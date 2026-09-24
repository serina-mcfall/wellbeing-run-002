#!/usr/bin/env python3
"""CI guardrail: fail the build if a committed file looks like it holds a secret.

RED guardrail in guardrails/GUARDRAILS.md: secrets in commits. This checks the
tracked tree only - it never reads untracked local environment files.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("discord webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+")),
    ("openai-style key", re.compile(r"\bsk-(?:or-v1-|proj-|lf-)?[A-Za-z0-9._-]{24,}")),
    ("supabase key", re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9._-]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("xai key", re.compile(r"\bxai-[A-Za-z0-9._-]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp3", ".ogg",
                 ".wav", ".woff", ".woff2", ".ttf", ".pdf", ".zip")

# This file necessarily contains the patterns it looks for.
SELF = "scripts/check_no_secrets.py"
ALLOWED = {SELF, "control/redact.py", "tests/test_control_plane.py"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def main() -> int:
    problems: list[str] = []
    for name in tracked_files():
        if name in ALLOWED or name.endswith(SKIP_SUFFIXES):
            continue
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{name}:{line}: possible {label}")

    if problems:
        print("RED GUARDRAIL — possible secret material in tracked files:")
        for problem in problems:
            print(f"  {problem}")
        print("\nThe matched value is deliberately not printed. Remove it, rotate the "
              "credential, and revoke the old one server-side.")
        return 1

    print(f"No secret-shaped material found in {len(tracked_files())} tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
