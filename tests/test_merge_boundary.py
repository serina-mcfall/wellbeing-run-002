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
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# This directory too, for the shared merge-evidence fixture.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mergeable_evidence import complete_evidence  # noqa: E402

from control import (  # noqa: E402
    clock,
    config,
    debt,
    routing,
    state as state_mod,
    supervisor as supervisor_mod,
)
import declared_phases  # noqa: E402

TZ = "Pacific/Auckland"
BRANCH_FMT = "task/{0}"
DIFF_HASH = "h" * 40
MERGED_SHA = "abc123def456abc123def456abc123def456abcd"
# The head the reviewer was dispatched against, and the head the pull request
# still carries. evaluate_merge requires the two to be equal.
REVIEWED_HEAD = "0" * 40


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
        # evaluate_merge compares this against record["reviewed_head"], which
        # every record fixture below sets to the same value.
        "headRefOid": REVIEWED_HEAD,
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
        self.sup.run_declared = mock.Mock(
            side_effect=declared_phases.provider_phases_suppressed)

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
                           "reviewed_head": REVIEWED_HEAD,
                           "review_cycles": 1})
            # evaluate_merge requires the Protocol v2 evidence classes for
            # the observed head. These tests are about the merge TRANSACTION
            # boundary, so the evidence must pass or no candidate would ever
            # reach the boundary being tested.
            record.update(complete_evidence(REVIEWED_HEAD, task_id.lower()))
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
        self.sup.run_declared = mock.Mock(
            side_effect=declared_phases.provider_phase_raises(
                InjectedFailure("jev")))
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
                           "reviewed_head": REVIEWED_HEAD,
                           "review_cycles": 1, "accepted_findings": []})
            record.update(complete_evidence(REVIEWED_HEAD, task_id.lower()))
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
            ], {})
        merge.assert_not_called()
        self.assertEqual(candidates, [("TASK-001", 100), ("TASK-002", 101)])

    def test_candidates_are_identifiers_not_live_document_objects(self):
        doc = self.seed()
        candidates = self.sup.route_prs(
            doc, None, [_open_pr(100, BRANCH_FMT.format("task-001"))], {})
        for candidate in candidates:
            for part in candidate:
                self.assertIsInstance(part, (str, int))
                self.assertNotIsInstance(part, dict)

    def test_a_pr_without_a_current_pass_is_not_queued(self):
        doc = self.seed()
        doc["prs"]["100"]["approval_current"] = False
        with mock.patch.object(self.sup, "route_awaiting_dispatch"):
            candidates = self.sup.route_prs(
                doc, None, [_open_pr(100, BRANCH_FMT.format("task-001"))], {})
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()


# ====================================================================== C-14.2
# Behavioural fail-before/pass-after coverage for three-source detection.
# These assert observable outcomes - task state, ledger events, notifications -
# and deliberately do NOT import control.merge_invariant, so a red run proves
# the missing BEHAVIOUR rather than a missing module name.


GUARDRAIL_RED = "GUARDRAIL_RED"
STATE_INVARIANT_VIOLATION = "STATE_INVARIANT_VIOLATION"


class InvariantDetectionCase(MergeBoundaryCase):
    """A real Supervisor with a REAL ledger on disk, so the control plane can
    read its own durable claims back the way C-14.2 requires."""

    def setUp(self):
        super().setUp()
        from control import ledger as ledger_mod
        self.ledger_path = Path(self.tmp.name) / "ledger.jsonl"
        self.sup.ledger = ledger_mod.Ledger(path=self.ledger_path, tz=TZ,
                                            experiment_id="run-002")
        del self.sup.log                      # restore the real, appending log()
        self.notifier = mock.Mock()
        self.notifier.send = mock.Mock(return_value={"ok": True})
        self.sup.notifier = self.notifier
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    # ------------------------------------------------------------- fixtures

    def merged_state(self, *, merged=False, task_state="REVIEW", debt=None,
                     last_review_at=mock.sentinel.default, history_pr_open=True):
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        from control import providers
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": task_state, "branch": "task/task-001", "pr": 100,
                     "worker": None})
        if history_pr_open:
            task["history"].append({"at": clock.iso(clock.now(TZ) - timedelta(hours=2)),
                                    "from": "ACTIVE", "to": "PR_OPEN", "reason": "opened"})
        record = routing.blank_pr_record(100, "TASK-001", "task/task-001")
        record.update({"review_verdict": routing.REVIEW_PASS, "approval_current": True,
                       "reviewed_diff_hash": DIFF_HASH, "merged": merged,
                       "reviewed_head": REVIEWED_HEAD,
                       "review_cycles": 1})
        record.update(complete_evidence(REVIEWED_HEAD))
        record["last_review_at"] = (clock.iso(clock.now(TZ) - timedelta(hours=1))
                                    if last_review_at is mock.sentinel.default
                                    else last_review_at)
        doc["prs"]["100"] = record
        if debt:
            doc["debt"] = dict(debt)
        self.store._write(doc)
        return doc

    def local_merged_event(self, *, debt_ids=(), status="NOT_REQUIRED",
                           detected_externally=mock.sentinel.absent):
        meta = {"reviewed_head": "0" * 40, "merged_sha": MERGED_SHA,
                "merge_sha_observed": True, "debt_recording_status": status,
                "debt_ids": list(debt_ids)}
        if detected_externally is not mock.sentinel.absent:
            meta["detected_externally"] = detected_externally
        self.sup.ledger.append("MERGED", task_id="TASK-001", pr_id=100,
                               branch="task/task-001", outcome="MERGED",
                               activity_class="ORCHESTRATION", metadata_redacted=meta)

    def blocked_merged_event(self, *, hours_ago):
        """A GUARDRAIL_RED substitution, stamped explicitly so the window can
        be exercised on both sides. Written raw because Ledger.append stamps
        its own timestamp."""
        event = {"timestamp": clock.iso(clock.now(TZ) - timedelta(hours=hours_ago)),
                 "experiment_id": "run-002", "event_type": GUARDRAIL_RED,
                 "guardrail": "SECRET_IN_LEDGER_EVENT_BLOCKED", "outcome": "BLOCKED",
                 "metadata_redacted": {"blocked_event_type": "MERGED"}}
        with open(self.ledger_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def run_external_branch(self, doc, *, pr_state="MERGED"):
        view = None if pr_state is None else {
            "number": 100, "state": pr_state, "isDraft": False,
            "mergeCommit": {"oid": MERGED_SHA}}
        # C-18 stage 1: the observation is taken by the pre-lock phase and
        # passed in, exactly as tick() does it. Driving it through
        # observe_closed_prs rather than hand-building the map keeps this
        # exercising the production wiring.
        with mock.patch.object(supervisor_mod.gh, "pr_view", return_value=view), \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            observations = self.sup.observe_closed_prs(doc, [])
            self.sup.route_prs(doc, None, [], observations)
        return doc

    def ledger_events(self, name=None):
        raw = self.ledger_path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in raw:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if name is None or event.get("event_type") == name:
                out.append(event)
        return out

    def violation(self):
        found = self.ledger_events(STATE_INVARIANT_VIOLATION)
        return found[-1] if found else None


class TestLostLocalCommit(InvariantDetectionCase):

    def test_f1_lost_local_commit_is_frozen_not_silently_completed(self):
        """F1. GitHub merged + durable local MERGED evidence + state unmerged.
        Today the external branch silently completes it."""
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        self.assertNotEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        violation = self.violation()
        self.assertIsNotNone(violation, "no STATE_INVARIANT_VIOLATION was emitted")
        self.assertEqual(violation["metadata_redacted"]["verdict"], "LOST_LOCAL_COMMIT")
        self.assertTrue(violation["metadata_redacted"]["froze"])

    def test_f1b_lost_local_commit_does_not_complete_the_task(self):
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertFalse(doc["prs"]["100"]["merged"])


class TestOrdinaryExternal(InvariantDetectionCase):

    def test_f3_ordinary_external_merge_still_completes_and_does_not_freeze(self):
        """F3. No local merge evidence -> unchanged behaviour."""
        doc = self.merged_state()
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        self.assertTrue(doc["prs"]["100"]["merged"])
        self.assertIsNone(self.violation())
        merged = self.ledger_events("MERGED")
        self.assertTrue(merged[-1]["metadata_redacted"]["detected_externally"])

    def test_f4b_blocked_merged_outside_the_window_stays_ordinary(self):
        """F4b. The window must NARROW, not merely fail closed."""
        self.blocked_merged_event(hours_ago=5)       # before last_review_at (1h ago)
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")
        self.assertIsNone(self.violation())


class TestUnprovable(InvariantDetectionCase):

    def test_f4_blocked_merged_inside_the_window_is_unprovable(self):
        self.blocked_merged_event(hours_ago=0)       # after last_review_at
        doc = self.merged_state()
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        self.assertEqual(self.violation()["metadata_redacted"]["verdict"], "UNPROVABLE")

    def test_f8_truncated_ledger_line_is_unprovable_not_absence(self):
        with open(self.ledger_path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "MERGED", "task_id": "TASK-0\n')  # corrupt
        doc = self.merged_state()
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violation()["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertFalse(meta["ledger_readable"])

    def test_f15_no_reliable_lower_bound_is_unprovable(self):
        self.blocked_merged_event(hours_ago=0)
        doc = self.merged_state(last_review_at=None, history_pr_open=False)
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violation()["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertIsNone(meta["window_lower_source"])

    def test_f13a_unobservable_without_contradiction_defers(self):
        """A failed GitHub observation is not a merge invariant violation. With
        no durable local merge claim there is nothing to reconcile - only
        insufficient evidence to reach a conclusion this tick."""
        doc = self.merged_state()
        self.run_external_branch(doc, pr_state=None)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "REVIEW")
        self.assertFalse(doc["prs"]["100"]["merged"])
        self.assertIsNone(self.violation())
        self.notifier.send.assert_not_called()
        self.assertEqual(doc["counters"]["human_interventions"], 0)
        self.assertEqual(self.ledger_events("MERGED"), [])

    def test_f13b_unobservable_with_a_durable_local_claim_is_unprovable(self):
        """Two sources already contradict each other. Being unable to ask
        GitHub makes that unverifiable, not benign."""
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc, pr_state=None)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violation()["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertTrue(meta["froze"])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_unobservable_does_not_hide_a_provable_debt_divergence(self):
        """An independently provable contradiction must not vanish because
        GitHub is temporarily unavailable."""
        self.local_merged_event(debt_ids=("TASK-001-PR100-F1",), status="RECORDED")
        doc = self.merged_state(merged=True, task_state="MERGE_READY")
        self.run_external_branch(doc, pr_state=None)
        # route_prs skips merged records, so prove it through the classifier
        # the Watchdog also uses.
        from control import merge_invariant as mi
        github, state_view, ledger_view = mi.gather(
            task_id="TASK-001", pr_number=100, doc=doc,
            task=doc["tasks"]["TASK-001"], record=doc["prs"]["100"],
            ledger=self.sup.ledger, pr_view=None,
            now_iso=clock.iso(clock.now(TZ)))
        v = mi.classify(task_id="TASK-001", pr_number=100, github=github,
                        state_view=state_view, ledger_view=ledger_view)
        self.assertEqual(v.verdict, "DEBT_DIVERGENCE")
        self.assertEqual(v.missing_debt_ids, ("TASK-001-PR100-F1",))

    def test_unreadable_ledger_still_fails_closed_when_github_is_unobservable(self):
        """The narrowing does not extend to ledger corruption: an incomplete
        ledger means neither presence nor absence of a local claim can be
        established, so neither concluding nor deferring is safe."""
        with open(self.ledger_path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "MERGED", "task_id": "TASK-0\n')
        doc = self.merged_state()
        self.run_external_branch(doc, pr_state=None)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FROZEN")
        meta = self.violation()["metadata_redacted"]
        self.assertEqual(meta["verdict"], "UNPROVABLE")
        self.assertFalse(meta["ledger_readable"])


class TestMergeOriginContract(InvariantDetectionCase):

    def test_f6_local_merge_records_detected_externally_false(self):
        doc = self.merged_state()
        with mock.patch.object(supervisor_mod.gh, "merge",
                               return_value=mock.Mock(ok=True, stderr="", stdout="")), \
                mock.patch.object(supervisor_mod.gh, "pr_view",
                                  return_value=_open_pr(100, "task/task-001")), \
                mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                  return_value=DIFF_HASH), \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            self.sup.execute_merges([("TASK-001", 100)])

        merged = self.ledger_events("MERGED")
        self.assertEqual(len(merged), 1)
        self.assertIn("detected_externally", merged[0]["metadata_redacted"])
        self.assertFalse(merged[0]["metadata_redacted"]["detected_externally"])

    def test_f7_historical_merged_without_the_key_counts_as_local(self):
        self.local_merged_event(detected_externally=mock.sentinel.absent)
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertEqual(self.violation()["metadata_redacted"]["verdict"],
                         "LOST_LOCAL_COMMIT")

    def test_external_merged_evidence_is_not_local_evidence(self):
        self.local_merged_event(detected_externally=True)
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertIsNone(self.violation())
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "COMPLETE")


class TestMetadataDiscipline(InvariantDetectionCase):

    def test_notification_status_is_a_finite_code_never_raw_text(self):
        self.notifier.send.return_value = {"ok": False, "status": 500,
                                           "error": "Traceback: secret-ish raw text"}
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)

        meta = self.violation()["metadata_redacted"]
        self.assertFalse(meta["notification_delivered"])
        self.assertEqual(meta["notification_status"], "NOTIFICATION_SEND_FAILED")
        self.assertNotIn("Traceback", json.dumps(meta))
        self.assertNotIn("raw text", json.dumps(meta))

    def test_existing_finite_notifier_reason_is_reused(self):
        self.notifier.send.return_value = {"ok": False,
                                           "reason": "DISCORD_WEBHOOK_URL_NOT_SET"}
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertEqual(self.violation()["metadata_redacted"]["notification_status"],
                         "DISCORD_WEBHOOK_URL_NOT_SET")

    def test_durable_metadata_keys_are_a_closed_set(self):
        """Absence of one known offender proves nothing - a NEW unbounded key
        would slip straight past. The key set itself is the contract."""
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        self.assertEqual(set(self.violation()["metadata_redacted"]), {
            "check_id", "verdict", "detector", "fingerprint", "freezable", "froze",
            "evidence_codes", "github_observed", "github_merged", "merged_sha",
            "state_merged", "task_state", "local_merged_events",
            "external_merged_events", "claimed_debt_status", "claimed_debt_ids",
            "missing_debt_ids", "ledger_readable", "ledger_unreadable_lines",
            "blocked_merged_in_window", "window_lower_bound", "window_lower_source",
            "notification_delivered", "notification_status",
        })

    def test_durable_evidence_is_structured_codes_not_prose(self):
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        meta = self.violation()["metadata_redacted"]
        self.assertNotIn("evidence", meta)
        self.assertIn("evidence_codes", meta)
        self.assertTrue(all(isinstance(c, str) and c.isupper().__bool__()
                            for c in meta["evidence_codes"]))
        self.assertIn("LOCAL_MERGE_EVIDENCE", meta["evidence_codes"])


class TestDuplicateSuppression(InvariantDetectionCase):

    def test_f10_identical_finding_annunciates_once(self):
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        first = len(self.ledger_events(STATE_INVARIANT_VIOLATION))
        self.assertEqual(first, 1)
        # Identical evidence, driven again. Nothing is mutated between runs.
        self.run_external_branch(doc)
        self.assertEqual(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), first)
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_f12_materially_changed_evidence_reannunciates(self):
        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)
        before = len(self.ledger_events(STATE_INVARIANT_VIOLATION))

        with mock.patch.object(supervisor_mod.gh, "pr_view", return_value={
                "number": 100, "state": "MERGED", "isDraft": False,
                "mergeCommit": {"oid": "f" * 40}}), \
                mock.patch.object(supervisor_mod.workers, "close_worker"):
            observations = self.sup.observe_closed_prs(doc, [])
            # same verdict, different SHA
            self.sup.route_prs(doc, None, [], observations)
        self.assertGreater(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), before)


class TestFreezability(InvariantDetectionCase):

    def test_f14_merge_ready_is_annunciated_not_frozen(self):
        self.local_merged_event()
        doc = self.merged_state(task_state="MERGE_READY")
        self.run_external_branch(doc)

        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "MERGE_READY")
        meta = self.violation()["metadata_redacted"]
        self.assertFalse(meta["freezable"])
        self.assertFalse(meta["froze"])
        self.assertEqual(self.violation()["state_after"], "MERGE_READY")


class TestCrossDetectorSuppression(InvariantDetectionCase):
    """F16. The fingerprint deliberately excludes detector identity: whichever
    component sees a finding first annunciates, and the other must recognise
    the same finding rather than duplicating it."""

    def test_f16_supervisor_then_watchdog_annunciate_once_between_them(self):
        from control import watchdog

        self.local_merged_event()
        doc = self.merged_state()
        self.run_external_branch(doc)                      # Supervisor first
        self.assertEqual(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), 1)

        cfg = SimpleNamespace(timezone=TZ, github_repo="UNASSIGNED")
        with mock.patch.object(watchdog.gh, "pr_view", return_value={
                "number": 100, "state": "MERGED", "isDraft": False,
                "mergeCommit": {"oid": MERGED_SHA}}):
            watchdog.reconcile_merge_invariants(cfg, self.sup.ledger,
                                                self.notifier, doc)

        self.assertEqual(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), 1)
        self.assertEqual(doc["counters"]["human_interventions"], 1)

    def test_f16b_watchdog_then_supervisor_annunciate_once_between_them(self):
        from control import watchdog

        self.local_merged_event()
        doc = self.merged_state()
        cfg = SimpleNamespace(timezone=TZ, github_repo="UNASSIGNED")
        with mock.patch.object(watchdog.gh, "pr_view", return_value={
                "number": 100, "state": "MERGED", "isDraft": False,
                "mergeCommit": {"oid": MERGED_SHA}}):
            watchdog.reconcile_merge_invariants(cfg, self.sup.ledger,
                                                self.notifier, doc)   # Watchdog first
        self.assertEqual(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), 1)

        self.run_external_branch(doc)
        self.assertEqual(len(self.ledger_events(STATE_INVARIANT_VIOLATION)), 1)
        self.assertEqual(doc["counters"]["human_interventions"], 1)
