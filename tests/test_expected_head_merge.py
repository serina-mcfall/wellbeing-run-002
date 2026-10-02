"""The merge call carries the head the gate judged.

THE DEFECT THIS PINS. Approval package §4's SHA-binding table and §5 both
state that the merge is bound to one commit: `PUT
/repos/{owner}/{repo}/pulls/{n}/merge` with the `sha` parameter, which
GitHub refuses if the head has moved. `control/gh.py::merge` ran `gh pr
merge <n> --repo <r> --squash --delete-branch` instead, with no head
anywhere in it. Every other link in the chain - evidence, review verdict,
gate verdict, posted status - was bound to a commit; the merge itself was
bound to a pull request NUMBER.

WHAT THAT COST. A push landing between `evaluate_merge` returning ELIGIBLE
and `gh.merge` running merged a head nothing had judged. The window was
narrow and the NEXT tick noticed it as `HEAD_SHA_CHANGED` - but that is
after the merge, and a merge is irreversible. The document described a
protection the code did not implement.

WHAT THESE TESTS PROVE, AND THE LIMIT OF IT.

  COMPONENT IMPLEMENTED: the arguments handed to `gh` carry the expected
  head, the endpoint is the documented one, and a merge with no expected
  head is refused here rather than sent.

  CONNECTED PATH, SIMULATED SERVICES: a real Supervisor, a real Store and
  a real Ledger drive a real tick to a real `attempt_merge`, and the head
  that reaches the merge call is the one GitHub's observation carried.

  NOT A DEPLOYED PATH. That GitHub answers 409 when `sha` does not match
  the head is GitHub's documented behaviour and is NOT asserted here. No
  network call is made by this file. The first real exercise is Stage 1 on
  the throwaway repository.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The module, NOT the class: importing the harness class by name would make
# unittest collect every one of its subclasses' tests a second time under
# this module.
import test_merge_boundary as boundary  # noqa: E402

from control import gh  # noqa: E402

REPO = "serina-mcfall/wellbeing-run-002"
HEAD = "f" * 40
OTHER_HEAD = "e" * 40
BRANCH = "task/task-001"


def _ok(*_args, **_kwargs):
    return gh.Result(True, "", "", 0)


class MergeCarriesTheExpectedHeadCase(unittest.TestCase):
    """`gh.merge`'s arguments, with the process edge stubbed."""

    def merge(self, *, returns=_ok, **kwargs):
        self.calls: list[list[str]] = []

        def record(args, *a, **k):
            self.calls.append(list(args))
            return returns(args, *a, **k)

        with mock.patch.object(gh, "run", side_effect=record):
            return gh.merge(REPO, 7, **kwargs)

    def test_the_expected_head_travels_as_the_sha_parameter(self):
        self.merge(expected_head=HEAD)
        self.assertIn(f"sha={HEAD}", self.calls[0])

    def test_the_endpoint_is_the_documented_rest_merge(self):
        self.merge(expected_head=HEAD)
        self.assertEqual(self.calls[0][:4],
                         ["gh", "api", "--method", "PUT"])
        self.assertIn(f"repos/{REPO}/pulls/7/merge", self.calls[0])

    def test_the_unbound_gh_pr_merge_form_is_gone(self):
        """The literal shape of the defect, named so a revert is visible."""
        self.merge(expected_head=HEAD)
        self.assertNotEqual(self.calls[0][:3], ["gh", "pr", "merge"])

    def test_the_method_is_still_carried(self):
        self.merge(expected_head=HEAD, method="rebase")
        self.assertIn("merge_method=rebase", self.calls[0])

    def test_an_expected_head_must_be_supplied_at_every_call_site(self):
        """REQUIRED KEYWORD. A future call site that forgets it fails loudly
        at the call rather than quietly merging an unjudged head."""
        with self.assertRaises(TypeError):
            gh.merge(REPO, 7)

    def test_a_missing_expected_head_refuses_without_asking_github(self):
        result = self.merge(expected_head=None)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, gh.EXPECTED_HEAD_MISSING)
        self.assertEqual(self.calls, [], "no process may start")

    def test_an_empty_expected_head_refuses_the_same_way(self):
        result = self.merge(expected_head="")
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    def test_the_head_ref_is_deleted_after_a_successful_merge(self):
        """`--delete-branch` has no equivalent on the REST merge endpoint, so
        the ref deletion is a second call. Best-effort by design: the merge
        has already happened and cannot be undone by refusing this."""
        self.merge(expected_head=HEAD, head_branch=BRANCH)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(f"repos/{REPO}/git/refs/heads/{BRANCH}", self.calls[1])
        self.assertIn("DELETE", self.calls[1])

    def test_a_refused_merge_deletes_nothing(self):
        """A head that moved is a 409. Deleting the branch after that would
        destroy the work the refusal just protected."""
        result = self.merge(expected_head=HEAD, head_branch=BRANCH,
                            returns=lambda *a, **k: gh.Result(False, "", "409", 1))
        self.assertFalse(result.ok)
        self.assertEqual(len(self.calls), 1)

    def test_the_merge_result_is_what_is_returned_not_the_deletion(self):
        """A failed ref deletion must not be reported as a failed merge: the
        merge is durable at GitHub either way, and saying otherwise would
        strand state exactly as C-14 describes."""
        outcomes = [gh.Result(True, "", "", 0), gh.Result(False, "", "no ref", 1)]
        result = self.merge(expected_head=HEAD, head_branch=BRANCH,
                            returns=lambda *a, **k: outcomes.pop(0))
        self.assertTrue(result.ok)


class SupervisorBindsTheMergeToTheObservedHeadCase(boundary.MergeBoundaryCase):
    """A real tick through `attempt_merge`, on the C-14.1 harness."""

    def merge_kwargs(self):
        self.gh_merge.assert_called_once()
        return self.gh_merge.call_args.kwargs

    def test_the_head_github_reported_is_the_head_the_merge_is_bound_to(self):
        self.seed()
        self.run_tick()
        self.assertEqual(self.merge_kwargs()["expected_head"],
                         boundary.REVIEWED_HEAD)

    def test_the_observed_branch_is_carried_for_the_ref_deletion(self):
        self.seed()
        self.run_tick()
        self.assertEqual(self.merge_kwargs()["head_branch"],
                         boundary.BRANCH_FMT.format("task-001"))

    def test_the_bound_head_comes_from_the_observation_not_the_record(self):
        """`record["reviewed_head"]` lives in `.runtime/state.json`, which
        every worker can write while they share one UID (C-22). The merge is
        bound to `pr["headRefOid"]` - GitHub's answer, taken under this
        transaction's lock - so rewriting the record cannot rebind it.

        Here the record is forged to a head the pull request does not carry.
        `evaluate_merge` refuses it, which is the outer protection, and the
        merge is never reached: the forged value never becomes a `sha`."""
        doc = self.seed()
        doc["prs"]["100"]["reviewed_head"] = OTHER_HEAD
        self.store._write(doc)
        self.run_tick()
        self.gh_merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
