"""GitHub operations through the `gh` CLI.

Thin, deterministic wrappers. The supervisor is the only component that calls
`merge`; Builders and Fixers have no merge path, and Codex authorises but does
not execute.

PER-ROLE AUTHENTICATION — approval action 6b. BUILT, OPT-IN, DEFAULT OFF.
========================================================================
`run` used to pass no `env=` at all, so every `gh` call inherited whatever
ambient auth the host happened to hold. The approval package's §9 action 6b
records what that costs: creating three Apps and installing them changes
nothing about who `gh` acts as, so §1's "the publisher cannot merge, the
merger cannot publish" is a property of the manifests and not of the running
system.

`run(..., role=...)` is the fix. A role names ONE of the three principals;
its token is read BY NAME from the environment at the moment of the call,
placed into an ALLOW-LISTED child environment as `GH_TOKEN`, and dropped
when the call returns.

  | role         | App                          | may         |
  |--------------|------------------------------|-------------|
  | `GATE`       | `run-002-independent-review` | write ONE commit status |
  | `SUPERVISOR` | `run-002-supervisor`         | merge |
  | `WORKER`     | `run-002-worker`             | push, open a PR |

THE DEFAULT IS UNCHANGED AND THAT IS DELIBERATE. `role` defaults to `None`,
which passes `env=None` — byte-for-byte today's behaviour, ambient auth and
all. No existing caller changes meaning, and switching a call site over is
one keyword. T+00 is NOT_STARTED and no App exists, so a default role would
be a change nobody could verify.

IT FAILS CLOSED, NEVER BACK TO AMBIENT. A role whose token is absent,
unrecognised, or past its recorded expiry does NOT fall through to the host
credential — it returns `AUTH_UNAVAILABLE` without starting a process. The
ambient identity holds `statuses: write` AND merge rights, so a silent
fallback would hand the weakest caller the strongest credential, which is
the exact hole action 6b exists to close.

THE VALUE IS NEVER RETURNED, LOGGED OR STORED. `role_base_env` builds the
child environment WITHOUT the credential and is the only env-shaped thing
this module hands back. The token is read inside `run`, written straight
into the dict `subprocess` is handed, and deleted. Nothing a test can
inspect carries it.

TOKEN RENEWAL — see `ensure_token`. An installation token lasts one hour
and a run lasts twenty-four, so the token in the environment is replaced
many times. The minting call is an EXTERNAL EDGE and is REQUIRED AND
INJECTED, exactly as `poster` and `http` are elsewhere: nothing in this
repository can mint anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import redact

TIMEOUT = 120

# ---------------------------------------------------------------- the roles

GATE = "gate"
SUPERVISOR = "supervisor"
WORKER = "worker"
ROLES = (GATE, SUPERVISOR, WORKER)

# The environment variable NAMES each principal's token travels under.
# NAMES ONLY. No value appears here, and nothing in this module ever returns,
# logs or compares one.
#
# The gate's name is spelled identically to the one the two publishing
# modules already read, on purpose: one principal, one name, so a token
# supplied for publication is the same token `gh` authenticates with. The
# other two are NEW — they are not yet in
# `experiment/github-app/env-var-names.md`, whose §2/§3 name only the
# standard `GH_TOKEN`, and adding them there is the integration owner's edit.
ROLE_TOKEN_ENV: dict[str, str] = {
    GATE: "RUN_002_GATE_APP_TOKEN",
    SUPERVISOR: "RUN_002_SUPERVISOR_APP_TOKEN",
    WORKER: "RUN_002_WORKER_APP_TOKEN",
}

# Where the token's EXPIRY is recorded. A timestamp, never a credential, so
# it is freely readable, printable and assertable — which is the whole point
# of keeping it in its own variable rather than inferring expiry from the
# token itself.
ROLE_EXPIRY_ENV: dict[str, str] = {
    role: f"{name}_EXPIRES_AT" for role, name in ROLE_TOKEN_ENV.items()
}

# The environment ONE `gh` child may see. Allow-list, for the same reason
# `gate_invoker.GATE_ENV_ALLOWED` and `worker_entry.WORKER_ENV_ALLOWED` are
# allow-lists, and shaped after them: a name invented tomorrow is dropped
# without anyone remembering to drop it.
#
# `GH_TOKEN` and `GITHUB_TOKEN` are ABSENT BY CONSTRUCTION. That is the
# load-bearing part. `gh` reads both, so a parent that holds the
# Supervisor's token would otherwise hand it to a worker's `gh` call no
# matter which role was named. The only `GH_TOKEN` a role-bound child ever
# sees is the one `run` writes from that role's own named variable.
#
# `GH_HOST` and `GH_REPO` are deliberately NOT here either: each silently
# redirects a call to a different host or repository than the arguments say.
GH_ENV_ALLOWED: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
    # gh's own per-role config directory — `env-var-names.md` §2 and §3.
    "GH_CONFIG_DIR",
})

# Exit code for "this never ran, because the role could not authenticate".
# Distinct from 124 (timeout) and 127 (no such binary) so an operator can
# tell a credential problem from an execution one.
AUTH_UNAVAILABLE = 125

# Exit code for "this never ran, because no expected head was supplied".
# A merge whose expected head is unknown is refused HERE rather than sent to
# GitHub with a missing or literal-`None` `sha`, which would be refused
# remotely for a reason an operator would have to decode from a 422.
EXPECTED_HEAD_MISSING = 123

# Finite reasons. Prose is for humans; these are what the ledger records.
ROLE_UNKNOWN = "ROLE_UNKNOWN"
ROLE_TOKEN_ABSENT = "ROLE_TOKEN_ABSENT"
ROLE_TOKEN_EXPIRED = "ROLE_TOKEN_EXPIRED"
EXPIRY_UNRECORDED = "ROLE_TOKEN_EXPIRY_UNRECORDED"

# How long before stated expiry a token is treated as due for renewal.
RENEWAL_MARGIN_SECONDS = 300

# How wrong this host's clock is allowed to be. Applied so the error is
# always on the side of refusing a token slightly EARLY: a token used one
# second after GitHub thinks it died fails with a 401 that looks like a
# permission problem, which is the hardest failure in this system to read.
CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True)
class Result:
    ok: bool
    stdout: str
    stderr: str
    code: int

    def json(self):
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None


def role_base_env(parent: dict) -> dict:
    """The environment ONE `gh` child may see, WITHOUT the credential.

    Public and pure so the allow-list can be asserted exhaustively, and
    CREDENTIAL-FREE so that asserting it can never expose one. `run` adds
    `GH_TOKEN` to its own private copy; this function never does.
    """
    return {name: parent[name] for name in GH_ENV_ALLOWED if name in parent}


def role_credential_present(role, environ=None) -> bool:
    """Whether a role's token is present. BY NAME ONLY — the value is never read.

    Shaped after the publishing module's own `credential_present`, and for
    the same reason:
    the question "is there a credential" has a boolean answer, and anything
    richer is an invitation to print one.
    """
    name = ROLE_TOKEN_ENV.get(role)
    if name is None:
        return False
    env = os.environ if environ is None else environ
    return bool(env.get(name))


def _now(now=None) -> datetime:
    return datetime.now(timezone.utc) if now is None else now


def parse_expiry(text):
    """An aware UTC datetime, or None. GitHub emits `2026-10-02T12:00:00Z`.

    A NAIVE timestamp returns None rather than being assumed UTC. "Whose
    clock is this" has no safe default answer, and guessing wrong by the
    host's offset is hours of validity invented or thrown away.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def token_expiry(role, environ=None):
    """The recorded expiry of a role's token, or None when none is recorded."""
    name = ROLE_EXPIRY_ENV.get(role)
    if name is None:
        return None
    env = os.environ if environ is None else environ
    return parse_expiry(env.get(name))


def token_usable(role, environ=None, now=None, margin_seconds: int = 0) -> tuple[bool, str]:
    """(usable, reason) for one role. NEVER reads the credential's value.

    Three refusals, in order, and NONE of them falls back to ambient auth:

      * `ROLE_UNKNOWN` — not one of the three principals.
      * `ROLE_TOKEN_ABSENT` — the named variable is unset or empty.
      * `ROLE_TOKEN_EXPIRY_UNRECORDED` — a token is present but nothing
        says when it dies. REFUSED RATHER THAN TRUSTED, deliberately. An
        installation token lasts an hour and a run lasts a day, so an
        untracked token is one that WILL expire mid-run and start
        returning 401s that read like permission faults. Refusing makes
        `ensure_token` — which always records an expiry — the only way to
        supply one, which is the behaviour the procedure depends on.
      * `ROLE_TOKEN_EXPIRED` — the recorded expiry has passed, or is
        within `margin_seconds + CLOCK_SKEW_SECONDS` of passing.
    """
    if role not in ROLE_TOKEN_ENV:
        return False, ROLE_UNKNOWN
    if not role_credential_present(role, environ):
        return False, ROLE_TOKEN_ABSENT
    expiry = token_expiry(role, environ)
    if expiry is None:
        return False, EXPIRY_UNRECORDED
    horizon = _now(now) + timedelta(seconds=margin_seconds + CLOCK_SKEW_SECONDS)
    if expiry <= horizon:
        return False, ROLE_TOKEN_EXPIRED
    return True, ""


# ------------------------------------------------------------ token renewal

TOKEN_FRESH = "TOKEN_FRESH"
TOKEN_RENEWED = "TOKEN_RENEWED"
MINT_FAILED = "TOKEN_MINT_FAILED"
MINT_MALFORMED = "TOKEN_MINT_MALFORMED"
MINT_TOO_SHORT = "TOKEN_MINT_EXPIRES_TOO_SOON"

RENEWAL_OUTCOMES = (TOKEN_FRESH, TOKEN_RENEWED, ROLE_UNKNOWN, MINT_FAILED,
                    MINT_MALFORMED, MINT_TOO_SHORT)


@dataclass(frozen=True)
class Renewal:
    """What one renewal attempt did. CARRIES NO CREDENTIAL.

    `expires_at` is a timestamp. There is deliberately no field a token
    could occupy, so nothing that logs, reprs or asserts a `Renewal` can
    expose one.
    """

    ok: bool
    outcome: str
    expires_at: str | None = None


# One lock per role. Two threads asking for the same role at once must mint
# ONCE: the second waits, re-checks inside the lock, sees the first thread's
# write and returns TOKEN_FRESH. Minting twice is not merely wasteful —
# GitHub invalidates nothing, so two live tokens exist and the one in the
# environment is whichever write landed last.
#
# ACROSS PROCESSES THERE IS NO SHARED STATE AND NONE IS WANTED. Each
# principal is its own process with its own environment (`env-var-names.md`
# §2: one `GH_TOKEN` per process, never both), so two processes renewing
# independently is correct rather than a race.
_RENEWAL_LOCKS: dict[str, threading.Lock] = {role: threading.Lock() for role in ROLES}


def _unpack_mint(minted) -> tuple:
    """(token, expires_at) from whatever the injected minter returned.

    GitHub's `POST /app/installations/{id}/access_tokens` answers with
    `{"token": ..., "expires_at": ...}`, so a mapping is accepted whole —
    the operator's minter can hand back the parsed response without
    reshaping it. A two-element sequence is accepted too.

    Anything else yields `(None, None)` and is reported as malformed. The
    token is NOT validated for shape beyond being a non-empty string:
    pattern-matching a credential is how one ends up in a diagnostic.
    """
    if isinstance(minted, dict):
        token, expires_at = minted.get("token"), minted.get("expires_at")
    elif isinstance(minted, (tuple, list)) and len(minted) == 2:
        token, expires_at = minted[0], minted[1]
    else:
        return None, None
    if not isinstance(token, str) or not token:
        return None, None
    return token, expires_at


def _good_for(role, env, now, margin_seconds) -> bool:
    usable, _ = token_usable(role, env, now, margin_seconds)
    return usable


def ensure_token(role, *, mint, environ=None, now=None,
                 margin_seconds: int = RENEWAL_MARGIN_SECONDS) -> Renewal:
    """Make `role`'s token good for at least `margin_seconds`. NEVER RAISES.

    `mint(role)` is REQUIRED AND UNDEFAULTED — the whole external edge,
    injected, exactly as `poster` is for the publisher and `http` is for
    the transport. NOTHING IN THIS REPOSITORY MINTS A TOKEN: there is no
    default minter, no JWT assembly, no private-key read, and no import
    that could reach one. Every test passes a simulated minter.

    It must return `{"token": ..., "expires_at": ...}` — GitHub's own
    response shape — or a `(token, expires_at)` pair.

    THE FRESH TOKEN LANDS IN THE ENVIRONMENT AND NOWHERE ELSE. It is never
    returned, never logged, never stored on this module or on the
    `Renewal`. `run(role=...)` then reads it back by name at call time, so
    there is exactly one place a live credential lives and it is the place
    the deployment already had to protect.

    THE FOUR FAILURES THE APPROVAL PACKAGE NAMES, EACH ITS OWN OUTCOME:

      * a renewal that FAILS — any exception out of the minter becomes
        `TOKEN_MINT_FAILED`, and the OLD token is left exactly as it was.
        A failed renewal must not destroy a credential that still has
        minutes left on it; the next attempt retries.
      * a token that EXPIRED MID-OPERATION — `margin_seconds` means
        renewal happens while the old one is still valid, and
        `run(role=...)` independently refuses a token past its recorded
        expiry, so an operation never starts on a dead credential.
      * CLOCK SKEW — `CLOCK_SKEW_SECONDS` is added to every horizon, so
        both the renew-now decision and the is-it-dead decision err early.
        A token minted with less than `margin + skew` of life left is
        refused as `TOKEN_MINT_EXPIRES_TOO_SOON` rather than installed,
        because installing it guarantees another renewal immediately.
      * CONCURRENT RENEWAL — one lock per role, with the freshness check
        re-made INSIDE it. The loser of the race mints nothing.
    """
    if role not in ROLE_TOKEN_ENV:
        return Renewal(False, ROLE_UNKNOWN)
    env = os.environ if environ is None else environ

    with _RENEWAL_LOCKS[role]:
        # Re-checked inside the lock, not outside. Checking before taking it
        # is what makes two threads both decide to mint.
        if _good_for(role, env, now, margin_seconds):
            return Renewal(True, TOKEN_FRESH, env.get(ROLE_EXPIRY_ENV[role]))

        try:
            minted = mint(role)
        except Exception:        # noqa: BLE001 - a minting fault is finite
            return Renewal(False, MINT_FAILED)

        token, expires_at = _unpack_mint(minted)
        if token is None:
            return Renewal(False, MINT_MALFORMED)

        parsed = parse_expiry(expires_at)
        if parsed is None:
            del token
            return Renewal(False, MINT_MALFORMED)
        if parsed <= _now(now) + timedelta(
                seconds=margin_seconds + CLOCK_SKEW_SECONDS):
            del token
            return Renewal(False, MINT_TOO_SHORT)

        env[ROLE_TOKEN_ENV[role]] = token
        env[ROLE_EXPIRY_ENV[role]] = expires_at
        del token
        return Renewal(True, TOKEN_RENEWED, expires_at)


# ------------------------------------------------------------------ the run


def run(args: list[str], cwd: str | None = None, timeout: int = TIMEOUT,
        *, role: str | None = None, environ=None) -> Result:
    """One `gh`/`git` invocation.

    `role` defaults to `None`, which passes `env=None` — the child inherits
    this process's environment and whatever ambient auth it holds. That is
    today's behaviour, unchanged, for every caller that does not opt in.

    Naming a role replaces that with an ALLOW-LISTED environment carrying
    exactly that principal's token as `GH_TOKEN`. The value is read here,
    handed to `subprocess`, and dropped; it is not returned and not stored.
    """
    env = None
    if role is not None:
        parent = os.environ if environ is None else environ
        usable, reason = token_usable(role, parent)
        if not usable:
            # NO FALLBACK. Refusing is the point: see the module docstring.
            return Result(False, "", f"{reason}: role={role}", AUTH_UNAVAILABLE)
        env = role_base_env(parent)
        env["GH_TOKEN"] = parent[ROLE_TOKEN_ENV[role]]

    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        return Result(False, "", f"timeout after {timeout}s", 124)
    except FileNotFoundError as exc:
        return Result(False, "", str(exc), 127)
    finally:
        # The only live copy of the credential this function made.
        del env
    return Result(
        proc.returncode == 0,
        redact.scrub(proc.stdout.strip()),
        redact.scrub(proc.stderr.strip()),
        proc.returncode,
    )


def git(args: list[str], cwd: str) -> Result:
    return run(["git", *args], cwd=cwd)


def repo_exists(repo: str, *, role: str | None = None) -> bool:
    return run(["gh", "repo", "view", repo, "--json", "name"], role=role).ok


def default_branch(repo: str, *, role: str | None = None) -> str | None:
    result = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"],
                 role=role)
    data = result.json() or {}
    return (data.get("defaultBranchRef") or {}).get("name")


PR_FIELDS = (
    "number,state,isDraft,title,headRefName,headRefOid,baseRefName,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,url,labels,author,mergeCommit"
)


def list_open_prs(repo: str, *, role: str | None = None) -> list[dict]:
    result = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                  "--json", PR_FIELDS, "--limit", "50"], role=role)
    return result.json() or []


def pr_view(repo: str, number: int, *, role: str | None = None) -> dict | None:
    result = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS],
                 role=role)
    return result.json()


def pr_diff_sha(repo: str, number: int, *, role: str | None = None) -> str | None:
    """The head commit of the PR - the identity of the material diff under review.

    The no-role call is made with the ARGUMENTS IT ALWAYS HAD, rather than
    with `role=None` passed explicitly. This is the one place a public
    function here calls another, so it is the one place where "opting in
    changes nothing for callers that do not" has to hold for test doubles
    as well as for production code: a double standing in for `pr_view`
    sees the same call it has always seen.
    """
    data = (pr_view(repo, number) if role is None
            else pr_view(repo, number, role=role)) or {}
    return data.get("headRefOid")


def checks_state(pr: dict, required: tuple[str, ...]) -> tuple[bool, str]:
    """Whether every required check has concluded successfully."""
    rollup = pr.get("statusCheckRollup") or []
    by_name = {}
    for check in rollup:
        name = check.get("name") or check.get("context") or ""
        conclusion = (check.get("conclusion") or check.get("state") or "").upper()
        status = (check.get("status") or "").upper()
        by_name[name] = (status, conclusion)

    if not required:
        if not rollup:
            return False, "no checks reported"
        failing = [n for n, (_, c) in by_name.items()
                   if c not in ("SUCCESS", "NEUTRAL", "SKIPPED", "")]
        pending = [n for n, (s, c) in by_name.items() if s and s != "COMPLETED" and not c]
        if failing:
            return False, f"failing: {', '.join(sorted(failing))}"
        if pending:
            return False, f"pending: {', '.join(sorted(pending))}"
        return True, "all reported checks passed"

    # A REQUIRED check is held to a stricter rule than a reported one, and
    # the three ways it used to be satisfiable without running are each
    # reproduced in tests/test_c23_required_check_integrity.py.
    #
    #   SKIPPED   a workflow that skips the `ci` job produced a conclusion
    #             this accepted as green. A required check that did not run
    #             is not a passing one - it is an absent one.
    #   NEUTRAL   same.
    #   a STATUS  `by_name` keys on `name` OR `context`, so a commit STATUS
    #             whose context is "ci" satisfied a requirement the workflow
    #             never met. A status is not a check run and is not produced
    #             by CI at all.
    #
    # `apparatus/adapters/ci-result.js` has always required `success`
    # exactly, on the check-run surface only. This brings the Python merge
    # path - the SOLE merge authority - to the rule the JavaScript gate
    # already applies, rather than inventing a new one.
    #
    # WHAT THIS DOES NOT CLOSE: a workflow MODIFIED in the pull request's
    # own branch that still produces a check run named `ci` concluding
    # success. Matching is by name, and nothing here reads the workflow
    # definition. That gap is recorded in the approval package §6 and needs
    # either workflow-content integrity or GitHub-side app pinning.
    # THE CHECK-RUN TEST IS STRUCTURAL, NOT `__typename`-BASED, AND THAT IS
    # DELIBERATE. `gh pr view --json statusCheckRollup` may well emit
    # `__typename`, but THIS REPOSITORY CANNOT PROVE IT - no real `gh` call
    # has ever been made here. Requiring a field whose presence is unverified
    # is how a gate denies every pull request on launch day, which is the
    # exact shape of two defects already found in this arrangement.
    #
    # So the kind is read from the SHAPE, using only fields the line above
    # already reads: a CheckRun carries `conclusion`; a StatusContext carries
    # `state` and has no `conclusion` at all. `__typename` is honoured when
    # it IS present and disagrees, which costs nothing and catches the case
    # where a future payload grows a `conclusion` on a status.
    by_shape = {}
    for check in rollup:
        name = check.get("name") or check.get("context") or ""
        kind = check.get("__typename")
        is_run = bool(check.get("conclusion")) and kind != "StatusContext"
        # An in-progress CheckRun has no conclusion yet; it is still a run,
        # and it is refused below by the SUCCESS test rather than here, so
        # the diagnostic says "not green" instead of "not a check run".
        if kind == "CheckRun" or check.get("status"):
            is_run = kind != "StatusContext"
        by_shape[name] = is_run

    missing = [name for name in required if name not in by_name]
    if missing:
        return False, f"required check(s) not reported: {', '.join(missing)}"
    not_a_run = [name for name in required if not by_shape.get(name)]
    if not_a_run:
        return False, ("required check(s) not satisfied by a CHECK RUN: "
                       f"{', '.join(sorted(not_a_run))}")
    bad = [name for name in required if by_name[name][1] != "SUCCESS"]
    if bad:
        return False, f"required check(s) not green: {', '.join(bad)}"
    return True, "required checks green"


def update_branch(repo: str, number: int, *, role: str | None = None) -> Result:
    """Reconcile the PR branch with current main."""
    return run(["gh", "pr", "update-branch", str(number), "--repo", repo], role=role)


def mark_ready(repo: str, number: int, *, role: str | None = None) -> Result:
    """Take a draft pull request out of draft.

    Grants nothing. It changes only whether GitHub considers the pull request
    open for review; approval, review verdict and merge eligibility are
    decided elsewhere and are untouched by this call.
    """
    return run(["gh", "pr", "ready", str(number), "--repo", repo], role=role)


def merge(repo: str, number: int, method: str = "squash",
          *, expected_head: str | None, head_branch: str | None = None,
          role: str | None = None) -> Result:
    """Merge a pull request, but ONLY if its head is still `expected_head`.

    THE EXPECTED HEAD IS NOT OPTIONAL — approval package §4's SHA-binding
    table and §5. Every other link in the chain (evidence, review, gate
    verdict, posted status) is bound to one commit; without `sha` the merge
    itself was the one link bound to a pull request NUMBER instead, so a
    push landing between the gate's verdict and this call merged a head
    nothing had judged. That window was closed only by the NEXT tick
    noticing `HEAD_SHA_CHANGED`, which is after the merge, not before it.

    REST rather than a `gh pr merge` flag, for the reason §5 records: a
    local hook on this host refuses all `gh pr` invocations including
    `--help`, so the flag could not be verified, and `PUT
    /repos/{owner}/{repo}/pulls/{n}/merge` with `sha` is documented.
    GitHub answers 409 when the head has moved, so the refusal is GitHub's,
    not ours.

    `--delete-branch` has no REST equivalent on that endpoint, so the ref
    is deleted by a second, BEST-EFFORT call: the merge has already
    happened and is irreversible, and a surviving branch is untidy rather
    than unsafe. Its result is deliberately not consulted.
    """
    if not expected_head:
        # NO FALLBACK TO AN UNBOUND MERGE. Refusing is the point.
        return Result(False, "", "no expected head: refusing to merge",
                      EXPECTED_HEAD_MISSING)
    result = run(["gh", "api", "--method", "PUT",
                  f"repos/{repo}/pulls/{number}/merge",
                  "-f", f"sha={expected_head}",
                  "-f", f"merge_method={method}"], role=role)
    if result.ok and head_branch:
        run(["gh", "api", "--method", "DELETE",
             f"repos/{repo}/git/refs/heads/{head_branch}"], role=role)
    return result


def create_pr(repo: str, head: str, base: str, title: str, body: str,
              *, role: str | None = None) -> Result:
    return run(["gh", "pr", "create", "--repo", repo, "--head", head, "--base", base,
                "--title", title, "--body", body], role=role)


def comment(repo: str, number: int, body: str, *, role: str | None = None) -> Result:
    return run(["gh", "pr", "comment", str(number), "--repo", repo,
                "--body", redact.scrub(body)], role=role)


def protection(repo: str, branch: str, *, role: str | None = None) -> dict | None:
    result = run(["gh", "api", f"repos/{repo}/branches/{branch}/protection"],
                 role=role)
    return result.json() if result.ok else None


def latest_run_conclusion(repo: str, branch: str,
                          *, role: str | None = None) -> tuple[str, str]:
    """(status, conclusion) of the most recent workflow run on a branch."""
    result = run(["gh", "run", "list", "--repo", repo, "--branch", branch,
                  "--limit", "1", "--json", "status,conclusion,name,databaseId"],
                 role=role)
    runs = result.json() or []
    if not runs:
        return "NONE", "NONE"
    return (runs[0].get("status") or "").upper(), (runs[0].get("conclusion") or "").upper()
