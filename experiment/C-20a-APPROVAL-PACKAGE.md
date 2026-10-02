# C-20a(C) — approval package

**One decision, complete on this page.** Everything needed to approve,
reject, or send back a line is here. `GITHUB-APP-WORKER-ISOLATION-PROPOSAL.md`
holds the derivations and the evidence behind each claim; this document is
the decision.

| | |
|---|---|
| Date | 2026-10-02 |
| Branch | `wip/c05-1-persistence` |
| State | **NOTHING CREATED. NOTHING CHANGED. NOTHING PUBLISHED.** Stage 1 is PREPARED, **not approved** |
| Supersedes | handover §41.7's permission set, entirely |
| Also supersedes | the proposal's §7 R2 and runbook step 11, both of which say the publisher "does not exist". It exists — `control/publisher.py` — and so does its transport (`status_transport.py`). **F7 is closed.** What replaces it is narrower and still true: no request has ever been sent |
| Revised | 2026-10-02, across three passes: an independent review of this document, an adversarial review of the code it describes, and an attempt to actually RUN the export. Every change is marked **CORRECTED**, **ADDED** or **UPDATED** in place. Four change the deployment sequence: throwaway-first installation (3a/10c), the protection capture (11a), the dependency install (7b), and `gh.py`'s token (6b) |

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

**Four things to read before deciding. Each was a claim this document
made that the code did not support, found by checking rather than by
reasoning:** the `blockedOnlyByPendingIndependentReview` flag **cannot
tell its own missing context from any other unmet protection rule**,
though it can never cause a merge (§4); two adapters **deny every pull
request when run from the export** unless action 8 is built the way §7
specifies; the export **cannot load the gate at all** without action 7b,
reproduced as `Cannot find module 'ajv/dist/2020'`; and the transport now
exists but **has never sent a request**, which is what `Commit statuses:
write` is being approved for (§2).

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
| Commit statuses | **read + write** *(call site: `status_transport.py` — see below)* | read | — |
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

> **THE PERMISSION THIS WHOLE ARRANGEMENT EXISTS TO GRANT NOW HAS A CALL
> SITE — UPDATED 2026-10-02.** `Commit statuses: write` is exercised by
> `experiment/github-app/status_transport.py`, the only module in Run 002
> that builds a GitHub commit-status request. Its contract is taken from
> GitHub's own documentation, read 2026-10-02:
>
> ```
> POST /repos/{owner}/{repo}/statuses/{sha}
> Accept: application/vnd.github+json
> X-GitHub-Api-Version: 2026-03-10
> body {state, context, description?, target_url?}
> state ∈ {"error","failure","pending","success"}   201 Created
> ```
>
> **F7 IS NOW FULLY CLOSED AS A "NOBODY WROTE IT" FINDING.** The permission
> serves real code. What replaces it is a narrower and more honest
> statement: **no request has ever been sent.** `http` is a required,
> undefaulted argument; the module imports no `urllib`, `requests`,
> `socket` or `httpx`; nothing imports the module; and the enable switch is
> a required injected callable, re-read per call. Four mechanical checks
> (`E2`) assert each of those, and all four were proved red by mutation.
>
> **`check-templates.py` check E changed its claim rather than being
> relaxed.** It used to assert "no code anywhere constructs a commit-status
> API call", which this transport makes false. Because the check greps
> outside `experiment/`, leaving it alone would have kept it GREEN while
> its stated claim was false — which is worse than red, and was refused on
> exactly those grounds once before. E now asserts what survives: no
> ORDINARY EXECUTION PATH builds the call. E2 asserts what keeps the
> transport inert.

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

> ### WHAT THE FLAG ESTABLISHES, AND WHAT IT DOES NOT — REWRITTEN 2026-10-02
>
> **It establishes eligibility to PUBLISH this gate's own
> independent-review result for this head. Nothing else.** It is not a
> claim that every branch-protection condition is satisfied, and it must
> not be read as one: GitHub collapses every unsatisfied protection rule
> into one `mergeStateStatus: BLOCKED`, and `live-gate.js:516-523` raises
> exactly one reason code for it, so the flag cannot tell its own missing
> context from a second required check, an unresolved conversation, or a
> restored approving-review requirement.
>
> **THE SAFETY ARGUMENT NO LONGER RESTS ON "EXACTLY TWO CONTEXTS."** The
> earlier wording argued the flag was safe because §3's AFTER protection
> leaves only `ci` and this one, so there was no third rule to hide. That
> is a shape **nothing enforces** — anyone adding a required check would
> have silently invalidated it. It has been replaced by an argument that
> holds for any number of requirements:
>
> 1. **Publishing is a side effect on a status context. It merges
>    nothing.**
> 2. **Merging requires `routing.evaluate_merge` to allow**, and that
>    function denies on `mergeStateStatus == BLOCKED` *without ever reading
>    the flag* — it cannot read it. The flag is not among its parameters
>    and the string does not occur in `control/routing.py` at all.
>
> **Both halves are now TESTED, not asserted** —
> `tests/test_c20c_publish_eligibility_is_not_merge_eligibility.py`
> publishes the status for a blocked pull request and then asks the merge
> authority, which refuses; adds five unrelated satisfied contexts and an
> unmet extra required check, and it still refuses; and asserts
> structurally that the merge gate cannot reference the flag. Mutation:
> making `routing.py` mention the flag turns that last test red.
>
> **V12 remains worth running** — on the throwaway, add a second required
> context and confirm the flag goes true while the merge stays blocked —
> because it observes the behaviour against real GitHub rather than against
> this repository's model of it.

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

> **IMPLEMENTED 2026-10-02, at `47f35f5`. Until then this section
> described a protection the code did not have.** `control/gh.py::merge`
> ran `gh pr merge <n> --repo <r> --squash --delete-branch`, with no head
> anywhere in it: every other link in §4's table was bound to a commit and
> the merge alone was bound to a pull request NUMBER. A push landing
> between the gate's verdict and the call merged a head nothing had
> judged, caught only by `HEAD_SHA_CHANGED` on the NEXT tick — after the
> merge, and a merge is irreversible.
>
> It now sends exactly the call above, with `expected_head` a **required
> keyword** so no call site can omit it, and the supervisor passes
> `pr["headRefOid"]` — GitHub's observation under the merge transaction's
> lock, never the forgeable `record["reviewed_head"]`. `--delete-branch`
> has no REST equivalent on that endpoint, so the ref deletion is a
> second, best-effort call; both are covered by the supervisor manifest's
> existing `contents: write`, and no permission changed.
>
> **This is a COMPONENT test, not a deployed one.** 13 tests, proved able
> to fail. That GitHub answers 409 to a stale `sha` is documented
> behaviour and is **not** asserted anywhere here — no network call has
> ever been made from this repository. The first real exercise is Stage 1.

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
| Another worker's worktree | — | **reachable** | nothing — **UNRESOLVED**, recorded. No operator acceptance exists |
| `<main>/.git/objects/`, `.git/refs/heads/run-002/`, its own worktree gitdir | read+write | **read+write** | **what SHIPS.** A worker cannot commit without all three — measured |
| `<main>/.git/hooks/`, `.git/config` | read+write | **no** | `0750`, owner `run002-sup`. A hook is code the Supervisor's git runs. Verified a commit succeeds with `hooks/` read-only |
| `<main>/.git/worktrees/gate-<SHA>/` — the export's gitdir | read+write | **no** | `0700`, owner `run002-sup`. `export_revision` reads its `HEAD` to prove the export is at the pin |
| another worker's branch ref | — | **reachable** | **UNRESOLVED, and not a launch blocker.** POSIX cannot scope writes per ref. A moved local ref cannot produce an unreviewed merge: `evaluate_merge` denies `HEAD_SHA_CHANGED`/`HEAD_SHA_UNVERIFIABLE` against the head GitHub reports. Under the single `run002-wrk` identity peers reach each other's files regardless |
| `.github/workflows/` | read+write | **read, not write** in the main checkout | **ADDED 2026-10-02 — AND NOT CLOSED BY THE UID SPLIT.** See the box below |
| `bin/` | read+write | read, not write | listed in this table but missing from the pin's drift set; now added to `F2` |

> ### WHAT SHIPS FOR RUN 002, AND WHAT IS BUILT BUT DEFERRED — 2026-10-02
>
> **SHIPPING: linked worktrees with the narrowed ownership above.** This is
> what `control/supervisor.py` actually does today (six `acquire_worktree`
> call sites) and what the suite exercises.
>
> **BUILT, TESTED, NOT WIRED, DEFERRED TO RUN 003: a separate clone per
> worker** (`control/worker_git.py`, 41 tests). It is strictly better —
> under a clone a worker needs no write anywhere under `<main>/.git` — but
> wiring it touches ten Supervisor call sites plus a new fetch in the
> Builder→Fixer handoff, immediately before deployment, **for a property
> that cannot be achieved anyway under the single `run002-wrk` identity
> this section specifies** (V14e cannot pass). Run 002 is a bounded
> experiment; this is the kind of hardening Run 003 is for.
>
> **Two measurements from that work are kept, because they would have
> caused a real incident either way:**
>
> 1. **A default local `git clone` shares the object file's INODE.**
>    Verified: `chmod 600` on a clone's object changed the **source's** mode
>    to `600`. Any future clone-based deployment must use `--no-hardlinks`.
> 2. **Approved action 6c has a defect on this host.** `~/.gitconfig`
>    carries `url.git@github.com:.insteadOf = https://github.com/`.
>    Verified: after `git remote set-url origin https://…`,
>    `git config remote.origin.url` reads **HTTPS** while
>    `git remote get-url` and `--push` both resolve to **SSH** — the key
>    action 5 exists to take away. **V14f is how you find out before a
>    worker pushes as `serina`.**

> ### §6's GIT ROW WAS UNIMPLEMENTABLE AS A LINKED WORKTREE — ADDED 2026-10-02
>
> This section proposed `.git/` as **read, not write** for `run002-wrk`.
> **Applied as written, no worker could make a single commit** and the
> factory would not run at all. Measured on a disposable repository, not
> reasoned about — `tests/test_c22_worker_git_writes.py`:
>
> | What was made read-only | What the worker got |
> |---|---|
> | `.git/objects/` | `git add` → *"error: insufficient permission for adding an object to repository database"* |
> | `.git/refs/` | `git commit` → *"fatal: cannot lock ref 'HEAD': Permission denied"* |
> | `.git/hooks/` | **commit succeeds** — this one CAN be withheld |
>
> **Where a worker's git writes actually land.** A linked worktree's
> `.git` is a FILE containing `gitdir: <main>/.git/worktrees/<name>`. So a
> commit made inside a worker's worktree writes **into the main
> repository**: objects to the shared store, HEAD and index to that
> worktree's gitdir, and the branch ref under `.git/refs/heads/run-002/`.
> There is no such thing as a worker that commits without writing to
> `<main>/.git`.
>
> **What this means for the arrangement.** The directories a worker must
> write are narrower than `.git/`, so the two that matter to the gate can
> still be withheld: the **gate export's gitdir** (the pin proof) and
> **`hooks/`/`config`** (code the Supervisor's git executes). That is the
> corrected table above, and it permits every operation the factory needs.
>
> **ONE RESIDUAL, AND IT IS UNRESOLVED RATHER THAN ACCEPTED.** POSIX modes
> cannot scope write access per ref. A worker that can create its own
> `run-002/<task>` branch can also move another task's branch in the same
> directory. It is **narrowed, not closed**, by the gate anchoring on the
> head GitHub reports and cross-checking it against git: a locally moved
> ref that disagrees with GitHub produces `HEAD_SHA_UNVERIFIED` or
> `PR_HEAD_MOVED` and denies. Per-ref isolation would need a different
> mechanism — separate clones per worker, or a git wrapper — and neither is
> in this proposal. **No operator acceptance exists for this residual.**
>
> **THIS IS A PERMISSION SIMULATION, NOT AN IDENTITY VERIFICATION.** The
> measurements above ran as ONE user, using `chmod` on a throwaway
> repository. They establish which paths git must write. They do **not**
> establish how `run002-wrk` behaves against files owned by `run002-sup` —
> group membership, umask and any root-owned process can all differ.

> ### HOW A SUPERVISOR-CREATED WORKTREE BECOMES WORKER-WRITABLE — DECIDED 2026-10-02
>
> The table above says what the worker must be able to write. It did not
> say **how**, and `control/worker_git.py`'s "WHO MAKES THE CLONE" note left
> the choice open between a root helper at dispatch and a setgid
> group-writable root. **The second is chosen, applied to linked worktrees.
> No root helper runs at dispatch and no `sudo` rule is added.**
>
> **Three parts, and all three are required.** Measured in
> `tests/test_c22_shared_dispatch_modes.py` (8 tests; the two load-bearing
> parts were watched failing):
>
> | Part | What | Without it |
> |---|---|---|
> | 1 | `git config core.sharedRepository group` on the checkout | New object fan-out directories are `0755`; the worker cannot add an object the Supervisor's git created a directory for |
> | 2 | A one-time `chmod -R g+ws` of `.git/objects`, `.git/refs`, `.git/logs` | Part 1 is **not retroactive** — these exist at `0755` from `git init` and every commit touches them |
> | 3 | `umask 0002` in `bin/supervisor.sh` | The checked-out files come out `0644` and the worktree gitdir `2755`; the worker cannot write its own worktree |
>
> **Do not substitute `git init --shared=group`.** On an existing
> repository it repairs no existing mode and rewrites the config value to
> the numeric form. Both measured.
>
> **THE WORKER WORKTREE ROOT IS OUTSIDE `<WS>`.** Worker worktrees are
> created by `workmux add`; workmux 0.1.231's default `worktree_dir` is the
> **sibling** `<project>__worktrees`, and this host has no global and no
> repository-local override. So the root is
> `/home/serina/wellbeing-agent-experiment/agent-run-002__worktrees`, it
> does not exist yet, and its parent is `drwxr-xr-x serina:serina` — which
> means **`run002-sup` cannot create it** and the first dispatch would fail
> outright. A5 pre-creates it `2770 run002-sup:run002`. `control/config.py`'s
> `WORKTREE_ROOT` points somewhere else entirely and must not be used here.
>
> **WHAT THIS COSTS, AND IT IS NOT NEW.** Group-writable `objects`, `refs`
> and `logs` are writable by every member of `run002`. Under the single
> `run002-wrk` identity this section deploys, peers already share a uid, so
> the arrangement **adds no reachability that did not already exist**. It is
> the same residual recorded above as UNRESOLVED, and it is why **V14e is
> NOT APPLICABLE** rather than passed.
>
> **STILL A SIMULATION.** Everything above was measured as one user, on a
> disposable repository. **V14b/V14c/V14d under the real identities are what
> establish it**, and V14 no longer performs a `chown` that would have
> masked the question.
> **Confirming it under the real identities is V-step work after action 1**,
> and is listed as V14 below. Nothing here creates a user or changes an
> owner.

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
> **THE GOVERNING REQUIREMENT** is `config/experiment.json`'s
> `github.required_checks = ["ci"]`, which `routing.evaluate_merge` — the
> sole merge authority — asks `gh.checks_state` about and merges on the
> answer. Protocol v2 §"PR contract" states only "CI validates the schema".
>
> **THREE WAYS A PRODUCT PR COULD SATISFY IT WITHOUT CI PASSING, ALL
> REPRODUCED 2026-10-02, TWO NOW CLOSED:**
>
> | | Was | Now |
> |---|---|---|
> | **Skip** — the `ci` job concludes `SKIPPED` | satisfied the gate | **refused** |
> | the `ci` job concludes `NEUTRAL` | satisfied the gate | **refused** |
> | **Replace** — a commit STATUS whose context is `ci` | satisfied the gate, because `checks_state` keyed on `name` OR `context` | **refused — a status is not a check run** |
> | **Modify** — the PR's branch redefines the workflow, still emits a green run named `ci` | satisfied the gate | **STILL SATISFIES IT. OPEN** |
>
> The remedy was the smallest one available and invented no new rule:
> `apparatus/adapters/ci-result.js` has always required `success` exactly
> on the check-run surface, so the Python path was brought to the rule the
> JavaScript gate already applied. Detection of "is this a check run" is
> **structural** — a check run carries `conclusion`, a status carries
> `state` — deliberately NOT `__typename`, because this repository cannot
> prove `gh` emits that field and a gate requiring an unverified field
> denies every pull request on launch day. 11 tests, 3 mutations.
>
> **MODIFY REMAINS OPEN AND IS NOT CLOSEABLE LOCALLY.** Matching is by
> NAME: nothing in the merge path reads the workflow definition.
> `ci-result.js:155-157` is `run.name === required` — no App id, no
> workflow path, no actor.
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
> `.github/` is inside the pin's drift set (`F2`), so the trusted
> revision's CI definition cannot move without the pin going red — but that
> governs *this* checkout, not what a pull request branch carries.
>
> **CORRECTED 2026-10-02 — THE PREVIOUS VERSION OF THIS BOX OFFERED APP
> PINNING AS A REMEDY FOR A MODIFIED WORKFLOW. IT IS NOT ONE.** The two
> mechanisms address different threats and neither substitutes for the
> other:
>
> | Mechanism | The threat it addresses | Does it stop a MODIFIED workflow? |
> |---|---|---|
> | **Expected-App pinning** — `branch-protection-AFTER.json` pins `ci` to `app_id 15368` | a check run named `ci` produced by something that is **not GitHub Actions** | **NO.** A modified workflow runs THROUGH GitHub Actions, so its check run carries `app_id 15368` legitimately and pinning accepts it |
> | **Workflow integrity** | the workflow definition itself being changed by the branch under judgement | **YES — it is the only one of the two that does** |
>
> App pinning is still worth having, and V3c still tests whether GitHub
> honours it. It simply does not touch this gap.
>
> **THE PROPOSED AMENDMENT, AND IT IS NOT A BAN ON CHANGING CI.** A product
> pull request may not modify the paths the trusted CI workflow executes.
> Such a change is permitted — through a **separately reviewed apparatus
> amendment**, the same shape as the governed decisions already recorded in
> the contradiction audit. The rule is *"a product task may not change the
> thing that judges it in the same breath"*, not *"this may never change"*.
>
> **The protected set is derived from execution, not taste**, and the
> derivation is CHECKED: the test re-reads `ci.yml` and fails if a step
> references a path no prefix covers.
>
> | Prefix | The `ci.yml` step that makes it load-bearing |
> |---|---|
> | `.github/workflows/` | the definition itself — it decides what every other step is |
> | `apparatus/` | `npm test` (`node --test`) and `node apparatus/pr-evidence/validate.js`, which also requires `severity-floor.js` and `requirement-registry.js` |
> | `control/` | `python -m unittest discover -s tests` — the suite imports the control plane it tests |
> | `tests/` | the same step; the suite IS the assertions CI makes |
> | `scripts/` | `python scripts/check_no_secrets.py`, the committed-secret guardrail |
> | `protocol/` | `validate.js` reads `protocol/PR-EVIDENCE-V2.schema.json` |
>
> `evidence/` is deliberately NOT protected: it holds submitted packages —
> the thing CI judges, not part of what judges. A product PR adding one
> there is the normal case, and a test asserts ordinary product work
> (`src/`, `package.json`, `public/`, an evidence package) is not refused.
>
> **BUILT AND PENDING APPROVAL:** `control/ci_protected_paths.py`, 12
> tests. **Nothing calls it**, and a test pins that — wiring it into the
> merge gate is the governance change, and it is yours. Enforcement also
> needs the PR's changed-file list, which the merge path does not fetch
> today.
>
> **Recorded as an UNRESOLVED RISK.** No operator acceptance exists for
> it, and this document must not imply one. It is for you to rule on; it
> is not something this arrangement currently solves.

> **One change the GitHub side cannot substitute for:**
> `control/worker_entry.py:180` is `env = dict(os.environ)` with no scrub
> before `Popen`. Until that is an allow-list, a worker inherits the
> Supervisor's entire environment and the boundary is not enforced
> whatever the file modes say.
>
> **The allow-list is WRITTEN AND TESTED — `worker_entry.worker_child_env`,
> 10 tests — and deliberately NOT WIRED.** A test pins the unwired status
> so taking action 6 is a decision rather than a drift. **And wiring it is
> not sufficient on its own:** `GH_TOKEN` is ON the allow-list, because a
> worker needs its own 1-hour token. If the Supervisor still holds its own
> `GH_TOKEN` in the environment when the filter is switched on, every
> worker inherits the identity that merges. Action 6 is the filter **plus**
> the UID split that gives the worker a different token. Names:
> `experiment/github-app/env-var-names.md` §3.

---

## 7. The trusted revision

> **TRUSTED APPARATUS REVISION:
> `ab1ceee8b2d8ac88663c51a36ccf99b240f4c061`**, on
> `wip/c05-1-persistence`. Full 40 hex, never a branch name.

**`main` is disqualified, and this was checked rather than assumed.**
`main` is `4eeaa7c`, **86 commits behind** (re-counted at the 2026-10-02
re-pin; it was 61 at `8be6a97`), and contains **no**
`live-gate.js`, **no** `ci-result.js`, **no** `reviewer-identity.js`,
**no** `requirement-registry.js`, **no** `gate_evidence.py`, **no**
`accessibility_services.py`. Exporting `main` would not fail closed — it
would fail to exist.

**The pin must be confirmed or re-stated at approval time.**
`check-templates.py` enforces the invariant that matters: the pin is
reachable from HEAD (`F1`), and everything the deployment EXECUTES is
unchanged since it (`F2`):

    apparatus/  control/  protocol/  prompts/  config/  bin/  .github/
    experiment/github-app/status_transport.py
    experiment/github-app/publication_path.py
    apparatus/package.json  apparatus/package-lock.json

The last five were added 2026-10-02. `bin/` was protected by §6 but outside
the set; `.github/` defines the `ci` check the gate matches by name alone;
and **the two `experiment/` modules are executable components, not
documentation** — one builds the commit-status request, the other joins the
gate to the publisher. The two `package` files decide what `npm ci`
installs into the read-only export at action 7b, so they determine what
code the gate actually loads. `check-templates.py` is deliberately OUTSIDE
the set: it is a verification tool, not a deployed component.

Documentation on top of a pin is harmless; one line of executed code is
not, and `F2` goes red naming the file. Both halves have been watched
failing — five times across two sessions, every one demanded by the check
rather than noticed by a person.

The export is created once, read-only — **and the dependency install in
the middle is not optional; see action 7b**:
`git worktree add --detach /opt/run-002/gate-<SHA> <SHA>` →
`cd <export>/apparatus && npm ci --omit=dev` →
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
| ~~which `gh pr merge` flag carries the expected head~~ | **MOOT 2026-10-02.** The hook still refuses all `gh pr` invocations including `--help`, so the flag is still unverified — but nothing depends on it: §5 is now implemented with the REST call it always specified |
| **whether GitHub's 409 on a stale `sha` behaves as documented** | **ADDED with the §5 implementation.** No network call has ever been made from this repository. First exercise is Stage 1 |
| whether an App is already installed | needs App authentication |
| live branch-protection state on 2026-10-02 | deliberately not re-queried; §3's BEFORE is the 2026-10-01 GET |
| whether `Contents: write` is needed for the supervisor alongside `Pull requests: write` | **ADDED.** Flagged in the supervisor manifest's own `unverified` list and in §8, but §2's table marked only `Issues: write` |
| **whether GitHub's GET still returns a `required_pull_request_reviews` OBJECT after a PUT with count 0** | **ADDED, and it decides whether `ctl start` works.** `control/preflight.py:174` computes `reviews is not None`; if GitHub echoes `null` once the count is zero, the REQUIRED `github_main_protection` gate fails and `ctl start` refuses. The AFTER payload sends an object, but what the GET returns is GitHub's choice. **Check this at action 12 before assuming the arrangement is live** |
| whether GitHub honours `app_id` pinning on a required context | the §1 separation leans on it; the proposal's V3c exists to test it on the throwaway |
| whether a `pull_request` workflow resolves from the head branch or the base | bears on the `.github/` risk in §6; the name-only match records the risk regardless |

---

## 9. What needs YOUR approval — the twenty actions, in order

**It was twelve until 2026-10-02.** Three passes changed it: an
independent review of this document, an adversarial review of the code,
and an attempt to actually RUN the export. The twelve as written would
have left no process authenticating as any of the three Apps, left workers
unable to push, installed production credentials before any falsification
ran, overwritten live protection with a reconstruction — and produced an
export that could not load the gate.

**THE COLUMN THAT MATTERS IS "BUILT?".** Several actions are now mostly
done: the code exists, is tested against simulated services, and is
waiting for a credential or a host. Those are **not** new approvals — they
are the same action with less work left in it. The actions that need a
decision from you are marked **DECISION**; the rest are execution.

| Marker | Meaning |
|---|---|
| **BUILT** | code exists locally, tested, nothing deployed. No approval needed for what exists |
| **DEPLOY** | an action on the host or on GitHub. Needs your authorisation |
| **DECISION** | a choice only you can make; the work cannot start without it |
| **GATE** | a verification that must pass before the next step. **These are not optional and none has been removed** |

**None of the DEPLOY or DECISION rows has been done. Each is yours.**

| # | Kind | Action | Reversible? |
|---|---|---|---|
| 0 | DEPLOY | Create the throwaway repository that actions 3a and 10 use | yes — delete |
| 1 | DEPLOY | Create OS users `run002-sup` and `run002-wrk`, group `run002` | yes |
| 2 | DEPLOY | Create the three GitHub Apps with §2's permissions, zero events | yes — delete |
| 3a | DEPLOY | **Install all three on the THROWAWAY repository first** | yes — uninstall |
| 4 | DEPLOY | Generate private keys; place `0400` owned by `run002-sup` | yes — revoke. **Revoke the old key BEFORE issuing a new one** — a fresh key does not invalidate its predecessor |
| 5 | DEPLOY | Re-own `.runtime/` and the checkout per §6 — **including the corrected git row**; `chmod 0700 .runtime/` | yes |
| 6 | **BUILT** + DEPLOY | The allow-list derivation exists and is tested (`worker_child_env`, 10 tests). What remains: one line at the call site, **plus** the UID split that gives the worker its own `GH_TOKEN` — the filter alone is not sufficient | yes — code |
| 6b | DEPLOY | **WITHOUT THIS THE REST DOES NOT WORK.** Change `control/gh.py:33` to pass an explicit per-role `GH_TOKEN`. See the box below | yes — code |
| 6c | **DECISION** | `origin` SSH → HTTPS for the worker's worktrees. After action 5 a worker cannot read `serina`'s SSH key, so declining needs another answer to how it pushes | yes |
| 7 | DEPLOY | Create the gate export at the §7 pin — `git worktree add --detach` | yes — delete |
| 7b | **BUILT** + DEPLOY | **WITHOUT THIS THE GATE CANNOT RUN AT ALL.** The packaging fix is done (`ajv` → `dependencies`, lockfile regenerated, `npm ci --omit=dev` verified). What remains: run it inside the export, **before** the `chown`/`chmod` | yes — delete the export |
| 7c | DEPLOY | **Only now** `chown -R run002-sup:run002` and `chmod -R a-w` the export | yes |
| 8 | **BUILT** | The invoker (`gate_invoker.py`) and the gate program (`gate-cli.js`, 29 tests) both exist and are built around all three `__dirname` traps. Nothing remains for this action but to point it at a real export | yes — code |
| 9 | **BUILT** + DEPLOY | The transport (`status_transport.py`, 31 tests) and the join (`publication_path.py`) both exist. What remains: supply the gate credential and set the enable variable. **The request itself is written** | yes — code |
| 10 | **GATE** | Run the falsification plan on the THROWAWAY repository — **V1–V14**. V10 reproduces the F5 deadlock; V12 is the second-required-context check (§4); **V13 runs the gate from a real read-only export** — the step that would have caught the §7 trap; **V14 confirms the corrected git ownership under the REAL identities**, which no local test can. V13 must pass before action 11 | n/a |
| 10b | **GATE** | **STOP.** Do not proceed unless every V-step passed | n/a |
| 10c | DEPLOY | **Only now** install the three Apps on `serina-mcfall/wellbeing-run-002` | yes — uninstall |
| 11a | **GATE** | GET `/repos/{owner}/{repo}/branches/main/protection` and save the bytes. **THAT file is the rollback artefact**, not `branch-protection-BEFORE.json` | n/a |
| 11 | **DECISION** + DEPLOY | **Apply `branch-protection-AFTER.json` to `main`.** This is the protection change, including `required_approving_review_count` 1 → 0 | yes — the bytes from 11a |
| 12 | **GATE** | Re-run `ctl preflight`; confirm `github_main_protection` still PASSes. **Then separately** `gh api .../protection` and confirm by eye that `run-002/independent-review` is in `contexts` and the review count is 0 — the gate checks neither | n/a |

**Step 11 is last, and that ordering is not a preference.** Applying it
before 8–10 deadlocks every product PR permanently.

### THE EXACT APPROVAL REQUEST

Everything above is either already built or mechanical once authorised.
**Four things actually need a decision from you**, and nothing starts
without the first:

| | Decision | If you say no |
|---|---|---|
| **A** | **Authorise the DEPLOY actions in §9, in order.** Identities, Apps, keys, ownership, export, credential, falsification | nothing proceeds; the apparatus stays as it is, fully built and inert |
| **B** | **Apply `branch-protection-AFTER.json`** — action 11, which includes `required_approving_review_count` **1 → 0**. This is the hinge: `main` moves from "a human approved this" to "the gate approved this" | reject the arrangement rather than trimming it. Every other part exists to make this one substitution safe |
| **C** | **`origin` SSH → HTTPS for the worker's worktrees** — action 6c. Recommended yes | you must supply another answer to how a worker pushes after action 5 |
| **D** | **The trusted-CI protected path set** (§6) — wire `ci_protected_paths.py` into the merge gate, so a product PR touching what CI executes needs a separately reviewed apparatus amendment. Recommended yes | the modified-workflow gap stays open and UNRESOLVED |

**B and D are governance; A and C are operational.** D can be taken
independently of A — it is local code and needs no App, no host user and
no protection change.

**The three-principal design (§1) is the recommendation and is not
reopened here.** It is what A authorises. The alternative — widening the
gate App — costs the property that makes V8 falsifiable, and §12 records
why.

**What is NOT being asked.** Nothing about the already-built components
needs approval: they exist, they are tested against simulated services,
and none of them can act. Approving A does not approve D, and approving
the local implementation has never approved the protection change in B.

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
| The pin, and a check that fails when EXECUTED code moves under it | `check-templates.py` `F1`/`F2` — drift set now also covers `bin/`, `.github/`, the two executable `experiment/github-app` modules and `apparatus/package*.json` |
| **The worker environment allow-list** for action 6, written and tested, **deliberately not wired** — a test pins the unwired status so taking action 6 is a decision, not a drift | `control/worker_entry.py::worker_child_env`, `tests/test_c22_worker_env_allowlist.py` (10 tests) |
| **The gate invoker** (action 8, Python half): required+undefaulted export path, the live repo root refused if it is the export, the export's actual revision compared to the pin, 11 finite fail-closed outcomes, never raises | `control/gate_invoker.py` |
| **The publication path** (action 9, the join): gate invoker → publisher, same environment switch checked **before** any subprocess, no default transport. Placed in `experiment/github-app/` on purpose, so `control/` and `bin/` stay publisher-free and that guarantee survives literally | `experiment/github-app/publication_path.py` |
| Both of the above **verified as a connected path against simulated services** — real `node`, a real throwaway export, a real throwaway git repo, and a gate program the test writes. 60 tests, 10 mutations | `tests/test_c20a_publication_path.py` |
| **The sixth provenance condition**, which C-04c recorded as unmatchable. It closed a real hole: the merge path never required a `REVIEW_DISPATCHED` at all, so one appended `REVIEW_RESULT` attributed to nobody satisfied the whole code-review leg | `control/routing.py::_review_worker_attests`, `tests/test_c04c_merge_attestation.py` |
| The recursive protection-restatement check, and the nested field it immediately caught | `check-templates.py` check B, `branch-protection-AFTER.json` |
| **The gate program itself** (action 8's deployment half): envelope in on stdin, the gate's decision out verbatim, built around all three `__dirname` traps and proved by a regression test that runs it from a directory that is NOT the configured workspace | `apparatus/pr-evidence/gate-cli.js`, 29 tests |
| **The `ajv` packaging fix** for action 7b, with `npm ci --omit=dev` verified to install it and not playwright | `apparatus/package.json`, `package-lock.json` |
| **The gate process's environment allow-list** — `NODE_PATH` and `NODE_OPTIONS` both choose what code Node runs, and the spawn passed neither through a filter | `control/gate_invoker.py::gate_child_env`, 7 tests |
| **The commit-status transport**, contract derived from GitHub's published documentation, **with no client, no socket import, no importer and no request ever sent** | `experiment/github-app/status_transport.py`, 31 tests, 6 mutations, 4 mechanical checks (`E2`) |
| **C-23 — two of the three ways a product PR could satisfy the required CI check without CI passing**, closed by bringing the Python path to the rule `ci-result.js` already applied. The third is open, §6 | `control/gh.py::checks_state`, `tests/test_c23_required_check_integrity.py` (11 tests, 3 mutations) |
| **The pending-review flag's contract**, rewritten as publish-eligibility and TESTED both ways — publish then ask the merge authority, which refuses | `tests/test_c20c_publish_eligibility_is_not_merge_eligibility.py` (8 tests, 1 mutation) |
| **The trusted-CI protected path set** (decision D), derived from what `ci.yml` executes, with the derivation CHECKED against the workflow. **Not wired** — a test pins that | `control/ci_protected_paths.py`, `tests/test_c23_ci_protected_paths.py` (12 tests) |
| **The measurement that §6's git row was unimplementable**, on a disposable repository: read-only objects or refs make a worker commit impossible; `hooks/` can be withheld | `tests/test_c22_worker_git_writes.py` (7 tests) |

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

**On the status body.** `description` and `target_url` are optional in
GitHub's schema and are **omitted**; the context name carries the result.
**No decision is required** — a description can be added later without
touching protection, identities or the gate. `pending` is **never
published**: the gate posts only `success` or `failure`, at a verdict. That
default is retained, and nothing in Protocol v2 or this arrangement
requires otherwise.

| Question | Recommendation | Why |
|---|---|---|
| Third principal, or widen the gate App? | **Third principal — this is the recommendation and §1 is written to it. Not reopened.** | Widening the gate App costs the property that makes V8 falsifiable: that the publisher cannot act on what it blessed |
| Move `origin` from SSH to HTTPS? | **RESOLVED 2026-10-02: yes** — and **not** "for the worker's worktrees only", which is not achievable: linked worktrees share the main `.git/config`, and after action 5 there is one checkout and it belongs to `run002-sup`. Recorded as decided so Stage 1 is executable end to end; **overrule it in the approval if you disagree**, and A6 reverts to a STOP | Today the strongest write credential in the system is a personal SSH key sitting entirely outside the scheme. **It is not optional:** after action 5, a worker can read neither `serina`'s SSH key nor her `gh` config, so declining needs some other answer to how a worker pushes, and none exists. **Measured 2026-10-02: the stored `origin` is ALREADY HTTPS**, and `~/.gitconfig`'s `insteadOf` is what resolves it back to SSH — so action 6c is nearly a no-op here, and **what actually delivers the property is the new identity**, whose home carries no such rewrite. **V14f verifies it; it is not assumed.** Action 6c |

The first is settled as the proposal's design and is listed here for
visibility, not as an open question. The second is a required action with a
recommended answer — decision C in §9.

---

## 13. THE STAGED DEPLOYMENT REQUEST — three stages, approved separately

**ADDED 2026-10-02.** §9 is one list of twenty actions. That is the right
shape for *what must happen*; it is the wrong shape for *what to approve*,
because it bundles reversible work on a throwaway repository with a
protection change on `main` and with the paid run. **These are three
different risks and they are now three different approvals.**

**DECISION D IS ALREADY APPROVED** and is recorded verbatim at
`experiment/evidence/D-ci-protection-amendment-approval.txt`. It is a
repository-level rule enforced by Run 002's own merge path. It is **not**
part of any stage below: it touches no host, no credential and no GitHub
setting.

| Stage | What it is | Touches `main`? | Costs money? | Reversible? |
|---|---|---|---|---|
| **1** | Host isolation + throwaway repository | **no** | no | yes, entirely |
| **2** | The Run 002 repository and its branch protection | **YES** | no | yes, via the 11a capture |
| **3** | Paid preflight, then the 24-hour launch | yes | **YES** | the run is not |

**Stage 2 cannot be approved before Stage 1's verification passes**, and
Stage 3 cannot be approved before Stage 2's. That is not ceremony: V13 and
V14 only exist inside Stage 1, and they are what establish that the gate
can run at all and that the git ownership permits a worker to commit.

---

### STAGE 1 — host isolation and the throwaway repository

> **STATUS: PREPARED, NOT APPROVED. NOTHING IN IT HAS BEEN DONE.**
> Its prerequisites are complete, so it is ready to be *decided*. No App,
> credential, OS user, ownership change, export or repository exists.

**Only decision D has been approved so far** — the CI-protection amendment,
recorded verbatim at `experiment/evidence/D-ci-protection-amendment-approval.txt`.
D is a repository-level rule, is wired into the merge path, and is **not**
part of any deployment stage.

| | |
|---|---|
| **Actions** | §9 rows **0, 1, 2, 3a, 4, 5, 6, 6b, 6c, 7, 7b, 7c, 10, 10b**, preceded by **A0**, the pre-change capture added 2026-10-02 |
| **What it changes** | writes a pre-change capture to `~/run-002-stage1-rollback/` (A0); creates a NEW throwaway GitHub repository; creates OS users `run002-sup` and `run002-wrk` and group `run002`; creates three GitHub Apps installed **on the throwaway only**; writes three private keys at `0400`; changes ownership/modes of `.runtime/`, the checkout and `.git/` per §6; **sets `core.sharedRepository=group` and pre-creates the worker worktree root `<WS>__worktrees` at `2770` — the dispatch-writability arrangement, decided 2026-10-02**; creates a read-only export at `/opt/run-002/gate-<pin>`; creates **one orphan probe branch and worktree** for V14 and pushes it **to the throwaway**; flips `worker_entry`'s env pass-through to the built allow-list; changes `gh.py` to pass a per-role token — **the mechanism only**, see R1 |
| **What it does NOT touch** | `main`; its branch protection; the Run 002 repository's **settings**; any product PR; any provider that charges — **and, from 2026-10-02, the Run 002 repository at all.** The 2026-10-02 correction that admitted "it does write ONE branch to `<PROD>`" is **withdrawn in favour of removing the write**: V14's probe now pushes to the **throwaway**, on an **orphan** branch, so no Run 002 history leaves this host and `<PROD>` has nothing to roll back. No requirement was found that needs `<PROD>` — V14 establishes a *local* property (can `run002-wrk` write the real checkout's objects, refs and worktree gitdir) plus *which URL a push resolves to*, and any GitHub remote exercises the second equally. The probe is still a dedicated `probe/v14` branch and worktree, created by the step and deleted by its undo |
| **Credentials — IT DOES CREATE THEM** | **CORRECTED.** Stage 1 creates **three real GitHub App private keys** and the installation tokens minted from them. What it does NOT need is the **eight product secrets** (`~/.config/run-002/secrets.env`) — those are Stage 3. "No secrets needed" earlier in this document meant the eight, and must not be read as "no credentials created". The keys are live credentials from the moment they exist: `0400`, owner `run002-sup`, outside the repository, and **revoked before replacement** if ever suspected |
| **Cost — AND ONE UNVERIFIED ASSUMPTION** | **No paid provider call.** GitHub Apps, a repository and installation tokens are free; the cost is operator time and the keys above. **BUT "no billable operation" was overstated.** The procedure's A1 creates the throwaway as a **private** repository carrying a workflow that produces a check run named `ci`, and **V10 and V12 each require "let `ci` pass"** — so Stage 1 does run GitHub Actions there. Actions minutes are **free on public repositories and metered on private ones**. The account's remaining allowance **could not be read from this host**: `gh api /users/serina-mcfall/settings/billing/actions` returns 404 because the ambient token lacks the `user` scope, and refreshing that scope is itself an authorisation change outside this stage. **The bounded decision, yours:** create the throwaway **public** and the question disappears (one commit, a trivial workflow, no product code, no secret, no credential), or keep it **private** and accept a bound of **under ten runs of a workflow that must be trivial** — a single `ubuntu-latest` step that exits 0, explicitly **not** a copy of this repository's `.github/workflows/ci.yml`, which installs Playwright Chromium under `timeout-minutes: 25`. The V-steps need a check run *named* `ci` with a conclusion; they need nothing it does |
| **Verification** | **V1–V14f on the throwaway**, ending at the 10b STOP gate. The four that matter most: **V3/V3b/V8** prove the permission split in both directions (the worker is refused a status, the gate can post one, the publisher is refused a merge); **V10** reproduces the F5 deadlock deliberately; **V13** runs the gate from the real read-only export — the step that would have caught both `WORKSPACE_MISMATCH` and the missing `ajv`; **V14a–f** confirm the git ownership under the real identities, including **V14d, which must SUCCEED** (objects and refs stay writable or no worker can commit) and **V14e, which CANNOT PASS and must be recorded not-applicable** |
| **How to stop it** | it is a sequence of operator actions with a STOP gate at 10b. Stop at any step; nothing downstream is automatic |
| **Rollback, in order — EVERY ITEM NAMES SOMETHING THIS STAGE CREATED** | **REWRITTEN 2026-10-02 against A0's capture.** 1. Revoke the three App private keys **before** issuing any replacement (a fresh key does not invalidate the old one). 2. Uninstall and delete the three Apps. 3. Delete the throwaway repository. 4. `rm -rf /opt/run-002/gate-<pin>`. 5. Delete the V14 probe: `git push probe --delete probe/v14` **on the throwaway**, `git worktree remove --force <WS>__worktrees/probe-v14`, `git branch -D probe/v14` — and nothing else under `refs/heads`, checked against A0's `refs-BEFORE.txt`. **5b. Undo the dispatch arrangement:** `git config --unset core.sharedRepository` and `rm -rf <WS>__worktrees`, each **only if A0's capture recorded it ABSENT**; `bin/supervisor.sh`'s `umask 0002` reverts with item 8's `git revert`. 6. Restore ownership and modes **by replaying `~/run-002-stage1-rollback/ownership-BEFORE.txt`**, not by `chown -R serina:serina` — `chmod -R 0750` over a workspace is not invertible from the after-state, and not every path under `<WS>` was `serina:serina` to begin with. 7. `userdel run002-wrk run002-sup` and `groupdel run002` **only for the identities A0 recorded as ABSENT**, and **without `-r`**: the procedure said `userdel -r` while this row said plain `userdel`, and `-r` against a pre-existing account deletes a home directory this stage never created. 8. Revert the two code changes (actions 6 and 6b) with `git revert`. **No product data exists at any point, and nothing in Run 001 is touched** |
| **What it buys** | it converts every unverified row in §8 into a measured answer on a repository that can be deleted, and it is what **closes C-22** |
| **C-22 closure — PRECISELY** | **CORRECTED.** C-22 closes when `.runtime/` is `0700` owned by a user the workers are not, **and V14a–f have been run and passed**. Approval does not close it; the verified ownership change does. **V14e cannot pass** under the single `run002-wrk` identity this section specifies — record it NOT APPLICABLE. So worker-to-worker reachability survives Stage 1 as a separate unresolved residual, and C-22's closure must be claimed only for what it covers: the records, evidence and credentials that decide a worker's own merge |

### STAGE 2 — the Run 002 repository and its branch protection

| | |
|---|---|
| **Actions** | §9 rows **10c, 11a, 11, 12** |
| **Affected resources** | the three Apps installed on `serina-mcfall/wellbeing-run-002`; **branch protection on `main`** |
| **The change that matters** | `required_approving_review_count` **1 → 0**, plus a new required context `run-002/independent-review` and `enforce_admins` false → true. **This is the hinge: `main` moves from "a human approved this" to "the gate approved this"** |
| **WHAT IT CREATES THE MOMENT IT LANDS — ADDED 2026-10-02** | **Nothing in the running system posts that context.** `control/gate_invoker.py` and `control/publisher.py` are built and deliberately unwired — `check-templates.py` `E2` asserts nothing in `control/`, `bin/`, `apparatus/` or `experiment/github-app/` imports the transport — so publication is a hand-run operator step (procedure C5–C6). From the moment this PUT lands, GitHub refuses **every** merge, including the Supervisor App's, until a human posts a status for that exact head. That is V10's F5 deadlock arriving as ordinary operation. It is tracked as **R3** in `experiment/RUN-003-BACKLOG.md` and is **required before THIS stage — corrected 2026-10-02 from "before Stage 3"**, which placed the remedy one stage after the breakage it remedies. The required context starts refusing merges at this PUT, so the publisher must already exist and have been seen posting |
| **Verification** | 11a captures live protection to a file BEFORE the PUT — that file, not the 2026-10-01 `branch-protection-BEFORE.json`, is the rollback artefact. Then `ctl preflight`, then a separate `gh api .../protection` read confirming by eye that the context is present and the count is 0, because the gate checks neither |
| **Rollback** | `PUT` the bytes captured at 11a. Uninstall the three Apps. `enforce_admins` returning to `false` restores your ability to merge by hand |
| **Prerequisite** | **Stage 1 complete, with every V-step passed at the 10b STOP gate** — **AND R1, R2 and R3 implemented and verified (ADDED 2026-10-02).** "Verified" means observed on Stage 1's throwaway: the publication path posts `run-002/independent-review` for a specific head under the role's own credential, and a merge succeeds with it present. Do not make the context required while the thing that posts it is unwired — that is approving a required check nothing can satisfy. R1/R2/R3 are in `experiment/RUN-003-BACKLOG.md` under "NOT deferred", and R3 is a **decision** before it is an implementation |

---

### STAGE 3 — paid preflight, then the 24-hour launch

| | |
|---|---|
| **Actions** | provision the eight secrets; run the complete real `ctl preflight`; the remaining Phase 3 conditions; then T+00 |
| **Affected resources** | paid provider calls (Claude, Codex, Grok heartbeats); a REAL Discord notification to a phone; durable ledger writes; live product workers; real merges to `main` |
| **Costs money** | yes — this is the first stage that does |
| **Verification** | all 24 governed gates green in a real `preflight.json`; the eleven Protocol v2 §"Preflight" conditions no gate checks (Phase 3), **including the contradiction-audit PASS, which is the binding one**; a final clean preflight on a quiet host immediately before start |
| **Rollback** | the preflight is re-runnable. **The 24-hour run is not reversible** — Protocol v2's clock never pauses, and an early stop must never be reported as a completed run |
| **Prerequisite** | Stage 2 complete; **C-22 closed by Stage 1's UID split, verified under the real identities**; the audit marked PASS. **R1, R2 and R3 are no longer listed here: they moved UP to Stage 2's prerequisites on 2026-10-02**, because Stage 2's PUT is what makes the status required and therefore what the absent publisher deadlocks. By the time this stage is considered they are already done and verified, or Stage 2 was not approvable. Restating them here as a Stage 3 condition is what let the ordering be wrong for a day |

---

### WHAT IS STILL TRUE OF ALL THREE

**C-22 is open until Stage 1's isolation is deployed AND verified under
the real OS identities.** Everything built locally is tamper evidence, not
prevention: the ledger has no hash chain and the same UID can append to
it. Stage 1 is what closes it; nothing before Stage 1 can.

**The five-hour standalone rehearsal remains waived** (C-18a). Every
functional and deployed integration check it did not cover remains
required — V1–V14, the C-04a multi-cycle verification, and Phase 3.
