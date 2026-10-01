"""C-18 stage 6: the fixer migrated onto the shared dispatch harness.

Stage 4 built the harness and moved the builder onto it. The fixer was
sequenced LAST, and the plan of record names the reason:

    `workers.acquire_worktree(..., reuse_if_checked_out=True)` REUSES an
    existing checkout.

The builder creates a private worktree named after its own worker; nothing
else can be in it. The fixer deliberately does the opposite - it commits to
the Builder's PR branch, and git allows a branch in exactly one worktree, so
it works in whichever worktree already holds that branch. That worktree is
SHARED MUTABLE STATE. Before C-18 the claim that authorised using it was
taken in the same transaction as the spawn, so it could not go stale between
the two. Splitting plan from execute opens that window, and
`TheReusedWorktreeHazard` below is the evidence it is closed: the claim is
re-verified against the committed document immediately before anything
irreversible happens, and a dispatch whose claim has moved on spawns nothing.

What else this file proves:

  * EVERY external call the fixer used to make inside T1 now happens with the
    state lock free - measured against the REAL `flock` at the instant of each
    call, including a genuine 127.0.0.1 bind, with companion tests proving the
    instrument would report a violation.
  * `repair_cycles` increments at COMMIT, not at plan, so a dispatch that
    never spawned cannot consume one of the governed `max_repair_cycles`.
  * A crash between the claim and the spawn leaves a durable claim, no worker
    and no counted repair cycle, and a fresh Supervisor resumes it exactly
    once under the same identity.
  * `route_awaiting_dispatch` will not dispatch a second fixer against a task
    whose claim is planned but not yet spawned.

Everything runs against a real `state.Store` on a temporary path, a real
worker-log directory and real job files. Nothing launches a worker, nothing
makes a paid call, nothing sends a notification and nothing reaches the
network - the only socket operation is a loopback bind on a port the test
first proved was free.
"""

from __future__ import annotations

import contextlib
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
    notify,
    providers,
    routing,
    state as state_mod,
    supervisor as supervisor_mod,
    workers,
)
import declared_phases  # noqa: E402

# Captured before any patch replaces the module attribute, so the job-file
# spy can record the lock and still write a REAL job file - which is what the
# crash-recovery tests read back.
REAL_WRITE_JOB = workers.write_job

TZ = "Pacific/Auckland"
PR = 100
BRANCH = "task/task-001"
FINDINGS = [
    {"id": "F1", "severity": "P1", "category": "SECURITY", "summary": "one"},
    {"id": "F2", "severity": "P2", "category": "TESTS", "summary": "two"},
]


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


class FixerHarnessCase(unittest.TestCase):
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
        # The Builder's worktree: the shared checkout the Fixer reuses.
        self.builder_worktree = self.root / "task-001-builder"
        self.builder_worktree.mkdir()
        self.sup = self.supervisor()

    def supervisor(self):
        """A Supervisor as a fresh process would build one.

        Built twice on purpose: a crash test needs a second Supervisor that
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
        sup.run_declared = mock.Mock(
            side_effect=declared_phases.provider_phases_suppressed)
        return sup

    # ------------------------------------------------------------ fixtures

    def seed(self, *, task_state="REVIEW", verdict=routing.REVIEW_FAIL,
             findings=None, repair_cycles=0, write=True) -> dict:
        doc = state_mod.initial_document("run-002", "2.0")
        doc["started_at"] = clock.iso(clock.now(TZ))
        providers.ensure(doc)
        migration_lock.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "Build the thing", [],
                                  "feature", False, TZ)
        task.update({"state": task_state, "branch": BRANCH, "pr": PR,
                     "last_progress_at": clock.iso(clock.now(TZ))})
        record = routing.blank_pr_record(PR, "TASK-001", BRANCH)
        record["review_verdict"] = verdict
        record["review_cycles"] = 1
        record["repair_cycles"] = repair_cycles
        record["pending_findings"] = list(
            FINDINGS if findings is None else findings)
        doc["prs"][str(PR)] = record
        if write:
            self.store._write(doc)
        return doc

    def open_pr(self) -> dict:
        return {"number": PR, "state": "OPEN", "isDraft": False,
                "headRefName": BRANCH, "mergeStateStatus": "CLEAN"}

    # ------------------------------------------------------------- driving

    def run_tick(self, sup=None, *, existing_worktree="builder",
                 create=True, created_path="builder", start=True, probe=True,
                 real_probe=False, port_range=None, skip_execute=False,
                 skip_confirm=False, entries=None, real_write_job=True):
        """One real tick, recording whether the state lock was held at the
        moment of every external dispatch call the fixer makes.

        `workers.acquire_worktree` is NOT mocked: the real reuse decision runs,
        with only its `git worktree list` call (`worktree_for_branch`) spied.
        `skip_execute` and `skip_confirm` simulate a process that died in the
        corresponding window.
        """
        sup = sup or self.sup
        self.calls: list[tuple[str, bool]] = []
        resolve = {"builder": self.builder_worktree, None: None}

        def spy(name, answer):
            def inner(*_a, **_kw):
                self.calls.append((name, lock_is_held(self.store.lock_path)))
                return answer() if callable(answer) else answer
            return inner

        def spied_write_job(*args, **kwargs):
            self.calls.append(("write_job", lock_is_held(self.store.lock_path)))
            if real_write_job:
                return REAL_WRITE_JOB(*args, **kwargs)
            return self.job_dir / "unwritten.job.json"

        patches = [
            mock.patch.object(supervisor_mod.gh, "list_open_prs",
                              return_value=[self.open_pr()]),
            mock.patch.object(supervisor_mod.workers, "read_status",
                              return_value=None),
            mock.patch.object(supervisor_mod.proc, "worker_entry_processes",
                              return_value={} if entries is None else entries),
            mock.patch.object(supervisor_mod.config, "HEARTBEAT_PATH",
                              self.heartbeat_path),
            mock.patch.object(supervisor_mod.prompts, "write",
                              side_effect=spy("prompts.write",
                                              lambda: self.root / "p.md")),
            mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                              side_effect=spy("worktree_for_branch",
                                              lambda: resolve[existing_worktree])),
            mock.patch.object(supervisor_mod.workers, "create_worker",
                              side_effect=spy("create_worker",
                                              ok_result if create else fail_result)),
            mock.patch.object(supervisor_mod.workers, "worktree_path",
                              side_effect=spy("worktree_path",
                                              lambda: resolve[created_path])),
            mock.patch.object(supervisor_mod.workers, "write_job",
                              side_effect=spied_write_job),
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

    def record(self) -> dict:
        return self.durable()["prs"][str(PR)]

    def durable_claim(self):
        return state_mod.dispatch_claim(self.task(), "fixer")

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def event(self, name: str) -> dict:
        found = [fields for event, fields in self.events if event == name]
        self.assertTrue(found, f"no {name} event in {self.names()}")
        return found[0]

    def plan(self, *, worker="task-001-fixer-1", pr=PR, findings=None):
        return supervisor_mod.DispatchPlan(
            role="fixer", task_id="TASK-001", pr=pr, worker=worker,
            branch=BRANCH, port_candidates=(39110,), observed_head=None,
            context={"findings": list(FINDINGS if findings is None else findings),
                     "port_exhaustion_reason": ""})


# ================================================== the transaction boundary


class NoFixerCallHappensUnderTheStateLock(FixerHarnessCase):
    """The C-18 guarantee for the fixer path, measured against the real flock
    rather than read off the source."""

    def test_every_external_fixer_call_is_made_with_the_lock_free(self):
        self.seed()
        self.run_tick()

        self.assertTrue(self.calls,
                        "the test proves nothing if nothing external ran")
        self.assertEqual(
            sorted({name for name, _ in self.calls}),
            ["probe_port", "prompts.write", "start_job", "worktree_for_branch",
             "write_job"],
            "every external call dispatch_fixer used to make inside T1 must "
            "appear; worktree_for_branch is acquire_worktree's git subprocess")
        self.assertEqual([held for _, held in self.calls],
                         [False] * len(self.calls),
                         f"an external call ran under the lock: {self.calls}")

    def test_the_create_path_is_also_entirely_outside_the_lock(self):
        """Reuse is the normal case, but a missing checkout falls through to
        `workmux add`, and that subprocess must be outside T1 too."""
        self.seed()
        self.run_tick(existing_worktree=None, created_path="builder")

        self.assertIn("create_worker", [name for name, _ in self.calls])
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
        self.assertEqual(self.durable()["workers"]["task-001-fixer-1"]["port"],
                         port, "the real bind must be the port that was committed")

    def test_the_lock_probe_can_actually_detect_a_held_lock(self):
        """Without this, the tests above could pass because the probe is
        broken rather than because the code is right."""
        self.seed()
        self.assertFalse(lock_is_held(self.store.lock_path))
        with self.store.transaction():
            self.assertTrue(lock_is_held(self.store.lock_path))
        self.assertFalse(lock_is_held(self.store.lock_path))

    def test_the_bind_spy_would_notice_a_bind_inside_a_transaction(self):
        """The companion proof for the REAL bind: the same spy socket reports
        True for a deliberate in-transaction bind, so a green boundary test
        cannot be a broken instrument."""
        self.seed()
        self.calls = []
        with mock.patch.object(supervisor_mod.workers, "socket",
                               self.spy_socket_module()):
            with self.store.transaction():
                supervisor_mod.workers.probe_port(free_port())
        self.assertEqual(self.calls, [("bind", True)])

    def test_the_call_spy_would_notice_a_call_inside_a_transaction(self):
        """The same companion proof for the non-socket calls."""
        self.seed()
        self.calls = []
        with mock.patch.object(
                supervisor_mod.workers, "start_job",
                side_effect=lambda *a, **k: self.calls.append(
                    ("start_job", lock_is_held(self.store.lock_path)))):
            with self.store.transaction():
                supervisor_mod.workers.start_job("w", Path("/j"), Path("/wt"))
        self.assertEqual(self.calls, [("start_job", True)])

    def test_the_planning_half_touches_no_file_and_no_subprocess(self):
        """T1's own guarantee, stated separately from the tick-wide one: the
        claim is made without any of the moved calls happening at all."""
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod, "prompts") as prompts, \
                mock.patch.object(supervisor_mod.workers, "acquire_worktree") as acquire, \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch") as by_branch, \
                mock.patch.object(supervisor_mod.workers, "probe_port") as probe, \
                mock.patch.object(supervisor_mod.workers, "allocate_port") as alloc, \
                mock.patch.object(supervisor_mod.workers, "write_job") as job, \
                mock.patch.object(supervisor_mod.workers, "start_job") as start, \
                mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                                  return_value=([39110], "")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertEqual(prompts.mock_calls, [])
        for spy in (acquire, by_branch, probe, alloc, job, start):
            spy.assert_not_called()
        self.assertTrue(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                 "fixer"))

    def test_the_claim_carries_the_port_candidates_chosen_under_the_lock(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                               return_value=([39110, 39111], "")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        claim = state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "fixer")
        self.assertEqual(claim["port_candidates"], [39110, 39111])
        self.assertEqual(claim["pr"], PR)
        self.assertEqual(claim["worker"], "task-001-fixer-1")
        self.assertEqual(claim["branch"], BRANCH)

    def test_the_claim_survives_a_json_round_trip(self):
        """It is written to the state document, so everything on it must be
        JSON - findings included."""
        self.seed()
        self.run_tick(skip_execute=True)
        claim = self.durable_claim()
        self.assertEqual(json.loads(json.dumps(claim)), claim)
        self.assertEqual([f["id"] for f in claim["context"]["findings"]],
                         ["F1", "F2"])

    def test_the_findings_in_the_claim_are_copied_not_aliased(self):
        """A reference into record["pending_findings"] would let the execute
        phase render a prompt from a list a later transaction had changed."""
        doc = self.seed(write=False)
        findings = doc["prs"][str(PR)]["pending_findings"]
        with mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                               return_value=([39110], "")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, findings)
        claim = state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "fixer")
        findings[0]["summary"] = "mutated after the claim was taken"
        self.assertEqual(claim["context"]["findings"][0]["summary"], "one")


# ============================================== THE REUSED WORKTREE HAZARD


class TheReusedWorktreeHazard(FixerHarnessCase):
    """Why the fixer was sequenced last.

    `reuse_if_checked_out=True` hands the execute phase a worktree it did not
    create and does not exclusively own. The claim that authorised reusing it
    was taken in T1; by the time the spawn happens T1 has committed and
    something else may have taken the fixer slot. Spawning anyway would put a
    paid agent into a shared branch checkout that a different owner is now
    responsible for, and no later transaction can take that back.
    """

    def steal_the_claim(self):
        """A `ctl` command - or any writer - replaces the claim between T1
        committing and the execute phase running."""
        with self.store.transaction() as doc:
            task = doc["tasks"]["TASK-001"]
            state_mod.dispatch_claims(task)["fixer"] = state_mod.new_dispatch_claim(
                role="fixer", task_id="TASK-001", worker="task-001-fixer-9",
                branch=BRANCH, claimed_at=clock.iso(clock.now(TZ)),
                lease_expires_at=self.sup._lease_expires("fixer"),
                pr=PR, port_candidates=[39110])

    def test_the_reused_worktree_really_is_the_builders(self):
        """The premise. Without this the hazard tests could be about a
        worktree the fixer created for itself, which carries no hazard."""
        self.seed()
        self.run_tick()
        record = self.durable()["workers"]["task-001-fixer-1"]
        self.assertEqual(record["worktree"], str(self.builder_worktree))
        self.assertNotIn("create_worker", [name for name, _ in self.calls],
                         "a reused checkout must never be created a second time")

    def test_a_claim_stolen_before_the_spawn_spawns_nothing(self):
        self.seed()
        plan = self.plan()
        self.steal_the_claim()
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "probe_port",
                                  return_value=True) as probe, \
                mock.patch.object(supervisor_mod.workers, "write_job") as job, \
                mock.patch.object(supervisor_mod.workers, "start_job") as start:
            result = self.sup._execute_fixer_dispatch(plan)

        self.assertFalse(result.ok)
        start.assert_not_called()
        job.assert_not_called()
        probe.assert_not_called()
        self.assertIn("no longer current", result.reason)
        self.assertEqual(
            self.event("DISPATCH_EXECUTION_ABANDONED")["outcome"],
            "CLAIM_NO_LONGER_CURRENT")

    def test_the_stolen_claim_is_left_intact_for_its_own_owner(self):
        """Abandoning must not clear somebody else's reservation."""
        self.seed()
        plan = self.plan()
        self.steal_the_claim()
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "start_job"):
            result = self.sup._execute_fixer_dispatch(plan)
        with self.store.transaction() as doc:
            self.sup.apply_dispatch_result(doc, result)

        self.assertEqual(self.durable_claim()["worker"], "task-001-fixer-9")
        self.assertEqual(self.record().get("dispatch_failures", 0), 0,
                         "a superseded claim is not this dispatch's failure")
        self.assertEqual(self.record()["repair_cycles"], 0)

    def test_a_claim_cleared_before_the_spawn_spawns_nothing(self):
        """The other shape: the claim is gone entirely rather than replaced."""
        self.seed()
        plan = self.plan()
        with self.store.transaction() as doc:
            state_mod.dispatch_claims(doc["tasks"]["TASK-001"]).pop("fixer", None)
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "start_job") as start:
            result = self.sup._execute_fixer_dispatch(plan)
        self.assertFalse(result.ok)
        start.assert_not_called()

    def test_an_unreadable_document_refuses_to_spawn(self):
        """Fail-closed. A document that cannot be read cannot prove the claim
        survived, and refusing costs one retry while spawning costs a shared
        branch."""
        self.seed()
        self.store.path.write_text("{ this is not json", encoding="utf-8")
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "start_job") as start:
            result = self.sup._execute_fixer_dispatch(self.plan())
        self.assertFalse(result.ok)
        start.assert_not_called()

    def test_a_missing_document_refuses_to_spawn(self):
        self.seed()
        self.store.path.unlink()
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "start_job") as start:
            result = self.sup._execute_fixer_dispatch(self.plan())
        self.assertFalse(result.ok)
        start.assert_not_called()

    def test_an_unchanged_claim_proceeds_normally(self):
        """The guard must not be a blanket refusal - otherwise every test
        above would pass with the fixer permanently broken."""
        self.seed()
        self.run_tick()
        self.assertIn("task-001-fixer-1", self.durable()["workers"])
        self.assertNotIn("DISPATCH_EXECUTION_ABANDONED", self.names())

    def test_the_re_verification_happens_before_the_job_file_is_written(self):
        """Ordering is the whole point. The job file is the durable spawn
        evidence `resume_dispatch_claims` reads, so writing one for a claim
        that has moved on would make a later tick commit a worker that never
        ran."""
        self.seed()
        plan = self.plan()
        self.steal_the_claim()
        with mock.patch.object(supervisor_mod.prompts, "write",
                               return_value=self.root / "p.md"), \
                mock.patch.object(supervisor_mod.workers, "worktree_for_branch",
                                  return_value=self.builder_worktree), \
                mock.patch.object(supervisor_mod.workers, "start_job"):
            self.sup._execute_fixer_dispatch(plan)
        self.assertEqual(list(self.job_dir.glob("*.job.json")), [],
                         "no spawn evidence may exist for an abandoned dispatch")

    def test_the_reuse_path_prepares_nothing_inside_the_worktree(self):
        """Why a crash mid-reuse leaves nothing half-prepared: when the branch
        is already checked out, acquire_worktree performs NO mutation at all -
        it looks the worktree up and returns it. Everything the fixer does
        prepare (the prompt file, the job file) lives outside the worktree and
        is rewritten identically on a retry."""
        before = sorted(p.name for p in self.builder_worktree.iterdir())
        with mock.patch.object(workers, "worktree_for_branch",
                               return_value=self.builder_worktree), \
                mock.patch.object(workers, "create_worker") as create:
            path, why = workers.acquire_worktree(
                "task-001-fixer-1", BRANCH, "main", "sess",
                self.root / "p.md", reuse_if_checked_out=True)
        self.assertEqual(path, self.builder_worktree)
        self.assertEqual(why, "")
        create.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.builder_worktree.iterdir()),
                         before)

    def test_a_crash_before_the_job_file_replans_the_same_reuse(self):
        """The half-prepared case, end to end. The prompt was written and the
        worktree reused, then the process died. The next tick re-plans the
        SAME claim, reuses the SAME worktree and spawns exactly once."""
        self.seed()
        self.run_tick(skip_execute=True)
        self.assertEqual(self.durable_claim()["worker"], "task-001-fixer-1")
        self.assertEqual(list(self.job_dir.glob("*.job.json")), [])

        fresh = self.supervisor()
        self.run_tick(fresh)
        workers_now = self.durable()["workers"]
        self.assertEqual(list(workers_now), ["task-001-fixer-1"])
        self.assertEqual(workers_now["task-001-fixer-1"]["worktree"],
                         str(self.builder_worktree))
        self.assertEqual(
            [name for name, _ in self.calls].count("start_job"), 1,
            "exactly one spawn across the crash")
        self.assertEqual(self.record()["repair_cycles"], 1)


# =========================================== the commit side is what it was


class TheFixerCommitsExactlyWhatItUsedTo(FixerHarnessCase):

    def test_a_successful_tick_dispatches_exactly_one_fixer(self):
        self.seed()
        self.run_tick()
        record = self.durable()["workers"]["task-001-fixer-1"]
        self.assertEqual(record["role"], "fixer")
        self.assertEqual(record["pr"], PR)
        self.assertEqual(record["worktree"], str(self.builder_worktree))
        self.assertEqual(record["port"], 39110)
        self.assertIsNotNone(record["lease_expires_at"])
        self.assertEqual(self.task()["state"], "FIX_REQUIRED")
        self.assertEqual(self.task()["worker"], "task-001-fixer-1")
        self.assertEqual(self.record()["repair_cycles"], 1)
        self.assertEqual(self.record()["open_finding_ids"], ["F1", "F2"])
        self.assertIsNone(self.durable_claim(), "the claim is cleared on commit")
        self.assertEqual(self.event("FIX_DISPATCHED")["outcome"], "DISPATCHED")

    def test_the_commit_queues_the_notification_and_sends_nothing(self):
        """C-18 stage 2 made notify_out a durable queue write. The commit
        phase must keep it that way: the intent is committed with the state
        change that earned it, and no send happens inside the transaction."""
        self.seed()
        self.run_tick(skip_confirm=True)
        with mock.patch.object(self.sup.notifier, "send") as send:
            with self.store.transaction() as doc:
                self.sup.apply_dispatch_result(
                    doc, supervisor_mod.DispatchResult(
                        plan=self.plan(), ok=True,
                        worktree=str(self.builder_worktree), port=39110))
        send.assert_not_called()
        queued = list(notify.queue(self.durable()).values())
        dispatched = [i for i in queued if "fixer dispatched" in i["title"]]
        self.assertEqual([i["title"] for i in dispatched],
                         [f"TASK-001 PR #{PR}: fixer dispatched"])
        self.assertIn("Repair cycle 1", dispatched[0]["body"])

    def test_any_delivery_of_that_intent_happens_with_the_lock_free(self):
        """The other half of stage 2: the drain delivers later in the tick,
        holding no state lock. Measured against the real flock."""
        self.seed()
        sent: list[bool] = []

        def send(*_a, **_kw):
            sent.append(lock_is_held(self.store.lock_path))
            return {"ok": True}

        with mock.patch.object(self.sup.notifier, "send", side_effect=send):
            self.run_tick()
        self.assertTrue(sent, "nothing was delivered; the test is vacuous")
        self.assertEqual(sent, [False] * len(sent),
                         "a notification was delivered under the state lock")

    def test_the_retained_worktree_claim_is_still_popped(self):
        doc = self.seed(write=False)
        doc["tasks"]["TASK-001"]["retained_worktrees"] = {
            str(self.builder_worktree): {"why": "TERMINAL_REAP"}}
        self.store._write(doc)
        self.run_tick()
        self.assertNotIn(str(self.builder_worktree),
                         self.task()["retained_worktrees"])

    def test_repair_cycles_increments_only_on_a_real_dispatch(self):
        self.seed()
        self.run_tick(start=False)
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.record()["dispatch_failures"], 1)
        self.assertEqual(self.task()["state"], "REVIEW")
        self.assertIsNone(self.durable_claim())

    def test_repair_cycles_is_not_incremented_at_plan_time(self):
        """Stated on its own: after T1 and before the spawn, the counter is
        still at its old value, so a crash in that window cannot burn one of
        the governed max_repair_cycles."""
        self.seed()
        self.run_tick(skip_execute=True)
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertIsNotNone(self.durable_claim())

    def test_a_missing_worktree_is_an_explicit_dispatch_failure(self):
        self.seed()
        self.run_tick(existing_worktree=None, create=False, created_path=None)
        self.assertEqual(self.record()["dispatch_failures"], 1)
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.event("DISPATCH_FAILED")["outcome"], "FAILED")

    def test_an_unbindable_candidate_set_fails_the_dispatch(self):
        self.seed()
        self.run_tick(probe=False)
        self.assertEqual(self.record()["dispatch_failures"], 1)
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertEqual(self.event("PORT_ALLOCATION_FAILED")["role"], "fixer")

    def test_an_empty_candidate_list_fails_inside_the_transaction(self):
        """No claim is written at all, so there is nothing to recover and no
        external effect to undo."""
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                               return_value=([], "range exhausted")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "fixer"))
        self.assertEqual(self.sup._dispatch_plans, [])
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"], 1)

    def test_the_repair_cycle_limit_still_escalates_before_any_claim(self):
        doc = self.seed(repair_cycles=self.cfg.max_repair_cycles, write=False)
        self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "fixer"))
        self.assertEqual(self.sup._dispatch_plans, [])

    def test_a_paused_provider_still_waits_before_any_claim(self):
        doc = self.seed(write=False)
        with mock.patch.object(supervisor_mod.providers, "may",
                               return_value=False):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertEqual(doc["tasks"]["TASK-001"]["state"],
                         "WAITING_PROVIDER_RESET")
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "fixer"))

    def test_a_claim_refused_by_state_is_a_dispatch_failure_not_a_crash(self):
        """A claim the control plane would later refuse must never be stored:
        it would reserve the fixer slot with something that can never spawn."""
        doc = self.seed(write=False)
        doc["tasks"]["TASK-001"]["branch"] = ""     # refused: empty branch
        with mock.patch.object(supervisor_mod.workers, "select_port_candidates",
                               return_value=([39110], "")):
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        self.assertIsNone(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                   "fixer"))
        self.assertEqual(doc["prs"][str(PR)]["dispatch_failures"], 1)
        self.assertEqual(self.event("DISPATCH_CLAIM_REFUSED")["outcome"],
                         "CLAIM_REFUSED")

    def test_committing_twice_cannot_double_count_repair_cycles(self):
        self.seed()
        self.run_tick()
        plan = self.plan()
        result = supervisor_mod.DispatchResult(
            plan=plan, ok=True, worktree=str(self.builder_worktree), port=39110)
        with self.store.transaction() as doc:
            self.sup.apply_dispatch_result(doc, result)
        self.assertEqual(self.record()["repair_cycles"], 1)

    def test_a_superseded_claim_cannot_be_committed(self):
        self.seed()
        self.run_tick(skip_confirm=True)
        plan = self.plan()
        with self.store.transaction() as doc:
            state_mod.dispatch_claims(doc["tasks"]["TASK-001"]).pop("fixer")
        with self.store.transaction() as doc:
            self.sup.apply_dispatch_result(
                doc, supervisor_mod.DispatchResult(
                    plan=plan, ok=True, worktree=str(self.builder_worktree),
                    port=39110))
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertEqual(self.event("DISPATCH_COMMIT_REFUSED")["outcome"],
                         "CLAIM_SUPERSEDED")

    def test_a_vanished_pr_record_commits_nothing_and_releases_the_claim(self):
        self.seed()
        self.run_tick(skip_confirm=True)
        with self.store.transaction() as doc:
            doc["prs"].pop(str(PR))
        with self.store.transaction() as doc:
            self.sup.apply_dispatch_result(
                doc, supervisor_mod.DispatchResult(
                    plan=self.plan(), ok=True,
                    worktree=str(self.builder_worktree), port=39110))
        self.assertEqual(self.durable()["workers"], {})
        self.assertIsNone(self.durable_claim())
        self.assertEqual(self.event("DISPATCH_COMMIT_REFUSED")["outcome"],
                         "PR_RECORD_GONE")


# ============================================ a crash between plan and spawn


class ACrashBetweenPlanAndSpawnIsRecovered(FixerHarnessCase):

    def test_the_claim_is_durable_and_nothing_was_dispatched(self):
        self.seed()
        self.run_tick(skip_execute=True)
        claim = self.durable_claim()
        self.assertIsNotNone(claim, "the claim must survive the crash")
        self.assertEqual(claim["claim_state"], state_mod.DISPATCH_PLANNED)
        self.assertEqual(claim["worker"], "task-001-fixer-1")
        self.assertEqual(claim["pr"], PR)
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.task()["state"], "REVIEW")
        self.assertEqual(self.record()["repair_cycles"], 0)
        self.assertEqual(list(self.job_dir.glob("*.job.json")), [])

    def test_a_fresh_supervisor_resumes_it_and_dispatches_once(self):
        self.seed()
        self.run_tick(skip_execute=True)

        fresh = self.supervisor()
        self.run_tick(fresh)
        self.assertEqual(list(self.durable()["workers"]), ["task-001-fixer-1"])
        self.assertEqual(self.record()["repair_cycles"], 1)
        self.assertEqual(
            [name for name, _ in self.calls].count("start_job"), 1)
        self.assertEqual(self.event("DISPATCH_RESUMED")["outcome"], "REPLANNED")

    def test_the_resumed_dispatch_reuses_the_claimed_identity(self):
        """Not a new worker name. The claim is the reservation, and a second
        name would mean two reservations for one slot."""
        self.seed()
        self.run_tick(skip_execute=True)
        claimed = self.durable_claim()["worker"]
        fresh = self.supervisor()
        self.run_tick(fresh)
        self.assertEqual(list(self.durable()["workers"]), [claimed])

    def test_a_crash_between_spawn_and_commit_commits_without_respawning(self):
        """The job file is the spawn evidence: a worker may be running, so
        recovery commits it rather than starting another."""
        self.seed()
        self.run_tick(skip_confirm=True)
        self.assertTrue((self.job_dir / "task-001-fixer-1.job.json").exists())
        self.assertEqual(self.durable()["workers"], {})

        fresh = self.supervisor()
        self.run_tick(fresh)
        self.assertEqual(list(self.durable()["workers"]), ["task-001-fixer-1"])
        self.assertEqual(self.record()["repair_cycles"], 1)
        self.assertEqual([name for name, _ in self.calls].count("start_job"), 0,
                         "recovery must never spawn a second fixer")
        self.assertEqual(self.event("DISPATCH_RECOVERED")["outcome"],
                         "SPAWN_EVIDENCE_FOUND")

    def test_recovery_reads_the_worktree_and_port_from_the_job_file(self):
        self.seed()
        self.run_tick(skip_confirm=True)
        fresh = self.supervisor()
        self.run_tick(fresh)
        record = self.durable()["workers"]["task-001-fixer-1"]
        job = json.loads((self.job_dir / "task-001-fixer-1.job.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(record["worktree"], job["worktree"])
        self.assertEqual(record["port"], job["port"])

    def test_a_claim_with_no_observation_is_left_exactly_as_it_is(self):
        """Nothing may be concluded about a spawn nobody looked at."""
        self.seed()
        self.run_tick(skip_execute=True)
        before = self.durable_claim()
        fresh = self.supervisor()
        with mock.patch.object(fresh, "observe_dispatch_claims",
                               return_value={}):
            self.run_tick(fresh)
        self.assertEqual(self.durable_claim(), before)
        self.assertEqual(self.durable()["workers"], {})
        self.assertEqual(self.event("DISPATCH_RESUME_DEFERRED")["outcome"],
                         "OBSERVATION_MISSING")


# ============================================= the claim reserves the slot


class AFixerClaimReservesTheFixerSlot(FixerHarnessCase):

    def claimed_doc(self) -> dict:
        """A task whose fixer is claimed but not yet spawned."""
        doc = self.seed(write=False)
        state_mod.dispatch_claims(doc["tasks"]["TASK-001"])["fixer"] = \
            state_mod.new_dispatch_claim(
                role="fixer", task_id="TASK-001", worker="task-001-fixer-1",
                branch=BRANCH, claimed_at=clock.iso(clock.now(TZ)),
                lease_expires_at=self.sup._lease_expires("fixer"),
                pr=PR, port_candidates=[39110])
        return doc

    def test_has_worker_sees_a_claim_with_no_worker_record(self):
        doc = self.claimed_doc()
        self.assertEqual(doc["workers"], {})
        self.assertTrue(self.sup.has_worker(doc, PR, ("reviewer", "fixer")))

    def test_route_awaiting_dispatch_will_not_dispatch_a_second_fixer(self):
        doc = self.claimed_doc()
        task = doc["tasks"]["TASK-001"]
        with mock.patch.object(self.sup, "dispatch_fixer") as fixer, \
                mock.patch.object(self.sup, "dispatch_reviewer") as reviewer:
            self.sup.route_awaiting_dispatch(doc, task, PR, doc["prs"][str(PR)])
        fixer.assert_not_called()
        reviewer.assert_not_called()

    def test_without_the_claim_the_same_task_is_dispatched(self):
        """The control, so the test above cannot pass for the wrong reason."""
        doc = self.seed(write=False)
        task = doc["tasks"]["TASK-001"]
        with mock.patch.object(self.sup, "dispatch_fixer") as fixer:
            self.sup.route_awaiting_dispatch(doc, task, PR, doc["prs"][str(PR)])
        fixer.assert_called_once()

    def test_a_whole_tick_with_a_claim_present_plans_nothing_new(self):
        """End to end: the claim is resumed, never re-planned alongside a
        second dispatch of the same task."""
        doc = self.claimed_doc()
        self.store._write(doc)
        self.run_tick()
        self.assertEqual(list(self.durable()["workers"]), ["task-001-fixer-1"])
        self.assertEqual(
            [name for name, _ in self.calls].count("start_job"), 1)
        self.assertEqual(self.record()["repair_cycles"], 1)

    def test_the_planner_itself_refuses_to_replan_a_claimed_fixer(self):
        doc = self.claimed_doc()
        before = dict(state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "fixer"))
        with mock.patch.object(supervisor_mod.workers,
                               "select_port_candidates") as select:
            self.sup.dispatch_fixer(doc, doc["tasks"]["TASK-001"], PR, FINDINGS)
        select.assert_not_called()
        self.assertEqual(self.sup._dispatch_plans, [])
        self.assertEqual(state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                                  "fixer"), before)

    def test_an_inactive_claim_does_not_reserve_the_slot(self):
        doc = self.claimed_doc()
        state_mod.dispatch_claim(doc["tasks"]["TASK-001"],
                                 "fixer")["claim_state"] = "DONE"
        self.assertFalse(self.sup.has_worker(doc, PR, ("reviewer", "fixer")))

    def test_a_claim_on_another_pull_request_does_not_block_this_one(self):
        doc = self.claimed_doc()
        state_mod.dispatch_claim(doc["tasks"]["TASK-001"], "fixer")["pr"] = 999
        self.assertFalse(self.sup.has_worker(doc, PR, ("reviewer", "fixer")))


if __name__ == "__main__":
    unittest.main()
