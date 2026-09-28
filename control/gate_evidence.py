"""C-05.1: durable accessibility evidence, one write-once attempt at a time.

The accessibility apparatus (apparatus/accessibility/run.js) has run real
Chromium and real axe-core since B1, but nothing ever persisted what it
found: `details` lived in a returned object, screenshots were keyed by
SHA alone and overwrote themselves on rerun, and no caller existed at
all. Evidence never captured cannot be reconstructed afterwards, which is
why C-12 names this a pre-T+00 dependency.

This module is that persisting caller, and nothing more. It allocates an
attempt directory, runs the apparatus inside it, and returns a finite
record bound to a task ID and a PR SHA. It does not dispatch workers, does
not touch the state document, does not write the ledger, and does not
decide anything about a merge - those belong to C-05.2 and C-04.

Two rules shape everything here:

1. **An attempt is write-once.** The directory is created with an
   exclusive mkdir, so a rerun at the same SHA gets its own directory and
   the earlier attempt's screenshots and result survive untouched.
2. **A run that did not happen never reads as a run that passed.** A
   non-zero exit, a hard timeout, a missing result file, or a result
   bound to a different SHA all record FAILED with an empty check list
   and null counts. Zero failures would read as a clean scan; unknown is
   not clean.

Evidence is local, under .runtime/evidence. C-05 publishes nothing.

C-05.2 adds the read side: an attempt lifecycle marker, and a scanner
that parses the browser sidecars run.js leaves behind. This module owns
the evidence LAYOUT and its PARSING only - it reads no /proc, resolves no
PID, and decides nothing about liveness or orphanhood. Those are
judgements about the running host, and they belong with the host
observation code (control/proc.py) and its callers.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config, workers

COMPLETED = "COMPLETED"
FAILED = "FAILED"

# Attempt lifecycle (C-05.2): two presence-only files, never one file
# rewritten. They bound the window in which a browser is expected to
# exist, and nothing more - they are one input to a classification, not
# the classification:
#
#   * while the attempt is RUNNING, an OPEN sidecar whose browser is
#     verified alive may be entirely legitimate - the apparatus working;
#   * once the attempt is TERMINAL, that same OPEN sidecar is a leak only
#     if its (pid, start_ticks) is independently verified alive;
#   * verified-dead is a stale sidecar, not a leak, in either window;
#   * unverifiable identity stays unknown in either window, and unknown
#     is never resolved into either answer.
#
# Existence is the entire datum. Both are empty and created O_CREAT|
# O_EXCL, so a zero-length marker is valid by definition, no partial
# write can be misread, and each is write-once.
#
# Absence proves neither state: a kill between the two leaves exactly
# what a genuinely in-flight attempt leaves, so "no TERMINAL marker" is
# never promoted to "finished".
RUNNING = "RUNNING"
TERMINAL = "TERMINAL"

RUNNING_MARKER = "attempt-running.marker"
TERMINAL_MARKER = "attempt-terminal.marker"

# Sidecar states written by apparatus/accessibility/run.js. Any other
# value is unrecognised evidence, which reads as unknown - never as
# CLOSED, and never as "no browser was launched".
OPEN = "OPEN"
CLOSED = "CLOSED"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

RUN_JS = config.REPO_ROOT / "apparatus" / "accessibility" / "run.js"

# There are deliberately NO default timeouts here. config/experiment.json
# carries no accessibility timeout, so any number this module picked would
# be policy invented by the apparatus rather than governed by the
# experiment - and a default is the quietest way for an ungoverned number
# to become the one everything actually runs on. Every caller states both
# bounds explicitly; C-05.3 governs them when dispatch is wired.


@dataclass(frozen=True)
class AttemptContext:
    """Every path the apparatus writes, derived from one attempt directory."""
    attempt_dir: Path
    out_dir: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    sidecar_path: Path
    running_marker_path: Path
    terminal_marker_path: Path
    sha: str
    url: str


def context(attempt_dir: Path, sha: str, url: str) -> AttemptContext:
    attempt_dir = Path(attempt_dir)
    out_dir = attempt_dir / "accessibility"
    return AttemptContext(
        attempt_dir=attempt_dir,
        out_dir=out_dir,
        result_path=out_dir / "result.json",
        stdout_path=out_dir / "stdout.txt",
        stderr_path=out_dir / "stderr.txt",
        sidecar_path=out_dir / "browser.json",
        # Lifecycle markers sit at the attempt root, not inside
        # accessibility/: they describe the whole attempt, not one
        # artifact directory within it.
        running_marker_path=attempt_dir / RUNNING_MARKER,
        terminal_marker_path=attempt_dir / TERMINAL_MARKER,
        sha=sha,
        url=url,
    )


class LifecycleMarkerError(RuntimeError):
    """A marker's durability could not be established AND the marker
    could not be withdrawn again.

    This is the one lifecycle condition that cannot be reported as a
    return value. False means "no transition happened", but a marker left
    on disk says the opposite to every later reader, and the two cannot
    both be published. Raising keeps the contradiction from being
    swallowed by a caller that would otherwise carry on."""


def _fsync_path(path: Path, flags: int) -> bool:
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return False
    ok = True
    try:
        os.fsync(fd)
    except OSError:
        ok = False
    finally:
        try:
            os.close(fd)
        except OSError:
            ok = False
    return ok


def _discard_marker(path: Path) -> bool:
    """Withdraw a marker whose durability was never established.

    True means no reader can take it for a lifecycle fact: it was
    unlinked and the removal was committed, or it was already absent.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return _fsync_path(path.parent, os.O_RDONLY)


def _create_marker(path: Path) -> bool:
    """Create one presence-only lifecycle marker, exclusively and durably.

    O_EXCL establishes that the marker is ours and was not already
    claimed; it does not establish that it survives a crash. An empty
    file that is only in page cache is exactly the marker that would go
    missing in the power loss it exists to be read after - so the fd is
    fsynced, and then the CONTAINING DIRECTORY is fsynced too, because
    the datum here is a directory entry rather than file content, and a
    synced inode with an unsynced entry is still an absent marker.

    A marker only counts once every step has succeeded. If any step after
    creation fails the file is withdrawn again, because presence is the
    entire datum readers use and a half-established marker would publish
    exactly the claim this return value denies.

    False means no lifecycle transition was established, for any reason -
    already claimed, unwritable, or a failed sync - and the caller must
    not proceed as though it happened. If the withdrawal ALSO fails,
    LifecycleMarkerError is raised rather than returned: see its note.
    """
    path = Path(path)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    synced = True
    try:
        os.fsync(fd)
    except OSError:
        synced = False
    finally:
        try:
            os.close(fd)
        except OSError:
            synced = False
    if synced and _fsync_path(path.parent, os.O_RDONLY):
        return True
    if not _discard_marker(path):
        raise LifecycleMarkerError(
            f"{path} could not be durably established and could not be "
            "withdrawn; its presence now asserts a lifecycle transition "
            "that did not durably happen")
    return False


def mark_running(ctx: AttemptContext) -> bool:
    return _create_marker(ctx.running_marker_path)


def mark_terminal(ctx: AttemptContext) -> bool:
    return _create_marker(ctx.terminal_marker_path)


def lifecycle(attempt_dir: Path) -> str | None:
    """TERMINAL, RUNNING, or None - from marker PRESENCE alone.

    Presence means some process created the marker and did not withdraw
    it. _create_marker withdraws the ones whose durability it could not
    establish, and hard-fails when it cannot withdraw one, so a marker
    surviving a completed call was durably established - but a process
    killed between the create and the sync can still leave one behind,
    and this cannot tell that apart. TERMINAL wins over RUNNING because
    the markers accumulate rather than replace each other.

    None means no marker exists, or the directory could not be read:
    unknown, which is never either state.
    """
    attempt_dir = Path(attempt_dir)
    try:
        if (attempt_dir / TERMINAL_MARKER).is_file():
            return TERMINAL
        if (attempt_dir / RUNNING_MARKER).is_file():
            return RUNNING
    except OSError:
        return None
    return None


def node_command(ctx: AttemptContext) -> list[str]:
    """The real apparatus invocation. --out-json keeps the complete result
    off stdout, where it would meet run_bounded's ledger-sized tail."""
    return ["node", str(RUN_JS), ctx.url, ctx.sha, str(ctx.out_dir),
            "--out-json", str(ctx.result_path)]


def allocate_attempt(task_id: str, sha: str, *, root: Path | None = None) -> Path:
    """Claim a new attempt directory for this task and SHA, exclusively.

    mkdir is atomic: the ordinal that wins is the one that created the
    directory, so two callers racing at the same SHA cannot be handed the
    same attempt, and an attempt that already exists is never reused.
    """
    if not SHA_RE.match(sha or ""):
        raise ValueError("sha must be a full 40-character lowercase-hex git SHA")
    base = Path(root) if root is not None else config.EVIDENCE_DIR
    holder = base / task_id / sha
    holder.mkdir(parents=True, exist_ok=True)
    ordinal = 1
    while True:
        attempt = holder / f"attempt-{ordinal:04d}"
        try:
            attempt.mkdir()
        except FileExistsError:
            ordinal += 1
            continue
        (attempt / "accessibility").mkdir()
        return attempt


def _failed(ctx: AttemptContext, task_id: str, result: dict, reason: str) -> dict:
    return _record(ctx, task_id, result, FAILED, reason, [], None)


def _record(ctx: AttemptContext, task_id: str, result: dict, status: str,
            reason: str, checks: list, counts: dict | None) -> dict:
    return {
        "task_id": task_id,
        "sha": ctx.sha,
        "url": ctx.url,
        "attempt_id": ctx.attempt_dir.name,
        "attempt_dir": str(ctx.attempt_dir),
        "status": status,
        "reason": reason,
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "soft_timeout_exceeded": result["soft_timeout_exceeded"],
        "duration_ms": result["duration_ms"],
        "result_path": str(ctx.result_path),
        "stdout_path": str(ctx.stdout_path),
        "stderr_path": str(ctx.stderr_path),
        "browser_path": str(ctx.sidecar_path),
        # Absence is unknown, not "no browser was launched". C-05.2's
        # orphan detection must not read a missing sidecar as a clean run.
        "browser_sidecar_present": ctx.sidecar_path.is_file(),
        "screenshots": sorted(str(p) for p in ctx.out_dir.glob("*.png")),
        "checks": checks,
        "check_counts": counts,
        "stdout_tail_scrubbed": result["stdout"],
        "stderr_tail_scrubbed": result["stderr"],
    }


def run_accessibility(task_id: str, sha: str, url: str, *,
                      soft_timeout_seconds: int, hard_timeout_seconds: int,
                      root: Path | None = None, runner=None) -> dict:
    """Run the apparatus once into a fresh attempt and persist everything.

    Both timeout bounds are REQUIRED keyword arguments, with no defaults -
    see the note above. Omitting either is a TypeError at the call site,
    which is louder and earlier than an ungoverned number quietly
    becoming policy.

    `runner` is a test seam: a callable taking the AttemptContext and
    returning the command to run. Production uses node_command.
    """
    attempt = allocate_attempt(task_id, sha, root=root)
    ctx = context(attempt, sha, url)
    command = (runner or node_command)(ctx)

    result = workers.run_bounded(
        command, str(config.REPO_ROOT), soft_timeout_seconds, hard_timeout_seconds,
        stdout_path=ctx.stdout_path, stderr_path=ctx.stderr_path,
    )

    if result["timed_out"]:
        return _failed(ctx, task_id, result, "TIMED_OUT")
    if not result["ok"]:
        return _failed(ctx, task_id, result, "EXIT_NONZERO")
    if not ctx.result_path.is_file():
        return _failed(ctx, task_id, result, "RESULT_MISSING")

    try:
        payload = json.loads(ctx.result_path.read_text(encoding="utf-8"))
        checks = payload["checks"]
    except (OSError, ValueError, KeyError, TypeError):
        return _failed(ctx, task_id, result, "RESULT_UNREADABLE")
    if not isinstance(checks, list) or not checks:
        return _failed(ctx, task_id, result, "RESULT_EMPTY")

    # Evidence bound to a different commit is not evidence about this one.
    if any(check.get("sha") != sha for check in checks):
        return _failed(ctx, task_id, result, "SHA_MISMATCH")

    counts = {
        "PASS": sum(1 for c in checks if c.get("result") == "PASS"),
        "FAIL": sum(1 for c in checks if c.get("result") == "FAIL"),
    }
    return _record(ctx, task_id, result, COMPLETED, "", checks, counts)
