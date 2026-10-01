"""The automated accessibility attempt — G3's phases on G2/G9's lifecycle.

Every external service is injected, so nothing here installs, builds,
binds, spawns or scans. What is tested is the SEQUENCING, the BOUNDS and
the ADJUDICATION, which is where the rules live.

The four properties that matter most, each with its own case:

  * every G3 blocking state has its OWN finite diagnostic, and there is no
    NOT_APPLICABLE route out of any of them;
  * teardown runs on EVERY exit path where a server was started, including
    when a phase raised;
  * a combined budget is not an uninterruptible one - each phase gets
    `min(own bound, what remains)`, recomputed from the clock;
  * the port is released ONLY when the listener is observed gone, so a
    perfect scan whose teardown cannot be confirmed is not a pass.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_contract as ac  # noqa: E402
from control import accessibility_evidence as ae  # noqa: E402
from control import config, routing  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40
PORT = 3200
BUDGET = ae.AttemptBudget.from_timeouts(config.load().extra["timeouts"])
PLAN = ae.AccessibilityPlan("TASK-001", 7, SHA, "attempt-0001", PORT)


def checks(result="PASS", sha=SHA, only=None, detail=None):
    out = []
    for check_id in routing.AUTOMATED_CHECK_IDS:
        entry = {"check_id": check_id, "sha": sha,
                 "artifact_reference": "evidence/a11y",
                 "result": result if (only is None or check_id in only) else "PASS"}
        if detail is not None and (only is None or check_id in only):
            entry["detail"] = detail
        out.append(entry)
    return out


class Services:
    """An injected stand-in for everything that touches the world."""

    def __init__(self, **over):
        self.stopped = 0
        self.bounds = {}
        self.cfg = dict(entrypoint=True, lockfile=True, install=(True, None),
                        server="handle", ready=True, scan=checks(),
                        listening=set(), raise_in=None)
        self.cfg.update(over)

    def exists(self, path):
        return self.cfg["entrypoint"] if path == ae.PRODUCT_ENTRYPOINT \
            else self.cfg["lockfile"]

    def _maybe_raise(self, phase):
        if self.cfg["raise_in"] == phase:
            raise RuntimeError("service exploded with a secret: hunter2")

    def install_build(self, bound):
        self.bounds["install"] = bound
        self._maybe_raise("install")
        return self.cfg["install"]

    def start_server(self, port, bound):
        self.bounds["serve"] = bound
        self._maybe_raise("serve")
        return self.cfg["server"]

    def await_ready(self, port, bound):
        self.bounds["ready"] = bound
        self._maybe_raise("ready")
        return self.cfg["ready"]

    def scan(self, port, sha, bound):
        self.bounds["scan"] = bound
        self._maybe_raise("scan")
        return self.cfg["scan"]

    def stop_server(self, handle, bound):
        self.bounds["stop"] = bound
        self.stopped += 1

    def listening_ports(self):
        return self.cfg["listening"]


def run(services, elapsed=0.0):
    return ae.run_attempt(PLAN, BUDGET, services, lambda: elapsed)


class BudgetCase(unittest.TestCase):

    def test_the_total_equals_the_sum_of_its_phases(self):
        self.assertEqual(BUDGET.phases_sum(), BUDGET.total)
        self.assertEqual(
            (BUDGET.install_build, BUDGET.readiness, BUDGET.scan,
             BUDGET.teardown, BUDGET.total),
            (600, 120, 300, 60, 1080))

    def test_each_phase_gets_its_own_bound_when_time_is_plentiful(self):
        svc = Services()
        run(svc)
        self.assertEqual(svc.bounds["install"], 600)
        self.assertEqual(svc.bounds["ready"], 120)
        self.assertEqual(svc.bounds["scan"], 300)

    def test_a_combined_budget_is_not_an_uninterruptible_one(self):
        # 1000 s already spent: install may have 80, not 600. The bound is
        # recomputed from the clock, not carried from an earlier plan.
        svc = Services()
        run(svc, elapsed=1000.0)
        self.assertEqual(svc.bounds["install"], 80.0)

    def test_a_phase_that_cannot_fit_is_never_started(self):
        svc = Services()
        outcome = run(svc, elapsed=BUDGET.total + 1)
        self.assertEqual(outcome.status, ae.FAILED)
        self.assertEqual(outcome.reason, ac.TIMED_OUT)
        self.assertNotIn("install", svc.bounds)

    def test_teardown_is_bounded_too(self):
        svc = Services()
        run(svc)
        self.assertLessEqual(svc.bounds["stop"], BUDGET.teardown)


class BlockingProductStatesCase(unittest.TestCase):
    """G3: no NOT_APPLICABLE route out of any of these."""

    def expect(self, reason, phase, **over):
        svc = Services(**over)
        outcome = run(svc)
        self.assertEqual(outcome.status, ae.FAILED)
        self.assertEqual(outcome.reason, reason)
        self.assertEqual(outcome.phase, phase)
        self.assertIsNone(outcome.verdict)
        return svc, outcome

    def test_a_missing_entrypoint_blocks_with_its_own_diagnostic(self):
        self.expect(ac.PRODUCT_ENTRYPOINT_MISSING, "PREFLIGHT",
                    entrypoint=False)

    def test_a_missing_lockfile_blocks_with_its_own_diagnostic(self):
        # npm ci requires it; without one the dependency set is resolved
        # fresh, which is a different build from the commit's.
        self.expect(ac.PRODUCT_LOCKFILE_MISSING, "PREFLIGHT", lockfile=False)

    def test_a_failed_install_blocks(self):
        self.expect(ac.PRODUCT_INSTALL_FAILED, "INSTALL_BUILD",
                    install=(False, ac.PRODUCT_INSTALL_FAILED))

    def test_a_failed_build_blocks(self):
        self.expect(ac.PRODUCT_BUILD_FAILED, "INSTALL_BUILD",
                    install=(False, ac.PRODUCT_BUILD_FAILED))

    def test_a_server_that_will_not_start_blocks(self):
        self.expect(ac.PRODUCT_SERVER_UNREADY, "SERVE", server=None)

    def test_a_server_that_never_answers_blocks(self):
        self.expect(ac.PRODUCT_SERVER_UNREADY, "READY", ready=False)

    def test_no_failure_path_produces_a_verdict(self):
        # The whole point: an attempt that could not run is the ABSENCE of
        # a judgement, never a judgement that the product is inaccessible.
        for over in (dict(entrypoint=False), dict(lockfile=False),
                     dict(install=(False, ac.PRODUCT_BUILD_FAILED)),
                     dict(server=None), dict(ready=False), dict(scan=None)):
            with self.subTest(over=over):
                self.assertIsNone(run(Services(**over)).verdict)

    def test_every_reason_emitted_is_in_the_governed_vocabulary(self):
        for over in (dict(entrypoint=False), dict(lockfile=False),
                     dict(install=(False, ac.PRODUCT_INSTALL_FAILED)),
                     dict(server=None), dict(ready=False), dict(scan=None),
                     dict(raise_in="install")):
            with self.subTest(over=over):
                reason = run(Services(**over)).reason
                self.assertIn(reason, ac.ACCESSIBILITY_AUTO_FAILURE_REASONS)


class TeardownAlwaysRunsCase(unittest.TestCase):

    def test_the_server_is_stopped_after_a_clean_scan(self):
        svc = Services()
        run(svc)
        self.assertEqual(svc.stopped, 1)

    def test_the_server_is_stopped_when_a_later_phase_fails(self):
        for over in (dict(ready=False), dict(scan=None)):
            with self.subTest(over=over):
                svc = Services(**over)
                run(svc)
                self.assertEqual(svc.stopped, 1)

    def test_the_server_is_stopped_when_a_phase_RAISES(self):
        # The path that matters most, and the one a happy-path cleanup
        # misses entirely.
        svc = Services(raise_in="scan")
        outcome = run(svc)
        self.assertEqual(svc.stopped, 1)
        self.assertEqual(outcome.status, ae.FAILED)

    def test_nothing_is_stopped_when_no_server_was_started(self):
        for over in (dict(entrypoint=False), dict(server=None)):
            with self.subTest(over=over):
                svc = Services(**over)
                run(svc)
                self.assertEqual(svc.stopped, 0)

    def test_a_raising_service_never_leaks_its_text_into_the_outcome(self):
        # C-16: fixed finite structural evidence only. The stub raises with
        # a secret-shaped string in the message on purpose.
        outcome = run(Services(raise_in="install"))
        self.assertEqual(outcome.reason, ac.SPAWN_OR_RUN_INCOMPLETE)
        self.assertNotIn("hunter2", repr(outcome))

    def test_a_teardown_that_raises_does_not_mask_the_result(self):
        svc = Services()
        svc.stop_server = lambda h, b: (_ for _ in ()).throw(OSError("boom"))
        outcome = run(svc)
        self.assertEqual(outcome.status, ae.COMPLETED)


class PortReleaseCase(unittest.TestCase):
    """G2: a stopped process is not a freed port."""

    def claim(self):
        return {"port": PORT, "port_released": False}

    def test_an_observed_gone_listener_releases_the_port(self):
        svc = Services()
        claim = self.claim()
        outcome = ae.confirm_release(run(svc), claim, svc)
        self.assertTrue(outcome.port_released)
        self.assertTrue(claim["port_released"])

    def test_a_still_listening_port_is_not_released_and_not_a_pass(self):
        svc = Services(listening={PORT})
        claim = self.claim()
        outcome = ae.confirm_release(run(svc), claim, svc)
        self.assertEqual(outcome.status, ae.FAILED)
        self.assertEqual(outcome.reason, ac.PRODUCT_SERVER_NOT_RELEASED)
        self.assertFalse(claim["port_released"])

    def test_a_failed_observation_is_not_an_absence(self):
        svc = Services(listening=None)
        claim = self.claim()
        outcome = ae.confirm_release(run(svc), claim, svc)
        self.assertEqual(outcome.reason, ac.PRODUCT_SERVER_NOT_RELEASED)
        self.assertFalse(claim["port_released"])

    def test_an_observation_that_raises_is_also_not_an_absence(self):
        svc = Services()
        svc.listening_ports = lambda: (_ for _ in ()).throw(OSError("x"))
        claim = self.claim()
        ae.confirm_release(run(svc), claim, svc)
        self.assertFalse(claim["port_released"])

    def test_an_attempt_that_never_bound_the_port_releases_it_regardless(self):
        # The asymmetry found while wiring this up. A missing entrypoint,
        # a failed build or a server that would not start means THIS
        # attempt never bound the port, so there is nothing of ours to
        # confirm gone. Applying the listener rule here would strand the
        # port permanently whenever something ELSE was listening on it.
        for over in (dict(entrypoint=False), dict(lockfile=False),
                     dict(install=(False, ac.PRODUCT_BUILD_FAILED)),
                     dict(server=None)):
            with self.subTest(over=over):
                svc = Services(listening={PORT}, **over)
                claim = self.claim()
                outcome = ae.confirm_release(run(svc), claim, svc)
                self.assertTrue(claim["port_released"])
                self.assertTrue(outcome.port_released)
                # The failure reason is preserved - releasing the port is
                # not the same as the attempt having succeeded.
                self.assertEqual(outcome.status, ae.FAILED)

    def test_an_attempt_that_DID_bind_still_needs_the_listener_gone(self):
        # The control for the case above: once a server really started,
        # the listener rule applies in full.
        for over in (dict(ready=False), dict(scan=None), {}):
            with self.subTest(over=over):
                svc = Services(listening={PORT}, **over)
                claim = self.claim()
                ae.confirm_release(run(svc), claim, svc)
                self.assertFalse(claim["port_released"])

    def test_server_started_is_recorded_honestly(self):
        self.assertFalse(run(Services(server=None)).server_started)
        self.assertFalse(run(Services(entrypoint=False)).server_started)
        self.assertTrue(run(Services(ready=False)).server_started)
        self.assertTrue(run(Services()).server_started)

    def test_a_perfect_scan_with_unconfirmed_teardown_does_not_pass(self):
        # Stated separately because it is the tempting shortcut: the scan
        # succeeded, so surely the attempt succeeded. It did not.
        svc = Services(listening={PORT})
        clean = run(svc)
        self.assertEqual(clean.status, ae.COMPLETED)
        self.assertEqual(
            ae.confirm_release(clean, self.claim(), svc).status, ae.FAILED)


class AdjudicationCase(unittest.TestCase):

    def test_all_passing_checks_yield_an_auto_pass(self):
        outcome = run(Services())
        self.assertEqual(outcome.status, ae.COMPLETED)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_PASS)
        self.assertEqual(ae.findings_for(outcome), [])

    def test_a_failing_check_yields_an_auto_fail_and_findings(self):
        svc = Services(scan=checks(result="FAIL", only={"TOUCH_TARGETS"}))
        outcome = run(svc)
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_FAIL)
        findings = ae.findings_for(outcome)
        self.assertEqual([f["unmet_requirement"] for f in findings],
                         ["ACC-DOD-TOUCH_TARGETS"])

    def test_a_composite_failure_yields_one_finding_per_condition(self):
        svc = Services(scan=checks(
            result="FAIL", only={"LABELS_AND_TEXT_ERRORS"},
            detail={"offenders": [{"reason": "NO_ACCESSIBLE_NAME"},
                                  {"reason": "INVALID_WITHOUT_TEXT_ERROR"}]}))
        findings = ae.findings_for(run(svc))
        self.assertEqual(
            [f["unmet_requirement"] for f in findings],
            ["ACC-DOD-MEANINGFUL_LABELS", "ACC-DOD-SCREEN_READER_FORMS_ERRORS"])

    def test_an_unmapped_failure_still_blocks(self):
        svc = Services(scan=checks(result="FAIL", only={"AXE_SCAN"},
                                   detail={"violations": [{"id": "image-alt"}]}))
        findings = ae.findings_for(run(svc))
        self.assertTrue(routing.accessibility_findings_block_merge(findings))

    def test_a_scan_of_the_wrong_commit_is_refused_not_adjudicated(self):
        svc = Services(scan=checks(sha=OTHER))
        outcome = run(svc)
        self.assertEqual(outcome.status, ae.FAILED)
        self.assertEqual(outcome.reason, ac.SHA_MISMATCH)

    def test_an_attempt_with_no_verdict_produces_no_findings(self):
        # An apparatus failure must never become evidence about the
        # product. A run that did not happen is not a failing page.
        for over in (dict(entrypoint=False), dict(ready=False),
                     dict(scan=None)):
            with self.subTest(over=over):
                self.assertEqual(ae.findings_for(run(Services(**over))), [])


if __name__ == "__main__":
    unittest.main()
