"""C-05.1: durable accessibility-evidence attempts and browser lifecycle.

Before this work the accessibility apparatus had no persisting caller:
`details` (the axe violations, the focus-order walk, the touch-target
offenders) existed only in a returned object or on stdout, screenshots
were keyed by SHA alone and overwrote themselves on rerun, and
`workers.run_bounded` kept only the last 8000 scrubbed characters - a
tail, which is not raw evidence.

These tests pin the persistence contract:

  * an attempt directory is created EXCLUSIVELY, so a rerun at the same
    SHA can never overwrite the evidence of an earlier attempt;
  * complete stdout/stderr reach disk untruncated, while the ledger-bound
    tail stays scrubbed and is never labelled raw;
  * a run that fails, times out, produces no result or produces a result
    bound to a different SHA records FAILED and never synthesises a PASS;
  * a browser sidecar that was never closed stays recognisably OPEN and
    still carries the (pid, start_ticks) identity C-09's
    proc.verified_alive() needs.

The real Chromium/axe proofs (--out-json round-trip, screenshots landing
in the supplied per-attempt directory, a normally-closed sidecar) live in
apparatus/accessibility/run.test.js, which already owns real-browser
work. They are deliberately NOT duplicated here: this suite runs in CI,
where apparatus/node_modules is absent, so a real browser launch would
fail for reasons that have nothing to do with the control plane.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import config, gate_evidence, workers  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40
URL = "file:///fixtures/clean.html"

# A stand-in for the Node apparatus. Deterministic, launches no browser,
# and lets each test drive one exact failure mode. It writes through the
# same paths gate_evidence derives, so the contract under test is the
# real one.
FAKE_RUNNER = textwrap.dedent(
    """
    import json, os, sys, time

    mode, result_path, sidecar_path, out_dir, sha = sys.argv[1:6]

    if mode in ("open_browser", "hang"):
        os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
        with open(sidecar_path, "w") as fh:
            json.dump({"pid": os.getpid(),
                       "start_ticks": 4242,
                       "state": "OPEN",
                       "opened_at": "2026-09-28T00:00:00+13:00",
                       "closed_at": None}, fh)

    if mode == "hang":
        time.sleep(600)

    if mode == "loud":
        sys.stdout.write("x" * 20000)
        sys.stderr.write("e" * 20000)

    if mode == "fail":
        sys.stderr.write("apparatus exploded")
        sys.exit(3)

    if mode == "no_result":
        sys.exit(0)

    checks = [{"check_id": "AXE_SCAN", "result": "PASS",
               "sha": SHA_OVERRIDE or sha, "artifact_reference": out_dir}]
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w") as fh:
        json.dump({"checks": checks, "details": {"AXE_SCAN": {"violations": []}}}, fh)
    """
)


def _fake(tmp: Path, sha_override: str | None = None) -> Path:
    body = FAKE_RUNNER.replace(
        "SHA_OVERRIDE", json.dumps(sha_override) if sha_override else "None")
    path = tmp / "fake_runner.py"
    path.write_text(body, encoding="utf-8")
    return path


def _runner(script: Path, mode: str):
    def build(ctx):
        return [sys.executable, str(script), mode, str(ctx.result_path),
                str(ctx.sidecar_path), str(ctx.out_dir), ctx.sha]
    return build


class TestAttemptAllocation(unittest.TestCase):
    """Write-once attempts. A rerun must never overwrite its predecessor."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_attempt_path_is_task_then_sha_then_attempt(self):
        attempt = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        self.assertEqual(attempt.parent.name, SHA_A)
        self.assertEqual(attempt.parent.parent.name, "TASK-001")
        self.assertTrue((attempt / "accessibility").is_dir())

    def test_two_attempts_at_the_same_sha_are_different_directories(self):
        first = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        second = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_a_rerun_cannot_overwrite_the_earlier_attempt(self):
        first = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        evidence = first / "accessibility" / "result.json"
        evidence.write_text('{"checks": ["original"]}', encoding="utf-8")
        gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        self.assertEqual(json.loads(evidence.read_text())["checks"], ["original"])

    def test_allocation_skips_a_directory_that_already_exists(self):
        """Exclusive creation, not a name guess: a pre-existing attempt
        directory must never be handed out again."""
        squatter = self.tmp / "TASK-001" / SHA_A / "attempt-0001"
        squatter.mkdir(parents=True)
        (squatter / "claim.txt").write_text("mine", encoding="utf-8")
        attempt = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        self.assertNotEqual(attempt, squatter)
        self.assertEqual((squatter / "claim.txt").read_text(), "mine")

    def test_different_shas_do_not_share_an_attempt_space(self):
        a = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=self.tmp)
        b = gate_evidence.allocate_attempt("TASK-001", SHA_B, root=self.tmp)
        self.assertNotEqual(a.parent, b.parent)

    def test_a_malformed_sha_is_refused_outright(self):
        for bad in ("", "abc", SHA_A.upper(), SHA_A + "a"):
            with self.assertRaises(ValueError):
                gate_evidence.allocate_attempt("TASK-001", bad, root=self.tmp)


class TestFullOutputPreservation(unittest.TestCase):
    """Complete output on disk; only the ledger-bound tail is truncated."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_run_bounded_sinks_preserve_output_longer_than_the_tail_limit(self):
        out = self.tmp / "stdout.txt"
        err = self.tmp / "stderr.txt"
        script = self.tmp / "loud.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('x' * 20000)\n"
            "sys.stderr.write('e' * 20000)\n",
            encoding="utf-8")
        result = workers.run_bounded(
            [sys.executable, str(script)], str(self.tmp), 30, 60,
            stdout_path=out, stderr_path=err)
        self.assertTrue(result["ok"])
        self.assertEqual(len(out.read_text()), 20000)
        self.assertEqual(len(err.read_text()), 20000)

    def test_run_bounded_without_sinks_is_unchanged(self):
        """The Observer's existing contract must not move."""
        script = self.tmp / "quiet.py"
        script.write_text("print('hello')\n", encoding="utf-8")
        result = workers.run_bounded(
            [sys.executable, str(script)], str(self.tmp), 30, 60)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "hello")
        self.assertIsNone(result["stdout_path"])
        self.assertIsNone(result["stderr_path"])

    def test_both_sinks_together_are_accepted(self):
        script = self.tmp / "both.py"
        script.write_text(
            "import sys\nsys.stdout.write('o')\nsys.stderr.write('e')\n",
            encoding="utf-8")
        result = workers.run_bounded(
            [sys.executable, str(script)], str(self.tmp), 30, 60,
            stdout_path=self.tmp / "o.txt", stderr_path=self.tmp / "e.txt")
        self.assertEqual((self.tmp / "o.txt").read_text(), "o")
        self.assertEqual((self.tmp / "e.txt").read_text(), "e")
        self.assertEqual(result["stdout_path"], str(self.tmp / "o.txt"))

    def test_stdout_sink_alone_is_refused(self):
        """Half a sink would put one complete stream beside one silently
        truncated one, with nothing saying which was which."""
        with self.assertRaises(ValueError):
            workers.run_bounded([sys.executable, "-c", "pass"], str(self.tmp),
                                30, 60, stdout_path=self.tmp / "o.txt")

    def test_stderr_sink_alone_is_refused(self):
        with self.assertRaises(ValueError):
            workers.run_bounded([sys.executable, "-c", "pass"], str(self.tmp),
                                30, 60, stderr_path=self.tmp / "e.txt")

    def test_a_refused_sink_pair_runs_nothing_at_all(self):
        """Deterministic and immediate: the guard fires before the child."""
        marker = self.tmp / "ran.txt"
        script = self.tmp / "marker.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n",
                          encoding="utf-8")
        with self.assertRaises(ValueError):
            workers.run_bounded([sys.executable, str(script)], str(self.tmp),
                                30, 60, stdout_path=self.tmp / "o.txt")
        self.assertFalse(marker.exists())

    def test_record_keeps_full_output_on_disk_and_names_the_tail_a_tail(self):
        record = gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp), "loud"),
            soft_timeout_seconds=30, hard_timeout_seconds=60)
        self.assertEqual(len(Path(record["stdout_path"]).read_text()), 20000)
        self.assertEqual(len(Path(record["stderr_path"]).read_text()), 20000)
        # The tail exists for the ledger, is bounded, and is never called raw.
        self.assertLessEqual(len(record["stdout_tail_scrubbed"]), 8000)
        self.assertNotIn("stdout", set(record) - {"stdout_path", "stdout_tail_scrubbed"})

    def test_full_details_round_trip_into_the_record(self):
        record = gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp), "ok"),
            soft_timeout_seconds=30, hard_timeout_seconds=60)
        self.assertEqual(record["status"], gate_evidence.COMPLETED)
        persisted = json.loads(Path(record["result_path"]).read_text())
        self.assertIn("details", persisted)
        self.assertEqual(persisted["details"]["AXE_SCAN"], {"violations": []})
        self.assertEqual(record["check_counts"], {"PASS": 1, "FAIL": 0})


class TestFailureNeverPasses(unittest.TestCase):
    """A run that did not happen is not a run that passed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, mode, sha_override=None, hard=60):
        return gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp, sha_override), mode),
            soft_timeout_seconds=1, hard_timeout_seconds=hard)

    def test_non_zero_exit_is_failed_with_no_checks(self):
        record = self._run("fail")
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["checks"], [])

    def test_a_failed_run_reports_null_counts_never_zero(self):
        """Zero failures would read as a clean scan. Unknown is not clean."""
        record = self._run("fail")
        self.assertIsNone(record["check_counts"])

    def test_missing_result_file_is_failed_not_passed(self):
        record = self._run("no_result")
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["reason"], "RESULT_MISSING")
        self.assertEqual(record["checks"], [])

    def test_hard_timeout_is_failed_and_recorded_as_timed_out(self):
        record = self._run("hang", hard=2)
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["checks"], [])

    def test_result_bound_to_a_different_sha_is_refused(self):
        record = self._run("ok", sha_override=SHA_B)
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["reason"], "SHA_MISMATCH")
        self.assertEqual(record["checks"], [])

    def test_the_record_is_bound_to_the_requested_task_and_sha(self):
        record = self._run("ok")
        self.assertEqual(record["task_id"], "TASK-001")
        self.assertEqual(record["sha"], SHA_A)

    def test_a_failed_run_still_leaves_its_output_on_disk(self):
        record = self._run("fail")
        self.assertIn("apparatus exploded", Path(record["stderr_path"]).read_text())


class TestBrowserSidecar(unittest.TestCase):
    """An interrupted run must still identify the browser it left behind."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_killed_run_leaves_the_sidecar_open_with_c09_identity(self):
        record = gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp), "hang"),
            soft_timeout_seconds=1, hard_timeout_seconds=2)
        self.assertTrue(record["timed_out"])
        sidecar = json.loads(Path(record["browser_path"]).read_text())
        self.assertEqual(sidecar["state"], "OPEN")
        self.assertIsInstance(sidecar["pid"], int)
        # (pid, start_ticks) is the identity proc.verified_alive() needs;
        # a PID alone cannot survive PID reuse.
        self.assertIsInstance(sidecar["start_ticks"], int)

    def test_the_record_points_at_the_sidecar_inside_its_own_attempt(self):
        record = gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp), "open_browser"),
            soft_timeout_seconds=30, hard_timeout_seconds=60)
        self.assertTrue(
            Path(record["browser_path"]).is_relative_to(Path(record["attempt_dir"])))


class TestProductionCommand(unittest.TestCase):
    """The injected runner is a test seam; production must still be real."""

    def test_default_command_invokes_the_real_apparatus_with_out_json(self):
        tmp = Path(tempfile.mkdtemp())
        attempt = gate_evidence.allocate_attempt("TASK-001", SHA_A, root=tmp)
        ctx = gate_evidence.context(attempt, SHA_A, URL)
        command = gate_evidence.node_command(ctx)
        self.assertEqual(command[0], "node")
        self.assertTrue(command[1].endswith("apparatus/accessibility/run.js"))
        self.assertEqual(command[2:5], [URL, SHA_A, str(ctx.out_dir)])
        self.assertEqual(command[5], "--out-json")
        self.assertEqual(command[6], str(ctx.result_path))

    def test_evidence_root_is_under_the_runtime_directory(self):
        self.assertEqual(config.EVIDENCE_DIR.parent, config.RUNTIME_DIR)


class TestTimeoutsMustBeStated(unittest.TestCase):
    """config/experiment.json governs no accessibility timeout, so this
    module must not invent one. A default would quietly become the number
    everything runs on, with no governance behind it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _call(self, **kwargs):
        return gate_evidence.run_accessibility(
            "TASK-001", SHA_A, URL, root=self.tmp,
            runner=_runner(_fake(self.tmp), "ok"), **kwargs)

    def test_omitting_both_timeouts_is_refused(self):
        with self.assertRaises(TypeError):
            self._call()

    def test_omitting_the_hard_timeout_is_refused(self):
        with self.assertRaises(TypeError):
            self._call(soft_timeout_seconds=30)

    def test_omitting_the_soft_timeout_is_refused(self):
        with self.assertRaises(TypeError):
            self._call(hard_timeout_seconds=60)

    def test_stating_both_is_accepted(self):
        record = self._call(soft_timeout_seconds=30, hard_timeout_seconds=60)
        self.assertEqual(record["status"], gate_evidence.COMPLETED)

    def test_the_module_publishes_no_timeout_policy(self):
        """Not just unused - absent. A named default is an invitation."""
        leaked = [name for name in dir(gate_evidence)
                  if "TIMEOUT" in name.upper()]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
