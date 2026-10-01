"""C-05.3c — the automated accessibility pipeline actually RUN.

WHAT THIS CLOSES. `control/accessibility_services.py` landed with 36
fixture tests in which **every external edge is injected**: no npm had
run, no socket had been bound, no browser had been driven, no process
group had been killed. The pipeline was connected and had never executed.
Handover 43.5(a) recorded that as the first remaining piece.

Everything below uses the REAL services: the real `subprocess` runner, the
real `Popen` spawner in its own process group, the real `urllib` readiness
probe, the real `/proc/net/tcp` listener scan, the real `git worktree
add --detach`, the real `node apparatus/accessibility/run.js` driving a
real Chromium through a real axe-core scan.

WHAT IT STILL IS NOT. A FIXTURE. The thing being built and scanned is
`apparatus/accessibility/fixture-product/`, two dependency-free static
servers that exist to be scanned. There is no product app, no PR, no
deployed URL, no provider call and no GitHub call anywhere in this file.
`prompts/accessibility.md`'s "has not yet been exercised against a real
product app or PR" remains true, and C-02b's record says so explicitly.

WHY IT IS OPT-IN. Each scan launches Chromium and takes tens of seconds,
so the suite that runs on every edit must not pay for it. Set
`RUN_002_LOCAL_FIXTURE_SCAN=1` to run it. That is a deliberate trade and
it has a cost: an unattended suite does not notice this breaking.

ISOLATION. Every attempt directory is a `TemporaryDirectory`, never
`.runtime/evidence`; no durable run state is created, read or modified.
The port comes from the governed candidate range and is probed free
first; the claim is a local dict, not a committed worker record.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_contract as ac
from control import (accessibility_evidence, accessibility_registry,
                     accessibility_services, config, gate_evidence, gh,
                     hostcheck, proc, routing, severity)

FIXTURE_ROOT = "apparatus/accessibility/fixture-product"
ENABLED = os.environ.get("RUN_002_LOCAL_FIXTURE_SCAN") == "1"
SKIP_WHY = "set RUN_002_LOCAL_FIXTURE_SCAN=1 — launches a real browser"

# The nine automated check ids run.js produces. The tenth in the schema's
# enum, COGNITIVE_SENSORY_REVIEW, is the qualitative review and is
# deliberately not among them.
EXPECTED_CHECKS = frozenset({
    "RESPONSIVE_375PX", "NO_OVERFLOW_CLIPPING_OVERLAP", "TOUCH_TARGETS",
    "KEYBOARD_OPERATION", "FOCUS_ORDER_VISIBLE_NO_TRAPS",
    "LABELS_AND_TEXT_ERRORS", "REDUCED_MOTION_NO_FLASHING_AUTOPLAY",
    "AXE_SCAN", "SCREENSHOTS_ARTIFACTS",
})


def head_sha() -> str:
    result = gh.git(["rev-parse", "HEAD"], str(config.REPO_ROOT))
    return result.stdout.strip()


def free_governed_port() -> int:
    """A port in the governed candidate range that nothing holds.

    Probed by actually binding it and letting go, not by reading the
    listener table: the table answers "is something listening", and a
    socket in another state can still refuse the bind we are about to ask
    the fixture server to make.
    """
    lo, hi = hostcheck.read_candidate_port_range()
    for port in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise unittest.SkipTest(f"no free port in the governed range {lo}-{hi}")


def budget() -> accessibility_evidence.AttemptBudget:
    timeouts = json.loads(
        (config.REPO_ROOT / "config" / "experiment.json").read_text(
            encoding="utf-8"))["timeouts"]
    return accessibility_evidence.AttemptBudget.from_timeouts(timeouts)


@unittest.skipUnless(ENABLED, SKIP_WHY)
class RealServicesCase(unittest.TestCase):
    """One attempt per variant, through the real services, start to finish."""

    def setUp(self):
        self.sha = head_sha()
        self.assertRegex(self.sha, r"^[0-9a-f]{40}$",
                         "git did not give a usable HEAD")
        self.root = tempfile.TemporaryDirectory(prefix="run002-fixture-scan-")
        self.addCleanup(self.root.cleanup)

    # ------------------------------------------------------------ helpers

    def _attempt(self, variant: str, port: int | None = None):
        """Allocate an isolated attempt and REAL services for one variant.

        The checkout is a genuine detached worktree at HEAD, so the page
        that gets scanned is the page that is committed at the SHA the
        evidence will name. That is the provenance property, exercised
        rather than asserted.
        """
        port = free_governed_port() if port is None else port
        attempt_dir = gate_evidence.allocate_attempt(
            "TASK-FIXTURE", self.sha, root=Path(self.root.name))
        ctx = gate_evidence.context(
            attempt_dir, self.sha, f"http://127.0.0.1:{port}/")
        tree = accessibility_services.isolated_checkout(self.sha, attempt_dir)
        self.assertIsNotNone(tree, "git worktree add --detach failed")
        product = Path(tree) / FIXTURE_ROOT / variant
        self.assertTrue((product / "package.json").is_file(),
                        f"{variant} fixture is not committed at {self.sha}")
        services = accessibility_services.ProductServices(product, ctx)
        self.addCleanup(services.dispose)
        plan = accessibility_evidence.AccessibilityPlan(
            task_id="TASK-FIXTURE", pr=0, sha=self.sha,
            attempt_id=attempt_dir.name, port=port)
        return plan, services, ctx

    def _run(self, plan, services):
        started = time.monotonic()
        return accessibility_evidence.run_attempt(
            plan, budget(), services, lambda: time.monotonic() - started)

    def _assert_nine_checks_bound_to_head(self, outcome, plan):
        ids = {c["check_id"] for c in outcome.checks}
        self.assertEqual(ids, EXPECTED_CHECKS)
        for check in outcome.checks:
            self.assertEqual(
                check["sha"], plan.sha,
                "a check named a commit other than the one scanned")

    # ------------------------------------------------------- the pass case

    def test_the_clean_fixture_passes_through_the_real_services(self):
        """Install, build, serve, poll, scan, tear down, release — for real."""
        plan, services, ctx = self._attempt("clean")
        outcome = self._run(plan, services)

        self.assertEqual(outcome.status, accessibility_evidence.COMPLETED,
                         f"attempt failed in {outcome.phase}: {outcome.reason}")
        self.assertEqual(outcome.verdict, ac.ACCESSIBILITY_AUTO_PASS)
        self.assertTrue(outcome.server_started,
                        "a passing scan must have had a server to scan")
        self._assert_nine_checks_bound_to_head(outcome, plan)

        # The scan wrote its evidence where C-05.1 persists it, not to a
        # path of its own choosing.
        self.assertTrue(Path(ctx.result_path).is_file())
        payload = json.loads(Path(ctx.result_path).read_text(encoding="utf-8"))
        self.assertEqual({c["check_id"] for c in payload["checks"]},
                         EXPECTED_CHECKS)

        # G2's teardown confirmation, against the real listener table.
        claim = {"port": plan.port, "port_released": False}
        released = accessibility_evidence.confirm_release(
            outcome, claim, services)
        self.assertTrue(released.port_released,
                        f"port not released: {released.reason}")
        self.assertTrue(claim["port_released"])
        lo, hi = hostcheck.read_candidate_port_range()
        self.assertNotIn(plan.port, proc.listening_ports(lo, hi) or set(),
                         "the fixture server outlived teardown")

    # ---------------------------------------------------- the failure case

    def test_the_violations_fixture_fails_through_the_real_services(self):
        """A known-inaccessible page is caught by the same real path."""
        plan, services, _ = self._attempt("violations")
        outcome = self._run(plan, services)

        self.assertEqual(outcome.status, accessibility_evidence.COMPLETED,
                         f"attempt failed in {outcome.phase}: {outcome.reason}")
        self.assertEqual(
            outcome.verdict, ac.ACCESSIBILITY_AUTO_FAIL,
            "the violations fixture scanned as accessible")
        self._assert_nine_checks_bound_to_head(outcome, plan)

        failed = {c["check_id"] for c in outcome.checks
                  if c["result"] == "FAIL"}
        self.assertTrue(failed, "AUTO_FAIL with nothing actually failing")

        # The failure is usable evidence, not just a verdict. Every failing
        # check becomes a finding, and EVERY finding blocks — by the P1
        # floor when G6 maps it to a requirement, and by invalidity when it
        # does not. G6's "unmapped failures remain blocking" is the half
        # that is easy to get wrong, so it is asserted directly.
        findings = accessibility_evidence.findings_for(outcome)
        self.assertTrue(findings)
        known = accessibility_registry.requirement_ids()
        rated = [severity.apply_severity_policy(f, known_requirement_ids=known)
                 for f in findings]
        for finding, verdict in zip(findings, rated):
            self.assertTrue(
                verdict["merge_blocked"],
                f"a failing check produced a non-blocking finding: {finding}")
            if verdict["valid"]:
                self.assertIn(
                    verdict["severity"], {"P0", "P1"},
                    "an accessibility FAILURE finding escaped the P1 floor")
            else:
                self.assertIsNone(
                    finding["unmet_requirement"],
                    "a finding citing a real requirement was rated INVALID")
        self.assertTrue(
            any(v["valid"] and v["severity"] == "P1" for v in rated),
            "no failing check mapped to a real requirement at all")

        # Same teardown guarantee on the failure path — the path where a
        # leaked server is most likely.
        claim = {"port": plan.port, "port_released": False}
        released = accessibility_evidence.confirm_release(
            outcome, claim, services)
        self.assertTrue(released.port_released,
                        f"port not released: {released.reason}")

    # ------------------------------------------- the properties in isolation

    def test_a_still_listening_server_is_not_released(self):
        """G2's rule, proved against a server that is genuinely still up.

        Not a stub returning a port number: the fixture server is started
        here through the real spawner and deliberately NOT stopped, so the
        real /proc listener scan sees it, and `confirm_release` must
        refuse. Then it is stopped and the release must succeed — the same
        observation flipping on a real change in the world.
        """
        plan, services, _ = self._attempt("clean")
        ok, reason = services.install_build(600)
        self.assertTrue(ok, f"fixture would not build: {reason}")
        handle = services.start_server(plan.port, 120)
        self.assertIsNotNone(handle)
        self.addCleanup(services.stop_server, handle, 60)
        self.assertTrue(services.await_ready(plan.port, 120),
                        "the fixture server never answered")

        scanned = accessibility_evidence.AttemptOutcome(
            status=accessibility_evidence.COMPLETED,
            verdict=ac.ACCESSIBILITY_AUTO_PASS, reason="", checks=[],
            port_released=False, phase="SCAN", server_started=True)

        claim = {"port": plan.port, "port_released": False}
        refused = accessibility_evidence.confirm_release(
            scanned, claim, services)
        self.assertFalse(refused.port_released)
        self.assertEqual(refused.reason, ac.PRODUCT_SERVER_NOT_RELEASED)
        self.assertFalse(claim["port_released"],
                         "a refused release must not mutate the claim")

        services.stop_server(handle, 60)
        allowed = accessibility_evidence.confirm_release(
            scanned, claim, services)
        self.assertTrue(allowed.port_released,
                        f"still refused after teardown: {allowed.reason}")

    def test_the_process_group_really_dies(self):
        """`npm run start` execs node; killing npm alone leaves the listener.

        start_new_session + killpg is the reason teardown can finish. This
        observes the actual pid after the stop, not a mock's call log.
        """
        plan, services, _ = self._attempt("clean")
        ok, reason = services.install_build(600)
        self.assertTrue(ok, f"fixture would not build: {reason}")
        handle = services.start_server(plan.port, 120)
        self.assertIsNotNone(handle)
        self.assertTrue(services.await_ready(plan.port, 120))
        pid = handle.process.pid

        services.stop_server(handle, 60)
        self.assertIsNotNone(handle.process.poll(), "npm itself survived")
        deadline = time.monotonic() + 10
        lo, hi = hostcheck.read_candidate_port_range()
        while time.monotonic() < deadline:
            if plan.port not in (proc.listening_ports(lo, hi) or set()):
                break
            time.sleep(0.1)
        self.assertNotIn(plan.port, proc.listening_ports(lo, hi) or set(),
                         "the server the npm wrapper exec'd is still bound")
        self.assertFalse(proc.is_running(pid),
                         "the process group leader is still alive")

    def test_install_build_honours_the_bound_it_is_given(self):
        """Bounded execution, measured — not asserted from the source.

        An impossible bound must come back as a finite diagnostic in about
        the time of the bound, never as an exception and never as a hang.
        """
        plan, services, _ = self._attempt("clean")
        started = time.monotonic()
        ok, reason = services.install_build(0.05)
        waited = time.monotonic() - started
        self.assertFalse(ok)
        self.assertEqual(reason, ac.PRODUCT_INSTALL_FAILED)
        self.assertLess(waited, 30,
                        "the bound was not enforced on the real subprocess")

    def test_a_checkout_without_the_product_entrypoint_blocks(self):
        """No NOT_APPLICABLE route, against a real empty directory."""
        plan, services, _ = self._attempt("clean")
        empty = accessibility_services.ProductServices(
            Path(self.root.name) / "nothing-here", services.ctx)
        outcome = self._run(plan, empty)
        self.assertEqual(outcome.status, accessibility_evidence.FAILED)
        self.assertEqual(outcome.reason, ac.PRODUCT_ENTRYPOINT_MISSING)
        self.assertEqual(outcome.phase, "PREFLIGHT")
        self.assertFalse(outcome.server_started)
        self.assertIsNone(outcome.verdict,
                          "a blocked attempt must not carry a verdict")

    def test_evidence_from_this_scan_is_refused_for_a_different_head(self):
        """Exact-head provenance, on real scan output.

        The same check list that is usable evidence about the commit it
        names must be unusable about any other. This is the one assertion
        that makes the SHA on each check load-bearing rather than
        decorative.
        """
        plan, services, _ = self._attempt("clean")
        outcome = self._run(plan, services)
        self.assertEqual(outcome.status, accessibility_evidence.COMPLETED)

        other = "0" * 39 + "1"
        verdict, why = routing.normalize_accessibility_auto(
            outcome.checks, other)
        self.assertIsNone(verdict)
        self.assertEqual(why, routing.SHA_MISMATCH)

    def test_the_fixture_is_not_mistaken_for_the_product(self):
        """The checkout root has no entrypoint; only the fixture subdir does.

        If a `package.json` ever lands at the repository root, a real
        attempt would build and scan THAT, and this fixture work would
        have quietly changed what the gate judges.
        """
        _, services, _ = self._attempt("clean")
        root = Path(services.checkout).parents[
            len(Path(FIXTURE_ROOT).parts)]
        self.assertFalse((root / "package.json").exists(),
                         "a root package.json would be scanned as the product")
        self.assertTrue((root / "AGENTS.md").is_file(),
                        "sanity: this should be the worktree root")


@unittest.skipUnless(ENABLED, SKIP_WHY)
class RealToolingCase(unittest.TestCase):
    """The host can actually do what the pipeline assumes."""

    def test_npm_and_node_are_present(self):
        for tool in (["node", "--version"], ["npm", "--version"]):
            done = subprocess.run(tool, capture_output=True, text=True,
                                  timeout=60, check=False)
            self.assertEqual(done.returncode, 0, f"{tool[0]} is unusable")


if __name__ == "__main__":
    unittest.main()
