"""C-09 core: worker resource ownership lifecycle.

Covers the accepted C-09 design plus the three final gate corrections:
  - dispatch populates worktree/port/lease_expires_at durably;
  - the port allocator excludes committed owners, live worker-entry job
    files, identity-verified surviving agents (fail-closed on ambiguity),
    and externally bound listeners;
  - lease expiry with a live governed process escalates to HUMAN_REQUIRED
    via the C-08b intervention lifecycle, never a kill;
  - lease_expired RETRY/FAIL are refused while the entry, the recorded
    agent, or the worker's assigned listener survives;
  - worktree ownership transfers ACTIVE <-> RETAINED atomically;
  - reverse (reality -> state) orphan detection for worktrees, worker-entry
    processes, surviving agents and in-range listeners (IPv4 + IPv6);
  - orphan annunciation with a durable pre-send ATTEMPTING fence: at most
    one external send per durably authorised attempt.

Fixture style follows tests/test_reconcile.py (raw observation primitives
mocked) and tests/test_intervention_resolution.py (isolated state/ledger,
stubbed config, cli.main driven end to end).
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    budget,
    cli,
    clock,
    config,
    intervention,
    ledger as ledger_mod,
    notify,
    proc,
    providers,
    reconcile,
    state,
    supervisor as supervisor_mod,
    watchdog,
    worker_entry,
    workers,
)

TZ = "Pacific/Auckland"
LO, HI = 43210, 43229  # test-local range, outside the governed [3200, 3299]


def iso_offset(seconds: float) -> str:
    return clock.iso(clock.now(TZ) + timedelta(seconds=seconds))


def ok_result():
    from control import gh
    return gh.Result(True, "", "", 0)


# --------------------------------------------------------------- proc facts


class TestProcPrimitives(unittest.TestCase):
    def test_start_ticks_of_this_process_is_a_positive_int(self):
        import os
        ticks = proc.start_ticks(os.getpid())
        self.assertIsInstance(ticks, int)
        self.assertGreater(ticks, 0)

    def test_stat_parse_survives_parens_and_spaces_in_comm(self):
        text = ("4242 (tmux: server) S 1 4242 4242 0 -1 4194560 1 0 0 0 5 3 0 0 "
                "20 0 1 0 987654 1000000 100 18446744073709551615 1 1 0 0 0 0 0 "
                "0 0 0 0 0 17 1 0 0 0 0 0")
        self.assertEqual(proc._ticks_from_stat(text), 987654)

    def test_verified_alive_contract(self):
        with mock.patch.object(proc, "is_running", return_value=False):
            self.assertIs(proc.verified_alive(4242, 100), False)
        with mock.patch.object(proc, "is_running", return_value=True), \
                mock.patch.object(proc, "start_ticks", return_value=100):
            self.assertIs(proc.verified_alive(4242, 100), True)
            self.assertIs(proc.verified_alive(4242, 999), False)
            self.assertIsNone(proc.verified_alive(4242, None))
        with mock.patch.object(proc, "is_running", return_value=True), \
                mock.patch.object(proc, "start_ticks", return_value=None):
            self.assertIsNone(proc.verified_alive(4242, 100))

    def test_listen_parse_merges_ipv4_and_ipv6(self):
        v4 = ("  sl  local_address rem_address   st ...\n"
              "   0: 0100007F:0C82 00000000:0000 0A 00000000:00000000 ...\n"
              "   1: 0100007F:0C83 00000000:0000 01 00000000:00000000 ...\n")
        v6 = ("  sl  local_address rem_address st ...\n"
              "   0: 00000000000000000000000000000000:0C84 "
              "00000000000000000000000000000000:0000 0A 00000000:00000000 ...\n")
        ports = proc._parse_listen_ports(v4, 3200, 3299) | \
            proc._parse_listen_ports(v6, 3200, 3299)
        self.assertEqual(ports, {0x0C82, 0x0C84})  # 3202 listening, 3203 not-LISTEN

    def test_listening_ports_sees_a_real_dual_stack_ipv6_listener(self):
        """A [::] listener occupies the IPv4 wildcard too; reading only
        /proc/net/tcp would miss it, so this pins the tcp6 merge for real."""
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            self.skipTest("IPv6 unavailable on this host")
        with sock:
            try:
                sock.bind(("::", 0))
            except OSError:
                self.skipTest("IPv6 bind unavailable on this host")
            sock.listen(1)
            port = sock.getsockname()[1]
            found = proc.listening_ports(port, port)
            self.assertIsNotNone(found)
            self.assertIn(port, found)


# ------------------------------------------------------------ port allocator


class AllocatorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job_dir = Path(tmp.name)
        patcher = mock.patch.object(config, "WORKER_LOG_DIR", self.job_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def doc(self, **worker_ports):
        d = {"workers": {}}
        for name, port in worker_ports.items():
            d["workers"][name] = {"port": port}
        return d

    def write_job(self, worker: str, port: int):
        (self.job_dir / f"{worker}.job.json").write_text(
            json.dumps({"worker": worker, "port": port}), encoding="utf-8")

    def write_status(self, worker: str, *, phase="RUNNING", agent_pid=4242,
                     ticks=100):
        payload = {"phase": phase, "agent_pid": agent_pid,
                   "agent_start_ticks": ticks}
        (self.job_dir / f"{worker}.status.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def allocate(self, doc, *, entries=None, alive=False):
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value=entries if entries is not None else {}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=alive):
            return workers.allocate_port(doc, LO, HI)

    def test_skips_a_port_owned_by_a_committed_worker_record(self):
        port, why = self.allocate(self.doc(w1=LO))
        self.assertEqual((port, why), (LO + 1, ""))

    def test_skips_a_job_file_port_whose_entry_process_is_alive(self):
        self.write_job("w1", LO)
        port, _ = self.allocate(self.doc(), entries={"w1": 500})
        self.assertEqual(port, LO + 1)

    def test_critical_race_entry_gone_agent_alive_port_unbound_is_excluded(self):
        self.write_job("w1", LO)
        self.write_status("w1")
        port, _ = self.allocate(self.doc(), entries={}, alive=True)
        self.assertEqual(port, LO + 1)

    def test_reused_agent_pid_with_different_ticks_frees_the_port(self):
        self.write_job("w1", LO)
        self.write_status("w1")
        port, _ = self.allocate(self.doc(), entries={}, alive=False)
        self.assertEqual(port, LO)

    def test_ambiguous_agent_identity_conservatively_retains_the_port(self):
        self.write_job("w1", LO)
        self.write_status("w1")
        port, _ = self.allocate(self.doc(), entries={}, alive=None)
        self.assertEqual(port, LO + 1)

    def test_terminal_status_releases_the_job_file_port(self):
        self.write_job("w1", LO)
        self.write_status("w1", phase="DONE")
        port, _ = self.allocate(self.doc(), entries={}, alive=True)
        self.assertEqual(port, LO)

    def test_proc_scan_failure_fails_closed_and_retains_job_file_ports(self):
        self.write_job("w1", LO)
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value=None), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            port, _ = workers.allocate_port(self.doc(), LO, HI)
        self.assertEqual(port, LO + 1)

    def test_externally_bound_port_is_rejected_by_the_bind_test(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", LO))
        blocker.listen(1)
        port, _ = self.allocate(self.doc())
        self.assertEqual(port, LO + 1)

    def test_exhaustion_returns_an_explicit_reason(self):
        port, why = self.allocate(self.doc(w1=LO), entries={})
        # single-port range fully owned
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value={}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            port, why = workers.allocate_port(self.doc(w1=LO), LO, LO)
        self.assertIsNone(port)
        self.assertTrue(why)


# ------------------------------------------------------- supervisor dispatch


class SupervisorCase(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        with mock.patch.object(supervisor_mod, "ledger_mod"), \
                mock.patch.object(supervisor_mod, "notify"), \
                mock.patch.object(supervisor_mod, "telemetry"), \
                mock.patch.object(supervisor_mod, "jev"):
            self.sup = supervisor_mod.Supervisor(self.cfg)
        self.sup.log = mock.Mock()
        self.sup.notify_out = mock.Mock(return_value={"ok": True})

    def base_doc(self, task_state="READY"):
        doc = state.initial_document("run-002", "v2.0")
        task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task["state"] = task_state
        return doc, task


class TestDispatchPopulation(SupervisorCase):
    def test_governed_lease_grace_is_frozen_config(self):
        self.assertEqual(self.cfg.extra["timeouts"]["lease_grace_seconds"], 60)

    def dispatch(self, doc, task, *, port=(LO, "")):
        wt = Path("/wt/task-001-builder")
        with mock.patch.object(supervisor_mod.workers, "allocate_port",
                               return_value=port) as alloc, \
                mock.patch.object(supervisor_mod.workers, "create_worker",
                                  return_value=ok_result()), \
                mock.patch.object(supervisor_mod.workers, "worktree_path",
                                  return_value=wt), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/j")) as wj, \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=ok_result()) as sj, \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p")):
            self.sup.dispatch_builder(doc, task)
        return alloc, wj, sj, wt

    def test_builder_record_carries_worktree_port_and_lease(self):
        doc, task = self.base_doc()
        _, wj, _, wt = self.dispatch(doc, task)
        record = doc["workers"]["task-001-builder"]
        self.assertEqual(record["worktree"], str(wt))
        self.assertEqual(record["port"], LO)
        self.assertEqual(wj.call_args.kwargs.get("port"), LO)
        expected = clock.parse(task["assigned_at"]) + timedelta(
            seconds=self.cfg.extra["timeouts"]["builder"]
            + self.cfg.extra["timeouts"]["lease_grace_seconds"])
        actual = clock.parse(record["lease_expires_at"])
        self.assertLess(abs((actual - expected).total_seconds()), 5)

    def test_dispatch_pops_the_retained_worktree_claim(self):
        doc, task = self.base_doc()
        task["retained_worktrees"] = {"/wt/task-001-builder": {"why": "TERMINAL_REAP"}}
        self.dispatch(doc, task)
        self.assertNotIn("/wt/task-001-builder", task["retained_worktrees"])

    def test_port_exhaustion_is_an_explicit_dispatch_failure(self):
        doc, task = self.base_doc()
        _, _, sj, _ = self.dispatch(doc, task, port=(None, "range exhausted"))
        self.assertEqual(task["state"], "BLOCKED")
        sj.assert_not_called()

    def test_fixer_gets_a_port_and_reviewer_does_not(self):
        doc, task = self.base_doc("FIX_REQUIRED")
        task.update({"branch": "task/task-001", "pr": 7})
        from control import routing
        doc["prs"]["7"] = routing.blank_pr_record(7, "TASK-001", "task/task-001")
        wt = Path("/wt/task-001-fixer-1")
        with mock.patch.object(supervisor_mod.providers, "may", return_value=True), \
                mock.patch.object(supervisor_mod.workers, "allocate_port",
                                  return_value=(LO, "")) as alloc, \
                mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                                  return_value=(wt, "")), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/j")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=ok_result()), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p")):
            self.sup.dispatch_fixer(doc, task, 7, [{"id": "F1"}])
        alloc.assert_called_once()
        record = doc["workers"]["task-001-fixer-1"]
        self.assertEqual(record["port"], LO)
        self.assertEqual(record["worktree"], str(wt))
        self.assertIsNotNone(record["lease_expires_at"])

        doc2, task2 = self.base_doc("PR_OPEN")
        task2.update({"branch": "task/task-001", "pr": 7})
        doc2["prs"]["7"] = routing.blank_pr_record(7, "TASK-001", "task/task-001")
        sha = "a" * 40
        wt2 = Path("/wt/task-001-review-1")
        with mock.patch.object(supervisor_mod.providers, "may", return_value=True), \
                mock.patch.object(supervisor_mod.workers, "allocate_port") as alloc2, \
                mock.patch.object(supervisor_mod.gh, "pr_diff_sha",
                                  return_value=sha), \
                mock.patch.object(supervisor_mod.evidence, "collect",
                                  return_value=[]), \
                mock.patch.object(supervisor_mod.evidence, "render",
                                  return_value=""), \
                mock.patch.object(supervisor_mod.routing, "material_diff_hash",
                                  return_value="h"), \
                mock.patch.object(supervisor_mod.workers, "acquire_worktree",
                                  return_value=(wt2, "")), \
                mock.patch.object(supervisor_mod.workers, "write_job",
                                  return_value=Path("/tmp/j")), \
                mock.patch.object(supervisor_mod.workers, "start_job",
                                  return_value=ok_result()), \
                mock.patch.object(supervisor_mod.prompts, "write",
                                  return_value=Path("/tmp/p")):
            self.sup.dispatch_reviewer(doc2, task2, 7)
        alloc2.assert_not_called()
        record2 = doc2["workers"]["task-001-review-1"]
        self.assertIsNone(record2["port"])
        self.assertEqual(record2["worktree"], str(wt2))
        self.assertIsNotNone(record2["lease_expires_at"])


# ------------------------------------------------- reap transfer + guard


class TestReapOwnershipTransfer(SupervisorCase):
    def seed_finished_builder(self, doc, task):
        task["worker"] = "task-001-builder"
        task["branch"] = "task/task-001"
        doc["workers"]["task-001-builder"] = state.new_worker_record(
            "builder", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            worktree="/wt/task-001-builder")

    def reap(self, doc, status):
        with mock.patch.object(supervisor_mod.workers, "read_status",
                               return_value=status), \
                mock.patch.object(self.sup, "on_builder_finished") as handler:
            self.sup.reap_workers(doc)
        return handler

    def test_terminal_reap_transfers_worktree_to_task_retained_map(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed_finished_builder(doc, task)
        handler = self.reap(doc, {"phase": "DONE", "outcome": "SUCCESS"})
        handler.assert_called_once()
        self.assertNotIn("task-001-builder", doc["workers"])
        self.assertIn("/wt/task-001-builder", task["retained_worktrees"])

    def test_reap_on_human_required_task_never_runs_the_role_handler(self):
        doc, task = self.base_doc("HUMAN_REQUIRED")
        self.seed_finished_builder(doc, task)
        handler = self.reap(doc, {"phase": "FAILED", "outcome": "FAILED"})
        handler.assert_not_called()
        self.assertNotIn("task-001-builder", doc["workers"])
        self.assertEqual(task["state"], "HUMAN_REQUIRED")
        self.assertIn("/wt/task-001-builder", task["retained_worktrees"])

    def test_reap_under_human_required_retains_a_reviewer_worktree_too(self):
        """The guard skips the role handler, which is also what would have
        physically removed a reviewer's worktree - so on this path every
        role's worktree must transfer to RETAINED, never end up unclaimed."""
        doc, task = self.base_doc("HUMAN_REQUIRED")
        task["worker"] = "task-001-review-1"
        doc["workers"]["task-001-review-1"] = state.new_worker_record(
            "reviewer", "TASK-001", "review/c1/task/task-001",
            clock.iso(clock.now(TZ)), worktree="/wt/task-001-review-1")
        with mock.patch.object(supervisor_mod.workers, "read_status",
                               return_value={"phase": "DONE",
                                             "outcome": "SUCCESS"}), \
                mock.patch.object(self.sup, "on_reviewer_finished") as handler:
            self.sup.reap_workers(doc)
        handler.assert_not_called()
        self.assertNotIn("task-001-review-1", doc["workers"])
        self.assertIn("/wt/task-001-review-1", task["retained_worktrees"])

    def test_reap_under_human_required_leaves_no_d2_contradiction(self):
        """After the guard pops the record, the parked task must present no
        worker-reference contradiction to real D2 reconciliation."""
        doc, task = self.base_doc("HUMAN_REQUIRED")
        self.seed_finished_builder(doc, task)
        self.reap(doc, {"phase": "FAILED", "outcome": "FAILED"})
        from control import reconcile as reconcile_mod
        self.assertEqual(reconcile_mod.reconcile(doc, tz=TZ), [])

    def test_stale_recycle_removes_physically_and_does_not_retain(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed_finished_builder(doc, task)
        task["last_progress_at"] = iso_offset(-3600)
        task["progress_marker"] = 10
        status = {"phase": "RUNNING", "prompt_accepted": True,
                  "output_bytes": 10, "agent_pid": 4242}
        with mock.patch.object(supervisor_mod.workers, "read_status",
                               return_value=status), \
                mock.patch.object(supervisor_mod.workers, "process_alive",
                                  return_value=True), \
                mock.patch.object(supervisor_mod.workers, "remove_worker") as rm:
            self.sup.detect_stale(doc)
        rm.assert_called_once_with("task-001-builder")
        self.assertNotIn("/wt/task-001-builder",
                         task.get("retained_worktrees", {}))

    def test_freeze_sweep_retains_every_live_worker_worktree(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed_finished_builder(doc, task)
        providers.ensure(doc)
        budget.ensure(doc, 100.0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(config, "RUNTIME_DIR", Path(tmp.name)), \
                mock.patch.object(supervisor_mod.gh, "git",
                                  return_value=ok_result()), \
                mock.patch.object(supervisor_mod.workers, "close_worker",
                                  return_value=ok_result()):
            self.sup.freeze(doc)
        self.assertIn("/wt/task-001-builder", task["retained_worktrees"])
        self.assertEqual(doc["workers"], {})


# ------------------------------------------------------ lease expiry check


class TestLeaseExpiry(SupervisorCase):
    def seed(self, doc, task, *, lease_offset=-120):
        task["worker"] = "task-001-builder"
        doc["workers"]["task-001-builder"] = state.new_worker_record(
            "builder", "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            worktree="/wt/x", lease_expires_at=iso_offset(lease_offset))

    def detect(self, doc, *, entries, alive, status=None):
        with mock.patch.object(supervisor_mod.proc, "worker_entry_processes",
                               return_value=entries), \
                mock.patch.object(supervisor_mod.proc, "verified_alive",
                                  return_value=alive), \
                mock.patch.object(supervisor_mod.workers, "read_status",
                                  return_value=status or {"agent_pid": 4242,
                                                          "agent_start_ticks": 9}):
            self.sup.detect_lease_expiry(doc)

    def open_lease_interventions(self, doc):
        return [r for r in doc.get("interventions", {}).values()
                if r["condition_code"] == "lease_expired"]

    def test_expired_lease_with_live_process_escalates_without_a_kill(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed(doc, task)
        self.detect(doc, entries={"task-001-builder": 500}, alive=False)
        self.assertEqual(task["state"], "HUMAN_REQUIRED")
        records = self.open_lease_interventions(doc)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "HUMAN_APPARATUS_AUTHORISATION")
        self.assertIn("task-001-builder", doc["workers"])  # ownership retained

    def test_expired_lease_with_dead_process_creates_no_obligation(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed(doc, task)
        self.detect(doc, entries={}, alive=False)
        self.assertEqual(task["state"], "ACTIVE")
        self.assertEqual(self.open_lease_interventions(doc), [])

    def test_unexpired_lease_is_untouched(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed(doc, task, lease_offset=3600)
        self.detect(doc, entries={"task-001-builder": 500}, alive=True)
        self.assertEqual(task["state"], "ACTIVE")

    def test_second_pass_does_not_duplicate_the_obligation(self):
        doc, task = self.base_doc("ACTIVE")
        self.seed(doc, task)
        self.detect(doc, entries={"task-001-builder": 500}, alive=False)
        self.detect(doc, entries={"task-001-builder": 500}, alive=False)
        self.assertEqual(len(self.open_lease_interventions(doc)), 1)


# ------------------------------------------------ reverse orphan detection


class DetectOrphansCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_dir = Path(tmp.name)
        patcher = mock.patch.object(config, "WORKER_LOG_DIR", self.log_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo_root = "/repo"
        # workmux's default worktree_dir: the sibling '<project>__worktrees'
        # (verified via `workmux config reference`; no override configured).
        self.managed = "/repo__worktrees"

    def doc(self):
        d = state.initial_document("run-002", "v2.0")
        state.add_task(d, "TASK-001", "T", [], "feature", False, TZ)
        return d

    def run_detect(self, doc, *, worktrees=(), git_ok=True, entries=None,
                   alive=False, listening=frozenset(), ports_ok=True):
        git_result = mock.Mock(ok=git_ok, stdout="\n".join(
            f"worktree {p}" for p in ([self.repo_root] + list(worktrees))))
        with mock.patch.object(reconcile.gh, "git", return_value=git_result), \
                mock.patch.object(reconcile.proc, "worker_entry_processes",
                                  return_value=entries if entries is not None else {}), \
                mock.patch.object(reconcile.proc, "verified_alive",
                                  return_value=alive), \
                mock.patch.object(reconcile.proc, "listening_ports",
                                  return_value=set(listening) if ports_ok else None), \
                mock.patch.object(reconcile.hostcheck, "read_candidate_port_range",
                                  return_value=(3200, 3299)):
            return reconcile.detect_orphans(doc, repo_root=self.repo_root)

    def ids(self, findings):
        return sorted((f.check_id, f.resource_id) for f in findings)

    def write_status(self, worker, *, phase="RUNNING", agent_pid=4242, ticks=100):
        (self.log_dir / f"{worker}.status.json").write_text(json.dumps(
            {"phase": phase, "agent_pid": agent_pid, "agent_start_ticks": ticks}),
            encoding="utf-8")

    def test_unclaimed_worktree_is_orphan_and_claims_are_not(self):
        doc = self.doc()
        doc["workers"]["w1"] = state.new_worker_record(
            "builder", "TASK-001", "b", clock.iso(clock.now(TZ)),
            worktree=f"{self.managed}/active")
        doc["tasks"]["TASK-001"]["retained_worktrees"] = {
            f"{self.managed}/kept": {}}
        findings, scan_ok = self.run_detect(
            doc, worktrees=[f"{self.managed}/active", f"{self.managed}/kept",
                            f"{self.managed}/orphan"])
        self.assertEqual(self.ids(findings),
                         [("ORPHAN_WORKTREE", f"{self.managed}/orphan")])
        self.assertTrue(scan_ok["worktree"])

    def test_unrelated_worktree_outside_the_managed_root_is_ignored(self):
        """A manual/unrelated git worktree of this repository is not a
        Run 002 resource; only worktrees under the workmux-managed root may
        be classified as Run 002 orphans."""
        findings, scan_ok = self.run_detect(
            self.doc(), worktrees=["/home/serina/manual-checkout",
                                   f"{self.managed}/orphan"])
        self.assertEqual(self.ids(findings),
                         [("ORPHAN_WORKTREE", f"{self.managed}/orphan")])
        self.assertTrue(scan_ok["worktree"])

    def test_the_main_checkout_is_never_an_orphan(self):
        findings, _ = self.run_detect(self.doc(), worktrees=[])
        self.assertEqual(findings, [])

    def test_git_failure_fails_closed_with_scan_not_ok(self):
        findings, scan_ok = self.run_detect(
            self.doc(), worktrees=[f"{self.managed}/x"], git_ok=False)
        self.assertFalse(scan_ok["worktree"])
        self.assertNotIn("ORPHAN_WORKTREE", [f.check_id for f in findings])

    def test_live_entry_without_a_worker_record_is_an_orphan_process(self):
        self.write_status("ghost-builder")
        findings, scan_ok = self.run_detect(self.doc(),
                                            entries={"ghost-builder": 1234})
        self.assertIn(("ORPHAN_WORKER_PROCESS", "ghost-builder"),
                      self.ids(findings))
        self.assertTrue(scan_ok["process"])

    def test_a_recorded_worker_entry_is_not_an_orphan(self):
        doc = self.doc()
        doc["workers"]["w1"] = state.new_worker_record(
            "builder", "TASK-001", "b", clock.iso(clock.now(TZ)))
        findings, _ = self.run_detect(doc, entries={"w1": 1234})
        self.assertEqual([f for f in findings
                          if f.check_id == "ORPHAN_WORKER_PROCESS"], [])

    def test_surviving_agent_with_matching_ticks_is_an_orphan(self):
        self.write_status("gone-builder")
        findings, _ = self.run_detect(self.doc(), entries={}, alive=True)
        self.assertIn(("ORPHAN_AGENT_PROCESS", "gone-builder"),
                      self.ids(findings))

    def test_reused_pid_or_missing_ticks_is_never_claimed(self):
        self.write_status("gone-builder")
        for verdict in (False, None):
            with self.subTest(verdict=verdict):
                findings, _ = self.run_detect(self.doc(), entries={},
                                              alive=verdict)
                self.assertEqual([f for f in findings
                                  if f.check_id == "ORPHAN_AGENT_PROCESS"], [])

    def test_unowned_in_range_listener_is_flagged_and_owned_is_not(self):
        doc = self.doc()
        doc["workers"]["w1"] = state.new_worker_record(
            "builder", "TASK-001", "b", clock.iso(clock.now(TZ)), port=3205)
        findings, scan_ok = self.run_detect(doc, listening={3205, 3210})
        self.assertEqual([(f.check_id, f.resource_id) for f in findings
                          if f.check_id == "FOREIGN_OR_ORPHAN_LISTENER"],
                         [("FOREIGN_OR_ORPHAN_LISTENER", "3210")])
        self.assertTrue(scan_ok["port"])

    def test_duplicate_port_assignment_is_a_conflict_finding(self):
        doc = self.doc()
        for name in ("w1", "w2"):
            doc["workers"][name] = state.new_worker_record(
                "builder", "TASK-001", "b", clock.iso(clock.now(TZ)), port=3205)
        findings, _ = self.run_detect(doc)
        self.assertIn("PORT_ASSIGNMENT_CONFLICT",
                      [f.check_id for f in findings])

    def test_listener_scan_failure_fails_closed(self):
        findings, scan_ok = self.run_detect(self.doc(), ports_ok=False)
        self.assertFalse(scan_ok["port"])
        self.assertEqual([f for f in findings
                          if f.check_id == "FOREIGN_OR_ORPHAN_LISTENER"], [])


# ------------------------------------------- orphan annunciation lifecycle


class AnnunciationCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = state.Store(path=root / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.notifier = mock.Mock(spec=notify.Notifier)
        self.notifier.send.return_value = {"ok": True}
        self.cfg = SimpleNamespace(timezone=TZ)
        self.finding = reconcile.OrphanFinding(
            "ORPHAN_WORKTREE", "/wt/orphan", "unclaimed")
        self.all_ok = {"worktree": True, "process": True, "port": True}

    def run_pass(self, findings, scan_ok=None):
        with mock.patch.object(watchdog.reconcile, "detect_orphans",
                               return_value=(findings, scan_ok or self.all_ok)):
            watchdog.annunciate_orphans(self.cfg, self.ledger, self.notifier,
                                        store=self.store)

    def events(self, event_type):
        lines = Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines if l and
                json.loads(l).get("event_type") == event_type]

    def entry(self):
        doc = self.store.read()
        entries = doc.get("orphan_annunciations", {})
        return entries.get("ORPHAN_WORKTREE:/wt/orphan")

    def test_first_observation_sends_once_and_converges_to_delivered(self):
        self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(len(self.events("ORPHAN_DETECTED")), 1)
        self.assertEqual(len(self.events("ORPHAN_ANNUNCIATION_DELIVERED")), 1)
        self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 1)  # no repeat
        self.assertEqual(self.entry()["status"], "DELIVERED")

    def test_failed_send_becomes_retryable_only_via_durable_failure_evidence(self):
        self.notifier.send.return_value = {"ok": False, "status": 500}
        self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(len(self.events("ORPHAN_ANNUNCIATION_FAILED")), 1)
        self.assertEqual(self.entry()["status"], "ATTEMPTING")
        self.notifier.send.return_value = {"ok": True}
        self.run_pass([self.finding])  # converge OBSERVED, fence, resend
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(len(self.events("ORPHAN_DETECTED")), 1)  # never duplicated

    def test_attempting_with_no_evidence_suppresses_automatic_resend(self):
        """A crash between the fence commit and the send leaves ATTEMPTING
        durable with neither DELIVERED nor FAILED evidence - the ambiguous
        attempt is never automatically repeated."""
        with self.store.transaction() as doc:
            doc["orphan_annunciations"] = {
                "ORPHAN_WORKTREE:/wt/orphan": {
                    "occurrence_id": "ORP-aaaaaaaaaaaaaaaa", "status": "ATTEMPTING",
                    "attempt": 1, "first_observed_at": iso_offset(-60),
                    "check_id": "ORPHAN_WORKTREE", "resource_id": "/wt/orphan"}}
        self.run_pass([self.finding])
        self.notifier.send.assert_not_called()
        self.assertEqual(self.entry()["status"], "ATTEMPTING")

    def test_delivered_ledger_evidence_alone_suppresses_resend(self):
        """Delivery evidence survives any number of lost state commits."""
        self.ledger.append(
            "ORPHAN_ANNUNCIATION_DELIVERED", outcome="DELIVERED",
            activity_class="ORCHESTRATION",
            metadata_redacted={"occurrence_id": "ORP-bbbbbbbbbbbbbbbb",
                               "fingerprint": "ORPHAN_WORKTREE:/wt/orphan"})
        with self.store.transaction() as doc:
            doc["orphan_annunciations"] = {
                "ORPHAN_WORKTREE:/wt/orphan": {
                    "occurrence_id": "ORP-bbbbbbbbbbbbbbbb", "status": "ATTEMPTING",
                    "attempt": 1, "first_observed_at": iso_offset(-60),
                    "check_id": "ORPHAN_WORKTREE", "resource_id": "/wt/orphan"}}
        self.run_pass([self.finding])
        self.notifier.send.assert_not_called()
        self.assertEqual(self.entry()["status"], "DELIVERED")

    def test_disappearance_clears_and_recurrence_notifies_with_new_occurrence(self):
        self.run_pass([self.finding])
        first_occ = self.entry()["occurrence_id"]
        self.run_pass([])  # complete scan, affirmatively absent
        self.assertIsNone(self.entry())
        self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertNotEqual(self.entry()["occurrence_id"], first_occ)
        self.assertEqual(len(self.events("ORPHAN_DETECTED")), 2)

    def test_scan_failure_never_clears_occurrence_state(self):
        self.run_pass([self.finding])
        self.run_pass([], scan_ok={"worktree": False, "process": True,
                                   "port": True})
        self.assertIsNotNone(self.entry())

    def failing_append(self, event_type: str):
        """Wrap the real ledger.append to raise exactly once, on the first
        append of the named event type; every other append is real."""
        real = self.ledger.append
        state = {"fired": False}

        def wrapper(evt, **fields):
            if evt == event_type and not state["fired"]:
                state["fired"] = True
                raise OSError("ledger write failed")
            return real(evt, **fields)

        return mock.patch.object(self.ledger, "append", side_effect=wrapper)

    def test_pre_send_ledger_failure_is_safely_retryable(self):
        """ORPHAN_DETECTED failing to append happens BEFORE any external
        effect, so the occurrence may safely return to OBSERVED and retry -
        this is not the post-send ambiguity the fence exists for."""
        with self.failing_append("ORPHAN_DETECTED"):
            self.run_pass([self.finding])
        self.notifier.send.assert_not_called()
        self.assertEqual(self.entry()["status"], "OBSERVED")
        self.run_pass([self.finding])  # healthy pass: fence again, send once
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(len(self.events("ORPHAN_DETECTED")), 1)
        self.assertEqual(len(self.events("ORPHAN_ANNUNCIATION_DELIVERED")), 1)

    def test_post_send_delivered_append_failure_stays_fenced(self):
        """Once the send has begun, a failure is ambiguous - delivery may
        have happened - so the occurrence remains ATTEMPTING and is never
        automatically resent."""
        with self.failing_append("ORPHAN_ANNUNCIATION_DELIVERED"):
            self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 1)
        self.assertEqual(self.entry()["status"], "ATTEMPTING")
        self.run_pass([self.finding])
        self.assertEqual(self.notifier.send.call_count, 1)  # no resend
        self.assertEqual(self.entry()["status"], "ATTEMPTING")

    def test_raised_exception_prose_is_never_persisted(self):
        """Runtime exception text can carry anything - paths, env fragments,
        secret-shaped material. Only finite structural metadata may reach
        the ledger, and a failed observation transaction must produce no
        external notification."""
        canary = "C09-SECRET-CANARY-DO-NOT-PERSIST"
        with mock.patch.object(watchdog.reconcile, "detect_orphans",
                               side_effect=RuntimeError(f"boom {canary} boom")):
            watchdog.annunciate_orphans(self.cfg, self.ledger, self.notifier,
                                        store=self.store)
        self.notifier.send.assert_not_called()
        ledger_text = Path(self.ledger.path).read_text(encoding="utf-8")
        self.assertNotIn(canary, ledger_text)
        self.assertNotIn(canary,
                         Path(self.store.path).read_text(encoding="utf-8"))
        errors = self.events("ORPHAN_ANNUNCIATION_ERROR")
        self.assertEqual(len(errors), 1)  # the failure itself is recorded
        meta = errors[0].get("metadata_redacted") or {}
        self.assertEqual(meta, {"phase": "OBSERVE_TRANSACTION",
                                "error_code": "OBSERVATION_FAILED"})


# ------------------------------------------------ lease_expired resolution


def _fake_config() -> types.SimpleNamespace:
    return types.SimpleNamespace(timezone=TZ, experiment_id="run-002",
                                 max_repair_cycles=3)


class LeaseResolutionCase(unittest.TestCase):
    WORKER = "task-001-builder"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state_path = root / "state.json"
        self.ledger_path = root / "ledger.jsonl"
        self.worker_log = root / "workers"
        self.worker_log.mkdir()
        for patcher in (
            mock.patch.object(cli.config, "STATE_PATH", self.state_path),
            mock.patch.object(cli.config, "LEDGER_PATH", self.ledger_path),
            mock.patch.object(cli.config, "load", _fake_config),
            mock.patch.object(cli.config, "WORKER_LOG_DIR", self.worker_log),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_job_file(self, *, port, raw: bytes | None = None):
        path = self.worker_log / f"{self.WORKER}.job.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(json.dumps({"worker": self.WORKER, "port": port}),
                            encoding="utf-8")

    def pop_worker_record(self):
        """Simulate the HUMAN_REQUIRED terminal reap: record gone, worktree
        already transferred to the task's retained map."""
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        meta = doc["workers"].pop(self.WORKER)
        doc["tasks"]["TASK-001"].setdefault("retained_worktrees", {})[
            meta["worktree"]] = {"why": "TERMINAL_REAP"}
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def seed(self, *, role="builder", port=LO, lock_owner=False,
             with_pr=False) -> str:
        doc = state.initial_document("run-002", "2.0")
        task = state.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": "HUMAN_REQUIRED", "branch": "task/task-001",
                     "worker": self.WORKER})
        if with_pr:
            from control import routing
            task["pr"] = 7
            doc["prs"]["7"] = routing.blank_pr_record(7, "TASK-001",
                                                      "task/task-001")
        doc["workers"][self.WORKER] = state.new_worker_record(
            role, "TASK-001", "task/task-001", clock.iso(clock.now(TZ)),
            worktree="/wt/task-001-builder", port=port,
            lease_expires_at=iso_offset(-600))
        if lock_owner:
            doc["migration_lock"].update({"state": "HELD",
                                          "owner_task": "TASK-001",
                                          "owner_pr": None})
        record, _ = intervention.request(
            doc, type_="HUMAN_APPARATUS_AUTHORISATION", scope="task",
            task_id="TASK-001", reason="seeded", condition_code="lease_expired",
            tz=TZ)
        intervention.acknowledge(doc, record["id"], by="serina", tz=TZ)
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return record["id"]

    def doc(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def resolve(self, iid, outcome, *, entries=None, alive=False,
                listening=frozenset(), listen_ok=True, note=None,
                status=None):
        argv = ["human-resolve", iid, "--outcome", outcome, "--by", "serina"]
        if note is not None:
            argv += ["--note", note]
        buffer = io.StringIO()
        with mock.patch.object(cli.proc, "worker_entry_processes",
                               return_value=entries if entries is not None else {}), \
                mock.patch.object(cli.proc, "verified_alive",
                                  return_value=alive), \
                mock.patch.object(cli.proc, "listening_ports",
                                  return_value=set(listening) if listen_ok
                                  else None), \
                mock.patch.object(cli.workers, "read_status",
                                  return_value=status if status is not None
                                  else {"agent_pid": 4242,
                                        "agent_start_ticks": 100}), \
                contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_no_action_parks_and_retains_everything(self):
        iid = self.seed(lock_owner=True)
        code, _ = self.resolve(iid, "NO_ACTION",
                               entries={self.WORKER: 500}, alive=True)
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "HUMAN_REQUIRED")
        self.assertIn(self.WORKER, doc["workers"])
        self.assertEqual(doc["migration_lock"]["owner_task"], "TASK-001")
        self.assertEqual(doc["interventions"][iid]["status"], "RESOLVED")

    def assert_refused(self, iid, outcome, **kwargs):
        before = self.doc()
        code, out = self.resolve(iid, outcome, **kwargs)
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)
        self.assertEqual(self.doc(), before)

    def test_retry_refused_while_the_entry_process_is_alive(self):
        iid = self.seed()
        self.assert_refused(iid, "RETRY", entries={self.WORKER: 500})

    def test_retry_refused_while_the_recorded_agent_is_alive(self):
        iid = self.seed()
        self.assert_refused(iid, "RETRY", alive=True)

    def test_retry_refused_when_agent_identity_is_ambiguous(self):
        iid = self.seed()
        self.assert_refused(iid, "RETRY", alive=None)

    def test_retry_refused_while_the_assigned_port_is_listening(self):
        iid = self.seed()
        self.assert_refused(iid, "RETRY", listening={LO})

    def test_fail_refused_while_listener_survives_even_with_a_note(self):
        iid = self.seed(lock_owner=True)
        self.assert_refused(iid, "FAIL", listening={LO},
                            note="schema attested safe")

    def test_listener_scan_failure_fails_closed(self):
        iid = self.seed()
        self.assert_refused(iid, "RETRY", listen_ok=False)

    def test_retry_with_everything_absent_redispatches_the_builder(self):
        iid = self.seed(lock_owner=True)
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        doc = self.doc()
        task = doc["tasks"]["TASK-001"]
        self.assertEqual(task["state"], "READY")
        self.assertNotIn(self.WORKER, doc["workers"])
        self.assertIn("/wt/task-001-builder", task["retained_worktrees"])
        self.assertEqual(doc["migration_lock"]["owner_task"], "TASK-001")

    def test_retry_targets_are_per_role(self):
        # The fixer target is PR_OPEN, not FIX_REQUIRED: FIX_REQUIRED is
        # D2-reconcilable, so resolving into it with the record released
        # would hand the Watchdog a stale task["worker"] contradiction in
        # the window before redispatch. PR_OPEN is route_awaiting_dispatch's
        # designed waiting state - REVIEW_FAIL verdict + pending findings
        # dispatch a fresh fixer from there.
        for role, target, with_pr in (("fixer", "PR_OPEN", True),
                                      ("reviewer", "PR_OPEN", True)):
            with self.subTest(role=role):
                iid = self.seed(role=role, port=None, with_pr=with_pr)
                code, _ = self.resolve(iid, "RETRY")
                self.assertEqual(code, 0)
                self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"],
                                 target)

    def reconcile_findings(self):
        """Real D2 reconciliation against the post-resolution document -
        exactly what the Watchdog would run before any Supervisor
        redispatch. No observation mocks are needed: the assertion is that
        the resolution left the task in a state D2 does not gate on, so the
        loop never reaches an observation."""
        return reconcile.reconcile(self.doc(), tz=TZ)

    def test_lease_retry_creates_no_d2_contradiction_for_any_role(self):
        """RETRY releases the worker record; if the target state were
        reconcilable, the retained task["worker"] name would be an
        ORPHANED_TASK_WORKER_REF the moment the Watchdog ran - a freeze
        caused by the resolution itself."""
        for role, with_pr in (("builder", False), ("fixer", True),
                              ("reviewer", True)):
            with self.subTest(role=role):
                iid = self.seed(role=role, port=None, with_pr=with_pr)
                code, _ = self.resolve(iid, "RETRY")
                self.assertEqual(code, 0)
                doc = self.doc()
                self.assertNotIn(self.WORKER, doc["workers"])
                findings = self.reconcile_findings()
                self.assertEqual(
                    [f.check_id for f in findings
                     if f.check_id in ("ORPHANED_TASK_WORKER_REF",
                                       "MISSING_TASK_WORKER_REF")], [],
                    f"lease RETRY for {role} handed D2 a worker-reference "
                    f"contradiction: {findings}")

    def test_lease_retry_after_terminal_reap_is_equally_clean(self):
        """The HUMAN_REQUIRED reap guard pops the record before any human
        resolves; RETRY must still verify absence from the status file and
        resolve into a non-reconcilable state."""
        iid = self.seed(role="fixer", port=None, with_pr=True)
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        meta = doc["workers"].pop(self.WORKER)  # the reap guard's pop
        doc["tasks"]["TASK-001"].setdefault("retained_worktrees", {})[
            meta["worktree"]] = {"why": "TERMINAL_REAP"}
        self.state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        self.assertEqual(self.reconcile_findings(), [])  # parked: no claim
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "PR_OPEN")
        self.assertEqual(
            [f.check_id for f in self.reconcile_findings()
             if f.check_id in ("ORPHANED_TASK_WORKER_REF",
                               "MISSING_TASK_WORKER_REF")], [])

    def test_post_reap_retry_and_fail_refuse_while_job_file_port_listens(self):
        """The terminal reap removed the worker record - but the governed
        PORT lives durably in <worker>.job.json, and a surviving dev-server
        listener on it must still block RETRY and FAIL."""
        iid = self.seed(role="fixer", port=LO, with_pr=True)
        self.pop_worker_record()
        self.write_job_file(port=LO)
        self.assert_refused(iid, "RETRY", listening={LO})
        self.assert_refused(iid, "FAIL", listening={LO},
                            note="schema attested safe")

    def test_post_reap_retry_succeeds_once_the_job_file_port_is_free(self):
        iid = self.seed(role="fixer", port=LO, with_pr=True)
        self.pop_worker_record()
        self.write_job_file(port=LO)
        code, _ = self.resolve(iid, "RETRY")
        self.assertEqual(code, 0)
        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "PR_OPEN")

    def test_unreadable_job_file_fails_closed_when_the_record_is_gone(self):
        """Job evidence exists but cannot be read: the governed port it may
        own is unprovable, so the resolution must refuse rather than
        silently conclude 'no port'."""
        iid = self.seed(role="fixer", port=LO, with_pr=True)
        self.pop_worker_record()
        self.write_job_file(port=None, raw=b"\x00not json")
        self.assert_refused(iid, "RETRY")

    def test_lease_fail_creates_no_d2_contradiction(self):
        iid = self.seed(role="builder", port=None)
        code, _ = self.resolve(iid, "FAIL", note="schema attested safe")
        self.assertEqual(code, 0)
        self.assertEqual(self.doc()["tasks"]["TASK-001"]["state"], "FAILED")
        self.assertEqual(self.reconcile_findings(), [])

    def test_no_assigned_port_skips_the_listener_precondition(self):
        iid = self.seed(port=None)
        listener_probe = mock.Mock(side_effect=AssertionError("must not probe"))
        with mock.patch.object(cli.proc, "listening_ports", listener_probe):
            with mock.patch.object(cli.proc, "worker_entry_processes",
                                   return_value={}), \
                    mock.patch.object(cli.proc, "verified_alive",
                                      return_value=False), \
                    mock.patch.object(cli.workers, "read_status",
                                      return_value={"agent_pid": 4242,
                                                    "agent_start_ticks": 1}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["human-resolve", iid, "--outcome", "RETRY",
                                 "--by", "serina"])
        self.assertEqual(code, 0)

    def test_fail_with_everything_absent_uses_note_guarded_lock_release(self):
        iid = self.seed(lock_owner=True)
        code, out = self.resolve(iid, "FAIL")  # no note: refused by C-15 rule
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)
        code, _ = self.resolve(iid, "FAIL", note="schema attested safe")
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "FAILED")
        self.assertIsNone(doc["migration_lock"]["owner_task"])
        self.assertNotIn(self.WORKER, doc["workers"])


# --------------------------------------------------------- worker entry


class TestWorkerEntry(unittest.TestCase):
    def test_port_reaches_the_agent_env_and_ticks_reach_the_status_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        prompt = root / "p.md"
        prompt.write_text("go", encoding="utf-8")
        job = {
            "worker": "w1", "role": "builder", "provider": "claude",
            "model": None, "effort": None, "fallback_model": None,
            "task_id": "TASK-001", "pr": None, "worktree": str(root),
            "prompt_path": str(prompt),
            "status_path": str(root / "w1.status.json"),
            "output_path": str(root / "w1.out"),
            "last_message_path": str(root / "w1.last.txt"),
            "timezone": TZ, "hard_timeout_seconds": 5, "port": 3210,
        }
        job_path = root / "w1.job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")

        fake = mock.Mock()
        fake.pid = 4242
        fake.poll.return_value = 0
        fake.wait.return_value = 0
        fake.stdin = io.BytesIO()
        popen = mock.Mock(return_value=fake)
        with mock.patch.object(worker_entry.subprocess, "Popen", popen), \
                mock.patch.object(worker_entry.proc, "start_ticks",
                                  return_value=55555):
            worker_entry.main(str(job_path))

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env.get("PORT"), "3210")
        status = json.loads((root / "w1.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["agent_start_ticks"], 55555)
        self.assertEqual(status["agent_pid"], 4242)


if __name__ == "__main__":
    unittest.main()
