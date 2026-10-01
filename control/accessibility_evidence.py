"""The automated accessibility attempt: G3's phases on G2/G9's lifecycle.

ONE ATTEMPT, five phases, every external service INJECTED:

    preflight  the governed product entrypoint and lockfile exist
    install    npm ci + npm run build        <= install_build_seconds
    serve      start the product server      (port owned by the claim)
    ready      poll until it answers         <= product_server_readiness_seconds
    scan       run.js against that server    <= accessibility_hard_seconds
    teardown   stop it, CONFIRM gone         <= accessibility_teardown_seconds

bounded in total by `accessibility_attempt_seconds`, which equals the sum
of its parts (600 + 120 + 300 + 60 = 1080).

WHY "ONE COMBINED TIMEOUT" IS NOT UNINTERRUPTIBLE. Each phase is handed
`min(its own bound, what remains of the attempt)`, recomputed immediately
before it starts from the clock rather than from a plan made earlier. A
phase that cannot fit in what remains is never started. The injected
service is required to honour the bound it is given by killing its
subprocess - a service that blocks past its bound is a defect in that
service, and `run_attempt` cannot rescue it, which is stated here rather
than pretended otherwise.

TEARDOWN ALWAYS RUNS. If a server was started, it is stopped on every exit
path - success, failure, refusal, or an exception out of any phase. That
is G3's "failure or cancellation at any phase must trigger cleanup of
owned processes", and it is a `finally`, not a happy-path step.

THE PORT IS NOT RELEASED UNTIL THE LISTENER IS OBSERVED GONE. G2's rule,
and the one a plausible implementation gets wrong: a stopped process is
not a freed port. `workers.port_release_permitted` fails closed on a port
still listening AND on a failed observation, so "we could not look" never
reads as "nothing is there". An attempt that scanned perfectly but could
not confirm teardown reports PRODUCT_SERVER_NOT_RELEASED and keeps the
port claimed.

NO NOT_APPLICABLE ROUTE. A missing entrypoint, a missing lockfile, a
failed install, a failed build and an unreachable server are all BLOCKING
outcomes with their own finite diagnostic. None of them is an exemption,
and none of them is a judgement that the product is inaccessible either -
they are the absence of a judgement, which holds the gate.

Nothing in this module spawns, binds, reads a file or touches durable
state. It sequences, bounds and adjudicates; the services do the work.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import accessibility_contract as ac
from . import routing, workers

# The governed product entrypoint and lockfile, from the LOCKED
# product/ARCHITECTURE.md ("TypeScript; Next.js + React; ... npm") and
# TASK-001's body ("Next.js TypeScript app ... CI/test scripts"). Named
# here rather than declared per-PR by a builder: a builder that chose how
# it is built would be choosing how it is judged.
PRODUCT_ENTRYPOINT = "package.json"
PRODUCT_LOCKFILE = "package-lock.json"

COMPLETED = "COMPLETED"
FAILED = "FAILED"


@dataclass(frozen=True)
class AttemptBudget:
    """G3's five bounds. `total` must equal the sum of the four phases."""

    install_build: int
    readiness: int
    scan: int
    teardown: int
    total: int

    @classmethod
    def from_timeouts(cls, timeouts: dict) -> "AttemptBudget":
        return cls(
            install_build=int(timeouts["accessibility_install_build_seconds"]),
            readiness=int(timeouts["product_server_readiness_seconds"]),
            scan=int(timeouts["accessibility_hard_seconds"]),
            teardown=int(timeouts["accessibility_teardown_seconds"]),
            total=int(timeouts["accessibility_attempt_seconds"]),
        )

    def phases_sum(self) -> int:
        return self.install_build + self.readiness + self.scan + self.teardown


@dataclass(frozen=True)
class AccessibilityPlan:
    """The immutable facts the execute phase needs, copied out under the
    lock. No reference to the task, PR record or claim leaves T1."""

    task_id: str
    pr: int
    sha: str
    attempt_id: str
    port: int


@dataclass(frozen=True)
class AttemptOutcome:
    """What one attempt produced. Exactly one of verdict or reason."""

    status: str
    verdict: str | None
    reason: str
    checks: list
    port_released: bool
    phase: str
    # Whether THIS attempt ever had a server running on the claimed port.
    # It decides which release rule applies, and the distinction is not
    # cosmetic - see `confirm_release`.
    server_started: bool = False

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "reason": self.reason,
            "checks": list(self.checks),
            "port_released": self.port_released,
            "phase": self.phase,
            "server_started": self.server_started,
        }


def _failed(reason: str, phase: str, *, released: bool, checks=None,
            server_started: bool = False) -> AttemptOutcome:
    return AttemptOutcome(status=FAILED, verdict=None, reason=reason,
                          checks=list(checks or []), port_released=released,
                          phase=phase, server_started=server_started)


def run_attempt(plan: AccessibilityPlan, budget: AttemptBudget, services,
                elapsed) -> AttemptOutcome:
    """Run one automated accessibility attempt. Never raises.

    `elapsed()` returns seconds since the attempt began - injected, so
    every bound in here is testable against a controlled clock.

    `services` must provide:

        exists(relative_path) -> bool
        install_build(bound) -> (ok: bool, reason: str | None)
        start_server(port, bound) -> handle | None
        await_ready(port, bound) -> bool
        scan(port, sha, bound) -> list | None      # the check list
        stop_server(handle, bound) -> None
        listening_ports() -> set[int] | None
    """
    try:
        return _run_phases(plan, budget, services, elapsed, _Box())
    except Exception:  # noqa: BLE001
        # A service that raises is a service that did not finish. C-16:
        # no exception text reaches durable evidence, only a finite token.
        return _failed(ac.SPAWN_OR_RUN_INCOMPLETE, "UNKNOWN", released=False)


class _Box:
    """Carries the server handle out of the phase sequence so teardown can
    find it however that sequence ended - including when it ended by
    raising."""

    def __init__(self):
        self.handle = None


def _remaining(budget: AttemptBudget, elapsed) -> float:
    return budget.total - elapsed()


def _phase_bound(budget: AttemptBudget, elapsed, own: int) -> float:
    """What this phase may have: its own bound, capped by what is left.

    Recomputed from the clock immediately before the phase starts, never
    carried from a plan made earlier - that is what makes the combined
    budget interruptible rather than merely a sum.
    """
    return min(float(own), _remaining(budget, elapsed))


def _run_phases(plan, budget, services, elapsed, box) -> AttemptOutcome:
    try:
        return _sequence(plan, budget, services, elapsed, box)
    finally:
        # G3: cleanup of owned processes on EVERY exit path. A `finally`,
        # not a happy-path step, because the paths that need it most are
        # the ones that failed.
        if box.handle is not None:
            try:
                services.stop_server(
                    box.handle,
                    max(0.0, _phase_bound(budget, elapsed, budget.teardown)))
            except Exception:  # noqa: BLE001 - teardown must not mask a result
                pass


def _sequence(plan, budget, services, elapsed, box) -> AttemptOutcome:
    # ---------------------------------------------------- preflight
    # Missing product files BLOCK. There is no NOT_APPLICABLE route: every
    # task in this product graph requires a runnable app, so an absent
    # entrypoint is a product fault with a finite diagnostic, not an
    # exemption from being checked.
    if not services.exists(PRODUCT_ENTRYPOINT):
        return _failed(ac.PRODUCT_ENTRYPOINT_MISSING, "PREFLIGHT",
                       released=False)
    if not services.exists(PRODUCT_LOCKFILE):
        # npm ci REQUIRES a lockfile, and without one the dependency set
        # would be resolved fresh - a different build from the commit's.
        return _failed(ac.PRODUCT_LOCKFILE_MISSING, "PREFLIGHT",
                       released=False)

    # ---------------------------------------------------- install + build
    bound = _phase_bound(budget, elapsed, budget.install_build)
    if bound <= 0:
        return _failed(ac.TIMED_OUT, "INSTALL_BUILD", released=False)
    ok, reason = services.install_build(bound)
    if not ok:
        return _failed(reason or ac.PRODUCT_BUILD_FAILED, "INSTALL_BUILD",
                       released=False)

    # ---------------------------------------------------- serve
    bound = _phase_bound(budget, elapsed, budget.readiness)
    if bound <= 0:
        return _failed(ac.TIMED_OUT, "SERVE", released=False)
    box.handle = services.start_server(plan.port, bound)
    if box.handle is None:
        return _failed(ac.PRODUCT_SERVER_UNREADY, "SERVE", released=False)

    # ---------------------------------------------------- ready
    bound = _phase_bound(budget, elapsed, budget.readiness)
    if bound <= 0 or not services.await_ready(plan.port, bound):
        return _failed(ac.PRODUCT_SERVER_UNREADY, "READY", released=False,
                       server_started=True)

    # ---------------------------------------------------- scan
    bound = _phase_bound(budget, elapsed, budget.scan)
    if bound <= 0:
        return _failed(ac.TIMED_OUT, "SCAN", released=False,
                       server_started=True)
    checks = services.scan(plan.port, plan.sha, bound)
    if checks is None:
        return _failed(ac.RESULT_MISSING, "SCAN", released=False,
                       server_started=True)

    # The verdict, from the LANDED normaliser. Every check is compared to
    # the attempt's own sha, so a scan of the wrong commit is refused here
    # rather than adjudicated.
    verdict, why = routing.normalize_accessibility_auto(checks, plan.sha)
    if verdict is None:
        return _failed(why, "SCAN", released=False, checks=checks,
                       server_started=True)
    return AttemptOutcome(status=COMPLETED, verdict=verdict, reason="",
                          checks=list(checks), port_released=False,
                          phase="SCAN", server_started=True)


def confirm_release(outcome: AttemptOutcome,
                    claim: dict, services) -> AttemptOutcome:
    """G2's teardown confirmation, applied to a finished attempt.

    Called AFTER `run_attempt` has stopped the server. Observes the port
    and releases the claim only when the listener is genuinely gone.

    A perfect scan whose server cannot be confirmed gone does NOT become a
    pass: the port stays claimed and the attempt reports
    PRODUCT_SERVER_NOT_RELEASED. Releasing on a failed observation is the
    mistake this exists to prevent - it would hand a live socket to a
    Builder.

    AN ATTEMPT THAT NEVER STARTED A SERVER IS THE OTHER CASE, and it is
    not symmetric. A missing entrypoint, a failed build or a server that
    would not start means this attempt never bound the port at all, so
    there is nothing of ours to confirm gone and the claim is released
    unconditionally. Applying the listener rule there would strand the
    port permanently whenever something ELSE happened to be listening on
    it - C-09's reverse-listener detection owns foreign listeners, and a
    Builder probes before binding regardless. G2's guarantee is about not
    handing out a port OUR server may still hold; it is not a general
    lease on the number.
    """
    if not outcome.server_started:
        claim["port_released"] = True
        return AttemptOutcome(status=outcome.status, verdict=outcome.verdict,
                              reason=outcome.reason, checks=outcome.checks,
                              port_released=True, phase=outcome.phase,
                              server_started=False)
    try:
        listening = services.listening_ports()
    except Exception:  # noqa: BLE001
        listening = None
    ok, _why = workers.release_claimed_port(claim, listening)
    if ok:
        return AttemptOutcome(status=outcome.status, verdict=outcome.verdict,
                              reason=outcome.reason, checks=outcome.checks,
                              port_released=True, phase=outcome.phase,
                              server_started=True)
    return _failed(ac.PRODUCT_SERVER_NOT_RELEASED, "TEARDOWN",
                   released=False, checks=outcome.checks, server_started=True)


def findings_for(outcome: AttemptOutcome) -> list[dict]:
    """The severity-policy findings an ACCESSIBILITY_AUTO_FAIL asserts.

    One per failing check, expanded through G6's maps - so a composite
    check contributes one finding per sub-condition that actually fired,
    and an unmapped failure contributes an uncited one that blocks.

    An attempt that produced no verdict contributes NO findings: it made
    no judgement about the product, and inventing one would turn an
    apparatus failure into evidence of inaccessibility.
    """
    if outcome.status != COMPLETED or \
            outcome.verdict != ac.ACCESSIBILITY_AUTO_FAIL:
        return []
    findings: list[dict] = []
    for check in outcome.checks:
        if not isinstance(check, dict) or check.get("result") != "FAIL":
            continue
        check_id = check.get("check_id")
        if not isinstance(check_id, str):
            continue
        findings.extend(
            routing.accessibility_findings_for_check(check_id,
                                                     check.get("detail")))
    return findings
