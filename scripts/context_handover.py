#!/usr/bin/env python3
"""Report a Claude Code session's context occupancy, and hand over at a threshold.

WHAT THIS IS FOR. A 24-hour run outlives one overseeing session. When the
session driving it approaches its context limit the work must move to a
fresh session WITHOUT clearing this one, interrupting a worker, or
restarting the experiment clock. This reports the occupancy and, past a
threshold, sends ONE handover through the notifier the experiment already
uses.

WHAT IT IS NOT. Not a daemon, not a monitor, not a framework. It runs when
someone runs it. There is no timer, no hook and no background process,
because an unattended watcher that has never fired is a claim, not a
control.

IT READS A TRANSCRIPT AND EMITS ONLY NUMBERS. A Claude Code transcript is
exactly the kind of file the 2026-07-27 credential incident was about, so
this parses JSON and prints integers it computed. No message content, no
tool output, and no transcript field other than `usage` integers and the
model id ever reaches an output stream. Do not add a debug print.

THE WINDOW SIZE IS REQUIRED AND UNDEFAULTED. The transcript records the
model as `claude-opus-5` whether the session is the 200k or the 1M
variant, so the size cannot be read from it. Defaulting would silently
report 29% as 147%, or the reverse. Supply it.

IT FAILS CLOSED. A transcript with no usage records is UNREADABLE, not
0%. A session whose occupancy cannot be established is the one most
likely to need a handover, and reporting it as empty would be the worst
possible lie.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import notify  # noqa: E402

# The three usage fields that make up one request's input. `output_tokens`
# is deliberately absent: it is not occupancy, it is what the turn emitted.
INPUT_FIELDS = ("input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens")

UNREADABLE = -1


def last_input_total(transcript: Path) -> int:
    """Input tokens carried by the most recent assistant message.

    That total IS the occupancy: every request re-sends the whole
    conversation, so the last one's input is what the window currently
    holds. Returns UNREADABLE when no usage record exists at all.
    """
    total = UNREADABLE
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UNREADABLE
    for raw in text.splitlines():
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue          # a half-written final line is not a failure
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        total = sum(int(usage.get(name) or 0) for name in INPUT_FIELDS)
    return total


def percent(total: int, window: int) -> float:
    if total == UNREADABLE or window <= 0:
        return float(UNREADABLE)
    return 100.0 * total / window


def shown(pct: float) -> str:
    """One decimal, ROUNDED DOWN. 49.9999% printed as `50.0` beside the
    line `below threshold 50.0` reads as a contradiction, and the number a
    human uses to decide whether to hand over must never say the window is
    fuller than it is. The DECISION uses the exact value; only the display
    is floored."""
    return f"{math.floor(pct * 10) / 10:.1f}"


def already_sent(marker: Path | None) -> bool:
    """ONCE PER SESSION. The marker is session-scoped by where it is put,
    not by anything this script knows about sessions."""
    return marker is not None and marker.exists()


def handover_body(pct: float, total: int, window: int, text: str) -> str:
    return (f"Context {shown(pct)}% ({total:,} of {window:,} tokens).\n"
            "This session is NOT being cleared and nothing is being stopped.\n"
            f"{text.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--window", type=int, required=True,
                        help="context window size in tokens; see the module "
                             "docstring for why this has no default")
    parser.add_argument("--threshold", type=float, default=50.0)
    parser.add_argument("--handover", type=Path,
                        help="file holding the handover text to send")
    parser.add_argument("--marker", type=Path,
                        help="once-per-session marker; written only on a send")
    parser.add_argument("--send", action="store_true",
                        help="attempt a REAL notification. Without it the "
                             "notifier runs dry. Needs the operator's word.")
    args = parser.parse_args(argv)

    total = last_input_total(args.transcript)
    pct = percent(total, args.window)
    if total == UNREADABLE:
        print("occupancy UNREADABLE — no usage record in this transcript")
        return 2

    print(f"input_tokens {total}")
    print(f"window {args.window}")
    print(f"percent {shown(pct)}")

    if pct < args.threshold:
        print(f"below threshold {args.threshold:.1f} — no handover")
        return 0
    print(f"AT OR ABOVE threshold {args.threshold:.1f}")

    if args.handover is None:
        print("no --handover text supplied — nothing sent")
        return 0
    if already_sent(args.marker):
        print("already sent once this session — suppressed")
        return 0

    notifier = notify.Notifier("run-002", dry_run=not args.send)
    result = notifier.send(
        notify.ATTENTION,
        "Overseeing session context handover",
        handover_body(pct, total, args.window,
                      args.handover.read_text(encoding="utf-8")),
    )
    print(f"send ok={result.get('ok')} "
          f"reason={result.get('reason') or result.get('status')}")
    # The marker records a DELIVERED handover only. A dry run and a refusal
    # both leave it unwritten, so the next invocation tries again rather
    # than suppressing a handover that never reached anybody.
    if result.get("ok") and args.send and args.marker is not None:
        args.marker.write_text("sent\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
