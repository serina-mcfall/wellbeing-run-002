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
import stat
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

# The attempt layout, spelled once. context() builds these paths,
# allocate_attempt creates the directory, and scan_sidecars walks for them
# later with no AttemptContext to hand - three spellings of one string is
# one rename away from a scanner that silently finds nothing.
ARTIFACT_DIR = "accessibility"
SIDECAR_NAME = "browser.json"

# How a sidecar READ went, kept separate from what it said. Collapsing
# these into "state is None" tells a caller that something is unknown
# while hiding whether the evidence is absent, unreachable or garbled -
# and those are not the same finding. Finite values only; an exception's
# prose is diagnostic text, not a status, and is never persisted.
#
# The invariant callers may rely on: sidecar_status is SIDECAR_OK if and
# only if state is OPEN or CLOSED.
# The control plane's own adjudication of one attempt, beside the markers
# at the attempt root because it describes the whole attempt rather than
# one artifact directory within it.
#
#   result.json            - what the apparatus found, when it ran at all
#   attempt-outcome.json   - what the CONTROL PLANE concluded, always
#   RUNNING/TERMINAL        - lifecycle facts
#
# The middle one has to exist before TERMINAL can be honest: result.json
# is absent on three of the seven outcomes, so without this the marker
# would be the only record that an attempt ended, asserting a durable
# outcome that was never written down.
OUTCOME_NAME = "attempt-outcome.json"

# Why an attempt's control-plane EVIDENCE could not be established. These
# are not accessibility results and never become one - see
# AttemptEvidenceError.
RUNNING_MARKER_FAILED = "RUNNING_MARKER_FAILED"
OUTCOME_PERSIST_FAILED = "OUTCOME_PERSIST_FAILED"
TERMINAL_MARKER_FAILED = "TERMINAL_MARKER_FAILED"

ATTEMPT_ERROR_CODES = frozenset({
    RUNNING_MARKER_FAILED, OUTCOME_PERSIST_FAILED, TERMINAL_MARKER_FAILED})

SIDECAR_OK = "OK"                  # read, parsed, state recognised
SIDECAR_MISSING = "MISSING"        # no such file
SIDECAR_UNREADABLE = "UNREADABLE"  # exists, and the read did not yield bytes
SIDECAR_INVALID = "INVALID"        # read, but not usable evidence

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
    out_dir = attempt_dir / ARTIFACT_DIR
    return AttemptContext(
        attempt_dir=attempt_dir,
        out_dir=out_dir,
        result_path=out_dir / "result.json",
        stdout_path=out_dir / "stdout.txt",
        stderr_path=out_dir / "stderr.txt",
        sidecar_path=out_dir / SIDECAR_NAME,
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


class AttemptEvidenceError(RuntimeError):
    """The attempt's control-plane evidence could not be established.

    NOT an accessibility result, and never encoded as one. A marker that
    would not write says nothing about whether a page is accessible, and
    returning status=FAILED for it would put a verdict about the PRODUCT
    into a record that only ever described the apparatus failing to write
    a file. Callers branch on status; they must not be handed a fabricated
    one.

    Carries ONE finite code and nothing else, so str(exc) is exactly that
    code. A caller that logs or persists this exception cannot leak a
    path, an errno string, or anything a child process emitted - which
    matters because LifecycleMarkerError's own message interpolates the
    marker path, and that text must not become reachable through this.
    """

    def __init__(self, error_code: str):
        if error_code not in ATTEMPT_ERROR_CODES:
            raise ValueError("unknown attempt evidence error code")
        super().__init__(error_code)
        self.error_code = error_code


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


def _marker_present(path: Path) -> tuple[bool, bool]:
    """(present, observed).

    Path.is_file() answers False for a marker it could not stat just as
    readily as for one that is not there, so a permission error on the
    attempt directory reads as "no marker" - a filesystem failure wearing
    the disguise of a fact about terminality. os.lstat separates them:
    only ENOENT proves absence, and every other errno proves nothing.

    lstat, never stat: the marker must be a REAL REGULAR FILE at that
    exact path. A symlink, a directory or a device there is not a marker
    this code created - _create_marker opens O_CREAT|O_EXCL, which refuses
    a symlinked final component outright - so it is an object we cannot
    account for, and an unaccountable object is unknown rather than a
    lifecycle transition we are willing to publish.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False, True
    except OSError:
        return False, False
    if not stat.S_ISREG(info.st_mode):
        return False, False
    return True, True


def _lifecycle(attempt_dir: Path) -> tuple[str | None, bool]:
    """(state, observed) - the observable form scan_sidecars needs.

    A TERMINAL marker we can see settles the question outright. Anything
    else rests on a stat that may have failed, and a marker we could not
    stat is never promoted to absent: `observed` carries that outward so
    the walk can refuse to call itself complete.
    """
    attempt_dir = Path(attempt_dir)
    terminal, terminal_ok = _marker_present(attempt_dir / TERMINAL_MARKER)
    if terminal:
        return TERMINAL, True
    running, running_ok = _marker_present(attempt_dir / RUNNING_MARKER)
    if running:
        return RUNNING, terminal_ok
    return None, terminal_ok and running_ok


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

    The scanner needs those two Nones told apart, so the work is done by
    _lifecycle(), which returns the observability flag alongside. This
    public form deliberately drops it: its contract is unchanged.
    """
    return _lifecycle(attempt_dir)[0]


def _sidecar(status: str, state: str | None = None, pid: int | None = None,
             start_ticks: int | None = None) -> dict:
    return {"sidecar_status": status, "state": state,
            "pid": pid, "start_ticks": start_ticks}


def _identity_int(value) -> int | None:
    """A real positive int, or None.

    bool is an int in Python, so True would otherwise pass as pid 1. A
    float or a numeric string is a value somebody would have to coerce,
    and proc.verified_alive() must never be handed an identity it had to
    guess at: an identity we are unsure of has to read as unknown, which
    fails closed, rather than as a number that might match some other
    process entirely.

    The > 0 floor is a FAIL-CLOSED CHOICE, not a proc.py invariant.
    proc.py rejects a non-positive PID but nowhere rejects a non-positive
    start_ticks; real /proc values are merely OBSERVED positive (see
    test_c09_resource_lifecycle.py, test_start_ticks_of_this_process_is_
    a_positive_int). Rejecting one only degrades the identity to unknown,
    which claims less - it cannot manufacture a false match.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def read_sidecar(path) -> dict:
    """Parse one browser.json into {sidecar_status, state, pid, start_ticks}.

    Never raises, never guesses, always the same four keys.

    The read status and the contents are separate answers. A sidecar that
    is absent, unreachable or garbled is not the same evidence as one that
    parsed cleanly and honestly recorded an unknown identity - run.js
    writes `pid: null` whenever the browser child is ambiguous, so
    SIDECAR_OK with state OPEN and a null identity is ORDINARY output and
    must stay distinguishable from corruption.

    A recognised state is part of being readable: a file saying state
    "EXITED" parsed fine but is not evidence this module can interpret, so
    it is INVALID rather than OK-with-a-blank-state. That keeps the
    invariant tight - OK if and only if OPEN or CLOSED.

    Unknown is never CLOSED and never "no browser was launched": run.js
    writes the sidecar only after chromium.launch() has returned, so a
    browser can be running while this file does not yet exist.

    The read does not follow symlinks. O_NOFOLLOW refuses a symlinked
    final component, and the regular-file check is made on the OPEN
    DESCRIPTOR rather than the path, so nothing swapped in afterwards can
    redirect it. Evidence is only evidence where the evidence tree put it;
    a link pointing out of the tree reads as UNREADABLE, which is a finite
    status and not a story about an exception.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _sidecar(SIDECAR_MISSING)
    except OSError:
        # ELOOP (the final component is a symlink), EACCES, EISDIR on some
        # platforms, ENOTDIR for a broken layout - all unreachable bytes.
        return _sidecar(SIDECAR_UNREADABLE)
    chunks: list[bytes] = []
    readable = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            readable = False  # a directory or device is not sidecar evidence
        else:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        readable = False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not readable:
        return _sidecar(SIDECAR_UNREADABLE)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is a ValueError
        return _sidecar(SIDECAR_INVALID)
    if not isinstance(payload, dict) or payload.get("state") not in (OPEN, CLOSED):
        return _sidecar(SIDECAR_INVALID)
    return _sidecar(SIDECAR_OK, payload["state"],
                    _identity_int(payload.get("pid")),
                    _identity_int(payload.get("start_ticks")))


def _subdirectories(path: Path, prefix: str = "") -> tuple[list[Path], bool]:
    """Immediate REAL subdirectories, sorted by name, and whether the
    listing was COMPLETE.

    False means something here could not be observed: the directory is
    absent, it could not be opened, or an entry could not be stat'ed. None
    of those is a "no" - a subtree we failed to read is not a subtree with
    nothing in it, and keeping that difference is the whole point.

    Symlinks are never traversed. follow_symlinks=False means a link is
    not a directory here however it resolves, so the walk can never be
    redirected out of the evidence tree, and an attempt cannot be counted
    twice by linking it in beside itself. A link that WOULD have resolved
    to a directory is standing exactly where a task, SHA or attempt
    directory belongs, so it clears the flag rather than being quietly
    dropped; a link to a plain file is not a directory candidate at all
    and is skipped like any other non-directory.

    An entry that fails individually is skipped with the flag cleared, so
    the siblings we did read still reach the caller. A failure of the
    directory read itself returns nothing, which is the fail-closed
    direction rather than a partial list that looks whole.
    """
    found: list[Path] = []
    ok = True
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(Path(entry.path))
                    elif entry.is_symlink() and entry.is_dir(follow_symlinks=True):
                        ok = False  # a directory we refuse to enter is not absent
                except OSError:
                    ok = False  # a stat we could not do is not a "not a directory"
    except OSError:
        return [], False
    return sorted(found, key=lambda p: p.name), ok


def scan_sidecars(root: Path | None = None) -> tuple[list[dict], bool]:
    """Every allocated attempt's sidecar, and whether the WALK completed.

    Walks <root>/<task>/<sha>/attempt-* one directory read at a time,
    because a glob reports a subtree it could not enter exactly the way it
    reports one that is empty. Every listing, every entry stat and every
    lifecycle-marker stat is checked; a failure clears the flag instead of
    vanishing. Nothing is followed through a symlink at any level.

    One record per discovered attempt, in deterministic sorted order,
    including attempts with NO sidecar - omitting those would delete the
    very uncertainty a caller has to fail closed on. Each record also
    names the task_id and sha its attempt belongs to: the layout is this
    module's to know, so no caller re-derives them from the path.

    This resolves nothing. No /proc is read, no identity is verified, no
    orphan is classified. What an OPEN sidecar under a TERMINAL attempt
    means is the caller's judgement.

    An absent or unreadable root returns ([], False). The scanner cannot
    prove WHY the root is missing - never allocated, or deleted underneath
    it - and "no attempt has ever run" is a claim only a caller with
    independent authority can make.

    walk_ok IS NOT BROWSER-OBSERVATION COMPLETENESS, and must never be
    handed straight to scan_ok["browser"]. It covers traversal and marker
    stats only. A sidecar that is MISSING, UNREADABLE or INVALID leaves
    walk_ok standing and travels as that record's sidecar_status, as does
    an OPEN record whose identity cannot be verified. Deciding the browser
    set is observed well enough to CLEAR a prior finding needs walk_ok AND
    every status OK AND every OPEN identity verifiable - that derivation
    belongs to reconcile/metrics, which alone may do the
    proc.verified_alive half of it.
    """
    base = Path(root) if root is not None else config.EVIDENCE_DIR
    records: list[dict] = []
    tasks, walk_ok = _subdirectories(base)
    for task in tasks:
        shas, shas_ok = _subdirectories(task)
        walk_ok = walk_ok and shas_ok
        for sha in shas:
            attempts, attempts_ok = _subdirectories(sha, "attempt-")
            walk_ok = walk_ok and attempts_ok
            for attempt in attempts:
                sidecar = attempt / ARTIFACT_DIR / SIDECAR_NAME
                state, observed = _lifecycle(attempt)
                walk_ok = walk_ok and observed
                record = read_sidecar(sidecar)
                record["attempt_dir"] = str(attempt)
                record["sidecar_path"] = str(sidecar)
                record["lifecycle"] = state
                record["task_id"] = task.name
                record["sha"] = sha.name
                records.append(record)
    return records, walk_ok


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
        (attempt / ARTIFACT_DIR).mkdir()
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


def _publish_outcome(attempt_dir: Path, record: dict) -> bool:
    """Publish one attempt's adjudicated outcome, atomically and once.

    True only when the outcome is durable AND visible under its final
    name. False means the attempt has no durable outcome, and TERMINAL
    must not follow - a lifecycle marker saying an attempt ended, with
    nothing on disk saying how, is the contradiction this ordering exists
    to prevent.

    Written to a temporary name, fsynced, then LINKED into place rather
    than renamed. Three properties are needed and only link gives all
    three: rename would silently replace an existing outcome, and an
    attempt's adjudication is write-once for the same reason its
    directory is; writing straight to the final name under O_EXCL would
    be write-once but could leave a HALF-WRITTEN file under the name
    readers trust; link is atomic, refuses a name already taken, and
    exposes only bytes that were complete before the link existed.

    The containing directory is fsynced after publication, because the
    datum is a directory entry and a synced inode with an unsynced entry
    is still an absent file - and again after the temp is removed, so the
    cleanup is durable too and a crash cannot leave debris that looks
    like an outcome in progress.

    A published-but-unsynced outcome is NOT withdrawn, unlike a marker
    whose durability fails. A marker's whole content is its presence, so
    a doubtful one must go; an outcome carries adjudication that cannot
    be reconstructed, and an outcome present without TERMINAL reads as
    "may still be running" - conservative in the direction that matters.
    Deleting an adjudication that may already be durable is worse.
    """
    attempt_dir = Path(attempt_dir)
    final = attempt_dir / OUTCOME_NAME
    tmp = attempt_dir / (OUTCOME_NAME + ".tmp")
    try:
        payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    except (TypeError, ValueError):
        return False
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        return False
    written = True
    try:
        view = payload
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError:
        written = False
    finally:
        try:
            os.close(fd)
        except OSError:
            written = False
    published = False
    if written:
        try:
            os.link(str(tmp), str(final))
            published = True
        except OSError:
            published = False  # already claimed, or the link did not happen
    entry_durable = _fsync_path(attempt_dir, os.O_RDONLY) if published else False
    try:
        tmp.unlink()
    except OSError:
        pass
    else:
        if published:
            # Best-effort: the outcome is already linked and synced, so a
            # failure here leaves tidy-up undone, never the outcome undone.
            _fsync_path(attempt_dir, os.O_RDONLY)
    return published and entry_durable


def _adjudicate(ctx: AttemptContext, task_id: str, result: dict) -> dict:
    """The control plane's verdict on one finished run - unchanged from
    C-05.1, lifted out so the lifecycle ordering around it reads in one
    line each instead of being interleaved with seven early returns."""
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
    if any(check.get("sha") != ctx.sha for check in checks):
        return _failed(ctx, task_id, result, "SHA_MISMATCH")

    counts = {
        "PASS": sum(1 for c in checks if c.get("result") == "PASS"),
        "FAIL": sum(1 for c in checks if c.get("result") == "FAIL"),
    }
    return _record(ctx, task_id, result, COMPLETED, "", checks, counts)


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

    Raises AttemptEvidenceError - never a synthetic accessibility result -
    when the attempt's own evidence cannot be established. There is no
    finally block anywhere below: TERMINAL is reachable only along the
    one path where the runner returned AND the outcome is already
    durable, and a blind finally would write it on every other path too,
    which is precisely the lie the marker must not tell.
    """
    attempt = allocate_attempt(task_id, sha, root=root)
    ctx = context(attempt, sha, url)

    # RUNNING first, and durably, because the marker is what bounds the
    # window in which a browser may exist. A runner launched before it is
    # a browser nothing on disk accounts for.
    #
    # The finite error is raised AFTER leaving the except block, never
    # inside it. `raise ... from None` only sets __cause__ and suppresses
    # display; __context__ would still hold the LifecycleMarkerError,
    # whose message interpolates the marker path - reachable by anything
    # that walks the chain.
    marker_failed = False
    try:
        started = mark_running(ctx)
    except LifecycleMarkerError:
        marker_failed = True
        started = False
    if marker_failed or not started:
        raise AttemptEvidenceError(RUNNING_MARKER_FAILED)

    command = (runner or node_command)(ctx)
    result = workers.run_bounded(
        command, str(config.REPO_ROOT), soft_timeout_seconds, hard_timeout_seconds,
        stdout_path=ctx.stdout_path, stderr_path=ctx.stderr_path,
    )

    record = _adjudicate(ctx, task_id, result)

    # The outcome BEFORE the marker. TERMINAL asserts the attempt ended,
    # and that assertion is only true if what it ended AS is already on
    # disk - otherwise a reader finds a finished attempt with no verdict.
    if not _publish_outcome(attempt, record):
        raise AttemptEvidenceError(OUTCOME_PERSIST_FAILED)

    marker_failed = False
    try:
        ended = mark_terminal(ctx)
    except LifecycleMarkerError:
        marker_failed = True
        ended = False
    if marker_failed or not ended:
        # The published outcome stays exactly as written - not rewritten,
        # not deleted. The accessibility adjudication really did complete;
        # only the lifecycle record of it did not, and overwriting a true
        # COMPLETED with a synthetic FAILED would destroy real evidence to
        # paper over a marker failure.
        raise AttemptEvidenceError(TERMINAL_MARKER_FAILED)

    return record
