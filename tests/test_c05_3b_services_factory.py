"""C-05.3b: the services factory, the plumbing that was never built.

`accessibility_evidence.run_attempt` has always taken its external
services by injection, and nothing ever injected one -
`Supervisor.accessibility_services_factory` was None and `route_evidence`
is gated on it, so the automated half was fully wired and completely
inert. An earlier handover called that "no decision needed, it resolves
when a product exists to build". The plumbing is APPARATUS code though,
not product code: left undone, the first product PR would arrive with
nothing able to check it.

These cases exercise the REAL `control/accessibility_services`
ProductServices and the REAL `run_attempt` sequencing. Every genuinely
external edge - subprocess run, process spawn, HTTP readiness, the
/proc listener scan - is injected, because this suite must not run npm,
bind a socket, read /proc or create a git worktree. What is being proved
is that the apparatus honours its own contract; that is exactly the
discipline run_attempt itself follows.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import accessibility_contract as ac  # noqa: E402
from control import accessibility_evidence as ae  # noqa: E402
from control import accessibility_services as asvc  # noqa: E402
from control import config, gate_evidence, routing  # noqa: E402

SHA = "a" * 40
PORT = 3207


class FakeClock:
    """A monotonic clock that only moves when something says it did, so
    every bound below is exact rather than racing real time."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """A stand-in for subprocess.run with a scripted answer per command."""

    def __init__(self, answers=None, clock=None, costs=None):
        self.answers = answers or {}
        self.costs = costs or {}
        self.clock = clock
        self.calls: list[tuple[tuple, str, float]] = []

    def __call__(self, command, cwd, timeout, env=None):
        key = tuple(command)[:2]
        self.calls.append((tuple(command), cwd, timeout))
        if self.clock is not None:
            self.clock.advance(self.costs.get(key, 0.0))
        return self.answers.get(key, (True, 0))


def services_in(root: Path, *, runner=None, spawner=None, http_ok=None,
                listener=None, clock=None, checkout_name="checkout"):
    checkout = root / checkout_name
    checkout.mkdir(parents=True, exist_ok=True)
    ctx = gate_evidence.context(root, SHA, f"http://127.0.0.1:{PORT}/")
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or FakeClock()
    return asvc.ProductServices(
        checkout, ctx,
        runner=runner or Recorder(clock=clock),
        spawner=spawner or (lambda *a, **k: None),
        http_ok=http_ok or (lambda url, timeout: True),
        listener=listener or (lambda: set()),
        sleep=lambda seconds: clock.advance(seconds),
        monotonic=clock), checkout, ctx, clock


class PreflightCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_exists_reads_the_isolated_checkout_not_the_repository(self):
        """The whole point of G3's clean checkout: the apparatus must not
        answer "is there a package.json" by looking at its own tree."""
        svc, checkout, _, _ = services_in(self.root)
        self.assertFalse(svc.exists(ae.PRODUCT_ENTRYPOINT))
        (checkout / ae.PRODUCT_ENTRYPOINT).write_text("{}", encoding="utf-8")
        self.assertTrue(svc.exists(ae.PRODUCT_ENTRYPOINT))
        self.assertFalse(svc.exists(ae.PRODUCT_LOCKFILE))


class InstallBuildCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, answers=None, costs=None):
        clock = FakeClock()
        runner = Recorder(answers=answers, clock=clock, costs=costs)
        svc, _, _, _ = services_in(self.root, runner=runner, clock=clock)
        return svc, runner

    def test_a_clean_run_reports_no_reason(self):
        svc, runner = self.build()
        self.assertEqual(svc.install_build(600), (True, None))
        self.assertEqual([c[0][:2] for c in runner.calls],
                         [("npm", "ci"), ("npm", "run")])

    def test_a_failed_install_is_distinguished_from_a_failed_build(self):
        """Two very different places to send a task. Collapsing them into
        one code would lose which one happened."""
        svc, _ = self.build(answers={("npm", "ci"): (False, 1)})
        self.assertEqual(svc.install_build(600),
                         (False, ac.PRODUCT_INSTALL_FAILED))

    def test_a_failed_build_says_so(self):
        svc, _ = self.build(answers={("npm", "run"): (False, 1)})
        self.assertEqual(svc.install_build(600),
                         (False, ac.PRODUCT_BUILD_FAILED))

    def test_the_build_is_never_started_after_a_failed_install(self):
        svc, runner = self.build(answers={("npm", "ci"): (False, 1)})
        svc.install_build(600)
        self.assertEqual([c[0][:2] for c in runner.calls], [("npm", "ci")])

    def test_the_install_gets_its_governed_share_of_the_bound(self):
        svc, runner = self.build()
        svc.install_build(600)
        self.assertAlmostEqual(runner.calls[0][2], 600 * asvc.INSTALL_SHARE)

    def test_the_build_gets_what_is_actually_left_not_a_fixed_share(self):
        """Read from the clock, so an install that finished early hands
        its remainder over instead of wasting it."""
        svc, runner = self.build(costs={("npm", "ci"): 10.0})
        svc.install_build(600)
        self.assertAlmostEqual(runner.calls[1][2], 590.0)

    def test_an_install_that_eats_the_whole_bound_times_out(self):
        """And does not start a build with a negative budget."""
        svc, runner = self.build(costs={("npm", "ci"): 600.0})
        self.assertEqual(svc.install_build(600), (False, ac.TIMED_OUT))
        self.assertEqual(len(runner.calls), 1)


class ServerLifecycleCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_server_that_will_not_spawn_is_none_not_an_exception(self):
        """run_attempt turns None into PRODUCT_SERVER_UNREADY. An
        exception would be swallowed by its blanket handler and collapse
        to SPAWN_OR_RUN_INCOMPLETE, losing which thing failed."""
        svc, _, _, _ = services_in(self.root, spawner=lambda *a, **k: None)
        self.assertIsNone(svc.start_server(PORT, 120))

    def test_the_claimed_port_reaches_the_server_through_the_environment(self):
        seen = {}

        def spawner(command, cwd, env):
            seen.update(env=env, command=tuple(command), cwd=cwd)
            return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        svc, checkout, _, _ = services_in(self.root, spawner=spawner)
        svc.start_server(PORT, 120)
        self.assertEqual(seen["env"]["PORT"], str(PORT))
        self.assertEqual(seen["env"]["NODE_ENV"], "production")
        self.assertEqual(seen["command"], asvc.START_COMMAND)
        self.assertEqual(seen["cwd"], str(checkout))

    def test_readiness_is_whether_it_answered_at_all(self):
        svc, _, _, _ = services_in(self.root, http_ok=lambda url, t: True)
        self.assertTrue(svc.await_ready(PORT, 120))

    def test_readiness_gives_up_at_its_bound_rather_than_forever(self):
        clock = FakeClock()
        svc, _, _, _ = services_in(self.root, http_ok=lambda url, t: False,
                                   clock=clock)
        self.assertFalse(svc.await_ready(PORT, 30))
        self.assertGreaterEqual(clock.t, 30)

    def test_readiness_polls_the_claimed_port_on_loopback_only(self):
        asked = []
        svc, _, _, _ = services_in(
            self.root, http_ok=lambda url, t: asked.append(url) or True)
        svc.await_ready(PORT, 120)
        self.assertEqual(asked, [f"http://127.0.0.1:{PORT}/"])


class TeardownCase(unittest.TestCase):
    """G2's rule has teeth only if teardown can actually finish: a server
    that survives SIGTERM holds a governed port for the rest of the run,
    because confirm_release refuses to release a listening one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.signals: list[tuple[int, int]] = []

    def handle(self, *, dies_after=0):
        state = {"polls": 0}

        def poll():
            state["polls"] += 1
            return 0 if state["polls"] > dies_after else None
        return SimpleNamespace(process=SimpleNamespace(pid=4242, poll=poll),
                               pgid=4242)

    def run_stop(self, handle, bound=60):
        clock = FakeClock()
        svc, _, _, _ = services_in(self.root, clock=clock)
        with mock.patch.object(asvc.os, "killpg",
                               side_effect=lambda p, s: self.signals.append((p, s))):
            svc.stop_server(handle, bound)

    def test_a_cooperative_server_is_only_asked_politely(self):
        self.run_stop(self.handle(dies_after=1))
        self.assertEqual([s for _, s in self.signals], [signal.SIGTERM])

    def test_a_server_that_ignores_sigterm_is_killed(self):
        self.run_stop(self.handle(dies_after=10_000))
        self.assertEqual([s for _, s in self.signals],
                         [signal.SIGTERM, signal.SIGKILL])

    def test_the_whole_process_group_is_signalled_not_just_npm(self):
        """`npm run start` execs Next, which spawns workers. Signalling
        only npm leaves the real listener holding the port."""
        self.run_stop(self.handle(dies_after=10_000))
        self.assertEqual({pgid for pgid, _ in self.signals}, {4242})

    def test_stopping_nothing_is_not_an_error(self):
        self.run_stop(None)
        self.assertEqual(self.signals, [])

    def test_a_process_that_vanished_is_the_outcome_we_wanted(self):
        clock = FakeClock()
        svc, _, _, _ = services_in(self.root, clock=clock)
        with mock.patch.object(asvc.os, "killpg",
                               side_effect=ProcessLookupError):
            svc.stop_server(self.handle(dies_after=0), 60)   # must not raise


class ScanCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_result(self, ctx, payload):
        Path(ctx.result_path).write_text(json.dumps(payload), encoding="utf-8")

    def test_the_checks_come_from_the_attempts_own_result_file(self):
        svc, _, ctx, _ = services_in(self.root)
        self.write_result(ctx, {"checks": [{"check_id": "AXE_SCAN"}]})
        self.assertEqual(svc.scan(PORT, SHA, 300),
                         [{"check_id": "AXE_SCAN"}])

    def test_a_missing_result_file_is_none_not_an_empty_pass(self):
        svc, _, _, _ = services_in(self.root)
        self.assertIsNone(svc.scan(PORT, SHA, 300))

    def test_an_unreadable_result_file_is_none(self):
        svc, _, ctx, _ = services_in(self.root)
        Path(ctx.result_path).write_text("{not json", encoding="utf-8")
        self.assertIsNone(svc.scan(PORT, SHA, 300))

    def test_a_non_list_checks_field_is_unusable_not_empty(self):
        """Reading a broken result as "no checks" is exactly how a failed
        scan becomes a clean pass."""
        svc, _, ctx, _ = services_in(self.root)
        self.write_result(ctx, {"checks": "everything was fine"})
        self.assertIsNone(svc.scan(PORT, SHA, 300))

    def test_the_scan_runs_the_landed_apparatus_command(self):
        runner = Recorder()
        svc, _, ctx, _ = services_in(self.root, runner=runner)
        self.write_result(ctx, {"checks": []})
        svc.scan(PORT, SHA, 300)
        self.assertEqual(list(runner.calls[0][0]),
                         gate_evidence.node_command(ctx))
        self.assertEqual(runner.calls[0][2], 300)


class FactoryCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = ae.AccessibilityPlan(task_id="TASK-001", pr=7, sha=SHA,
                                         attempt_id="a11y-attempt-0001",
                                         port=PORT)

    def allocate(self, task_id, sha):
        attempt = self.root / task_id / sha / "attempt-0001"
        (attempt / gate_evidence.ARTIFACT_DIR).mkdir(parents=True)
        return attempt

    def test_it_builds_services_for_the_claimed_port_and_sha(self):
        made = {}

        def checkout(sha, attempt_dir):
            made.update(sha=sha)
            tree = Path(attempt_dir) / "checkout"
            tree.mkdir()
            return tree
        svc = asvc.factory(self.plan, allocate=self.allocate, checkout=checkout)
        self.assertEqual(made["sha"], SHA)
        self.assertEqual(svc.ctx.sha, SHA)
        self.assertEqual(svc.ctx.url, f"http://127.0.0.1:{PORT}/")

    def test_a_checkout_that_cannot_be_made_fails_preflight_not_the_harness(self):
        """The distinction worth keeping: "the product has no entrypoint"
        and "the apparatus fell over" are different findings. Raising here
        would be caught by run_attempt's blanket handler and collapse both
        into SPAWN_OR_RUN_INCOMPLETE."""
        svc = asvc.factory(self.plan, allocate=self.allocate,
                           checkout=lambda sha, attempt_dir: None)
        self.assertFalse(svc.exists(ae.PRODUCT_ENTRYPOINT))

        outcome = ae.run_attempt(self.plan, ae.AttemptBudget(
            600, 120, 300, 60, 1080), svc, lambda: 0.0)
        self.assertEqual(outcome.reason, ac.PRODUCT_ENTRYPOINT_MISSING)
        self.assertEqual(outcome.phase, "PREFLIGHT")


class ThroughTheRealAttemptCase(unittest.TestCase):
    """The factory's services driven by the REAL run_attempt, so the
    contract is proved against the sequencer that will actually call it
    rather than against this file's reading of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = ae.AccessibilityPlan(task_id="TASK-001", pr=7, sha=SHA,
                                         attempt_id="a11y-attempt-0001",
                                         port=PORT)
        self.budget = ae.AttemptBudget(600, 120, 300, 60, 1080)
        self.stopped: list = []

    def build(self, *, checks=None, http=True):
        clock = FakeClock()
        self.handle = SimpleNamespace(process=SimpleNamespace(
            pid=4242, poll=lambda: 0), pgid=4242)
        svc, checkout, ctx, _ = services_in(
            self.root, clock=clock,
            spawner=lambda *a, **k: self.handle.process,
            http_ok=lambda url, t: http,
            listener=lambda: set())
        (checkout / ae.PRODUCT_ENTRYPOINT).write_text("{}", encoding="utf-8")
        (checkout / ae.PRODUCT_LOCKFILE).write_text("{}", encoding="utf-8")
        if checks is not None:
            Path(ctx.result_path).write_text(json.dumps({"checks": checks}),
                                             encoding="utf-8")
        real_stop = svc.stop_server
        svc.stop_server = lambda handle, bound: (
            self.stopped.append(bound), None)[1]
        self.real_stop = real_stop
        return svc, clock

    def passing_checks(self):
        return [{"check_id": cid, "sha": SHA, "result": "PASS",
                 "artifact_reference": "evidence/a11y"}
                for cid in routing.AUTOMATED_CHECK_IDS]

    def test_a_clean_attempt_completes_through_every_phase(self):
        svc, _ = self.build(checks=self.passing_checks())
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        self.assertEqual(outcome.status, ae.COMPLETED)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_PASS)

    def test_the_server_is_torn_down_even_when_the_scan_produced_nothing(self):
        """G3's cleanup-on-every-exit-path, proved against a real failure
        rather than the happy path."""
        svc, _ = self.build(checks=None)
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        self.assertEqual(outcome.reason, ac.RESULT_MISSING)
        self.assertEqual(len(self.stopped), 1)

    def test_an_unreachable_server_is_blocking_and_still_torn_down(self):
        svc, _ = self.build(checks=self.passing_checks(), http=False)
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        self.assertEqual(outcome.reason, ac.PRODUCT_SERVER_UNREADY)
        self.assertEqual(len(self.stopped), 1)

    def test_the_port_is_not_released_while_anything_is_listening(self):
        """The factory's listener feeds G2's rule. A stopped process is
        not a freed port, and "we could not look" is not "nothing there"."""
        svc, _ = self.build(checks=self.passing_checks())
        svc._listener = lambda: {PORT}
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        confirmed = ae.confirm_release(outcome, {"port": PORT}, svc)
        self.assertFalse(confirmed.port_released)

    def test_a_failed_listener_observation_also_holds_the_port(self):
        svc, _ = self.build(checks=self.passing_checks())
        svc._listener = lambda: None
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        confirmed = ae.confirm_release(outcome, {"port": PORT}, svc)
        self.assertFalse(confirmed.port_released)

    def test_a_quiet_port_is_released(self):
        svc, _ = self.build(checks=self.passing_checks())
        outcome = ae.run_attempt(self.plan, self.budget, svc, lambda: 0.0)
        confirmed = ae.confirm_release(outcome, {"port": PORT}, svc)
        self.assertTrue(confirmed.port_released)


class DisposalCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_disposing_removes_the_isolated_checkout(self):
        svc, checkout, _, _ = services_in(self.root)
        self.assertTrue(checkout.exists())
        with mock.patch.object(asvc.gh, "git",
                               return_value=SimpleNamespace(ok=True)):
            svc.dispose()
        self.assertFalse(checkout.exists())

    def test_disposal_never_raises_when_git_refuses(self):
        """A checkout that will not go away is disk to reclaim, never a
        reason to lose a verdict that is already durable."""
        svc, checkout, _, _ = services_in(self.root)
        with mock.patch.object(asvc.gh, "git",
                               return_value=SimpleNamespace(ok=False)):
            svc.dispose()
        self.assertFalse(checkout.exists())   # the rmtree fallback ran

    def test_disposing_twice_is_harmless(self):
        svc, _, _, _ = services_in(self.root)
        with mock.patch.object(asvc.gh, "git",
                               return_value=SimpleNamespace(ok=True)):
            svc.dispose()
            svc.dispose()


class WiringCase(unittest.TestCase):
    """The factory is only useful if the Supervisor actually holds one."""

    def test_a_constructed_supervisor_has_the_real_factory(self):
        from control import supervisor as sv_mod
        with mock.patch.object(sv_mod, "ledger_mod"), \
                mock.patch.object(sv_mod, "notify"), \
                mock.patch.object(sv_mod, "telemetry"), \
                mock.patch.object(sv_mod, "jev"):
            sup = sv_mod.Supervisor(config.load())
        self.assertIs(sup.accessibility_services_factory, asvc.factory)

    def test_a_bare_supervisor_still_refuses_to_plan(self):
        """A Supervisor built through __new__ - which several suites do -
        has no factory attribute at all, and route_evidence's getattr
        default must keep it from claiming ports it could never use."""
        from control import supervisor as sv_mod
        bare = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.assertIsNone(getattr(bare, "accessibility_services_factory", None))


if __name__ == "__main__":
    unittest.main()
