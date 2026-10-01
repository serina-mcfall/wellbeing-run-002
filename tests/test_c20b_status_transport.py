"""The commit-status transport — built, exercised, and unable to send.

SIMULATED RESPONSES ONLY. Every `http` here is a local function that
records its arguments and returns a canned status code. No socket, no
token, no App, no network. The module has no default client, so a test
that forgot to inject one fails with a TypeError rather than reaching out.

THE CONTRACT UNDER TEST IS GITHUB'S, read from
https://docs.github.com/en/rest/commits/statuses on 2026-10-02:

    POST /repos/{owner}/{repo}/statuses/{sha}
    Accept: application/vnd.github+json
    X-GitHub-Api-Version: 2026-03-10
    body {state, context, description?, target_url?}
    state ∈ {"error","failure","pending","success"}
    201 Created

These tests assert that shape literally. They CANNOT prove GitHub accepts
it - no request has ever been sent - and that gap is V3b's, not theirs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import publisher

# The transport lives in a hyphenated directory, so it is not importable
# as a package. Loading it takes a deliberate path-based import - which is
# the friction that keeps an unapproved deployment artefact out of every
# ordinary execution path, and is asserted as such below.
_PATH = (Path(__file__).resolve().parent.parent
         / "experiment" / "github-app" / "status_transport.py")
_SPEC = importlib.util.spec_from_file_location("status_transport", _PATH)
transport = importlib.util.module_from_spec(_SPEC)
# Registered BEFORE exec: `@dataclass` resolves its own module through
# `sys.modules[cls.__module__]`, which is None for a module being executed
# but not yet registered. Without this the import raises AttributeError
# inside dataclasses, nowhere near the real cause.
sys.modules["status_transport"] = transport
_SPEC.loader.exec_module(transport)

REPO = "serina-mcfall/wellbeing-run-002"
SHA = "a" * 40
OTHER = "b" * 40
CONTEXT = "run-002/independent-review"
TOKEN = {transport.TOKEN_ENV: "simulated-not-a-token"}
ON = dict(TOKEN, **{publisher.ENABLE_ENV: "1"})


class Response:
    def __init__(self, status):
        self.status = status


class Client:
    """A simulated HTTP client. Records; never reaches anything."""

    def __init__(self, status=transport.CREATED):
        self.calls: list[tuple] = []
        self.status = status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(self.status)


def on(): return True
def off(): return False


class TheRequestMatchesTheDocumentationCase(unittest.TestCase):
    """Field by field against the published contract."""

    def built(self, **over):
        kwargs = {"description": None, "target_url": None}
        kwargs.update(over)
        request, reason = transport.build_request(
            REPO, SHA, "success", CONTEXT, **kwargs)
        self.assertEqual(reason, "")
        return request

    def test_the_method_and_path_are_the_documented_ones(self):
        request = self.built()
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.url,
            f"https://api.github.com/repos/{REPO}/statuses/{SHA}")

    def test_the_two_required_headers_are_present_and_exact(self):
        headers = self.built().headers
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertEqual(headers["X-GitHub-Api-Version"], "2026-03-10")

    def test_the_body_carries_state_and_context_and_nothing_else(self):
        self.assertEqual(json.loads(self.built().json_body()),
                         {"state": "success", "context": CONTEXT})

    def test_optional_fields_are_omitted_rather_than_sent_as_null(self):
        """An absent optional field is absent, not an explicit null."""
        body = json.loads(self.built().json_body())
        self.assertNotIn("description", body)
        self.assertNotIn("target_url", body)

    def test_optional_fields_are_included_when_given(self):
        body = json.loads(self.built(description="d",
                                     target_url="https://e/x").json_body())
        self.assertEqual(body["description"], "d")
        self.assertEqual(body["target_url"], "https://e/x")

    def test_the_documented_state_enum_is_exactly_these_four(self):
        self.assertEqual(set(transport.STATES),
                         {"error", "failure", "pending", "success"})

    def test_the_two_states_this_system_publishes_are_inside_the_enum(self):
        """The join that makes publisher.py's states legal GitHub values."""
        self.assertIn(publisher.SUCCESS, transport.STATES)
        self.assertIn(publisher.FAILURE, transport.STATES)


class WhatIsRefusedBeforeARequestExistsCase(unittest.TestCase):

    def refused(self, *args, **kwargs):
        request, reason = transport.build_request(*args, **kwargs)
        self.assertIsNone(request)
        return reason

    def test_a_state_outside_the_enum_is_refused(self):
        for bad in ("SUCCESS", "ok", "passed", "", None, 1, True):
            self.assertEqual(
                self.refused(REPO, SHA, bad, CONTEXT), transport.BAD_STATE)

    def test_a_non_sha_target_is_refused(self):
        for bad in ("", "main", "a" * 39, "A" * 40, "g" * 40, None, 40):
            self.assertEqual(
                self.refused(REPO, bad, "success", CONTEXT),
                transport.BAD_TARGET)

    def test_a_repo_that_would_address_another_endpoint_is_refused(self):
        """This string is interpolated into a URL path.

        A repo carrying an extra slash, a parent reference or a query
        character does not name a repository - it names a different
        request than the one this module claims to make.
        """
        for bad in ("owner", "a/b/c", "../../admin", "owner/..",
                    "owner/repo?x=1", "owner/repo#f", "/repo", "owner/", ""):
            self.assertEqual(
                self.refused(bad, SHA, "success", CONTEXT),
                transport.BAD_TARGET, f"{bad!r} was accepted as a repo")

    def test_an_empty_context_is_refused(self):
        self.assertEqual(self.refused(REPO, SHA, "success", ""),
                         transport.BAD_TARGET)


class TheCredentialNeverLeavesTheSendCase(unittest.TestCase):
    """The rule from the global instruction, enforced in code.

    A token must not reach any structure that could be logged, returned,
    compared or reprd.
    """

    def test_a_built_request_carries_no_authorization_header(self):
        request, _ = transport.build_request(REPO, SHA, "success", CONTEXT)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("authorization",
                         {k.lower() for k in request.headers})

    def test_sending_does_not_write_the_token_back_onto_the_request(self):
        request, _ = transport.build_request(REPO, SHA, "success", CONTEXT)
        transport.send(request, http=Client(), environ=TOKEN)
        self.assertNotIn("Authorization", request.headers)

    def test_the_token_value_appears_in_no_returned_value(self):
        request, _ = transport.build_request(REPO, SHA, "success", CONTEXT)
        ok, reason = transport.send(request, http=Client(), environ=TOKEN)
        self.assertNotIn("simulated-not-a-token", repr((ok, reason, request)))

    def test_the_authorization_header_is_bearer_and_reaches_the_client(self):
        """It must still actually be sent - a transport that drops the
        credential would 401 forever and look like a permission problem."""
        client = Client()
        request, _ = transport.build_request(REPO, SHA, "success", CONTEXT)
        transport.send(request, http=client, environ=TOKEN)
        sent = client.calls[0][2]
        self.assertEqual(sent["Authorization"], "Bearer simulated-not-a-token")


class SendOutcomesCase(unittest.TestCase):

    def request(self):
        request, _ = transport.build_request(REPO, SHA, "success", CONTEXT)
        return request

    def test_201_is_the_only_success(self):
        ok, reason = transport.send(self.request(), http=Client(201),
                                    environ=TOKEN)
        self.assertTrue(ok)
        self.assertEqual(reason, transport.POSTED)

    def test_every_other_status_is_a_refusal_not_a_success(self):
        for status in (200, 202, 301, 400, 401, 403, 404, 422, 500, None):
            ok, reason = transport.send(self.request(), http=Client(status),
                                        environ=TOKEN)
            self.assertFalse(ok, f"HTTP {status} was treated as posted")
            self.assertEqual(reason, transport.REJECTED)

    def test_a_missing_credential_refuses_before_any_call(self):
        client = Client()
        ok, reason = transport.send(self.request(), http=client, environ={})
        self.assertFalse(ok)
        self.assertEqual(reason, transport.NO_CREDENTIAL)
        self.assertEqual(client.calls, [],
                         "a request was sent with no credential")

    def test_a_raising_client_is_a_finite_outcome_not_an_exception(self):
        def explode(*_):
            raise OSError("simulated network fault")
        ok, reason = transport.send(self.request(), http=explode,
                                    environ=TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, transport.TRANSPORT_FAILED)

    def test_a_client_returning_nonsense_does_not_become_a_success(self):
        ok, reason = transport.send(self.request(), http=lambda *_: object(),
                                    environ=TOKEN)
        self.assertFalse(ok)
        self.assertEqual(reason, transport.REJECTED)


class TheSwitchCase(unittest.TestCase):
    """Disabled means no request is built and no client is touched."""

    def test_a_disabled_poster_makes_no_call_at_all(self):
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=TOKEN,
                                      enabled=off)
        self.assertFalse(poster(CONTEXT, "success", SHA))
        self.assertEqual(client.calls, [],
                         "a disabled transport reached the client")

    def test_the_switch_is_consulted_per_call_not_captured_once(self):
        """A list-backed switch that flips mid-run proves it is re-read."""
        state = {"on": True}
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=TOKEN,
                                      enabled=lambda: state["on"])
        self.assertTrue(poster(CONTEXT, "success", SHA))
        state["on"] = False
        self.assertFalse(poster(CONTEXT, "success", SHA))
        self.assertEqual(len(client.calls), 1)

    def test_an_enabled_poster_sends_exactly_one_request(self):
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=TOKEN,
                                      enabled=on)
        self.assertTrue(poster(CONTEXT, "success", SHA))
        self.assertEqual(len(client.calls), 1)
        method, url, _, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn(SHA, url)
        self.assertEqual(json.loads(body)["context"], CONTEXT)

    def test_there_is_no_default_client(self):
        with self.assertRaises(TypeError):
            transport.poster_for(REPO, environ=TOKEN, enabled=on)

    def test_there_is_no_default_switch(self):
        with self.assertRaises(TypeError):
            transport.poster_for(REPO, http=Client(), environ=TOKEN)


class ThroughTheRealPublisherCase(unittest.TestCase):
    """The join: publisher decides, transport sends. Simulated client.

    This is the closest thing to the production path that exists, and it
    is still SIMULATED - the client is a local function. Nothing here
    proves GitHub accepts the request.
    """

    def eligible(self, head=SHA):
        return {"decision": "ELIGIBLE", "trustedHeadSha": head,
                "blockedOnlyByPendingIndependentReview": False, "reasons": []}

    def test_an_eligible_decision_posts_success_for_that_head(self):
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=ON,
                                      enabled=lambda: publisher.enabled(ON))
        result = publisher.publish(self.eligible(), SHA, poster=poster,
                                   environ=ON)
        self.assertTrue(result.posted)
        _, url, _, body = client.calls[0]
        self.assertTrue(url.endswith(f"/statuses/{SHA}"))
        self.assertEqual(json.loads(body),
                         {"state": "success", "context": publisher.CONTEXT})

    def test_a_denied_decision_posts_failure_not_success(self):
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=ON,
                                      enabled=lambda: publisher.enabled(ON))
        publisher.publish({"decision": "DENIED", "trustedHeadSha": SHA,
                           "blockedOnlyByPendingIndependentReview": False,
                           "reasons": [{"code": "CI_UNVERIFIED"}]},
                          SHA, poster=poster, environ=ON)
        self.assertEqual(json.loads(client.calls[0][3])["state"], "failure")

    def test_the_whole_chain_is_inert_with_the_switch_unset(self):
        """The property that matters most, end to end."""
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=TOKEN,
                                      enabled=lambda: publisher.enabled({}))
        result = publisher.publish(self.eligible(), SHA, poster=poster,
                                   environ={})
        self.assertFalse(result.posted)
        self.assertEqual(result.reason, publisher.DISABLED)
        self.assertEqual(client.calls, [])

    def test_a_head_the_gate_did_not_judge_never_reaches_the_client(self):
        client = Client()
        poster = transport.poster_for(REPO, http=client, environ=ON,
                                      enabled=lambda: publisher.enabled(ON))
        result = publisher.publish(self.eligible(head=OTHER), SHA,
                                   poster=poster, environ=ON)
        self.assertFalse(result.posted)
        self.assertEqual(client.calls, [])


class NothingReachesThisModuleCase(unittest.TestCase):
    """It is built to be reviewed, not deployed."""

    def test_no_module_in_the_repository_imports_the_transport(self):
        root = Path(__file__).resolve().parent.parent
        callers = []
        for path in list((root / "control").rglob("*.py")) + \
                list((root / "bin").rglob("*")) + \
                list((root / "experiment" / "github-app").glob("*.py")):
            # check-templates.py is excluded because it ASSERTS the
            # transport's properties - that it has no default client, no
            # socket import and no importers. A checker that proves a
            # module is unwired necessarily names it, and counting that as
            # wiring would make the two guards contradict each other.
            if (not path.is_file()
                    or path.name in ("status_transport.py",
                                     "check-templates.py")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "status_transport" in text:
                callers.append(str(path.relative_to(root)))
        self.assertEqual(callers, [],
                         f"the transport is referenced by {callers} - "
                         "wiring it is C-20a(C) action 9, which is not "
                         "approved")

    def test_the_module_opens_no_socket_of_its_own(self):
        """No default client is a promise; this is the check.

        A transport that imported `urllib` or `requests` could reach the
        network whatever its arguments say.
        """
        source = _PATH.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        for forbidden in ("urllib", "http.client", "requests", "socket",
                          "urlopen", "httpx"):
            self.assertNotIn(forbidden, body,
                             f"the transport reaches for {forbidden}")


if __name__ == "__main__":
    unittest.main()
