"""The trusted-CI protected path set. APPROVED and IN FORCE (C-23a).

The operator's rule, verbatim:

    "Ordinary product PRs cannot change trusted CI or its validation
     machinery; those changes require a separately reviewed apparatus
     amendment."

This module is the vocabulary - which paths are protected, and why each
one is. The WIRING is `routing.ci_paths_clear_for_merge`, called from
`supervisor.attempt_merge` immediately before `evaluate_merge`; the
amendment escape hatch is `routing.authorised_ci_amendments`.
`tests/test_c23_ci_protected_paths.py` pins that it IS wired (it used to
pin the opposite, while the rule was still a proposal) and
`tests/test_c23_merge_path_protection.py` proves the merge path reaches
it.

THE GAP IT ADDRESSES, AND WHY APP PINNING IS NOT AN ANSWER TO IT.
C-23 closed two of three ways a product pull request could satisfy the
required `ci` check without CI passing. The third is a workflow MODIFIED
in the pull request's own branch that still emits a check run named `ci`
concluding success.

  * **Expected-App pinning** (`branch-protection-AFTER.json` pins the `ci`
    context to `app_id 15368`) answers a DIFFERENT threat: a check run
    produced by something that is not GitHub Actions. A modified workflow
    runs THROUGH GitHub Actions, so its check run carries app_id 15368
    legitimately and pinning accepts it. **App pinning does not mitigate a
    modified workflow at all**, and offering it as such was wrong.
  * **Workflow integrity** - this - is the only one of the two that
    addresses a modified workflow.

They are complementary and neither substitutes for the other.

WHAT IS PROTECTED, AND IT IS DERIVED FROM EXECUTION RATHER THAN TASTE.
Every entry below is something `.github/workflows/ci.yml` actually runs or
reads. `derivation_evidence()` states the step each comes from, and the
test re-reads `ci.yml` and fails if a step appears that no entry covers -
so a new CI step cannot silently widen what CI trusts without widening
this set too.

WHAT THIS IS NOT. It is **not a permanent ban on changing CI.** A change
to any protected path is permitted through an APPARATUS AMENDMENT that is
reviewed separately from the product task that wants it - the same shape
as the governed decisions already recorded in the contradiction audit. The
rule is "a product task may not change the thing that judges it in the
same breath", not "this may never change".

That amendment is a RESOLVED `HUMAN_APPARATUS_AUTHORISATION` intervention
bound to one head and one set of paths, corroborated by the append-only
ledger. It is NOT non-self-asserting: both records are written by the UID
that runs the workers and neither is hash-chained, so a sufficiently
determined worker can author its own authorisation. That is C-22, C-22 is
open, and `routing.authorised_ci_amendments` states the limit where the
next reader will meet it.
"""

from __future__ import annotations

# Path prefixes a product pull request may not modify without a separately
# reviewed apparatus amendment. Prefixes, because CI executes whole trees.
PROTECTED_PREFIXES: tuple[str, ...] = (
    ".github/workflows/",
    "apparatus/",
    "control/",
    "tests/",
    "scripts/",
    "protocol/",
)


def derivation_evidence() -> dict:
    """Which `ci.yml` step makes each prefix load-bearing.

    Kept as data rather than prose so the test can check it covers every
    entry, and so a reviewer can see the reason without reading the
    workflow.
    """
    return {
        ".github/workflows/": "the workflow definition itself - it decides "
                              "what every other step is",
        "apparatus/": "`npm test` (node --test over apparatus/**) and "
                      "`node apparatus/pr-evidence/validate.js`; "
                      "validate.js also requires severity-floor.js and "
                      "requirement-registry.js",
        "control/": "`python -m unittest discover -s tests` - the suite "
                    "imports the control plane it tests",
        "tests/": "the same step; the suite IS the assertions CI makes",
        "scripts/": "`python scripts/check_no_secrets.py`, the committed-"
                    "secret guardrail",
        "protocol/": "validate.js reads protocol/PR-EVIDENCE-V2.schema.json "
                     "as its schema",
    }


def touches_protected(changed_paths) -> tuple[bool, tuple[str, ...]]:
    """(touched, the offending paths). Pure; reads nothing.

    `changed_paths` are repository-relative, as `git diff --name-only`
    emits them. A non-string entry is treated as offending rather than
    skipped: a path this cannot read is a path it cannot clear, and the
    whole point is to fail closed.
    """
    offending = []
    for path in changed_paths or ():
        if not isinstance(path, str) or not path:
            offending.append(repr(path))
            continue
        # `removeprefix`, NOT `lstrip("./")`. lstrip takes a CHARACTER SET,
        # so it ate the leading dot of `.github/workflows/ci.yml` and the
        # workflow definition - the most important protected path there is -
        # matched nothing. Caught by the test that names it.
        normalised = path[2:] if path.startswith("./") else path
        if any(normalised.startswith(prefix) for prefix in PROTECTED_PREFIXES):
            offending.append(path)
    return bool(offending), tuple(sorted(offending))


def amendment_required_reason(changed_paths) -> str | None:
    """A finite diagnostic, or None when the change is ordinary product work.

    Deliberately names the remedy. A rule that only refuses teaches the
    next agent to work around it; one that says "this needs an apparatus
    amendment" teaches the process.
    """
    touched, offending = touches_protected(changed_paths)
    if not touched:
        return None
    return ("CI_TRUST_PATHS_MODIFIED: this pull request changes paths the "
            "trusted CI workflow executes - "
            + ", ".join(offending)
            + ". A product task may not change what judges it. Route this "
              "through a separately reviewed apparatus amendment.")
