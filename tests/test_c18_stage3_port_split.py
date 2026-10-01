"""C-18 stage 3: port candidate selection splits from the bind probe.

C-18's row names `workers.allocate_port` as external work inside T1 because
it "performs a real 127.0.0.1 bind test". The handover's §14.0 item 4 states
the requirement precisely: allocate_port "is not a pure state reservation -
it must split into 'choose a candidate under the lock' and 'prove it binds
outside the lock'."

Stage 3 delivers exactly that split and nothing more:

  * `select_port_candidates` - the under-lock half. No bind, no network, and
    deliberately no write: choosing a candidate makes no durable claim, so a
    failed probe has no reservation to leak and no cleanup path to skip.
  * `probe_port` - the external half. One real bind, closed immediately; a
    point-in-time observation, never a reservation.
  * `allocate_port` - unchanged signature, contract and exhaustion message,
    now composed from the two.

WHAT STAGE 3 DID NOT DO, recorded here because this file is where the
boundary has been tracked: stage 3 delivered only the split, leaving
`dispatch_builder` and `dispatch_fixer` still calling the composed
`allocate_port` inside T1. Stage 4 migrated the builder and stage 6 the
fixer, so `NoDispatchPathStillBindsUnderTheLock` at the end of this file now
pins that no supervisor dispatch path binds under the state lock at all.

Nothing here launches a worker, and every bind is against 127.0.0.1 in the
governed test range.
"""

from __future__ import annotations

import copy
import fcntl
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import (  # noqa: E402
    config,
    state as state_mod,
    supervisor as supervisor_mod,
    workers,
)

TZ = "Pacific/Auckland"
# The same window tests/test_c09_resource_lifecycle.py uses.
LO, HI = 39110, 39119


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
    """A port nothing holds right now, for probe tests that need one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PortHarness(unittest.TestCase):
    """A real job-file directory, so the reservation logic is the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_dir = self.root / "workers"
        self.job_dir.mkdir()
        patch = mock.patch.object(config, "WORKER_LOG_DIR", self.job_dir)
        patch.start()
        self.addCleanup(patch.stop)

    # ------------------------------------------------------------ fixtures

    def doc(self, **worker_ports) -> dict:
        return {"workers": {name: {"port": port}
                            for name, port in worker_ports.items()}}

    def write_job(self, worker: str, port: int) -> None:
        (self.job_dir / f"{worker}.job.json").write_text(
            json.dumps({"worker": worker, "port": port}), encoding="utf-8")

    def write_status(self, worker: str, *, phase="RUNNING", agent_pid=4242,
                     ticks=100) -> None:
        (self.job_dir / f"{worker}.status.json").write_text(
            json.dumps({"phase": phase, "agent_pid": agent_pid,
                        "agent_start_ticks": ticks}), encoding="utf-8")

    def select(self, doc, *, entries=None, alive=False):
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value=entries if entries is not None else {}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=alive):
            return workers.select_port_candidates(doc, LO, HI)

    def allocate(self, doc, *, entries=None, alive=False):
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value=entries if entries is not None else {}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=alive):
            return workers.allocate_port(doc, LO, HI)


# ------------------------------------------------ the under-lock half is pure


class SelectionPerformsNoExternalWork(PortHarness):

    def test_selection_opens_no_socket_at_all(self):
        with mock.patch.object(workers, "socket") as sock_mod:
            self.select(self.doc())
        self.assertEqual(sock_mod.mock_calls, [],
                         "candidate selection must not touch the network")

    def test_selection_mutates_nothing(self):
        doc = self.doc(w1=LO)
        before = copy.deepcopy(doc)
        self.select(doc)
        self.assertEqual(doc, before)

    def test_selection_makes_no_durable_reservation(self):
        """Nothing to leak on a failed probe, because nothing was claimed."""
        before = sorted(p.name for p in self.job_dir.iterdir())
        self.select(self.doc())
        self.assertEqual(sorted(p.name for p in self.job_dir.iterdir()), before)

    def test_the_probe_half_really_does_bind(self):
        """Otherwise the test above could pass because nothing binds ever.

        Same spy as the purity test, so the two answer the same question and
        differ only in which half is under it."""
        with mock.patch.object(workers, "socket") as sock_mod:
            workers.probe_port(free_port())
        self.assertTrue(sock_mod.mock_calls, "probe_port must open a socket")
        self.assertIn("bind", str(sock_mod.mock_calls))


class SelectionUnderTheStateLock(PortHarness):
    """The transaction-boundary test. A bind placed back inside selection is
    a bind under the state lock, and this is what must notice."""

    def setUp(self):
        super().setUp()
        self.store = state_mod.Store(path=self.root / "state.json", tz=TZ)
        self.store._write({"workers": {}})
        self.binds_with_lock_held: list[bool] = []

        harness = self

        class SpySocket:
            def __init__(self, *args, **kwargs):
                self._real = socket.socket(*args, **kwargs)

            def bind(self, address):
                harness.binds_with_lock_held.append(
                    lock_is_held(harness.store.lock_path))
                return self._real.bind(address)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._real.close()
                return False

            def __getattr__(self, name):
                return getattr(self._real, name)

        self.spy_module = mock.Mock(wraps=socket)
        self.spy_module.socket = SpySocket
        self.spy_module.AF_INET = socket.AF_INET
        self.spy_module.SOCK_STREAM = socket.SOCK_STREAM

    def test_the_lock_probe_can_detect_a_held_lock(self):
        self.assertFalse(lock_is_held(self.store.lock_path))
        with self.store.transaction():
            self.assertTrue(lock_is_held(self.store.lock_path))
        self.assertFalse(lock_is_held(self.store.lock_path))

    def test_selection_inside_a_transaction_binds_nothing(self):
        with mock.patch.object(workers, "socket", self.spy_module):
            with self.store.transaction() as doc:
                doc.setdefault("workers", {})
                with mock.patch.object(workers.proc, "worker_entry_processes",
                                       return_value={}), \
                        mock.patch.object(workers.proc, "verified_alive",
                                          return_value=False):
                    candidates, _ = workers.select_port_candidates(doc, LO, HI)
        self.assertTrue(candidates, "the test proves nothing with no candidates")
        self.assertEqual(self.binds_with_lock_held, [],
                         "no bind may happen while the state lock is held")

    def test_the_spy_would_notice_a_bind_under_the_lock(self):
        """Proves the assertion above can fail - without this, a broken spy
        would make the boundary test green for the wrong reason."""
        with mock.patch.object(workers, "socket", self.spy_module):
            with self.store.transaction():
                workers.probe_port(free_port())
        self.assertEqual(self.binds_with_lock_held, [True])

    def test_probing_outside_the_transaction_is_recorded_as_lock_free(self):
        with mock.patch.object(workers, "socket", self.spy_module):
            workers.probe_port(free_port())
        self.assertEqual(self.binds_with_lock_held, [False])


# -------------------------------------------------------------- the probe


class TheProbeIsAnObservationNotAReservation(PortHarness):

    def test_a_free_port_probes_true(self):
        self.assertTrue(workers.probe_port(free_port()))

    def test_a_bound_port_probes_false(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.assertFalse(workers.probe_port(blocker.getsockname()[1]))

    def test_the_probe_releases_the_port_immediately(self):
        """It holds nothing: the same port probes true twice running."""
        port = free_port()
        self.assertTrue(workers.probe_port(port))
        self.assertTrue(workers.probe_port(port))

    def test_a_successful_probe_does_not_keep_the_port_available(self):
        """Point-in-time, and the docstring says so. Something else can take
        the port straight afterwards - C-09's reverse listener detection is
        what surfaces that, not this function."""
        port = free_port()
        self.assertTrue(workers.probe_port(port))
        taker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(taker.close)
        taker.bind(("127.0.0.1", port))
        taker.listen(1)
        self.assertFalse(workers.probe_port(port))


# ------------------------------------- selection honours every C-09 exclusion


class SelectionHonoursTheGovernedExclusions(PortHarness):

    def test_the_full_range_is_offered_when_nothing_is_excluded(self):
        candidates, _ = self.select(self.doc())
        self.assertEqual(candidates, list(range(LO, HI + 1)))

    def test_candidates_are_deterministic_and_ascending(self):
        first, _ = self.select(self.doc())
        second, _ = self.select(self.doc())
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_the_isolation_range_bounds_the_candidates(self):
        candidates, _ = self.select(self.doc())
        self.assertTrue(all(LO <= port <= HI for port in candidates))

    def test_a_port_owned_by_a_committed_record_is_excluded(self):
        candidates, _ = self.select(self.doc(w1=LO))
        self.assertNotIn(LO, candidates)
        self.assertEqual(candidates[0], LO + 1)

    def test_a_job_file_port_with_a_live_entry_is_excluded(self):
        self.write_job("w1", LO)
        candidates, _ = self.select(self.doc(), entries={"w1": 500})
        self.assertNotIn(LO, candidates)

    def test_a_proc_scan_failure_fails_closed(self):
        self.write_job("w1", LO)
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value=None), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            candidates, _ = workers.select_port_candidates(self.doc(), LO, HI)
        self.assertNotIn(LO, candidates)

    def test_an_ambiguous_agent_identity_retains_the_port(self):
        self.write_job("w1", LO)
        self.write_status("w1")
        candidates, _ = self.select(self.doc(), entries={}, alive=None)
        self.assertNotIn(LO, candidates)

    def test_a_reused_pid_with_different_ticks_frees_the_port(self):
        self.write_job("w1", LO)
        self.write_status("w1")
        candidates, _ = self.select(self.doc(), entries={}, alive=False)
        self.assertIn(LO, candidates)

    def test_a_terminal_status_frees_the_port(self):
        self.write_job("w1", LO)
        self.write_status("w1", phase="DONE")
        candidates, _ = self.select(self.doc(), entries={}, alive=True)
        self.assertIn(LO, candidates)

    def test_record_ownership_and_job_reservation_collide_independently(self):
        self.write_job("w1", LO + 1)
        candidates, _ = self.select(self.doc(other=LO), entries={"w1": 5})
        self.assertNotIn(LO, candidates)
        self.assertNotIn(LO + 1, candidates)
        self.assertEqual(candidates[0], LO + 2)

    def test_a_fully_excluded_range_yields_no_candidates_and_a_reason(self):
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value={}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            candidates, why = workers.select_port_candidates(
                self.doc(w1=LO), LO, LO)
        self.assertEqual(candidates, [])
        self.assertTrue(why)

    def test_the_reason_names_the_range_and_the_exclusion_counts(self):
        _, why = self.select(self.doc(w1=LO))
        self.assertIn(str(LO), why)
        self.assertIn(str(HI), why)
        self.assertIn("owned=1", why)
        self.assertIn("reserved=0", why)


# --------------------------------------- the composed form is unchanged


class AllocatePortStillBehavesExactlyAsBefore(PortHarness):

    def test_it_returns_the_first_candidate_that_binds(self):
        port, why = self.allocate(self.doc())
        self.assertEqual((port, why), (LO, ""))

    def test_an_externally_bound_candidate_is_skipped(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", LO))
        blocker.listen(1)
        port, _ = self.allocate(self.doc())
        self.assertEqual(port, LO + 1)

    def test_an_owned_port_is_skipped_without_being_probed(self):
        """Selection excludes it, so no bind is even attempted for it."""
        probed: list[int] = []
        with mock.patch.object(workers, "probe_port",
                               side_effect=lambda p: probed.append(p) or True):
            self.allocate(self.doc(w1=LO))
        self.assertNotIn(LO, probed)
        self.assertEqual(probed, [LO + 1])

    def test_probing_stops_at_the_first_success(self):
        probed: list[int] = []
        with mock.patch.object(workers, "probe_port",
                               side_effect=lambda p: probed.append(p) or True):
            port, _ = self.allocate(self.doc())
        self.assertEqual(probed, [LO])
        self.assertEqual(port, LO)

    def test_every_candidate_failing_to_bind_returns_the_governed_reason(self):
        with mock.patch.object(workers, "probe_port", return_value=False):
            port, why = self.allocate(self.doc())
        self.assertIsNone(port)
        self.assertIn("no bindable unowned port", why)

    def test_no_candidates_at_all_returns_the_governed_reason(self):
        with mock.patch.object(workers.proc, "worker_entry_processes",
                               return_value={}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            port, why = workers.allocate_port(self.doc(w1=LO), LO, LO)
        self.assertIsNone(port)
        self.assertIn("no bindable unowned port", why)

    def test_a_failed_allocation_leaves_no_reservation_behind(self):
        before = sorted(p.name for p in self.job_dir.iterdir())
        doc = self.doc()
        with mock.patch.object(workers, "probe_port", return_value=False):
            port, _ = self.allocate(doc)
        self.assertIsNone(port)
        self.assertEqual(sorted(p.name for p in self.job_dir.iterdir()), before)
        self.assertEqual(doc, self.doc(), "the document must be untouched")

    def test_the_default_range_still_comes_from_the_isolation_config(self):
        with mock.patch.object(workers.hostcheck, "read_candidate_port_range",
                               return_value=(LO, HI)) as read_range, \
                mock.patch.object(workers.proc, "worker_entry_processes",
                                  return_value={}), \
                mock.patch.object(workers.proc, "verified_alive",
                                  return_value=False):
            workers.allocate_port(self.doc())
        read_range.assert_called_once()


# ------------------------------------------- the integration boundary, stated


class NoDispatchPathStillBindsUnderTheLock(unittest.TestCase):
    """The stage-3 boundary marker, narrowed again by stage 6.

    It was written so nobody could read "stage 3 done" as "port binds have
    left T1" while BOTH dispatch sites still called the composed allocator.
    Stage 4 migrated the builder and the marker narrowed to "one composed
    call left, and it is the fixer's". Stage 6 migrated the fixer, which is
    the last of the two - so it narrows once more, to the end state it was
    always counting down to: NO composed allocator call survives in
    `supervisor.py`, and each dispatch site selects under the lock and probes
    outside it.

    It is still the answer to "have port binds left T1 yet?" - the answer is
    now "yes, for both dispatch roles" - and it still fails the moment a bind
    is put back, which is what the mutation testing for stages 4 and 6 uses it
    for. It deliberately keeps counting source occurrences rather than being
    deleted: a structural marker costs nothing and is the only thing that
    notices a regression someone writes by hand.
    """

    def source(self) -> str:
        return Path(supervisor_mod.__file__).read_text(encoding="utf-8")

    def test_no_composed_allocator_call_is_left(self):
        self.assertEqual(self.source().count("workers.allocate_port(doc)"), 0,
                         "a composed allocate_port call in the supervisor is a "
                         "bind under the state lock; use select_port_candidates "
                         "in the planning half and probe_port in the execute half")

    def test_the_builder_uses_the_split_halves(self):
        source = self.source()
        builder = source.index("def dispatch_builder")
        execute = source.index("def _execute_builder_dispatch", builder)
        commit = source.index("def _commit_builder_dispatch", execute)
        # Selection under the lock, in the planning half.
        self.assertIn("workers.select_port_candidates",
                      source[builder:execute])
        self.assertNotIn("workers.probe_port", source[builder:execute])
        # The bind outside it, in the execute half.
        self.assertIn("workers.probe_port", source[execute:commit])
        self.assertNotIn("workers.allocate_port", source[execute:commit])

    def test_the_fixer_uses_the_split_halves(self):
        source = self.source()
        fixer = source.index("def dispatch_fixer")
        execute = source.index("def _execute_fixer_dispatch", fixer)
        commit = source.index("def _commit_fixer_dispatch", execute)
        # Selection under the lock, in the planning half.
        self.assertIn("workers.select_port_candidates", source[fixer:execute])
        self.assertNotIn("workers.probe_port", source[fixer:execute])
        # The bind outside it, in the execute half.
        self.assertIn("workers.probe_port", source[execute:commit])
        self.assertNotIn("workers.allocate_port", source[execute:commit])

    def test_the_split_halves_exist_and_are_separately_callable(self):
        self.assertTrue(callable(workers.select_port_candidates))
        self.assertTrue(callable(workers.probe_port))


if __name__ == "__main__":
    unittest.main()
