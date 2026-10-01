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
| Also supersedes | the proposal's §7 R2 and runbook step 11, both of which say the publisher "does not exist". It exists — `control/publisher.py`. It still has no transport, which is the half of F7 that remains |
| Revised | 2026-10-02, after an independent review. Eight corrections are marked **CORRECTED** or **ADDED** in place; two of them (actions 3a/3b and the protection capture) change the deployment sequence |

**What approving this authorises:** the twenty actions in §9, in that
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
requirement is unsatisfiable rather than protective. The gate's **code**
executes from a pinned, read-only export, never from the pull request's
own tree — though its **git facts** necessarily come from the live
checkout, because that is the only place a real head SHA exists (§7).

**The hinge, stated plainly: this takes `main` from "a human approved
this" to "the gate approved this".** If that trade is unacceptable, reject
the arrangement rather than trimming it — every part of it exists to make
that single substitution safe.

**Three things to read before deciding, because each is a claim the code
did not originally support and now does:** the `Commit statuses: write`
permission still has **no call site** (§2); the
`blockedOnlyByPendingIndependentReview` flag **cannot tell its own missing
context from any other unmet protection rule**, though it can never cause
a merge (§4); and two adapters **deny every pull request when run from the
export** unless action 8 is built the way §7 specifies.

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
| Commit statuses | **read + write** *(no call site — see below)* | read | — |
| Pull requests | read | write | write |
| Contents | — | write *(unverified, §8)* | write |
| Administration | — | read | — |
| Actions | — | read | — |
| Issues | — | write *(unverified, §8)* | — |

**The three minima you asked for, read off the table:**

| Capability | Minimum permission | Held by |
|---|---|---|
| read CI / check results | `Checks: read` (+ `Commit statuses: read` for the rollup) | gate, supervisor |
| publish the required result | `Commit statuses: write` | **gate only** |
| execute merges | `Pull requests: write` + `Contents: write` | **supervisor only** |

**Almost every one of these is derived from a call site, and the
derivation is re-checked mechanically.** `experiment/github-app/check-templates.py`
asserts that each `gh`/API call still exists at the file and line the
permission was derived from, and fails loudly if one moves. Run it:
`python3 experiment/github-app/check-templates.py`.

> **THE ONE EXCEPTION IS THE PERMISSION THIS WHOLE ARRANGEMENT EXISTS TO
> GRANT.** `Commit statuses: write` has **no call site**, because
> `control/publisher.py` has no transport: its `poster` is a required,
> undefaulted argument and every caller in the repository is a test passing
> a simulated one. So the check cannot assert anything about it, and does
> not pretend to.
>
> This is the surviving half of finding F7. The other half is closed —
> F7 was "the permission at the centre of this proposal exists to serve
> code nobody has written", and that code is now written. What remains is
> narrower and still true: **nothing in this repository has ever posted a
> commit status, and no code here builds the `statuses/` path at all.**
> Approving this permission approves it for a transport that does not yet
> exist; building that transport is action 9.

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
re-queried** since; if live state has drifted, **the bytes captured at
action 11a win** (that action did not exist until 2026-10-02 — §3 pointed
at a capture step the action list did not contain).

| Setting | BEFORE | AFTER | Why |
|---|---|---|---|
| `required_status_checks.strict` | `true` | `true` | unchanged |
| `required_status_checks.checks` | `[{ci, app_id 15368}]` | `[{ci, app_id 15368}, {run-002/independent-review, <gate app id>}]` | the existing CI requirement is **preserved byte-identically**; a check asserts it |
| `required_approving_review_count` | `1` | **`0`** | **the one reduction.** Unsatisfiable: author and authenticated identity are both `serina-mcfall`, and GitHub forbids self-approval |
| `required_pull_request_reviews` | object | **object** (never `null`) | `control/preflight.py:174` computes `bool(reviews is not None and …)`; sending `null` fails the REQUIRED `github_main_protection` gate and `ctl start` refuses |
| `enforce_admins` | `false` | **`true`** | a protection an admin can walk past is not one. **The cost, stated:** you can no longer merge `main` by hand if the gate stalls. §11 row 2 becomes the only way out |
| `require_last_push_approval` | `false` | `false` | **a second declared restatement.** It was MISSING from the AFTER payload until an independent review walked the two documents by hand; the check that was supposed to catch it compared top-level keys only and reported "missing: []". Both are fixed — the field is restated identically, and the check is now recursive |
| `bypass_pull_request_allowances` | — | `{users: [], teams: [], apps: []}` | explicit, empty |
| `allow_force_pushes` / `allow_deletions` | `false` | `false` | unchanged |
| `restrictions` | `null` | `null` | unchanged |
| everything else | — | **restated identically** | a protection PUT replaces the WHOLE object; a field omitted is a field cleared. A check asserts AFTER restates every field BEFORE sets, **at every depth** — it compared only top-level keys until 2026-10-02, and a nested field had already slipped through |

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
2. `blockedOnlyByPendingIndependentReview === true` — the only thing the
   gate **can see** holding the pull request back is a branch-protection
   rule, **and CI is independently verified green**. Read the next
   sub-section before approving this one: the gate cannot tell *which*
   protection rule.

Anything else earns `failure`. An **unusable decision object** — not a
dict, or carrying a verdict the gate never emits — publishes **nothing at
all**, because a failure status would assert a judgement the gate did not
make.

> **CORRECTED 2026-10-02, because this paragraph used to claim more than
> the code does.** It read "an unusable decision, **or a head the gate did
> not judge**, publishes nothing at all". The second half is false.
> `live-gate.js:588` always returns a `trustedHeadSha` key, `null` when the
> head could not be resolved; `publisher.py:170` skips its mismatch guard
> when that value is `None`, so the decision falls through to `state_for`,
> comes back `DENIED`, and **a `failure` status is posted** —
> `tests/test_c20a_publisher_disabled.py::test_a_decision_with_no_trusted_head_is_still_judged`
> asserts exactly that, deliberately, so a pull request is not left with no
> report at all.
>
> Two defensible designs; the document described one and the code
> implements the other. **The code's behaviour is the one on offer here**,
> and it is the weaker claim of the two: the gate will tell GitHub
> "independent review failed" for a commit whose head it could not resolve.
> If you want the stricter "publish nothing unless the head is known",
> say so and it is a four-line change to `publisher.py` — but it must be
> decided rather than inherited from a sentence nobody checked.

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

> ### THE LIMIT OF THAT FLAG — verified 2026-10-02, and it bounds what you are approving
>
> **It cannot distinguish its own missing context from any other unmet
> protection rule.** GitHub collapses every unsatisfied protection
> requirement into one `mergeStateStatus: BLOCKED`, and
> `live-gate.js:516-523` raises exactly **one** reason code for it. The CI
> leg is checked independently, so a red `ci` is caught — but a *second*
> required context, `required_conversation_resolution`, or a restored
> approving-review requirement would be invisible, and the flag would go
> true with that rule still unmet.
>
> **What that cannot do: cause a merge.** Verified twice over.
> `verified` requires `reasons.length === 0` (`live-gate.js:544`), so the
> flag can never raise `decision` to `ELIGIBLE`; and the Python merge gate
> — `routing.evaluate_merge`, the sole merge authority — never reads the
> flag at all. Grep confirms its only reader in the repository is
> `control/publisher.py`. GitHub also still holds the pull request on the
> unmet rule.
>
> **What it can do: publish a true-sounding status that is not the whole
> truth.** The gate would assert `run-002/independent-review: success`
> while some other protection rule is unsatisfied. The consequence is a
> misleading public assertion, not a wrongful merge.
>
> **Why it is nevertheless safe as proposed, and the condition that keeps
> it so.** The AFTER protection in §3 leaves exactly two required contexts
> (`ci` and this one) and zero required approving reviews, so today there
> is no third rule for the flag to hide. **That is a load-bearing
> invariant and it is not self-enforcing.** If anyone later adds a required
> context, turns on conversation resolution, or restores the review count,
> this flag silently becomes over-permissive in what it publishes. **That
> falsification step now exists as V12**: on the throwaway, add a second
> required context and confirm the flag goes true **while the merge stays
> blocked**. Both halves must be observed — the second is the one that
> proves a true flag never produces a merge.

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
| `.git/` — refs and objects | read+write | **read, not write** | **ADDED 2026-10-02.** `git-head.js` resolves the trusted head from here, so a worker that can write refs can move the fact the gate calls trusted. Same owner/mode treatment as the source directories |
| `.github/workflows/` | read+write | **read, not write** in the main checkout | **ADDED 2026-10-02 — AND NOT CLOSED BY THE UID SPLIT.** See the box below |
| `bin/` | read+write | read, not write | listed in this table but missing from the pin's drift set; now added to `F2` |

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
> ### THE CI CHECK IS DEFINED BY THE BRANCH IT JUDGES — ADDED 2026-10-02
>
> **VERIFIED:** `apparatus/adapters/ci-result.js:155-157` matches a required
> check by **name only** — `run.name === required`. No App id, no workflow
> path, no actor. The governed list is `["ci"]`, so any check run named
> `ci` that concluded success satisfies the gate's CI leg.
>
> **VERIFIED:** `.github/workflows/ci.yml` triggers on `pull_request`, and
> the worker holds `Contents: write` and authors files freely in its own
> branch — including `.github/workflows/`.
>
> **NOT VERIFIABLE FROM THIS REPOSITORY:** whether GitHub resolves a
> `pull_request` workflow from the head branch or the base for a same-repo
> pull request. The finding does not depend on it; the name-only match is
> enough to record the risk.
>
> **The UID split does not close this**, which is why it is called out
> separately: it is a GitHub-side trust question, not a filesystem one.
> `.github/` is now inside the pin's drift set (`F2`), so the trusted
> revision's CI definition cannot move without the pin going red — but that
> governs *this* checkout, not what a pull request branch carries.
> **Recorded as an open, accepted risk for the operator to rule on, not as
> something this arrangement solves.**

> **One change the GitHub side cannot substitute for:**
> `control/worker_entry.py:180` is `env = dict(os.environ)` with no scrub
> before `Popen`. Until that is an allow-list, a worker inherits the
> Supervisor's entire environment and the boundary is not enforced
> whatever the file modes say. Names for the allow-list:
> `experiment/github-app/env-var-names.md` §3.

---

## 7. The trusted revision

> **TRUSTED APPARATUS REVISION:
> `2c7c6cf257054872171d6e8f34d6329368a056a6`**, on
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

> ### THE INVOKER TRAP — ADDED 2026-10-02, and the obvious implementation denies every pull request
>
> `apparatus/adapters/git-head.js:163` computes
> `RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..')` — **its own
> location** — and `resolveRun002TrustedHeadSha` then refuses with
> `WORKSPACE_MISMATCH` unless that equals `config/isolation.json`'s
> `workspace`, which is pinned to
> `/home/serina/wellbeing-agent-experiment/agent-run-002`.
>
> Run from `/opt/run-002/gate-<SHA>`, those can never be equal. The adapter
> returns `ok: false` → `HEAD_SHA_UNVERIFIED` → **DENIED, on every pull
> request, forever.** And the export is `chmod -R a-w`, so it cannot be
> patched in place. This is the F5 deadlock again, in a different
> component, and it would have been found on launch day.
>
> **IT IS TWO ADAPTERS, NOT ONE.** `apparatus/adapters/reviewer-identity.js:252-253`
> derives `RUNTIME_DIR` from `__dirname` the same way. `.runtime/` is
> gitignored, so `git worktree add` creates none in the export —
> `resolveRun002ReviewerIdentity` returns `LEDGER_UNREADABLE` →
> `REVIEWER_UNVERIFIED` → **also a permanent deny, also unpatchable under
> `chmod -R a-w`.** Verified independently, not inherited.
>
> The other two wrappers are **not** traps, and the distinction is the
> rule: `task-record.js` and `ci-result.js` derive roots that feed only
> **config** (`config/tasks.json`, `config/experiment.json`), and reading
> config from the pinned export is exactly right.
>
> > **THE RULE, FOR WHOEVER BUILDS ACTION 8: config from the export, facts
> > from the live checkout.**
>
> **The invoker must therefore call the low-level
> `resolveTrustedHeadSha(identity, { repoRoot })`** with the LIVE checkout
> as `repoRoot`, not the convenience wrapper — and the same for the
> reviewer adapter's low-level form.
>
> **Which forces something to be said out loud:** the gate's *code* comes
> from the immutable export, but its *git facts* necessarily come from the
> mutable checkout — there is no other place a real head SHA exists.
> `.git/` therefore needs the same ownership treatment as `.runtime/`, and
> it is listed in §6 for that reason. **That verification step now exists
> as V13**: run the invoker against a real read-only export and get a real
> SHA back, **before action 11**. Applying protection first would turn a
> broken gate into a permanent deadlock.

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
| whether `Contents: write` is needed for the supervisor alongside `Pull requests: write` | **ADDED.** Flagged in the supervisor manifest's own `unverified` list and in §8, but §2's table marked only `Issues: write` |
| **whether GitHub's GET still returns a `required_pull_request_reviews` OBJECT after a PUT with count 0** | **ADDED, and it decides whether `ctl start` works.** `control/preflight.py:174` computes `reviews is not None`; if GitHub echoes `null` once the count is zero, the REQUIRED `github_main_protection` gate fails and `ctl start` refuses. The AFTER payload sends an object, but what the GET returns is GitHub's choice. **Check this at action 12 before assuming the arrangement is live** |
| whether GitHub honours `app_id` pinning on a required context | the §1 separation leans on it; the proposal's V3c exists to test it on the throwaway |
| whether a `pull_request` workflow resolves from the head branch or the base | bears on the `.github/` risk in §6; the name-only match records the risk regardless |

---

## 9. What needs YOUR approval — the twenty actions, in order

**It was twelve until 2026-10-02.** An independent review found that the
twelve, carried out exactly as written, would have left no process
authenticating as any of the three Apps, left workers unable to push at
all, installed production credentials before any falsification ran, and
overwritten live branch protection with a reconstruction. The six added
rows are marked **ADDED**; two existing rows are marked **CORRECTED**.

**None of these has been done. Each is yours.**

| # | Action | Reversible? |
|---|---|---|
| 0 | **ADDED. Create the throwaway repository** that actions 3a and 10 use | yes — delete |
| 1 | Create OS users `run002-sup` and `run002-wrk`, group `run002` | yes |
| 2 | Create the three GitHub Apps with §2's permissions, zero events | yes — delete |
| 3a | **CORRECTED. Install all three on the THROWAWAY repository first** | yes — uninstall |
| 4 | Generate private keys; place `0400` owned by `run002-sup` | yes — revoke. **Revoke the old key BEFORE issuing a new one** — a fresh key does not invalidate its predecessor |
| 5 | Re-own `.runtime/` and the checkout per §6; `chmod 0700 .runtime/` | yes |
| 6 | Replace `worker_entry.py`'s environment pass-through with an allow-list. **The derivation is already written and tested** — `worker_entry.worker_child_env`, 10 tests. This action is the one line at the call site, plus confirming a real worker still starts | yes — code |
| 6b | **ADDED, AND THE ORIGINAL TWELVE DID NOT WORK WITHOUT IT.** Change `control/gh.py:33` to pass an explicit per-role `GH_TOKEN`. See the box below | yes — code |
| 6c | **ADDED.** Decide `origin` SSH → HTTPS for the worker's worktrees. Promoted out of §12, because after action 5 this is not optional | yes |
| 7 | Create the gate export at the §7 pin — `git worktree add --detach` | yes — delete |
| **7b** | **ADDED 2026-10-02, AND WITHOUT IT THE GATE CANNOT RUN AT ALL.** `cd <export>/apparatus && npm ci --omit=dev`, **before** the `chown`/`chmod`. See the box below | yes — delete the export |
| 7c | **Only now** `chown -R run002-sup:run002` and `chmod -R a-w` the export | yes |
| 8 | Build the gate invoker that runs `live-gate.js` from that export. **Read the §7 box on `WORKSPACE_MISMATCH` first** — the obvious implementation denies every pull request | yes — code |
| 9 | Wire `control/publisher.py` to the gate invoker and the gate credential, and give it a real transport | yes — code |
| 10 | Run the falsification plan on the THROWAWAY repository — **V1–V13**, including deliberately reproducing the F5 deadlock (V10). **V12 and V13 were added 2026-10-02**: V12 is the second-required-context check behind §4's box, and **V13 runs the gate from the read-only export, which is the step that would have caught the §7 trap.** V13 must pass before action 11 | n/a |
| 10b | **STOP GATE.** Do not proceed unless every V-step passed. The proposal had two such gates; compressing to twelve actions lost both | n/a |
| 10c | **CORRECTED. Only now install the three Apps on `serina-mcfall/wellbeing-run-002`** | yes — uninstall |
| 11a | **ADDED. GET `/repos/{owner}/{repo}/branches/main/protection` and save the bytes.** THAT file is the rollback artefact, not `branch-protection-BEFORE.json` | n/a |
| 11 | **Apply `branch-protection-AFTER.json` to `main`** | yes — the bytes from 11a |
| 12 | Re-run `ctl preflight`; confirm `github_main_protection` still PASSes. **Then separately** `gh api .../protection` and confirm by eye that `run-002/independent-review` is in `contexts` and the review count is 0 — the gate checks neither | n/a |

**Step 11 is last, and that ordering is not a preference.** Applying it
before 8–10 deadlocks every product PR permanently.

> ### ACTION 7b — THE EXPORT AS SPECIFIED COULD NOT RUN THE GATE. REPRODUCED.
>
> A real `git worktree add --detach` of the pin, loaded with `node`:
>
> ```
> Error: Cannot find module 'ajv/dist/2020'
> ```
>
> `validate.js:49` requires `ajv` at **module load**, and `live-gate.js:40`
> requires `validate.js` at its own. `node_modules/` is gitignored, so a
> worktree export contains none, and Node resolves by `__dirname` ancestry —
> `/opt/run-002/gate-<SHA>/…` walks up to `/` and finds nothing.
>
> **The gate CRASHES before deciding anything.** It fails closed, so it is
> not a safety hole — it is a 100% outage, and it would have been discovered
> on launch day. No existing test caught it because every simulated export
> contains only adapter files, which use Node built-ins exclusively.
>
> **Fixed at the packaging end, already done locally:** `ajv` moved from
> `devDependencies` to `dependencies` — it is a runtime dependency of the
> gate, not a test tool — and the lockfile regenerated so `npm ci` still
> matches. **Verified:** `npm ci --omit=dev` installs it (5 packages) and
> pulls in **no** playwright, so no browser download happens inside the
> export.
>
> **Do NOT symlink or bind-mount the live `apparatus/node_modules` into the
> export.** That reintroduces a mutable, worker-reachable dependency tree as
> the code the gate executes — the exact property the export exists to
> remove. Install, so `chmod -R a-w` freezes the dependencies too.
>
> Installing does not disturb the pin: the revision check reads `HEAD`, not
> a tree hash.

> ### ACTION 6b — WITHOUT IT, APPROVING THE REST BREAKS THE SYSTEM
>
> `control/gh.py:33` runs `subprocess.run(args, cwd=cwd, …)` with **no
> `env=`**, so every `gh` call in Run 002 inherits whatever ambient auth
> the host has. Verified by reading the function; `env-var-names.md:10-12`
> says the same.
>
> Two consequences, and the second is the one that bites:
>
> 1. **No principal ever authenticates as its App.** Creating three Apps
>    and installing them changes nothing about who `gh` acts as. The
>    separation in §1 is a property of the manifests, not of the running
>    system — and the ambient identity holds `statuses: write`, so the
>    "publisher cannot merge, merger cannot publish" property does not hold
>    in the deployed arrangement.
> 2. **After action 5, workers stop working.** Re-owning the checkout to
>    `run002-wrk` leaves them unable to read `serina`'s `0600`
>    `~/.config/gh/hosts.yml` or use `serina`'s SSH key, so they can
>    neither call `gh` nor `git push`. That is why 6c is promoted out of
>    §12's recommendations: declining the HTTPS change, after action 5,
>    means no worker can push a branch.
>
> §12 still presents 6c as a recommendation you may decline. **Declining it
> now requires a different answer to how a worker pushes**, not simply the
> status quo.

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
| The pin, and a check that fails when gate code moves under it | `check-templates.py` `F1`/`F2` — drift set now also covers `bin/` and `.github/` |
| **The worker environment allow-list** for action 6, written and tested, **deliberately not wired** — a test pins the unwired status so taking action 6 is a decision, not a drift | `control/worker_entry.py::worker_child_env`, `tests/test_c22_worker_env_allowlist.py` (10 tests) |
| **The gate invoker** (action 8, Python half): required+undefaulted export path, the live repo root refused if it is the export, the export's actual revision compared to the pin, 11 finite fail-closed outcomes, never raises | `control/gate_invoker.py` |
| **The publication path** (action 9, the join): gate invoker → publisher, same environment switch checked **before** any subprocess, no default transport. Placed in `experiment/github-app/` on purpose, so `control/` and `bin/` stay publisher-free and that guarantee survives literally | `experiment/github-app/publication_path.py` |
| Both of the above **verified as a connected path against simulated services** — real `node`, a real throwaway export, a real throwaway git repo, and a gate program the test writes. 60 tests, 10 mutations | `tests/test_c20a_publication_path.py` |
| **The sixth provenance condition**, which C-04c recorded as unmatchable. It closed a real hole: the merge path never required a `REVIEW_DISPATCHED` at all, so one appended `REVIEW_RESULT` attributed to nobody satisfied the whole code-review leg | `control/routing.py::_review_worker_attests`, `tests/test_c04c_merge_attestation.py` |
| The recursive protection-restatement check, and the nested field it immediately caught | `check-templates.py` check B, `branch-protection-AFTER.json` |
| **The gate program itself** (action 8's deployment half): envelope in on stdin, the gate's decision out verbatim, built around all three `__dirname` traps and proved by a regression test that runs it from a directory that is NOT the configured workspace | `apparatus/pr-evidence/gate-cli.js`, 29 tests |
| **The `ajv` packaging fix** for action 7b, with `npm ci --omit=dev` verified to install it and not playwright | `apparatus/package.json`, `package-lock.json` |
| **The gate process's environment allow-list** — `NODE_PATH` and `NODE_OPTIONS` both choose what code Node runs, and the spawn passed neither through a filter | `control/gate_invoker.py::gate_child_env`, 7 tests |

---

## 11. Rollback

| If | Then |
|---|---|
| protection is wrong | `PUT` **the bytes captured at action 11a**. `branch-protection-BEFORE.json` is a 2026-10-01 GET and is the FALLBACK, not the artefact — this row used to call it "the verified prior state", while §3 simultaneously warned that a fresh capture wins. Both could not be true, and no action captured anything. Action 11a is that capture |
| the gate denies everything | unset `RUN_002_PUBLISH_INDEPENDENT_REVIEW`; remove the required context from protection via the BEFORE payload |
| a credential is suspected | revoke the App's private key in GitHub settings **before** issuing a new one — a fresh key does not invalidate the old one |
| the whole arrangement is wrong | uninstall all three Apps, apply BEFORE, delete the export, restore the previous ownership of `.runtime/`. No product data is involved at any point |

**Nothing here touches Run 001, and no step in §9 is irreversible.**

---

## 12. Recommendations on the two open questions

| Question | Recommendation | Why |
|---|---|---|
| Third principal, or widen the gate App? | **Third principal** | Widening the gate App costs the property that makes V8 falsifiable: that the publisher cannot act on what it blessed |
| Move `origin` from SSH to HTTPS? | **Yes, for the worker's worktrees only** — leave your own checkout on SSH | Today the strongest write credential in the system is a personal SSH key sitting entirely outside the scheme. That is a larger hole than the one the App closes. **AND, as of 2026-10-02, it is no longer optional:** after action 5 re-owns the checkout, a worker cannot read `serina`'s SSH key or `gh` config, so declining this requires some other answer to how a worker pushes a branch. Promoted to action 6c |

The first remains yours to settle. The second is now a required action with
a recommended answer, not a free choice.
