"""C-14.1: the merge commit boundary.

C-14's root defect is that `gh.merge()` - irreversible and external - runs
inside `tick()`'s single late-committing `Store.transaction()`, while the
ledger event asserting the merge is already durable. Any later exception in
unrelated tick work rolls the state document back, leaving GitHub merged and
`state.json` claiming the task never was.

These tests use a real `state.Store` on a temporary path, so a rollback is a
genuine rollback and not a simulation. Every GitHub edge is a mock - no
GitHub side effect is manufactured anywhere in this file.

C-14.2 (three-source detection and annunciation) is deliberately NOT covered
here: these tests say nothing about how a divergence is discovered, only that
C-14.1 stops the control plane creating one.
"""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import (  # noqa: E402
    clock,
    config,
    debt,
    routing,
    state as state_mod,
    supervisor as supervisor_mod,
)

TZ = "Pacific/Auckland"
BRANCH_FMT = "task/{0}"
DIFF_HASH = "h" * 40
MERGED_SHA = "abc123def456abc123def456abc123def456abcd"


class InjectedFailure(RuntimeError):
    """Deliberate fault, injected into ordinary tick work after route_prs."""


def _open_pr(number: int, branch: str) -> dict:
    """Shaped exactly like a gh.list_open_prs / gh.pr_view entry (same
    PR_FIELDS), and carrying a mergeCommit so the post-merge observation in
    attempt_merge finds a SHA without needing a second, different response."""
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "headRefName": branch,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
        "mergeCommit": {"oid": MERGED_SHA},
    }


def _accepted_finding(fid: str = "F1") -> dict:
    return {"id": fid, "severity": "P2", "category": "TESTS",
            "summary": "non-blocking"}


class MergeBoundaryCase(unittest.TestCase):
    """A real Supervisor against a real Store, with every outbound edge
    stubbed. Mirrors the harness in tests/test_debt.py and
    tests/test_dispatch_invariant.py, plus a genuine state file."""

    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = state_mod.Store(path=root / "state.json", tz=TZ)
        self.sup.store = self.store
        self.heartbeat_path = root / "heartbeat.json"

        self.events: list[tuple[str, dict]] = []
        self.sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        self.sup.notify_out = mock.Mock(return_value={"ok": True})
        # Jev and the Observer are slow provider work that runs after the tick
        # transaction; they are not part of this boundary.
        self.sup.run_declared = mock.Mock()

    # ------------------------------------------------------------- fixtures

    def seed(self, task_ids=("TASK-001",), *, accepted=True):
        """A document whose named tasks are each merge-ready: REVIEW, a
        current Codex REVIEW_PASS, and a reviewed diff hash that still
        matches. started_at is a value in this temporary file only - it does
        not touch .runtime/state.json and does not start the experiment."""
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        from control import providers
        providers.ensure(doc)
        for index, task_id in enumerate(task_ids):
            number = 100 + index
            branch = BRANCH_FMT.format(task_id.lower())
            task = state_mod.add_task(doc, task_id, task_id, [], "feature", False, TZ)
            task.update({"state": "REVIEW", "branch": branch, "pr": number,
                         "worker": None})
            record = routing.blank_pr_record(number, task_id, branch)
            record.update({"review_verdict": routing.REVIEW_PASS,
                           "approval_current": True,
                           "reviewed_diff_hash": DIFF_HASH,
                           "reviewed_head": "0" * 40,
                           "review_cycles": 1})
            if accepted:
                record["accepted_findings"] = [_accepted_finding()]
            doc["prs"][str(number)] = record
        self.store._write(doc)
        return doc

    def pr_numbers(self, task_ids):
        return [100 + i for i, _ in enumerate(task_ids)]

    def run_tick(self, task_ids=("TASK-001",), *, merge_ok=True,
                 fail_in=None, pr_view=mock.sentinel.default,
                 merge_side_effect=None, between_transactions=None,
                 open_prs=mock.sentinel.default):
        """Drive one real tick. `fail_in` names a Supervisor method patched to
        raise InjectedFailure. `between_transactions` runs after T1's block
        closes, to mutate durable state before T2 revalidates."""
        prs = ([_open_pr(n, BRANCH_FMT.format(t.lower()))
                for n, t in zip(self.pr_numbers(task_ids), task_ids)]
               if open_prs is mock.sentinel.default else open_prs)
        if pr_view is mock.sentinel.default:
            # Faithful to gh.pr_view: the observation is for the number asked
            # for. attempt_merge derives its PR number from this dict, so a
            # stub that ignores the argument would merge the wrong PR.
            by_number = {pr["number"]: pr for pr in prs}
            view = lambda repo, number: by_number.get(number)  # noqa: E731
        else:
            view = pr_view

        self.gh_merge = mock.Mock(
            side_effect=merge_side_effect,
            return_value=mock.Mock(ok=merge_ok, stderr="", stdout=""))
        self.pr_view = mock.Mock(side_effect=(
            view if callable(view) else lambda *a, **k: view))
        self.evaluate = mock.Mock(side_effect=routing.evaluate_merge)

        patches = [
            mock.patch.object(supervisor_mod.gh, "list_open_prs", return_value=prs),
            mock.patch.object(supervisor_mod.gh, "merge", self.gh_merge),
            mock.patch.object(supervisor_mod.gh, "pr_view", self.pr_view),
            mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                              return_value=DIFF_HASH),
            mock.patch.object(supervisor_mod.routing, "evaluate_merge", self.evaluate),
            mock.patch.object(supervisor_mod.workers, "close_worker"),
            mock.patch.object(supervisor_mod.workers, "read_status", return_value=None),
            mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                              self.heartbeat_path),
        ]
        if fail_in:
            patches.append(mock.patch.object(
                self.sup, fail_in, side_effect=InjectedFailure(fail_in)))
        if between_transactions is not None:
            original = self.store.transaction
            calls = {"n": 0}

            def counting_transaction():
                calls["n"] += 1
                if calls["n"] == 2:      # after T1 closed, before T2 opens
                    between_transactions()
                return original()

            patches.append(mock.patch.object(self.store, "transaction",
                                             side_effect=counting_transaction))

        with self._nest(patches):
            self.sup.tick()

    @staticmethod
    def _nest(patches):
        ctx = contextlib.ExitStack()
        for patch in patches:
            ctx.enter_context(patch)
        return ctx

    # ------------------------------------------------------------- helpers

    def durable(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def task_state(self, task_id="TASK-001", doc=None):
        return (doc or self.durable())["tasks"][task_id]["state"]

    def record(self, number=100, doc=None):
        return (doc or self.durable())["prs"][str(number)]

    def event_types(self):
        return [name for name, _ in self.events]

    def merged_numbers(self):
        """PR numbers gh.merge was called for, in call order."""
        return [call.args[1] for call in self.gh_merge.call_args_list]


# ------------------------------------------------------ the C-14.1 defect


class TestLaterTickFailureCannotStrandAMerge(MergeBoundaryCase):

    def test_later_t1_failure_prevents_the_merge_entirely(self):
        """THE C-14.1 REGRESSION PROOF.

        A merge-ready PR, and an exception injected into detect_stale - which
        runs after route_prs inside the same transaction.

        Before C-14.1: gh.merge() runs, the ledger's MERGED event is durable,
        then detect_stale raises, the transaction rolls back, and durable
        state still says the task is in REVIEW and the PR is unmerged. The
        external merge happened and the local record of it was lost.

        After C-14.1: detect_stale raises inside T1, T1 never commits, so T2
        never begins and gh.merge() is never called. Nothing is lost because
        nothing irreversible was done.
        """
        self.seed()
        with self.assertRaises(InjectedFailure):
            self.run_tick(fail_in="detect_stale")

        merged_in_state = bool(self.record().get("merged"))
        state_now = self.task_state()

        self.assertFalse(
            self.gh_merge.called and not merged_in_state,
            f"C-14.1 DEFECT REPRODUCED: gh.merge was called "
            f"({self.merged_numbers()}) but durable state has merged="
            f"{merged_in_state!r} and task state {state_now!r}. The external "
            f"merge happened and the local merged/debt/completion state was "
            f"rolled back by the later failure.",
        )
        self.gh_merge.assert_not_called()
        self.assertEqual(state_now, "REVIEW")
        self.assertFalse(merged_in_state)
        self.assertEqual(self.durable()["debt"], {})

    def test_failure_after_t2_cannot_roll_back_a_committed_merge(self):
        """The complementary half: once T2 has committed, later work outside
        it cannot undo the merge, the debt or the completion."""
        self.seed()
        self.sup.run_declared = mock.Mock(side_effect=InjectedFailure("jev"))
        with self.assertRaises(InjectedFailure):
            self.run_tick()

        self.gh_merge.assert_called_once()
        doc = self.durable()
        self.assertEqual(self.task_state(doc=doc), "COMPLETE")
        record = self.record(doc=doc)
        self.assertTrue(record["merged"])
        self.assertEqual(record["merged_sha"], MERGED_SHA)
        self.assertEqual(record["debt_recording_status"], debt.RECORDED)
        self.assertEqual(len(doc["debt"]), 1)


class TestCandidatesCommitIndependently(MergeBoundaryCase):

    def test_first_candidate_survives_a_second_candidate_failing(self):
        tasks = ("TASK-001", "TASK-002")
        self.seed(tasks)

        def merge(repo, number, *a, **k):
            if number == 101:
                raise InjectedFailure("second candidate")
            return mock.Mock(ok=True, stderr="", stdout="")

        with self.assertRaises(InjectedFailure):
            self.run_tick(tasks, merge_side_effect=merge)

        doc = self.durable()
        self.assertEqual(self.task_state("TASK-001", doc=doc), "COMPLETE")
        self.assertTrue(self.record(100, doc=doc)["merged"])
        self.assertEqual(self.task_state("TASK-002", doc=doc), "REVIEW")
        self.assertFalse(self.record(101, doc=doc).get("merged"))


class TestRevalidationInsideT2(MergeBoundaryCase):

    def test_candidate_invalidated_between_t1_and_t2_is_not_merged(self):
        self.seed()

        def revoke():
            with self.store.transaction() as doc:
                doc["prs"]["100"]["approval_current"] = False

        self.run_tick(between_transactions=revoke)

        self.gh_merge.assert_not_called()
        self.assertEqual(self.task_state(), "REVIEW")
        self.assertIn("MERGE_BLOCKED", self.event_types())
        # T2's own revalidation must refuse this, not the merge gate further
        # in: evaluate_merge also rejects a stale approval, which would mask a
        # missing revalidation. Refusing before the PR is even observed is the
        # only evidence that the revalidation itself did the work.
        self.pr_view.assert_not_called()

    def test_task_frozen_between_t1_and_t2_is_not_merged(self):
        self.seed()

        def freeze():
            with self.store.transaction() as doc:
                state_mod.transition(doc, "TASK-001", "FROZEN", "watchdog", TZ)

        self.run_tick(between_transactions=freeze)

        self.gh_merge.assert_not_called()
        self.assertEqual(self.task_state(), "FROZEN")
        self.assertIn("MERGE_BLOCKED", self.event_types())
        # evaluate_merge does not look at task state at all, so only T2's
        # revalidation can catch a Watchdog freeze landing in the gap.
        self.pr_view.assert_not_called()

    def test_verdict_cleared_between_t1_and_t2_is_not_merged(self):
        self.seed()

        def clear_verdict():
            with self.store.transaction() as doc:
                doc["prs"]["100"]["review_verdict"] = None

        self.run_tick(between_transactions=clear_verdict)

        self.gh_merge.assert_not_called()
        self.assertIn("MERGE_BLOCKED", self.event_types())
        self.pr_view.assert_not_called()

    def test_unobservable_pull_request_is_not_merged(self):
        self.seed()
        self.run_tick(pr_view=None)

        self.gh_merge.assert_not_called()
        self.assertEqual(self.task_state(), "REVIEW")
        self.assertIn("MERGE_BLOCKED", self.event_types())

    def test_t2_evaluates_the_fresh_observation_not_the_t1_snapshot(self):
        """T1 sees a CLEAN PR; the fresh T2 observation says BLOCKED. The
        merge gate must judge the fresh one."""
        self.seed()
        stale = _open_pr(100, BRANCH_FMT.format("task-001"))
        fresh = dict(stale, mergeStateStatus="BLOCKED")
        self.run_tick(open_prs=[stale], pr_view=fresh)

        self.gh_merge.assert_not_called()
        judged = self.evaluate.call_args.args[0]
        self.assertEqual(judged["mergeStateStatus"], "BLOCKED")
        self.assertIsNot(judged, stale)


class TestDeterministicOrdering(MergeBoundaryCase):

    def test_candidates_merge_in_task_id_order(self):
        tasks = ("TASK-003", "TASK-001", "TASK-002")
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        from control import providers
        providers.ensure(doc)
        numbers = {}
        for index, task_id in enumerate(tasks):        # inserted out of order
            number = 100 + index
            numbers[task_id] = number
            branch = BRANCH_FMT.format(task_id.lower())
            task = state_mod.add_task(doc, task_id, task_id, [], "feature", False, TZ)
            task.update({"state": "REVIEW", "branch": branch, "pr": number,
                         "worker": None})
            record = routing.blank_pr_record(number, task_id, branch)
            record.update({"review_verdict": routing.REVIEW_PASS,
                           "approval_current": True,
                           "reviewed_diff_hash": DIFF_HASH,
                           "review_cycles": 1, "accepted_findings": []})
            doc["prs"][str(number)] = record
        self.store._write(doc)

        prs = [_open_pr(numbers[t], BRANCH_FMT.format(t.lower())) for t in tasks]
        self.run_tick(tasks, open_prs=prs,
                      pr_view=lambda repo, number: _open_pr(number, "b"))

        self.assertEqual(
            self.merged_numbers(),
            [numbers["TASK-001"], numbers["TASK-002"], numbers["TASK-003"]])


class TestRoutePrsQueuesButNeverMerges(MergeBoundaryCase):

    def test_route_prs_returns_candidates_and_does_not_merge(self):
        doc = self.seed(("TASK-001", "TASK-002"))
        with mock.patch.object(supervisor_mod.gh, "merge") as merge:
            candidates = self.sup.route_prs(doc, None, [
                _open_pr(100, BRANCH_FMT.format("task-001")),
                _open_pr(101, BRANCH_FMT.format("task-002")),
            ])
        merge.assert_not_called()
        self.assertEqual(candidates, [("TASK-001", 100), ("TASK-002", 101)])

    def test_candidates_are_identifiers_not_live_document_objects(self):
        doc = self.seed()
        candidates = self.sup.route_prs(
            doc, None, [_open_pr(100, BRANCH_FMT.format("task-001"))])
        for candidate in candidates:
            for part in candidate:
                self.assertIsInstance(part, (str, int))
                self.assertNotIsInstance(part, dict)

    def test_a_pr_without_a_current_pass_is_not_queued(self):
        doc = self.seed()
        doc["prs"]["100"]["approval_current"] = False
        with mock.patch.object(self.sup, "route_awaiting_dispatch"):
            candidates = self.sup.route_prs(
                doc, None, [_open_pr(100, BRANCH_FMT.format("task-001"))])
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
