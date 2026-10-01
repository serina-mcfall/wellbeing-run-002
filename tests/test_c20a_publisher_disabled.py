"""The independent-review publisher: built, exercised, and disabled.

SIMULATED RESPONSES ONLY. Every `poster` here is a local function that
records its arguments. No GitHub call, no token, no App, no network. The
module has no default transport, so a test that forgot to inject one
would fail with a TypeError rather than silently reaching out.

The first class is the one that matters most: it asserts the thing is OFF
and stays off. If `DisabledByDefaultCase` ever goes red, publication has
been switched on by something, and that is a deployment nobody approved.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import publisher

HEAD = "a" * 40
OTHER = "b" * 40

ON = {publisher.ENABLE_ENV: "1", publisher.TOKEN_ENV: "simulated-not-a-token"}


class Recorder:
    """A simulated status endpoint. Records; never reaches anything."""

    def __init__(self, ok=True):
        self.calls: list[tuple] = []
        self.ok = ok

    def __call__(self, context, state, sha):
        self.calls.append((context, state, sha))
        return self.ok


def eligible(head=HEAD, **over):
    decision = {"decision": "ELIGIBLE", "trustedHeadSha": head,
                "blockedOnlyByPendingIndependentReview": False,
                "blockedOnlyByDraft": False, "reasons": []}
    decision.update(over)
    return decision


def denied(head=HEAD, **over):
    decision = {"decision": "DENIED", "trustedHeadSha": head,
                "blockedOnlyByPendingIndependentReview": False,
                "blockedOnlyByDraft": False,
                "reasons": [{"code": "CI_UNVERIFIED"}]}
    decision.update(over)
    return decision


class DisabledByDefaultCase(unittest.TestCase):
    """OFF, and off in the way that matters: no call is made."""

    def test_the_real_process_environment_does_not_enable_it(self):
        """Measured against os.environ, not against a fixture.

        This is the assertion that would catch publication having been
        switched on for this checkout.
        """
        self.assertFalse(publisher.enabled(),
                         "publication is ENABLED in this environment - "
                         "deployment is not authorised")

    def test_a_disabled_publisher_makes_no_call_at_all(self):
        poster = Recorder()
        result = publisher.publish(eligible(), HEAD, poster=poster, environ={})
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.DISABLED)
        self.assertEqual(poster.calls, [],
                         "a disabled publisher reached the transport")

    def test_only_the_exact_string_one_enables_it(self):
        """A permissive parse is how a stray value turns this on."""
        for value in ("", "0", "true", "True", "yes", "on", " 1", "1 ", "01"):
            with self.subTest(value=value):
                self.assertFalse(
                    publisher.enabled({publisher.ENABLE_ENV: value}))
        self.assertTrue(publisher.enabled({publisher.ENABLE_ENV: "1"}))

    def test_an_absent_variable_is_disabled(self):
        self.assertFalse(publisher.enabled({}))

    def test_nothing_in_the_control_plane_calls_publish(self):
        """Built, not wired. If this fails, it has been deployed.

        Searched over control/ and bin/, excluding this module's own
        definition and the tests.

        Matched on a real IMPORT or ATTRIBUTE reference, not on the bare
        word. The first version of this guard searched for "publisher"
        and matched the English word in three unrelated comments - a
        guard that cries wolf is one people switch off.
        """
        import re
        root = Path(__file__).resolve().parent.parent
        # WIDENED 2026-10-02. This pattern missed
        # `from control.publisher import publish` - the most idiomatic
        # wiring there is - along with import_module, run_path and
        # exec-by-path. Kept identical to the one in
        # tests/test_c20a_publication_path.py, which asserts its own
        # coverage spelling by spelling; if these two ever diverge, that
        # file is the one that proves which is right.
        wired = re.compile(r"\bimport\s+publisher\b"
                           r"|\bimport\s+[\w.]*\.publisher\b"
                           r"|\bfrom\s+[\w.]*\bpublisher\s+import\b"
                           r"|\bfrom\s+\.+\s*[\w.]*\bpublisher\s+import\b"
                           r"|\bpublisher\.(?!py\b)[A-Za-z_]"
                           r"|import_module\([^\n]*publisher"
                           r"|spec_from_file_location[^\n]*publisher"
                           r"|run_path\([^\n]*publisher"
                           r"|exec\s*\([^\n]*publisher")
        callers = []
        for path in list((root / "control").rglob("*.py")) + \
                list((root / "bin").rglob("*")):
            if not path.is_file() or path.name == "publisher.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if wired.search(text):
                callers.append(str(path.relative_to(root)))
        self.assertEqual(
            callers, [],
            f"the publisher is referenced by {callers} - it is built to be "
            "reviewed, not deployed, and wiring it is C-20a(C)")


class CredentialCase(unittest.TestCase):
    """Presence only. The value is never read, compared or logged."""

    def test_an_enabled_publisher_with_no_credential_refuses(self):
        poster = Recorder()
        result = publisher.publish(
            eligible(), HEAD, poster=poster,
            environ={publisher.ENABLE_ENV: "1"})
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.NO_CREDENTIAL)
        self.assertEqual(poster.calls, [])

    def test_presence_is_a_boolean_not_the_value(self):
        self.assertIs(
            publisher.credential_present({publisher.TOKEN_ENV: "abc"}), True)
        self.assertIs(publisher.credential_present({}), False)

    def test_no_refusal_ever_carries_the_credential(self):
        """A reason string that quoted the token would leak it into the
        ledger, which is where these reasons go."""
        secret = "simulated-not-a-token"
        result = publisher.publish(denied(), HEAD, poster=Recorder(),
                                   environ=ON)
        for value in result.as_dict().values():
            self.assertNotIn(secret, str(value))


class WhatEarnsASuccessCase(unittest.TestCase):
    """Two states earn success. Exactly two."""

    def test_an_eligible_decision_earns_success(self):
        poster = Recorder()
        result = publisher.publish(eligible(), HEAD, poster=poster, environ=ON)
        self.assertTrue(result.posted, result.reason)
        self.assertEqual(result.state, publisher.SUCCESS)
        self.assertEqual(poster.calls,
                         [(publisher.CONTEXT, publisher.SUCCESS, HEAD)])

    def test_blocked_only_on_this_very_status_earns_success(self):
        """F5's deadlock break.

        Once the context is required, the gate denies for
        PR_BLOCKED_BY_BRANCH_PROTECTION until this publishes - and this
        publishes because of that flag. The flag's safety lives in
        live-gate.js, which computes it from the reason list plus verified
        CI, never from mergeStateStatus.
        """
        decision = denied(
            blockedOnlyByPendingIndependentReview=True,
            reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"}])
        poster = Recorder()
        result = publisher.publish(decision, HEAD, poster=poster, environ=ON)
        self.assertTrue(result.posted, result.reason)
        self.assertEqual(result.state, publisher.SUCCESS)

    def test_an_ordinary_denial_earns_failure(self):
        poster = Recorder()
        result = publisher.publish(denied(), HEAD, poster=poster, environ=ON)
        self.assertTrue(result.posted)
        self.assertEqual(result.state, publisher.FAILURE)
        self.assertEqual(poster.calls[0][1], publisher.FAILURE)

    def test_a_denial_with_several_reasons_is_never_a_success(self):
        """Not even when one of them is the protection reason.

        The flag means PRECISELY ONE reason. A decision carrying the
        protection reason alongside a failing CI must not publish success,
        and this asserts the publisher does not second-guess the flag.
        """
        decision = denied(
            blockedOnlyByPendingIndependentReview=False,
            reasons=[{"code": "PR_BLOCKED_BY_BRANCH_PROTECTION"},
                     {"code": "CI_UNVERIFIED"}])
        result = publisher.publish(decision, HEAD, poster=Recorder(),
                                   environ=ON)
        self.assertEqual(result.state, publisher.FAILURE)

    def test_a_truthy_non_true_flag_does_not_earn_success(self):
        """`is True`, not truthiness. JSON can carry a string."""
        for value in ("true", 1, [1], {"a": 1}):
            with self.subTest(value=value):
                decision = denied(
                    blockedOnlyByPendingIndependentReview=value)
                result = publisher.publish(decision, HEAD, poster=Recorder(),
                                           environ=ON)
                self.assertEqual(result.state, publisher.FAILURE)


class WhatPublishesNothingCase(unittest.TestCase):
    """Absent is not the same as failing, and must not be reported as it."""

    def test_an_unusable_decision_publishes_nothing(self):
        for decision in (None, [], "ELIGIBLE", 7,
                         {"decision": "MAYBE"}, {}):
            with self.subTest(decision=decision):
                poster = Recorder()
                result = publisher.publish(decision, HEAD, poster=poster,
                                           environ=ON)
                self.assertFalse(result.posted)
                self.assertEqual(result.reason, publisher.UNUSABLE_DECISION)
                self.assertEqual(
                    poster.calls, [],
                    "a failure status was posted for a head the gate never "
                    "judged")

    def test_an_unusable_head_publishes_nothing(self):
        for head in (None, "", "not-a-sha", HEAD[:-1], HEAD.upper(), 7):
            with self.subTest(head=head):
                poster = Recorder()
                result = publisher.publish(eligible(), head, poster=poster,
                                           environ=ON)
                self.assertFalse(result.posted)
                self.assertEqual(result.reason, publisher.UNUSABLE_HEAD)
                self.assertEqual(poster.calls, [])

    def test_a_decision_about_another_commit_publishes_nothing(self):
        """The worst thing this module could do.

        A verdict reached about one commit, posted against another, is a
        pass the gate never granted - and the status would be bound to the
        wrong SHA forever.
        """
        poster = Recorder()
        result = publisher.publish(eligible(head=OTHER), HEAD, poster=poster,
                                   environ=ON)
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.HEAD_MISMATCH)
        self.assertEqual(poster.calls, [])

    def test_a_decision_with_no_trusted_head_is_still_judged(self):
        """`trustedHeadSha` absent is not a mismatch.

        The gate omits it when it could not resolve one; the decision is
        then DENIED for HEAD_SHA_UNVERIFIED and earns a failure status.
        Treating absence as a mismatch would publish nothing and leave the
        pull request with no report at all.
        """
        decision = denied()
        decision.pop("trustedHeadSha")
        result = publisher.publish(decision, HEAD, poster=Recorder(),
                                   environ=ON)
        self.assertTrue(result.posted)
        self.assertEqual(result.state, publisher.FAILURE)


class ASuccessMustNameTheHeadItJudgedCase(unittest.TestCase):
    """The asymmetry between a failure and a success, and why it exists.

    `test_a_decision_with_no_trusted_head_is_still_judged` above is
    deliberate: a DENIED decision that could not resolve a head still
    earns a truthful `failure`, so the pull request is not left with no
    report. A SUCCESS carrying no resolved head is a different animal
    entirely - it blesses a commit the gate never judged.

    MEASURED 2026-10-02 before the guard existed: one decision object with
    `trustedHeadSha: None` and the pending flag set posted `success`
    against TWO DIFFERENT heads. live-gate.js cannot currently emit that
    pair, but that is an unstated invariant of a program in another
    language, and this module already declines to take the flag itself on
    trust.
    """

    def _pending(self, trusted):
        return {"decision": "DENIED", "trustedHeadSha": trusted,
                "blockedOnlyByPendingIndependentReview": True, "reasons": []}

    def test_a_success_with_no_resolved_head_publishes_nothing(self):
        poster = Recorder()
        result = publisher.publish(self._pending(None), HEAD,
                                   poster=poster, environ=ON)
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.UNUSABLE_HEAD)
        self.assertEqual(poster.calls, [],
                         "a success was posted for a head the gate never "
                         "resolved")

    def test_the_same_null_head_decision_cannot_bless_two_commits(self):
        """The reproduction, kept as the regression."""
        poster = Recorder()
        decision = self._pending(None)
        for head in (HEAD, OTHER):
            publisher.publish(decision, head, poster=poster, environ=ON)
        self.assertEqual(poster.calls, [])

    def test_a_non_sha_trusted_head_also_refuses(self):
        for bad in ("", "not-a-sha", "A" * 40, 40 * 1, ["a" * 40]):
            poster = Recorder()
            result = publisher.publish(self._pending(bad), HEAD,
                                       poster=poster, environ=ON)
            self.assertFalse(result.posted, f"{bad!r} earned a success")
            self.assertEqual(poster.calls, [])

    def test_a_resolved_matching_head_still_succeeds(self):
        """The control. A guard that refuses everything proves nothing."""
        poster = Recorder()
        result = publisher.publish(self._pending(HEAD), HEAD,
                                   poster=poster, environ=ON)
        self.assertTrue(result.posted)
        self.assertEqual(result.state, publisher.SUCCESS)

    def test_a_failure_for_an_unresolved_head_is_still_published(self):
        """The deliberate behaviour this guard must NOT have broken."""
        decision = {"decision": "DENIED", "trustedHeadSha": None,
                    "blockedOnlyByPendingIndependentReview": False,
                    "reasons": [{"code": "HEAD_SHA_UNVERIFIED"}]}
        poster = Recorder()
        result = publisher.publish(decision, HEAD, poster=poster, environ=ON)
        self.assertTrue(result.posted)
        self.assertEqual(result.state, publisher.FAILURE)


class TransportFailureCase(unittest.TestCase):
    """A transport fault is a finite outcome, never an exception."""

    def test_a_poster_returning_false_is_a_finite_refusal(self):
        result = publisher.publish(eligible(), HEAD, poster=Recorder(ok=False),
                                   environ=ON)
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.POST_FAILED)

    def test_a_poster_that_raises_does_not_escape(self):
        def exploding(context, state, sha):
            raise RuntimeError("simulated transport failure")

        result = publisher.publish(eligible(), HEAD, poster=exploding,
                                   environ=ON)
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.POST_FAILED)

    def test_no_exception_text_reaches_the_outcome(self):
        """C-16: finite tokens in durable evidence, never raw prose."""
        def exploding(context, state, sha):
            raise RuntimeError("SENSITIVE-DETAIL-FROM-THE-TRANSPORT")

        result = publisher.publish(eligible(), HEAD, poster=exploding,
                                   environ=ON)
        self.assertNotIn("SENSITIVE-DETAIL", str(result.as_dict()))

    def test_there_is_no_default_transport(self):
        """`poster` is required and undefaulted, so this module cannot
        reach GitHub by accident."""
        with self.assertRaises(TypeError):
            publisher.publish(eligible(), HEAD)      # noqa: missing poster


class ContextNameCase(unittest.TestCase):
    def test_the_context_is_the_one_branch_protection_will_require(self):
        self.assertEqual(publisher.CONTEXT, "run-002/independent-review")

    def test_the_context_is_not_in_the_required_checks_list(self):
        """Proposal §4.3a. `required_checks` is iterated against the
        CHECK-RUN surface, where a commit STATUS never appears, so adding
        it there is a permanent silent deny."""
        import json
        from control import config
        required = json.loads(
            (config.REPO_ROOT / "config" / "experiment.json").read_text(
                encoding="utf-8"))["github"]["required_checks"]
        self.assertNotIn(publisher.CONTEXT, required)


if __name__ == "__main__":
    unittest.main()
