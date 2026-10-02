"""GitHub operations through the `gh` CLI.

Thin, deterministic wrappers. The supervisor is the only component that calls
`merge`; Builders and Fixers have no merge path, and Codex authorises but does
not execute.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import redact

TIMEOUT = 120


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


def run(args: list[str], cwd: str | None = None, timeout: int = TIMEOUT) -> Result:
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return Result(False, "", f"timeout after {timeout}s", 124)
    except FileNotFoundError as exc:
        return Result(False, "", str(exc), 127)
    return Result(
        proc.returncode == 0,
        redact.scrub(proc.stdout.strip()),
        redact.scrub(proc.stderr.strip()),
        proc.returncode,
    )


def git(args: list[str], cwd: str) -> Result:
    return run(["git", *args], cwd=cwd)


def repo_exists(repo: str) -> bool:
    return run(["gh", "repo", "view", repo, "--json", "name"]).ok


def default_branch(repo: str) -> str | None:
    result = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"])
    data = result.json() or {}
    return (data.get("defaultBranchRef") or {}).get("name")


PR_FIELDS = (
    "number,state,isDraft,title,headRefName,headRefOid,baseRefName,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,url,labels,author,mergeCommit"
)


def list_open_prs(repo: str) -> list[dict]:
    result = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                  "--json", PR_FIELDS, "--limit", "50"])
    return result.json() or []


def pr_view(repo: str, number: int) -> dict | None:
    result = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])
    return result.json()


def pr_diff_sha(repo: str, number: int) -> str | None:
    """The head commit of the PR - the identity of the material diff under review."""
    data = pr_view(repo, number) or {}
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


def update_branch(repo: str, number: int) -> Result:
    """Reconcile the PR branch with current main."""
    return run(["gh", "pr", "update-branch", str(number), "--repo", repo])


def mark_ready(repo: str, number: int) -> Result:
    """Take a draft pull request out of draft.

    Grants nothing. It changes only whether GitHub considers the pull request
    open for review; approval, review verdict and merge eligibility are
    decided elsewhere and are untouched by this call.
    """
    return run(["gh", "pr", "ready", str(number), "--repo", repo])


def merge(repo: str, number: int, method: str = "squash") -> Result:
    return run(["gh", "pr", "merge", str(number), "--repo", repo, f"--{method}",
                "--delete-branch"])


def create_pr(repo: str, head: str, base: str, title: str, body: str) -> Result:
    return run(["gh", "pr", "create", "--repo", repo, "--head", head, "--base", base,
                "--title", title, "--body", body])


def comment(repo: str, number: int, body: str) -> Result:
    return run(["gh", "pr", "comment", str(number), "--repo", repo,
                "--body", redact.scrub(body)])


def protection(repo: str, branch: str) -> dict | None:
    result = run(["gh", "api", f"repos/{repo}/branches/{branch}/protection"])
    return result.json() if result.ok else None


def latest_run_conclusion(repo: str, branch: str) -> tuple[str, str]:
    """(status, conclusion) of the most recent workflow run on a branch."""
    result = run(["gh", "run", "list", "--repo", repo, "--branch", branch,
                  "--limit", "1", "--json", "status,conclusion,name,databaseId"])
    runs = result.json() or []
    if not runs:
        return "NONE", "NONE"
    return (runs[0].get("status") or "").upper(), (runs[0].get("conclusion") or "").upper()
