"""The real services the automated accessibility attempt runs against.

WHAT THIS CLOSES. `accessibility_evidence.run_attempt` is pure sequencing
with every external service injected, and until now NOTHING injected one:
`Supervisor.accessibility_services_factory` was `None`, `route_evidence`
is gated on it being non-None, so the automated half was fully wired and
completely inert. A previous handover recorded this as needing no
decision because "it resolves when a product exists to build". That is
only half true - the plumbing is APPARATUS code, not product code, and
leaving it for the product builder would mean the first product build
arrives with nothing able to check it. This module is that plumbing.

THE SEVEN SERVICES, exactly `run_attempt`'s documented contract:

    exists(relative_path)     -> bool
    install_build(bound)      -> (ok, finite reason | None)
    start_server(port, bound) -> handle | None
    await_ready(port, bound)  -> bool
    scan(port, sha, bound)    -> list | None
    stop_server(handle, bound)-> None
    listening_ports()         -> set[int] | None

G3: A CLEAN ISOLATED BUILD AT THE TRUSTED HEAD, NO ARTEFACT REUSE. The
attempt does not run in the Builder's worktree. That worktree carries the
Builder's `node_modules`, its `.next` output and whatever else the
Builder left behind, so a scan there would be judging artefacts rather
than the commit. Each attempt gets its own detached checkout of exactly
`plan.sha`, built from the lockfile, and removed afterwards.

EVERY EXTERNAL EDGE IS INJECTED, with a real default. Subprocess
execution, process spawning, HTTP readiness and the listener scan are all
parameters. That is what lets the fixture tests exercise the real
sequencing, the real bound arithmetic and the real teardown ordering
without running npm, binding a socket or reading /proc - and it is the
same discipline `run_attempt` itself follows.

NOTHING HERE ADJUDICATES. It runs things and reports finite codes;
`run_attempt` decides what they mean and `routing` decides what the
checks are worth. A service that cannot honour the bound it is given is a
defect in the service - every subprocess below is given an explicit
timeout and killed at it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import accessibility_contract as ac
from . import config, gate_evidence, gh, hostcheck, proc

# The governed product commands. Named here, not taken from the PR: a
# builder that chose how its work is built would be choosing how it is
# judged. `npm ci` (not `install`) is deliberate - it is the command that
# honours the lockfile exactly, which is why the lockfile's absence is a
# blocking preflight failure rather than something to work around.
INSTALL_COMMAND = ("npm", "ci")
BUILD_COMMAND = ("npm", "run", "build")
START_COMMAND = ("npm", "run", "start")

# How the install bound is split. The build is the longer half in a Next.js
# project, and an install that eats the whole budget leaves nothing to
# build with - so the split is explicit rather than first-come.
INSTALL_SHARE = 0.4

# Readiness polling. Short enough that a server that comes up quickly is
# not made to wait, long enough that polling is not itself a load.
READY_POLL_SECONDS = 1.0
READY_REQUEST_TIMEOUT = 5.0


def _run(command, cwd: str, timeout: float, env=None) -> tuple[bool, int]:
    """One bounded subprocess. True only on a clean zero exit.

    Output is deliberately discarded here rather than returned: the
    attempt's durable evidence is written by the scan through
    gate_evidence, and build logs are not part of the accessibility
    verdict. A timeout is a failure, never an exception that escapes.
    """
    try:
        completed = subprocess.run(
            list(command), cwd=cwd, timeout=max(0.0, timeout),
            capture_output=True, text=True, check=False, env=env)
        return completed.returncode == 0, completed.returncode
    except subprocess.TimeoutExpired:
        return False, 124
    except OSError:
        return False, 127


def _spawn(command, cwd: str, env) -> subprocess.Popen | None:
    """Start the product server in its OWN process group.

    start_new_session matters more than it looks. `npm run start` is a
    shell wrapper that execs Next, which spawns workers of its own.
    Killing only the npm process would leave the real listener holding the
    claimed port, and G2 then refuses to release that port forever -
    correctly, because something really is still bound to it. The group is
    what makes teardown able to finish.
    """
    try:
        return subprocess.Popen(
            list(command), cwd=cwd, env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _http_ok(url: str, timeout: float) -> bool:
    """Whether the product server answered at all.

    Any HTTP response means a server is listening and serving, which is
    all readiness claims. A 404 from a route that does not exist yet is
    still a running server; judging the content is the scan's job.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 500
    except Exception:  # noqa: BLE001 - not ready is not an error here
        return False


def _listening() -> set[int] | None:
    lo, hi = hostcheck.read_candidate_port_range()
    return proc.listening_ports(lo, hi)


def _with_details(checks: list, details) -> list:
    """Join run.js's two halves back together, by check_id.

    FOUND BY RUNNING IT, NOT BY READING IT. run.js writes
    `{checks: [...], details: {CHECK_ID: {...}}}` - the results and their
    diagnostics in SEPARATE top-level fields. `findings_for` reads
    `check.get("detail")`, which no check has ever carried, so
    `accessibility_check_signals` was handed None on every real run and
    returned no signals every time.

    The consequence was not a hole, it was a DOWNGRADE. G6's approved
    composite sub-condition structure - a keyboard trap citing
    ACC-DOD-KEYBOARD_OPERATION, an unnamed control citing
    ACC-DOD-MEANINGFUL_LABELS, two of them at once from one check - could
    never fire. Every composite failure collapsed to one uncited finding,
    which severity.py rates INVALID with merge_blocked True. Still
    blocking, so nothing merged that should not have; but the reviewer was
    told "no requirement cited" about a failure whose requirement the
    repository already knows.

    The join is done HERE, at the one seam where run.js's output becomes
    the control plane's, rather than by changing run.js's schema-shaped
    output or by threading a second map through run_attempt and the
    AttemptOutcome.

    Mutation is deliberate and local: these dicts were parsed from this
    attempt's own result.json microseconds ago and are owned by nothing
    else. A check whose detail is missing is left exactly as it was -
    absent detail must stay absent, because an invented empty one would
    read as "this check reported no sub-condition" rather than "this check
    reported nothing we could read".
    """
    if not isinstance(details, dict):
        return checks
    for check in checks:
        if not isinstance(check, dict):
            continue
        detail = details.get(check.get("check_id"))
        if detail is not None:
            check["detail"] = detail
    return checks


@dataclass
class _Handle:
    """What start_server hands back. Carries the process group, because
    that is what teardown has to kill."""
    process: subprocess.Popen
    pgid: int | None


class ProductServices:
    """One attempt's services, bound to one isolated checkout."""

    def __init__(self, checkout: Path, ctx, *, runner=_run, spawner=_spawn,
                 http_ok=_http_ok, listener=_listening, sleep=time.sleep,
                 monotonic=time.monotonic):
        self.checkout = Path(checkout)
        self.ctx = ctx
        self._run = runner
        self._spawn = spawner
        self._http_ok = http_ok
        self._listener = listener
        self._sleep = sleep
        self._monotonic = monotonic

    # ------------------------------------------------------- preflight

    def exists(self, relative_path: str) -> bool:
        return (self.checkout / relative_path).exists()

    # --------------------------------------------------- install+build

    def install_build(self, bound: float) -> tuple[bool, str | None]:
        """npm ci, then npm run build, inside one shared bound.

        Returns the FINITE diagnostic that distinguishes them, because
        "the dependencies would not install" and "the code would not
        compile" send a task to very different places.
        """
        install_bound = bound * INSTALL_SHARE
        started = self._monotonic()
        ok, _ = self._run(INSTALL_COMMAND, str(self.checkout), install_bound)
        if not ok:
            return False, ac.PRODUCT_INSTALL_FAILED
        # What is LEFT, read from the clock - not the other share. An
        # install that finished early hands its remainder to the build.
        remaining = bound - (self._monotonic() - started)
        if remaining <= 0:
            return False, ac.TIMED_OUT
        ok, _ = self._run(BUILD_COMMAND, str(self.checkout), remaining)
        if not ok:
            return False, ac.PRODUCT_BUILD_FAILED
        return True, None

    # ------------------------------------------------------------ serve

    def start_server(self, port: int, bound: float):
        env = dict(os.environ)
        # PORT rather than an argv flag: every Node server framework in
        # this stack honours it, and it does not depend on the shape of
        # the product's own "start" script, which the apparatus does not
        # own and must not assume.
        env["PORT"] = str(int(port))
        env["NODE_ENV"] = "production"
        process = self._spawn(START_COMMAND, str(self.checkout), env)
        if process is None:
            return None
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = None
        return _Handle(process=process, pgid=pgid)

    def await_ready(self, port: int, bound: float) -> bool:
        url = f"http://127.0.0.1:{int(port)}/"
        deadline = self._monotonic() + max(0.0, bound)
        while self._monotonic() < deadline:
            if self._http_ok(url, min(READY_REQUEST_TIMEOUT,
                                      max(0.1, deadline - self._monotonic()))):
                return True
            self._sleep(READY_POLL_SECONDS)
        return False

    # ------------------------------------------------------------- scan

    def scan(self, port: int, sha: str, bound: float):
        """Run the landed apparatus against the live server.

        Returns the check list from the attempt's own result.json, or None
        - which `run_attempt` turns into RESULT_MISSING. The command and
        every path come from gate_evidence, so the scan writes its
        evidence exactly where C-05.1 persists it.
        """
        command = gate_evidence.node_command(self.ctx)
        ok, _ = self._run(command, str(config.REPO_ROOT), bound)
        try:
            payload = json.loads(
                Path(self.ctx.result_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        checks = payload.get("checks")
        # A non-list is not "no checks" - it is an unusable result, and
        # reading it as empty is how a broken scan becomes a clean pass.
        if not isinstance(checks, list):
            return None
        return _with_details(checks, payload.get("details"))

    # --------------------------------------------------------- teardown

    def stop_server(self, handle, bound: float) -> None:
        """Stop the whole process group, then make sure it is gone.

        SIGTERM to the group, wait up to the bound, then SIGKILL. The kill
        is not optional politeness: `confirm_release` refuses to return
        the port while anything is still listening, so a server that
        ignored SIGTERM would hold a governed port for the rest of the
        run.
        """
        if handle is None:
            return
        process = getattr(handle, "process", None)
        pgid = getattr(handle, "pgid", None)
        self._signal(process, pgid, signal.SIGTERM)
        deadline = self._monotonic() + max(0.0, bound)
        while self._monotonic() < deadline:
            if process is None or process.poll() is not None:
                break
            self._sleep(0.1)
        if process is not None and process.poll() is None:
            self._signal(process, pgid, signal.SIGKILL)

    @staticmethod
    def _signal(process, pgid, sig) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            elif process is not None:
                process.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass          # already gone is the outcome we wanted

    def listening_ports(self):
        return self._listener()

    # ---------------------------------------------------------- disposal

    def dispose(self) -> None:
        """Remove the isolated checkout. Never raises.

        Called after the attempt has committed, not inside run_attempt:
        the checkout must outlive the scan, and a failure to remove it is
        disk to reclaim, never a reason to lose a verdict that is already
        durable.
        """
        if self.checkout is None or not self.checkout.exists():
            return
        gh.git(["worktree", "remove", "--force", str(self.checkout)],
               str(config.REPO_ROOT))
        if self.checkout.exists():
            shutil.rmtree(self.checkout, ignore_errors=True)


def isolated_checkout(sha: str, attempt_dir: Path) -> Path | None:
    """A detached worktree at exactly `sha`, inside this attempt.

    `--detach` because the attempt judges a COMMIT, not a branch: a branch
    that moves under the scan would mean the evidence named one head and
    described another. Returns None rather than raising - a checkout that
    cannot be made is a blocking preflight failure, which the caller
    reports through the ordinary finite diagnostics.
    """
    target = Path(attempt_dir) / "checkout"
    result = gh.git(["worktree", "add", "--detach", str(target), sha],
                    str(config.REPO_ROOT))
    if not result.ok or not target.exists():
        return None
    return target


def factory(plan, *, allocate=gate_evidence.allocate_attempt,
            checkout=isolated_checkout, services=ProductServices):
    """Build the services for one planned attempt.

    This is what `Supervisor.accessibility_services_factory` is set to.
    It runs in Phase C, outside every transaction, which is why it may
    create directories and run git at all.

    A failed checkout still returns a usable services object - one whose
    `exists` is False for everything, so `run_attempt` reports
    PRODUCT_ENTRYPOINT_MISSING through its ordinary preflight path. The
    alternative, raising, would be caught by run_attempt's blanket handler
    and collapse to SPAWN_OR_RUN_INCOMPLETE, losing the distinction
    between "the product has no entrypoint" and "the apparatus fell over".
    """
    attempt_dir = allocate(plan.task_id, plan.sha)
    ctx = gate_evidence.context(
        attempt_dir, plan.sha, f"http://127.0.0.1:{int(plan.port)}/")
    tree = checkout(plan.sha, attempt_dir)
    # A non-existent path makes every `exists` check False, which is
    # exactly the preflight answer a missing checkout deserves.
    return services(tree if tree is not None else attempt_dir / "checkout",
                    ctx)
