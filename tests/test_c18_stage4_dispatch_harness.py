"""C-18 stage 4: the shared dispatch harness, and the builder migrated onto it.

C-18's finding is that dispatch performed its external work - prompt file,
workmux/git subprocess, worktree path resolution, a real 127.0.0.1 bind, the
job file and the process spawn - while the Supervisor held its one exclusive
state transaction (T1). One slow subprocess therefore stalled every task in
the run.

This file proves two things, and is careful to keep them apart:

  1. THE HARNESS. A durable dispatch claim, an accumulator that lets a plan
     escape a `reap_workers` callback, a role-keyed handler table, execution
     with no lock held, per-result commits, and recovery driven by the job
     file. Stages 5 (reviewer) and 6 (fixer) add one table entry each; these
     tests pin the contract they will build against.

  2. THE BUILDER, as the harness's first consumer. Every external call has
     left T1 - established by probing the REAL `flock` at the moment of each
     call, with a companion test proving the probe would catch a violation -
     and the commit-side state still commits exactly once per real dispatch.

The hazard this stage carries is C-15: `on_builder_dispatch_failure` may only
auto-release the migration lock for a FRESH owner, and once a failure can be
handled after T1 has committed, "fresh" can no longer be a boolean remembered
from the acquiring transaction. `TheMigrationLockDecisionIsRederived` is the
evidence that it is rebuilt from the committed document instead.

Everything runs against a real `state.Store` on a temporary path, with a real
worker-log directory and real job files. Nothing launches a worker, nothing
makes a paid call and nothing reaches the network - the only socket operation
is a loopback bind on a port this test first proved was free.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import socket
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
SHA = "a" * 40


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


def free_port() -> int:
    """A port nothing holds right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ok_result():
    return mock.Mock(ok=True, stdout="", stderr="", code=0)


def fail_result(stderr="boom"):
    return mock.Mock(ok=False, stdout="", stderr=stderr, code=1)


class HarnessCase(unittest.TestCase):
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
        self.sup = self.supervisor()
        self.worktree = self.root / "wt"
        self.worktree.mkdir()

    def supervisor(self):
        """A Supervisor as a fresh process would build one.

        Used twice on purpose: a crash test needs a second Supervisor that
        shares nothing in memory with the first, so that anything it
        recovers it recovered from durable state.
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

    def seed(self, *, state="READY", schema_changing=False, write=True) -> dict:
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "Build the thing", [],
                                  "feature", schema_changing, TZ)
        task["state"] = state
        migration_lock.ensure(doc)
        if write:
            self.store._write(doc)
        return doc

    def seed_pr_task(self, *, number=100, state="PR_OPEN") -> dict:
        doc = self.seed(write=False)
        task = doc["tasks"]["TASK-001"]
        task.update({"state": state, "branch": "task/task-001", "pr": number})
        doc["prs"][str(number)] = routing.blank_pr_record(
            number, "TASK-001", "task/task-001")
        self.store._write(doc)
        return doc

    def claim(self, doc, *, role="builder", pr=None, worker="task-001-builder",
              claim_state=state_mod.DISPATCH_PLANNED) -> dict:
        task = doc["tasks"]["TASK-001"]
        record = state_mod.new_dispatch_claim(
            role=role, task_id="TASK-001", worker=worker,
            branch="task/task-001", claimed_at=clock.iso(clock.now(TZ)),
            lease_expires_at=self.sup._lease_expires("builder"),
            pr=pr, port_candidates=[39110])
        record["claim_state"] = claim_state
        state_mod.dispatch_claims(task)[role] = record
        return record

    # ------------------------------------------------------------- driving

    def run_tick(self, sup=None, *, create=True, path=True, start=True,
                 probe=True, real_probe=False, port_range=None,
                 skip_execute=False, skip_confirm=False, entries=None):
        """One real tick, recording whether the state lock was held at the
        moment of every external dispatch call.

        `skip_execute` and `skip_confirm` simulate a process that died in the
        corresponding window: the state that was already committed stays, and
        nothing in memory survives into the next Supervisor.
        """
        sup = sup or self.sup
        self.calls: list[tuple[str, bool]] = []

        def spy(name, answer):
            def inner(*_a, **_kw):
                self.calls.append((name, lock_is_held(self.store.lock_path)))
                return answer() if callable(answer) else answer
            return inner

        patches = [
            mock.patch.object(supervisor_mod.gh, "list_open_prs", return_value=[]),
            mock.patch.object(supervisor_mod.workers, "read_status",
                              return_value=None),
            mock.patch.object(supervisor_mod.proc, "worker_entry_processes",
                              return_value={} if entries is None else entries),
            mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                              self.heartbeat_path),
            mock.patch.object(supervisor_mod.prompts, "write",
                              side_effect=spy("prompts.write",
                                              lambda: self.root / "p.md")),
            mock.patch.object(supervisor_mod.workers, "create_worker",
                              side_effect=spy("create_worker",
                                              ok_result if create else fail_result)),
            mock.patch.object(supervisor_mod.workers, "worktree_path",
                              side_effect=spy("worktree_path",
                                              lambda: self.worktree if path else None)),
            mock.patch.object(supervisor_mod.workers, "start_job",
                              side_effect=spy("start_job",
                                              ok_result if start else fail_result)),
        ]
        if real_probe:
            # The genuine article: workers.probe_port is NOT patched, so a
            # real 127.0.0.1 bind happens. A spy socket records the lock at
            # the instant of the bind itself.
            patches.append(mock.patch.object(supervisor_mod.workers, "socket",
                                             self.spy_socket_module()))
            patches.append(mock.patch.object(
                supervisor_mod.workers.hostcheck, "read_candidate_port_range",
                return_value=port_range))
        else:
            patches.append(mock.patch.object(
                supervisor_mod.workers, "probe_port",
                side_effect=spy("probe_port", probe)))
            patches.append(mock.patch.object(
                supervisor_mod.workers, "select_port_candidates",
                return_value=([39110], "range exhausted")))
        if skip_execute:
            patches.append(mock.patch.object(sup, "execute_dispatches",
                                             return_value=[]))
        if skip_confirm:
            patches.append(mock.patch.object(sup, "confirm_dispatches"))

        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            sup.tick()

    def spy_socket_module(self):
        """A `socket` stand-in whose bind records the real lock state."""
        harness = self

        class SpySocket:
            def __init__(self, *args, **kwargs):
                self._real = socket.socket(*args, **kwargs)

            def bind(self, address):
                harness.calls.append(
                    ("bind", lock_is_held(harness.store.lock_path)))
                return self._real.bind(address)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._real.close()
                return False

            def __getattr__(self, name):
                return getattr(self._real, name)

        module = mock.Mock(wraps=socket)
        module.socket = SpySocket
        module.AF_INET = socket.AF_INET
        module.SOCK_STREAM = socket.SOCK_STREAM
        return module

    # ------------------------------------------------------------- helpers

    def durable(self) -> dict:
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def task(self) -> dict:
        return self.durable()["tasks"]["TASK-001"]

    def durable_claim(self, role="builder"):
        return state_mod.dispatch_claim(self.task(), role)

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def event(self, name: str) -> dict:
        found = [fields for event, fields in self.events if event == name]
        self.assertTrue(found, f"no {name} event in {self.names()}")
        return found[0]

    def plan(self, **context) -> supervisor_mod.DispatchPlan:
        return supervisor_mod.DispatchPlan(
            role="builder", task_id="TASK-001", pr=None,
            worker="task-001-builder", branch="task/task-001",
            port_candidates=(39110,), observed_head=None,
            context=dict(context))


# ================================================== the transaction boundary


class NoExternalCallHappensUnderTheStateLock(HarnessCase):
    """The C-18 guarantee for the builder path, measured against the real
    flock rather than read off the source."""

    def test_every_external_dispatch_call_is_made_with_the_lock_free(self):
        self.seed()
        self.run_tick()

        self.assertTrue(self.calls,
                        "the test proves nothing if nothing external ran")
        self.assertEqual(
            sorted({name for name, _ in self.calls}),
            ["create_worker", "probe_port", "prompts.write", "start_job",
             "worktree_path"],
            "every external call dispatch used to make inside T1 must appear")
        self.assertEqual([held for _, held in self.calls],
                         [False] * len(self.calls),
                         f"an external call ran under the lock: {self.calls}")

    def test_the_real_port_bind_also_happens_with_the_lock_free(self):
        """probe_port is NOT mocked here: a genuine 127.0.0.1 bind runs."""
        port = free_port()
        self.seed()
        self.run_tick(real_probe=True, port_range=(port, port))

        binds = [held for name, held in self.calls if name == "bind"]
        self.assertTrue(binds, "no real bind happened; the test is vacuous")
        self.assertEqual(binds, [False] * len(binds))

    def test_the_lock_probe_can_actually_detect_a_held_lock(self):
        """Without this, the tests above could pass because the probe is
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
                supervisor_mod.workers, "create_worker",
                side_effect=lambda *a, **k: self.calls.append(
                    ("create_worker", lock_is_held(self.store.lock_path)))):
            with self.store.transaction():
                supervisor_mod.workers.create_worker("w", "b", Path("/p"), "s")
        self.assertEqual(self.calls, [("create_worker", True)])

    def test_the_planning_half_touches_no_file_and_no_subprocess(self):
        """T1's own guarantee, stated separately from the tick-wide one: the
        claim is made without any of the moved calls happening at all."""
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod, "prompts") as prompts, \
                mock.patch.object(supervisor_mod.workers, "create_worker") as create, \
                mock.patch.object(supervisor_mod.workers, "worktree_path") as path, \
                mock.patch.object(supervisor_mod.workers, "probe_port") as probe, \
                mock.patch.object(supervisor_mod.workers, "write_job") as job, \
                mock.patch.object(supervisor_mod.workers, "start_job") as start, \
                mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                                  return_value=([39110], "")):
            self.sup.dispatch_builder(doc, doc["tasks"]["TASK-001"])
        self.assertEqual(prompts.mock_calls, [])
        for spy in (create, path, probe, job, start):
            spy.assert_not_called()
        self.assertTrue(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                 "builder"))


# ================================================== the claim record itself


class TheDispatchClaimRecord(unittest.TestCase):

    def good(self, **overrides) -> dict:
        kwargs = dict(role="builder", task_id="TASK-001",
                      worker="task-001-builder", branch="task/task-001",
                      claimed_at="2026-10-01T10:00:00+13:00",
                      lease_expires_at="2026-10-01T11:00:00+13:00")
        kwargs.update(overrides)
        return state_mod.new_dispatch_claim(**kwargs)

    def test_it_carries_every_field_the_harness_contract_names(self):
        claim = self.good(pr=7, port_candidates=[39110, 39111],
                          observed_head=SHA, context={"cycle": 2})
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(claim["task_id"], "TASK-001")
        self.assertEqual(claim["pr"], 7)
        self.assertEqual(claim["worker"], "task-001-builder")
        self.assertEqual(claim["branch"], "task/task-001")
        self.assertEqual(claim["port_candidates"], [39110, 39111])
        self.assertEqual(claim["observed_head"], SHA)
        self.assertEqual(claim["claim_state"], state_mod.DISPATCH_PLANNED)
        self.assertEqual(claim["lease_expires_at"],
                         "2026-10-01T11:00:00+13:00")
        self.assertEqual(claim["context"], {"cycle": 2})

    def test_the_context_is_copied_not_aliased(self):
        context = {"cycle": 2}
        claim = self.good(context=context)
        context["cycle"] = 99
        self.assertEqual(claim["context"], {"cycle": 2})

    def test_it_survives_a_json_round_trip_unchanged(self):
        """It lives in state.json, so anything it cannot serialise is a
        claim that would be silently mangled by the next commit."""
        claim = self.good(pr=7, port_candidates=[39110], observed_head=SHA,
                          context={"lock_newly_acquired": True})
        self.assertEqual(json.loads(json.dumps(claim)), claim)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(ValueError):
            self.good(role="observer")

    def test_a_worker_name_that_is_not_one_path_component_is_refused(self):
        for bad in ("../escape", "has space", "UPPER", "", "a/b"):
            with self.subTest(worker=bad), self.assertRaises(ValueError):
                self.good(worker=bad)

    def test_a_lease_that_does_not_outlast_the_claim_is_refused(self):
        with self.assertRaises(ValueError):
            self.good(lease_expires_at="2026-10-01T10:00:00+13:00")

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            self.good(claimed_at="2026-10-01T10:00:00")

    def test_a_short_or_uppercase_head_is_refused(self):
        for bad in ("a" * 39, "A" * 40, "g" * 40):
            with self.subTest(head=bad), self.assertRaises(ValueError):
                self.good(observed_head=bad)

    def test_a_non_positive_pr_is_refused(self):
        for bad in (0, -1, True):
            with self.subTest(pr=bad), self.assertRaises(ValueError):
                self.good(pr=bad)

    def test_non_integer_port_candidates_are_refused(self):
        with self.assertRaises(ValueError):
            self.good(port_candidates=["39110"])

    def test_planned_and_spawned_are_active_and_nothing_else_is(self):
        self.assertTrue(state_mod.dispatch_claim_active(
            {"claim_state": state_mod.DISPATCH_PLANNED}))
        self.assertTrue(state_mod.dispatch_claim_active(
            {"claim_state": state_mod.DISPATCH_SPAWNED}))
        for inert in ({"claim_state": "COMPLETE"}, {}, None, "PLANNED", 7):
            with self.subTest(claim=inert):
                self.assertFalse(state_mod.dispatch_claim_active(inert))


# ========================================================== the harness API


class TheHarnessContract(HarnessCase):
    """What stages 5 and 6 build against."""

    def test_the_handler_table_is_role_keyed_and_names_three_methods(self):
        handler = self.sup.DISPATCH_HANDLERS["builder"]
        for name in (handler.execute, handler.commit, handler.fail):
            self.assertTrue(callable(getattr(self.sup, name)), name)

    def test_a_role_with_no_handler_is_held_rather_than_crashing(self):
        # "observer" is a real role that is deliberately never dispatched
        # through this harness, so this stays an unhandled role however many
        # stages register themselves. (Stage 5 took "reviewer", which is what
        # this test originally used.)
        plan = supervisor_mod.DispatchPlan(
            role="observer", task_id="TASK-001", pr=7, worker="task-001-observer-1",
            branch="task/task-001")
        self.seed()
        results = self.sup.execute_dispatches([plan], snapshot=self.durable())
        self.assertEqual(results, [])
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "NO_HANDLER")

    def test_plan_dispatch_accumulates_and_escapes_a_reap_callback(self):
        """Reviewer and fixer plans originate inside reap_workers' callbacks,
        which are nested in T1 and cannot return anything to tick(). This is
        the property that lets them out."""
        self.seed()
        escaped = self.plan()

        def finished(doc, worker, meta, status):
            self.sup._plan_dispatch(escaped)

        self.sup._dispatch_plans = []
        with self.store.transaction() as doc:
            doc["workers"]["w1"] = state_mod.new_worker_record(
                "builder", "TASK-001", "task/task-001",
                clock.iso(clock.now(TZ)))
            with mock.patch.object(supervisor_mod.workers, "read_status",
                                   return_value={"phase": "DONE"}), \
                    mock.patch.object(self.sup, "on_builder_finished", finished):
                self.sup.reap_workers(doc)
        self.assertEqual(self.sup._dispatch_plans, [escaped])

    def test_the_plan_carries_no_reference_into_the_transaction(self):
        doc = self.seed(write=False)
        claim = self.claim(doc)
        plan = self.sup.dispatch_plan(claim)
        claim["branch"] = "mutated"
        claim["context"]["injected"] = True
        self.assertEqual(plan.branch, "task/task-001")
        self.assertNotIn("injected", plan.context)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.worker = "other"

    def test_each_result_is_committed_in_its_own_transaction(self):
        """A fail-closed abort on one task must not discard another task's
        commit - that other task's worker has already been spawned."""
        doc = self.seed(write=False)
        state_mod.add_task(doc, "TASK-002", "Second", [], "feature", False, TZ)
        doc["tasks"]["TASK-002"]["state"] = "READY"
        self.store._write(doc)

        good = self.plan()
        bad = supervisor_mod.DispatchPlan(
            role="builder", task_id="TASK-002", pr=None,
            worker="task-002-builder", branch="task/task-002")
        with self.store.transaction() as live:
            self.claim(live)
            state_mod.dispatch_claims(live["tasks"]["TASK-002"])["builder"] = \
                state_mod.new_dispatch_claim(
                    role="builder", task_id="TASK-002",
                    worker="task-002-builder", branch="task/task-002",
                    claimed_at=clock.iso(clock.now(TZ)),
                    lease_expires_at=self.sup._lease_expires("builder"))

        real_commit = self.sup._commit_builder_dispatch

        def commit(doc_, plan_, result_):
            if plan_.task_id == "TASK-002":
                raise RuntimeError("fail closed")
            return real_commit(doc_, plan_, result_)

        with mock.patch.object(self.sup, "_commit_builder_dispatch", commit):
            with self.assertRaises(RuntimeError):
                self.sup.confirm_dispatches([
                    supervisor_mod.DispatchResult(
                        plan=good, ok=True, worktree=str(self.worktree),
                        port=39110),
                    supervisor_mod.DispatchResult(
                        plan=bad, ok=True, worktree=str(self.worktree),
                        port=39111),
                ])
        self.assertIn("task-001-builder", self.durable()["workers"])
        self.assertNotIn("task-002-builder", self.durable()["workers"])

    def test_apply_routes_success_to_commit_and_failure_to_fail(self):
        doc = self.seed(write=False)
        self.claim(doc)
        with mock.patch.object(self.sup, "_commit_builder_dispatch") as commit, \
                mock.patch.object(self.sup, "_fail_builder_dispatch") as fail:
            self.sup.apply_dispatch_result(
                doc, supervisor_mod.DispatchResult(plan=self.plan(), ok=True))
            commit.assert_called_once()
            fail.assert_not_called()
            self.sup.apply_dispatch_result(
                doc, supervisor_mod.DispatchResult(plan=self.plan(), ok=False,
                                                   reason="nope"))
            fail.assert_called_once()

    def test_a_superseded_claim_cannot_be_committed(self):
        doc = self.seed(write=False)
        self.claim(doc, worker="task-001-builder")
        plan = self.plan()
        state_mod.dispatch_claims(doc["tasks"]["TASK-001"])["builder"][
            "worker"] = "someone-else"
        self.sup._commit_builder_dispatch(
            doc, plan, supervisor_mod.DispatchResult(
                plan=plan, ok=True, worktree=str(self.worktree), port=39110))
        self.assertEqual(doc["workers"], {})
        self.assertEqual(self.event("DISPATCH_COMMIT_REFUSED")["outcome"],
                         "CLAIM_SUPERSEDED")


class ControlsAreReReadBeforeAnythingSpawns(HarnessCase):
    """T1 has committed and released the lock by the time execution runs, so
    the decision that matters is the one true at the instant a worker would
    actually start."""

    def held(self, mutate) -> list:
        doc = self.seed(write=False)
        mutate(doc)
        self.store._write(doc)
        return self.sup.execute_dispatches([self.plan()], snapshot=doc)

    def test_a_freeze_arriving_after_t1_stops_the_spawn(self):
        self.assertEqual(
            self.held(lambda d: d.update({"frozen_at": clock.iso(clock.now(TZ))})),
            [])
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "RUN_FROZEN")

    def test_a_paused_provider_policy_stops_the_spawn(self):
        with mock.patch.object(supervisor_mod.providers, "may",
                               return_value=False):
            self.assertEqual(self.held(lambda d: None), [])
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "PROVIDER_PAUSED")

    def test_a_safe_hold_stops_the_builder_spawn(self):
        with mock.patch.object(supervisor_mod.providers, "safe_hold",
                               return_value=True):
            self.assertEqual(self.held(lambda d: None), [])
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "SAFE_HOLD")

    def test_unreadable_controls_block_rather_than_permit(self):
        self.assertEqual(self.sup._dispatch_block({}, "builder"),
                         "CONTROLS_UNREADABLE")

    def test_a_stop_signal_stops_the_spawn(self):
        self.seed()
        self.sup.stopping = True
        self.assertEqual(
            self.sup.execute_dispatches([self.plan()], snapshot=self.durable()),
            [])
        self.assertEqual(self.event("DISPATCH_EXECUTION_HELD")["outcome"],
                         "SUPERVISOR_STOPPING")

    def test_a_held_plan_leaves_its_claim_exactly_as_it_was(self):
        doc = self.seed(write=False)
        before = dict(self.claim(doc))
        self.store._write(doc)
        self.sup.stopping = True
        self.sup.execute_dispatches([self.plan()], snapshot=doc)
        self.assertEqual(self.durable_claim(), before)


# ============================================== the builder, end to end


class TheBuilderCommitsExactlyWhatItUsedTo(HarnessCase):

    def test_a_successful_tick_dispatches_exactly_one_builder(self):
        self.seed()
        self.run_tick()
        task = self.task()
        self.assertEqual(task["state"], "ASSIGNED")
        self.assertEqual(task["worker"], "task-001-builder")
        self.assertEqual(task["branch"], "task/task-001")
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["progress_marker"], 0)
        self.assertTrue(task["assigned_at"])
        record = self.durable()["workers"]["task-001-builder"]
        self.assertEqual(record["role"], "builder")
        self.assertEqual(record["worktree"], str(self.worktree))
        self.assertEqual(record["port"], 39110)
        self.assertTrue(record["lease_expires_at"])
        self.assertIsNone(self.durable_claim())

    def test_the_retained_worktree_claim_is_still_popped(self):
        doc = self.seed(write=False)
        doc["tasks"]["TASK-001"]["retained_worktrees"] = {
            str(self.worktree): {"why": "TERMINAL_REAP"}}
        self.store._write(doc)
        self.run_tick()
        self.assertEqual(self.task()["retained_worktrees"], {})

    def test_a_failed_spawn_leaves_no_worker_record_and_no_claim(self):
        self.seed()
        self.run_tick(start=False)
        self.assertEqual(self.durable()["workers"], {})
        self.assertIsNone(self.durable_claim())
        self.assertEqual(self.task()["state"], "BLOCKED")
        self.assertEqual(self.task()["attempts"], 0)

    def test_attempts_increments_only_on_a_real_dispatch(self):
        self.seed()
        self.run_tick(create=False)
        self.assertEqual(self.task()["attempts"], 0)

    def test_an_unbindable_candidate_set_fails_the_dispatch(self):
        self.seed()
        self.run_tick(probe=False)
        self.assertEqual(self.task()["state"], "BLOCKED")
        self.assertEqual(self.event("PORT_ALLOCATION_FAILED")["role"], "builder")

    def test_an_empty_candidate_list_fails_inside_the_transaction(self):
        """The one failure path that never leaves T1: selection found
        nothing, so nothing is ever claimed or executed."""
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                               return_value=([], "range exhausted")):
            self.sup._dispatch_plans = []
            self.sup.dispatch_builder(doc, doc["tasks"]["TASK-001"])
        self.assertEqual(self.sup._dispatch_plans, [])
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "builder"))
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "BLOCKED")

    def test_the_claim_blocks_a_second_plan_for_the_same_task(self):
        doc = self.seed(write=False)
        self.claim(doc)
        self.sup._dispatch_plans = []
        with mock.patch.object(supervisor_mod.workers,
                               "select_port_candidates") as select:
            self.sup.dispatch_builder(doc, doc["tasks"]["TASK-001"])
        select.assert_not_called()
        self.assertEqual(self.sup._dispatch_plans, [])

    def test_a_builder_claim_consumes_a_governed_concurrency_slot(self):
        """max_builders must bound claims as well as committed dispatches,
        or it stops being a limit."""
        doc = self.seed(write=False)
        for index in range(2, 2 + self.cfg.max_builders):
            extra = state_mod.add_task(doc, f"TASK-{index:03d}", "t", [],
                                       "feature", False, TZ)
            extra["state"] = "READY"
        self.claim(doc)
        cs = self.sup.clock_state(doc)
        offered = [t["id"] for t in self.sup.dispatchable(doc, cs)]
        self.assertNotIn("TASK-001", offered)
        self.assertEqual(len(offered), max(0, self.cfg.max_builders - 1))


# ============================================ crash recovery, both windows


class ACrashBetweenPlanAndSpawnIsRecovered(HarnessCase):

    def test_the_claim_is_durable_and_nothing_was_dispatched(self):
        self.seed()
        self.run_tick(skip_execute=True)
        claim = self.durable_claim()
        self.assertIsNotNone(claim, "the claim must survive the crash")
        self.assertEqual(claim["claim_state"], state_mod.DISPATCH_PLANNED)
        self.assertEqual(claim["worker"], "task-001-builder")
        self.assertEqual(claim["port_candidates"], [39110])
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.task()["attempts"], 0)
        self.assertEqual(self.task()["state"], "READY")

    def test_a_fresh_supervisor_resumes_it_and_dispatches_once(self):
        self.seed()
        self.run_tick(skip_execute=True)
        before = dict(self.durable_claim())

        restarted = self.supervisor()
        self.assertEqual(restarted._dispatch_plans, [],
                         "nothing may survive in memory")
        self.run_tick(restarted)

        self.assertEqual(self.event("DISPATCH_RESUMED")["outcome"], "REPLANNED")
        self.assertEqual(self.task()["attempts"], 1,
                         "exactly one attempt for one real dispatch")
        self.assertEqual(self.task()["state"], "ASSIGNED")
        record = self.durable()["workers"]["task-001-builder"]
        self.assertEqual(record["worktree"], str(self.worktree))
        self.assertIsNone(self.durable_claim())
        # Identity came from the committed claim, not a fresh derivation.
        self.assertEqual(self.task()["worker"], before["worker"])
        self.assertEqual(self.task()["branch"], before["branch"])

    def test_the_resumed_dispatch_reuses_the_claimed_identity(self):
        doc = self.seed(write=False)
        claim = self.claim(doc)
        claim["worker"] = "task-001-builder"
        claim["branch"] = "task/renamed"
        self.store._write(doc)
        self.run_tick()
        self.assertEqual(self.task()["branch"], "task/renamed",
                         "the committed claim, not a fresh derivation, "
                         "decides the identity")


class ACrashBetweenSpawnAndCommitIsRecovered(HarnessCase):
    """The job file is the spawn evidence, exactly as it is in C-05.3a."""

    def crash_after_spawn(self):
        self.seed()
        self.run_tick(skip_confirm=True)

    def test_the_job_file_exists_and_nothing_was_committed(self):
        self.crash_after_spawn()
        self.assertTrue((self.job_dir / "task-001-builder.job.json").exists())
        self.assertEqual(self.durable()["workers"], {})
        self.assertIsNotNone(self.durable_claim())

    def test_a_fresh_supervisor_commits_it_without_spawning_again(self):
        self.crash_after_spawn()
        restarted = self.supervisor()
        self.run_tick(restarted)

        starts = [name for name, _ in self.calls if name == "start_job"]
        self.assertEqual(starts, [],
                         "a worker that already spawned must never spawn again")
        self.assertEqual(self.event("DISPATCH_RECOVERED")["outcome"],
                         "SPAWN_EVIDENCE_FOUND")
        record = self.durable()["workers"]["task-001-builder"]
        self.assertEqual(record["worktree"], str(self.worktree))
        self.assertEqual(record["port"], 39110)
        self.assertEqual(self.task()["attempts"], 1)
        self.assertIsNone(self.durable_claim())

    def test_recovery_reads_the_worktree_and_port_from_the_job_file(self):
        self.crash_after_spawn()
        job_path = self.job_dir / "task-001-builder.job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job.update({"worktree": "/elsewhere", "port": 39119})
        job_path.write_text(json.dumps(job), encoding="utf-8")
        self.run_tick(self.supervisor())
        record = self.durable()["workers"]["task-001-builder"]
        self.assertEqual(record["worktree"], "/elsewhere")
        self.assertEqual(record["port"], 39119)

    def test_a_claim_with_no_observation_is_left_exactly_as_it_is(self):
        doc = self.seed(write=False)
        before = dict(self.claim(doc))
        self.store._write(doc)
        with self.store.transaction() as live:
            self.sup.resume_dispatch_claims(live, {})
        self.assertEqual(self.durable_claim(), before)
        self.assertEqual(self.event("DISPATCH_RESUME_DEFERRED")["outcome"],
                         "OBSERVATION_MISSING")

    def test_committing_twice_cannot_double_count_attempts(self):
        self.crash_after_spawn()
        self.run_tick(self.supervisor())
        self.assertEqual(self.task()["attempts"], 1)
        # A second recovery pass over the same already-committed worker.
        plan = self.plan()
        with self.store.transaction() as doc:
            self.sup.apply_dispatch_result(
                doc, supervisor_mod.DispatchResult(
                    plan=plan, ok=True, worktree=str(self.worktree), port=39110))
        self.assertEqual(self.task()["attempts"], 1)

    def test_the_observation_is_bound_to_the_claims_own_worker(self):
        doc = self.seed(write=False)
        self.claim(doc)
        self.store._write(doc)
        observations = {("TASK-001", "builder"): supervisor_mod.DispatchObservation(
            task_id="TASK-001", role="builder", worker="someone-else",
            job_file=True)}
        with self.store.transaction() as live:
            self.sup.resume_dispatch_claims(live, observations)
        self.assertIsNotNone(self.durable_claim())
        self.assertEqual(self.durable()["workers"], {})


# ================================= a claim stops a second dispatch going out


class AClaimReservesTheRoleSlot(HarnessCase):

    def test_has_worker_sees_a_claim_with_no_worker_record(self):
        doc = self.seed_pr_task()
        self.claim(doc, role="reviewer", pr=100, worker="task-001-review-1")
        self.assertTrue(self.sup.has_worker(doc, 100, ("reviewer", "fixer")))

    def test_route_awaiting_dispatch_will_not_dispatch_a_second_reviewer(self):
        doc = self.seed_pr_task()
        self.claim(doc, role="reviewer", pr=100, worker="task-001-review-1")
        with mock.patch.object(self.sup, "dispatch_reviewer") as reviewer, \
                mock.patch.object(self.sup, "dispatch_fixer") as fixer:
            self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], 100,
                                             doc["prs"]["100"])
        reviewer.assert_not_called()
        fixer.assert_not_called()

    def test_route_awaiting_dispatch_will_not_dispatch_a_second_fixer(self):
        doc = self.seed_pr_task(state="FIX_REQUIRED")
        doc["prs"]["100"].update({"review_verdict": routing.REVIEW_FAIL,
                                  "pending_findings": [{"id": "F1"}]})
        self.claim(doc, role="fixer", pr=100, worker="task-001-fixer-1")
        with mock.patch.object(self.sup, "dispatch_fixer") as fixer:
            self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], 100,
                                             doc["prs"]["100"])
        fixer.assert_not_called()

    def test_without_the_claim_the_same_task_is_dispatched(self):
        """The companion: the test above must fail for the claim's sake, not
        because routing refuses this fixture anyway."""
        doc = self.seed_pr_task()
        with mock.patch.object(self.sup, "dispatch_reviewer") as reviewer:
            self.sup.route_awaiting_dispatch(doc, doc["tasks"]["TASK-001"], 100,
                                             doc["prs"]["100"])
        reviewer.assert_called_once()

    def test_a_claim_on_another_pull_request_does_not_block_this_one(self):
        doc = self.seed_pr_task()
        self.claim(doc, role="reviewer", pr=999, worker="task-001-review-1")
        self.assertFalse(self.sup.has_worker(doc, 100, ("reviewer", "fixer")))

    def test_a_claim_in_another_role_does_not_block(self):
        doc = self.seed_pr_task()
        self.claim(doc, role="builder", pr=None)
        self.assertFalse(self.sup.has_worker(doc, 100, ("reviewer", "fixer")))

    def test_an_inactive_claim_does_not_block(self):
        doc = self.seed_pr_task()
        claim = self.claim(doc, role="reviewer", pr=100,
                           worker="task-001-review-1")
        claim["claim_state"] = "COMPLETE"
        self.assertFalse(self.sup.has_worker(doc, 100, ("reviewer", "fixer")))

    def test_a_live_worker_record_still_blocks(self):
        doc = self.seed_pr_task()
        doc["workers"]["task-001-review-1"] = state_mod.new_worker_record(
            "reviewer", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            pr=100)
        self.assertTrue(self.sup.has_worker(doc, 100, ("reviewer", "fixer")))


# ===================================================== the C-15 hazard


class TheMigrationLockDecisionIsRederived(HarnessCase):
    """C-15's fresh-vs-re-entrant branch, after a failure that is handled in
    a LATER transaction than the one that acquired the lock.

    Before C-18 this read a local boolean from a few statements earlier in
    the same transaction. That boolean describes a document which, by the
    time a post-commit failure is handled, no longer exists.
    """

    def failing_tick(self, *, schema_changing=True):
        self.seed(schema_changing=schema_changing)
        self.run_tick(start=False)

    def test_a_genuinely_fresh_owner_is_released_with_its_marker(self):
        self.failing_tick()
        doc = self.durable()
        self.assertEqual(doc["migration_lock"]["state"], migration_lock.FREE)
        self.assertIsNone(doc["migration_lock"]["owner_task"])
        marker = doc["tasks"]["TASK-001"]["migration_lock_auto_release"]
        self.assertEqual(marker["why"], "DISPATCH_FAILED_BEFORE_BUILDER_STARTED")
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "BLOCKED")
        self.assertEqual(doc["interventions"], {})

    def test_a_reentrant_owner_is_never_released(self):
        doc = self.seed(schema_changing=True, write=False)
        self.assertTrue(migration_lock.acquire(doc, "TASK-001", TZ))
        self.store._write(doc)
        self.run_tick(start=False)
        after = self.durable()
        self.assertEqual(after["migration_lock"]["owner_task"], "TASK-001")
        self.assertEqual(after["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertNotIn("migration_lock_auto_release", after["tasks"]["TASK-001"])

    def base_lock(self, acquired_at="2026-10-01T10:00:00+13:00"):
        return {"state": migration_lock.HELD, "owner_task": "TASK-001",
                "owner_pr": None, "acquired_at": acquired_at, "waiters": [],
                "contention_events": 0}

    def permitted(self, *, lock=None, task=None, **context) -> bool:
        doc = {"migration_lock": lock if lock is not None else self.base_lock(),
               "tasks": {"TASK-001": task if task is not None else {}}}
        full = {"lock_newly_acquired": True,
                "lock_acquired_at": "2026-10-01T10:00:00+13:00"}
        full.update(context)
        return self.sup._migration_lock_release_permitted(doc, self.plan(**full))

    def test_the_fresh_case_is_permitted(self):
        self.assertTrue(self.permitted())

    def test_a_plan_that_inherited_the_lock_is_refused(self):
        self.assertFalse(self.permitted(lock_newly_acquired=False))

    def test_a_different_owner_is_refused(self):
        lock = self.base_lock()
        lock["owner_task"] = "TASK-009"
        self.assertFalse(self.permitted(lock=lock))

    def test_a_different_ownership_episode_is_refused(self):
        """Released and re-acquired between plan and commit: acquired_at
        moved, so this plan knows nothing about the episode it would be
        releasing."""
        self.assertFalse(self.permitted(lock=self.base_lock(
            "2026-10-01T10:30:00+13:00")))

    def test_a_released_lock_is_refused(self):
        lock = self.base_lock(None)
        lock.update({"state": migration_lock.FREE, "owner_task": None})
        self.assertFalse(self.permitted(lock=lock))

    def test_a_builder_dispatched_under_this_episode_refuses_release(self):
        """The literal statement of C-15's ground, read from durable state:
        assigned_at is written only by a committed builder dispatch."""
        self.assertFalse(self.permitted(
            task={"assigned_at": "2026-10-01T10:05:00+13:00"}))

    def test_a_builder_dispatched_before_this_episode_still_permits_release(self):
        self.assertTrue(self.permitted(
            task={"assigned_at": "2026-10-01T09:55:00+13:00"}))

    def test_an_unparseable_timestamp_refuses_release(self):
        self.assertFalse(self.permitted(task={"assigned_at": "not a time"}))

    def test_a_missing_acquisition_moment_refuses_release(self):
        self.assertFalse(self.permitted(lock_acquired_at=None))

    def test_the_comparison_survives_a_daylight_saving_offset_change(self):
        """Pacific/Auckland changes offset twice a year. Lexicographically
        02:10+12:00 sorts before 02:30+13:00 while being LATER in absolute
        time, so a string comparison here would permit a release it must
        refuse."""
        self.assertFalse(self.permitted(
            lock_acquired_at="2026-04-05T02:30:00+13:00",
            lock=self.base_lock("2026-04-05T02:30:00+13:00"),
            task={"assigned_at": "2026-04-05T02:10:00+12:00"}))

    def test_a_failure_for_a_task_that_vanished_changes_nothing(self):
        doc = self.seed(write=False)
        plan = self.plan(lock_newly_acquired=True, lock_acquired_at="x")
        doc["tasks"].clear()
        self.sup._fail_builder_dispatch(
            doc, plan, supervisor_mod.DispatchResult(plan=plan, ok=False,
                                                     reason="nope"))
        self.assertEqual(self.event("DISPATCH_FAILURE_REFUSED")["outcome"],
                         "TASK_GONE")


if __name__ == "__main__":
    unittest.main()
