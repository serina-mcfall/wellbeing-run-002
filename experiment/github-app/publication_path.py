"""The publication path: gate invoker -> publisher. Approval action 9.

WHAT THIS IS. C-20a(C) §9 action 9 is "wire `control/publisher.py` to the
gate invoker and the gate credential". This is that join, and it is the
ONLY module in this repository that imports the publisher.

WHY IT LIVES HERE AND NOT IN `control/`. `control/` and `bin/` are the
ordinary execution paths: the Supervisor tick, the CLI, the entry scripts.
`tests/test_c20a_publisher_disabled.py` asserts that NOTHING in either
directory references the publisher, and that assertion is the strongest
statement anyone can make about this system — not "publication is
configured off" but "no path through the running control plane arrives at
publication at all". Putting the join in `control/` would spend that
guarantee to buy an import convenience.

So it sits with the other things C-20a(C) has prepared and not deployed:
`app-manifest-*.json`, `branch-protection-*.json`, `env-var-names.md`,
`check-templates.py`. It is in a directory whose name contains a hyphen,
so it is not even importable as `experiment.github_app.publication_path` —
reaching it takes a deliberate `importlib` load against an explicit file
path. That awkwardness is the correct amount of friction for an unapproved
deployment artefact.

THE SAME SWITCH, CHECKED FIRST. `publisher.enabled()` governs this module
too — the identical environment variable, read through the publisher's own
function rather than re-parsed here, so there is exactly one definition of
"on" and it is the strict one (`== "1"`, never truthiness). It is checked
BEFORE the gate is invoked, so a disabled publication path starts no
subprocess either. Publication is the effect being withheld; running the
gate to find out what it would have published is still work the switch is
meant to prevent.

NO DEFAULT TRANSPORT. `poster` is required and undefaulted, exactly as in
`control/publisher.py`, and nothing in this repository passes a real one.

THERE IS NO TRANSPORT IN THIS REPOSITORY, AND THAT IS A DELIBERATE GAP.
No code here constructs the commit-status request. Three reasons, in order
of weight:

  1. The approval package specifies the PERMISSION (§2: `Commit statuses:
     write`, gate App only) but never the REQUEST. §5 writes out the merge
     endpoint in full; nothing writes out the status one. A transport
     would therefore be written from memory against an App that does not
     exist, whose permissions §8 records as unverified.
  2. It cannot be tested. A real socket cannot take part in a verification
     with simulated services, so writing it would add the only untested
     code in this chain at exactly the point where a mistake posts a false
     pass against a real commit.
  3. `experiment/github-app/check-templates.py` check E greps every
     tracked `.py`/`.js` outside `experiment/` and asserts no code anywhere
     posts a commit status. §10 cites that check as live evidence that F7
     is contained. Writing the transport inside `experiment/`, where the
     grep does not look, would keep the check green while making its claim
     false — which is worse than the gap.

The seam is here instead, fully exercised: `poster(context, state, sha) ->
bool`. Supplying one is deployment.

WHAT IS STILL MISSING ABOVE THIS MODULE. Nothing assembles a gate request
in production. `evaluateLiveMergeEligibility` needs an identity, a PR
number, the submitted evidence package, one `gh pr view` observation and
the decoded ledger, plus four adapters; the Supervisor's merge path uses
`control/routing.py::evaluate_merge` and never calls live-gate.js at all.
`gate_request` is therefore a required argument taken whole from the
caller, and no part of this module pretends to build one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from control import gate_invoker, publisher   # noqa: E402

# Finite outcomes this module adds. Everything else it reports is a token
# that `gate_invoker` or `publisher` already defined, passed through
# unaltered so one reason string always has one meaning.
GATE_UNUSABLE = "GATE_UNUSABLE"


@dataclass(frozen=True)
class Attempt:
    """One publication attempt, end to end.

    `gate_outcome` is always a `gate_invoker` token once the gate ran, and
    None when it did not — which is how a disabled run is distinguishable
    from a run whose gate failed.
    """

    posted: bool
    state: str | None
    reason: str
    gate_outcome: str | None = None
    head_sha: str | None = None

    def as_dict(self) -> dict:
        return {"posted": self.posted, "state": self.state,
                "reason": self.reason, "gate_outcome": self.gate_outcome,
                "head_sha": self.head_sha}


def publish_independent_review(gate_request, head_sha: str, *, export_root,
                               expected_revision, live_repo_root, poster,
                               runner=None, environ=None,
                               timeout: int = gate_invoker.TIMEOUT) -> Attempt:
    """Judge one head with the trusted gate, then publish the result.

    NEVER RAISES, and makes no call of any kind when the switch is off.

    Every external edge is a required, undefaulted argument: where the
    trusted code is (`export_root`), which commit it must be
    (`expected_revision`), which checkout its git facts come from
    (`live_repo_root`), and what posts the status (`poster`). None of the
    four has a sensible default, and inventing one for any of them is how
    "we never called it" becomes false.

    `export_root` and `live_repo_root` are two different places on purpose
    and the invoker refuses them if they are the same one. The code is
    immutable and pinned; the facts - the head SHA, the worktree register,
    the append-only ledger - exist only in the mutable checkout. See THE
    INVOKER TRAP in `control/gate_invoker.py`.

    `head_sha` is the head publication is REQUESTED for, and it is kept
    separate from the gate's own `trustedHeadSha` on purpose: the publisher
    refuses when they differ. A gate that judged commit X cannot bless
    commit Y, and collapsing the two values into one would delete the only
    check that notices.
    """
    if not publisher.enabled(environ):
        return Attempt(posted=False, state=None, reason=publisher.DISABLED)
    if not publisher.credential_present(environ):
        return Attempt(posted=False, state=None,
                       reason=publisher.NO_CREDENTIAL)

    run = gate_invoker.invoke_gate(
        gate_request, export_root=export_root,
        expected_revision=expected_revision, live_repo_root=live_repo_root,
        runner=runner, timeout=timeout)
    if not run.ok or run.decision is None:
        # The gate reached no usable judgement. Publishing a FAILURE here
        # would assert this head was judged and found wanting, which is a
        # different and untrue claim - the same distinction publisher.py
        # draws for an unusable decision.
        return Attempt(posted=False, state=None, reason=GATE_UNUSABLE,
                       gate_outcome=run.outcome)

    # The decision is handed on EXACTLY as the gate emitted it. Nothing
    # here reads `reasons`, re-derives
    # `blockedOnlyByPendingIndependentReview`, or consults
    # `mergeStateStatus`. live-gate.js computes that flag from the reason
    # list plus the CI adapter's own `ok`, and its `F5 MUTATION` test shows
    # the obvious alternative would publish a pass for a red build. A
    # second opinion formed here would be formed without the CI leg.
    result = publisher.publish(run.decision, head_sha, poster=poster,
                               environ=environ)
    return Attempt(posted=result.posted, state=result.state,
                   reason=result.reason, gate_outcome=run.outcome,
                   head_sha=result.head_sha)
