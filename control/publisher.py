"""The independent-review publisher — BUILT, TESTED, AND DISABLED.

WHAT THIS IS. C-20a(C)'s proposal §9.2 gives the gate principal one job
that nothing else in this system may do: post the
`run-002/independent-review` commit status against a trusted head SHA,
and only when `apparatus/pr-evidence/live-gate.js` says that head has
earned it. §7 R2 recorded it as F7 — "the permission at the centre of
this entire proposal exists to serve code that has not been written".
This is that code.

WHAT THIS IS NOT, AND THE FILE ENFORCES IT RATHER THAN PROMISING IT.

  * IT IS DISABLED. `enabled()` is False unless the environment variable
    `RUN_002_PUBLISH_INDEPENDENT_REVIEW` is exactly "1". There is no
    config-file switch, deliberately: a committed flag is one edit away
    from being on, and `config/` is inside the trusted-revision pin, so
    flipping it would also move the pin. An environment variable has to
    be set by whoever runs the process, every time.
  * IT HAS NO DEFAULT TRANSPORT. `poster` is a required argument with no
    default, so this module cannot reach GitHub by accident. The same
    discipline the C-04 adapters use, and for the same reason: a default
    entrypoint is how "we never called it" becomes false.
  * NO APP, CREDENTIAL OR INSTALLATION EXISTS. Nothing here creates one.
    It names an environment variable that holds a token; it never reads,
    logs or echoes the value.
  * NOTHING CALLS THIS. It is not wired into the Supervisor, the tick, or
    any entry point. Deployment is C-20a(C) and remains unapproved.

THE DECISION IT ACTS ON. The gate returns `blockedOnlyByPendingIndependentReview`
when the ONLY thing holding a pull request back is the required context
this publisher has not posted yet, and CI is independently verified green.
That flag — and `decision === 'ELIGIBLE'` — are the two states that earn a
success status. Everything else earns failure or nothing at all. The flag
is computed in live-gate.js from the reason list, never from
`mergeStateStatus`; see the comment there for why that distinction is the
whole safety argument.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The required context, and the one name branch protection will require.
# It is NOT in config/experiment.json's `required_checks`: that list is
# iterated against the CHECK-RUN surface by apparatus/adapters/ci-result.js,
# where a commit STATUS never appears, so adding it there is a permanent
# silent deny (proposal §4.3a).
CONTEXT = "run-002/independent-review"

# Presence-only. The value is a token and is never read by this module.
TOKEN_ENV = "RUN_002_GATE_APP_TOKEN"
ENABLE_ENV = "RUN_002_PUBLISH_INDEPENDENT_REVIEW"

SUCCESS = "success"
FAILURE = "failure"

# Finite outcomes. Prose is for humans; these are what the ledger records.
PUBLISHED = "PUBLISHED"
DISABLED = "PUBLISHER_DISABLED"
NO_CREDENTIAL = "PUBLISHER_CREDENTIAL_ABSENT"
UNUSABLE_DECISION = "DECISION_UNUSABLE"
UNUSABLE_HEAD = "HEAD_UNUSABLE"
HEAD_MISMATCH = "DECISION_HEAD_MISMATCH"
POST_FAILED = "STATUS_POST_FAILED"

_SHA = "0123456789abcdef"


@dataclass(frozen=True)
class Publication:
    """What one publish attempt did. Exactly one of posted or reason."""

    posted: bool
    state: str | None
    reason: str
    context: str = CONTEXT
    head_sha: str | None = None

    def as_dict(self) -> dict:
        return {"posted": self.posted, "state": self.state,
                "reason": self.reason, "context": self.context,
                "head_sha": self.head_sha}


def _refused(reason: str, head_sha: str | None = None) -> Publication:
    return Publication(posted=False, state=None, reason=reason,
                       head_sha=head_sha)


def enabled(environ=None) -> bool:
    """Whether publication is switched on. DEFAULT FALSE.

    Exactly "1". Not "true", not "yes", not any non-empty string - a
    permissive parse is how a stray value turns a disabled publisher on.
    """
    env = os.environ if environ is None else environ
    return env.get(ENABLE_ENV) == "1"


def credential_present(environ=None) -> bool:
    """Whether a token is present. BY NAME ONLY — the value is never read.

    Returns a boolean. It does not return, log or compare the value, and
    nothing in this module ever does.
    """
    env = os.environ if environ is None else environ
    return bool(env.get(TOKEN_ENV))


def _is_sha(value) -> bool:
    return (isinstance(value, str) and len(value) == 40
            and all(c in _SHA for c in value))


def state_for(decision) -> tuple[str | None, str]:
    """The commit-status state one gate decision earns, or None.

    (state, reason). `None` means publish NOTHING: the gate could not
    reach a usable judgement, and an absent status is the correct report
    for that - a failure status would assert this head was judged and
    found wanting, which is a different and untrue claim.

    SUCCESS requires `decision == 'ELIGIBLE'`, or the gate's explicit
    `blockedOnlyByPendingIndependentReview`. Those are the only two. In
    particular a DENIED decision with a long reason list earns FAILURE,
    never success, however sympathetic the reasons look.
    """
    if not isinstance(decision, dict):
        return None, UNUSABLE_DECISION
    verdict = decision.get("decision")
    if verdict == "ELIGIBLE":
        return SUCCESS, ""
    if verdict != "DENIED":
        # An unrecognised verdict is not a denial - it is an unusable
        # answer, and publishing a failure for it would report a judgement
        # the gate did not make.
        return None, UNUSABLE_DECISION
    if decision.get("blockedOnlyByPendingIndependentReview") is True:
        return SUCCESS, ""
    return FAILURE, ""


def publish(decision, head_sha: str, *, poster, environ=None) -> Publication:
    """Post the independent-review status for one head. NEVER RAISES.

    `poster(context, state, sha)` is REQUIRED and undefaulted - the whole
    external edge, injected. Every test passes a simulated one; nothing in
    this repository passes a real one.

    Order matters and is deliberate: disabled first, then credential, then
    the inputs, then the decision. A disabled publisher must not even
    report on whether a credential exists, because the answer is not its
    business when it is switched off.
    """
    if not enabled(environ):
        return _refused(DISABLED, head_sha if _is_sha(head_sha) else None)
    if not credential_present(environ):
        return _refused(NO_CREDENTIAL)
    if not _is_sha(head_sha):
        return _refused(UNUSABLE_HEAD)
    if not isinstance(decision, dict):
        return _refused(UNUSABLE_DECISION, head_sha)

    # The gate's own trusted head must be the head being published for.
    # Publishing a verdict against a commit the gate did not judge is the
    # single worst thing this module could do, so it is checked before the
    # verdict is even looked at.
    trusted = decision.get("trustedHeadSha")
    if trusted is not None and trusted != head_sha:
        return _refused(HEAD_MISMATCH, head_sha)

    state, why = state_for(decision)
    if state is None:
        return _refused(why or UNUSABLE_DECISION, head_sha)

    # A SUCCESS must name the head the gate actually judged.
    #
    # The guard above compares `trusted` to `head_sha` only when `trusted`
    # is not None, because a DENIED decision that could not resolve a head
    # still earns a truthful `failure` - that is deliberate and tested.
    # But the same absence on the SUCCESS path means something entirely
    # different: it blesses a commit the gate never resolved.
    #
    # MEASURED 2026-10-02, not imagined: one decision object carrying
    # `trustedHeadSha: null` and `blockedOnlyByPendingIndependentReview:
    # true` posted `success` against TWO DIFFERENT heads from the same
    # object. Today's live-gate.js cannot emit that pair - a null head
    # adds HEAD_SHA_UNVERIFIED, so the reason list is longer than one and
    # the flag is false - but that is an unstated invariant of a program in
    # another language, and this module already refuses to take the flag
    # itself on trust two lines below. It should not take this on trust
    # either.
    if state == SUCCESS and not _is_sha(trusted):
        return _refused(UNUSABLE_HEAD, head_sha)

    try:
        ok = poster(CONTEXT, state, head_sha)
    except Exception:          # noqa: BLE001 - a transport fault is finite
        return _refused(POST_FAILED, head_sha)
    if not ok:
        return _refused(POST_FAILED, head_sha)
    return Publication(posted=True, state=state, reason=PUBLISHED,
                       head_sha=head_sha)
