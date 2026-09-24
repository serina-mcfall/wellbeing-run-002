"""Process liveness that a zombie cannot fake.

`os.kill(pid, 0)` succeeds for a zombie: a killed child whose parent has not
reaped it keeps its PID table entry. A watchdog relying on that signal reports a
dead supervisor as healthy. This reads the process state instead, and treats
`Z` (zombie) and `X` (dead) as not running.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        state = _state(pid)
    except (OSError, ValueError):
        return False
    if state is None:
        # No procfs: fall back to the signal probe, which is better than nothing.
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    return state not in ("Z", "X")


def _state(pid: int) -> str | None:
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        if Path("/proc/self").exists():
            return "X"  # procfs exists and the pid does not
        return None
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return None


def reap_children() -> None:
    """Collect finished children so they cannot linger as zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return
