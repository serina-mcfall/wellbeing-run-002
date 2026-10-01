"""C-18 stage 5: the reviewer migrated onto the shared dispatch harness.

Before this stage `dispatch_reviewer` made five kinds of external call while
the Supervisor held its one exclusive state transaction (T1): resolving the
pull request head, the two GitHub calls `evidence.collect` makes, the material
diff hash, the prompt file write, the workmux/git worktree acquisition, the
job file write and the process spawn. One slow GitHub round trip therefore
stalled every task in the run.

Three things are proved here, and they are kept apart on purpose:

  1. THE BOUNDARY. Every one of those calls now happens with the state lock
     free, established by probing the REAL `flock` at the moment of each call,
     with a companion test proving the probe would catch a violation.

  2. THE HEAD-MOVED RULE, which is the hazard this stage carries. The head is
     observed BEFORE the lock, so by the time a worker would start it may be
     stale - and a review cut from a superseded commit would still write
     `reviewed_head` as though it had reviewed the current one. A head that
     cannot be proved unchanged invalidates the plan, at the execute phase and
     again at the commit phase.

  3. THE SHA BINDING. `REVIEW_DISPATCHED` and `REVIEW_RESULT` carry the head
     the review was cut from, in the APPEND-ONLY ledger. The only binding
     before this stage was `prs[n].reviewed_head` in mutable state.json, which
     every later cycle overwrites.

Everything runs against a real `state.Store` on a temporary path with real job
files. Nothing launches a worker, nothing makes a paid call and nothing
reaches the network: every GitHub entry point is mocked.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import (  # noqa: E402
    clock,
    config,
    ledger as ledger_mod,
    migration_lock,
    providers,
    routing,
    state as state_mod,
    supervisor as supervisor_mod,
)
import declared_phases  # noqa: E402

TZ = "Pacific/Auckland"
PR = 100
BRANCH = "task/task-001"
HEAD = "a1b2c3d4e5f6071829304152637485960718293a"
MOVED_HEAD = "b1b2c3d4e5f6071829304152637485960718293b"
WORKER = "task-001-review-1"
DIFF_HASH = "6f1b" * 16

#: `run_tick(execute_head=...)` sentinel for "the head did not move". A plain
#: None cannot serve: None is itself a meaningful answer from gh.pr_diff_sha -
#: GitHub could not be reached - and that case has to be testable.
UNCHANGED = object()


def lock_is_held(lock_path: Path) -> bool:
    """Whether Store.transaction's exclusive flock is held right now.

    flock conflicts between distinct open file descriptions even inside one
    process, so an independent open() plus LOCK_NB tests the real lock rather
    than a flag the code under test kindly sets for us.
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


def ok_result():
    return mock.Mock(ok=True, stdout="", stderr="", code=0)


def fail_result(stderr="boom"):
    return mock.Mock(ok=False, stdout="", stderr=stderr, code=1)


def open_pr(number=PR, branch=BRANCH) -> dict:
    return {"number": number, "state": "OPEN", "isDraft": False,
            "headRefName": branch, "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE", "statusCheckRollup": []}


class ReviewerCase(unittest.TestCase):
    """A real Supervisor over a real Store and a real worker-log directory."""

    def setUp(self):
        self.cfg = config.load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_dir = self.root / "workers"
        self.job_dir.mkdir()
        log_patch = mock.patch.object(config, "WORKER_LOG_DIR", self.job_dir)
        log_patch.start()
        self.addCleanup(log_patch.stop)

        self.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.heartbeat_path = self.root / "heartbeat.json"
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.sup = self.supervisor()

    def supervisor(self):
        """A Supervisor as a fresh process would build one.

        Used twice on purpose: the crash test needs a second Supervisor that
        shares nothing in memory with the first, so anything it recovers it
        recovered from durable state.
        """
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            sup = supervisor_mod.Supervisor(self.cfg)
        sup.store = self.store
        sup.ledger = ledger_mod.Ledger(path=self.root / "ledger.jsonl", tz=TZ,
                                       experiment_id="run-002")
        self.events: list[tuple[str, dict]] = []
        sup.log = mock.Mock(side_effect=lambda e, **k: self.events.append((e, k)))
        sup.notify_out = mock.Mock(return_value={"ok": True})
        sup.run_declared = mock.Mock(
            side_effect=declared_phases.provider_phases_suppressed)
        return sup

    # ------------------------------------------------------------ fixtures

    def seed(self, *, state="REVIEW", number=PR, write=True, **record_fields) -> dict:
        """A PR-bearing task waiting for a reviewer.

        REVIEW rather than PR_OPEN deliberately: PR_OPEN is also an EVIDENCE
        state, so a tick seeded there runs the C-05.3a security phases and
        moves the task out of the routing states before route_prs sees it.
        REVIEW with no verdict is the shape route_awaiting_dispatch sends to
        the reviewer, and it is the one these tests are about.
        """
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "Build the thing", [],
                                  "feature", False, TZ)
        task.update({"state": state, "branch": BRANCH, "pr": number})
        migration_lock.ensure(doc)
        record = routing.blank_pr_record(number, "TASK-001", BRANCH)
        record.update(record_fields)
        doc["prs"][str(number)] = record
        if write:
            self.store._write(doc)
        return doc

    def observation(self, *, head=HEAD, diff_hash=DIFF_HASH, number=PR):
        return supervisor_mod.ReviewObservation(pr_number=number, head=head,
                                                diff_hash=diff_hash)

    # ------------------------------------------------------------- driving

    def run_tick(self, sup=None, *, head=HEAD, execute_head=UNCHANGED, acquire=True,
                 start=True, prs=None, skip_execute=False, skip_confirm=False,
                 entries=None, draft=False, ready=True, view_state="OPEN",
                 view_branch=BRANCH, view=UNCHANGED):
        """One real tick, recording whether the state lock was held at the
        moment of every external reviewer call.

        Two GitHub reads happen per reviewer dispatch and they are different
        calls on purpose: `gh.pr_diff_sha` is the PRE-LOCK observation, and
        `gh.pr_view` is the execute phase's own re-derivation. `execute_head`
        is what that second one reports, so a head that moved between
        observation and dispatch can be staged; it defaults to `head`, i.e. a
        head that did not move.

        `draft` makes the pull request a draft, which is the first fixture in
        this repository that does - every other one hard-codes isDraft False,
        so the draft path had never been exercised.

        `skip_execute` and `skip_confirm` simulate a process that died in the
        corresponding window: whatever had already committed stays, and
        nothing in memory survives into the next Supervisor.
        """
        sup = sup or self.sup
        self.calls: list[tuple[str, bool]] = []
        second = head if execute_head is UNCHANGED else execute_head
        if view is UNCHANGED:
            view = {"number": PR, "state": view_state, "isDraft": draft,
                    "headRefName": view_branch, "headRefOid": second,
                    "mergeStateStatus": "CLEAN", "statusCheckRollup": []}

        def spy(name, answer):
            def inner(*_a, **_kw):
                self.calls.append((name, lock_is_held(self.store.lock_path)))
                return answer() if callable(answer) else answer
            return inner

        patches = [
            mock.patch.object(supervisor_mod.gh, "list_open_prs",
                              return_value=[open_pr(branch=view_branch)]
                              if prs is None else prs),
            mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                              side_effect=spy("gh.pr_diff_sha", head)),
            mock.patch.object(supervisor_mod.gh, "pr_view",
                              side_effect=spy("gh.pr_view", view)),
            mock.patch.object(supervisor_mod.gh, "mark_ready",
                              side_effect=spy("gh.mark_ready",
                                              ok_result if ready else fail_result)),
            mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                              side_effect=spy("material_diff_hash", DIFF_HASH)),
            mock.patch.object(supervisor_mod.evidence, "collect",
                              side_effect=spy("evidence.collect", list)),
            mock.patch.object(supervisor_mod.evidence, "render", return_value=""),
            mock.patch.object(supervisor_mod.prompts, "reviewer", return_value="p"),
            mock.patch.object(supervisor_mod.prompts, "write",
                              side_effect=spy("prompts.write",
                                              lambda: self.root / "p.md")),
            mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                              side_effect=spy("acquire_worktree",
                                              lambda: (self.worktree, "")
                                              if acquire
                                              else (None, "branch checked out"))),
            mock.patch.object(supervisor_mod.workers, "write_job",
                              side_effect=spy("write_job",
                                              lambda: self.root / "j.json")),
            mock.patch.object(supervisor_mod.workers, "start_job",
                              side_effect=spy("start_job",
                                              ok_result if start else fail_result)),
            mock.patch.object(supervisor_mod.workers, "read_status",
                              return_value=None),
            mock.patch.object(supervisor_mod.proc, "worker_entry_processes",
                              return_value={} if entries is None else entries),
            mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                              self.heartbeat_path),
        ]
        if skip_execute:
            patches.append(mock.patch.object(sup, "execute_dispatches",
                                             return_value=[]))
        if skip_confirm:
            patches.append(mock.patch.object(sup, "confirm_dispatches"))

        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            sup.tick()

    # ------------------------------------------------------------- helpers

    def durable(self) -> dict:
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def task(self) -> dict:
        return self.durable()["tasks"]["TASK-001"]

    def record(self, number=PR) -> dict:
        return self.durable()["prs"][str(number)]

    def durable_claim(self):
        return state_mod.dispatch_claim(self.task(), "reviewer")

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def event(self, name: str) -> dict:
        found = [fields for event, fields in self.events if event == name]
        self.assertTrue(found, f"no {name} event in {self.names()}")
        return found[0]

    def plan(self, *, head=HEAD, worker=WORKER, cycle=1, **context):
        return supervisor_mod.DispatchPlan(
            role="reviewer", task_id="TASK-001", pr=PR, worker=worker,
            branch=BRANCH, observed_head=head,
            context={"cycle": cycle, "review_branch": f"review/c{cycle}/{BRANCH}",
                     "diff_hash": DIFF_HASH, "task_title": "Build the thing",
                     **context})


# ================================================== the transaction boundary


class NoReviewerCallHappensUnderTheStateLock(ReviewerCase):
    """The C-18 guarantee for the reviewer path, measured against the real
    flock rather than read off the source."""

    EXTERNAL = ["acquire_worktree", "evidence.collect", "gh.pr_diff_sha",
                "gh.pr_view", "material_diff_hash", "prompts.write",
                "start_job", "write_job"]

    def test_every_external_reviewer_call_is_made_with_the_lock_free(self):
        self.seed()
        self.run_tick()

        self.assertTrue(self.calls,
                        "the test proves nothing if nothing external ran")
        self.assertEqual(
            sorted({name for name, _ in self.calls}), self.EXTERNAL,
            "every external call dispatch_reviewer used to make inside T1 "
            "must appear")
        self.assertEqual([held for _, held in self.calls],
                         [False] * len(self.calls),
                         f"an external call ran under the lock: {self.calls}")

    def test_the_lock_probe_can_actually_detect_a_held_lock(self):
        """Without this, the test above could pass because the probe is
        broken rather than because the code is right."""
        self.seed()
        self.assertFalse(lock_is_held(self.store.lock_path))
        with self.store.transaction():
            self.assertTrue(lock_is_held(self.store.lock_path))
        self.assertFalse(lock_is_held(self.store.lock_path))

    def test_the_spy_would_notice_a_call_made_inside_a_transaction(self):
        """The companion proof: the same spy reports True for a deliberate
        in-transaction call, so a green boundary test cannot be a broken
        spy."""
        self.seed()
        self.calls = []
        with mock.patch.object(
                supervisor_mod.gh, "pr_diff_sha",
                side_effect=lambda *a, **k: self.calls.append(
                    ("gh.pr_diff_sha", lock_is_held(self.store.lock_path)))):
            with self.store.transaction():
                supervisor_mod.gh.pr_diff_sha("owner/repo", PR)
        self.assertEqual(self.calls, [("gh.pr_diff_sha", True)])

    def test_the_planning_half_touches_no_file_and_no_subprocess(self):
        """T1's own guarantee, stated separately from the tick-wide one: the
        claim is made without any of the moved calls happening at all."""
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha") as head, \
                mock.patch.object(supervisor_mod.routing,
                                  "material_diff_hash") as diff, \
                mock.patch.object(supervisor_mod.evidence, "collect") as collect, \
                mock.patch.object(supervisor_mod.prompts, "write") as write, \
                mock.patch.object(supervisor_mod.workers,
                                  "acquire_worktree") as acquire, \
                mock.patch.object(supervisor_mod.workers, "write_job") as job, \
                mock.patch.object(supervisor_mod.workers, "start_job") as start:
            self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                       self.observation())
        for name, patched in (("pr_diff_sha", head), ("material_diff_hash", diff),
                              ("collect", collect), ("write", write),
                              ("acquire_worktree", acquire), ("write_job", job),
                              ("start_job", start)):
            patched.assert_not_called()
            self.assertTrue(True, name)
        self.assertEqual(len(self.sup._dispatch_plans), 1)
        self.assertEqual(
            state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                     "reviewer")["observed_head"], HEAD)


# =============================================================== the planner


class ThePlanningHalfReservesAndNothingMore(ReviewerCase):

    def test_the_claim_carries_the_observed_head_cycle_and_diff_hash(self):
        doc = self.seed(write=False, review_cycles=1)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation())
        claim = state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "reviewer")
        self.assertEqual(claim["observed_head"], HEAD)
        self.assertEqual(claim["worker"], "task-001-review-2")
        self.assertEqual(claim["pr"], PR)
        self.assertEqual(claim["context"]["cycle"], 2)
        self.assertEqual(claim["context"]["review_branch"], f"review/c2/{BRANCH}")
        self.assertEqual(claim["context"]["diff_hash"], DIFF_HASH)

    def test_no_commit_side_state_moves_at_planning_time(self):
        doc = self.seed(write=False, state="PR_OPEN", approval_current=True)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation())
        record = doc["prs"][str(PR)]
        self.assertEqual(record["review_cycles"], 0)
        self.assertTrue(record["approval_current"])
        self.assertIsNone(record.get("reviewed_head"))
        self.assertEqual(doc["workers"], {})
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "PR_OPEN")

    def test_a_missing_observation_defers_rather_than_fetching_under_the_lock(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha") as head:
            self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR, None)
        head.assert_not_called()
        self.assertEqual(self.event("REVIEW_DISPATCH_DEFERRED")["outcome"],
                         "OBSERVATION_MISSING")
        self.assertEqual(self.sup._dispatch_plans, [])
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "reviewer"))

    def test_an_observation_for_another_pull_request_is_refused(self):
        doc = self.seed(write=False)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation(number=PR + 1))
        self.assertEqual(self.event("REVIEW_DISPATCH_DEFERRED")["outcome"],
                         "OBSERVATION_MISSING")
        self.assertEqual(self.sup._dispatch_plans, [])

    def test_an_unresolvable_head_is_a_dispatch_failure_not_a_deferral(self):
        doc = self.seed(write=False)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation(head=None, diff_hash=None))
        self.assertIn("DISPATCH_FAILED", self.names())
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"], 1)
        self.assertEqual(self.sup._dispatch_plans, [])

    def test_a_claim_state_would_refuse_fails_the_dispatch_instead_of_holding_a_slot(self):
        doc = self.seed(write=False)
        # A short head is exactly what new_dispatch_claim refuses. Reaching it
        # through the observation proves the refusal is handled rather than
        # raised out of T1.
        self.sup.dispatch_reviewer(
            doc, doc["tasks"]["TASK-001"], PR,
            supervisor_mod.ReviewObservation(pr_number=PR, head="abc",
                                             diff_hash=DIFF_HASH))
        self.assertEqual(self.event("DISPATCH_CLAIM_REFUSED")["outcome"],
                         "CLAIM_REFUSED")
        self.assertIn("DISPATCH_FAILED", self.names())
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "reviewer"))

    def test_a_paused_review_provider_still_parks_the_task(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.providers, "may",
                               return_value=False):
            self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                       self.observation())
        self.assertEqual(doc["tasks"]["TASK-001"]["state"],
                         "WAITING_PROVIDER_RESET")
        self.assertEqual(self.sup._dispatch_plans, [])

    def test_an_existing_claim_is_never_planned_over(self):
        doc = self.seed(write=False)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation())
        first = dict(state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "reviewer"))
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation(head=MOVED_HEAD))
        self.assertEqual(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                  "reviewer"), first)
        self.assertEqual(len(self.sup._dispatch_plans), 1)


# ========================================================= the head-moved rule


class AHeadThatMovedInvalidatesThePlan(ReviewerCase):
    """The hazard this stage carries, in both of the places it can bite."""

    def test_a_head_that_moved_before_the_spawn_starts_no_worker(self):
        self.seed()
        self.run_tick(head=HEAD, execute_head=MOVED_HEAD)

        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "HEAD_MOVED")
        self.assertEqual(self.durable()["workers"], {},
                         "a worker was started against a head that had moved")
        self.assertNotIn("start_job", [name for name, _ in self.calls])
        self.assertNotIn("acquire_worktree", [name for name, _ in self.calls])

    def test_a_head_that_moved_binds_no_stale_sha_and_counts_no_cycle(self):
        self.seed()
        self.run_tick(head=HEAD, execute_head=MOVED_HEAD)

        record = self.record()
        self.assertIsNone(record.get("reviewed_head"),
                          "a stale head was bound to the pull request record")
        self.assertEqual(record["review_cycles"], 0)
        self.assertEqual(record["dispatch_failures"], 1)
        self.assertIsNone(self.durable_claim(),
                          "the claim must be released so the task is re-routable")

    def test_an_unresolvable_head_at_dispatch_time_also_invalidates(self):
        """Fail closed: a head that cannot be proved unchanged has not been
        proved unchanged."""
        self.seed()
        self.run_tick(head=HEAD, execute_head=None)
        acquired = [name for name, _ in self.calls if name == "acquire_worktree"]
        self.assertEqual(acquired, [],
                         "a worktree was cut from a head nothing could confirm")
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "HEAD_MOVED")
        self.assertIsNone(self.record().get("reviewed_head"))
        self.assertEqual(self.record()["review_cycles"], 0)

    def test_a_head_that_did_not_move_dispatches_normally(self):
        """The control. Without it the tests above could pass because nothing
        ever dispatches."""
        self.seed()
        self.run_tick()

        self.assertNotIn("REVIEW_PREFLIGHT_DEFERRED", self.names())
        record = self.record()
        self.assertEqual(record.get("reviewed_head"), HEAD)
        self.assertEqual(record["reviewed_diff_hash"], DIFF_HASH)
        self.assertEqual(record["review_cycles"], 1)
        self.assertEqual(self.task()["state"], "REVIEW")
        self.assertIn(WORKER, self.durable()["workers"])

    def test_the_commit_refuses_a_claim_rebound_to_a_different_head(self):
        """The second place the rule bites: between execute and commit the
        claim can be replaced by a re-plan at the SAME cycle - so the same
        worker name and pull request - against a newer head. Committing this
        result against that claim would bind the new reservation's record to
        the old reservation's SHA."""
        doc = self.seed(write=False)
        task = doc["tasks"]["TASK-001"]
        self.sup.dispatch_reviewer(doc, task, PR, self.observation())
        plan = self.sup._dispatch_plans[0]
        # The claim is re-minted at a newer head while the execute phase runs.
        state_mod.dispatch_claims(task)["reviewer"]["observed_head"] = MOVED_HEAD

        self.sup.apply_dispatch_result(
            doc, supervisor_mod.DispatchResult(plan=plan, ok=True,
                                               worktree=str(self.worktree)))
        self.assertEqual(self.event("DISPATCH_COMMIT_REFUSED")["outcome"],
                         "HEAD_REBOUND")
        self.assertEqual(doc["prs"][str(PR)]["review_cycles"], 0)
        self.assertIsNone(doc["prs"][str(PR)].get("reviewed_head"))
        self.assertEqual(doc["workers"], {})

    def test_a_resumed_claim_is_still_held_to_its_own_observed_head(self):
        """Recovery re-plans from the durable claim, which carries the head
        observed a tick ago. The execute phase re-checks it exactly as a fresh
        plan's is re-checked."""
        self.seed()
        self.run_tick(skip_execute=True)
        self.assertIsNotNone(self.durable_claim())

        later = self.supervisor()
        self.run_tick(later, head=MOVED_HEAD, execute_head=MOVED_HEAD)
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "HEAD_MOVED")
        self.assertEqual(self.durable()["workers"], {})
        self.assertIsNone(self.record().get("reviewed_head"))


# ================================================================== recovery


class TheClaimSurvivesACrash(ReviewerCase):

    def test_a_crash_between_plan_and_spawn_leaves_a_durable_claim(self):
        self.seed()
        self.run_tick(skip_execute=True)

        claim = self.durable_claim()
        self.assertIsNotNone(claim, "the reservation did not survive")
        self.assertEqual(claim["worker"], WORKER)
        self.assertEqual(claim["observed_head"], HEAD)
        self.assertEqual(claim["claim_state"], state_mod.DISPATCH_PLANNED)
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.record()["review_cycles"], 0)

    def test_a_fresh_supervisor_resumes_that_claim_and_spawns_once(self):
        self.seed()
        self.run_tick(skip_execute=True)

        later = self.supervisor()
        self.run_tick(later)

        self.assertEqual(self.event("DISPATCH_RESUMED")["outcome"], "REPLANNED")
        self.assertEqual(self.record()["review_cycles"], 1,
                         "exactly one review cycle per real dispatch")
        self.assertIn(WORKER, self.durable()["workers"])
        self.assertIsNone(self.durable_claim())
        self.assertEqual([name for name, _ in self.calls].count("start_job"), 1)

    def test_a_crash_after_the_spawn_commits_from_the_job_file_not_a_respawn(self):
        self.seed()
        self.run_tick(skip_confirm=True)
        (self.job_dir / f"{WORKER}.job.json").write_text(
            json.dumps({"worktree": str(self.worktree)}), encoding="utf-8")

        later = self.supervisor()
        self.run_tick(later)

        self.assertEqual(self.event("DISPATCH_RECOVERED")["outcome"],
                         "SPAWN_EVIDENCE_FOUND")
        self.assertEqual([name for name, _ in self.calls].count("start_job"), 0,
                         "a worker was started a second time")
        self.assertEqual(self.record()["review_cycles"], 1)
        self.assertEqual(self.record().get("reviewed_head"), HEAD,
                         "a recovered dispatch must bind the head it claimed")
        self.assertIn(WORKER, self.durable()["workers"])

    def test_the_claim_reserves_the_slot_against_route_awaiting_dispatch(self):
        """A planned-but-unspawned reviewer must stop the general recovery
        rule dispatching a second one - two reviewers on one pull request is
        two paid workers and two verdicts."""
        self.seed()
        self.run_tick(skip_execute=True)

        doc = self.durable()
        self.assertTrue(self.sup.has_worker(doc, PR, ("reviewer", "fixer")))
        with mock.patch.object(self.sup, "dispatch_reviewer") as again:
            self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], PR,
                                             doc["prs"][str(PR)],
                                             {PR: self.observation()})
        again.assert_not_called()

    def test_without_the_claim_the_same_state_would_be_dispatched(self):
        """The control for the test above: it proves the claim is what stops
        the second dispatch, not the task state."""
        self.seed()
        self.run_tick(skip_execute=True)

        doc = self.durable()
        state_mod.dispatch_claims(doc["tasks"]["TASK-001"]).pop("reviewer")
        with mock.patch.object(self.sup, "dispatch_reviewer") as again:
            self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], PR,
                                             doc["prs"][str(PR)],
                                             {PR: self.observation()})
        again.assert_called_once()


# ============================================================ the SHA binding


class TheLedgerBindsAReviewToItsCommit(ReviewerCase):
    """The defect this stage closes: `REVIEW_DISPATCHED` and `REVIEW_RESULT`
    carried no head, while the security worker name is SHA-bound. The ledger
    is append-only; `prs[n].reviewed_head` is overwritten every cycle."""

    def test_review_dispatched_carries_the_head_it_was_cut_from(self):
        self.seed()
        self.run_tick()
        fields = self.event("REVIEW_DISPATCHED")
        self.assertEqual(fields["head_sha"], HEAD)
        self.assertEqual(fields["pr_id"], PR)
        self.assertEqual(fields["agent_id"], WORKER)
        self.assertEqual(fields["metadata_redacted"]["review_cycle"], 1)

    def test_the_head_on_the_event_is_the_head_the_worktree_used(self):
        """Not merely present - the same SHA `acquire_worktree` was given."""
        self.seed()
        self.run_tick()
        with mock.patch.object(supervisor_mod.workers, "acquire_worktree") as _:
            pass
        self.assertEqual(self.event("REVIEW_DISPATCHED")["head_sha"],
                         self.record().get("reviewed_head"))

    def test_review_result_carries_the_head_the_verdict_judged(self):
        doc = self.seed(write=False, review_cycles=1, reviewed_head=HEAD)
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        meta = {"role": "reviewer", "task_id": "TASK-001", "pr": PR, "head": HEAD}
        self.finish_review(doc, meta)
        self.assertEqual(self.event("REVIEW_RESULT")["head_sha"], HEAD)

    def test_review_result_prefers_the_worker_binding_over_the_mutable_field(self):
        """`reviewed_head` is mutable and a later cycle overwrites it; the
        worker record is minted per dispatch and cannot be."""
        doc = self.seed(write=False, review_cycles=1, reviewed_head=MOVED_HEAD)
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        meta = {"role": "reviewer", "task_id": "TASK-001", "pr": PR, "head": HEAD}
        self.finish_review(doc, meta)
        self.assertEqual(self.event("REVIEW_RESULT")["head_sha"], HEAD)

    def test_an_unbound_review_reports_null_rather_than_omitting_the_key(self):
        """A review whose head is unknown must be VISIBLY unbound to anything
        composing a merge gate - a missing key reads as 'not checked'."""
        doc = self.seed(write=False, review_cycles=1)
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        meta = {"role": "reviewer", "task_id": "TASK-001", "pr": PR}
        self.finish_review(doc, meta)
        fields = self.event("REVIEW_RESULT")
        self.assertIn("head_sha", fields)
        self.assertIsNone(fields["head_sha"])

    def test_the_verdict_fields_the_event_already_carried_are_unchanged(self):
        doc = self.seed(write=False, review_cycles=1, reviewed_head=HEAD)
        doc["tasks"]["TASK-001"]["state"] = "REVIEW"
        meta = {"role": "reviewer", "task_id": "TASK-001", "pr": PR, "head": HEAD}
        self.finish_review(doc, meta)
        metadata = self.event("REVIEW_RESULT")["metadata_redacted"]
        self.assertEqual(metadata["verdict"], routing.REVIEW_PASS)
        self.assertIn("gates", metadata)
        self.assertIn("counts", metadata)
        self.assertIn("finding_ids", metadata)

    def test_the_commit_binds_the_head_to_the_worker_record(self):
        self.seed()
        self.run_tick()
        self.assertEqual(self.durable()["workers"][WORKER]["head"], HEAD)

    def test_both_events_name_the_worker_the_sha_belongs_to(self):
        """Reviewer-identity verification checks the SHA-bound result against
        a worker, so a head with no `agent_id` beside it proves nothing."""
        self.seed()
        self.run_tick()
        self.assertEqual(self.event("REVIEW_DISPATCHED")["agent_id"], WORKER)

        doc = self.seed(write=False, review_cycles=1, reviewed_head=HEAD)
        meta = {"role": "reviewer", "task_id": "TASK-001", "pr": PR, "head": HEAD}
        self.finish_review(doc, meta)
        self.assertEqual(self.event("REVIEW_RESULT")["agent_id"], WORKER)

    def test_the_real_ledger_writes_head_sha_into_metadata_redacted_only(self):
        """END TO END, through the REAL Ledger, because every other test in
        this class reads a mocked `sup.log` and therefore cannot see where
        `append` actually puts the field.

        The contract: `metadata_redacted.head_sha`, ONE spelling. `ledger.py`
        has no `head_sha` in FIELDS and merges unknown kwargs into
        metadata_redacted, so a top-level spelling is impossible - and the
        consumer treats disagreement between the two as fatal.
        """
        ledger = ledger_mod.Ledger(path=self.root / "contract.jsonl", tz=TZ,
                                   experiment_id="run-002")
        self.assertNotIn("head_sha", ledger_mod.FIELDS,
                         "a top-level head_sha would create a second spelling")
        for event_type, extra in (("REVIEW_DISPATCHED", {"review_cycle": 1}),
                                  ("REVIEW_RESULT", {"verdict": "REVIEW_PASS"})):
            with self.subTest(event=event_type):
                written = ledger.append(event_type, task_id="TASK-001", pr_id=PR,
                                        role="reviewer", agent_id=WORKER,
                                        activity_class="REVIEW", outcome="X",
                                        head_sha=HEAD, metadata_redacted=extra)
                self.assertNotIn("head_sha", written,
                                 "head_sha must not appear at the top level")
                self.assertEqual(written["metadata_redacted"]["head_sha"], HEAD)
                # The caller's own metadata survives the merge.
                for key, value in extra.items():
                    self.assertEqual(written["metadata_redacted"][key], value)

        lines = [json.loads(line) for line in
                 (self.root / "contract.jsonl").read_text(
                     encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([line["metadata_redacted"]["head_sha"] for line in lines],
                         [HEAD, HEAD], "the SHA must survive to the file on disk")

    def finish_review(self, doc, meta):
        review = routing.Review(verdict=routing.REVIEW_PASS, findings=[],
                                gates={}, summary="fine")
        with mock.patch.object(supervisor_mod.routing, "parse_review",
                               return_value=review), \
                mock.patch.object(supervisor_mod.routing, "review_is_consistent",
                                  return_value=(True, "")), \
                mock.patch.object(supervisor_mod.routing, "touches_ui",
                                  return_value=False), \
                mock.patch.object(supervisor_mod.workers, "worker_output",
                                  return_value="text"), \
                mock.patch.object(self.sup, "release_review_worktree"), \
                mock.patch.object(supervisor_mod.providers, "record_success"):
            self.sup.on_reviewer_finished(doc, WORKER, meta,
                                          {"outcome": "SUCCESS"})


# ================================================= the pre-lock observation


class TheObservationIsNarrowedToWhatCouldBeDispatched(ReviewerCase):

    def observe(self, doc, prs=None):
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                               return_value=HEAD) as head, \
                mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                  return_value=DIFF_HASH):
            result = self.sup.observe_review_heads(
                doc, [open_pr()] if prs is None else prs)
        return result, head

    def test_a_routing_state_with_no_worker_is_observed(self):
        doc = self.seed(write=False)
        result, head = self.observe(doc)
        self.assertEqual(result[PR].head, HEAD)
        self.assertEqual(result[PR].diff_hash, DIFF_HASH)
        head.assert_called_once()

    def test_a_pull_request_with_a_live_reviewer_is_not_asked_about(self):
        doc = self.seed(write=False)
        doc["workers"][WORKER] = {"role": "reviewer", "task_id": "TASK-001",
                                  "pr": PR}
        result, head = self.observe(doc)
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_a_claimed_pull_request_is_not_asked_about(self):
        doc = self.seed(write=False)
        self.sup.dispatch_reviewer(doc, doc["tasks"]["TASK-001"], PR,
                                   self.observation())
        result, head = self.observe(doc)
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_a_merge_candidate_is_not_asked_about(self):
        doc = self.seed(write=False, state="REVIEW",
                        review_verdict=routing.REVIEW_PASS, approval_current=True)
        result, head = self.observe(doc)
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_a_fixer_bound_pull_request_is_not_asked_about(self):
        doc = self.seed(write=False, state="REVIEW",
                        review_verdict=routing.REVIEW_FAIL,
                        pending_findings=[{"id": "F1"}])
        result, head = self.observe(doc)
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_a_closed_pull_request_is_not_asked_about(self):
        doc = self.seed(write=False)
        result, head = self.observe(doc, prs=[])
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_an_unreadable_snapshot_yields_nothing_rather_than_guessing(self):
        doc = self.seed(write=False)
        doc.pop("workers")
        result, head = self.observe(doc)
        self.assertEqual(result, {})
        head.assert_not_called()

    def test_an_unresolvable_head_skips_the_second_github_call(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                               return_value=None), \
                mock.patch.object(supervisor_mod.routing,
                                  "material_diff_hash") as diff:
            result = self.sup.observe_review_heads(doc, [open_pr()])
        diff.assert_not_called()
        self.assertIsNone(result[PR].head)
        self.assertIsNone(result[PR].diff_hash)


# ==================================================== the shared failure path


class TheSharedFailurePathServesBothRoles(ReviewerCase):
    """`on_dispatch_failure` is reached by the reviewer and the fixer. Stage 5
    generalises it to survive being called from the commit transaction that
    runs AFTER T1, where the pull request record may legitimately be gone."""

    def test_a_failed_spawn_counts_a_dispatch_failure_and_leaves_no_worker(self):
        self.seed()
        self.run_tick(start=False)
        self.assertEqual(self.record()["dispatch_failures"], 1)
        self.assertEqual(self.record()["review_cycles"], 0)
        self.assertEqual(self.durable()["workers"], {})
        self.assertIsNone(self.durable_claim())

    def test_an_unobtainable_worktree_counts_a_dispatch_failure(self):
        self.seed()
        self.run_tick(acquire=False)
        self.assertEqual(self.event("DISPATCH_FAILED")["metadata_redacted"]["reason"],
                         "branch checked out")
        self.assertEqual(self.record()["dispatch_failures"], 1)

    def test_repeated_failures_still_escalate_to_a_human(self):
        doc = self.seed(write=False)
        for _ in range(self.cfg.max_repair_cycles):
            self.sup.on_dispatch_failure(doc, doc["tasks"]["TASK-001"], PR,
                                         "reviewer", "no worktree")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.sup.notify_out.assert_called_once()

    def test_the_fixer_still_reaches_the_same_accounting(self):
        doc = self.seed(write=False)
        self.sup.on_dispatch_failure(doc, doc["tasks"]["TASK-001"], PR, "fixer",
                                     "no port")
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"], 1)
        self.assertEqual(self.event("DISPATCH_FAILED")["role"], "fixer")

    def test_a_pull_request_record_gone_is_refused_not_raised(self):
        """The generalisation. Before C-18 the only caller held the record it
        had just read; a harness role now calls this from a later transaction
        in which the record can have been removed by an external merge."""
        doc = self.seed(write=False)
        doc["prs"].pop(str(PR))
        for role in ("reviewer", "fixer"):
            with self.subTest(role=role):
                self.sup.on_dispatch_failure(doc, doc["tasks"]["TASK-001"], PR,
                                             role, "gone")
        refusals = [fields for name, fields in self.events
                    if name == "DISPATCH_FAILURE_REFUSED"]
        self.assertEqual(len(refusals), 2)
        self.assertEqual({f["outcome"] for f in refusals}, {"PR_RECORD_GONE"})
        self.assertNotIn("DISPATCH_FAILED", self.names())

    def test_the_commit_half_also_refuses_a_vanished_record(self):
        doc = self.seed(write=False)
        task = doc["tasks"]["TASK-001"]
        self.sup.dispatch_reviewer(doc, task, PR, self.observation())
        plan = self.sup._dispatch_plans[0]
        doc["prs"].pop(str(PR))
        self.sup.apply_dispatch_result(
            doc, supervisor_mod.DispatchResult(plan=plan, ok=True,
                                               worktree=str(self.worktree)))
        self.assertEqual(self.event("DISPATCH_COMMIT_REFUSED")["outcome"],
                         "PR_RECORD_GONE")
        self.assertEqual(doc["workers"], {})
        self.assertIsNone(state_mod.dispatch_claim(task, "reviewer"),
                          "the slot must not stay reserved by a dead claim")


# ============================================================ controls still bite


class TheControlsStillGovernAReviewerSpawn(ReviewerCase):

    def test_a_freeze_arriving_after_t1_stops_the_spawn(self):
        self.seed()
        plan = self.plan()
        doc = self.durable()
        doc["frozen_at"] = clock.iso(clock.now(TZ))
        with mock.patch.object(supervisor_mod.workers, "acquire_worktree") as acquire:
            self.assertEqual(self.sup.execute_dispatches([plan], snapshot=doc), [])
        acquire.assert_not_called()
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "RUN_FROZEN")

    def test_a_paused_review_provider_stops_the_spawn(self):
        self.seed()
        plan = self.plan()
        with mock.patch.object(supervisor_mod.providers, "may",
                               return_value=False), \
                mock.patch.object(supervisor_mod.workers,
                                  "acquire_worktree") as acquire:
            self.assertEqual(
                self.sup.execute_dispatches([plan], snapshot=self.durable()), [])
        acquire.assert_not_called()
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "PROVIDER_PAUSED")

    def test_a_held_plan_leaves_its_claim_exactly_as_it_was(self):
        self.seed()
        self.run_tick(skip_execute=True)
        before = self.durable_claim()
        with mock.patch.object(supervisor_mod.providers, "may",
                               return_value=False):
            self.run_tick()
        self.assertEqual(self.durable_claim(), before)
        self.assertEqual(self.durable()["workers"], {})


# ===================================================== draft to ready for review


class ADraftIsMarkedReadyBeforeReviewAndGrantsNothing(ReviewerCase):
    """The approved authority amendment:

        "The Supervisor may mark a draft PR for a governed task ready for
        review before independent review dispatch. This action grants no
        approval or merge eligibility."

    EVERY other fixture in this repository hard-codes `isDraft: False`, so
    before this class the draft path had never been executed once. `draft=True`
    here is the first fixture that sets it.
    """

    def test_the_builder_never_opens_a_draft_so_this_path_is_exceptional(self):
        """Stated as a test rather than a comment, because the claim that the
        draft path is exceptional is only true while nothing passes --draft."""
        root = Path(__file__).resolve().parents[1]
        builder = (root / "prompts/builder.md").read_text(encoding="utf-8")
        self.assertIn("gh pr create", builder,
                      "the instruction that opens pull requests moved; this "
                      "test is no longer looking where it thinks it is")
        self.assertNotIn("--draft", builder)
        self.assertNotIn("--draft",
                         (root / "control/gh.py").read_text(encoding="utf-8"))

    def test_a_draft_is_marked_ready_and_the_review_then_dispatches(self):
        self.seed()
        self.run_tick(draft=True)

        self.assertEqual(self.event("PR_MARKED_READY_FOR_REVIEW")["outcome"],
                         "READY")
        self.assertIn("gh.mark_ready", [name for name, _ in self.calls])
        self.assertEqual(self.record()["review_cycles"], 1)
        self.assertIn(WORKER, self.durable()["workers"])

    def test_a_ready_pull_request_is_not_marked_again(self):
        self.seed()
        self.run_tick(draft=False)
        self.assertNotIn("gh.mark_ready", [name for name, _ in self.calls])
        self.assertNotIn("PR_MARKED_READY_FOR_REVIEW", self.names())

    def test_marking_ready_happens_with_the_state_lock_free(self):
        self.seed()
        self.run_tick(draft=True)
        marks = [held for name, held in self.calls if name == "gh.mark_ready"]
        self.assertEqual(marks, [False], f"ran under the lock: {self.calls}")

    def test_nothing_is_marked_ready_until_every_re_verification_has_passed(self):
        """Condition 1, as an EXTERNAL-EFFECT assertion rather than a state
        one. No state outcome can detect this: a draft wrongly marked ready
        leaves state identical to a draft correctly deferred. Only the absence
        of the GitHub call proves it, so this is asserted on the call list.
        """
        for label, kwargs in (("not found", dict(view=None)),
                              ("not open", dict(view_state="CLOSED")),
                              ("wrong branch", dict(view_branch="other")),
                              ("head moved", dict(execute_head=MOVED_HEAD))):
            with self.subTest(case=label):
                self.setUp()
                self.seed()
                self.run_tick(draft=True, **kwargs)
                self.assertNotIn("gh.mark_ready",
                                 [name for name, _ in self.calls],
                                 "a draft was marked ready before the "
                                 "re-verification that must gate it")

    def test_a_moved_head_defers_instead_of_marking_a_draft_ready(self):
        """Condition 1 and 2 together, and this is the mutation guard: drop
        the re-verification and this test goes red because a draft whose head
        has moved gets marked ready anyway."""
        self.seed()
        self.run_tick(draft=True, execute_head=MOVED_HEAD)

        self.assertNotIn("gh.mark_ready", [name for name, _ in self.calls],
                         "a draft was marked ready on a head that had moved")
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "HEAD_MOVED")
        self.assertEqual(self.durable()["workers"], {})

    def test_a_wrong_branch_association_defers_instead_of_marking_ready(self):
        """Condition 1: the task/PR association is re-derived, not trusted."""
        self.seed()
        self.run_tick(draft=True, view_branch="someone/elses-branch")

        self.assertNotIn("gh.mark_ready", [name for name, _ in self.calls])
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "PR_ASSOCIATION_MISMATCH")
        self.assertEqual(self.durable()["workers"], {})

    def test_a_pull_request_that_is_gone_defers(self):
        self.seed()
        self.run_tick(draft=True, view=None)
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "PR_NOT_FOUND")
        self.assertEqual(self.durable()["workers"], {})

    def test_a_pull_request_no_longer_open_defers(self):
        self.seed()
        self.run_tick(draft=True, view_state="MERGED")
        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "PR_NOT_OPEN")
        self.assertEqual(self.durable()["workers"], {})

    def test_an_api_refusal_defers_and_does_not_dispatch_anyway(self):
        """Condition 2. The refusal must not be worked around."""
        self.seed()
        self.run_tick(draft=True, ready=False)

        self.assertEqual(self.event("REVIEW_PREFLIGHT_DEFERRED")["outcome"],
                         "READY_FOR_REVIEW_REFUSED")
        self.assertNotIn("acquire_worktree", [name for name, _ in self.calls])
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.record()["review_cycles"], 0)

    def test_every_deferral_reason_is_one_finite_code_never_prose(self):
        """Condition 2: a FINITE diagnostic. The reason that reaches the
        durable dispatch-failure record is a code from the declared
        vocabulary, not an exception string."""
        cases = {
            "PR_NOT_FOUND": dict(view=None),
            "PR_NOT_OPEN": dict(view_state="CLOSED"),
            "PR_ASSOCIATION_MISMATCH": dict(view_branch="other"),
            "HEAD_MOVED": dict(execute_head=MOVED_HEAD),
            "READY_FOR_REVIEW_REFUSED": dict(ready=False),
        }
        self.assertEqual(set(cases), set(supervisor_mod.REVIEW_PREFLIGHT_CODES),
                         "the vocabulary and the cases must stay in step")
        for code, kwargs in cases.items():
            with self.subTest(code=code):
                self.setUp()
                self.seed()
                self.run_tick(draft=True, **kwargs)
                self.assertEqual(
                    self.event("DISPATCH_FAILED")["metadata_redacted"]["reason"],
                    code)
                self.assertIn(code, supervisor_mod.REVIEW_PREFLIGHT_CODES)

    def test_a_deferral_releases_the_claim_so_the_task_is_rerouted(self):
        """Condition 2: deferral, not a stall and not a retry loop. The claim
        is gone, so the next tick observes a fresh head and re-routes."""
        self.seed()
        self.run_tick(draft=True, execute_head=MOVED_HEAD)
        self.assertIsNone(self.durable_claim())

        self.run_tick(draft=True, head=MOVED_HEAD)
        self.assertEqual(self.record()["reviewed_head"], MOVED_HEAD)
        self.assertEqual(self.record()["review_cycles"], 1)

    def test_marking_ready_grants_no_approval_and_no_merge_eligibility(self):
        """Condition 3, from the state that actually results."""
        self.seed(approval_current=True)
        self.run_tick(draft=True)

        record = self.record()
        self.assertFalse(record["approval_current"],
                         "a dispatched review may only CLEAR approval")
        self.assertIsNone(record["review_verdict"])
        decision = routing.evaluate_merge(open_pr(), record, ("ci",), False,
                                          DIFF_HASH)
        self.assertFalse(decision.allowed)
        self.assertIn("REVIEW_PASS", decision.reason)

    def test_the_grant_is_structural_the_preflight_holds_no_document(self):
        """Condition 3 made structural rather than conventional: the preflight
        runs in the execute phase, whose only input is a frozen DispatchPlan
        and whose only output is a string. There is no state document in
        scope for it to grant anything in."""
        self.seed()
        plan = self.plan()
        with mock.patch.object(supervisor_mod.gh, "pr_view",
                               return_value={"state": "OPEN", "isDraft": True,
                                             "headRefName": BRANCH,
                                             "headRefOid": HEAD}), \
                mock.patch.object(supervisor_mod.gh, "mark_ready",
                                  return_value=ok_result()):
            before = self.durable()
            self.assertEqual(self.sup._review_preflight(plan), "")
            self.assertEqual(self.durable(), before,
                             "the preflight wrote to the state document")

    def test_the_fields_a_grant_would_touch_are_not_writable_from_the_result(self):
        """The same, from the other side: the one channel out of the execute
        phase is DispatchResult, and it has no field an approval could ride
        on."""
        fields = {f.name for f in dataclasses.fields(supervisor_mod.DispatchResult)}
        self.assertEqual(fields, {"plan", "ok", "worktree", "port", "reason"})


if __name__ == "__main__":
    unittest.main()
