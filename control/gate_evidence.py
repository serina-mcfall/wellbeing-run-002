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
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import config, debt, redact, routing, security_contract, workers

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

# ------------------------------------------------- C-05.3a security evidence
#
# A security review is a QUALITATIVE provider judgement about a diff, not a
# browser run, so it gets its own attempt namespace beside the accessibility
# one rather than sharing it. "security-attempt-NNNN" cannot match the
# "attempt-" prefix scan_sidecars walks, so browser observation never sees
# these directories - and no RUNNING/TERMINAL markers are written here,
# because nothing launches a browser whose window they would bound.
SECURITY_OUTCOME_NAME = "security-outcome.json"
SECURITY_ATTEMPT_RE = re.compile(r"^security-attempt-[0-9]{4}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# C-05.3a provenance. Written into the attempt directory - which is already
# addressed by (task, sha, attempt) - immediately after the worktree is
# acquired and BEFORE the worker is spawned. It answers two questions that
# nothing else on disk can:
#
#   1. Which worker's artefacts belong to this attempt. The publisher reads
#      status and output by worker name, and a name is only trustworthy if
#      something durable says this attempt chose it. Missing or mismatched
#      provenance therefore refuses publication rather than guessing.
#   2. Which worktree this attempt acquired. That path is the only handle
#      cleanup has, and a crash between acquisition and Phase D would
#      otherwise lose it - leaving a real directory owned by nothing.
#
# Written before the spawn, not after, so the crash window it covers is the
# one that actually exists. It is NOT written when worktree acquisition
# fails, which is exactly what makes the reproduced "materialised but never
# acquired" path fail closed: no provenance, no publication.
SECURITY_PROVENANCE_NAME = "security-attempt.json"
SECURITY_PROVENANCE_KEYS = frozenset({
    "task_id", "pr", "sha", "attempt_id", "worker", "worktree",
})

# Why the attempt is not a completed review. ALIASES of
# control/security_contract, which owns the single string definition of
# each and composes the eleven-member union. These five describe running a
# process - this module's own concern - and the other six describe
# adjudicating the result, but both vocabularies are needed here and by
# the PR-record claim validator, so neither is restated anywhere.
# TIMED_OUT and EXIT_NONZERO coincide with the accessibility reasons in
# _adjudicate by design: same condition, same name.
TIMED_OUT = security_contract.TIMED_OUT
EXIT_NONZERO = security_contract.EXIT_NONZERO
OUTPUT_MISSING = security_contract.OUTPUT_MISSING
PROVIDER_FAILURE = security_contract.PROVIDER_FAILURE
SPAWN_OR_RUN_INCOMPLETE = security_contract.SPAWN_OR_RUN_INCOMPLETE

SECURITY_FAILURE_REASONS = security_contract.SECURITY_FAILURE_REASONS

# Bounds for everything provider-controlled that reaches durable evidence.
# A review-level summary is bounded at 500 to match routing.Review.as_dict;
# a per-finding summary uses debt.SUMMARY_MAX, the convention already
# governing a RETAINED finding's summary, rather than inventing a second
# number for the same kind of text.
REVIEW_SUMMARY_MAX = 500
REQUIRED_CHANGE_MAX = 1000
FILE_MAX = 512
PROVENANCE_MAX = 64

# The complete finite security-outcome key sets. CLOSED, following the
# convention metrics.METADATA_KEYS already sets for durable evidence:
# exactly these keys, always. An extra key on disk means the file was not
# written by this module, and a reader that tolerated it would let
# provider-shaped data ride into the Supervisor inside a record that
# otherwise looks plausible.
SECURITY_OUTCOME_KEYS = frozenset({
    "task_id", "pr", "sha", "attempt_id", "status", "reason", "verdict",
    "surfaces", "findings", "finding_counts", "summary", "provider", "model",
    "exit_code", "timed_out", "duration_ms", "stdout_name", "stderr_name",
})
SECURITY_FINDING_KEYS = frozenset({
    "id", "severity", "surface", "category", "file", "summary",
    "required_change",
})

STDOUT_NAME = "stdout.txt"
STDERR_NAME = "stderr.txt"

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


def _publish_json_once(directory: Path, filename: str, record: dict) -> bool:
    """Publish one JSON record into a directory, atomically and once.

    True only when the record is durable AND visible under its final
    name. False means nothing was durably published, and for an attempt
    outcome that means TERMINAL must not follow - a lifecycle marker
    saying an attempt ended, with nothing on disk saying how, is the
    contradiction this ordering exists to prevent.

    Written to a temporary name, fsynced, then LINKED into place rather
    than renamed. Three properties are needed and only link gives all
    three: rename would silently replace an existing record, and an
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

    A published-but-unsynced record is NOT withdrawn, unlike a marker
    whose durability fails. A marker's whole content is its presence, so
    a doubtful one must go; an outcome carries adjudication that cannot
    be reconstructed, and an outcome present without TERMINAL reads as
    "may still be running" - conservative in the direction that matters.
    Deleting an adjudication that may already be durable is worse.

    INTERNAL. `filename` is supplied only from trusted control-plane
    constants - OUTCOME_NAME and its siblings - never from a caller's
    input, a reviewer's output or anything reaching this process from
    outside. It is a fixed name chosen by this module, not a parameter a
    caller may steer.
    """
    directory = Path(directory)
    final = directory / filename
    tmp = directory / (filename + ".tmp")
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
    entry_durable = _fsync_path(directory, os.O_RDONLY) if published else False
    try:
        tmp.unlink()
    except OSError:
        pass
    else:
        if published:
            # Best-effort: the outcome is already linked and synced, so a
            # failure here leaves tidy-up undone, never the outcome undone.
            _fsync_path(directory, os.O_RDONLY)
    return published and entry_durable


def _publish_outcome(attempt_dir: Path, record: dict) -> bool:
    """C-05.2's accessibility attempt outcome, atomically and once.

    Contract unchanged: True only when the outcome is durable and visible
    under attempt-outcome.json, and TERMINAL must not follow a False. The
    durability algorithm and the reasoning behind every step of it live
    in _publish_json_once.
    """
    return _publish_json_once(Path(attempt_dir), OUTCOME_NAME, record)


def publish_security_outcome(attempt_dir: Path, record: dict) -> bool:
    """C-05.3a's security attempt outcome, atomically and once.

    Same durability contract as _publish_outcome and the same algorithm -
    see _publish_json_once - under a different fixed name, so a security
    attempt's adjudication is as write-once as an accessibility one.
    """
    return _publish_json_once(Path(attempt_dir), SECURITY_OUTCOME_NAME, record)


def _read_json_nofollow(path: Path):
    """One JSON record from a real, non-symlink regular file, or None.

    None for every unusable reason alike - absent, a symlink (ELOOP), not a
    regular file, unreadable, garbled. Never an exception carrying
    provider-shaped text, and never a partially-trusted record.

    Symlinks are not followed anywhere in the evidence tree: a link could
    point a reader at a record describing a different commit entirely, which
    is the stale-SHA attribution the whole design exists to prevent.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None  # absent, a symlink (ELOOP), unreadable - all unusable
    chunks: list[bytes] = []
    readable = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            readable = False
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
        return None
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:
        return None


def _provenance_invariants_hold(record) -> bool:
    """A provenance record this control plane could have written.

    Closed key set, canonical four-part identity, and two bounded strings.
    The worker name is checked against routing's own pattern rather than a
    second copy of it, so the thing validated here is the same thing that
    names the files on disk.
    """
    if not isinstance(record, dict) or set(record) != SECURITY_PROVENANCE_KEYS:
        return False
    if not _canonical_identity(record["task_id"], record["pr"], record["sha"],
                               record["attempt_id"]):
        return False
    worker = record["worker"]
    if not isinstance(worker, str) or not routing.SECURITY_WORKER_RE.match(worker):
        return False
    return _bounded_str(record["worktree"], 4096) and bool(record["worktree"])


def write_security_provenance(attempt_dir: Path, record: dict) -> bool:
    """Record which worker and worktree this attempt owns.

    Idempotent rather than write-once, and the difference matters: a crash
    between acquisition and spawn is resumed by re-running Phase C for the
    SAME attempt, which re-derives byte-identical provenance. Refusing the
    rewrite would strand the resumed attempt, so an existing record that
    already says the same thing is success. An existing record that says
    something DIFFERENT is a genuine identity collision and is refused.
    """
    if not _provenance_invariants_hold(record):
        return False
    existing = read_security_provenance(attempt_dir)
    if existing is not None:
        return existing == record
    return _publish_json_once(Path(attempt_dir), SECURITY_PROVENANCE_NAME,
                              record)


def read_security_provenance(attempt_dir: Path) -> dict | None:
    """The attempt's provenance record, or None for any unusable reason.

    None covers absent, unreadable, a symlink, not a regular file, garbled
    and structurally invalid alike. Callers must treat None as "provenance
    not established" and refuse to attribute any worker artefact to this
    attempt - never as "no constraint".
    """
    record = _read_json_nofollow(Path(attempt_dir) / SECURITY_PROVENANCE_NAME)
    if record is None or not _provenance_invariants_hold(record):
        return None
    return record


def scan_security_attempts(root: Path | None = None) -> tuple[list[dict], bool]:
    """Every readable security-attempt provenance record, and whether the
    walk was complete.

    (records, ok). ok is False when any part of the tree could not be
    enumerated, and a caller deciding whether a resource may be RELEASED
    must treat an incomplete walk as "unknown", never as "nothing found" -
    the same fail-closed rule scan_sidecars applies to browsers.

    Reads only. Nothing here creates, removes or repairs anything.
    """
    base = Path(root) if root is not None else config.EVIDENCE_DIR
    records: list[dict] = []
    if not base.is_dir():
        # No evidence tree yet is a complete answer, not a failed walk:
        # nothing has been attempted, so nothing is owned. Distinct from a
        # tree that exists but cannot be read, which falls through below.
        return records, True
    ok = True
    try:
        task_dirs = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return records, False
    for task_dir in task_dirs:
        try:
            sha_dirs = sorted(p for p in task_dir.iterdir() if p.is_dir())
        except OSError:
            ok = False
            continue
        for sha_dir in sha_dirs:
            try:
                attempts = sorted(p for p in sha_dir.iterdir()
                                  if p.is_dir()
                                  and SECURITY_ATTEMPT_RE.match(p.name))
            except OSError:
                ok = False
                continue
            for attempt in attempts:
                record = read_security_provenance(attempt)
                if record is None:
                    # Either no provenance was ever written (an attempt that
                    # never acquired a worktree owns nothing) or it is
                    # unreadable. Neither yields a resource to release, and
                    # an unreadable one must not silently become "absent".
                    if (attempt / SECURITY_PROVENANCE_NAME).exists():
                        ok = False
                    continue
                # The record must agree with WHERE it was found. The path
                # encodes task, full SHA and attempt independently of the
                # file's contents, so a record naming a different attempt is
                # one that has been moved, copied or corrupted - and this
                # record is what the publisher treats as an attempt's
                # identity. Trusting a displaced one would attribute a
                # review to whatever it happens to name. Dropped, and the
                # walk is no longer complete, so nothing is released on it.
                if (record["task_id"] != task_dir.name
                        or record["sha"] != sha_dir.name
                        or record["attempt_id"] != attempt.name):
                    ok = False
                    continue
                records.append(record)
    return records, ok


def security_attempt_dir(task_id: str, sha: str, attempt_id: str, *,
                         root: Path | None = None) -> Path:
    """Address the EXACT claimed security attempt. Allocates nothing.

    The ordinal is chosen under the state lock, in the Supervisor's
    state-only claim, and this function's whole job is to say where that
    claim lives. It never probes the filesystem, never increments, never
    recomputes an ordinal, and never substitutes a different directory
    because the named one already exists - any of those would let the
    filesystem, rather than the durable claim, decide which attempt is
    being written, which is precisely the race the state-first design
    removes.

    Every component is validated rather than trusted: a task id or attempt
    id carrying a separator, a parent reference or an absolute prefix would
    escape the evidence tree entirely. Rejection raises ValueError, because
    a caller handing an unvalidated identifier here has a bug that must not
    be papered over with a fallback path.
    """
    if not TASK_ID_RE.match(task_id or ""):
        raise ValueError("task_id must be a bare alphanumeric identifier")
    if not SHA_RE.match(sha or ""):
        raise ValueError("sha must be a full 40-character lowercase-hex git SHA")
    if not SECURITY_ATTEMPT_RE.match(attempt_id or ""):
        raise ValueError("attempt_id must be exactly security-attempt-NNNN")
    base = Path(root) if root is not None else config.EVIDENCE_DIR
    return base / task_id / sha / attempt_id


def _canonical_identity(task_id, pr, sha, attempt_id) -> bool:
    """Is this the four-part identity a security outcome is bound to?

    ONE definition, used when writing and when reading. Two copies would
    drift, and the failure mode of drift here is silent: a writer that
    accepts what the reader refuses produces durable evidence nothing can
    ever ingest. bool is excluded from pr explicitly - True would otherwise
    pass as PR number 1.
    """
    return (isinstance(task_id, str) and bool(TASK_ID_RE.match(task_id))
            and isinstance(pr, int) and not isinstance(pr, bool) and pr > 0
            and isinstance(sha, str) and bool(SHA_RE.match(sha))
            and isinstance(attempt_id, str)
            and bool(SECURITY_ATTEMPT_RE.match(attempt_id)))


def _bounded_str(value, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _count_int(value) -> bool:
    """A real non-negative count. bool is an int in Python, and True would
    otherwise read as a count of 1."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_exit_code(value) -> bool:
    """An int or None. None is legitimate: an attempt that never launched
    has no exit code, which is not the same as exiting zero."""
    return value is None or (isinstance(value, int)
                             and not isinstance(value, bool))


def _valid_duration_ms(value) -> bool:
    """A finite non-negative number, or None.

    NaN and the infinities are excluded explicitly. json.dumps emits them
    unquoted and json.loads reads them back, so a NaN duration would
    round-trip through the artifact and compare unequal to itself in
    anything that later reasoned about it.
    """
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _runtime_fields(result: dict) -> dict:
    """The three runtime-metadata fields, validated once for writer and
    reader alike.

    A malformed value here is a CONTROL-PLANE bug, not a provider failure,
    so it raises rather than becoming a persisted reason: filing our own
    schema mistake under a finite code would record it as evidence about
    the review. Nothing is coerced - a string exit code is a caller error,
    and turning it into an int would hide that.
    """
    if not isinstance(result, dict):
        raise ValueError("result must be a mapping")
    exit_code = result.get("exit_code")
    duration = result.get("duration_ms")
    if not _valid_exit_code(exit_code):
        raise ValueError("exit_code must be an int or None")
    if not _valid_duration_ms(duration):
        raise ValueError(
            "duration_ms must be a finite non-negative number or None")
    # Present AND an actual bool - never bool(...) over whatever arrived.
    # Coercion is the quietest kind of wrong here: "false" becomes True,
    # a missing key becomes False, and the record then states as fact that
    # the attempt did or did not time out when nothing ever said so.
    if "timed_out" not in result or not isinstance(result["timed_out"], bool):
        raise ValueError("timed_out must be present and an actual bool")
    return {"exit_code": exit_code,
            "timed_out": result["timed_out"],
            "duration_ms": duration}


def _safe_text(value, limit: int) -> str:
    """Scrub then hard-bound one provider-controlled string.

    Scrubbed rather than rejected, following control/debt.py: this is model
    output, and a redacted string is still valid evidence. Bounded second,
    so the bound applies to what will actually be written.
    """
    return redact.scrub(str("" if value is None else value))[:limit]


def _evidence_relative_path(value) -> str | None:
    """A source-relative file reference, or None if it is not one.

    None means the finding is INVALID, not that the path is blanked: a
    finding whose location cannot be trusted is not a lesser finding, and
    silently emptying the field would leave a blocking finding pointing
    nowhere. Absolute paths are refused outright rather than trimmed,
    because /home/<user>/... in durable evidence leaks the host layout, and
    a '..' segment claims to describe something outside the tree.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(("/", "\\", "~")):
        return None
    if re.match(r"^[A-Za-z]:", text):
        return None  # drive-letter absolute
    if ".." in text.replace("\\", "/").split("/"):
        return None
    return _safe_text(text, FILE_MAX)


def _normalise_finding(finding: dict) -> dict | None:
    """One finding reduced to the seven fields routing and fixing need.

    None means invalid - the caller fails the whole outcome closed rather
    than dropping this finding, because dropping the one finding that will
    not validate is how a malformed review becomes a clean pass.

    Every other key the reviewer sent is discarded, `evidence` most
    deliberately of all: it is unbounded prose quoting the diff, it is the
    field most likely to carry a secret verbatim, and nothing downstream
    needs it. `category` is set to SECURITY so the finding is consumable by
    the existing fixer path, which reads routing.KNOWN_GATES.
    """
    if not isinstance(finding, dict):
        return None
    finding_id = finding.get("id")
    if not isinstance(finding_id, str) or not finding_id.strip():
        return None
    severity = finding.get("severity")
    surface = finding.get("surface")
    if severity not in routing.SEVERITIES:
        return None
    if surface not in routing.SECURITY_SURFACES:
        return None
    path = _evidence_relative_path(finding.get("file"))
    if path is None:
        return None
    return {
        "id": _safe_text(finding_id.strip(), PROVENANCE_MAX),
        "severity": severity,
        "surface": surface,
        "category": "SECURITY",
        "file": path,
        "summary": _safe_text(finding.get("summary"), debt.SUMMARY_MAX),
        "required_change": _safe_text(finding.get("required_change"),
                                      REQUIRED_CHANGE_MAX),
    }


def _security_failed(task_id, pr, sha, attempt_id, result, reason,
                     provider, model) -> dict:
    if reason not in SECURITY_FAILURE_REASONS:
        raise ValueError("reason must be one of the finite security failure codes")
    return {
        "task_id": task_id, "pr": pr, "sha": sha, "attempt_id": attempt_id,
        "status": FAILED, "reason": reason,
        "verdict": None, "surfaces": None,
        "findings": [], "finding_counts": None, "summary": "",
        "provider": _safe_text(provider, PROVENANCE_MAX) if provider else None,
        "model": _safe_text(model, PROVENANCE_MAX) if model else None,
        **_runtime_fields(result),
        "stdout_name": STDOUT_NAME, "stderr_name": STDERR_NAME,
    }


def normalize_security_outcome(*, task_id: str, pr: int, sha: str,
                               attempt_id: str, result: dict,
                               review=None, reason: str = "",
                               provider: str | None = None,
                               model: str | None = None) -> dict:
    """The control plane's durable record of one security attempt.

    One helper, because normalisation and adjudication cannot be separated
    here without letting a caller persist a verdict the contract refuses.
    Pass a finite `reason` for an apparatus failure; pass a parsed
    SecurityReview and this decides whether it is a coherent C-05b contract
    and downgrades it to FAILED with routing's own finite reason if not.

    COMPLETED means reason == "", a recognised verdict, all twelve surfaces
    and populated counts. FAILED means a finite reason, null verdict, null
    surfaces, null counts. A valid SECURITY_FAIL is COMPLETED with a
    failing verdict - a review that ran and found something is not an
    apparatus failure, and conflating the two would hide real findings
    behind a broken-tooling reading.

    ALL valid findings are kept, P2/P3 included. The artifact is the
    evidence; separating blocking from debt is the caller's routing
    decision, and discarding debt here would destroy the record of it.
    """
    # Identifiers first, with the same predicate the strict reader uses.
    # Producing a structurally valid record under an identity the reader
    # will later refuse is worse than failing here: the attempt would
    # publish, look complete, and never be ingestible.
    if not _canonical_identity(task_id, pr, sha, attempt_id):
        raise ValueError("task_id, pr, sha and attempt_id must be canonical")
    if reason:
        return _security_failed(task_id, pr, sha, attempt_id, result, reason,
                                provider, model)
    if review is None:
        raise ValueError("normalize_security_outcome needs a review or a reason")

    ok, why = routing.security_is_consistent(review)
    if not ok:
        return _security_failed(task_id, pr, sha, attempt_id, result, why,
                                provider, model)

    findings = []
    for raw in review.findings:
        normalised = _normalise_finding(raw)
        if normalised is None:
            # The contract passed but a finding will not reduce - an
            # unusable path, a blank id. Fail the whole outcome closed
            # rather than persist a partial finding set that would read as
            # complete.
            return _security_failed(task_id, pr, sha, attempt_id, result,
                                    routing.FINDING_FIELDS_INVALID,
                                    provider, model)
        findings.append(normalised)

    return {
        "task_id": task_id, "pr": pr, "sha": sha, "attempt_id": attempt_id,
        "status": COMPLETED, "reason": "",
        "verdict": review.verdict,
        "surfaces": {s: review.surfaces[s] for s in routing.SECURITY_SURFACES},
        "findings": findings,
        "finding_counts": {s: sum(1 for f in findings if f["severity"] == s)
                           for s in routing.SEVERITIES},
        "summary": _safe_text(review.summary, REVIEW_SUMMARY_MAX),
        "provider": _safe_text(provider, PROVENANCE_MAX) if provider else None,
        "model": _safe_text(model, PROVENANCE_MAX) if model else None,
        **_runtime_fields(result),
        "stdout_name": STDOUT_NAME, "stderr_name": STDERR_NAME,
    }


def _is_canonical_text(value, limit: int) -> bool:
    """Whether a stored string is EXACTLY what the writer would have stored.

    Length is not the test. The writer scrubs and then bounds, and
    _safe_text is idempotent, so a value the writer produced survives being
    put through it again unchanged. A value that changes was never written
    here: it carries a raw secret the scrub would have redacted, or
    whitespace the writer would have stripped, or length the writer would
    have cut. Checking only the bound would let a forged record carry an
    unredacted credential in a field of perfectly legal size.

    The canonical replacement is deliberately NOT substituted for the
    stored value. A record that needed repairing is not evidence this
    module wrote, and silently repairing it on read would launder a forgery
    into trusted state.
    """
    return isinstance(value, str) and value == _safe_text(value, limit)


def _stored_finding_is_valid(finding) -> bool:
    """Whether one finding on disk is exactly what _normalise_finding
    produced - the same rules as writing, applied in reverse."""
    if not isinstance(finding, dict) or set(finding) != SECURITY_FINDING_KEYS:
        return False
    if finding["severity"] not in routing.SEVERITIES:
        return False
    if finding["surface"] not in routing.SECURITY_SURFACES:
        return False
    if finding["category"] != "SECURITY":
        return False
    finding_id = finding["id"]
    if not isinstance(finding_id, str) or not finding_id.strip():
        return False
    if finding_id != _safe_text(finding_id.strip(), PROVENANCE_MAX):
        return False
    # Structurally a source-relative path AND already in the exact form
    # _evidence_relative_path would have produced.
    if finding["file"] != _evidence_relative_path(finding["file"]):
        return False
    if not _is_canonical_text(finding["summary"], debt.SUMMARY_MAX):
        return False
    return _is_canonical_text(finding["required_change"], REQUIRED_CHANGE_MAX)


def _outcome_invariants_hold(record: dict) -> bool:
    """Whether a record on disk is a shape this module would have written.

    Strict on purpose, and strict about the boring fields too. A corrupt or
    hand-written file must not become trusted Supervisor evidence merely
    because its verdict and surfaces look plausible - those are the two
    fields an author would get right. The closed key set, the fixed artifact
    names, the bounds, and finding_counts agreeing with the findings it
    counts are what a forgery has to reproduce as well.
    """
    if not isinstance(record, dict) or set(record) != SECURITY_OUTCOME_KEYS:
        return False
    if not _canonical_identity(record["task_id"], record["pr"], record["sha"],
                               record["attempt_id"]):
        return False
    if record["stdout_name"] != STDOUT_NAME or record["stderr_name"] != STDERR_NAME:
        return False
    if not isinstance(record["timed_out"], bool):
        return False
    # The same predicates the writer validated against, so the writer
    # cannot emit a record this refuses. Absent bounds are legitimate: an
    # attempt that never launched has no exit code and no duration, which
    # is not the same as exiting zero in zero milliseconds.
    if not _valid_exit_code(record["exit_code"]):
        return False
    if not _valid_duration_ms(record["duration_ms"]):
        return False
    for key in ("provider", "model"):
        if record[key] is not None and not _is_canonical_text(record[key],
                                                              PROVENANCE_MAX):
            return False
    if not _is_canonical_text(record["summary"], REVIEW_SUMMARY_MAX):
        return False
    if not isinstance(record["findings"], list):
        return False

    status, reason = record["status"], record["reason"]
    if status == COMPLETED:
        if reason != "":
            return False
        if record["verdict"] not in (routing.SECURITY_PASS,
                                     routing.SECURITY_FAIL):
            return False
        surfaces = record["surfaces"]
        if not isinstance(surfaces, dict):
            return False
        if set(surfaces) != set(routing.SECURITY_SURFACES):
            return False
        if any(v not in routing.SURFACE_VALUES for v in surfaces.values()):
            return False
        if not all(_stored_finding_is_valid(f) for f in record["findings"]):
            return False
        counts = record["finding_counts"]
        if not isinstance(counts, dict) or set(counts) != set(routing.SEVERITIES):
            return False
        if not all(_count_int(v) for v in counts.values()):
            return False
        # The counts must count THESE findings. A tally that disagrees with
        # the list beside it is the cheapest way to make a review look
        # clean while its findings say otherwise.
        actual = {s: sum(1 for f in record["findings"] if f["severity"] == s)
                  for s in routing.SEVERITIES}
        if counts != actual:
            return False
        # The C-05b verdict rule, re-checked on the way in: a stored PASS
        # carrying a blocker, or a stored FAIL carrying none, was never
        # written by normalize_security_outcome.
        blocking = actual["P0"] + actual["P1"]
        if record["verdict"] == routing.SECURITY_PASS and blocking:
            return False
        return not (record["verdict"] == routing.SECURITY_FAIL and not blocking)

    if status == FAILED:
        return (reason in SECURITY_FAILURE_REASONS
                and record["verdict"] is None
                and record["surfaces"] is None
                and record["finding_counts"] is None
                and record["findings"] == []
                and record["summary"] == "")
    return False


def read_security_outcome(task_id: str, pr: int, sha: str, attempt_id: str, *,
                          root: Path | None = None) -> dict | None:
    """The durable outcome of one EXACT claimed attempt, or None.

    Bound to the whole four-part identity (task_id, pr, sha, attempt_id).
    A record naming a different PR is refused exactly like one naming a
    different SHA: the evidence tree is keyed by task and commit, so two
    pull requests can legitimately reach the same commit, and attributing
    one's review to the other would be the same wrong-head mistake in a
    different direction.

    None means "not usable evidence for these identifiers", for any reason:
    absent, unreadable, not a regular file, garbled, structurally invalid,
    or recording a different task, PR, SHA or attempt than the one asked
    for. Never an exception carrying provider-shaped text, and never a
    partially-trusted record.

    Symlinks are not followed. Evidence is only evidence where the evidence
    tree put it, and a link could point the reader at a record describing a
    different commit entirely - which is exactly the stale-SHA attribution
    this contract exists to prevent.

    No "latest attempt" scan: the caller holds the authoritative claimed
    attempt_id from durable state, and letting directory ordering choose
    would reintroduce the race the state-first claim removes.
    """
    if not _canonical_identity(task_id, pr, sha, attempt_id):
        return None
    try:
        path = security_attempt_dir(task_id, sha, attempt_id,
                                    root=root) / SECURITY_OUTCOME_NAME
    except ValueError:
        return None
    record = _read_json_nofollow(path)
    if record is None or not _outcome_invariants_hold(record):
        return None
    if (record["task_id"] != task_id or record["pr"] != pr
            or record["sha"] != sha or record["attempt_id"] != attempt_id):
        return None  # a record about something else is not this attempt
    return record


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
