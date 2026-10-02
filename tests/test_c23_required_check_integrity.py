"""C-23 — what is allowed to satisfy a REQUIRED check.

THE GOVERNING REQUIREMENT. `config/experiment.json` declares
`github.required_checks = ["ci"]`, and Protocol v2's §"PR contract" says
"CI validates the schema". `control/routing.py::evaluate_merge` - the sole
merge authority - asks `gh.checks_state(pr, required_checks)` whether that
requirement is met, and merges on the answer.

WHAT WAS REPRODUCED. Three things satisfied the requirement without CI
having run and passed:

  * a `ci` check run concluding SKIPPED
  * a `ci` check run concluding NEUTRAL
  * a commit STATUS whose context is "ci" - not a check run at all, and
    not produced by the workflow

`apparatus/adapters/ci-result.js` has always required `success` exactly on
the check-run surface. The Python path was the weaker of the two gates,
and these tests pin the rule it now applies.

WHAT IS STILL OPEN, AND THESE TESTS SAY SO RATHER THAN IMPLYING
OTHERWISE. Matching is by NAME. A workflow modified in a pull request's own
branch that still emits a check run named `ci` concluding success satisfies
this. Closing that needs workflow-content integrity or GitHub-side app
pinning, neither of which is a local change.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gh

REQUIRED = ("ci",)


def run(name="ci", conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name,
            "status": status, "conclusion": conclusion}


def status_context(context="ci", state="SUCCESS"):
    return {"__typename": "StatusContext", "context": context, "state": state}


def ask(*rollup):
    return gh.checks_state({"statusCheckRollup": list(rollup)}, REQUIRED)


class AGenuineGreenRunSatisfiesItCase(unittest.TestCase):
    """The control. A rule that refuses everything proves nothing."""

    def test_a_completed_successful_check_run_passes(self):
        ok, _ = ask(run())
        self.assertTrue(ok)

    def test_other_checks_alongside_it_do_not_interfere(self):
        ok, _ = ask(run(), run(name="lint", conclusion="FAILURE"),
                    status_context(context="coverage"))
        self.assertTrue(ok, "a non-required check changed the required answer")


class ASkippedCheckIsNotAPassCase(unittest.TestCase):
    """A required check that did not run is absent, not green."""

    def test_skipped_does_not_satisfy_a_required_check(self):
        ok, detail = ask(run(conclusion="SKIPPED"))
        self.assertFalse(ok, "a skipped ci job satisfied the merge gate")
        self.assertIn("not green", detail)

    def test_neutral_does_not_satisfy_a_required_check(self):
        ok, _ = ask(run(conclusion="NEUTRAL"))
        self.assertFalse(ok)

    def test_an_unconcluded_run_does_not_satisfy_it(self):
        ok, _ = ask(run(status="IN_PROGRESS", conclusion=None))
        self.assertFalse(ok)

    def test_every_non_success_conclusion_is_refused(self):
        for conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT",
                           "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE",
                           "", None):
            ok, _ = ask(run(conclusion=conclusion))
            self.assertFalse(ok, f"conclusion {conclusion!r} was accepted")


class AStatusIsNotACheckRunCase(unittest.TestCase):
    """The substitution that needed no workflow at all."""

    def test_a_commit_status_named_ci_does_not_satisfy_the_requirement(self):
        ok, detail = ask(status_context())
        self.assertFalse(ok, "a commit status satisfied a required CHECK")
        self.assertIn("CHECK RUN", detail)

    def test_a_status_cannot_stand_in_beside_a_failing_run(self):
        ok, _ = ask(run(conclusion="FAILURE"), status_context())
        self.assertFalse(ok)

    def test_a_genuine_run_without_a_typename_still_passes(self):
        """DETECTION IS STRUCTURAL, AND THIS IS WHY.

        An earlier version of this rule required `__typename == "CheckRun"`.
        No real `gh` call has ever been made from this repository, so
        whether `gh pr view --json statusCheckRollup` emits that field is
        UNVERIFIED here - and a gate that requires an unverified field
        denies every pull request on launch day. That is the shape of two
        defects already found in this arrangement.

        A CheckRun carries `conclusion`; a StatusContext carries `state`
        and has none. The kind is read from that, using only fields the
        function already read.
        """
        ok, _ = ask({"name": "ci", "status": "COMPLETED",
                     "conclusion": "SUCCESS"})
        self.assertTrue(ok, "a genuine green run was refused for lacking a "
                            "field this repository cannot prove exists")

    def test_a_status_without_a_typename_is_still_refused(self):
        """The substitution must not survive the field being absent."""
        ok, detail = ask({"context": "ci", "state": "SUCCESS"})
        self.assertFalse(ok)
        self.assertIn("CHECK RUN", detail)

    def test_a_shapeless_entry_fails_closed(self):
        """Neither conclusion nor status: nothing establishes it ran."""
        ok, detail = ask({"name": "ci"})
        self.assertFalse(ok, "an entry establishing nothing was accepted")
        self.assertIn("CHECK RUN", detail)


class WhatThisDoesNotCloseCase(unittest.TestCase):
    """Recorded so the gap is inherited, not rediscovered.

    Matching is by NAME. If a pull request's own branch redefines the
    workflow and still emits a run named `ci` that concludes success, this
    passes - and it is indistinguishable here from a genuine run.
    """

    def test_a_renamed_job_emitting_ci_still_satisfies_it(self):
        ok, _ = ask(run())
        self.assertTrue(
            ok,
            "this asserts the LIMIT, not the goal: nothing in checks_state "
            "reads the workflow definition, so a modified workflow that "
            "emits a green run named `ci` is accepted. Closing it needs "
            "workflow-content integrity or GitHub-side app pinning")

    def test_a_missing_required_check_is_still_refused(self):
        ok, detail = ask(run(name="build"))
        self.assertFalse(ok)
        self.assertIn("not reported", detail)


if __name__ == "__main__":
    unittest.main()
