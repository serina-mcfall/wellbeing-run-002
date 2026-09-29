"""C-05.2: attempt lifecycle ordering, and the durable outcome it rests on.

C-05.1 gave run_accessibility a write-once attempt directory and an
adjudicated verdict. The verdict lived in a returned dict and nothing
else, so for six of the seven outcomes nothing on disk recorded that the
attempt had ended, or how: result.json is written by the apparatus and is
absent whenever the apparatus timed out, exited non-zero, or produced
nothing.

That is why a TERMINAL marker could not honestly be written before now,
and it is what these tests are about. Three artifacts, three jobs:

  result.json           what the apparatus found, when it ran at all
  attempt-outcome.json  what the CONTROL PLANE concluded, always
  RUNNING / TERMINAL    lifecycle facts, presence-only, accumulating

The ordering is the contract:

  allocate -> RUNNING durable -> runner -> adjudicate
           -> outcome durable -> TERMINAL durable -> return

Each arrow is a claim that must not get ahead of its evidence. A runner
launched before RUNNING is a browser nothing accounts for. A TERMINAL
written before the outcome is a finished attempt with no verdict. There
is deliberately no finally block in run_accessibility: TERMINAL is
reachable only along the single path where both preconditions hold, and
a blind finally would write it on every failure path too.

Evidence-control failures are NOT accessibility results. A marker that
would not write says nothing about whether a page is accessible, so they
raise AttemptEvidenceError carrying one finite code - never a synthetic
status=FAILED record, which would put a fabricated verdict about the
product into a document that only ever described a file not being
written.

The exception chain is part of that contract. LifecycleMarkerError's
message interpolates the marker path, and `raise ... from None` sets
__cause__ while leaving __context__ pointing at it - still reachable by
anything walking the chain. The finite error is therefore raised after
leaving the except block, and these tests assert all four properties.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_evidence, workers  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "b" * 40
URL = "http://localhost:3200/"

# Shaped like a real credential so redact.scrub actually acts on it; an
# arbitrary path would prove nothing about scrubbing.
CANARY = "sk-proj-" + "A" * 30


class AttemptLifecycleCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.observed = []

    # ------------------------------------------------------------ helpers

    def runner(self, *, result_checks=None, exit_code=0):
        """A recording seam. It snapshots the attempt directory AT LAUNCH,
        which is the only moment at which "RUNNING already exists and
        TERMINAL does not" can actually be observed."""
        def build(ctx):
            self.observed.append({
                "running": ctx.running_marker_path.is_file(),
                "terminal": ctx.terminal_marker_path.is_file(),
                "outcome": (ctx.attempt_dir /
                            gate_evidence.OUTCOME_NAME).is_file(),
                "ctx": ctx,
            })
            if result_checks is None:
                return [sys.executable, "-c", f"raise SystemExit({exit_code})"]
            payload = json.dumps({"checks": result_checks})
            return [sys.executable, "-c",
                    "import pathlib,sys;"
                    "pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                    str(ctx.result_path), payload]
        return build

    def check(self, sha=SHA, result="PASS"):
        return {"check_id": "AXE_SCAN", "sha": sha, "result": result}

    def run_attempt(self, **kwargs):
        return gate_evidence.run_accessibility(
            "TASK001", SHA, URL, soft_timeout_seconds=5,
            hard_timeout_seconds=10, root=self.root, **kwargs)

    def attempt_dir(self):
        """The one attempt allocated by the most recent run."""
        return self.observed[-1]["ctx"].attempt_dir

    def outcome(self, attempt_dir=None):
        path = (attempt_dir or self.attempt_dir()) / gate_evidence.OUTCOME_NAME
        return json.loads(path.read_text(encoding="utf-8"))

    def sole_attempt(self):
        """Find the attempt directory without relying on the runner having
        been invoked - needed where the runner must never be called."""
        found = sorted(self.root.glob("*/*/attempt-*"))
        self.assertEqual(len(found), 1, "exactly one attempt is allocated")
        return found[0]

    # ------------------------------------------------------------ ordering

    def test_running_exists_and_terminal_does_not_when_the_runner_launches(self):
        self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        self.assertTrue(self.observed[0]["running"])
        self.assertFalse(self.observed[0]["terminal"])

    def test_the_outcome_does_not_exist_yet_when_the_runner_launches(self):
        self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        self.assertFalse(self.observed[0]["outcome"])

    def test_a_completed_attempt_publishes_its_outcome_then_terminal(self):
        record = self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        self.assertEqual(record["status"], gate_evidence.COMPLETED)
        attempt = self.attempt_dir()
        self.assertEqual(self.outcome(attempt), record)
        self.assertEqual(gate_evidence.lifecycle(attempt),
                         gate_evidence.TERMINAL)

    def test_a_governed_failure_is_published_just_as_durably(self):
        # A real adjudication path, not a simulated one: evidence bound to
        # a different commit is not evidence about this one.
        record = self.run_attempt(
            runner=self.runner(result_checks=[self.check(sha=OTHER_SHA)]))
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["reason"], "SHA_MISMATCH")
        self.assertEqual(self.outcome()["reason"], "SHA_MISMATCH")
        self.assertEqual(gate_evidence.lifecycle(self.attempt_dir()),
                         gate_evidence.TERMINAL)

    def test_both_markers_remain_after_a_completed_attempt(self):
        # They accumulate; TERMINAL never replaces or removes RUNNING.
        self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        attempt = self.attempt_dir()
        self.assertTrue((attempt / gate_evidence.RUNNING_MARKER).is_file())
        self.assertTrue((attempt / gate_evidence.TERMINAL_MARKER).is_file())

    def test_no_temp_file_survives_a_successful_publication(self):
        self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        leftovers = list(self.attempt_dir().glob("*.tmp"))
        self.assertEqual(leftovers, [])

    # ------------------------------------------------- RUNNING fails closed

    def test_a_refused_running_marker_stops_the_attempt_before_launch(self):
        seam = self.runner(result_checks=[self.check()])
        with mock.patch.object(gate_evidence, "mark_running",
                               return_value=False):
            with self.assertRaises(gate_evidence.AttemptEvidenceError) as caught:
                self.run_attempt(runner=seam)
        self.assertEqual(caught.exception.error_code,
                         gate_evidence.RUNNING_MARKER_FAILED)
        self.assertEqual(self.observed, [], "the runner seam was never called")
        attempt = self.sole_attempt()
        self.assertFalse((attempt / gate_evidence.OUTCOME_NAME).exists())
        self.assertIsNone(gate_evidence.lifecycle(attempt))

    def test_a_raising_running_marker_stops_the_attempt_before_launch(self):
        seam = self.runner(result_checks=[self.check()])
        boom = gate_evidence.LifecycleMarkerError(
            f"/evidence/{CANARY}/attempt-running.marker could not be withdrawn")
        with mock.patch.object(gate_evidence, "mark_running", side_effect=boom):
            with self.assertRaises(gate_evidence.AttemptEvidenceError) as caught:
                self.run_attempt(runner=seam)
        self.assertEqual(caught.exception.error_code,
                         gate_evidence.RUNNING_MARKER_FAILED)
        self.assertEqual(self.observed, [], "the runner seam was never called")
        self.assertIsNone(gate_evidence.lifecycle(self.sole_attempt()))

    # -------------------------------------------------- outcome fails closed

    def test_a_failed_publication_never_reaches_the_terminal_marker(self):
        seam = self.runner(result_checks=[self.check()])
        with mock.patch.object(gate_evidence, "_publish_outcome",
                               return_value=False):
            with self.assertRaises(gate_evidence.AttemptEvidenceError) as caught:
                self.run_attempt(runner=seam)
        self.assertEqual(caught.exception.error_code,
                         gate_evidence.OUTCOME_PERSIST_FAILED)
        attempt = self.attempt_dir()
        self.assertEqual(gate_evidence.lifecycle(attempt),
                         gate_evidence.RUNNING)
        self.assertNotEqual(gate_evidence.lifecycle(attempt),
                            gate_evidence.TERMINAL)

    # ------------------------------------------------- TERMINAL fails closed

    def test_a_refused_terminal_marker_denies_a_clean_return(self):
        seam = self.runner(result_checks=[self.check()])
        with mock.patch.object(gate_evidence, "mark_terminal",
                               return_value=False):
            with self.assertRaises(gate_evidence.AttemptEvidenceError) as caught:
                self.run_attempt(runner=seam)
        self.assertEqual(caught.exception.error_code,
                         gate_evidence.TERMINAL_MARKER_FAILED)

    def test_a_raising_terminal_marker_denies_a_clean_return(self):
        seam = self.runner(result_checks=[self.check()])
        boom = gate_evidence.LifecycleMarkerError(
            f"/evidence/{CANARY}/attempt-terminal.marker could not be withdrawn")
        with mock.patch.object(gate_evidence, "mark_terminal", side_effect=boom):
            with self.assertRaises(gate_evidence.AttemptEvidenceError) as caught:
                self.run_attempt(runner=seam)
        self.assertEqual(caught.exception.error_code,
                         gate_evidence.TERMINAL_MARKER_FAILED)

    def test_a_failed_terminal_marker_never_rewrites_the_published_outcome(self):
        # The accessibility adjudication really did complete. Overwriting a
        # true COMPLETED with a synthetic FAILED would destroy real evidence
        # to paper over a marker failure.
        seam = self.runner(result_checks=[self.check()])
        with mock.patch.object(gate_evidence, "mark_terminal",
                               return_value=False):
            with self.assertRaises(gate_evidence.AttemptEvidenceError):
                self.run_attempt(runner=seam)
        stored = self.outcome()
        self.assertEqual(stored["status"], gate_evidence.COMPLETED)
        self.assertEqual(stored["reason"], "")

    # --------------------------------------------------- the exception chain

    def test_the_finite_error_reaches_no_underlying_exception(self):
        for marker, code in (("mark_running",
                              gate_evidence.RUNNING_MARKER_FAILED),
                             ("mark_terminal",
                              gate_evidence.TERMINAL_MARKER_FAILED)):
            with self.subTest(marker=marker):
                self.setUp()
                boom = gate_evidence.LifecycleMarkerError(
                    f"/evidence/{CANARY}/marker could not be withdrawn")
                with mock.patch.object(gate_evidence, marker, side_effect=boom):
                    with self.assertRaises(
                            gate_evidence.AttemptEvidenceError) as caught:
                        self.run_attempt(runner=self.runner(
                            result_checks=[self.check()]))
                exc = caught.exception
                self.assertEqual(str(exc), code)
                self.assertIsNone(exc.__cause__)
                # The one `raise ... from None` cannot give: raising inside
                # the except block would leave the LifecycleMarkerError here,
                # and its message carries the marker path.
                self.assertIsNone(exc.__context__)
                self.assertNotIn(CANARY, repr(exc))

    def test_an_unknown_error_code_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            gate_evidence.AttemptEvidenceError("SOMETHING_INVENTED")

    # ------------------------------------------------------- write-once

    def test_an_existing_outcome_is_never_silently_overwritten(self):
        attempt = self.root / "TASK001" / SHA / "attempt-0001"
        (attempt / gate_evidence.ARTIFACT_DIR).mkdir(parents=True)
        claimed = attempt / gate_evidence.OUTCOME_NAME
        claimed.write_text('{"status": "FIRST"}', encoding="utf-8")
        self.assertFalse(
            gate_evidence._publish_outcome(attempt, {"status": "SECOND"}))
        self.assertEqual(json.loads(claimed.read_text(encoding="utf-8")),
                         {"status": "FIRST"})
        self.assertEqual(list(attempt.glob("*.tmp")), [])

    def test_an_unsynced_directory_entry_is_not_a_durable_outcome(self):
        # The datum is a directory ENTRY: a synced inode behind an unsynced
        # entry is still an absent file after a power loss, so publication
        # that cannot sync the entry does not count - and run_accessibility
        # turns that False into OUTCOME_PERSIST_FAILED with no TERMINAL,
        # which test_a_failed_publication_never_reaches_the_terminal_marker
        # pins end to end.
        #
        # Asserted here at unit level because _fsync_path is shared with
        # _create_marker: patching it across a whole run_accessibility call
        # would fail RUNNING first and never reach publication at all.
        #
        # The companion rule rides along. A marker's whole content is its
        # presence, so a doubtful one is withdrawn; an outcome carries
        # adjudication that cannot be reconstructed, so a doubtful one
        # STAYS. Deleting an adjudication that may already be on disk is
        # the worse error.
        attempt = self.root / "TASK001" / SHA / "attempt-0001"
        (attempt / gate_evidence.ARTIFACT_DIR).mkdir(parents=True)
        with mock.patch.object(gate_evidence, "_fsync_path",
                               return_value=False):
            self.assertFalse(
                gate_evidence._publish_outcome(attempt, {"status": "KEEP"}))
        published = attempt / gate_evidence.OUTCOME_NAME
        self.assertTrue(published.is_file())
        self.assertEqual(json.loads(published.read_text(encoding="utf-8")),
                         {"status": "KEEP"})

    def test_an_unserialisable_record_is_a_publication_failure(self):
        attempt = self.root / "TASK001" / SHA / "attempt-0001"
        (attempt / gate_evidence.ARTIFACT_DIR).mkdir(parents=True)
        self.assertFalse(
            gate_evidence._publish_outcome(attempt, {"bad": {1, 2}}))
        self.assertFalse((attempt / gate_evidence.OUTCOME_NAME).exists())

    # ------------------------------------------------------ evidence hygiene

    def test_a_missing_executable_never_yields_raw_exception_prose(self):
        # run_bounded's FileNotFoundError branch returned raw str(exc) into
        # a field named stderr_tail_scrubbed. Harmless while the record
        # lived only in memory; durable evidence the moment C-05 persists it.
        result = workers.run_bounded(
            [f"/nonexistent/{CANARY}/node"], str(self.root), 1, 2)
        self.assertEqual(result["exit_code"], 127)
        self.assertNotIn(CANARY, result["stderr"])
        self.assertIn("redacted", result["stderr"])
        self.assertLessEqual(len(result["stderr"]), 2000)

    def test_no_canary_survives_into_the_published_outcome(self):
        def seam(ctx):
            self.observed.append({"ctx": ctx, "running": True,
                                  "terminal": False, "outcome": False})
            return [f"/nonexistent/{CANARY}/node"]
        record = self.run_attempt(runner=seam)
        self.assertEqual(record["status"], gate_evidence.FAILED)
        raw = (self.attempt_dir() /
               gate_evidence.OUTCOME_NAME).read_text(encoding="utf-8")
        self.assertNotIn(CANARY, raw)

    # ------------------------------------------------- C-05.1 not regressed

    def test_attempt_allocation_and_artifact_paths_are_unchanged(self):
        record = self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        attempt = self.attempt_dir()
        self.assertEqual(attempt.name, "attempt-0001")
        self.assertEqual(record["attempt_dir"], str(attempt))
        self.assertTrue(Path(record["stdout_path"]).is_file())
        self.assertTrue(Path(record["stderr_path"]).is_file())
        self.assertEqual(record["check_counts"], {"PASS": 1, "FAIL": 0})
        # A rerun at the same SHA gets its own directory; the first is
        # untouched, markers, outcome and all.
        self.run_attempt(runner=self.runner(result_checks=[self.check()]))
        self.assertEqual(self.attempt_dir().name, "attempt-0002")
        self.assertEqual(gate_evidence.lifecycle(attempt),
                         gate_evidence.TERMINAL)
        self.assertEqual(self.outcome(attempt)["attempt_id"], "attempt-0001")


if __name__ == "__main__":
    unittest.main()
