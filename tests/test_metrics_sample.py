"""C-08a: runtime resource sampling.

control/metrics.py is a bounded, pure sample builder - it composes the
existing C-08c host readers and C-09 resource-observation primitives into
one fixed-shape finite metadata dict, and never mutates state, writes the
ledger, notifies, or evaluates thresholds. control/watchdog.py owns the
governed 300-second monotonic cadence, the RESOURCE_SAMPLE append and
top-level failure containment.

Failure semantics under test everywhere: an unavailable observation is
null plus an explicit *_observed=false - never zero, never invented, and
never exception prose in the ledger.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (  # noqa: E402
    hostcheck,
    ledger as ledger_mod,
    metrics,
    state,
    watchdog,
)

TZ = "Pacific/Auckland"

# The complete governed RESOURCE_SAMPLE key set, spelled literally so a
# key silently dropped from (or added to) production is caught even if
# metrics.METADATA_KEYS is edited in the same mistake.
EXPECTED_KEYS = frozenset({
    "load1", "logical_cpus", "cpu_observed",
    "mem_available_bytes", "ram_observed",
    "disk_free_bytes", "disk_observed",
    "inotify_instances_used", "inotify_watches_used",
    "inotify_max_instances", "inotify_max_watches", "inotify_observed",
    "ports_owned", "ports_listening_in_range",
    "port_range_lo", "port_range_hi", "ports_observed",
    "worker_records", "live_worker_entries", "verified_agents",
    "processes_observed",
    "worktrees_registered_managed", "worktrees_active",
    "worktrees_retained", "worktrees_orphan", "worktrees_observed",
    "browser_count", "browser_observed",
})


def _doc(workers=None, tasks=None):
    doc = state.initial_document("run-002", "v2.0")
    if workers:
        doc["workers"] = workers
    if tasks:
        doc["tasks"] = tasks
    return doc


class SampleFixtureCase(unittest.TestCase):
    """metrics.sample() against fully deterministic inputs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A pretend repository root; managed worktrees live in the
        # workmux-convention sibling '<name>__worktrees'.
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.managed = self.root / "repo__worktrees"
        self.managed.mkdir()
        self.job_dir = self.root / "workers"
        self.job_dir.mkdir()
        # C-05 evidence root. Created but empty by default, so the fixture
        # case is hermetic - before C-05.2 these tests read the real
        # .runtime/evidence - and the default browser answer is a truthful
        # complete zero rather than an accident of the host.
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()

        # Real fixture files for the readers that accept path constants.
        self.loadavg = self.root / "loadavg"
        self.loadavg.write_text("1.25 0.50 0.20 1/234 9999\n", encoding="utf-8")
        self.meminfo = self.root / "meminfo"
        self.meminfo.write_text(
            "MemTotal:       16000000 kB\nMemAvailable:    8192000 kB\n",
            encoding="utf-8")
        self.max_watches = self.root / "max_user_watches"
        self.max_watches.write_text("65536\n", encoding="utf-8")
        self.max_instances = self.root / "max_user_instances"
        self.max_instances.write_text("128\n", encoding="utf-8")

    def _status_file(self, worker: str, agent_pid: int, ticks: int):
        (self.job_dir / f"{worker}.status.json").write_text(
            json.dumps({"phase": "RUNNING", "agent_pid": agent_pid,
                        "agent_start_ticks": ticks}), encoding="utf-8")

    def run_sample(self, doc, *, listening=frozenset({4210, 4211}),
                   entries={"task-001-builder": 111},
                   alive_pairs=frozenset({(2222, 777)}),
                   unverifiable=frozenset(), evidence=None,
                   registered=None, fail=frozenset()):
        """Run metrics.sample with every observation deterministic.

        `fail` names observation classes whose readers break for the test:
        cpu, ram, disk, inotify, ports, processes, worktrees.

        `unverifiable` names (pid, ticks) pairs for which verified_alive
        answers None - running, but identity unconfirmable. That third
        answer is distinct from False and the browser sample treats it as
        such, so the fixture has to be able to produce it.
        """
        if registered is None:
            registered = (True, set())
        self.verified_calls = []

        def _raise(*_a, **_k):
            raise hostcheck.HostCheckError("fixture: reader unavailable")

        def _verified(pid, ticks):
            self.verified_calls.append((pid, ticks))
            if (pid, ticks) in unverifiable:
                return None
            return (pid, ticks) in alive_pairs

        patches = [
            mock.patch.object(metrics.hostcheck, "LOADAVG_PATH", self.loadavg),
            mock.patch.object(metrics.hostcheck, "MEMINFO_PATH", self.meminfo),
            mock.patch.object(metrics.hostcheck, "INOTIFY_MAX_WATCHES_PATH",
                              self.max_watches),
            mock.patch.object(metrics.hostcheck, "INOTIFY_MAX_INSTANCES_PATH",
                              self.max_instances),
            mock.patch.object(metrics.hostcheck, "logical_cpu_count",
                              _raise if "cpu" in fail else (lambda: 8)),
            mock.patch.object(metrics.hostcheck, "read_disk_free_bytes",
                              _raise if "disk" in fail
                              else (lambda path: 123456789)),
            mock.patch.object(
                metrics.hostcheck, "current_real_uid_inotify_usage",
                _raise if "inotify" in fail else (lambda: (7, 4200))),
            mock.patch.object(
                metrics.hostcheck, "read_candidate_port_range",
                _raise if "ports" in fail else (lambda: (4200, 4260))),
            mock.patch.object(
                metrics.proc, "listening_ports",
                mock.Mock(return_value=None if "ports" in fail
                          else set(listening))),
            mock.patch.object(
                metrics.proc, "worker_entry_processes",
                lambda job_dir: None if "processes" in fail else dict(entries)),
            mock.patch.object(metrics.proc, "verified_alive", _verified),
            mock.patch.object(metrics.config, "WORKER_LOG_DIR", self.job_dir),
            # gate_evidence.scan_sidecars reads config.EVIDENCE_DIR from the
            # same module object, so this one patch scopes the browser
            # observation without metrics growing a parameter for it.
            mock.patch.object(metrics.config, "EVIDENCE_DIR",
                              self.evidence if evidence is None else evidence),
            mock.patch.object(
                metrics.reconcile, "registered_worktrees",
                lambda repo_root: (False, set()) if "worktrees" in fail
                else registered),
        ]
        if "cpu" in fail:
            patches.append(mock.patch.object(metrics.hostcheck, "read_load1",
                                             _raise))
        if "ram" in fail:
            patches.append(mock.patch.object(
                metrics.hostcheck, "read_mem_available_bytes", _raise))
        self._port_mock = patches[8].new  # for call-argument assertions
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return metrics.sample(doc, repo_root=self.repo)

    # ---------------------------------------------------------- schema

    def test_metadata_key_set_is_exactly_the_governed_allowlist(self):
        result = self.run_sample(_doc())
        self.assertEqual(set(result), EXPECTED_KEYS)
        self.assertEqual(metrics.METADATA_KEYS, EXPECTED_KEYS)

    def test_every_value_is_a_finite_scalar_or_null(self):
        result = self.run_sample(_doc())
        for key, value in result.items():
            self.assertIsInstance(
                value, (int, float, bool, type(None)),
                f"{key} must be a finite scalar or null, got {type(value)}")

    # ------------------------------------------------- host resources

    def test_cpu_fields_come_from_the_real_loadavg_reader(self):
        result = self.run_sample(_doc())
        self.assertEqual(result["load1"], 1.25)
        self.assertEqual(result["logical_cpus"], 8)
        self.assertTrue(result["cpu_observed"])

    def test_ram_bytes_come_from_the_real_meminfo_reader(self):
        result = self.run_sample(_doc())
        self.assertEqual(result["mem_available_bytes"], 8192000 * 1024)
        self.assertTrue(result["ram_observed"])

    def test_disk_free_bytes_recorded(self):
        result = self.run_sample(_doc())
        self.assertEqual(result["disk_free_bytes"], 123456789)
        self.assertTrue(result["disk_observed"])

    def test_inotify_usage_and_ceilings_recorded(self):
        result = self.run_sample(_doc())
        self.assertEqual(result["inotify_instances_used"], 7)
        self.assertEqual(result["inotify_watches_used"], 4200)
        self.assertEqual(result["inotify_max_instances"], 128)
        self.assertEqual(result["inotify_max_watches"], 65536)
        self.assertTrue(result["inotify_observed"])

    # ------------------------------------------------------------ ports

    def test_port_levels_use_the_ipv6_merging_primitive(self):
        """The dual-stack IPv4+IPv6 merge itself is pinned for real in
        tests/test_c09_resource_lifecycle.py; what C-08a must pin is that
        the sampler consumes proc.listening_ports over the governed range
        rather than any IPv4-only or bind-probe substitute."""
        doc = _doc(workers={
            "w1": {"port": 4205}, "w2": {"port": 4206}, "w3": {"port": 4206},
            "w4": {"port": "not-an-int"},
        })
        result = self.run_sample(doc, listening={4210, 4211, 4212})
        self.assertEqual(result["ports_owned"], 2)  # distinct int ports
        self.assertEqual(result["ports_listening_in_range"], 3)
        self.assertEqual(result["port_range_lo"], 4200)
        self.assertEqual(result["port_range_hi"], 4260)
        self.assertTrue(result["ports_observed"])
        self._port_mock.assert_called_once_with(4200, 4260)

    def test_port_observation_failure_is_null_never_zero(self):
        result = self.run_sample(_doc(workers={"w1": {"port": 4205}}),
                                 fail={"ports"})
        self.assertIsNone(result["ports_listening_in_range"])
        self.assertIsNone(result["port_range_lo"])
        self.assertIsNone(result["port_range_hi"])
        self.assertFalse(result["ports_observed"])
        # State-derived ownership is still mechanically available.
        self.assertEqual(result["ports_owned"], 1)

    # -------------------------------------------------------- processes

    def test_process_triple_stays_separate_and_never_sums(self):
        self._status_file("task-001-builder", 2222, 777)   # verified alive
        self._status_file("task-002-builder", 3333, 888)   # not verified
        doc = _doc(workers={"task-001-builder": {}, "task-002-builder": {}})
        result = self.run_sample(doc, entries={"task-001-builder": 111})
        self.assertEqual(result["worker_records"], 2)
        self.assertEqual(result["live_worker_entries"], 1)
        self.assertEqual(result["verified_agents"], 1)
        self.assertTrue(result["processes_observed"])
        # The one worker with both a live entry and a verified agent is one
        # worker, not two: no field may ever contain 111+ or a 2+1 sum.
        self.assertNotIn(3, (result["live_worker_entries"],
                             result["verified_agents"]))

    def test_process_scan_failure_is_null_but_records_still_count(self):
        doc = _doc(workers={"task-001-builder": {}})
        result = self.run_sample(doc, fail={"processes"})
        self.assertIsNone(result["live_worker_entries"])
        self.assertFalse(result["processes_observed"])
        self.assertEqual(result["worker_records"], 1)

    # -------------------------------------------------------- worktrees

    def test_worktree_dispositions_active_retained_orphan(self):
        active_wt = str(self.managed / "task-001-builder")
        retained_wt = str(self.managed / "task-002-builder")
        orphan_wt = str(self.managed / "task-old-reviewer")
        unmanaged = str(self.root / "elsewhere" / "checkout")
        doc = _doc(
            workers={"task-001-builder": {"worktree": active_wt}},
            tasks={"TASK-002": {"retained_worktrees": {retained_wt: "kept"}}},
        )
        result = self.run_sample(doc, registered=(
            True, {str(self.repo), active_wt, retained_wt, orphan_wt,
                   unmanaged}))
        self.assertEqual(result["worktrees_registered_managed"], 3)
        self.assertEqual(result["worktrees_active"], 1)
        self.assertEqual(result["worktrees_retained"], 1)
        self.assertEqual(result["worktrees_orphan"], 1)
        self.assertTrue(result["worktrees_observed"])

    def test_retained_is_never_counted_as_active(self):
        retained_wt = str(self.managed / "task-002-builder")
        doc = _doc(tasks={"TASK-002": {"retained_worktrees":
                                       {retained_wt: "kept"}}})
        result = self.run_sample(doc, registered=(True, {retained_wt}))
        self.assertEqual(result["worktrees_active"], 0)
        self.assertEqual(result["worktrees_retained"], 1)
        self.assertEqual(result["worktrees_orphan"], 0)

    def test_failed_worktree_observation_never_infers_an_orphan_count(self):
        active_wt = str(self.managed / "task-001-builder")
        doc = _doc(workers={"task-001-builder": {"worktree": active_wt}})
        result = self.run_sample(doc, fail={"worktrees"})
        self.assertIsNone(result["worktrees_registered_managed"])
        self.assertIsNone(result["worktrees_orphan"])
        self.assertFalse(result["worktrees_observed"])
        # State-derived dispositions remain mechanically available.
        self.assertEqual(result["worktrees_active"], 1)
        self.assertEqual(result["worktrees_retained"], 0)

    # ---------------------------------------------------------- browser
    #
    # browser_count is an INFERENCE over persisted C-05 evidence: sidecars
    # plus (pid, start_ticks) verification. The rule under test throughout
    # is all-or-nothing - a verified-live subset is not a smaller true
    # count, it is an unknown count with some known members, and reporting
    # it would understate real browser pressure exactly when observation
    # is degraded.

    def _attempt(self, task="TASK001", sha="a" * 40, ordinal=1, *,
                 sidecar=None, marker=None, root=None):
        """One evidence attempt; `sidecar` is raw text so a test can write
        a torn or unrecognised one."""
        base = self.evidence if root is None else root
        path = base / task / sha / f"attempt-{ordinal:04d}"
        (path / "accessibility").mkdir(parents=True)
        if sidecar is not None:
            (path / "accessibility" / "browser.json").write_text(
                sidecar, encoding="utf-8")
        if marker is not None:
            (path / marker).write_text("", encoding="utf-8")
        return path

    @staticmethod
    def _open(pid=2222, ticks=777, state="OPEN"):
        return json.dumps({"pid": pid, "start_ticks": ticks, "state": state,
                           "opened_at": "2026-09-29T00:00:00Z",
                           "closed_at": None})

    def test_absent_evidence_root_is_unknown_not_zero(self):
        # The scanner cannot see WHY the root is absent - never allocated,
        # or deleted underneath it - and only one of those means no browser.
        result = self.run_sample(_doc(), evidence=self.root / "nope")
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_present_empty_evidence_root_is_a_complete_zero(self):
        result = self.run_sample(_doc())
        self.assertEqual(result["browser_count"], 0)
        self.assertIs(result["browser_observed"], True)

    def test_one_verified_live_open_browser_counts_one(self):
        self._attempt(sidecar=self._open())
        result = self.run_sample(_doc())
        self.assertEqual(result["browser_count"], 1)
        self.assertIs(result["browser_observed"], True)

    def test_two_live_browsers_in_different_tasks_count_two(self):
        self._attempt(task="TASK001", sidecar=self._open(2222, 777))
        self._attempt(task="TASK002", sidecar=self._open(3333, 888))
        result = self.run_sample(_doc(),
                                 alive_pairs={(2222, 777), (3333, 888)})
        self.assertEqual(result["browser_count"], 2)
        self.assertIs(result["browser_observed"], True)

    def test_duplicate_open_records_of_one_identity_count_one_browser(self):
        # Two sidecars can describe the SAME child - a rerun that observed
        # it, evidence copied between attempts. The count is of browser
        # processes, so the identical (pid, start_ticks) is one browser,
        # never two, or bookkeeping would invent browser pressure.
        self._attempt(ordinal=1, sidecar=self._open(2222, 777))
        self._attempt(ordinal=2, sidecar=self._open(2222, 777))
        result = self.run_sample(_doc())
        self.assertEqual(result["browser_count"], 1)
        self.assertNotEqual(result["browser_count"], 2)
        self.assertIs(result["browser_observed"], True)

    def test_verified_dead_open_browser_counts_zero_and_stays_observed(self):
        self._attempt(sidecar=self._open(9999, 1))
        result = self.run_sample(_doc(), alive_pairs=frozenset())
        self.assertEqual(result["browser_count"], 0)
        self.assertIs(result["browser_observed"], True)

    def test_closed_sidecar_counts_zero_without_verifying_identity(self):
        # run.js writes CLOSED only after browser.close() RETURNED, so it
        # is positive evidence of no live browser - no /proc lookup needed.
        self._attempt(sidecar=self._open(2222, 777, state="CLOSED"))
        result = self.run_sample(_doc())
        self.assertEqual(result["browser_count"], 0)
        self.assertIs(result["browser_observed"], True)
        self.assertNotIn((2222, 777), self.verified_calls)

    def test_closed_sidecar_does_not_poison_a_live_sibling(self):
        self._attempt(ordinal=1, sidecar=self._open(2222, 777, state="CLOSED"))
        self._attempt(ordinal=2, sidecar=self._open(3333, 888))
        result = self.run_sample(_doc(), alive_pairs={(3333, 888)})
        self.assertEqual(result["browser_count"], 1)
        self.assertIs(result["browser_observed"], True)

    def test_attempt_with_no_sidecar_makes_the_whole_sample_unknown(self):
        self._attempt(ordinal=1, sidecar=self._open())
        self._attempt(ordinal=2)  # MISSING - a browser may exist unrecorded
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_unreadable_sidecar_makes_the_whole_sample_unknown(self):
        path = self._attempt(sidecar=self._open())
        target = path / "accessibility" / "browser.json"
        target.unlink()
        target.mkdir()  # UNREADABLE, with no dependence on privilege
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_malformed_sidecar_makes_the_whole_sample_unknown(self):
        self._attempt(sidecar='{"state": "OPEN", "pid": 22')
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_open_sidecar_without_a_pid_makes_the_sample_unknown(self):
        # run.js writes pid null whenever the browser child is ambiguous.
        self._attempt(sidecar=self._open(pid=None))
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_open_sidecar_without_start_ticks_makes_the_sample_unknown(self):
        self._attempt(sidecar=self._open(ticks=None))
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_a_non_ok_status_is_unknown_even_when_it_carries_an_identity(self):
        # read_sidecar happens to blank pid/start_ticks whenever the status
        # is not OK, so every fixture above would ALSO be caught by the
        # identity guard - which means those tests do not actually pin this
        # rule. metrics must not lean on another module's invariant: the
        # STATUS alone decides, and a synthetic record proves it does.
        for status in ("MISSING", "UNREADABLE", "INVALID"):
            with self.subTest(status=status):
                record = {"sidecar_status": status, "state": "OPEN",
                          "pid": 2222, "start_ticks": 777,
                          "attempt_dir": str(self.evidence / "x"),
                          "sidecar_path": str(self.evidence / "x" / "b.json"),
                          "lifecycle": None}
                with mock.patch.object(metrics.gate_evidence, "scan_sidecars",
                                       lambda root=None: ([record], True)):
                    result = self.run_sample(_doc())
                self.assertIsNone(result["browser_count"])
                self.assertIs(result["browser_observed"], False)

    def test_unverifiable_open_identity_makes_the_sample_unknown(self):
        self._attempt(sidecar=self._open(4444, 555))
        result = self.run_sample(_doc(), unverifiable={(4444, 555)})
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_a_verified_live_subset_is_never_reported_as_the_count(self):
        # The one that matters. One browser IS known live; the other is
        # running but unverifiable. Reporting 1 would read as "one browser
        # on this host" when the truth is "at least one, possibly two".
        self._attempt(ordinal=1, sidecar=self._open(2222, 777))
        self._attempt(ordinal=2, sidecar=self._open(4444, 555))
        result = self.run_sample(_doc(), unverifiable={(4444, 555)})
        self.assertIsNone(result["browser_count"])
        self.assertNotEqual(result["browser_count"], 1)
        self.assertIs(result["browser_observed"], False)

    def test_incomplete_walk_outranks_an_observed_live_browser(self):
        # A symlinked task directory is refused rather than traversed, so
        # the walk is incomplete however readable the rest was. No chmod:
        # this must hold identically for root and non-root.
        self._attempt(task="TASK001", sidecar=self._open())
        (self.evidence / "TASK002").symlink_to(self.evidence / "TASK001")
        result = self.run_sample(_doc())
        self.assertIsNone(result["browser_count"])
        self.assertIs(result["browser_observed"], False)

    def test_terminal_lifecycle_does_not_change_the_live_count(self):
        # Whether an OPEN sidecar under a finished attempt is a LEAK is
        # orphan classification's question, not this metric's. The count
        # is simply how many browsers are verifiably alive right now.
        self._attempt(sidecar=self._open(),
                      marker="attempt-terminal.marker")
        result = self.run_sample(_doc())
        self.assertEqual(result["browser_count"], 1)
        self.assertIs(result["browser_observed"], True)

    def test_no_unknown_browser_sample_is_ever_reported_as_zero(self):
        unknowns = [
            ("absent root", lambda: None, {"evidence": self.root / "gone"}),
            ("missing sidecar", lambda: self._attempt(ordinal=2), {}),
            ("malformed sidecar",
             lambda: self._attempt(ordinal=3, sidecar="{"), {}),
            ("no identity",
             lambda: self._attempt(ordinal=4, sidecar=self._open(pid=None)),
             {}),
        ]
        for label, build, kwargs in unknowns:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as fresh:
                    self.evidence = Path(fresh)
                    build()
                    result = self.run_sample(_doc(), **kwargs)
                    self.assertIsNone(result["browser_count"], label)
                    self.assertNotEqual(result["browser_count"], 0, label)
                    self.assertIs(result["browser_observed"], False, label)

    # --------------------------------------------- failure independence

    def test_one_unavailable_class_is_null_while_others_still_record(self):
        result = self.run_sample(_doc(), fail={"ram"})
        self.assertIsNone(result["mem_available_bytes"])
        self.assertFalse(result["ram_observed"])
        self.assertNotEqual(result["mem_available_bytes"], 0)
        # Every other class is untouched by RAM being unreadable.
        self.assertEqual(result["load1"], 1.25)
        self.assertEqual(result["disk_free_bytes"], 123456789)
        self.assertTrue(result["inotify_observed"])
        self.assertTrue(result["ports_observed"])
        self.assertEqual(set(result), EXPECTED_KEYS)

    def test_sampler_is_pure_and_mutates_nothing(self):
        doc = _doc(workers={"task-001-builder": {"port": 4205}})
        before = json.dumps(doc, sort_keys=True)
        self.run_sample(doc)
        self.assertEqual(json.dumps(doc, sort_keys=True), before)


class _FlakyLedger:
    """Wraps the real ledger and raises on the named event types - the
    durable sink itself failing, not the sample build."""

    def __init__(self, real, fail_types, message="disk full"):
        self._real = real
        self._fail = fail_types
        self._message = message
        self.attempted = []

    def append(self, event_type, **fields):
        self.attempted.append(event_type)
        if event_type in self._fail:
            raise OSError(self._message)
        return self._real.append(event_type, **fields)


class WatchdogCadenceCase(unittest.TestCase):
    """The Watchdog owns cadence, the RESOURCE_SAMPLE append and top-level
    failure containment. Real Store and Ledger on temp paths."""

    PAYLOAD = {"load1": 1.0, "cpu_observed": True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)
        self.store = state.Store(path=tmp_path / "state.json", tz=TZ)
        self.store.initialise("run-002", "v2.0")
        self.ledger = ledger_mod.Ledger(path=tmp_path / "ledger.jsonl", tz=TZ,
                                        experiment_id="run-002")
        self.cfg = SimpleNamespace(timezone=TZ)

    def _events(self, event_type=None):
        lines = [json.loads(line) for line in
                 Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
                 if line]
        if event_type:
            lines = [e for e in lines if e.get("event_type") == event_type]
        return lines

    def record(self, last, *, now, payload=None, raises=None):
        if raises is not None:
            sampler = mock.Mock(side_effect=raises)
        else:
            sampler = mock.Mock(return_value=dict(payload or self.PAYLOAD))
        with mock.patch.object(watchdog.metrics, "sample", sampler), \
                mock.patch.object(watchdog.time, "monotonic",
                                  return_value=now):
            return watchdog.record_resource_sample(
                self.cfg, self.ledger, last, store=self.store)

    def test_first_pass_samples_immediately(self):
        returned = self.record(None, now=1000.0)
        self.assertEqual(returned, 1000.0)
        events = self._events("RESOURCE_SAMPLE")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["activity_class"], "OBSERVATION")
        self.assertEqual(events[0]["outcome"], "OBSERVED")
        self.assertEqual(events[0]["metadata_redacted"], self.PAYLOAD)

    def test_pass_inside_the_interval_does_not_sample(self):
        returned = self.record(1000.0, now=1000.0 + 299.9)
        self.assertEqual(returned, 1000.0)
        self.assertEqual(self._events("RESOURCE_SAMPLE"), [])

    def test_pass_at_the_interval_appends_a_second_event(self):
        first = self.record(None, now=1000.0)
        returned = self.record(first, now=1300.0)
        self.assertEqual(returned, 1300.0)
        # Append-only: two intervals leave two durable events, never an
        # overwritten latest-value record.
        self.assertEqual(len(self._events("RESOURCE_SAMPLE")), 2)

    def test_interval_constant_is_the_governed_300_seconds(self):
        self.assertEqual(metrics.SAMPLE_INTERVAL_SECONDS, 300)

    def test_missing_state_document_defers_without_fabricating(self):
        absent = state.Store(path=Path(self.tmp.name) / "nope.json", tz=TZ)
        with mock.patch.object(watchdog.time, "monotonic",
                               return_value=1000.0):
            returned = watchdog.record_resource_sample(
                self.cfg, self.ledger, None, store=absent)
        self.assertIsNone(returned)  # retried next pass, no blind advance
        self.assertEqual(self._events(), [])

    def test_sampler_failure_records_fixed_error_and_advances_cadence(self):
        """A broken sampler must not storm every 30-second pass: the
        cadence point advances, so the next attempt is a full interval
        away, and the failure itself is durable finite evidence."""
        returned = self.record(None, now=1000.0,
                               raises=RuntimeError("boom"))
        self.assertEqual(returned, 1000.0)
        self.assertEqual(self._events("RESOURCE_SAMPLE"), [])
        errors = self._events("METRICS_SAMPLE_ERROR")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["metadata_redacted"],
                         {"phase": "RUNTIME_SAMPLE",
                          "error_code": "SAMPLE_FAILED"})

    def test_primary_append_failure_is_contained_and_advances_cadence(self):
        """If the RESOURCE_SAMPLE append itself is the failure, the error
        event is still attempted on the same ledger, the cadence advances,
        and nothing escapes to kill the Watchdog loop."""
        flaky = _FlakyLedger(self.ledger, {"RESOURCE_SAMPLE"})
        with mock.patch.object(watchdog.metrics, "sample",
                               mock.Mock(return_value=dict(self.PAYLOAD))), \
                mock.patch.object(watchdog.time, "monotonic",
                                  return_value=1000.0):
            returned = watchdog.record_resource_sample(
                self.cfg, flaky, None, store=self.store)
        self.assertEqual(returned, 1000.0)
        self.assertEqual(flaky.attempted,
                         ["RESOURCE_SAMPLE", "METRICS_SAMPLE_ERROR"])
        self.assertEqual(self._events("RESOURCE_SAMPLE"), [])
        errors = self._events("METRICS_SAMPLE_ERROR")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["metadata_redacted"],
                         {"phase": "RUNTIME_SAMPLE",
                          "error_code": "SAMPLE_FAILED"})

    def test_total_ledger_unavailability_never_escapes_or_fabricates(self):
        """Both appends failing is measurement-only degradation: nothing
        escapes, the cadence advances, no state mutates, and nothing is
        durably recorded - the durable sink itself is unavailable and the
        code must not falsely claim otherwise."""
        canary = "D2-SECRET-CANARY-DO-NOT-PERSIST"
        flaky = _FlakyLedger(self.ledger,
                             {"RESOURCE_SAMPLE", "METRICS_SAMPLE_ERROR"},
                             message=f"boom {canary}")
        before = Path(self.store.path).read_text(encoding="utf-8")
        with mock.patch.object(watchdog.metrics, "sample",
                               mock.Mock(return_value=dict(self.PAYLOAD))), \
                mock.patch.object(watchdog.time, "monotonic",
                                  return_value=1000.0):
            returned = watchdog.record_resource_sample(
                self.cfg, flaky, None, store=self.store)
        self.assertEqual(returned, 1000.0)
        self.assertEqual(flaky.attempted,
                         ["RESOURCE_SAMPLE", "METRICS_SAMPLE_ERROR"])
        self.assertEqual(self._events(), [])
        self.assertEqual(Path(self.store.path).read_text(encoding="utf-8"),
                         before)
        self.assertNotIn(canary,
                         Path(self.ledger.path).read_text(encoding="utf-8"))

    def test_exception_canary_never_reaches_ledger_or_state(self):
        canary = "D2-SECRET-CANARY-DO-NOT-PERSIST"
        self.record(None, now=1000.0,
                    raises=RuntimeError(f"boom {canary}"))
        self.assertNotIn(canary,
                         Path(self.ledger.path).read_text(encoding="utf-8"))
        self.assertNotIn(canary,
                         Path(self.store.path).read_text(encoding="utf-8"))

    def test_sampling_never_mutates_state_or_escalates(self):
        with self.store.transaction() as doc:
            task = state.add_task(doc, "TASK-001", "T", [], "feature",
                                  False, TZ)
            task["state"] = "ACTIVE"
        before = Path(self.store.path).read_text(encoding="utf-8")
        self.record(None, now=1000.0)
        after = Path(self.store.path).read_text(encoding="utf-8")
        self.assertEqual(before, after)
        doc = self.store.read()
        self.assertEqual(doc["tasks"]["TASK-001"]["state"], "ACTIVE")
        self.assertEqual(doc.get("interventions", {}), {})
        for event in self._events():
            self.assertNotEqual(event.get("event_type"),
                                "HUMAN_INTERVENTION_REQUESTED")
        # record_resource_sample takes no notifier and metrics imports no
        # notification machinery - measurement cannot escalate by shape.
        self.assertFalse(hasattr(metrics, "notify"))
        self.assertFalse(hasattr(metrics, "notifier"))


class LiveReaderSanityCase(unittest.TestCase):
    """One real-host read: types only, never exact values - the fixture
    cases above are the basis of correctness."""

    def test_real_sample_has_the_exact_key_set_and_sane_types(self):
        result = metrics.sample(state.initial_document("run-002", "v2.0"))
        self.assertEqual(set(result), EXPECTED_KEYS)
        if result["cpu_observed"]:
            self.assertIsInstance(result["load1"], float)
            self.assertGreaterEqual(result["logical_cpus"], 1)
        if result["ram_observed"]:
            self.assertGreater(result["mem_available_bytes"], 0)
        # The browser sample is asserted as an INVARIANT, not as fixed
        # values: .runtime/evidence may or may not exist on a given host,
        # and pinning one answer would make this go red for a reason that
        # has nothing to do with the code. Observed implies a real count;
        # unobserved implies null, never a zero.
        self.assertIsInstance(result["browser_observed"], bool)
        if result["browser_observed"]:
            self.assertIsInstance(result["browser_count"], int)
            self.assertGreaterEqual(result["browser_count"], 0)
        else:
            self.assertIsNone(result["browser_count"])


if __name__ == "__main__":
    unittest.main()
