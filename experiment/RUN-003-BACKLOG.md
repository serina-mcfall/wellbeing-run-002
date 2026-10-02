# Run 003 backlog

Things found during Run 002 preparation that are **deliberately not being
fixed before this run**. Run 002 is a bounded experiment; its job is to
generate evidence, and each item below was judged against the launch-blocker
test: does it prevent a startup or core lifecycle operation, risk
uncontrolled spending / exposed credentials / unauthorised actions /
unreviewed merges, or violate an explicit governing requirement?

**Every item here answers NO to all four.** Each records its practical
impact so the retrospective can weigh it against what actually happened.

| # | Item | Practical impact during Run 002 |
|---|---|---|
| 1 | **Worker git isolation by separate clone.** Built and tested (`control/worker_git.py`, 41 tests) but NOT wired; the Supervisor still uses linked worktrees. | A worker can move another worker's local branch ref. It **cannot** cause an unreviewed merge — `evaluate_merge` denies `HEAD_SHA_CHANGED`/`HEAD_SHA_UNVERIFIABLE` against the head GitHub reports. Under the single `run002-wrk` identity, peers reach each other's files regardless, so wiring it would not deliver the property anyway. Wiring touches ten Supervisor call sites plus a Builder→Fixer fetch. |
| 2 | **Which gate decides a merge.** Two gates exist; only `routing.evaluate_merge` is reachable from a tick, so `live-gate.js`'s extra checks never inform the merge DECISION. | None for operation: the Python gate is the sole merge authority and carries the evidence gate, all six ledger provenance conditions, CI integrity and the CI-path rule. The JS gate's extra checks are proved in the fixture, not in the tick. Joining them is a governance decision about which gate decides merges. **NARROWED 2026-10-02:** this row used to also cover the JS gate's OTHER job — deciding whether the required `run-002/independent-review` status may be posted. That half is not deferrable and moved to **R3** below. |
| 3 | ~~**`gh.merge` carries no expected-head.**~~ **CLOSED 2026-10-02** at `47f35f5`. | `gh.merge` now sends the documented `PUT .../merge` with `sha` set to `pr["headRefOid"]`, the head the gate judged; `expected_head` is a required keyword so no call site can omit it. 13 tests, proved able to fail. The ref deletion `--delete-branch` used to do is a second best-effort call. **Still only a component test** — that GitHub answers 409 on a moved head is documented, not yet observed here. First real exercise is Stage 1. |
| 4 | **`intervention.request` reads `record["dedup_key"]` unguarded.** | A malformed intervention record in `state.json` raises `KeyError` out of the tick — the same permanent-DoS shape C-04c fixed for `outcome`. Reachable only by something that can already write `state.json`, which is C-22. |
| 5 | **The 300-file cap in the changed-path check refuses very large PRs.** | A genuine 300-file product PR needs a human. No Run 002 task is expected to produce one. |
| 6 | **Compare-API wire format is unverified.** No real `gh` call has ever been made from this repository. | Fails closed: an unexpected shape makes the file list *incomplete*, which refuses the merge. Costs a merge, never leaks one. First real exercise is Stage 1. |
| 7 | **`evidence/` artifact lifecycle** (C-05's binary-artifact architecture) and the **Jev secret-leak investigation**. | Both recorded in the audit as investigations, neither started. No operational impact pre-T+24. |
| 8 | **Three worktree-root definitions disagree** (`workers.py`, `isolation.json`, `config.py`). | `git-head.js`'s worktree-kind identity is unusable; the gate uses branch identities, which work. Cosmetic until something tries worktree identity. |
| 9 | **Residual `orphan_annunciations` entry** (attempt 586, OBSERVED, resource gone). | One stale key in `state.json`. Preserved deliberately. Appears in the launch document as test residue. |

## NOT deferred — REQUIRED RUN 002 WIRING, before **STAGE 2**

These were wrongly listed above as Run 003 items. They are Run 002
work: without them the arrangement does not operate as designed, and a
24-hour run fails. **None of them blocks Stage 1.**

**ORDERING CORRECTED 2026-10-02 (second correction).** This section, the
approval package and the deployment procedure all said *before Stage 3*.
That was off by one stage. **Stage 2 is the step that makes
`run-002/independent-review` a required context on `main` with
`enforce_admins: true`** — so the deadlock starts at Stage 2's PUT, not at
Stage 3's launch. Approving Stage 2 against an unwired publisher would
create a required check that nothing in the running system can satisfy,
and GitHub would refuse every merge from that moment, including the
Supervisor App's.

**So the dependency runs the other way round: the publisher must exist and
be observed posting the status BEFORE the status is made required.**
Concretely, all three must be **implemented and verified** before Stage 2
is approved, and "verified" means observed on Stage 1's throwaway
repository, where posting a commit status is already exercised by V3b,
V8, V10 and V12 — not asserted from unit tests alone. Stage 1 is
unaffected: it creates no required context and none of the three blocks
it.

Each one is also a numbered gap in `experiment/github-app/DEPLOYMENT-PROCEDURE.md`
§10, which is where the deployment-side detail lives.

| # | Item | Why it is Run 002 work |
|---|---|---|
| **R1** | **Per-role `gh` authentication has no call site** (procedure §10 **G1**). `role=` exists on every wrapper; nothing in `control/` passes it | Until a call site passes it, every `gh` call uses ambient host auth, and the three-principal separation is a property of the manifests rather than of the running system. Ambient holds `statuses: write` **and** merge rights. **Required before Stage 2** — and Stage 2 is where it bites: once `enforce_admins: true` lands, ambient auth stops being a usable fallback, and the status must be posted by the principal whose manifest grants `statuses: write`, not by whatever token the host happens to hold |
| **R2** | **Token renewal has no caller** (procedure §10 **G5**). `gh.ensure_token` is built; no daemon or tick hook invokes it | An App installation token lasts one hour. A 24-hour run outlives it many times over, and every `gh` call fails once it expires. **Required before Stage 2.** Stage 2 itself is a short operator session that one token outlives, so R2 is not driven by Stage 2's own duration — it moves with R1 and R3 because the publisher R3 wires is what needs a live token every time it posts, and shipping a publisher that dies after an hour is shipping the deadlock with a delay on it |
| **R3** | **The gate invoker and the publisher have no caller** (procedure §10 **G6**). `control/gate_invoker.py` and `control/publisher.py` are built, tested and deliberately unwired; `check-templates.py` `E2` *asserts* nothing in `control/`, `bin/`, `apparatus/` or `experiment/github-app/` imports the transport, so publication is a **hand-run operator step** (procedure C5–C6) | **ADDED 2026-10-02, and it is the one with teeth.** Stage 2 makes `run-002/independent-review` a **required context** on `main` with `enforce_admins: true`. Nothing in the running system posts it. GitHub then refuses every merge — including the Supervisor App's — until a human posts a status for that exact head, which is V10's F5 deadlock arriving as ordinary operation rather than as a test. The frozen acceptance property is **"no human step"**, and in the fixture the gate and publisher are called automatically; deployed, they are not called at all. **Required before Stage 2 — CORRECTED 2026-10-02 from "before Stage 3", which put the fix a stage later than the breakage. It is a DECISION first**: either wire the publication path and re-aim `E2` at a narrower claim — the way `E` was re-aimed rather than relaxed — or accept a human status post per head and stop claiming the run is unattended. Writing an HTTP client into `control/` is what `E2` exists to prevent, so this cannot be done quietly |

## What is NOT on this list

C-22 — worker isolation from the records that decide their own merges —
is **not** deferred. It is an open launch blocker. **Stage 1 closes it only
once V14a–f have passed**, not on approval: the closure is the verified
ownership change, not the decision to make it. Note that **V14e cannot pass**
under the single `run002-wrk` identity §6 specifies, so worker-to-worker
reachability (item 1 above) survives Stage 1 and is a separate, unresolved
residual.
