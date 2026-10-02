"""C-22. A worker gets its OWN CLONE, not a linked worktree.

WHY THIS EXISTS. `tests/test_c22_worker_git_writes.py` measured that
approval-package §6's `.git/` row — refs and objects **read, not write**
for `run002-wrk` — is unimplementable against a LINKED WORKTREE: a
worktree's `.git` is a file pointing at `<main>/.git/worktrees/<name>`,
so every worker commit writes the MAIN repository. `.git/objects/`
read-only breaks `git add`; `.git/refs/` read-only breaks `git commit`.

That measurement left ONE RESIDUAL, and this module is the answer to it:

    POSIX modes cannot scope write access PER REF. A worker that can
    create `run-002/<its task>` in `<main>/.git/refs/heads/run-002/` can
    also move another task's branch in the same directory, and the
    shared object store it must be able to write is the one every other
    reader of that repository depends on.

THE ARRANGEMENT. The worker is given a SEPARATE CLONE, outside the
Supervisor's checkout, with its own object store and its own refs. It
commits there and pushes to GitHub. The Supervisor then obtains the
commits by fetching from the clone over a LOCAL PATH — no network, no
credential — into a ref namespace only the Supervisor writes.

What that buys, stated exactly:

  * a worker needs NO write anywhere under `<main>/.git`. §6's original
    git row becomes implementable as written, instead of corrected.
  * the gate export's gitdir (`<main>/.git/worktrees/gate-<SHA>/HEAD`,
    which `gate_invoker.export_revision` reads to prove the export is at
    the pin), `.git/hooks/` and `.git/config` are protected by the same
    single fact rather than by three separate per-directory modes.
  * the ref a worker can move is its OWN clone's copy. Nothing the
    Supervisor trusts reads it; the authoritative local ref lives in the
    Supervisor's repository, which the worker cannot write at all.

WHAT IT DOES NOT BUY, AND MUST NOT BE READ AS BUYING. §6 deploys ONE
worker identity, `run002-wrk`. Two workers running as the SAME uid can
reach each other's files whatever the layout is — §6 already records
"another worker's worktree: reachable, UNRESOLVED". Separate clones do
not change that; they change WHAT is reachable, from the Supervisor's
authoritative refs and shared object store to a peer's scratch clone.
Full per-worker isolation requires per-worker uids, and with them this
layout enforces it by `0750` while a linked worktree still cannot —
see `ownership_plan`.

FOUR THINGS WERE MEASURED, NOT ASSUMED (disposable fixtures, 2026-10-02,
this repository as the input). Each is pinned by a test in
`tests/test_c22_worker_clone_isolation.py`, with ONE stated exception:
the push half of measurement 2, because that suite does not push.

  1. `git clone <local path>` HARDLINKS the object files. Source and
     clone share an inode; `chmod 600` on the clone's copy changed the
     MAIN repository's object file to `600`. A `chown` handing such a
     clone to the worker would re-own the Supervisor's object store.
     Hence `--no-hardlinks`, forced in `clone_argv` and not optional.
  2. After any local clone, `origin` is the SOURCE PATH. A plain
     `git push origin <branch>` from such a clone created the branch and
     wrote three objects INTO the main repository. Leaving `origin`
     alone would reinstate, through the push path, precisely the write
     this arrangement exists to remove. Hence `origin_url` is a
     REQUIRED, UNDEFAULTED argument and must be `https://`.
  3. `git clone --shared` does NOT reintroduce writes to the shared
     store — a commit's objects landed in the clone (1 file -> 4) and
     the main store was unchanged (1376 -> 1376). It is still refused:
     it leaves `objects/info/alternates` pointing at the Supervisor's
     store, so the worker's history depends forever on a store the
     Supervisor may `gc`, and on that store staying readable to the
     worker. Costing 18MB per worker to not have that coupling is the
     cheaper side of the trade (see `ownership_plan` for the figures).
  4. FOUND WHILE BUILDING THIS, AND IT DEFEATS APPROVAL ACTION 6c AS
     SPECIFIED. This host's `~/.gitconfig` carries

         url.git@github.com:.insteadOf = https://github.com/

     After `git remote set-url origin https://github.com/<repo>.git`,
     `git config remote.origin.url` reads back the HTTPS URL — and
     `git remote get-url origin` AND `git remote get-url --push origin`
     both return `git@github.com:<repo>.git`. The URL git actually uses
     is SSH. Action 6c exists because "after action 5 a worker cannot
     read `serina`'s SSH key"; a silently rewritten remote sends the
     worker back to that key, which is the strongest write credential in
     the system and the one the split removes. So the effective URL is
     READ BACK FROM GIT and a mismatch is `ORIGIN_REWRITTEN` — a refused
     dispatch, never a worker quietly pushing as `serina`. The operator
     fix is to remove or scope that rewrite in the worker identity's own
     gitconfig; `DEPLOYMENT_CHECKS` V14f is where that gets confirmed.

NOTHING HERE CHOWNS, CHMODS, CREATES A USER, OR PUSHES. `ownership_plan`
and `DEPLOYMENT_CHECKS` are DATA describing what the deployment must do
and what only the deployed identities can confirm. `push_argv` returns
the command a worker will run; this module never runs it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

# Finite outcomes. Prose is for humans; these are what a caller records.
OK = "CLONE_OK"
DEST_INVALID = "CLONE_DEST_INVALID"
DEST_EXISTS = "CLONE_DEST_EXISTS"
SOURCE_INVALID = "CLONE_SOURCE_INVALID"
ORIGIN_INVALID = "CLONE_ORIGIN_INVALID"
BRANCH_INVALID = "CLONE_BRANCH_INVALID"
CLONE_FAILED = "CLONE_FAILED"
SEED_FAILED = "CLONE_SEED_FAILED"
BRANCH_FAILED = "CLONE_BRANCH_FAILED"
ORIGIN_FAILED = "CLONE_ORIGIN_FAILED"
ORIGIN_REWRITTEN = "CLONE_ORIGIN_REWRITTEN"
UPSTREAM_FAILED = "CLONE_UPSTREAM_FAILED"

OUTCOMES = (OK, DEST_INVALID, DEST_EXISTS, SOURCE_INVALID, ORIGIN_INVALID,
            BRANCH_INVALID, CLONE_FAILED, SEED_FAILED, BRANCH_FAILED,
            ORIGIN_FAILED, ORIGIN_REWRITTEN, UPSTREAM_FAILED)

# The ref namespace the Supervisor fetches worker commits into. Deliberately
# NOT `refs/heads/`: a fetched worker branch must never appear in
# `git branch`, never be checked out by accident, and never collide with a
# branch the Supervisor's own checkout holds. Only the Supervisor writes it.
SUPERVISOR_REF_PREFIX = "refs/run-002/workers"


def worker_clone_root(repo_root: Path | None = None) -> Path:
    """Where worker clones live: a SIBLING of the checkout, never inside it.

    Outside `config.REPO_ROOT` because everything that walks the checkout —
    the pin's drift set, the proposed trusted-CI protected path rule,
    `gate_invoker`'s refusal of export paths under the repository — would
    otherwise see 23MB of worker clone per worker as part of the apparatus.

    (That rule is named in prose rather than by its module name on
    purpose. Its C-23 test asserts that NO file under `control/` so much
    as mentions it by name, because it is a proposal pending approval and
    wiring it is a governance act rather than an implementation one.)

    Its own name, NOT `workers.managed_worktree_root()`. That root is what
    reverse orphan detection scopes to; a clone sitting in it would be
    classified as an orphaned git worktree, which it is not.
    """
    root = Path(repo_root) if repo_root is not None else config.REPO_ROOT
    return root.parent / f"{root.name}__worker-clones"


def clone_path(worker: str, repo_root: Path | None = None) -> Path:
    """This worker's clone. One directory per worker, named for the worker."""
    return worker_clone_root(repo_root) / worker


def supervisor_ref(worker: str, branch: str) -> str:
    """The ref in the SUPERVISOR's repository holding a worker's branch tip."""
    return f"{SUPERVISOR_REF_PREFIX}/{worker}/{branch}"


def https_origin_url(repo: str) -> str:
    """`origin` for a worker clone — HTTPS, per approval action 6c.

    After action 5 a worker cannot read `serina`'s SSH key, so SSH is not
    an option for it. HTTPS also means the credential is the worker App's
    own 1-hour installation token in `GH_TOKEN`, which is the identity the
    split exists to give it.
    """
    return f"https://github.com/{repo}.git"


def origin_url_permitted(url) -> bool:
    """Whether a URL may be a worker clone's `origin`. HTTPS, or nothing.

    THIS IS THE GUARD MEASUREMENT 2 BOUGHT. A local clone leaves `origin`
    at the source path, and a push to such an `origin` wrote straight into
    the main repository's refs and objects on a fixture. Anything that is
    not an `https://` URL — a path, `file://`, `ssh://`, `git@host:` — is
    refused, so the failure mode is a refused dispatch rather than a
    worker silently pushing into the Supervisor's repository.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return len(url) > len("https://")


def clone_argv(source, dest) -> list[str]:
    """The ONLY clone this module performs. `--no-hardlinks` is not optional.

    THIS IS THE GUARD MEASUREMENT 1 BOUGHT. Without it git hardlinks the
    object files, so the clone's objects and the Supervisor's objects are
    the same inodes and share one set of permissions and one owner.

    `--shared`/`--reference` are absent by construction rather than by
    being refused by name: there is one argv and it is this one.
    """
    return ["git", "clone", "--no-hardlinks", "--quiet", str(source), str(dest)]


def push_argv(branch: str) -> list[str]:
    """The push a worker will run. PREPARED HERE, NEVER EXECUTED HERE.

    `-u` matches `prompts/builder.md`; the upstream this also sets is
    configured at clone time so `prompts/fixer.md`'s bare `git push`
    works too.
    """
    return ["git", "push", "-u", "origin", branch]


def supervisor_fetch_argv(clone, branch: str, worker: str) -> list[str]:
    """How the Supervisor OBTAINS a worker's commits. Local path, offline.

    Run with `cwd` = the Supervisor's checkout. This is what replaces the
    shared object store: with a linked worktree the Supervisor already had
    the objects because the worker wrote them into its store; with a clone
    it fetches them, from a filesystem path, with no network call and no
    credential.

    `accessibility_services.isolated_checkout` is the only thing in this
    repository that needs a worker's commit locally — it runs
    `git worktree add --detach <sha>` in `config.REPO_ROOT` — and this
    fetch is what makes that SHA resolvable.

    NOT FORCED, DELIBERATELY. No leading `+`, so a worker that rewrote its
    branch cannot make the Supervisor's authoritative ref move
    non-fast-forward. The fetch fails and the Supervisor is told, rather
    than the ref silently changing meaning.
    """
    return ["git", "fetch", "--no-tags", str(clone),
            f"refs/heads/{branch}:{supervisor_ref(worker, branch)}"]


@dataclass(frozen=True)
class CloneResult:
    """One clone preparation. Exactly one of a usable path or a reason."""

    ok: bool
    outcome: str
    path: Path | None = None
    push: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"ok": self.ok, "outcome": self.outcome,
                "path": str(self.path) if self.path else None,
                "push": list(self.push)}


def _refused(outcome: str) -> CloneResult:
    return CloneResult(ok=False, outcome=outcome)


def run_git(argv: list[str], cwd: str | None = None,
            timeout: int = 300) -> tuple[bool, str]:
    """Default runner: a LOCAL subprocess. Returned faults, never raised.

    Returns `(ok, stdout)`. Stdout matters because one step - reading the
    EFFECTIVE remote URL back out of git - is the whole guard against an
    ambient `insteadOf` rewrite. Stderr is deliberately dropped: every way
    this can go wrong already has its own outcome token, and a git error
    string is not something to carry into durable evidence.

    Separated so every test injects its own and nothing spawns a process a
    test did not ask for.
    """
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, ""
    except (OSError, ValueError) as exc:  # noqa: BLE001 - finite, not raised
        del exc
        return False, ""
    return proc.returncode == 0, proc.stdout.strip()


def dest_permitted(dest, repo_root: Path | None = None) -> bool:
    """An absolute path outside the Supervisor's checkout and its worktrees.

    A clone inside `config.REPO_ROOT` would be read by everything that
    walks the apparatus; a clone inside the managed worktree root would be
    read as an orphaned worktree. Both are refused BY PATH here rather
    than by the caller remembering.
    """
    if not isinstance(dest, (str, Path)):
        return False
    text = str(dest)
    if not text or not Path(text).is_absolute():
        return False
    path = Path(text)
    root = Path(repo_root) if repo_root is not None else config.REPO_ROOT
    forbidden = (root, root.parent / f"{root.name}__worktrees",
                 config.WORKTREE_ROOT)
    for other in forbidden:
        try:
            other = Path(other)
        except (OSError, ValueError):
            continue
        if path == other or other in path.parents:
            return False
    return True


def prepare_clone(source, dest, *, branch: str, start_ref: str,
                  origin_url: str, repo_root: Path | None = None,
                  runner=None) -> CloneResult:
    """Create ONE worker clone, ready to commit and to push. NEVER RAISES.

    `origin_url` is REQUIRED and UNDEFAULTED for the reason measurement 2
    established: the default `origin` after a local clone is the source
    path, and a push to it writes the Supervisor's repository.

    `start_ref` is the ref IN THE SOURCE the branch starts from, so one
    function serves both roles without a mode flag:

      * a Builder starts from the base branch — `refs/heads/main`;
      * a Fixer starts from the Builder's tip, which the Supervisor has
        already fetched to `supervisor_ref(builder, branch)`.

    ORDER IS DELIBERATE. `origin` is rewritten IMMEDIATELY after the
    clone and before anything else — in particular before the branch
    exists — so there is no window in which a checkout carrying a
    worker's branch also carries an `origin` pointing at the Supervisor.
    """
    run = run_git if runner is None else runner

    if not dest_permitted(dest, repo_root):
        return _refused(DEST_INVALID)
    dest = Path(dest)
    if dest.exists():
        return _refused(DEST_EXISTS)
    source_path = Path(source) if isinstance(source, (str, Path)) else None
    if source_path is None or not (source_path / ".git").exists():
        return _refused(SOURCE_INVALID)
    if not origin_url_permitted(origin_url):
        return _refused(ORIGIN_INVALID)
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        return _refused(BRANCH_INVALID)
    if not isinstance(start_ref, str) or not start_ref:
        return _refused(BRANCH_INVALID)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _refused(DEST_INVALID)

    ok, _ = run(clone_argv(source_path, dest))
    if not ok:
        return _refused(CLONE_FAILED)

    ok, _ = run(["git", "remote", "set-url", "origin", origin_url], str(dest))
    if not ok:
        return _refused(ORIGIN_FAILED)

    # READ IT BACK FROM GIT, not from the config this just wrote. An
    # ambient `url.<ssh>.insteadOf <https>` makes `git config
    # remote.origin.url` report the HTTPS URL while every fetch and push
    # goes over SSH - measurement 4 in the module docstring, found on this
    # host. Both surfaces are checked because `pushInsteadOf` rewrites
    # only the push one.
    for scope in ([], ["--push"]):
        ok, effective = run(["git", "remote", "get-url", *scope, "origin"],
                            str(dest))
        if not ok:
            return _refused(ORIGIN_FAILED)
        # Coerced rather than trusted: this function promises never to
        # raise, and a runner that reported nothing must read as "not the
        # URL asked for" instead of as an AttributeError.
        if str(effective or "").strip() != origin_url:
            return _refused(ORIGIN_REWRITTEN)

    ok, _ = run(["git", "fetch", "--no-tags", str(source_path), start_ref],
                str(dest))
    if not ok:
        return _refused(SEED_FAILED)

    ok, _ = run(["git", "checkout", "-q", "-b", branch, "FETCH_HEAD"], str(dest))
    if not ok:
        return _refused(BRANCH_FAILED)

    # So `prompts/fixer.md`'s bare `git push` has an upstream. Written with
    # `git config` rather than `--set-upstream-to`, which would need an
    # `origin/<branch>` this offline clone has never seen.
    for key, value in (("remote", "origin"), ("merge", f"refs/heads/{branch}")):
        ok, _ = run(["git", "config", f"branch.{branch}.{key}", value],
                    str(dest))
        if not ok:
            return _refused(UPSTREAM_FAILED)

    return CloneResult(ok=True, outcome=OK, path=dest,
                       push=tuple(push_argv(branch)))


@dataclass(frozen=True)
class OwnedPath:
    """One line of the deployment's ownership table. Data, not an action."""

    path: str
    owner: str
    group: str
    mode: str
    why: str


def ownership_plan(worker: str, worker_identity: str = "run002-wrk",
                   repo_root: Path | None = None) -> tuple[OwnedPath, ...]:
    """What the deployment must set, and nothing this process may set.

    MEASURED COST OF THIS LAYOUT (disposable fixture built from this
    repository, 2026-10-02; `du` counting shared inodes once):

        linked worktree (today)          +5MB per worker, 0.05s
        clone, --no-hardlinks (this)    +23MB per worker, 0.16s
        clone, default hardlinks         +6MB per worker, 0.12s  REFUSED
        clone, --shared                  +5MB per worker, 0.07s  REFUSED

    18MB and 0.11s per worker is the whole price. The two cheaper clones
    are refused for the reasons in this module's docstring, not for cost.

    WHO MAKES THE CLONE, AND THEREFORE WHO OWNS IT. `create_worker_clone`
    runs inside the Supervisor, so the directory it creates is owned by
    `run002-sup`, and this process cannot `chown` it to anyone - that
    needs root. The deployment has to close that gap and there are only
    two shapes:

      * a ROOT HELPER chowns each new clone to the worker identity at
        dispatch. Strongest, and the only one under which the `0750` row
        below is literally true. Costs a privileged helper on the
        dispatch path.
      * the clone root is `2770` SETGID group `run002`, clones inherit
        the group, and the worker writes through the GROUP bit while
        `run002-sup` stays the owner. Needs no root and no per-dispatch
        step - but the clone is then writable by every member of
        `run002`, so it gives up the peer separation the `0750` row
        describes even if per-worker uids are deployed.

    This module takes no position between them and implements neither.
    V14a is what shows the chosen one actually lets a worker commit.

    `worker_identity` IS A PARAMETER FOR ONE REASON. §6 deploys a single
    `run002-wrk`, and under it two workers share a uid and can reach each
    other's clones — the residual §6 already records as UNRESOLVED. If the
    deployment instead gives each worker its own uid, THIS layout enforces
    the separation with `0750` because each worker's refs are in its own
    directory. A linked worktree cannot be made to enforce it at all: all
    workers' refs share `<main>/.git/refs/heads/run-002/`, and POSIX has
    no per-file-within-a-directory write bit to give them.
    """
    root = worker_clone_root(repo_root)
    return (
        OwnedPath(str(root), "run002-sup", "run002", "0755",
                  "the parent only; created once, holds no repository data"),
        OwnedPath(str(clone_path(worker, repo_root)), worker_identity,
                  "run002", "0750",
                  "the worker's whole clone - objects, refs, index, HEAD. "
                  "Group read lets the Supervisor run supervisor_fetch_argv "
                  "over a local path; group has no write bit, so with "
                  "per-worker uids a peer can read but not move its refs"),
        OwnedPath(str((Path(repo_root) if repo_root is not None
                       else config.REPO_ROOT) / ".git"),
                  "run002-sup", "run002", "0750",
                  "READ, NOT WRITE for the worker - §6's original row, which "
                  "a linked worktree made unimplementable and this layout "
                  "makes implementable. Read is still needed: the clone is "
                  "seeded from here"),
        OwnedPath(str((Path(repo_root) if repo_root is not None
                       else config.REPO_ROOT) / ".git" / "hooks"),
                  "run002-sup", "run002", "0700",
                  "code the Supervisor's own git executes; no worker reads "
                  "or writes it, and a clone never consults it"),
        OwnedPath(str((Path(repo_root) if repo_root is not None
                       else config.REPO_ROOT) / ".git" / "worktrees"),
                  "run002-sup", "run002", "0700",
                  "contains the gate export's gitdir, whose HEAD "
                  "gate_invoker.export_revision reads to prove the export "
                  "is at the pin. A worker needs nothing in here"),
    )


# ----------------------------------------------------------------------
# What ONLY the deployed identities can confirm.
#
# Everything this repository can check about the arrangement is checked in
# `tests/test_c22_worker_clone_isolation.py`, as ONE uid using `chmod` on
# disposable fixtures. That establishes which paths git touches and that a
# clone touches none of the Supervisor's. It establishes NOTHING about how
# `run002-wrk` behaves against files owned by `run002-sup`: group
# membership, umask, and any root-owned process can all differ.
#
# V14 AS WRITTEN DOES NOT COVER THIS ARRANGEMENT. It says "in a worker
# WORKTREE of the real checkout ... then attempt to write
# `<main>/.git/hooks/pre-commit`". Under separate clones there is no worker
# worktree of the real checkout, and the expectation is stronger: NOTHING
# under `<main>/.git` is writable, not just hooks. V14 must be replaced by
# the six checks below, which are its direct successors.
DEPLOYMENT_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("V14a",
     "As run002-wrk, in the worker clone created by action 5's dispatch: "
     "git add; git commit; git push -u origin <run-002 branch>",
     "all three SUCCEED - the clone is the worker's own and its origin is "
     "the worker App's HTTPS remote"),
    ("V14b",
     "As run002-wrk: write to <main>/.git/objects, <main>/.git/refs/heads, "
     "<main>/.git/config and <main>/.git/hooks/pre-commit",
     "ALL FOUR FAIL. This is the check V14 could not make: with a linked "
     "worktree the first two HAD to succeed, and now none may"),
    ("V14c",
     "As run002-wrk: read <main>/.git/worktrees/gate-<SHA>/HEAD",
     "FAILS. gate_invoker.export_revision reads that file to prove the "
     "export is at the pin; a worker must reach neither it nor its "
     "directory"),
    ("V14d",
     "As run002-sup, in the real checkout: run supervisor_fetch_argv for "
     "the branch V14a pushed, then git worktree add --detach <that sha>",
     "both SUCCEED - this is how the Supervisor obtains worker commits "
     "once the shared object store is gone, and the second is exactly what "
     "accessibility_services.isolated_checkout does"),
    ("V14e",
     "ONLY IF the deployment gives each worker its own uid: as worker A, "
     "write worker B's <clone>/.git/refs/heads/run-002/<B's task>",
     "FAILS. Under the single run002-wrk identity §6 specifies this check "
     "CANNOT PASS and must not be claimed - two workers sharing a uid "
     "reach each other's files whatever the layout. Record it as not "
     "applicable, never as passed"),
    ("V14f",
     "As run002-wrk, in that identity's OWN HOME: "
     "git config --get-regexp '^url\\.'",
     "EMPTY, or no entry whose insteadOf/pushInsteadOf value is a prefix "
     "of https://github.com/. This host's serina gitconfig carries "
     "url.git@github.com:.insteadOf=https://github.com/, which silently "
     "turns the worker's HTTPS origin back into SSH and sends it to the "
     "key action 5 took away. prepare_clone refuses with "
     "CLONE_ORIGIN_REWRITTEN if it is still there, so the failure is a "
     "refused dispatch - this check is how the operator finds out first"),
)
