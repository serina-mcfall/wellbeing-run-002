"""C-24 — per-role `gh` authentication and installation-token renewal.

Approval package §9 action 6b: `control/gh.py` ran `subprocess.run(args,
cwd=cwd, ...)` with no `env=`, so every `gh` call inherited ambient host
auth. The package states the consequence plainly — creating three Apps and
installing them changes nothing about who `gh` acts as, so §1's "the
publisher cannot merge, the merger cannot publish" was a property of the
manifests and not of the running system.

WHAT THESE TESTS PROVE, AND THE LIMIT OF IT.

  COMPONENT IMPLEMENTED, and CONNECTED PATH VERIFIED WITH SIMULATED
  SERVICES: a role-bound invocation really does start a real child process
  with a real, allow-listed environment, and that child really does see one
  token and no other. The child is `sys.executable`, not `gh`; the token is
  a literal this file authored; the minter is a function this file wrote.

  NOTHING HERE IS A DEPLOYED PATH VERIFIED AGAINST GITHUB. No App exists,
  no installation exists, no network call is made, and no real credential
  is read. That `gh` honours `GH_TOKEN` over a configured host credential
  is GitHub's documented behaviour and is NOT asserted here — it is a
  V-step in `experiment/github-app/DEPLOYMENT-PROCEDURE.md`.

NO REAL CREDENTIAL APPEARS IN THIS FILE OR IN ANYTHING IT ASSERTS. The
simulated values are obviously-not-tokens, and where a test must establish
that the right one arrived, the CHILD compares and prints a verdict so the
value itself never reaches an assertion or an output stream.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import gate_invoker, gh  # noqa: E402

# Simulated, and shaped so that nothing could mistake one for a credential.
# `scripts/check_no_secrets.py` matches `ghs_`-prefixed material of real
# length; these deliberately look like what they are.
GATE_TOKEN = "simulated-gate-not-a-token"
SUPERVISOR_TOKEN = "simulated-supervisor-not-a-token"
WORKER_TOKEN = "simulated-worker-not-a-token"
AMBIENT_TOKEN = "simulated-ambient-host-not-a-token"

# A child that reports whether a named variable reached it. It prints the
# NAME's presence or a MATCH verdict — never a value.
PRINT_VAR = (
    "import os,sys;"
    "sys.stdout.write(os.environ.get(sys.argv[1],'<unset>'))"
)
MATCH_VAR = (
    "import os,sys;"
    "sys.stdout.write('MATCH' if os.environ.get(sys.argv[1])==sys.argv[2]"
    " else 'MISMATCH')"
)
TOUCH = "import sys;open(sys.argv[1],'w').write('ran')"


def iso(delta_seconds: int) -> str:
    """An RFC 3339 instant `delta_seconds` from now, as GitHub spells it."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env_for(role: str, token: str, expires_in: int = 3600, **extra) -> dict:
    env = {gh.ROLE_TOKEN_ENV[role]: token,
           gh.ROLE_EXPIRY_ENV[role]: iso(expires_in)}
    env.update(extra)
    return env


# ------------------------------------------------- the default is unchanged


class AmbientRemainsTheDefaultCase(unittest.TestCase):
    """No caller changes meaning unless it opts in.

    Every existing call site in `control/routing.py`,
    `control/supervisor.py` and the CLI passes no role. If adding the
    parameter altered their behaviour at all, action 6b would be a
    behavioural change to the whole control plane rather than an opt-in,
    and it would be one nobody could verify before T+00.
    """

    def test_no_role_inherits_the_parent_environment(self):
        with mock.patch.dict(os.environ, {"RUN002_C24_SENTINEL": "ambient"}):
            result = gh.run([sys.executable, "-c", PRINT_VAR,
                             "RUN002_C24_SENTINEL"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "ambient")

    def test_no_role_still_passes_an_ambient_gh_token_through(self):
        """Stated as a fact rather than implied, because it is the status quo.

        Ambient auth IS what an un-roled call uses. The arrangement does
        not become safe by this parameter existing; it becomes safe when
        the call sites adopt it.
        """
        with mock.patch.dict(os.environ, {"GH_TOKEN": AMBIENT_TOKEN}):
            result = gh.run([sys.executable, "-c", MATCH_VAR,
                             "GH_TOKEN", AMBIENT_TOKEN])
        self.assertEqual(result.stdout, "MATCH")


# ------------------------------------------------------- the allow-list

class RoleChildEnvironmentCase(unittest.TestCase):
    """A role-bound child sees an allow-list, not an inheritance."""

    def test_the_base_environment_carries_no_credential_at_all(self):
        """The only env-shaped thing this module hands back is token-free.

        This is what makes the allow-list assertable without a test ever
        holding a credential: `role_base_env` cannot return one, by
        construction, for any input.
        """
        parent = env_for(gh.GATE, GATE_TOKEN, GH_TOKEN=AMBIENT_TOKEN,
                         GITHUB_TOKEN=AMBIENT_TOKEN, PATH="/usr/bin")
        base = gh.role_base_env(parent)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", *gh.ROLE_TOKEN_ENV.values()):
            self.assertNotIn(name, base)
        self.assertEqual(base, {"PATH": "/usr/bin"})

    def test_a_name_outside_the_allow_list_does_not_reach_the_child(self):
        parent = env_for(gh.GATE, GATE_TOKEN,
                         RUN002_C24_SENTINEL="must-not-arrive")
        result = gh.run([sys.executable, "-c", PRINT_VAR,
                         "RUN002_C24_SENTINEL"],
                        role=gh.GATE, environ=parent)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "<unset>")

    def test_the_allow_list_omits_every_name_that_redirects_a_call(self):
        """Absent BY CONSTRUCTION, which is the property that matters.

        `GH_TOKEN` and `GITHUB_TOKEN` choose the identity; `GH_HOST` and
        `GH_REPO` choose the host and the repository. A deny-list would
        have to remember each one; an allow-list drops the name invented
        tomorrow without anyone noticing it needed dropping.
        """
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_REPO",
                     "GH_ENTERPRISE_TOKEN", "NODE_OPTIONS"):
            self.assertNotIn(name, gh.GH_ENV_ALLOWED)

    def test_each_role_hands_the_child_its_own_token(self):
        """The separation, observed in a real child process.

        Three principals, three variables, one `GH_TOKEN` per invocation.
        The child decides MATCH or MISMATCH so no value is ever asserted
        on or printed into a transcript.
        """
        for role, token in ((gh.GATE, GATE_TOKEN),
                            (gh.SUPERVISOR, SUPERVISOR_TOKEN),
                            (gh.WORKER, WORKER_TOKEN)):
            with self.subTest(role=role):
                result = gh.run([sys.executable, "-c", MATCH_VAR,
                                 "GH_TOKEN", token],
                                role=role, environ=env_for(role, token))
                self.assertEqual(result.stdout, "MATCH")

    def test_a_worker_does_not_inherit_the_supervisors_ambient_token(self):
        """Action 6b's whole point, in one assertion.

        The parent holds the ambient host credential — which in the
        deployed arrangement is the identity that can merge AND write a
        commit status. A worker-roled call must see its own weaker token
        and nothing else.
        """
        parent = env_for(gh.WORKER, WORKER_TOKEN,
                         GH_TOKEN=AMBIENT_TOKEN,
                         GITHUB_TOKEN=AMBIENT_TOKEN)
        result = gh.run([sys.executable, "-c", MATCH_VAR,
                         "GH_TOKEN", AMBIENT_TOKEN],
                        role=gh.WORKER, environ=parent)
        self.assertEqual(result.stdout, "MISMATCH")

        result = gh.run([sys.executable, "-c", MATCH_VAR,
                         "GITHUB_TOKEN", AMBIENT_TOKEN],
                        role=gh.WORKER, environ=parent)
        self.assertEqual(result.stdout, "MISMATCH")


# --------------------------------------------------------- it fails closed

class RoleAuthFailsClosedCase(unittest.TestCase):
    """A role that cannot authenticate runs NOTHING. It does not fall back."""

    def _assert_never_ran(self, role, parent, expected_reason):
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "it-ran"
            result = gh.run([sys.executable, "-c", TOUCH, str(marker)],
                            role=role, environ=parent)
            self.assertFalse(marker.exists(),
                             "a role that could not authenticate still "
                             "started a process")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, gh.AUTH_UNAVAILABLE)
        self.assertIn(expected_reason, result.stderr)
        return result

    def test_an_absent_token_refuses_rather_than_using_ambient_auth(self):
        """The one that would have been easiest to get wrong.

        Falling back to the host credential when a role's token is missing
        hands the weakest caller the strongest identity — the exact hole
        action 6b exists to close — and it would look like success.
        """
        parent = {"GH_TOKEN": AMBIENT_TOKEN}
        self._assert_never_ran(gh.WORKER, parent, gh.ROLE_TOKEN_ABSENT)

    def test_an_unknown_role_refuses(self):
        self._assert_never_ran("admin", env_for(gh.GATE, GATE_TOKEN),
                               gh.ROLE_UNKNOWN)

    def test_an_expired_token_refuses_before_the_call_is_made(self):
        parent = env_for(gh.SUPERVISOR, SUPERVISOR_TOKEN, expires_in=-1)
        self._assert_never_ran(gh.SUPERVISOR, parent, gh.ROLE_TOKEN_EXPIRED)

    def test_a_token_with_no_recorded_expiry_refuses(self):
        """Refused rather than trusted, and that is a decision.

        An installation token lives one hour; the run lives twenty-four.
        A token nobody tracks the expiry of is one that WILL die mid-run
        and start returning 401s that read like permission faults — the
        hardest failure in this arrangement to diagnose. Refusing makes
        `ensure_token`, which always records an expiry, the only way to
        supply one.
        """
        parent = {gh.ROLE_TOKEN_ENV[gh.GATE]: GATE_TOKEN}
        self._assert_never_ran(gh.GATE, parent, gh.EXPIRY_UNRECORDED)

    def test_a_naive_expiry_is_unusable_rather_than_assumed_utc(self):
        """`2026-10-02T12:00:00` names no instant without a zone.

        Guessing UTC on a host offset by hours invents or discards hours
        of validity, and the direction of the error is unknowable.
        """
        parent = {gh.ROLE_TOKEN_ENV[gh.GATE]: GATE_TOKEN,
                  gh.ROLE_EXPIRY_ENV[gh.GATE]: "2099-01-01T00:00:00"}
        self._assert_never_ran(gh.GATE, parent, gh.EXPIRY_UNRECORDED)

    def test_the_refusal_names_the_reason_and_the_role_and_nothing_else(self):
        parent = env_for(gh.GATE, GATE_TOKEN, expires_in=-60)
        # Deliberately NOT a `gh` argv. Nothing in this suite may name a
        # real GitHub invocation, even one that is refused before it runs.
        result = gh.run([sys.executable, "-c", "pass"], role=gh.GATE,
                        environ=parent)
        self.assertNotIn(GATE_TOKEN, result.stderr)
        self.assertNotIn(GATE_TOKEN, result.stdout)
        self.assertEqual(result.stderr,
                         f"{gh.ROLE_TOKEN_EXPIRED}: role={gh.GATE}")

    def test_clock_skew_refuses_early_rather_than_late(self):
        """A token inside the skew window is already treated as dead.

        Erring late means a call goes out on a credential GitHub has
        already retired, and the resulting 401 is indistinguishable from a
        missing permission.
        """
        inside = gh.CLOCK_SKEW_SECONDS - 5
        usable, reason = gh.token_usable(
            gh.GATE, env_for(gh.GATE, GATE_TOKEN, expires_in=inside))
        self.assertFalse(usable)
        self.assertEqual(reason, gh.ROLE_TOKEN_EXPIRED)

        outside = gh.CLOCK_SKEW_SECONDS + 60
        usable, _ = gh.token_usable(
            gh.GATE, env_for(gh.GATE, GATE_TOKEN, expires_in=outside))
        self.assertTrue(usable)


# ---------------------------------------------------------------- renewal

class Minter:
    """A simulated installation-token minter. NOTHING REAL MINTS ANYTHING.

    Shaped like GitHub's own answer to
    `POST /app/installations/{id}/access_tokens`:
    `{"token": ..., "expires_at": ...}`.

    `delay` widens the window the minting call occupies. IT IS NOT
    DECORATION. With an instant minter the concurrency test below passed
    even against a build whose freshness re-check had been moved OUTSIDE
    the lock — measured, not assumed — because the first thread finished
    the whole renewal before the others reached their check. A race that
    only loses when the machine is slow is a race the suite does not test.
    """

    def __init__(self, lifetime: int = 3600, token: str = "simulated-renewed",
                 delay: float = 0.0):
        self.lifetime = lifetime
        self.token = token
        self.delay = delay
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, role):
        with self.lock:
            self.calls.append(role)
            sequence = len(self.calls)
        if self.delay:
            time.sleep(self.delay)
        return {"token": f"{self.token}-{sequence}",
                "expires_at": iso(self.lifetime)}


class TokenRenewalCase(unittest.TestCase):

    def test_a_fresh_token_is_not_renewed(self):
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=3600)
        minter = Minter()
        result = gh.ensure_token(gh.GATE, mint=minter, environ=env)
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, gh.TOKEN_FRESH)
        self.assertEqual(minter.calls, [])
        self.assertEqual(env[gh.ROLE_TOKEN_ENV[gh.GATE]], GATE_TOKEN)

    def test_a_token_inside_the_renewal_margin_is_replaced(self):
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=60)
        minter = Minter()
        result = gh.ensure_token(gh.GATE, mint=minter, environ=env)
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, gh.TOKEN_RENEWED)
        self.assertEqual(minter.calls, [gh.GATE])
        self.assertNotEqual(env[gh.ROLE_TOKEN_ENV[gh.GATE]], GATE_TOKEN)
        self.assertEqual(env[gh.ROLE_EXPIRY_ENV[gh.GATE]], result.expires_at)

    def test_the_renewed_token_is_immediately_usable_by_a_role_call(self):
        """The join: renewal writes where `run` reads. One place, by name."""
        env = env_for(gh.SUPERVISOR, SUPERVISOR_TOKEN, expires_in=-1)
        self.assertFalse(gh.token_usable(gh.SUPERVISOR, env)[0])
        gh.ensure_token(gh.SUPERVISOR, mint=Minter(), environ=env)
        usable, reason = gh.token_usable(gh.SUPERVISOR, env)
        self.assertTrue(usable, reason)

    def test_a_renewal_that_fails_leaves_the_old_credential_intact(self):
        """A failed mint must not destroy a token that still has life in it.

        The old one may have minutes left; discarding it turns a
        recoverable network blip into an outage the next attempt cannot
        recover from either.
        """
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=120)
        before = dict(env)

        def explode(role):
            raise RuntimeError("simulated mint failure")

        result = gh.ensure_token(gh.GATE, mint=explode, environ=env)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, gh.MINT_FAILED)
        self.assertEqual(env, before)

    def test_a_malformed_mint_is_refused_and_changes_nothing(self):
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=120)
        before = dict(env)
        for answer in (None, "a-bare-string", {}, {"token": ""},
                       {"token": "x"}, {"token": "x", "expires_at": "soon"},
                       {"token": "x", "expires_at": "2099-01-01T00:00:00"},
                       ("x",), ("x", "y", "z"), {"expires_at": iso(3600)}):
            with self.subTest(answer=answer):
                result = gh.ensure_token(gh.GATE, mint=lambda _r, a=answer: a,
                                         environ=env)
                self.assertFalse(result.ok)
                self.assertEqual(result.outcome, gh.MINT_MALFORMED)
                self.assertEqual(env, before)

    def test_a_token_that_arrives_already_near_death_is_refused(self):
        """Installing it guarantees another renewal on the next call.

        It is also the shape a clock-skewed host produces, so accepting it
        would turn a clock problem into a mint storm.
        """
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=120)
        before = dict(env)
        minter = Minter(lifetime=gh.RENEWAL_MARGIN_SECONDS)
        result = gh.ensure_token(gh.GATE, mint=minter, environ=env)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, gh.MINT_TOO_SHORT)
        self.assertEqual(env, before)

    def test_a_pair_is_accepted_as_well_as_githubs_own_shape(self):
        env = env_for(gh.WORKER, WORKER_TOKEN, expires_in=0)
        result = gh.ensure_token(
            gh.WORKER, mint=lambda _r: ("simulated-pair", iso(3600)),
            environ=env)
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, gh.TOKEN_RENEWED)

    def test_an_unknown_role_mints_nothing(self):
        minter = Minter()
        result = gh.ensure_token("admin", mint=minter, environ={})
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, gh.ROLE_UNKNOWN)
        self.assertEqual(minter.calls, [])

    def test_concurrent_renewal_mints_exactly_once(self):
        """Eight threads, one role, one mint.

        Two live tokens for one installation is not merely wasteful: GitHub
        invalidates neither, so which one is in the environment depends on
        which write landed last.
        """
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=-1)
        # The delay is what makes this test able to fail. See `Minter`.
        minter = Minter(delay=0.05)
        start = threading.Barrier(8)
        outcomes: list[str] = []
        guard = threading.Lock()

        def renew():
            start.wait()
            result = gh.ensure_token(gh.GATE, mint=minter, environ=env)
            with guard:
                outcomes.append(result.outcome)

        threads = [threading.Thread(target=renew) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(minter.calls), 1,
                         f"minted {len(minter.calls)} times for one role")
        self.assertEqual(outcomes.count(gh.TOKEN_RENEWED), 1)
        self.assertEqual(outcomes.count(gh.TOKEN_FRESH), 7)

    def test_the_minting_edge_is_required_and_undefaulted(self):
        """Nothing in this repository can mint a token.

        The same discipline the injected `poster` and `http` edges use: a
        default is how "we never called it" stops being true.
        """
        with self.assertRaises(TypeError):
            gh.ensure_token(gh.GATE)          # noqa: PLE1120 - that is the point
        # Imports and clients, not prose. The module's docstrings DO name
        # GitHub's token endpoint, because documenting the shape the
        # injected minter must return is the point of the contract; a
        # check that treated a sentence as a call would be the same false
        # positive this repository has already been bitten by twice.
        source = Path(gh.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "httpx", "socket", "cryptography",
                          "import requests"):
            self.assertNotIn(forbidden, source,
                             f"gh.py reaches for {forbidden} — the minting "
                             "edge must stay outside this repository")


class NoCredentialEscapesCase(unittest.TestCase):
    """The value is read at the call and dropped. It is never handed back."""

    def test_a_renewal_has_nowhere_to_put_a_token(self):
        fields = set(gh.Renewal.__dataclass_fields__)
        self.assertEqual(fields, {"ok", "outcome", "expires_at"})

    def test_a_renewal_repr_cannot_carry_the_minted_value(self):
        env = env_for(gh.GATE, GATE_TOKEN, expires_in=-1)
        minted = "simulated-value-that-must-not-escape"
        result = gh.ensure_token(
            gh.GATE, mint=lambda _r: {"token": minted, "expires_at": iso(3600)},
            environ=env)
        self.assertTrue(result.ok)
        self.assertNotIn(minted, repr(result))
        self.assertNotIn(minted, str(result))
        # It did land in the environment, which is the one place it may be.
        self.assertEqual(env[gh.ROLE_TOKEN_ENV[gh.GATE]], minted)

    def test_presence_is_a_boolean_and_nothing_richer(self):
        self.assertIs(gh.role_credential_present(
            gh.GATE, {gh.ROLE_TOKEN_ENV[gh.GATE]: GATE_TOKEN}), True)
        self.assertIs(gh.role_credential_present(gh.GATE, {}), False)
        self.assertIs(gh.role_credential_present("admin", {}), False)


# ------------------------------------------ the trusted export, from the host

class DeploymentResolutionCase(unittest.TestCase):
    """Where the export is and which commit it must be — resolved, not guessed."""

    PIN = "b2df44fb82a4b83fd5a3a4277cd6a69863e0019f"

    def live_root(self, tmp, workspace=None):
        root = Path(tmp) / "checkout"
        (root / "config").mkdir(parents=True)
        (root / "config" / "isolation.json").write_text(json.dumps(
            {"experiment_id": "run-002",
             "workspace": str(root.resolve() if workspace is None else workspace)}),
            encoding="utf-8")
        return root

    def test_the_happy_path_returns_exactly_the_three_gate_arguments(self):
        with TemporaryDirectory() as tmp:
            root = self.live_root(tmp)
            found, reason = gate_invoker.deployment_from_env(
                {gate_invoker.EXPORT_PATH_ENV: "/opt/run-002/gate-x",
                 gate_invoker.EXPORT_SHA_ENV: self.PIN},
                repo_root=root)
        self.assertEqual(reason, gate_invoker.DEPLOY_OK)
        self.assertEqual(found.expected_revision, self.PIN)
        self.assertEqual(found.live_repo_root, str(root.resolve()))
        self.assertEqual(set(gate_invoker.Deployment.__dataclass_fields__),
                         {"export_root", "expected_revision",
                          "live_repo_root"})

    def test_an_unset_or_relative_export_path_refuses(self):
        with TemporaryDirectory() as tmp:
            root = self.live_root(tmp)
            for value in (None, "", "   ", "opt/run-002/gate-x", "./gate"):
                with self.subTest(value=value):
                    env = {gate_invoker.EXPORT_SHA_ENV: self.PIN}
                    if value is not None:
                        env[gate_invoker.EXPORT_PATH_ENV] = value
                    found, reason = gate_invoker.deployment_from_env(
                        env, repo_root=root)
                    self.assertIsNone(found)
                    self.assertEqual(reason,
                                     gate_invoker.DEPLOY_EXPORT_UNSET)

    def test_a_pin_that_is_not_forty_hex_refuses(self):
        with TemporaryDirectory() as tmp:
            root = self.live_root(tmp)
            for value in (None, "", "HEAD", "wip/c05-1-persistence",
                          "b2df44f", self.PIN.upper(), self.PIN + "a"):
                with self.subTest(value=value):
                    env = {gate_invoker.EXPORT_PATH_ENV: "/opt/run-002/g"}
                    if value is not None:
                        env[gate_invoker.EXPORT_SHA_ENV] = value
                    found, reason = gate_invoker.deployment_from_env(
                        env, repo_root=root)
                    self.assertIsNone(found)
                    self.assertEqual(reason, gate_invoker.DEPLOY_PIN_UNSET)

    def test_an_unreadable_isolation_config_refuses(self):
        with TemporaryDirectory() as tmp:
            bare = Path(tmp) / "no-config"
            bare.mkdir()
            found, reason = gate_invoker.deployment_from_env(
                {gate_invoker.EXPORT_PATH_ENV: "/opt/run-002/g",
                 gate_invoker.EXPORT_SHA_ENV: self.PIN},
                repo_root=bare)
        self.assertIsNone(found)
        self.assertEqual(reason, gate_invoker.DEPLOY_ISOLATION_UNREADABLE)

    def test_a_checkout_isolation_json_does_not_name_refuses(self):
        """The invariant THE INVOKER TRAP forced the gate program to drop.

        `git-head.js`'s Run-002 wrapper refuses unless its own location
        equals `isolation.json`'s `workspace`; from the read-only export
        those can never be equal, so the gate program calls the low-level
        form instead and the comparison goes unmade. Re-making it here —
        against the LIVE checkout, where it is both true and checkable —
        is what stops the gate resolving a worktree register and a runtime
        directory belonging to some other workspace.
        """
        with TemporaryDirectory() as tmp:
            root = self.live_root(tmp, workspace="/somewhere/else/entirely")
            found, reason = gate_invoker.deployment_from_env(
                {gate_invoker.EXPORT_PATH_ENV: "/opt/run-002/g",
                 gate_invoker.EXPORT_SHA_ENV: self.PIN},
                repo_root=root)
        self.assertIsNone(found)
        self.assertEqual(reason, gate_invoker.DEPLOY_WORKSPACE_MISMATCH)

    def test_the_names_are_the_ones_the_approval_package_gives_them(self):
        self.assertEqual(gate_invoker.EXPORT_PATH_ENV,
                         "RUN002_TRUSTED_GATE_PATH")
        self.assertEqual(gate_invoker.EXPORT_SHA_ENV,
                         "RUN002_TRUSTED_GATE_SHA")

    def test_resolution_carries_no_credential_and_no_switch(self):
        """Resolving where the code is says nothing about whether to publish."""
        source = Path(gate_invoker.__file__).read_text(encoding="utf-8")
        for name in gh.ROLE_TOKEN_ENV.values():
            self.assertNotIn(name, source)
        self.assertNotIn("RUN_002_PUBLISH_INDEPENDENT_REVIEW", source)


if __name__ == "__main__":
    unittest.main()
