"""The commit-status transport. Approval action 9's last piece.

WHAT THIS IS. `control/publisher.py` decides WHETHER to publish and WHAT
state to publish; it has never known HOW, because `poster` is a required,
undefaulted argument and every caller in this repository passes a
simulated one. This module is the real `poster` — the only code in Run 002
that builds a GitHub commit-status request.

THE CONTRACT IS DERIVED FROM GITHUB'S OWN DOCUMENTATION, not from memory.
Read 2026-10-02 from https://docs.github.com/en/rest/commits/statuses:

    POST /repos/{owner}/{repo}/statuses/{sha}
    Accept: application/vnd.github+json
    X-GitHub-Api-Version: 2026-03-10
    Authorization: Bearer <token>
    body: {state, context, description?, target_url?}
    state ∈ {"error", "failure", "pending", "success"}   (exactly these)
    201 Created on success
    Limit: 1000 statuses per (sha, context) in a repository.

The publisher's SUCCESS and FAILURE constants are "success" and "failure",
both inside that enum, so the two states this system can publish are
valid. `STATES` below is the enum as documented, and anything outside it
is refused before a request is built — a state GitHub would reject is a
bug here, not a round trip.

WHY IT LIVES IN `experiment/github-app/` AND NOT IN `control/`.

The strongest guarantee this system makes about publication is that NO
module in `control/` or `bin/` reaches the publisher at all, so no
ordinary execution path — the Supervisor tick, the CLI, the entry scripts
— can arrive at a published status. The publication-path module sits here for
that reason and this sits beside it. The directory name contains a hyphen,
so neither is importable as a package; reaching either takes a deliberate
`importlib` load against an explicit file path.

> **AND THAT PLACEMENT WAS PREVIOUSLY REFUSED, FOR A REASON THAT HAS BEEN
> HONOURED RATHER THAN ROUTED AROUND.** `check-templates.py` check E
> greps for the `statuses/` path OUTSIDE `experiment/`, so putting a
> transport here would once have kept that check green while making its
> stated claim — "no code constructs a commit-status API call" — false.
> A green check whose claim is false is worse than a red one.
>
> So check E's claim was CHANGED to the one that is still true and still
> worth proving: no module in `control/` or `bin/` constructs the path,
> and the transport that now exists has no default client. The check was
> not relaxed to accommodate this file; it was re-aimed at the property
> that survives it.

WHAT STOPS THIS REACHING GITHUB TODAY.

  * `http` is REQUIRED and UNDEFAULTED. There is no default client, no
    `requests` import, no `urllib` call. This module cannot open a socket
    on its own; something must hand it one.
  * `enabled()` — the publisher's own, the same exact environment
    variable — is checked before a request is even built.
  * The token is read BY NAME from the environment at the moment of
    sending and is never returned, logged, stored on the request object,
    or included in anything a test inspects. `build_request` deliberately
    produces headers WITHOUT the Authorization header, so no structure
    this module hands back can carry a credential.
  * Nothing imports this module. The publication path does not; a test
    asserts that.

NO APP, INSTALLATION, CREDENTIAL OR DEPLOYMENT EXISTS. Nothing here
creates one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Documented 2026-10-02. The whole enum, not just the two this system uses,
# because the point of validating against it is to catch a value GitHub
# would reject before a request carries it.
STATES = ("error", "failure", "pending", "success")

ACCEPT = "application/vnd.github+json"
API_VERSION = "2026-03-10"
API_ROOT = "https://api.github.com"

# Presence-only, exactly as control/publisher.py uses it. The value is a
# token and is never read into any structure this module returns.
TOKEN_ENV = "RUN_002_GATE_APP_TOKEN"

# Finite outcomes. Prose is for humans; these are what a caller records.
POSTED = "STATUS_POSTED"
DISABLED = "TRANSPORT_DISABLED"
NO_CREDENTIAL = "TRANSPORT_CREDENTIAL_ABSENT"
BAD_STATE = "STATE_NOT_IN_ENUM"
BAD_TARGET = "TARGET_UNUSABLE"
REJECTED = "GITHUB_REJECTED"
TRANSPORT_FAILED = "TRANSPORT_FAILED"

CREATED = 201

_SHA = "0123456789abcdef"
_REPO_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _is_sha(value) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(c in _SHA for c in value))


def _is_repo(value) -> bool:
    """`owner/name`, both segments non-empty and path-safe.

    Checked because this string is interpolated into a URL path. A repo
    carrying a slash, a `..` or a query character would address a
    different endpoint than the one this module claims to call.
    """
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, _, name = value.partition("/")
    return (bool(owner) and bool(name)
            and all(c in _REPO_OK for c in owner + name)
            and owner not in (".", "..") and name not in (".", ".."))


@dataclass(frozen=True)
class StatusRequest:
    """One request, fully formed and CARRYING NO CREDENTIAL.

    `headers` deliberately omits Authorization. It is added at the moment
    of sending and never stored here, so nothing that inspects, logs or
    reprs a request can expose a token.
    """

    method: str
    url: str
    headers: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)

    def json_body(self) -> str:
        return json.dumps(self.body)


def build_request(repo: str, sha: str, state: str, context: str, *,
                  description: str | None = None,
                  target_url: str | None = None,
                  api_root: str = API_ROOT,
                  api_version: str = API_VERSION) -> tuple:
    """(StatusRequest, "") or (None, reason). PURE — never sends anything.

    Separated from the send so the request shape can be tested exhaustively
    without a client existing at all, which is the only way to check a
    contract against documentation rather than against a live endpoint.

    `description` and `target_url` are optional per the documentation and
    are OMITTED rather than sent as null when absent: an absent field and
    an explicit null are the same to GitHub here, and omitting keeps the
    body minimal and the diff of what we send obvious.
    """
    if state not in STATES:
        return None, BAD_STATE
    if not _is_repo(repo) or not _is_sha(sha):
        return None, BAD_TARGET
    if not isinstance(context, str) or not context:
        return None, BAD_TARGET

    body: dict = {"state": state, "context": context}
    if description is not None:
        body["description"] = description
    if target_url is not None:
        body["target_url"] = target_url

    return StatusRequest(
        method="POST",
        url=f"{api_root}/repos/{repo}/statuses/{sha}",
        headers={"Accept": ACCEPT, "X-GitHub-Api-Version": api_version},
        body=body,
    ), ""


def send(request: StatusRequest, *, http, environ) -> tuple:
    """Send one built request. (ok, reason). NEVER RAISES.

    `http(method, url, headers, body)` is REQUIRED and UNDEFAULTED — the
    whole external edge, injected. It must return an object with a
    `status` (int). Every test passes a simulated one; nothing in this
    repository passes a real one.

    The Authorization header is constructed HERE and discarded with the
    call. It is never written back onto `request`, so a request object
    that outlives this function carries no credential.
    """
    token = environ.get(TOKEN_ENV)
    if not token:
        return False, NO_CREDENTIAL

    headers = dict(request.headers)
    headers["Authorization"] = f"Bearer {token}"
    del token

    try:
        response = http(request.method, request.url, headers,
                        request.json_body())
    except Exception:            # noqa: BLE001 - a transport fault is finite
        return False, TRANSPORT_FAILED

    status = getattr(response, "status", None)
    if status == CREATED:
        return True, POSTED
    # Anything else is a refusal we report rather than interpret. A 422 is
    # the documented answer to exceeding 1000 statuses for one (sha,
    # context); a 403 is the answer to a credential without the permission.
    # Both mean "not posted", and guessing which would be inventing detail
    # this module cannot observe.
    return False, REJECTED


def poster_for(repo: str, *, http, environ, enabled):
    """A `poster` in `control/publisher.py`'s shape, bound to one repo.

    The publisher calls `poster(context, state, sha)` and treats a
    truthy answer as posted. This adapts the two-step build/send into that
    one callable, so the publisher's contract is unchanged and this module
    stays the only thing that knows the request exists.

    `enabled` IS REQUIRED AND UNDEFAULTED, and it is a callable rather than
    a boolean so it is evaluated per call rather than captured once. The
    publisher checks the switch before calling any poster; this checks it
    again, because a transport that only refuses when someone upstream
    remembered to ask is not disabled — it is merely unused so far.

    It is INJECTED rather than read here on purpose. The definition of
    "on" belongs to `control/publisher.py` (`RUN_002_PUBLISH_INDEPENDENT_REVIEW`
    exactly equal to "1"), and a second definition in a second module is
    how two switches drift into disagreeing. This module holds no policy.
    """
    def poster(context, state, sha):
        if not enabled():
            return False
        request, reason = build_request(repo, sha, state, context)
        if request is None:
            del reason
            return False
        ok, _ = send(request, http=http, environ=environ)
        return ok

    return poster
