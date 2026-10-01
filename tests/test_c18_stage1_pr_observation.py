"""C-18 stage 1: route_prs's GitHub observation moves out of the transaction.

C-18's finding names `route_prs` explicitly: it called `gh.pr_view` while the
Supervisor held its one exclusive state transaction (T1), so a slow or hanging
GitHub call stalled every task in the run, and no `declare_busy` bound covered
it. The remediation shape is C-14.1's: observe with no lock held, pass an
immutable answer in, decide inside.

This file proves the move, and proves it did not buy the move with a weaker
guarantee:

  * the observation really is taken with the state lock free - established by
    probing the real `flock`, not by reading the source;
  * `route_prs` has no fallback fetch, so a missing observation defers rather
    than reaching for GitHub under the lock;
  * an observation is bound to one pull request and one full head SHA, so one
    PR's answer can never be routed onto another's record;
  * an answer is never carried across ticks - each tick observes for itself;
  * the merge path's own fresh observation, which is what protects an action
    against a head that moved, is untouched.

Everything runs against a real `state.Store` on a temporary path with every
GitHub edge mocked. Nothing launches a worker and nothing makes a paid call.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# This directory too, for the shared merge-evidence fixture.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mergeable_evidence import complete_evidence  # noqa: E402

from control import (  # noqa: E402
    clock,
    config,
    ledger as ledger_mod,
    providers,
    routing,
    state as state_mod,
    supervisor as supervisor_mod,
)

TZ = "Pacific/Auckland"
BRANCH = "task/task-001"
DIFF_HASH = "h" * 40
HEAD_SHA = "a" * 40
MERGED_SHA = "b" * 40


def lock_is_held(lock_path: Path) -> bool:
    """Whether Store.transaction's exclusive flock is held right now.

    flock conflicts between distinct open file descriptions even inside one
    process, so an independent open() plus LOCK_NB is a real test of the real
    lock - not a flag the code under test kindly sets for us.
    """
    if not lock_path.exists():
        return False
    with open(lock_path, "r+", encoding="utf-8") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def closed_view(number: int, *, state: str = "MERGED", head: str | None = HEAD_SHA,
                merge_commit: str | None = MERGED_SHA) -> dict:
    """Shaped like a real `gh pr view` response - PR_FIELDS asks for number
    and headRefOid, so a faithful stub carries both."""
    view = {"number": number, "state": state, "isDraft": False,
            "headRefName": BRANCH}
    if head is not None:
        view["headRefOid"] = head
    if merge_commit is not None:
        view["mergeCommit"] = {"oid": merge_commit}
    return view


def open_pr(number: int, branch: str) -> dict:
    return {
        "number": number, "state": "OPEN", "isDraft": False,
        "headRefName": branch, "headRefOid": HEAD_SHA,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        "mergeCommit": {"oid": MERGED_SHA},
    }


class ObservationHarness(unittest.TestCase):
    """A real Supervisor over a real Store, every outbound edge stubbed."""

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.sup.store = self.store
        self.heartbeat_path = self.root / "heartbeat.json"
        # C-14.2's external-merge branch reads durable merge evidence, so a
        # mocked ledger would make every closed-PR route classify against
        # MagicMocks. A real, empty ledger states the premise these tests
        # need explicitly: no local merge was ever claimed.
        self.sup.ledger = ledger_mod.Ledger(
            path=self.root / "ledger.jsonl", tz=TZ, experiment_id="run-002")

        self.events: list[tuple[str, dict]] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})
        self.sup.run_declared = mock.Mock()

    # ------------------------------------------------------------ fixtures

    def seed(self, *, numbers=(100,), state="REVIEW", merged=False,
             approved=False, write=True) -> dict:
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        for index, number in enumerate(numbers):
            task_id = f"TASK-{index + 1:03d}"
            branch = f"task/{task_id.lower()}"
            task = state_mod.add_task(doc, task_id, task_id, [], "feature",
                                      False, TZ)
            task.update({"state": state, "branch": branch, "pr": number,
                         "worker": None})
            # Production guarantees this entry for any task carrying a PR:
            # attach_pr assigns task["pr"] and transitions to PR_OPEN in one
            # committed operation, and C-14.2 draws its merge-window lower
            # bound from it. Without it every closed-PR route is UNPROVABLE
            # for a reason that has nothing to do with C-18.
            task["history"].append(
                {"at": clock.iso(clock.now(TZ)), "from": "ACTIVE",
                 "to": "PR_OPEN", "reason": f"PR #{number} opened"})
            record = routing.blank_pr_record(number, task_id, branch)
            record.update({"reviewed_head": HEAD_SHA,
                           "reviewed_diff_hash": DIFF_HASH,
                           "merged": merged})
            if approved:
                record.update({"review_verdict": routing.REVIEW_PASS,
                               "approval_current": True, "review_cycles": 1})
                # evaluate_merge requires the Protocol v2 evidence classes
                # for the observed head. Attached only on the approved
                # path, so an unapproved PR stays unmergeable for its own
                # reason rather than gaining evidence it never earned.
                record.update(complete_evidence(HEAD_SHA))
            doc["prs"][str(number)] = record
        if write:
            self.store._write(doc)
        return doc

    # ------------------------------------------------------------- driving

    def run_tick(self, *, open_prs=(), pr_view=None):
        """One real tick, recording whether the state lock was held at the
        moment of every `gh.pr_view` call."""
        self.lock_states: list[bool] = []

        def spy(repo, number):
            self.lock_states.append(lock_is_held(self.store.lock_path))
            return pr_view(repo, number) if callable(pr_view) else pr_view

        self.pr_view = mock.Mock(side_effect=spy)
        patches = [
            mock.patch.object(supervisor_mod.gh, "list_open_prs",
                              return_value=list(open_prs)),
            mock.patch.object(supervisor_mod.gh, "pr_view", self.pr_view),
            mock.patch.object(supervisor_mod.gh, "merge",
                              return_value=mock.Mock(ok=True, stderr="", stdout="")),
            mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                              return_value=DIFF_HASH),
            mock.patch.object(supervisor_mod.workers, "close_worker"),
            mock.patch.object(supervisor_mod.workers, "read_status", return_value=None),
            mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                              self.heartbeat_path),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self.sup.tick()

    # ------------------------------------------------------------- helpers

    def durable(self) -> dict:
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    def event(self, name: str) -> dict:
        found = [fields for event, fields in self.events if event == name]
        self.assertTrue(found, f"no {name} event in {self.event_names()}")
        return found[0]


# ------------------------------------------------- the boundary itself


class TheObservationIsTakenOutsideTheTransaction(ObservationHarness):

    def test_every_pr_view_in_a_tick_happens_with_the_state_lock_free(self):
        """The C-18 guarantee, measured against the real flock."""
        self.seed()
        self.run_tick(open_prs=(), pr_view=lambda repo, number: closed_view(number))

        self.assertTrue(self.pr_view.called,
                        "the test proves nothing if GitHub is never observed")
        self.assertEqual(self.lock_states, [False] * self.pr_view.call_count)

    def test_the_lock_probe_can_actually_detect_a_held_lock(self):
        """Without this, the test above could pass because the probe is broken."""
        self.seed()
        self.assertFalse(lock_is_held(self.store.lock_path))
        with self.store.transaction():
            self.assertTrue(lock_is_held(self.store.lock_path))
        self.assertFalse(lock_is_held(self.store.lock_path))

    def test_route_prs_makes_no_github_call_of_any_kind(self):
        doc = self.seed(write=False)
        observation = routing.ClosedPrObservation(
            pr_number=100, observed_at=clock.iso(clock.now(TZ)),
            view=closed_view(100))
        with mock.patch.object(supervisor_mod, "gh") as gh, \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            self.sup.route_prs(doc, None, [], {100: observation})
        self.assertEqual(gh.mock_calls, [])
        # and it still did the work the observation authorised
        self.assertTrue(doc["prs"]["100"]["merged"])

    def test_the_observation_is_taken_before_the_transaction_opens(self):
        """Ordering, not merely absence: the answer must already exist when
        T1 starts, or route_prs would have nothing to route on."""
        self.seed()
        order: list[str] = []
        original = self.store.transaction

        def watched():
            order.append("transaction")
            return original()

        with mock.patch.object(self.store, "transaction", side_effect=watched):
            self.run_tick(open_prs=(),
                          pr_view=lambda repo, number: (order.append("pr_view")
                                                        or closed_view(number)))
        self.assertTrue(order, "nothing was recorded")
        self.assertEqual(order[0], "pr_view")


# ------------------------------------------- missing and failed observations


class MissingAndFailedObservations(ObservationHarness):

    def test_a_missing_observation_defers_and_does_not_fetch(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod, "gh") as gh:
            candidates = self.sup.route_prs(doc, None, [], {})
        self.assertEqual(gh.mock_calls, [], "no fallback GitHub call inside T1")
        self.assertEqual(candidates, [])
        self.assertFalse(doc["prs"]["100"]["merged"])
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        deferred = self.event("PR_ROUTING_DEFERRED")
        self.assertEqual(deferred["outcome"], "OBSERVATION_MISSING")
        self.assertEqual(deferred["pr_id"], 100)

    def test_an_observation_for_a_different_pr_is_not_used(self):
        """Missing means missing. A map holding somebody else's answer must
        not become this pull request's answer."""
        doc = self.seed(numbers=(100, 101), write=False)
        other = routing.ClosedPrObservation(
            pr_number=101, observed_at=clock.iso(clock.now(TZ)),
            view=closed_view(101))
        with mock.patch.object(supervisor_mod, "gh"):
            self.sup.route_prs(doc, None, [], {101: other})
        self.assertFalse(doc["prs"]["100"]["merged"])
        self.assertTrue(doc["prs"]["101"]["merged"])
        deferred = [f for name, f in self.events if name == "PR_ROUTING_DEFERRED"]
        self.assertEqual([f["pr_id"] for f in deferred], [100])

    def test_a_failed_observation_is_present_not_absent(self):
        """gh answering nothing is a fact about GitHub; it is not the same as
        never having asked, and the two must not collapse into one value."""
        self.seed()
        with mock.patch.object(supervisor_mod.gh, "pr_view", return_value=None):
            observations = self.sup.observe_closed_prs(self.durable(), [])
        self.assertIn(100, observations)
        self.assertFalse(observations[100].observed)
        self.assertIsNone(observations[100].head_sha)

    def test_a_failed_observation_fabricates_no_merge(self):
        self.seed()
        self.run_tick(open_prs=(), pr_view=None)
        record = self.durable()["prs"]["100"]
        self.assertFalse(record["merged"])
        self.assertEqual(self.durable()["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertNotIn("MERGED", self.event_names())

    def test_an_inconsistent_observation_is_rejected_then_deferred(self):
        """A view carrying another pull request's number is dropped at the
        observation phase, and route_prs then has nothing and defers."""
        self.seed()
        with mock.patch.object(supervisor_mod.gh, "pr_view",
                               return_value=closed_view(999)):
            observations = self.sup.observe_closed_prs(self.durable(), [])
        self.assertEqual(observations, {})
        rejected = self.event("PR_OBSERVATION_REJECTED")
        self.assertEqual(rejected["outcome"], "OBSERVATION_INCONSISTENT")
        self.assertEqual(rejected["pr_id"], 100)

    def test_an_inconsistent_observation_never_merges_the_wrong_pr(self):
        self.seed()
        self.run_tick(open_prs=(), pr_view=closed_view(999))
        self.assertFalse(self.durable()["prs"]["100"]["merged"])
        self.assertEqual(self.durable()["tasks"]["TASK-001"]["state"], "REVIEW")


# ----------------------------------------------------- binding to PR and SHA


class ObservationsAreBoundToOnePrAndOneHead(unittest.TestCase):

    def observation(self, **over):
        fields = {"pr_number": 100, "observed_at": "2026-10-01T09:00:00+13:00",
                  "view": closed_view(100)}
        fields.update(over)
        return routing.ClosedPrObservation(**fields)

    def test_a_view_for_another_pull_request_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.observation(view=closed_view(101))
        self.assertIn("101", str(caught.exception))

    def test_a_view_with_no_number_is_refused(self):
        view = closed_view(100)
        view.pop("number")
        with self.assertRaises(ValueError):
            self.observation(view=view)

    def test_a_truncated_head_sha_is_refused(self):
        with self.assertRaises(ValueError):
            self.observation(view=closed_view(100, head="a" * 12))

    def test_a_non_hex_head_sha_is_refused(self):
        with self.assertRaises(ValueError):
            self.observation(view=closed_view(100, head="z" * 40))

    def test_a_full_head_sha_is_carried_intact(self):
        self.assertEqual(self.observation().head_sha, HEAD_SHA)

    def test_an_absent_head_is_none_not_invented(self):
        self.assertIsNone(self.observation(view=closed_view(100, head=None)).head_sha)

    def test_a_failed_observation_reports_unobserved(self):
        obs = self.observation(view=None)
        self.assertFalse(obs.observed)
        self.assertIsNone(obs.head_sha)

    def test_a_successful_observation_reports_observed(self):
        self.assertTrue(self.observation().observed)

    def test_the_view_cannot_be_mutated_by_the_transaction(self):
        obs = self.observation()
        with self.assertRaises(TypeError):
            obs.view["state"] = "OPEN"
        with self.assertRaises(TypeError):
            obs.view["mergeCommit"]["oid"] = "0" * 40

    def test_the_observation_itself_is_frozen(self):
        obs = self.observation()
        with self.assertRaises(Exception):
            obs.pr_number = 101

    def test_a_nonsense_pr_number_is_refused(self):
        for bad in (0, -1, True, "100", None):
            with self.assertRaises(ValueError):
                self.observation(pr_number=bad)

    def test_an_observation_records_when_it_was_taken(self):
        self.assertEqual(self.observation().observed_at,
                         "2026-10-01T09:00:00+13:00")


class EachPullRequestGetsItsOwnAnswer(ObservationHarness):

    def test_two_closed_prs_are_routed_on_their_own_observations(self):
        self.seed(numbers=(100, 101))
        answers = {100: closed_view(100, state="MERGED"),
                   101: closed_view(101, state="CLOSED", merge_commit=None)}
        self.run_tick(open_prs=(), pr_view=lambda repo, number: answers[number])

        durable = self.durable()
        self.assertTrue(durable["prs"]["100"]["merged"])
        self.assertEqual(durable["prs"]["100"]["merged_sha"], MERGED_SHA)
        self.assertFalse(durable["prs"]["101"]["merged"])
        self.assertEqual(durable["tasks"]["TASK-002"]["state"], "REVIEW")


class AnObservationIsNeverProofOfTheCurrentHead(ObservationHarness):

    def test_each_tick_observes_again_rather_than_reusing_the_last_answer(self):
        self.seed()
        self.run_tick(open_prs=(),
                      pr_view=lambda repo, number: closed_view(number, state="OPEN",
                                                               merge_commit=None))
        first = self.pr_view.call_count
        self.assertEqual(first, 1)
        self.assertFalse(self.durable()["prs"]["100"]["merged"])

        self.run_tick(open_prs=(),
                      pr_view=lambda repo, number: closed_view(number, state="MERGED"))
        self.assertEqual(self.pr_view.call_count, 1)   # a fresh call, this tick
        self.assertTrue(self.durable()["prs"]["100"]["merged"])

    def test_nothing_from_the_observation_is_written_into_durable_state(self):
        """The observation is a within-tick value. Persisting it would be the
        first step towards a later tick treating it as the current head."""
        self.seed()
        self.run_tick(open_prs=(),
                      pr_view=lambda repo, number: closed_view(number, state="OPEN",
                                                               merge_commit=None))
        blob = json.dumps(self.durable())
        self.assertNotIn("observed_at", blob)
        self.assertNotIn("ClosedPrObservation", blob)

    def test_the_merge_path_still_takes_its_own_fresh_observation(self):
        """Stage 1 must not have removed the downstream re-read that protects
        a merge against a head that moved after routing decided.

        That re-read is deliberately inside each merge's own short
        transaction (C-14.1), and C-18's row excludes the merge from its
        finding, so this asserts the re-read still happens - not that it
        happens lock-free.
        """
        self.seed(state="REVIEW", approved=True)
        self.run_tick(open_prs=(open_pr(100, BRANCH),),
                      pr_view=lambda repo, number: open_pr(number, BRANCH))
        # The PR is open, so the pre-lock closed-PR phase asks nothing at all;
        # every call here belongs to the merge path, after T1 committed.
        self.assertTrue(self.pr_view.called)
        self.assertEqual([c.args[1] for c in self.pr_view.call_args_list], [100, 100])

    def test_a_merge_is_never_authorised_by_the_routing_observation(self):
        """merge_no_longer_ready plus a fresh pr_view govern the merge. A
        routing observation reaching that decision would be exactly the
        'earlier observation treated as the current head' this forbids."""
        self.seed(approved=True)
        with mock.patch.object(supervisor_mod.gh, "pr_view",
                               return_value=None) as pr_view, \
                mock.patch.object(self.sup, "attempt_merge") as attempt:
            self.sup.execute_merges([("TASK-001", 100)])
        pr_view.assert_called_once_with(self.cfg.github_repo, 100)
        attempt.assert_not_called()
        blocked = self.event("MERGE_BLOCKED")
        self.assertEqual(blocked["metadata_redacted"]["reason"],
                         "pull request could not be observed")


# --------------------------------------------- existing behaviour preserved


class TheObservationPhaseAsksOnlyWhatRoutingWouldHaveAsked(ObservationHarness):

    def test_an_open_pull_request_is_not_observed(self):
        self.seed()
        with mock.patch.object(supervisor_mod.gh, "pr_view") as pr_view:
            observations = self.sup.observe_closed_prs(
                self.durable(), [open_pr(100, BRANCH)])
        pr_view.assert_not_called()
        self.assertEqual(observations, {})

    def test_an_already_merged_record_is_not_observed(self):
        self.seed(merged=True)
        with mock.patch.object(supervisor_mod.gh, "pr_view") as pr_view:
            self.assertEqual(self.sup.observe_closed_prs(self.durable(), []), {})
        pr_view.assert_not_called()

    def test_a_task_with_no_pull_request_is_not_observed(self):
        doc = state_mod.initial_document("run-002", "2.0")
        providers.ensure(doc)
        state_mod.add_task(doc, "TASK-001", "t", [], "feature", False, TZ)
        with mock.patch.object(supervisor_mod.gh, "pr_view") as pr_view:
            self.assertEqual(self.sup.observe_closed_prs(doc, []), {})
        pr_view.assert_not_called()

    def test_a_pull_request_with_no_record_is_not_observed(self):
        doc = self.seed(write=False)
        doc["prs"].pop("100")
        with mock.patch.object(supervisor_mod.gh, "pr_view") as pr_view:
            self.assertEqual(self.sup.observe_closed_prs(doc, []), {})
        pr_view.assert_not_called()

    def test_one_call_per_closed_pull_request_and_no_more(self):
        self.seed(numbers=(100, 101))
        with mock.patch.object(supervisor_mod.gh, "pr_view",
                               side_effect=lambda repo, n: closed_view(n)) as pr_view:
            observations = self.sup.observe_closed_prs(self.durable(), [])
        self.assertEqual(pr_view.call_count, 2)
        self.assertEqual(sorted(observations), [100, 101])
        self.assertEqual(observations[100].pr_number, 100)
        self.assertEqual(observations[101].pr_number, 101)


class RoutingAndMergeBoundaryBehaviourIsUnchanged(ObservationHarness):

    def test_an_approved_open_pr_is_still_a_merge_candidate(self):
        doc = self.seed(approved=True, write=False)
        candidates = self.sup.route_prs(doc, None, [open_pr(100, BRANCH)], {})
        self.assertEqual(candidates, [("TASK-001", 100)])

    def test_route_prs_still_returns_identifiers_not_documents(self):
        doc = self.seed(approved=True, write=False)
        for candidate in self.sup.route_prs(doc, None, [open_pr(100, BRANCH)], {}):
            for part in candidate:
                self.assertIsInstance(part, (str, int))

    def test_a_closed_pr_never_becomes_a_merge_candidate(self):
        doc = self.seed(approved=True, write=False)
        observation = routing.ClosedPrObservation(
            pr_number=100, observed_at=clock.iso(clock.now(TZ)),
            view=closed_view(100, state="OPEN", merge_commit=None))
        with mock.patch.object(supervisor_mod, "gh"):
            candidates = self.sup.route_prs(doc, None, [], {100: observation})
        self.assertEqual(candidates, [])

    def test_an_open_pr_awaiting_dispatch_still_routes(self):
        doc = self.seed(state="PR_OPEN", write=False)
        with mock.patch.object(self.sup, "dispatch_reviewer") as reviewer:
            self.sup.route_prs(doc, None, [open_pr(100, BRANCH)], {})
        reviewer.assert_called_once()

    def test_an_externally_merged_pr_still_completes_its_task(self):
        self.seed()
        self.run_tick(open_prs=(), pr_view=lambda repo, number: closed_view(number))
        durable = self.durable()
        self.assertEqual(durable["tasks"]["TASK-001"]["state"], "COMPLETE")
        self.assertTrue(durable["prs"]["100"]["merged"])
        self.assertEqual(durable["prs"]["100"]["merged_sha"], MERGED_SHA)
        self.assertTrue(durable["prs"]["100"]["merge_sha_observed"])


if __name__ == "__main__":
    unittest.main()
