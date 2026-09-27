"""C-16: inherited raw-exception prose must not persist as durable evidence.

Two governed sites: Supervisor.run's tick catch-all (SUPERVISOR_ERROR) and
the preflight gate paths that feed PREFLIGHT_GATE / preflight.json - the
top-level gate-raised handler and hostcheck's per-signal observation
failures. The governed rule (already applied by C-08b.2, C-09, D2/C-14 and
C-08a): durable control-plane error evidence carries finite structural
phase/error codes, never runtime exception prose. Representation changes
only - every failure must stay fail-closed exactly as before.
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
    preflight,
    supervisor,
)

TZ = "Pacific/Auckland"
CANARY = "D2-SECRET-CANARY-DO-NOT-PERSIST"


class SupervisorTickErrorCase(unittest.TestCase):
    """The real Supervisor.run loop, with only tick/sleep/pid-path faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ledger_mod.Ledger(path=Path(self.tmp.name) / "ledger.jsonl",
                                        tz=TZ, experiment_id="run-002")

    def _events(self, event_type=None):
        lines = [json.loads(line) for line in
                 Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
                 if line]
        if event_type:
            lines = [e for e in lines if e.get("event_type") == event_type]
        return lines

    def _run(self, tick_effects):
        """Run the REAL run() loop; each entry is an exception to raise or
        None for a clean tick. The loop stops after the last entry."""
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.cfg = SimpleNamespace(timezone=TZ, poll_seconds=0,
                                  experiment_id="run-002")
        sup.tz = TZ
        sup.ledger = self.ledger
        sup.stopping = False
        calls = {"n": 0}

        def fake_tick():
            effect = tick_effects[calls["n"]]
            calls["n"] += 1
            if calls["n"] >= len(tick_effects):
                sup.stopping = True
            if effect is not None:
                raise effect

        sup.tick = fake_tick
        sleeps = mock.Mock()
        sup._interruptible_sleep = sleeps
        with mock.patch.object(supervisor.config, "PID_PATH",
                               Path(self.tmp.name) / "supervisor.pid"), \
                mock.patch.object(supervisor.config, "ensure_runtime_dirs",
                                  lambda: None), \
                mock.patch.object(supervisor.signal, "signal",
                                  lambda *_a, **_k: None):
            rc = sup.run()
        return rc, sleeps

    def test_tick_exception_emits_fixed_finite_supervisor_error(self):
        rc, _ = self._run([RuntimeError(f"boom {CANARY} at /home/user/x")])
        self.assertEqual(rc, 0)
        errors = self._events("SUPERVISOR_ERROR")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["outcome"], "ERROR")
        self.assertEqual(errors[0]["activity_class"], "FAILED_WORK")
        self.assertEqual(errors[0]["metadata_redacted"],
                         {"phase": "SUPERVISOR_TICK",
                          "error_code": "TICK_FAILED"})

    def test_canary_class_and_traceback_never_reach_the_ledger(self):
        self._run([RuntimeError(f"boom {CANARY} at /home/user/x")])
        text = Path(self.ledger.path).read_text(encoding="utf-8")
        self.assertNotIn(CANARY, text)
        self.assertNotIn("RuntimeError", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("/home/user/x", text)

    def test_loop_survives_failed_ticks_and_continues(self):
        """Two failing ticks then a clean one: the loop must reach all
        three, sleep after each, and stop cleanly - continuation behaviour
        is unchanged by the metadata fix."""
        rc, sleeps = self._run([RuntimeError(CANARY), ValueError(CANARY), None])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._events("SUPERVISOR_ERROR")), 2)
        self.assertEqual(len(self._events("SUPERVISOR_STOPPED")), 1)
        self.assertEqual(sleeps.call_count, 3)


class PreflightGateExceptionCase(unittest.TestCase):
    """The real Preflight.run gate loop with one deliberately raising gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ledger_mod.Ledger(path=Path(self.tmp.name) / "ledger.jsonl",
                                        tz=TZ, experiment_id="run-002")
        self.preflight_path = Path(self.tmp.name) / "preflight.json"

    def _events(self, event_type):
        return [json.loads(line) for line in
                Path(self.ledger.path).read_text(encoding="utf-8").splitlines()
                if line and json.loads(line).get("event_type") == event_type]

    def _run_single_raising_gate(self):
        pf = preflight.Preflight.__new__(preflight.Preflight)
        pf.cfg = SimpleNamespace(timezone=TZ, experiment_id="run-002")
        pf.tz = TZ
        pf.ledger = self.ledger
        pf.notifier = mock.Mock()
        pf.telemetry = mock.Mock()
        pf.skip = set()
        pf.results = []

        def raising_gate():
            raise RuntimeError(f"boom {CANARY} at /home/user/x")

        pf.gate_c16_probe = raising_gate
        with mock.patch.object(preflight.Preflight, "GATES",
                               (("c16_probe", "gate_c16_probe"),)), \
                mock.patch.object(preflight.config, "ensure_runtime_dirs",
                                  lambda: None), \
                mock.patch.object(preflight.config, "PREFLIGHT_PATH",
                                  self.preflight_path):
            return pf.run()

    def test_raising_gate_still_records_a_failed_preflight_gate(self):
        summary = self._run_single_raising_gate()
        self.assertFalse(summary["all_passed"])  # fail-closed preserved
        gates = self._events("PREFLIGHT_GATE")
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["outcome"], "FAIL")
        self.assertEqual(gates[0]["guardrail"], "GATE_FAILED")
        meta = gates[0]["metadata_redacted"]
        self.assertEqual(meta["gate"], "c16_probe")  # identity preserved
        self.assertTrue(meta["required"])
        self.assertFalse(meta["ok"])

    def test_gate_exception_evidence_is_the_finite_code_only(self):
        self._run_single_raising_gate()
        meta = self._events("PREFLIGHT_GATE")[0]["metadata_redacted"]
        self.assertEqual(meta["evidence"],
                         {"error_code": "GATE_EVALUATION_FAILED"})
        self.assertEqual(meta["detail"],
                         "gate raised: GATE_EVALUATION_FAILED "
                         "(exception content withheld from durable evidence)")

    def test_canary_absent_from_ledger_and_preflight_file(self):
        self._run_single_raising_gate()
        ledger_text = Path(self.ledger.path).read_text(encoding="utf-8")
        preflight_text = self.preflight_path.read_text(encoding="utf-8")
        for text in (ledger_text, preflight_text):
            self.assertNotIn(CANARY, text)
            self.assertNotIn("RuntimeError", text)
            self.assertNotIn("/home/user/x", text)


class HostcheckObservationErrorCase(unittest.TestCase):
    """headroom_check per-signal failure representation, deterministic."""

    READERS = ("read_load1", "read_mem_available_bytes",
               "read_disk_free_bytes", "read_fd_capacity",
               "read_inotify_ceilings", "current_real_uid_inotify_usage",
               "read_candidate_port_range")

    def _all_readers_raising(self):
        def _raise(*_a, **_k):
            raise hostcheck.HostCheckError(f"could not read /etc/x: {CANARY}")
        patches = [mock.patch.object(hostcheck, name, _raise)
                   for name in self.READERS]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_observation_failures_are_finite_and_fail_closed(self):
        self._all_readers_raising()
        result = hostcheck.headroom_check(root=Path("/nonexistent-c16"))
        self.assertFalse(result.ok)  # unreadable never passes
        for signal_name in ("cpu", "ram", "disk", "fd", "inotify", "ports"):
            self.assertIn(f"{signal_name}: OBSERVATION_FAILED", result.detail)
            self.assertEqual(result.fields[signal_name],
                             {"error_code": "OBSERVATION_FAILED"})
        blob = result.detail + json.dumps(result.fields)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("could not read", blob)

    def test_threshold_failure_still_carries_real_values(self):
        """Observation failure and threshold failure must stay
        distinguishable: a real observation that merely breaches its
        threshold keeps its measured values and ok=False - it is never
        collapsed into OBSERVATION_FAILED, and never passes."""
        with mock.patch.object(hostcheck, "read_load1", lambda *_: 999.0), \
                mock.patch.object(hostcheck, "logical_cpu_count", lambda: 1):
            self._patch_healthy_rest()
            result = hostcheck.headroom_check(root=Path("/"))
        self.assertFalse(result.ok)
        self.assertEqual(result.fields["cpu"]["load1"], 999.0)
        self.assertFalse(result.fields["cpu"]["ok"])
        self.assertNotIn("error_code", result.fields["cpu"])
        self.assertIn("cpu: load1 999.0 exceeds", result.detail)

    def _patch_healthy_rest(self):
        healthy = {
            "read_mem_available_bytes": lambda *_: hostcheck.RAM_MIN_BYTES * 2,
            "read_disk_free_bytes": lambda *_: hostcheck.DISK_MIN_BYTES * 2,
            "read_fd_capacity": lambda *_: (100, 0, 1_000_000),
            "read_inotify_ceilings": lambda *_: (100_000, 128),
            "current_real_uid_inotify_usage": lambda *_: (1, 10),
            "read_candidate_port_range": lambda *_: (4200, 4260),
            "count_bindable_ports": lambda *_a, **_k: 61,
        }
        for name, fn in healthy.items():
            p = mock.patch.object(hostcheck, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_no_exception_prose_survives_into_gate_as_dict(self):
        """The exact object PREFLIGHT_GATE persists for the host gate."""
        self._all_readers_raising()
        result = hostcheck.headroom_check(root=Path("/nonexistent-c16"))
        gate = preflight.Gate("host_headroom", True, result.ok,
                              result.detail, result.fields)
        blob = json.dumps(gate.as_dict())
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("could not read", blob)
        self.assertFalse(gate.ok)


if __name__ == "__main__":
    unittest.main()
