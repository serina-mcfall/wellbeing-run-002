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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config, workers

COMPLETED = "COMPLETED"
FAILED = "FAILED"

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
    sha: str
    url: str


def context(attempt_dir: Path, sha: str, url: str) -> AttemptContext:
    out_dir = Path(attempt_dir) / "accessibility"
    return AttemptContext(
        attempt_dir=Path(attempt_dir),
        out_dir=out_dir,
        result_path=out_dir / "result.json",
        stdout_path=out_dir / "stdout.txt",
        stderr_path=out_dir / "stderr.txt",
        sidecar_path=out_dir / "browser.json",
        sha=sha,
        url=url,
    )


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
