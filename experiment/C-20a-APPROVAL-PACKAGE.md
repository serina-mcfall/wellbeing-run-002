# C-20a(C) — approval package

**One decision, complete on this page.** Everything needed to approve,
reject, or send back a line is here. `GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md`
holds the derivations and the evidence behind each claim; this document is
the decision.

| | |
|---|---|
| Date | 2026-10-02 |
| Branch | `wip/c05-1-persistence` |
| State | **NOTHING CREATED. NOTHING CHANGED. NOTHING PUBLISHED.** |
| Supersedes | handover §41.7's permission set, entirely |

**What approving this authorises:** the twelve actions in §9, in that
order. Nothing else, and nothing until you say so.

---

## 0. The one-paragraph version

Three GitHub Apps, scoped to one repository. A **gate** that can read
check results and write one commit status and nothing else. A
**supervisor** that can merge but cannot write the status it merges on. A
**worker** that can push a branch and open a pull request and touch
nothing that bears on the judgement. Branch protection on `main` gains a
required context only the gate can satisfy, and loses its human
approving-review requirement — because you are both the author and the
authenticated identity, and GitHub forbids self-approval, so that
requirement is unsatisfiable rather than protective. The gate executes
from a pinned, read-only export, never from the pull request's own tree.

**The hinge, stated plainly: this takes `main` from "a human approved
this" to "the gate approved this".** If that trade is unacceptable, reject
the arrangement rather than trimming it — every part of it exists to make
that single substitution safe.

---

## 1. Identities

| | **gate** | **supervisor** | **worker** |
|---|---|---|---|
| App name | `run-002-independent-review` | `run-002-supervisor` | `run-002-worker` |
| Runs as (OS) | `run002-sup` | `run002-sup` | **`run002-wrk`** |
| Holds a private key | yes, `0400` | yes, `0400` | **no — 1-hour minted tokens only** |
| May write a commit status | **YES, and alone** | no | no |
| May merge | no | **YES, and alone** | no |
| May push / open a PR | no | yes | yes |

**Why three and not two.** §41.7 proposed two credentials — a publisher
and a worker — and **neither of them can merge**. That was defect F2. The
split also gives the property that makes the whole scheme falsifiable:
**the principal that publishes the verdict cannot act on it, and the
principal that acts on it cannot publish it.**

---

## 2. Exact permissions

Repository-scoped, "Only select repositories" → `serina-mcfall/wellbeing-run-002`.
Never "All repositories". Webhooks disabled, zero events subscribed, all three.

| Permission | gate | supervisor | worker |
|---|---|---|---|
| Metadata | read | read | read |
| Checks | **read** | read | — |
| Commit statuses | **read + write** | read | — |
| Pull requests | read | write | write |
| Contents | — | write | write |
| Administration | — | read | — |
| Actions | — | read | — |
| Issues | — | write *(unverified, §8)* | — |

**The three minima you asked for, read off the table:**

| Capability | Minimum permission | Held by |
|---|---|---|
| read CI / check results | `Checks: read` (+ `Commit statuses: read` for the rollup) | gate, supervisor |
| publish the required result | `Commit statuses: write` | **gate only** |
| execute merges | `Pull requests: write` + `Contents: write` | **supervisor only** |

**Every one of these is derived from a call site, and the derivation is
re-checked mechanically.** `experiment/github-app/check-templates.py`
asserts that each `gh`/API call still exists at the file and line the
permission was derived from — eleven of the thirteen rows have a site, and
it fails loudly if one moves. Run it: `python3 experiment/github-app/check-templates.py`.

**F1 is resolved and shown.** §41.7's set lacked `Checks: read`, so the
gate could not read `ci` — which lives only on the check-run surface — and
would have denied every pull request forever. The gate now holds it, and
check `D` asserts `apparatus/adapters/ci-result.js:69` still calls
`check-runs`.

---

## 3. Protection on `main` — before and after

**BEFORE** is the state verified by read-only GET on 2026-10-01 and kept
byte-for-byte as the rollback artefact
(`experiment/github-app/branch-protection-BEFORE.json`). It was **not
re-queried** since; if live state has drifted, the bytes captured at
runbook step 11 win.

| Setting | BEFORE | AFTER | Why |
|---|---|---|---|
| `required_status_checks.strict` | `true` | `true` | unchanged |
| `required_status_checks.checks` | `[{ci, app_id 15368}]` | `[{ci, app_id 15368}, {run-002/independent-review, <gate app id>}]` | the existing CI requirement is **preserved byte-identically**; a check asserts it |
| `required_approving_review_count` | `1` | **`0`** | **the one reduction.** Unsatisfiable: author and authenticated identity are both `serina-mcfall`, and GitHub forbids self-approval |
| `required_pull_request_reviews` | object | **object** (never `null`) | `control/preflight.py:174` computes `bool(reviews is not None and …)`; sending `null` fails the REQUIRED `github_main_protection` gate and `ctl start` refuses |
| `enforce_admins` | `false` | **`true`** | a protection an admin can walk past is not one |
| `bypass_pull_request_allowances` | — | `{users: [], teams: [], apps: []}` | explicit, empty |
| `allow_force_pushes` / `allow_deletions` | `false` | `false` | unchanged |
| `restrictions` | `null` | `null` | unchanged |
| everything else | — | **restated identically** | a protection PUT replaces the WHOLE object; a field omitted is a field cleared. A check asserts AFTER restates every field BEFORE sets |

**Two traps the payload avoids, both checked rather than promised:** the
deprecated `contexts` alias is omitted (sending it alongside `checks` is
an error), and `run-002/independent-review` is **not** added to
`config/experiment.json`'s `required_checks` — that list is iterated
against the CHECK-RUN surface, where a commit status never appears, so
adding it there would be a permanent silent deny.

**ORDERING IS PART OF THE APPROVAL.** The protection PUT is the LAST
action, step 11. Applying it before the publisher exists deadlocks every
product PR permanently — that is defect F5, and the sequence is the fix.

---

## 4. Independent-review provenance, and SHA binding

**What the gate publishes.** Context `run-002/independent-review`, posted
against a **40-hex commit SHA**, never a branch or a ref. Exactly two
decisions earn `success`:

1. `decision === 'ELIGIBLE'` — every adapter verified, all evidence
   SHA-bound, provenance proved from the append-only ledger;
2. `blockedOnlyByPendingIndependentReview === true` — the ONLY thing
   holding the pull request back is this very context, **and CI is
   independently verified green**.

Anything else earns `failure`. An unusable decision, or a head the gate
did not judge, publishes **nothing at all** — a failure status would
assert that head was judged and found wanting, which is a different and
untrue claim.

**The second case is F5's resolution and it is the one to scrutinise.**
Once the context is required, GitHub reports `BLOCKED` until something
publishes it, and the thing that publishes it is the gate. The flag is
computed from the **reason list** being precisely one
`PR_BLOCKED_BY_BRANCH_PROTECTION`, plus the CI adapter's own `ok` —
**never** from `mergeStateStatus === 'BLOCKED'`, which GitHub also reports
for a failing `ci`. Deriving it from the raw state would publish an
independent-review pass for a pull request whose CI is red. That is not
asserted: `live-gate.test.js`'s `F5 MUTATION` test computes both
formulations over the same observation of a failing-CI pull request and
shows the rejected one would publish.

The flag is a **reported field**. It is never an input to `verified`, and
`decision` is computed before it. A merge still requires `ELIGIBLE`.

**SHA binding, end to end:**

| Link | Bound by |
|---|---|
| evidence → commit | every SHA-bearing path in the package compared to the trusted head (`verifyEvidenceShaBinding`) |
| review → commit | append-only ledger `REVIEW_RESULT` carrying `head_sha` and `agent_id` (`verifyReviewProvenance`) |
| gate verdict → commit | `trustedHeadSha` from git, cross-checked against `prView.headRefOid` |
| status → commit | posted against the SHA, and refused if it differs from `trustedHeadSha` |
| merge → commit | expected-head on the merge call (§5) |

---

## 5. Expected-head protection

The merge is executed by `PUT /repos/{owner}/{repo}/pulls/{n}/merge` with
the `sha` parameter set to the head the gate judged. GitHub refuses the
merge if the head has moved. **REST rather than a `gh pr merge` flag**: a
local hook on this host refuses all `gh pr` invocations including
`--help`, so which flag carries the expected head could not be verified,
and specifying an unverified flag would be worse than specifying the API
that is documented.

---

## 6. Worker isolation — what is enforced, and what is not yet

| Asset | `run002-sup` | `run002-wrk` | Enforced by |
|---|---|---|---|
| `~serina/.ssh/*` | no | no | existing `0700`/`0600`, owner `serina` |
| `~serina/.config/gh/hosts.yml` | no | no | existing `0600` |
| All three App private keys | read | **no** | `0400`, owner `run002-sup`; the worker gets a 1-hour token, never a key |
| Trusted gate export | read | read | `chmod -R a-w`, owner `run002-sup` |
| `apparatus/`, `control/`, `bin/`, `config/`, `prompts/`, `protocol/` | read | **read, not write** | owner `run002-sup`, group `run002`, `0750`/`0640` |
| `.runtime/` — ledger, state, evidence, locks | read+write | **no** | `.runtime/` `0700`, owner `run002-sup` |
| Its own worktree | read+write | read+write | `0700`, owner `run002-wrk` |
| Another worker's worktree | — | **reachable** | nothing — accepted, recorded |

> ### C-22 — AN OPEN LAUNCH BLOCKER, AND THE REASON THIS SECTION MATTERS
>
> **None of the above is true today.** Workers are spawned by
> `control/worker_entry.py` with no setuid, no setgid and no container, so
> they run as the UID that owns `.runtime/`. Every field the merge gate
> trusts is in a file they can write.
>
> **Reproduced, not argued:** from a blank record for a pull request with
> no code review, no security evidence and neither accessibility leg,
> writing six fields into `state.json` turns the merge gate's answer from
> `(False, NO_REVIEW_PASS)` into `(True, MERGE_OK)`.
> (`tests/test_c04c_merge_attestation.py::ThreatModelCase`.)
>
> **What was done about it locally.** C-04c added a second source: the
> merge path now requires the durable ledger to attest all four evidence
> legs at the head GitHub reports. A forgery now needs state.json **and**
> four consistent ledger appends, and must leave a permanent trace in the
> evidence record.
>
> **THIS IS TAMPER EVIDENCE, NOT PREVENTION.** `control/ledger.py` has no
> hash chain and no signature, and `ledger.jsonl` is writable by the same
> UID. **Nothing in this repository can close C-22. Only the UID split in
> this section can.** It must not be recorded as closed until
> `.runtime/` is `0700` owned by a user the workers are not.
>
> **One change the GitHub side cannot substitute for:**
> `control/worker_entry.py:113` is `env = dict(os.environ)` with no scrub
> before `Popen`. Until that is an allow-list, a worker inherits the
> Supervisor's entire environment and the boundary is not enforced
> whatever the file modes say. Names for the allow-list:
> `experiment/github-app/env-var-names.md` §3.

---

## 7. The trusted revision

> **TRUSTED APPARATUS REVISION:
> `6c535409cf1768bcfbd694f3707cb8a674f49bfe`**, on
> `wip/c05-1-persistence`. Full 40 hex, never a branch name.

**`main` is disqualified, and this was checked rather than assumed.**
`main` is `4eeaa7c`, 61 commits behind, and contains **no**
`live-gate.js`, **no** `ci-result.js`, **no** `reviewer-identity.js`,
**no** `requirement-registry.js`, **no** `gate_evidence.py`, **no**
`accessibility_services.py`. Exporting `main` would not fail closed — it
would fail to exist.

**The pin must be confirmed or re-stated at approval time.**
`check-templates.py` enforces the invariant that matters: the pin is
reachable from HEAD (`F1`), and `apparatus/ control/ protocol/ prompts/
config/` are unchanged since it (`F2`). Documentation on top of a pin is
harmless; one line of gate code is not, and `F2` goes red naming the file.
Both halves have been watched failing.

The export is created once, read-only:
`git worktree add --detach /opt/run-002/gate-<SHA> <SHA>` →
`chown -R run002-sup:run002` → `chmod -R a-w`. The gate invoker uses only
that absolute path — never a path inside the pull request's worktree, and
never `config.REPO_ROOT`, which is a mutable tree.

---

## 8. What could not be verified, and why

| Claim | Why unverified |
|---|---|
| `Issues: write` is needed for `gh pr comment` | needs the App creation screen |
| whether `Contents: write` is needed alongside `Pull requests: write` for merge | same |
| whether bare `gh pr view` needs `Checks` | needs a token; added as verification step V11 |
| which `gh pr merge` flag carries the expected head | a local hook refuses all `gh pr` invocations, including `--help`. REST specified instead |
| whether an App is already installed | needs App authentication |
| live branch-protection state on 2026-10-02 | deliberately not re-queried; §3's BEFORE is the 2026-10-01 GET |

---

## 9. What needs YOUR approval — the twelve actions, in order

**None of these has been done. Each is yours.**

| # | Action | Reversible? |
|---|---|---|
| 1 | Create OS users `run002-sup` and `run002-wrk`, group `run002` | yes |
| 2 | Create the three GitHub Apps with §2's permissions, zero events | yes — delete |
| 3 | Install all three on `serina-mcfall/wellbeing-run-002` only | yes — uninstall |
| 4 | Generate private keys; place `0400` owned by `run002-sup` | yes — revoke |
| 5 | Re-own `.runtime/` and the checkout per §6; `chmod 0700 .runtime/` | yes |
| 6 | Replace `worker_entry.py`'s environment pass-through with an allow-list | yes — code |
| 7 | Create the read-only gate export at the §7 pin | yes — delete |
| 8 | Build the gate invoker that runs `live-gate.js` from that export | yes — code |
| 9 | Wire `control/publisher.py` to the gate invoker and the gate credential | yes — code |
| 10 | Run the falsification plan (V1–V11) on a THROWAWAY repository, including deliberately reproducing the F5 deadlock | n/a |
| 11 | **Apply `branch-protection-AFTER.json` to `main`** | yes — `branch-protection-BEFORE.json` |
| 12 | Re-run `ctl preflight`; confirm `github_main_protection` still PASSes with `required_approving_review_count: 0` | n/a |

**Step 11 is last, and that ordering is not a preference.** Applying it
before 8–10 deadlocks every product PR permanently.

---

## 10. What is already done, locally, and needs nothing from you

| | Where |
|---|---|
| The corrected permission set, derived per call site and mechanically re-checked | `experiment/github-app/app-manifest-*.json`, `check-templates.py` |
| Protection BEFORE and AFTER as applyable request bodies | `experiment/github-app/branch-protection-*.json` |
| Environment variable NAMES for both identities — names only, never values | `experiment/github-app/env-var-names.md` |
| **The publisher**, written and **disabled**: no default transport, enabled only by an exact environment variable, nothing imports it | `control/publisher.py`, 23 tests |
| **F5's resolution** in the gate, with the fail-open alternative demonstrated | `live-gate.js`, `live-gate.test.js` |
| **C-04c**, the ledger cross-check that narrows C-22 without closing it | `control/routing.py`, `control/supervisor.py`, 26 tests |
| The pin, and a check that fails when gate code moves under it | `check-templates.py` `F1`/`F2` |

---

## 11. Rollback

| If | Then |
|---|---|
| protection is wrong | `PUT` `branch-protection-BEFORE.json`. It is the verified prior state, kept byte-for-byte |
| the gate denies everything | unset `RUN_002_PUBLISH_INDEPENDENT_REVIEW`; remove the required context from protection via the BEFORE payload |
| a credential is suspected | revoke the App's private key in GitHub settings **before** issuing a new one — a fresh key does not invalidate the old one |
| the whole arrangement is wrong | uninstall all three Apps, apply BEFORE, delete the export, restore the previous ownership of `.runtime/`. No product data is involved at any point |

**Nothing here touches Run 001, and no step in §9 is irreversible.**

---

## 12. Recommendations on the two open questions

| Question | Recommendation | Why |
|---|---|---|
| Third principal, or widen the gate App? | **Third principal** | Widening the gate App costs the property that makes V8 falsifiable: that the publisher cannot act on what it blessed |
| Move `origin` from SSH to HTTPS? | **Yes, for the worker's worktrees only** — leave your own checkout on SSH | Today the strongest write credential in the system is a personal SSH key sitting entirely outside the scheme. That is a larger hole than the one the App closes |

Both remain yours to settle; they are recommendations, not decisions.
