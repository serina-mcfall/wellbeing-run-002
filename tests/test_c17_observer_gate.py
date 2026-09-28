"""C-17: the Observer preflight gate must exercise the production path.

Protocol v2 §"Preflight" is "realistic, not synthetic", and
grok_bounded_observer is a REQUIRED gate. Before C-17 the gate wrote its
own trivial probe ("State one FACT ... then stop"), which completes in
one or two turns, so it could PASS while the real Observer - whose prompt
requires several tool-using turns - failed. These tests pin that the gate
renders the SAME prompt production uses, against a representative
evidence snapshot, while keeping its fail-closed semantics exactly.

Nothing here weakens the Observer: the prompt content, the timeout bounds
and max_turns are all asserted to be unchanged by this work.
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
    config,
    preflight,
    prompts,
    state,
    supervisor,
)

TZ = "Pacific/Auckland"
OBSERVER_SPEC = {"model": None, "max_turns": 12, "soft_timeout_seconds": 120,
                 "hard_timeout_seconds": 330, "web_enabled": False,
                 "retry_once_fresh": True}


class ObserverGateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.obs_dir = Path(self.tmp.name) / "observer"
        p = mock.patch.object(config, "OBSERVER_DIR", self.obs_dir)
        p.start()
        self.addCleanup(p.stop)

    def _preflight(self):
        pf = preflight.Preflight.__new__(preflight.Preflight)
        pf.cfg = SimpleNamespace(timezone=TZ, experiment_id="run-002",
                                 observer=dict(OBSERVER_SPEC),
                                 protocol_version="v2.0",
                                 duration_hours=24)
        pf.tz = TZ
        pf.ledger = mock.Mock(count=mock.Mock(return_value=7))
        pf.notifier = mock.Mock()
        pf.telemetry = mock.Mock()
        pf.skip = set()
        pf.results = []
        return pf

    def _run_gate(self, *, ok=True, timed_out=False, exit_code=0):
        """Run the gate with the grok subprocess stubbed. Returns
        (gate, captured run_bounded calls)."""
        result = {"ok": ok, "exit_code": exit_code, "stdout": "FACT: something",
                  "stderr": "" if ok else "boom", "duration_ms": 1234.0,
                  "soft_timeout_exceeded": False, "timed_out": timed_out}
        runner = mock.Mock(return_value=result)
        with mock.patch.object(preflight.workers, "run_bounded", runner), \
                mock.patch.object(preflight.state_mod, "Store") as store_cls:
            store = store_cls.return_value
            store.exists.return_value = False
            gate = self._preflight().gate_grok_observer()
        return gate, runner

    def _written_prompt(self):
        files = sorted(self.obs_dir.glob("*.md"))
        self.assertEqual(len(files), 1, f"expected one prompt file, got {files}")
        return files[0].read_text(encoding="utf-8")

    def _written_evidence(self):
        files = sorted(self.obs_dir.glob("*.json"))
        self.assertEqual(len(files), 1, f"expected one evidence file, got {files}")
        return json.loads(files[0].read_text(encoding="utf-8"))

    # ------------------------------------------------------------ fidelity

    def test_gate_writes_the_production_observer_prompt(self):
        """The core C-17 pin: the gate's prompt must BE the production
        prompt, not a substitute probe."""
        self._run_gate()
        written = self._written_prompt()
        evidence_path = sorted(self.obs_dir.glob("*.json"))[0]
        self.assertEqual(written, prompts.observer(str(evidence_path), "T-pre"))

    def test_the_trivial_probe_text_is_gone(self):
        """Regression guard naming the exact substitute that caused C-17."""
        self._run_gate()
        written = self._written_prompt()
        self.assertNotIn("State one FACT about this repository", written)
        # and it really is the full production template, not a fragment
        self.assertIn("Grok Observer", written)
        self.assertIn("INFERENCE", written)
        self.assertIn("UNKNOWN", written)

    def test_prompt_points_at_an_evidence_file_that_exists(self):
        self._run_gate()
        evidence_path = sorted(self.obs_dir.glob("*.json"))[0]
        self.assertTrue(evidence_path.is_file())
        self.assertIn(str(evidence_path), self._written_prompt())

    def test_evidence_shape_matches_production_observer_evidence(self):
        """Drift guard: the gate builds its own snapshot, so its key set
        must stay identical to Supervisor.observer_evidence's. If
        production gains a field and the gate does not, the gate stops
        being representative - and that must fail here, loudly."""
        self._run_gate()
        gate_keys = set(self._written_evidence())

        class _View:  # minimal stand-in for what observer_evidence touches
            ledger = mock.Mock(count=mock.Mock(return_value=0))

            def label(self, doc):
                return "T-pre"

        doc = state.initial_document("run-002", "v2.0")
        production_keys = set(
            supervisor.Supervisor.observer_evidence(_View(), doc))
        self.assertEqual(gate_keys, production_keys)

    def test_gate_invokes_grok_with_the_governed_bounds(self):
        _, runner = self._run_gate()
        command, cwd = runner.call_args[0][0], runner.call_args[0][1]
        self.assertEqual(command[0], "grok")
        self.assertIn("--disable-web-search", command)
        self.assertIn("--max-turns", command)
        self.assertEqual(command[command.index("--max-turns") + 1], "12")
        self.assertEqual(runner.call_args[0][2], 120)   # soft timeout
        self.assertEqual(runner.call_args[0][3], 330)   # hard timeout

    # -------------------------------------------------------- fail-closed

    def test_non_zero_exit_still_fails_the_gate(self):
        gate, _ = self._run_gate(ok=False, exit_code=1)
        self.assertFalse(gate.ok)
        self.assertTrue(gate.required)
        self.assertEqual(gate.name, "grok_bounded_observer")

    def test_timeout_still_fails_the_gate(self):
        gate, _ = self._run_gate(ok=False, timed_out=True)
        self.assertFalse(gate.ok)
        self.assertTrue(gate.evidence["timed_out"])

    def test_a_timed_out_run_never_passes_even_if_ok_is_true(self):
        """ok and timed_out are separate signals; a timeout is fatal
        regardless of the exit status reported alongside it."""
        gate, _ = self._run_gate(ok=True, timed_out=True)
        self.assertFalse(gate.ok)

    def test_healthy_run_passes(self):
        gate, _ = self._run_gate(ok=True)
        self.assertTrue(gate.ok)

    def test_failure_is_retried_once_per_the_existing_spec(self):
        _, runner = self._run_gate(ok=False, exit_code=1)
        self.assertEqual(runner.call_count, 2)

    # ------------------------------------------------- nothing weakened

    def test_governed_observer_bounds_are_pinned(self):
        """The governed C-17 values, each backed by a real valid-repo
        diagnostic rather than an estimate: the Observer completed in
        exactly 8 turns taking 218.7 s, so 12 turns is 50% turn headroom
        and 330 s is ~50% timing headroom. soft_timeout_seconds stays 120
        and is informational only - run_bounded records it but never
        fails on it. Changing any of these needs its own governed
        decision backed by a fresh real run, not an estimate."""
        real = config.load().observer
        self.assertEqual(real["max_turns"], 12)
        self.assertEqual(real["soft_timeout_seconds"], 120)
        self.assertEqual(real["hard_timeout_seconds"], 330)
        self.assertIs(real["web_enabled"], False)

    def test_governed_bounds_clear_the_measured_requirement(self):
        """The pinned numbers must actually exceed what the real run
        needed - a guard against a future edit that lowers them back
        under the measured floor while still looking 'governed'."""
        real = config.load().observer
        self.assertGreater(real["max_turns"], 8)            # measured turns
        self.assertGreater(real["hard_timeout_seconds"], 219)  # measured seconds

    def test_production_observer_prompt_content_is_unchanged(self):
        """The fix is to the gate, never to the Observer's instructions."""
        text = prompts.observer("/tmp/e.json", "T-pre")
        for required in ("Do not modify anything, anywhere.",
                         "Do not use the web.",
                         "delivery progress, review independence, rework"):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
