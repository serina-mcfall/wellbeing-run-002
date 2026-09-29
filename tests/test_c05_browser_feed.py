"""C-05.2: reading the browser control-plane feed from persisted evidence.

C-05.1 made the accessibility apparatus persist an attempt: a directory
per (task, SHA, ordinal), a sidecar naming the browser it launched, and
two presence-only lifecycle markers. Nothing read any of it.

This suite pins the READ side, and specifically the shape of what that
read is allowed to claim. Three failures are in scope, and they are one
failure wearing three coats: evidence that could not be gathered reading
as evidence that nothing is there.

  * the WALK. A directory that could not be listed, an entry that could
    not be stat'ed, and a marker whose directory denied a stat must each
    clear walk_ok rather than vanish. An absent evidence root is not a
    proven zero - the scanner cannot see WHY it is absent.
  * the SIDECAR. Absent, unreachable and garbled are three different
    findings and must not collapse into one, and none of them is CLOSED.
    Crucially, a sidecar that parsed cleanly and honestly recorded a null
    identity is ORDINARY output - run.js writes `pid: null` whenever the
    browser child is ambiguous - and must stay distinguishable from
    corruption.
  * the SYMLINK. Nothing is followed. A link cannot redirect the walk out
    of the evidence tree, cannot make one attempt count twice, cannot
    stand in for a lifecycle marker, and cannot substitute a sidecar.
    Where a link sits in a position the walk would otherwise have
    entered, that is an observation problem, not an absence.

walk_ok IS NOT browser-observation completeness. It covers traversal and
marker stats only. Whether the browser set is observed well enough to
CLEAR a prior finding additionally needs every sidecar status OK and
every OPEN identity verifiable - that derivation belongs to
reconcile/metrics and is deliberately not asserted here.

No /proc is read and no orphan is classified, in the module or in this
suite. Both are judgements about the running host, and both belong
elsewhere.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_evidence  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40

NOT_ROOT = unittest.skipIf(os.getuid() == 0, "root bypasses permission checks")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sidecar_json(**overrides) -> str:
    """One realistic run.js sidecar, with fields overridden by keyword."""
    record = {"pid": 4242, "start_ticks": 99, "state": "OPEN",
              "opened_at": "2026-09-29T00:00:00Z", "closed_at": None}
    record.update(overrides)
    return json.dumps(record)


class ReadSidecarTests(unittest.TestCase):
    """Status and contents are two answers, and neither stands in for the
    other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "browser.json"

    def test_open_sidecar_reads_ok_and_carries_its_identity(self):
        write(self.path, sidecar_json())
        # Exact equality also pins the four-key shape: a caller must never
        # have to test for a key's presence before trusting the answer.
        self.assertEqual(gate_evidence.read_sidecar(self.path), {
            "sidecar_status": gate_evidence.SIDECAR_OK,
            "state": gate_evidence.OPEN,
            "pid": 4242,
            "start_ticks": 99,
        })

    def test_closed_sidecar_reads_ok_and_closed(self):
        write(self.path, sidecar_json(state="CLOSED",
                                      closed_at="2026-09-29T00:01:00Z"))
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"], gate_evidence.SIDECAR_OK)
        self.assertEqual(record["state"], gate_evidence.CLOSED)

    def test_absent_sidecar_is_missing_with_no_state(self):
        # Not CLOSED, and not "no browser was launched": run.js writes the
        # sidecar only AFTER chromium.launch() returns, so a browser can be
        # running while this file does not exist.
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"], gate_evidence.SIDECAR_MISSING)
        self.assertIsNone(record["state"])

    @NOT_ROOT
    def test_unreadable_sidecar_is_distinct_from_an_absent_one(self):
        write(self.path, sidecar_json())
        self.path.chmod(0o000)
        self.addCleanup(self.path.chmod, 0o644)
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"],
                         gate_evidence.SIDECAR_UNREADABLE)
        self.assertIsNone(record["state"])

    def test_truncated_json_is_invalid(self):
        write(self.path, '{"state": "OPEN", "pid": 42')
        self.assertEqual(
            gate_evidence.read_sidecar(self.path)["sidecar_status"],
            gate_evidence.SIDECAR_INVALID)

    def test_non_object_payload_is_invalid(self):
        write(self.path, '["OPEN"]')
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"], gate_evidence.SIDECAR_INVALID)
        self.assertIsNone(record["state"])

    def test_unrecognised_state_is_invalid_and_never_becomes_closed(self):
        write(self.path, sidecar_json(state="EXITED"))
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"], gate_evidence.SIDECAR_INVALID)
        self.assertIsNone(record["state"])

    def test_open_with_a_null_identity_is_ok_evidence_not_corruption(self):
        # run.js browserPid() returns null whenever the browser child is
        # ambiguous, and startTicks() returns null when /proc is unreadable.
        # This is its ORDINARY output, and it has to stay distinguishable
        # from a garbled file - one is an honest unknown identity, the
        # other is evidence we could not read at all.
        write(self.path, sidecar_json(pid=None, start_ticks=None))
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"], gate_evidence.SIDECAR_OK)
        self.assertEqual(record["state"], gate_evidence.OPEN)
        self.assertIsNone(record["pid"])
        self.assertIsNone(record["start_ticks"])

    def test_pid_that_is_not_a_real_positive_int_reads_as_unknown(self):
        for value in ("1234", 12.5, True, False, 0, -1, [4242]):
            with self.subTest(pid=value):
                write(self.path, sidecar_json(pid=value))
                record = gate_evidence.read_sidecar(self.path)
                self.assertEqual(record["sidecar_status"],
                                 gate_evidence.SIDECAR_OK)
                self.assertEqual(record["state"], gate_evidence.OPEN)
                self.assertIsNone(record["pid"])

    def test_start_ticks_that_is_not_a_real_positive_int_reads_as_unknown(self):
        for value in ("42", 4.0, True, False, 0, -5, {}):
            with self.subTest(start_ticks=value):
                write(self.path, sidecar_json(start_ticks=value))
                record = gate_evidence.read_sidecar(self.path)
                self.assertEqual(record["sidecar_status"],
                                 gate_evidence.SIDECAR_OK)
                self.assertIsNone(record["start_ticks"])

    def test_status_is_ok_exactly_when_the_state_is_recognised(self):
        payloads = [
            sidecar_json(),
            sidecar_json(state="CLOSED"),
            sidecar_json(state="EXITED"),
            sidecar_json(pid=None, start_ticks=None),
            '{"state": "OPEN"',
            '["OPEN"]',
            '"OPEN"',
            'null',
        ]
        for text in payloads:
            with self.subTest(payload=text[:40]):
                write(self.path, text)
                record = gate_evidence.read_sidecar(self.path)
                self.assertEqual(
                    record["sidecar_status"] == gate_evidence.SIDECAR_OK,
                    record["state"] in (gate_evidence.OPEN, gate_evidence.CLOSED))
        self.path.unlink()
        absent = gate_evidence.read_sidecar(self.path)
        self.assertNotEqual(absent["sidecar_status"], gate_evidence.SIDECAR_OK)
        self.assertIsNone(absent["state"])

    def test_symlinked_sidecar_is_not_read_through(self):
        outside = write(self.root / "outside.json", sidecar_json())
        self.path.symlink_to(outside)
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"],
                         gate_evidence.SIDECAR_UNREADABLE)
        self.assertIsNone(record["state"])

    def test_sidecar_path_that_is_a_directory_is_unreadable(self):
        self.path.mkdir()
        record = gate_evidence.read_sidecar(self.path)
        self.assertEqual(record["sidecar_status"],
                         gate_evidence.SIDECAR_UNREADABLE)
        self.assertIsNone(record["state"])


class ScanSidecarsTests(unittest.TestCase):
    """Completeness is earned by observing, never assumed from silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def attempt(self, task: str, sha: str, ordinal: int, *,
                sidecar: str | None = None, marker: str | None = None) -> Path:
        path = self.root / task / sha / f"attempt-{ordinal:04d}"
        (path / gate_evidence.ARTIFACT_DIR).mkdir(parents=True)
        if sidecar is not None:
            write(path / gate_evidence.ARTIFACT_DIR / gate_evidence.SIDECAR_NAME,
                  sidecar)
        if marker is not None:
            (path / marker).write_text("", encoding="utf-8")
        return path

    def test_every_attempt_is_discovered_in_deterministic_order(self):
        for task in ("TASK002", "TASK001"):
            for sha in (SHA_B, SHA_A):
                for ordinal in (2, 1):
                    self.attempt(task, sha, ordinal, sidecar=sidecar_json())
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertTrue(walk_ok)
        expected = [str(self.root / task / sha / f"attempt-{n:04d}")
                    for task in ("TASK001", "TASK002")
                    for sha in (SHA_A, SHA_B)
                    for n in (1, 2)]
        self.assertEqual([r["attempt_dir"] for r in records], expected)

    def test_absent_root_is_not_a_proven_zero(self):
        # The scanner cannot see whether the root was never allocated or
        # was deleted underneath it, and only one of those means no
        # browser ever ran. Claiming the safe one would be a guess.
        records, walk_ok = gate_evidence.scan_sidecars(self.root / "nope")
        self.assertEqual(records, [])
        self.assertFalse(walk_ok)

    @NOT_ROOT
    def test_unreadable_root_fails_closed(self):
        self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        self.root.chmod(0o000)
        self.addCleanup(self.root.chmod, 0o755)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertEqual(records, [])
        self.assertFalse(walk_ok)

    @NOT_ROOT
    def test_unreadable_task_subtree_clears_walk_ok_but_keeps_siblings(self):
        kept = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        self.attempt("TASK002", SHA_A, 1, sidecar=sidecar_json())
        blocked = self.root / "TASK002"
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertFalse(walk_ok)
        self.assertEqual([r["attempt_dir"] for r in records], [str(kept)])

    @NOT_ROOT
    def test_unreadable_sha_subtree_clears_walk_ok_but_keeps_siblings(self):
        kept = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        self.attempt("TASK001", SHA_B, 1, sidecar=sidecar_json())
        blocked = self.root / "TASK001" / SHA_B
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertFalse(walk_ok)
        self.assertEqual([r["attempt_dir"] for r in records], [str(kept)])

    def test_attempt_without_a_sidecar_is_still_a_record(self):
        path = self.attempt("TASK001", SHA_A, 1)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        # The WALK was clean, so walk_ok stands. The unknown does not
        # disappear - it travels as the record's status, and it is that
        # status which must later deny browser-observation completeness.
        self.assertTrue(walk_ok)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["attempt_dir"], str(path))
        self.assertEqual(records[0]["sidecar_status"],
                         gate_evidence.SIDECAR_MISSING)
        self.assertIsNone(records[0]["state"])

    def test_lifecycle_is_read_from_marker_presence(self):
        self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json(),
                     marker=gate_evidence.RUNNING_MARKER)
        self.attempt("TASK001", SHA_A, 2, sidecar=sidecar_json(),
                     marker=gate_evidence.TERMINAL_MARKER)
        self.attempt("TASK001", SHA_A, 3, sidecar=sidecar_json())
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertTrue(walk_ok)
        self.assertEqual([r["lifecycle"] for r in records],
                         [gate_evidence.RUNNING, gate_evidence.TERMINAL, None])

    @NOT_ROOT
    def test_unreadable_attempt_directory_clears_walk_ok(self):
        # Path.is_file() answers False for a marker it could not stat, so
        # before the strict form this read as "no marker" - a filesystem
        # failure disguised as a fact about terminality.
        path = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json(),
                            marker=gate_evidence.TERMINAL_MARKER)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o755)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertFalse(walk_ok)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["lifecycle"])

    def test_non_attempt_entries_beside_an_attempt_are_ignored(self):
        kept = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        (self.root / "TASK001" / SHA_A / "scratch").mkdir()
        write(self.root / "TASK001" / SHA_A / "notes.txt", "not an attempt")
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertTrue(walk_ok)
        self.assertEqual([r["attempt_dir"] for r in records], [str(kept)])

    def test_unreadable_sidecar_does_not_itself_clear_walk_ok(self):
        # walk_ok is TRAVERSAL completeness, not browser-observation
        # completeness. The unreadable sidecar travels as a per-record
        # status; it is reconcile/metrics that must refuse to call the
        # browser set observed while any record carries one. Asserting
        # walk_ok True here is not a claim that the browsers are known.
        path = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        sidecar = path / gate_evidence.ARTIFACT_DIR / gate_evidence.SIDECAR_NAME
        sidecar.unlink()
        sidecar.mkdir()  # an object we cannot read, with no permission games
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertTrue(walk_ok)
        self.assertEqual(records[0]["sidecar_status"],
                         gate_evidence.SIDECAR_UNREADABLE)
        self.assertIsNone(records[0]["state"])

    def test_symlinked_attempt_directory_is_not_traversed(self):
        real = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        link = self.root / "TASK001" / SHA_A / "attempt-0002"
        link.symlink_to(real)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        # Following it would count one browser twice; dropping it silently
        # would hide that something is standing in an attempt's place.
        self.assertFalse(walk_ok)
        self.assertEqual([r["attempt_dir"] for r in records], [str(real)])

    def test_symlinked_task_directory_is_not_traversed(self):
        self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        (self.root / "TASK002").symlink_to(self.root / "TASK001")
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertFalse(walk_ok)
        self.assertEqual(len(records), 1)

    def test_symlinked_marker_does_not_establish_a_lifecycle_state(self):
        path = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        decoy = write(self.root / "decoy", "")
        (path / gate_evidence.TERMINAL_MARKER).symlink_to(decoy)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        # A marker is a file _create_marker made with O_CREAT|O_EXCL, which
        # refuses a symlinked final component. Anything else at that path
        # is an object we cannot account for - unknown, never TERMINAL.
        self.assertIsNone(records[0]["lifecycle"])
        self.assertFalse(walk_ok)
        self.assertIsNone(gate_evidence.lifecycle(path))

    def test_directory_at_a_marker_path_does_not_establish_running(self):
        path = self.attempt("TASK001", SHA_A, 1, sidecar=sidecar_json())
        (path / gate_evidence.RUNNING_MARKER).mkdir()
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertIsNone(records[0]["lifecycle"])
        self.assertFalse(walk_ok)

    @NOT_ROOT
    def test_public_lifecycle_still_answers_none_on_an_unreadable_dir(self):
        # The strict form is private; lifecycle()'s contract is unchanged,
        # and it must still answer rather than raise.
        path = self.attempt("TASK001", SHA_A, 1,
                            marker=gate_evidence.TERMINAL_MARKER)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o755)
        self.assertIsNone(gate_evidence.lifecycle(path))


if __name__ == "__main__":
    unittest.main()
