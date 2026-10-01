"""Run the merge gate from the PINNED, READ-ONLY export. Approval action 8.

WHAT THIS IS. `apparatus/pr-evidence/live-gate.js` is a library function,
not a program: nothing in this repository runs it as a process. C-20a(C)
§9 action 8 is "build the gate invoker that runs `live-gate.js` from that
export". This is the Python half of it — the half that can be written and
tested without the export, the credential or the App existing.

WHY AN EXPORT AND NOT THIS TREE. §7 is explicit. The gate judges a pull
request, so it must not execute code that pull request supplied. The
export is created once, detached at the trusted revision, owned by
`run002-sup` and `chmod -R a-w`:

    git worktree add --detach /opt/run-002/gate-<SHA> <SHA>

`export_root` is therefore a REQUIRED, UNDEFAULTED argument, and
`config.REPO_ROOT` and the worker worktree root are refused BY NAME. A
default would be the whole vulnerability: the one path this must never
run from is the mutable tree this module itself lives in.

THE CODE COMES FROM THE EXPORT. THE GIT FACTS DO NOT, AND CANNOT.
================================================================
This is the distinction §7 turns on, and stating it loosely would make a
false claim. The export supplies the PROGRAM and the PINNED CONFIG. It
cannot supply the FACTS, because the only place a real head SHA, a real
`git worktree list`, or a real append-only ledger exists is the live
checkout. A detached export is a snapshot of committed files with no
branches worth resolving and no `.runtime/` at all.

So `live_repo_root` is a third REQUIRED, UNDEFAULTED argument, and it is
handed to the gate in the envelope below. §7's property survives intact:
the pull request under judgement does not supply the code that judges it.
What it does supply - because it has to - is the repository those facts
are read out of, which is why `.git/` needs the ownership treatment §6
gives `.runtime/`. "The gate reads nothing mutable" would be false, and
is not claimed here.

THE INVOKER TRAP, AND WHY THIS ARGUMENT EXISTS. Every `resolveRun002*`
wrapper in `apparatus/adapters/` derives its root from its OWN
`__dirname`. Run from the export, two of them break, each silently and
permanently:

  * `git-head.js::resolveRun002TrustedHeadSha` compares that root against
    `config/isolation.json`'s `workspace`. From the export they can never
    be equal, so it returns WORKSPACE_MISMATCH, live-gate denies
    HEAD_SHA_UNVERIFIED, and EVERY pull request is blocked forever.
  * `reviewer-identity.js::resolveRun002ReviewerIdentity` reads
    `<root>/.runtime/ledger.jsonl`. The export has no `.runtime/` - it is
    gitignored, so a fresh `git worktree add` creates none - and the
    adapter returns LEDGER_UNREADABLE, which live-gate reports as
    REVIEWER_UNVERIFIED. Also permanent.

The export is `chmod -R a-w`, so neither can be patched in place. The gate
program in the export must therefore call the LOW-LEVEL, repo-agnostic
functions - `resolveTrustedHeadSha(identity, { repoRoot })` and
`loadReviewerEvidence(runtimeDir, prNumber)`, both exported for exactly
this reason - with the live root this module supplies. The two Run002
wrappers must not appear in it at all. The two adapters whose roots feed
only CONFIG (`task-record.js`, `ci-result.js`) are a different case: the
pinned `config/tasks.json` and `config/experiment.json` are the trusted
copies and reading them from the export is correct.

AND IT MUST BE THE RIGHT COMMIT. `expected_revision` is required too, and
the export's actual checkout is read off disk and compared to it. A
detached `git worktree` writes `.git` as a file pointing at a gitdir whose
`HEAD` holds the SHA directly; that is read with ordinary file reads, no
subprocess and no git binary. A `HEAD` holding a SYMBOLIC ref is refused
outright — a branch moves, and an export pinned to a branch is not pinned.

FAIL CLOSED, FINITELY, AND WITHOUT RAISING. Every way this can go wrong
has its own token in OUTCOMES below, and `invoke_gate` returns one of them
rather than raising. There is no permissive branch: the only path that
returns a decision is the one where the export was trusted, the process
exited zero, the output parsed, and the decision carried every field the
publisher reads.

IT DOES NOT JUDGE, AND IT DOES NOT RECOMPUTE. The decision is passed
through exactly as the gate emitted it. In particular
`blockedOnlyByPendingIndependentReview` is TYPE-CHECKED and never derived:
live-gate.js computes it from the reason list plus the CI adapter's own
`ok`, and its own `F5 MUTATION` test demonstrates that the obvious
alternative — reading `mergeStateStatus === 'BLOCKED'` — would publish an
independent-review pass for a pull request whose CI concluded failure.
Anything downstream that recomputed the flag would be re-deriving it from
data that no longer carries the CI leg.

NOTHING CALLS THIS, AND IT REACHES NOTHING. It is not wired into the
Supervisor, the tick or any entry point, and it cannot publish: it has no
transport and no reference to the module that owns one. Deployment is
C-20a(C) and remains unapproved.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

# The program inside the export, relative to its root. The export is a
# checkout of this repository at the trusted revision, so the path is this
# repository's own layout. The script itself is the DEPLOYMENT half of
# action 8 and is deliberately not in this tree: wiring it means wiring
# `resolveRun002CiResult`, which shells out to `gh`, and `gh` is a real
# GitHub call. Its absence is reported as EXPORT_MISSING, not papered over.
GATE_ENTRY = "apparatus/pr-evidence/gate-cli.js"

# Finite outcomes. Prose is for humans; these are what the ledger records.
OK = "GATE_OK"
EXPORT_PATH_INVALID = "GATE_EXPORT_PATH_INVALID"
PIN_INVALID = "GATE_PIN_INVALID"
EXPORT_MISSING = "GATE_EXPORT_MISSING"
EXPORT_UNTRUSTED = "GATE_EXPORT_UNTRUSTED"
LIVE_ROOT_INVALID = "GATE_LIVE_ROOT_INVALID"
REQUEST_UNSERIALISABLE = "GATE_REQUEST_UNSERIALISABLE"
RUN_FAILED = "GATE_RUN_FAILED"
TIMED_OUT = "GATE_TIMED_OUT"
UNPARSEABLE = "GATE_OUTPUT_UNPARSEABLE"
MALFORMED = "GATE_DECISION_MALFORMED"

OUTCOMES = (OK, EXPORT_PATH_INVALID, PIN_INVALID, EXPORT_MISSING,
            EXPORT_UNTRUSTED, LIVE_ROOT_INVALID, REQUEST_UNSERIALISABLE,
            RUN_FAILED, TIMED_OUT, UNPARSEABLE, MALFORMED)

# The key the live checkout travels under, in the envelope the gate reads
# from stdin. The gate program is handed this value; it must never derive
# a root from its own `__dirname`. See THE INVOKER TRAP above.
LIVE_ROOT_KEY = "liveRepoRoot"
REQUEST_KEY = "request"

TIMEOUT = 120

ELIGIBLE = "ELIGIBLE"
DENIED = "DENIED"
VERDICTS = (ELIGIBLE, DENIED)

_SHA = "0123456789abcdef"


def _is_sha(value) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(c in _SHA for c in value))


@dataclass(frozen=True)
class RunResult:
    """One subprocess outcome. `timed_out` is its own fact, not an exit code.

    A gate that exited 124 of its own accord and a gate this module killed
    at the deadline are different events; collapsing them into one integer
    would lose the only one an operator can act on.
    """

    code: int
    stdout: str
    timed_out: bool = False


@dataclass(frozen=True)
class GateRun:
    """What one gate invocation produced. Exactly one of decision or a reason.

    `decision` is the gate's own object, unmodified. It is only ever
    non-None when `outcome` is OK.
    """

    ok: bool
    outcome: str
    decision: dict | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "outcome": self.outcome,
                "decision": self.decision}


def _refused(outcome: str) -> GateRun:
    return GateRun(ok=False, outcome=outcome, decision=None)


# The environment the GATE process may see. Allow-list, for the same reason
# `worker_entry.WORKER_ENV_ALLOWED` is one, and found the same way: by
# measuring rather than assuming.
#
# This spawn passed no `env=` at all, so the gate inherited the Supervisor's
# entire environment. Two names in it decide WHAT CODE THE GATE RUNS:
#
#   NODE_PATH     prepends module search roots. REPRODUCED 2026-10-02: a
#                 `live-gate.js` that fails `Cannot find module 'ajv/dist/2020'`
#                 from a real read-only export loads successfully when
#                 NODE_PATH points at a writable tree elsewhere. So the
#                 dependency tree of the thing judging a pull request could
#                 come from outside the pinned export.
#   NODE_OPTIONS  Node applies it to every process. `--require <file>` runs
#                 arbitrary code before the entry point.
#
# Neither is reachable by a product worker under the approval package's §6,
# so this is defence in depth rather than a live hole. It is cheap, the
# countermeasure already existed one layer down, and the whole point of §7
# is that the gate executes only code the pin covers - an argument that
# `NODE_PATH` quietly undoes.
#
# PATH is passed because `node` and `git` are looked up through it; the
# runbook should pin an absolute `node`, which is a deployment concern
# rather than something this module can enforce.
GATE_ENV_ALLOWED: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
})


def gate_child_env(parent: dict) -> dict:
    """The environment ONE gate process may see. Allow-list, not deny-list.

    A name invented tomorrow is dropped without anyone remembering to drop
    it. NODE_PATH and NODE_OPTIONS are absent by construction rather than
    by being named, which is the property that matters.
    """
    return {name: parent[name] for name in GATE_ENV_ALLOWED if name in parent}


def run_node(argv: list[str], stdin_text: str, timeout: int) -> RunResult:
    """The default runner: a LOCAL subprocess. Never a network call.

    Separated from `invoke_gate` so every test can inject its own and
    nothing spawns a process a test did not write itself. Shaped like
    `control/gh.py::run` — returned faults, never raised ones.

    The child environment is ALLOW-LISTED. See GATE_ENV_ALLOWED.
    """
    try:
        proc = subprocess.run(argv, input=stdin_text, capture_output=True,
                              text=True, timeout=timeout, check=False,
                              env=gate_child_env(os.environ))
    except subprocess.TimeoutExpired:
        return RunResult(code=124, stdout="", timed_out=True)
    except (OSError, ValueError) as exc:  # noqa: BLE001 - finite, not raised
        del exc
        return RunResult(code=127, stdout="")
    return RunResult(code=proc.returncode, stdout=proc.stdout)


def export_revision(export_root) -> str | None:
    """The 40-hex commit an export is checked out at, or None.

    Pure file reads: no git binary, no subprocess, nothing that could be
    made to execute something from inside the export. Two layouts are
    understood, because both occur —

      * `git worktree add --detach` writes `.git` as a FILE whose single
        line is `gitdir: <path>`; that directory's `HEAD` holds the SHA.
      * an ordinary clone has `.git` as a DIRECTORY with `HEAD` inside it.

    A `HEAD` holding `ref: refs/heads/...` returns None RATHER THAN being
    followed. An export pinned to a branch is not pinned at all: the branch
    moves, and the next commit on it silently becomes the code that judges
    pull requests. Refusing to resolve it is the point, not a limitation.
    """
    try:
        root = Path(export_root)
        marker = root / ".git"
        if marker.is_dir():
            head_file = marker / "HEAD"
        elif marker.is_file():
            line = marker.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = Path(line.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (root / gitdir).resolve()
            head_file = gitdir / "HEAD"
        else:
            return None
        head = head_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return head if _is_sha(head) else None


def _export_path_usable(export_root) -> Path | None:
    """An absolute path that is not a mutable tree of this experiment.

    §7: "never a path inside the pull request's worktree, and never
    `config.REPO_ROOT`, which is a mutable tree". Both are refused by name
    here rather than by convention, because a convention is not a check.
    """
    if not isinstance(export_root, (str, Path)):
        return None
    text = str(export_root)
    if not text or not Path(text).is_absolute():
        return None
    path = Path(text).resolve()
    for forbidden in (config.REPO_ROOT, config.WORKTREE_ROOT):
        try:
            forbidden_resolved = Path(forbidden).resolve()
        except (OSError, ValueError):
            continue
        if path == forbidden_resolved or forbidden_resolved in path.parents:
            return None
    return path


def _live_root_usable(live_repo_root, export_path: Path) -> Path | None:
    """A real checkout the gate can read git facts out of, and NOT the export.

    Three properties, all cheap and all load-bearing:

      * ABSOLUTE AND PRESENT, with a `.git` - the facts have to come from
        somewhere, and a path with no repository is not that somewhere. A
        worktree's `.git` is a file rather than a directory, so presence
        is what is checked, not kind.
      * NOT THE EXPORT, and not inside it. An export has no `.runtime/`
        and nothing worth resolving; pointing the gate at itself is the
        trap this argument exists to prevent, and it would fail as a
        permanent silent deny rather than as an error anyone could read.
      * NOT VALIDATED AGAINST `config/isolation.json`. Deliberately. The
        trap IS that comparison, made by a module against its own
        location; re-making it here would rebuild the coupling one layer
        up. The caller states which checkout it means, as it states where
        the trusted code is.
    """
    if not isinstance(live_repo_root, (str, Path)):
        return None
    text = str(live_repo_root)
    if not text or not Path(text).is_absolute():
        return None
    path = Path(text).resolve()
    if path == export_path or export_path in path.parents:
        return None
    if not path.is_dir() or not (path / ".git").exists():
        return None
    return path


def decision_is_usable(decision) -> bool:
    """Whether a parsed decision carries every field the publisher reads.

    SHAPE ONLY. Nothing here re-decides anything: a DENIED decision and an
    ELIGIBLE one are equally usable, and a decision this returns False for
    is one the gate did not finish making, not one it rejected.

    `blockedOnlyByPendingIndependentReview` must be a real bool. JSON can
    carry the string "true", and a downstream `if flag:` would treat it as
    a pass — so the type is checked here, at the boundary, while the value
    itself is passed through untouched.

    `trustedHeadSha` may be null: live-gate.js returns null when the task
    identity could not be resolved to a commit, and that is a decision it
    DID make (DENIED, HEAD_SHA_UNVERIFIED). Requiring a SHA would discard
    a verdict the publisher is entitled to act on.
    """
    if not isinstance(decision, dict):
        return False
    if decision.get("decision") not in VERDICTS:
        return False
    flag = decision.get("blockedOnlyByPendingIndependentReview")
    if flag is not True and flag is not False:
        return False
    if not isinstance(decision.get("reasons"), list):
        return False
    if "trustedHeadSha" not in decision:
        return False
    trusted = decision["trustedHeadSha"]
    return trusted is None or _is_sha(trusted)


def invoke_gate(request, *, export_root, expected_revision, live_repo_root,
                runner=None, timeout: int = TIMEOUT) -> GateRun:
    """Run the gate in `export_root` and return its decision. NEVER RAISES.

    `export_root`, `expected_revision` and `live_repo_root` are REQUIRED
    and undefaulted. There is no "the usual place" for any of them: saying
    where the trusted code is, which commit it must be, and which checkout
    its facts come from is the caller's job, every call.

    The gate is handed an ENVELOPE on stdin, not a bare request:

        {"liveRepoRoot": "<absolute path>", "request": <the request>}

    so the program in the export has the live root given to it and never
    has to derive one from its own location. See THE INVOKER TRAP in the
    module docstring for what happens when it does.

    Order is deliberate: the export is proved trustworthy BEFORE anything
    is executed from it, and the envelope is proved serialisable before a
    process is started that would be handed it.
    """
    path = _export_path_usable(export_root)
    if path is None:
        return _refused(EXPORT_PATH_INVALID)
    if not _is_sha(expected_revision):
        return _refused(PIN_INVALID)

    live_root = _live_root_usable(live_repo_root, path)
    if live_root is None:
        return _refused(LIVE_ROOT_INVALID)

    entry = path / GATE_ENTRY
    if not entry.is_file():
        return _refused(EXPORT_MISSING)

    actual = export_revision(path)
    if actual is None or actual != expected_revision:
        return _refused(EXPORT_UNTRUSTED)

    try:
        payload = json.dumps({LIVE_ROOT_KEY: str(live_root),
                              REQUEST_KEY: request})
    except (TypeError, ValueError):
        return _refused(REQUEST_UNSERIALISABLE)

    run = run_node if runner is None else runner
    try:
        result = run(["node", str(entry)], payload, timeout)
    except Exception:  # noqa: BLE001 - an injected runner must not escape
        return _refused(RUN_FAILED)
    if not isinstance(result, RunResult):
        return _refused(RUN_FAILED)
    if result.timed_out:
        return _refused(TIMED_OUT)
    if result.code != 0:
        return _refused(RUN_FAILED)

    try:
        decision = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _refused(UNPARSEABLE)
    if not isinstance(decision, dict):
        return _refused(UNPARSEABLE)
    if not decision_is_usable(decision):
        return _refused(MALFORMED)
    return GateRun(ok=True, outcome=OK, decision=decision)
