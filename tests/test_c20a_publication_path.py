"""The publication path: gate invoker -> publisher, built and disabled.

SIMULATED EXTERNAL SERVICES ONLY. Three edges exist in this chain and all
three are simulated here:

  * the STATUS ENDPOINT is a local function that records its arguments;
  * the GATE PROCESS is either an injected runner, or - in
    `ConnectedPathCase` - a real `node` run against a throwaway export
    this test file writes itself, in a temporary directory, printing a
    decision this test file composed;
  * the TRUSTED EXPORT is a directory tree built by `_FakeExport` with the
    `.git` layout `git worktree add --detach` produces.

No GitHub call, no `gh`, no token, no App, no network. The publisher has
no default transport and the publication path has no default export, so a
test that forgot to inject one fails with a TypeError rather than silently
reaching out.

WHAT EACH CLASS IS FOR
  DisabledByDefaultCase      the switch is off, and off means no effect
  ExportTrustCase            the gate runs the pinned code or does not run
  GateFailureModeCase        every way it can fail, each distinct
  FlagIsPassedThroughCase    the F5 flag is never re-derived downstream
  ConnectedPathCase          the joined path, end to end, real subprocess
  NothingReachesPublicationCase  the repo-wide unreachability guarantee
  LiveRootCase               the checkout git facts come from, policed
  InvokerTrapCase            the real adapters, run from somewhere else
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control import gate_invoker, publisher


def _load_publication_path():
    """Load the join module from its explicit path.

    It lives in `experiment/github-app/`, whose name contains a hyphen, so
    there is no importable package path to it. That is deliberate (see the
    module docstring) and this helper is the whole cost of it.
    """
    path = ROOT / "experiment" / "github-app" / "publication_path.py"
    spec = importlib.util.spec_from_file_location(
        "run002_publication_path", path)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution: `@dataclass` resolves annotations
    # through `sys.modules[cls.__module__]`, which is not yet populated
    # during `exec_module` otherwise. The standard importlib recipe.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publication_path = _load_publication_path()

LIVE = str(ROOT)

HEAD = "a" * 40
OTHER = "b" * 40
PIN = "c" * 40

ON = {publisher.ENABLE_ENV: "1", publisher.TOKEN_ENV: "simulated-not-a-token"}

REQUEST = {"identity": {"kind": "branch", "ref": "run-002/t"}, "prNumber": 1}


class Recorder:
    """A simulated status endpoint. Records; never reaches anything."""

    def __init__(self, ok=True):
        self.calls: list[tuple] = []
        self.ok = ok

    def __call__(self, context, state, sha):
        self.calls.append((context, state, sha))
        return self.ok


class FakeRunner:
    """A simulated gate process. Records; never spawns anything."""

    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result

    def __call__(self, argv, stdin_text, timeout):
        self.calls.append((argv, stdin_text, timeout))
        return self.result


def decision(verdict="ELIGIBLE", head=HEAD, **over):
    """A gate decision in live-gate.js's exact return shape."""
    value = {"decision": verdict, "trustedHeadSha": head,
             "blockedOnlyByPendingIndependentReview": False,
             "blockedOnlyByDraft": False, "reasons": [],
             "adapters": {}}
    value.update(over)
    return value


def ok_run(value=None):
    """A runner result carrying one JSON decision on stdout, exit 0."""
    body = decision() if value is None else value
    return gate_invoker.RunResult(code=0, stdout=json.dumps(body))


class _FakeExport:
    """A throwaway trusted export, in the layout `git worktree` produces.

    `git worktree add --detach` writes `.git` as a FILE holding
    `gitdir: <path>`, and the gitdir's `HEAD` holds the commit directly
    because the worktree is detached. Reproduced here rather than mocked,
    so `export_revision` is exercised against the real file layout.
    """

    def __init__(self, revision=PIN, entry_source=None, head_text=None,
                 git_as_dir=False):
        self.root = Path(tempfile.mkdtemp(prefix="run002-gate-export-"))
        head = f"{revision}\n" if head_text is None else head_text
        if git_as_dir:
            gitdir = self.root / ".git"
            gitdir.mkdir()
        else:
            gitdir = self.root / ".gitdir-store"
            gitdir.mkdir()
            (self.root / ".git").write_text(f"gitdir: {gitdir}\n",
                                            encoding="utf-8")
        (gitdir / "HEAD").write_text(head, encoding="utf-8")
        if entry_source is not None:
            entry = self.root / gate_invoker.GATE_ENTRY
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(entry_source, encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def node_available() -> bool:
    return shutil.which("node") is not None


# ---------------------------------------------------------------- disabled


class DisabledByDefaultCase(unittest.TestCase):
    """OFF, and off in the way that matters: nothing happens at all.

    If this class goes red, publication has been switched on by something,
    and that is a deployment nobody approved.
    """

    def test_the_real_process_environment_does_not_enable_it(self):
        """Measured against os.environ, not against a fixture."""
        self.assertFalse(publisher.enabled(),
                         "publication is ENABLED in this environment - "
                         "deployment is not authorised")
        self.assertNotEqual(os.environ.get(publisher.ENABLE_ENV), "1")

    def test_a_disabled_path_starts_no_subprocess_and_posts_nothing(self):
        """The switch is checked BEFORE the gate runs.

        Running the gate to find out what it WOULD have published is still
        work the switch exists to prevent, and it would execute code from
        the export on a host where publication is not authorised.
        """
        poster, runner = Recorder(), FakeRunner(ok_run())
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root="/opt/run-002/gate-x",
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, runner=runner, environ={})
        self.assertFalse(attempt.posted)
        self.assertEqual(attempt.reason, publisher.DISABLED)
        self.assertIsNone(attempt.gate_outcome,
                          "the gate ran on a disabled publication path")
        self.assertEqual(runner.calls, [], "a disabled path spawned a gate")
        self.assertEqual(poster.calls, [], "a disabled path reached the "
                                           "transport")

    def test_the_switch_is_the_publisher_s_own_and_is_exact(self):
        """One definition of "on", not two that can drift apart."""
        for value in ("", "0", "true", "True", "yes", " 1", "1 ", "01"):
            with self.subTest(value=value):
                poster, runner = Recorder(), FakeRunner(ok_run())
                attempt = publication_path.publish_independent_review(
                    REQUEST, HEAD, export_root="/opt/run-002/gate-x",
                    expected_revision=PIN,
                    live_repo_root=LIVE , poster=poster, runner=runner,
                    environ={publisher.ENABLE_ENV: value,
                             publisher.TOKEN_ENV: "simulated"})
                self.assertEqual(attempt.reason, publisher.DISABLED)
                self.assertEqual(runner.calls, [])

    def test_an_enabled_path_with_no_credential_starts_no_subprocess(self):
        poster, runner = Recorder(), FakeRunner(ok_run())
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root="/opt/run-002/gate-x",
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, runner=runner,
            environ={publisher.ENABLE_ENV: "1"})
        self.assertEqual(attempt.reason, publisher.NO_CREDENTIAL)
        self.assertEqual(runner.calls, [])
        self.assertEqual(poster.calls, [])

    def test_every_external_edge_is_required_and_undefaulted(self):
        """Four of them, and omitting any one is a TypeError.

        Not a silent fallback to a plausible value - that is how "we never
        called it" becomes false, and in `live_repo_root`'s case a
        plausible value is the export itself, which is THE INVOKER TRAP.
        """
        with self.assertRaises(TypeError):          # everything
            publication_path.publish_independent_review(REQUEST, HEAD)
        with self.assertRaises(TypeError):          # no poster
            publication_path.publish_independent_review(
                REQUEST, HEAD, export_root="/opt/x", expected_revision=PIN,
                live_repo_root=LIVE)
        with self.assertRaises(TypeError):          # no live root
            publication_path.publish_independent_review(
                REQUEST, HEAD, export_root="/opt/x", expected_revision=PIN,
                poster=Recorder())
        with self.assertRaises(TypeError):          # everything
            gate_invoker.invoke_gate(REQUEST)
        with self.assertRaises(TypeError):          # no live root
            gate_invoker.invoke_gate(REQUEST, export_root="/opt/x",
                                     expected_revision=PIN)


# ------------------------------------------------------------ export trust


class ExportTrustCase(unittest.TestCase):
    """The gate runs the pinned code, or it does not run."""

    def test_the_export_path_must_be_supplied_and_absolute(self):
        for value in (None, "", "opt/run-002/gate", "./gate", 7, b"/opt"):
            with self.subTest(value=value):
                runner = FakeRunner(ok_run())
                run = gate_invoker.invoke_gate(
                    REQUEST, export_root=value, expected_revision=PIN,
                    live_repo_root=LIVE ,
                    runner=runner)
                self.assertFalse(run.ok)
                self.assertEqual(run.outcome,
                                 gate_invoker.EXPORT_PATH_INVALID)
                self.assertEqual(runner.calls, [])

    def test_this_mutable_tree_is_refused_by_name(self):
        """§7: never `config.REPO_ROOT`, and never a worker's worktree.

        The gate judges a pull request, so it must not execute code that
        pull request supplied. This repository IS the tree a pull request
        can write, which is why the one path that must never be an export
        is the one a careless default would reach for first.
        """
        from control import config
        candidates = [
            config.REPO_ROOT,
            config.REPO_ROOT / "apparatus",
            config.WORKTREE_ROOT,
            config.WORKTREE_ROOT / "T-001-build",
        ]
        for value in candidates:
            with self.subTest(value=str(value)):
                runner = FakeRunner(ok_run())
                run = gate_invoker.invoke_gate(
                    REQUEST, export_root=str(value), expected_revision=PIN,
                    live_repo_root=LIVE ,
                    runner=runner)
                self.assertEqual(run.outcome,
                                 gate_invoker.EXPORT_PATH_INVALID,
                                 f"{value} was accepted as a trusted export")
                self.assertEqual(runner.calls, [])

    def test_the_expected_revision_must_be_a_full_sha(self):
        """A short SHA is ambiguous and a branch name is a moving target."""
        for value in (None, "", PIN[:7], PIN.upper(), "main",
                      "wip/c05-1-persistence", 7):
            with self.subTest(value=value):
                export = _FakeExport(entry_source="")
                self.addCleanup(export.cleanup)
                runner = FakeRunner(ok_run())
                run = gate_invoker.invoke_gate(
                    REQUEST, export_root=str(export.root),
                    expected_revision=value,
                    live_repo_root=LIVE , runner=runner)
                self.assertEqual(run.outcome, gate_invoker.PIN_INVALID)
                self.assertEqual(runner.calls, [])

    def test_an_export_at_the_wrong_commit_is_never_executed(self):
        export = _FakeExport(revision=OTHER, entry_source="")
        self.addCleanup(export.cleanup)
        runner = FakeRunner(ok_run())
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(export.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            runner=runner)
        self.assertEqual(run.outcome, gate_invoker.EXPORT_UNTRUSTED)
        self.assertEqual(runner.calls, [],
                         "code at an unpinned commit was executed")

    def test_an_export_pinned_to_a_branch_is_refused_not_followed(self):
        """A symbolic HEAD is not resolved, deliberately.

        An export whose HEAD says `ref: refs/heads/main` is pinned to
        nothing: the next commit on that branch silently becomes the code
        that judges pull requests. Following the ref would make the pin a
        formality.
        """
        export = _FakeExport(head_text="ref: refs/heads/main\n",
                             entry_source="")
        self.addCleanup(export.cleanup)
        runner = FakeRunner(ok_run())
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(export.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            runner=runner)
        self.assertEqual(run.outcome, gate_invoker.EXPORT_UNTRUSTED)
        self.assertEqual(runner.calls, [])
        self.assertIsNone(gate_invoker.export_revision(export.root))

    def test_both_git_layouts_resolve_to_the_same_revision(self):
        for git_as_dir in (False, True):
            with self.subTest(git_as_dir=git_as_dir):
                export = _FakeExport(revision=PIN, git_as_dir=git_as_dir)
                self.addCleanup(export.cleanup)
                self.assertEqual(gate_invoker.export_revision(export.root),
                                 PIN)

    def test_an_export_with_no_git_metadata_resolves_to_nothing(self):
        root = Path(tempfile.mkdtemp(prefix="run002-nogit-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.assertIsNone(gate_invoker.export_revision(root))
        self.assertIsNone(gate_invoker.export_revision(root / "absent"))

    def test_a_missing_entry_script_is_distinct_from_an_untrusted_one(self):
        """The two failures an operator would act on differently.

        "The export has not been created" and "the export is the wrong
        commit" need different remedies, so they are different tokens.
        """
        correct = _FakeExport(revision=PIN)              # no entry script
        self.addCleanup(correct.cleanup)
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(correct.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            runner=FakeRunner(ok_run()))
        self.assertEqual(run.outcome, gate_invoker.EXPORT_MISSING)

        wrong = _FakeExport(revision=OTHER, entry_source="")
        self.addCleanup(wrong.cleanup)
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(wrong.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            runner=FakeRunner(ok_run()))
        self.assertEqual(run.outcome, gate_invoker.EXPORT_UNTRUSTED)


# ------------------------------------------------------- gate failure modes


class GateFailureModeCase(unittest.TestCase):
    """Every way the gate can fail, each a distinct finite outcome.

    None of them is permissive, and none of them escapes as an exception.
    """

    def setUp(self):
        self.export = _FakeExport(revision=PIN, entry_source="")
        self.addCleanup(self.export.cleanup)

    def run_with(self, result):
        return gate_invoker.invoke_gate(
            REQUEST, export_root=str(self.export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , runner=FakeRunner(result))

    def test_a_non_zero_exit_is_a_refusal(self):
        for code in (1, 2, 127, -9):
            with self.subTest(code=code):
                run = self.run_with(gate_invoker.RunResult(
                    code=code, stdout=json.dumps(decision())))
                self.assertFalse(run.ok,
                                 "a gate that exited non-zero was believed")
                self.assertEqual(run.outcome, gate_invoker.RUN_FAILED)
                self.assertIsNone(run.decision)

    def test_a_timeout_is_distinct_from_a_non_zero_exit(self):
        """A gate killed at the deadline and a gate that exited 124 of its
        own accord are different events; only one is a deadline."""
        timed = self.run_with(gate_invoker.RunResult(
            code=124, stdout="", timed_out=True))
        self.assertEqual(timed.outcome, gate_invoker.TIMED_OUT)
        exited = self.run_with(gate_invoker.RunResult(code=124, stdout=""))
        self.assertEqual(exited.outcome, gate_invoker.RUN_FAILED)
        self.assertNotEqual(timed.outcome, exited.outcome)

    def test_unparseable_output_is_a_refusal(self):
        for stdout in ("", "   ", "not json", "{", "[1,2]", "null", "7",
                       '"ELIGIBLE"', "ELIGIBLE"):
            with self.subTest(stdout=stdout):
                run = self.run_with(gate_invoker.RunResult(
                    code=0, stdout=stdout))
                self.assertEqual(run.outcome, gate_invoker.UNPARSEABLE)
                self.assertIsNone(run.decision)

    def test_a_decision_missing_a_required_field_is_a_refusal(self):
        """Distinct from unparseable: this one WAS valid JSON.

        "The gate printed something that is not a decision" and "the gate
        printed a decision with a hole in it" point at different defects.
        """
        for missing in ("decision", "blockedOnlyByPendingIndependentReview",
                        "reasons", "trustedHeadSha"):
            with self.subTest(missing=missing):
                body = decision()
                body.pop(missing)
                run = self.run_with(gate_invoker.RunResult(
                    code=0, stdout=json.dumps(body)))
                self.assertEqual(run.outcome, gate_invoker.MALFORMED)
                self.assertIsNone(run.decision)

    def test_a_decision_with_a_field_of_the_wrong_type_is_a_refusal(self):
        bad = [
            decision(verdict="MAYBE"),
            decision(verdict="eligible"),
            decision(verdict=None),
            decision(blockedOnlyByPendingIndependentReview="true"),
            decision(blockedOnlyByPendingIndependentReview=1),
            decision(blockedOnlyByPendingIndependentReview=None),
            decision(reasons={}),
            decision(reasons=None),
            decision(head="not-a-sha"),
            decision(head=HEAD[:39]),
            decision(head=HEAD.upper()),
        ]
        for body in bad:
            with self.subTest(body=body):
                run = self.run_with(gate_invoker.RunResult(
                    code=0, stdout=json.dumps(body)))
                self.assertEqual(run.outcome, gate_invoker.MALFORMED)

    def test_a_null_trusted_head_is_usable_not_malformed(self):
        """live-gate.js returns null when it could not resolve a commit.

        That is a decision it DID make (DENIED, HEAD_SHA_UNVERIFIED), and
        the publisher is entitled to act on it. Rejecting it here would
        discard a real verdict as a protocol error.
        """
        body = decision(verdict="DENIED", head=None,
                        reasons=[{"code": "HEAD_SHA_UNVERIFIED"}])
        run = self.run_with(gate_invoker.RunResult(
            code=0, stdout=json.dumps(body)))
        self.assertTrue(run.ok, run.outcome)
        self.assertIsNone(run.decision["trustedHeadSha"])

    def test_an_unserialisable_request_never_starts_a_process(self):
        runner = FakeRunner(ok_run())
        run = gate_invoker.invoke_gate(
            {"identity": object()}, export_root=str(self.export.root),
            expected_revision=PIN, live_repo_root=LIVE, runner=runner)
        self.assertEqual(run.outcome, gate_invoker.REQUEST_UNSERIALISABLE)
        self.assertEqual(runner.calls, [])

    def test_a_runner_that_raises_does_not_escape(self):
        def exploding(argv, stdin_text, timeout):
            raise RuntimeError("SENSITIVE-DETAIL-FROM-THE-GATE-HOST")

        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(self.export.root),
            expected_revision=PIN, live_repo_root=LIVE, runner=exploding)
        self.assertEqual(run.outcome, gate_invoker.RUN_FAILED)
        self.assertNotIn("SENSITIVE-DETAIL", str(run.as_dict()))

    def test_a_runner_returning_the_wrong_shape_is_a_refusal(self):
        """Fail closed on a runner that does not honour the contract."""
        for result in (None, 0, "", {"code": 0, "stdout": "{}"},
                       (0, json.dumps(decision()))):
            with self.subTest(result=result):
                run = self.run_with(result)
                self.assertEqual(run.outcome, gate_invoker.RUN_FAILED)

    def test_every_failure_outcome_is_a_distinct_token(self):
        self.assertEqual(len(set(gate_invoker.OUTCOMES)),
                         len(gate_invoker.OUTCOMES))

    def test_a_failing_gate_publishes_nothing_at_all(self):
        """Not a failure status. A failure status asserts this head was
        judged and found wanting, which is a different and untrue claim."""
        for result in (gate_invoker.RunResult(code=1, stdout=""),
                       gate_invoker.RunResult(code=0, stdout="not json"),
                       gate_invoker.RunResult(code=0, stdout="{}"),
                       gate_invoker.RunResult(code=124, stdout="",
                                              timed_out=True)):
            with self.subTest(result=result):
                poster = Recorder()
                attempt = publication_path.publish_independent_review(
                    REQUEST, HEAD, export_root=str(self.export.root),
                    expected_revision=PIN, live_repo_root=LIVE, poster=poster,
                    runner=FakeRunner(result), environ=ON)
                self.assertFalse(attempt.posted)
                self.assertEqual(attempt.reason,
                                 publication_path.GATE_UNUSABLE)
                self.assertIsNone(attempt.state)
                self.assertEqual(poster.calls, [])


# --------------------------------------------------- the F5 flag, untouched


class FlagIsPassedThroughCase(unittest.TestCase):
    """The flag is read, never re-derived. This is F5's safety argument.

    `blockedOnlyByPendingIndependentReview` is computed in live-gate.js
    from the reason list being PRECISELY ONE
    `PR_BLOCKED_BY_BRANCH_PROTECTION`, **and** the CI adapter's own `ok`.
    live-gate.test.js's `F5 MUTATION` test shows that deriving it instead
    from `mergeStateStatus === 'BLOCKED'` would publish an
    independent-review pass for a pull request whose CI concluded failure.

    Everything downstream of the gate sees a decision object. That object
    does NOT carry the CI adapter's verdict in a form anything could
    recompute the flag from - so a second opinion formed here would be
    formed without the CI leg, which is exactly the fail-open the gate
    rejected. These tests assert the second opinion is never formed.
    """

    def setUp(self):
        self.export = _FakeExport(revision=PIN, entry_source="")
        self.addCleanup(self.export.cleanup)

    def attempt(self, body, head=HEAD, poster=None):
        poster = Recorder() if poster is None else poster
        return publication_path.publish_independent_review(
            REQUEST, head, export_root=str(self.export.root),
            expected_revision=PIN, live_repo_root=LIVE, poster=poster,
            runner=FakeRunner(ok_run(body)), environ=ON), poster

    def test_the_flag_true_earns_success(self):
        body = decision(verdict="DENIED",
                        blockedOnlyByPendingIndependentReview=True,
                        reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"}])
        attempt, poster = self.attempt(body)
        self.assertTrue(attempt.posted, attempt.reason)
        self.assertEqual(attempt.state, publisher.SUCCESS)
        self.assertEqual(poster.calls,
                         [(publisher.CONTEXT, publisher.SUCCESS, HEAD)])

    def test_the_protection_reason_alone_does_not_synthesise_the_flag(self):
        """The decision LOOKS like the flag's precondition and is not it.

        Exactly one reason, and it is the protection one - so anything
        recomputing the flag from the reason list would publish SUCCESS.
        The gate said False, because its CI leg was not verified, and the
        gate's answer is the only one that is consulted.
        """
        body = decision(verdict="DENIED",
                        blockedOnlyByPendingIndependentReview=False,
                        reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION",
                                  "determination": "VERIFIED_FALSE"}])
        attempt, poster = self.attempt(body)
        self.assertEqual(attempt.state, publisher.FAILURE,
                         "the flag was re-derived from the reason list, "
                         "without the CI leg that makes it safe")
        self.assertEqual(poster.calls[0][1], publisher.FAILURE)

    def test_a_blocked_merge_state_does_not_synthesise_the_flag(self):
        """The rejected formulation, carried on the decision itself.

        The F5 MUTATION case in live-gate.test.js, pushed one layer down:
        a decision that drags `mergeStateStatus: BLOCKED` along with it
        must not be re-judged on that field by anything downstream.
        """
        body = decision(verdict="DENIED",
                        blockedOnlyByPendingIndependentReview=False,
                        reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"},
                                 {"code": "CI_UNVERIFIED"}],
                        mergeStateStatus="BLOCKED")
        attempt, _ = self.attempt(body)
        self.assertEqual(attempt.state, publisher.FAILURE)

    def test_a_denial_for_other_reasons_publishes_failure_never_success(self):
        for reasons in ([{"code": "CI_UNVERIFIED"}],
                        [{"code": "REVIEWER_UNVERIFIED"}],
                        [{"code": "EVIDENCE_SHA_UNBOUND"}],
                        [{"code": "OFFLINE_POLICY_FAILED"},
                         {"code": "PR_IS_DRAFT"}]):
            with self.subTest(reasons=reasons):
                body = decision(verdict="DENIED", reasons=reasons)
                attempt, poster = self.attempt(body)
                self.assertTrue(attempt.posted)
                self.assertEqual(attempt.state, publisher.FAILURE)
                self.assertEqual(poster.calls[0][1], publisher.FAILURE)

    def test_the_decision_is_handed_on_byte_for_byte(self):
        """Nothing in the chain edits the gate's answer before acting on it."""
        body = decision(verdict="DENIED",
                        reasons=[{"code": "CI_UNVERIFIED"}])
        original = json.loads(json.dumps(body))
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(self.export.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            runner=FakeRunner(ok_run(body)))
        self.assertEqual(run.decision, original)

    def test_a_head_the_gate_did_not_judge_is_refused(self):
        """The worst thing this chain could do.

        A verdict reached about one commit, posted against another, is a
        pass the gate never granted - and the status would be bound to the
        wrong SHA forever.
        """
        body = decision(verdict="ELIGIBLE", head=OTHER)
        attempt, poster = self.attempt(body, head=HEAD)
        self.assertFalse(attempt.posted)
        self.assertEqual(attempt.reason, publisher.HEAD_MISMATCH)
        self.assertEqual(poster.calls, [],
                         "a status was posted against a commit the gate "
                         "never judged")

    def test_head_mismatch_is_refused_even_when_the_flag_is_true(self):
        body = decision(verdict="DENIED", head=OTHER,
                        blockedOnlyByPendingIndependentReview=True,
                        reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"}])
        attempt, poster = self.attempt(body, head=HEAD)
        self.assertEqual(attempt.reason, publisher.HEAD_MISMATCH)
        self.assertEqual(poster.calls, [])


# ------------------------------------------------------- the connected path


@unittest.skipUnless(node_available(), "node is not on PATH")
class ConnectedPathCase(unittest.TestCase):
    """The joined path, end to end, with a REAL subprocess.

    `run_node` is the production runner and would otherwise be the one
    untested link in the chain. Here it runs a gate program this test file
    writes, in a temporary export this test file builds, printing a
    decision this test file composed. A simulated external service, driven
    by the real machinery that will drive the true one.

    No network, no `gh`, no token: the only thing spawned is `node` on a
    file three lines long.
    """

    def export_printing(self, body, exit_code=0, stderr_noise=False):
        lines = ["const out = " + json.dumps(json.dumps(body)) + ";"]
        if stderr_noise:
            lines.append("process.stderr.write('gate: diagnostics\\n');")
        lines.append("process.stdout.write(out);")
        lines.append(f"process.exit({exit_code});")
        export = _FakeExport(revision=PIN, entry_source="\n".join(lines))
        self.addCleanup(export.cleanup)
        return export

    def test_an_eligible_decision_travels_the_whole_path_to_success(self):
        export = self.export_printing(decision())
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertTrue(attempt.posted, attempt.as_dict())
        self.assertEqual(attempt.state, publisher.SUCCESS)
        self.assertEqual(attempt.gate_outcome, gate_invoker.OK)
        self.assertEqual(poster.calls,
                         [(publisher.CONTEXT, publisher.SUCCESS, HEAD)])

    def test_the_pending_review_flag_travels_the_whole_path_to_success(self):
        export = self.export_printing(decision(
            verdict="DENIED", blockedOnlyByPendingIndependentReview=True,
            reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"}]))
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertTrue(attempt.posted, attempt.as_dict())
        self.assertEqual(attempt.state, publisher.SUCCESS)

    def test_a_denial_travels_the_whole_path_to_failure(self):
        export = self.export_printing(decision(
            verdict="DENIED", reasons=[{"code": "CI_UNVERIFIED"}]))
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertTrue(attempt.posted)
        self.assertEqual(attempt.state, publisher.FAILURE)

    def test_a_head_mismatch_is_refused_over_the_real_subprocess(self):
        export = self.export_printing(decision(head=OTHER))
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertFalse(attempt.posted)
        self.assertEqual(attempt.reason, publisher.HEAD_MISMATCH)
        self.assertEqual(poster.calls, [])

    def test_a_gate_that_exits_non_zero_publishes_nothing(self):
        export = self.export_printing(decision(), exit_code=3)
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertEqual(attempt.gate_outcome, gate_invoker.RUN_FAILED)
        self.assertEqual(poster.calls, [])

    def test_diagnostics_on_stderr_do_not_corrupt_the_decision(self):
        """Only stdout is the answer. A gate is allowed to be chatty."""
        export = self.export_printing(decision(), stderr_noise=True)
        poster = Recorder()
        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=poster, environ=ON)
        self.assertTrue(attempt.posted, attempt.as_dict())

    def test_the_envelope_reaches_the_gate_on_stdin(self):
        """The gate is handed the request AND the live root.

        Both, because a gate program that had to find either for itself
        would find them from its own `__dirname` - which is inside the
        immutable export, has no `.runtime/`, and can never match
        `config/isolation.json`'s workspace. THE INVOKER TRAP.
        """
        export = _FakeExport(revision=PIN, entry_source="""
let raw = '';
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', () => {
  const envelope = JSON.parse(raw);
  process.stdout.write(JSON.stringify({
    decision: 'DENIED',
    trustedHeadSha: null,
    blockedOnlyByPendingIndependentReview: false,
    blockedOnlyByDraft: false,
    reasons: [
      { code: 'ECHO_REF', detail: envelope.request.identity.ref },
      { code: 'ECHO_ROOT', detail: envelope.liveRepoRoot },
    ],
  }));
});
""")
        self.addCleanup(export.cleanup)
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(export.root), expected_revision=PIN,
            live_repo_root=LIVE)
        self.assertTrue(run.ok, run.outcome)
        self.assertEqual(run.decision["reasons"][0]["detail"],
                         REQUEST["identity"]["ref"])
        self.assertEqual(run.decision["reasons"][1]["detail"], LIVE,
                         "the gate was not told which checkout to read "
                         "git facts from")
        self.assertNotEqual(run.decision["reasons"][1]["detail"],
                            str(export.root),
                            "the gate was pointed at the export for facts "
                            "the export does not contain")

    def test_a_gate_that_hangs_is_killed_at_the_deadline(self):
        export = _FakeExport(
            revision=PIN,
            entry_source="setTimeout(() => process.exit(0), 60000);")
        self.addCleanup(export.cleanup)
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(export.root), expected_revision=PIN,
            live_repo_root=LIVE ,
            timeout=2)
        self.assertEqual(run.outcome, gate_invoker.TIMED_OUT)

    def test_a_disabled_path_spawns_nothing_even_with_a_real_export(self):
        """The switch, proved against the real runner rather than a fake.

        A fake runner recording zero calls proves the fake was not called.
        This proves no PROCESS was started: the gate program writes a file
        when it runs, and the file is still absent afterwards.
        """
        export = _FakeExport(revision=PIN, entry_source="")
        self.addCleanup(export.cleanup)
        witness = export.root / "the-gate-ran"
        entry = export.root / gate_invoker.GATE_ENTRY
        entry.write_text(
            "require('fs').writeFileSync(%s, 'ran');\n"
            "process.stdout.write('{}');\n" % json.dumps(str(witness)),
            encoding="utf-8")

        attempt = publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=Recorder(), environ={})
        # The witness is asserted FIRST, deliberately. Checking the reason
        # token first would let a weaker guard further up the function take
        # the failure, and this test would then be red for a reason that
        # says nothing about whether a process ran.
        self.assertFalse(witness.exists(),
                         "a disabled publication path executed the gate")
        self.assertEqual(attempt.reason, publisher.DISABLED)

        # And the witness is a real witness: it appears when enabled.
        publication_path.publish_independent_review(
            REQUEST, HEAD, export_root=str(export.root),
            expected_revision=PIN,
            live_repo_root=LIVE , poster=Recorder(), environ=ON)
        self.assertTrue(witness.exists(),
                        "the witness never fires, so its absence above "
                        "proved nothing")


# --------------------------------------------------- repo-wide containment


class NothingReachesPublicationCase(unittest.TestCase):
    """Built, not wired - asserted over the WHOLE repository.

    WHY THIS EXISTS ALONGSIDE THE ONE IN
    `tests/test_c20a_publisher_disabled.py`, RATHER THAN REPLACING IT.

    That test asserts nothing in `control/` or `bin/` references the
    publisher. Adding the publication path did not weaken it and did not
    require amending it: the join lives in `experiment/github-app/`, so
    the control plane and the entry scripts remain publisher-free and
    their guarantee stands exactly as written.

    This test is the strictly stronger statement that the join makes
    necessary. It scans EVERY tracked Python file in the repository, not
    two directories, and allows exactly one importer: the publication path
    itself. It then closes the loop the first test cannot - that nothing
    imports the publication path either, so the only way to reach
    publication is for a human to load a module from a hyphenated
    directory by explicit file path, with the environment variable set and
    a transport supplied by hand.

    The regex is the one the original guard arrived at after a false
    positive: a real IMPORT or ATTRIBUTE reference, never the bare English
    word, which appears in several unrelated comments.
    """

    # `publisher\.(?!py\b)` and not `publisher\.[A-Za-z_]`.
    #
    # The narrower form matched the FILENAME in ordinary prose: any document
    # that says "control/publisher.py" scored as a caller. It caught
    # experiment/github-app/check-templates.py for a sentence explaining why
    # the publisher has no transport - a false positive, and exactly the
    # failure handover §45.8 records ("a not-wired check that matched the
    # English word 'publisher' in three unrelated comments"). A guard that
    # cries wolf is a guard someone eventually deletes.
    #
    # Excluding the filename would open one real hole - loading the module by
    # PATH rather than by name - so that is closed explicitly by the last
    # alternative rather than left to the exclusion. Net: strictly fewer false
    # positives AND one more true positive than before.
    # WIDENED 2026-10-02 after an independent review ran the compiled
    # pattern against the spellings someone would actually use.
    #
    # `from control.publisher import publish` - THE most idiomatic way to
    # wire this - did not match. Neither did `import_module`, `run_path`
    # or `exec(open(...))`. A file could import the publisher and call it,
    # and the class whose entire purpose is the containment guarantee
    # stayed green. That hole predates the earlier narrowing and was not
    # introduced by it, but it was missed by it.
    #
    # The filename spelling (`publisher.py`) is still excluded from the
    # attribute-access alternative, because ordinary prose says it; the
    # load-by-path spellings are caught by naming the LOADER instead
    # (`run_path(`, `exec(`, `import_module(`, `spec_from_file_location`),
    # which prose does not contain. Both properties are asserted below.
    WIRED = re.compile(r"\bimport\s+publisher\b"
                       r"|\bimport\s+[\w.]*\.publisher\b"
                       r"|\bfrom\s+[\w.]*\bpublisher\s+import\b"
                       r"|\bfrom\s+\.+\s*[\w.]*\bpublisher\s+import\b"
                       r"|\bpublisher\.(?!py\b)[A-Za-z_]"
                       r"|import_module\([^\n]*publisher"
                       r"|spec_from_file_location[^\n]*publisher"
                       r"|run_path\([^\n]*publisher"
                       r"|exec\s*\([^\n]*publisher")

    # `tests/test_c20b_status_transport.py` exercises the publisher through
    # its public constants to prove the two states this system can emit are
    # inside GitHub's documented enum. That is a test OF publication, not a
    # wiring of it - the transport it drives is injected and simulated.
    ALLOWED = {"control/publisher.py",
               "tests/test_c20b_status_transport.py",
               "experiment/github-app/publication_path.py",
               "tests/test_c20a_publisher_disabled.py",
               "tests/test_c20a_publication_path.py"}

    PRUNED = {".git", "node_modules", "__pycache__", ".runtime", ".claude"}

    def python_files(self):
        """Every Python file in the repository, pruned at the walk.

        Pruned rather than filtered afterwards: `node_modules` is tens of
        thousands of files, and a scan slow enough to notice is a scan
        someone eventually moves out of the default suite.
        """
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.PRUNED]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = Path(dirpath) / name
                yield path.relative_to(ROOT).as_posix(), path

    # Every way a Python file can reach another module, written out. If a
    # spelling is missing here it is missing from the guard, and the guard
    # is the whole containment claim.
    WIRING_SPELLINGS = (
        "from control.publisher import publish",
        "from control import publisher",
        "import control.publisher",
        "import publisher",
        "from publisher import publish",
        "from .publisher import publish",
        "from ..control.publisher import publish",
        'importlib.import_module("control.publisher")',
        'runpy.run_path("control/publisher.py")',
        'exec(open("control/publisher.py").read())',
        "publisher.publish(decision, head, poster=poster)",
        'spec_from_file_location("publisher", path)',
    )

    # Prose that NAMES the publisher without reaching it. Every one of
    # these appears somewhere in this repository's documentation.
    PROSE = (
        "control/publisher.py posts only through an injected `poster`",
        "the publisher is still unwritten",
        "**The publisher**, written and **disabled**",
        "# publisher.py holds the context constant",
    )

    def test_the_guard_catches_every_way_of_wiring_it(self):
        """The guard for the guard.

        A scan is only as good as its pattern, and this one's pattern was
        wrong in a way no test could see: it missed
        `from control.publisher import publish`, so the containment claim
        would have survived the most ordinary wiring anyone would write.
        Scanning files proves nothing if the thing doing the scanning
        cannot recognise what it is looking for.
        """
        for spelling in self.WIRING_SPELLINGS:
            self.assertRegex(spelling, self.WIRED,
                             f"the guard does not recognise {spelling!r} - "
                             "the publisher could be wired this way and the "
                             "repo-wide scan would stay green")

    def test_the_guard_does_not_fire_on_prose_that_merely_names_it(self):
        """The other half, and it is not optional.

        A guard that cries wolf is a guard someone eventually deletes.
        This one already failed on a documentation sentence once.
        """
        for sentence in self.PROSE:
            self.assertNotRegex(sentence, self.WIRED,
                                f"the guard fires on prose: {sentence!r}")

    def test_only_the_publication_path_reaches_the_publisher(self):
        callers = []
        for rel, path in self.python_files():
            if rel in self.ALLOWED:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if self.WIRED.search(text):
                callers.append(rel)
        self.assertEqual(
            sorted(callers), [],
            f"the publisher is referenced by {sorted(callers)} - the only "
            "module permitted to reach it is the publication path, which "
            "is itself unreachable without the environment variable")

    def test_nothing_imports_the_publication_path(self):
        """The loop closed: the one importer has no importers of its own."""
        wired = re.compile(r"\bpublication_path\b")
        callers = []
        for rel, path in self.python_files():
            if rel in ("experiment/github-app/publication_path.py",
                       "tests/test_c20a_publication_path.py"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if wired.search(text):
                callers.append(rel)
        self.assertEqual(sorted(callers), [],
                         f"the publication path is referenced by "
                         f"{sorted(callers)} - it is built to be reviewed, "
                         "not deployed, and wiring it is C-20a(C) action 9")

    def test_no_ordinary_entry_point_reaches_either_module(self):
        """bin/ and the CLI, by content rather than by import graph.

        `bin/` holds shell scripts, so an import scan would miss a direct
        `python3 -c` invocation. This reads them as text.
        """
        suspects = []
        for path in sorted((ROOT / "bin").rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "publisher" in text or "publication_path" in text:
                suspects.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(suspects, [],
                         f"an entry script mentions publication: {suspects}")

    def test_the_gate_invoker_cannot_publish(self):
        """It runs the gate; it has no way to act on the answer."""
        source = (ROOT / "control" / "gate_invoker.py").read_text(
            encoding="utf-8")
        self.assertIsNone(self.WIRED.search(source))
        self.assertNotIn("publication_path", source)

    def test_the_supervisor_tick_does_not_reach_publication(self):
        """Named explicitly, because it is the one loop that runs forever."""
        source = (ROOT / "control" / "supervisor.py").read_text(
            encoding="utf-8")
        self.assertIsNone(self.WIRED.search(source))
        self.assertNotIn("publication_path", source)
        self.assertNotIn("gate_invoker", source)


# ------------------------------------------------------- the default runner


class DefaultRunnerCase(unittest.TestCase):
    """`run_node` returns faults; it never raises them."""

    def test_a_missing_interpreter_is_a_returned_fault(self):
        result = gate_invoker.run_node(
            ["definitely-not-a-real-binary-run002"], "", 5)
        self.assertIsInstance(result, gate_invoker.RunResult)
        self.assertNotEqual(result.code, 0)
        self.assertFalse(result.timed_out)

    def test_a_timeout_is_reported_as_one(self):
        result = gate_invoker.run_node(
            [sys.executable, "-c", "import time; time.sleep(30)"], "", 2)
        self.assertTrue(result.timed_out)

    def test_stdout_is_returned_and_stdin_is_delivered(self):
        result = gate_invoker.run_node(
            [sys.executable, "-c",
             "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            "hello", 10)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.stdout, "HELLO")


# --------------------------------------------- the checkout facts come from


class LiveRootCase(unittest.TestCase):
    """`live_repo_root` is policed, and the export is the thing it is not.

    The gate's CODE is pinned and immutable. Its FACTS - the head SHA, the
    worktree register, the append-only ledger - exist only in the mutable
    checkout, so the two are different arguments and this class proves the
    invoker will not accept one in place of the other.
    """

    def setUp(self):
        self.export = _FakeExport(revision=PIN, entry_source="")
        self.addCleanup(self.export.cleanup)

    def invoke(self, live):
        runner = FakeRunner(ok_run())
        run = gate_invoker.invoke_gate(
            REQUEST, export_root=str(self.export.root),
            expected_revision=PIN, live_repo_root=live, runner=runner)
        return run, runner

    def test_a_live_root_that_is_not_an_absolute_path_is_refused(self):
        for value in (None, "", "   ", "relative/path", "./here", 7,
                      b"/abs", ["/abs"]):
            with self.subTest(value=value):
                run, runner = self.invoke(value)
                self.assertEqual(run.outcome, gate_invoker.LIVE_ROOT_INVALID)
                self.assertEqual(runner.calls, [])

    def test_the_export_itself_is_refused_as_the_live_root(self):
        """THE INVOKER TRAP, refused by the one check that can see it.

        An export has no `.runtime/` and no branches worth resolving, so a
        gate pointed at itself for facts does not error - it DENIES every
        pull request, permanently and silently. The export is
        `chmod -R a-w`, so it cannot be patched afterwards either.
        """
        run, runner = self.invoke(str(self.export.root))
        self.assertEqual(run.outcome, gate_invoker.LIVE_ROOT_INVALID)
        self.assertEqual(runner.calls, [],
                         "the gate was pointed at the export for facts the "
                         "export does not contain")

    def test_a_path_inside_the_export_is_refused_too(self):
        inner = self.export.root / "apparatus"
        run, runner = self.invoke(str(inner))
        self.assertEqual(run.outcome, gate_invoker.LIVE_ROOT_INVALID)
        self.assertEqual(runner.calls, [])

    def test_a_directory_that_is_not_a_checkout_is_refused(self):
        """No `.git` means no facts. An empty answer is not a healthy one."""
        plain = Path(tempfile.mkdtemp(prefix="run002-not-a-repo-"))
        self.addCleanup(shutil.rmtree, plain, True)
        run, runner = self.invoke(str(plain))
        self.assertEqual(run.outcome, gate_invoker.LIVE_ROOT_INVALID)
        self.assertEqual(runner.calls, [])

    def test_a_live_root_that_does_not_exist_is_refused(self):
        run, runner = self.invoke("/definitely/not/here/run-002")
        self.assertEqual(run.outcome, gate_invoker.LIVE_ROOT_INVALID)
        self.assertEqual(runner.calls, [])

    def test_a_real_checkout_is_accepted(self):
        """The positive case, so the refusals above prove something."""
        run, runner = self.invoke(LIVE)
        self.assertTrue(run.ok, run.outcome)
        self.assertEqual(len(runner.calls), 1)

    def test_the_isolation_config_is_not_consulted(self):
        """Deliberately NOT validated against `config/isolation.json`.

        That comparison - a module checking a config against its own
        location - IS the trap. Re-making it here would rebuild the
        coupling one layer up, and this checkout is not the configured
        workspace either, so the invoker would refuse the only live root
        it has.
        """
        import json as _json
        configured = _json.loads(
            (ROOT / "config" / "isolation.json").read_text(
                encoding="utf-8"))["workspace"]
        run, _ = self.invoke(LIVE)
        self.assertTrue(run.ok, run.outcome)
        if Path(configured).resolve() == Path(LIVE).resolve():
            self.skipTest("this checkout IS the configured workspace, so "
                          "the two policies cannot be told apart here")


# ------------------------------------------------- the trap, against the real
#                                                     adapters


@unittest.skipUnless(node_available() and shutil.which("git"),
                     "node and git are both needed")
class InvokerTrapCase(unittest.TestCase):
    """The regression test for THE INVOKER TRAP, run against REAL adapters.

    This is the test that would have caught it. Nothing is mocked: the
    actual `apparatus/adapters/git-head.js` and
    `apparatus/adapters/reviewer-identity.js` are placed at the layout
    position they occupy inside a read-only export, alongside the real
    `config/isolation.json`, and executed by real `node` against a real
    throwaway `git` repository.

    Both adapters derive a root from their own `__dirname`. From an export
    that root is the export, and:

      * `resolveRun002TrustedHeadSha` can never match the configured
        workspace, so it returns WORKSPACE_MISMATCH -> live-gate denies
        HEAD_SHA_UNVERIFIED -> every pull request blocked forever;
      * `resolveRun002ReviewerIdentity` reads `<root>/.runtime/ledger.jsonl`,
        which an export does not have, so it returns LEDGER_UNREADABLE ->
        REVIEWER_UNVERIFIED -> blocked forever.

    Each is demonstrated failing, and the low-level function the gate
    program must call instead is demonstrated succeeding from the SAME
    export location. No GitHub, no network, no `gh`, no credential: `git`
    on a directory this test created and `node` on files it copied.
    """

    BRANCH = "run-002/trap-regression"

    @classmethod
    def setUpClass(cls):
        # A throwaway checkout: the only place real git facts exist.
        cls.live = Path(tempfile.mkdtemp(prefix="run002-live-checkout-"))
        cls.git("init", "-q", "-b", "main")
        (cls.live / "README").write_text("fixture\n", encoding="utf-8")
        cls.git("add", "README")
        cls.git("-c", "user.name=run002", "-c", "user.email=run002@local",
                "commit", "-q", "-m", "fixture")
        cls.git("branch", cls.BRANCH)
        cls.sha = subprocess.run(
            ["git", "-C", str(cls.live), "rev-parse", "--verify", cls.BRANCH],
            capture_output=True, text=True, check=True).stdout.strip()

        # A ledger, where reviewer-identity.js expects to find one.
        runtime = cls.live / ".runtime"
        runtime.mkdir()
        (runtime / "ledger.jsonl").write_text(
            json.dumps({"event_type": "REVIEW_RESULT", "task_id": "T-001",
                        "pr_id": 1, "role": "reviewer",
                        "agent_id": "t-001-review-1",
                        "metadata_redacted": {"head_sha": cls.sha}}) + "\n",
            encoding="utf-8")
        (runtime / "state.json").write_text(
            json.dumps({"prs": {"1": {"reviewed_head": cls.sha}}}),
            encoding="utf-8")

        # A simulated read-only export: the adapters at the exact layout
        # position they occupy inside one, plus the pinned config.
        cls.export = Path(tempfile.mkdtemp(prefix="run002-trap-export-"))
        adapters = cls.export / "apparatus" / "adapters"
        adapters.mkdir(parents=True)
        for name in ("git-head.js", "reviewer-identity.js"):
            shutil.copy2(ROOT / "apparatus" / "adapters" / name,
                         adapters / name)
        (cls.export / "config").mkdir()
        shutil.copy2(ROOT / "config" / "isolation.json",
                     cls.export / "config" / "isolation.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.live, ignore_errors=True)
        shutil.rmtree(cls.export, ignore_errors=True)

    @classmethod
    def git(cls, *args):
        subprocess.run(["git", "-C", str(cls.live), *args], check=True,
                       capture_output=True, text=True)

    def drive(self, body):
        """Run `body` inside the simulated export and return its JSON."""
        driver = self.export / "driver.js"
        driver.write_text(body, encoding="utf-8")
        self.addCleanup(lambda: driver.unlink(missing_ok=True))
        proc = subprocess.run(["node", str(driver)], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_convenience_wrapper_denies_everything_from_an_export(self):
        """The trap itself, reproduced rather than argued."""
        result = self.drive("""
const a = require('./apparatus/adapters/git-head.js');
process.stdout.write(JSON.stringify(
  a.resolveRun002TrustedHeadSha({ kind: 'branch', ref: %s })));
""" % json.dumps(self.BRANCH))
        self.assertIs(result["ok"], False,
                      "the trap did not reproduce - re-read this test "
                      "before trusting the fix it guards")
        self.assertEqual(result["reason"], "WORKSPACE_MISMATCH")

    def test_the_low_level_function_returns_a_real_sha_from_an_export(self):
        """THE FIX, proved: the gate program must call this one.

        Same module, same export location, same identity - the only
        difference is that the live checkout is handed in rather than
        derived from `__dirname`. It resolves, and to the SHA git itself
        reports.
        """
        result = self.drive("""
const a = require('./apparatus/adapters/git-head.js');
process.stdout.write(JSON.stringify(
  a.resolveTrustedHeadSha({ kind: 'branch', ref: %s },
                          { repoRoot: %s })));
""" % (json.dumps(self.BRANCH), json.dumps(str(self.live))))
        self.assertIs(result["ok"], True, result)
        self.assertEqual(result["sha"], self.sha)
        self.assertEqual(result["resolvedFrom"], "branch:" + self.BRANCH)

    def test_the_reviewer_wrapper_finds_no_ledger_in_an_export(self):
        """The SECOND instance of the same trap, in a second adapter.

        An export has no `.runtime/` - it is gitignored, so a fresh
        `git worktree add` creates none - and a missing ledger is reported
        as COULD_NOT_VERIFY, which live-gate turns into a denial. Same
        permanent block, different token.
        """
        result = self.drive("""
const a = require('./apparatus/adapters/reviewer-identity.js');
process.stdout.write(JSON.stringify(
  a.resolveRun002ReviewerIdentity({ prNumber: 1 })));
""")
        self.assertIs(result["ok"], False)
        self.assertEqual(result["reason"], "LEDGER_UNREADABLE")

    def test_the_low_level_loader_reads_the_live_ledger_from_an_export(self):
        result = self.drive("""
const a = require('./apparatus/adapters/reviewer-identity.js');
process.stdout.write(JSON.stringify(
  a.loadReviewerEvidence(%s, 1)));
""" % json.dumps(str(self.live / ".runtime")))
        self.assertIs(result["ok"], True, result)
        self.assertEqual(len(result["ledgerEvents"]), 1)
        self.assertEqual(result["prRecord"]["reviewed_head"], self.sha)

    def test_the_export_really_has_no_runtime_directory(self):
        """The precondition the two tests above rest on."""
        self.assertFalse((self.export / ".runtime").exists())


if __name__ == "__main__":
    unittest.main()
