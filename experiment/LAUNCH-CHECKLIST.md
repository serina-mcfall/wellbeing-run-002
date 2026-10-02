# Run 002 — Launch Checklist

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` (§"Preflight"),
`control/preflight.py` (`GATES`, `SPEC_FILES`), `experiment/CONTRADICTION-AUDIT.md`
(all C-numbers), `experiment/PREFLIGHT-FINDINGS.md` (the open-item register),
`experiment/FREEZE-PROTOCOL.md`. Where any of those disagrees with this
document, **they win**. This checklist orders and enumerates; it decides nothing.

T+00 remains **NOT_STARTED** (`experiment/TIMELINE.md`). Nothing in this
document authorises launch, and no partial completion of it does either.

**Session state as of 2026-10-02: `C05-3a-SESSION-HANDOVER.md` §49**, which
supersedes §48, §47, §46, §45, §44, §43 and §42. §41 and §16 are stale. Branch
`wip/c05-1-persistence`, HEAD **`8be6a97`** plus documentation commits.
Verification: **2,688 Python tests, 259 apparatus tests**, both exit 0.
**Trusted pin re-pinned to `8be6a97867cb21c62830af9f8b02a34a7af0fcb5`** —
the seventh re-pin in two sessions, every one demanded by `check-templates.py`
`F2` rather than noticed by a person.

All earlier approvals remain applied and none was reopened (G1, G2, G3's
phase limits, G4, G6, G7, G9, C-18a, C-20a A/D/E). Four delegated decisions
are APPLIED - C-02a, C-18 stage 7, the notification amendment, and C-02b.

> ## THE BLOCKER THAT OUTRANKS EVERYTHING ELSE ON THIS PAGE: C-22
>
> **Workers run as the UID that owns `.runtime/`.** `control/worker_entry.py`
> spawns them with no `setuid`, no `setgid` and no container, and
> `.runtime/state.json` is `0600` owned by that same user. So every field
> the merge gate trusts is in a file the agents being judged can write.
>
> **Reproduced, not argued:** from a blank record for a pull request with
> no code review, no security evidence and neither accessibility leg,
> writing six fields turns the merge gate from `(False, NO_REVIEW_PASS)`
> into `(True, MERGE_OK)`.
>
> C-04c added a second source - the durable ledger must independently
> attest all four legs at the head GitHub reports now - so a forgery needs
> two files instead of one and leaves a permanent trace. **That is tamper
> evidence, not prevention. The ledger has no hash chain and the same UID
> can append to it.** Nothing in this repository can close C-22; only the
> OS-level separation in `experiment/C-20a-APPROVAL-PACKAGE.md` §6 can.
>
> **It must not be recorded as closed, mitigated or accepted before that
> lands.** Every "PASS" below is a statement about a control plane whose
> inputs are, today, forgeable by the workers it governs.

**THE FINITE REMAINING PATH — three approvals and one provisioning step.**

Decision **D (the CI-protection amendment) is APPROVED** — recorded
verbatim at `experiment/evidence/D-ci-protection-amendment-approval.txt`.
It is a repository-level rule, not a deployment: no host, no credential,
no GitHub setting. Implementation is in progress.

The deployment request is now **three staged approvals**, each gated on
the previous stage's verification rather than on a date
(`experiment/C-20a-APPROVAL-PACKAGE.md` §13):

| Stage | What | Touches `main`? | Costs? | Status |
|---|---|---|---|---|
| **1** | Host isolation + throwaway repository — §9 rows 0–10b | **no** | no | **READY FOR APPROVAL.** Needs none of the eight secrets |
| **2** | The Run 002 repository and its branch protection — §9 rows 10c–12 | **YES** | no | gated on Stage 1's V-steps passing at the 10b STOP |
| **3** | Paid preflight, then T+00 | yes | **YES** | gated on Stage 2, on C-22 closed, and on the audit marked PASS |

**Provision the eight secrets** — `~/.config/run-002/secrets.env`, mode
600 — is a **Stage 3** prerequisite, not a Stage 1 one. Nothing in Stage 1
reads them. Names, purposes and steps:
`experiment/evidence/LAUNCH-READINESS-2026-10-02.txt` §3-4.

**THE NEXT EXECUTABLE STEP IS STAGE 1.** It is the only stage whose
prerequisites are complete, and it is what converts §8's unverified rows
into measured answers — including the two that no local test can reach:
**V13**, the gate running from a real read-only export, and **V14**, the
corrected git ownership under the real OS identities. **C-22 cannot close
before Stage 1**, and every "PASS" in this document is a statement about a
control plane whose inputs remain forgeable until it does.

**ACCEPTANCE SCENARIO — PASSING.** The connected lifecycle runs end to end
against the production composition: draft → ready → independent review at
the actual head → checks → eligible merge → confirm → dependents unblocked,
with **no human step**, **≥2 review cycles at two different heads**, and
**cycle-1 evidence refused at the new head**. 18/18 in
`apparatus/fixture-preflight/production-lifecycle.test.js`; 76/76 across the
Python connected-lifecycle suites. Audit C-04a is explicit that a real
product PR is **not** required for this.

**Scope is frozen.** Items found during preparation that are not launch
blockers are recorded once in `experiment/RUN-003-BACKLOG.md` with their
practical impact, and are not being fixed before this run.

**Test-count note.** This document previously carried 2,171/218 here and
1,852/197 in the `control_plane_self_tests` row below, as if both were
current. They were a current figure and a session-start baseline. Both now
read 2,364/230, measured at the HEAD named above.

---

## The governing rule

**Failing gates are not made to pass by weakening the gates. The deployment
environment is made to satisfy the governed gates.**

No threshold, gate or governed value may be relaxed merely to obtain a PASS, to
shorten a schedule, or because the current machine happens to fail it. Where a
gate cannot pass, the correct outcomes are: remediate the environment, or record
a governed decision that changes the requirement deliberately and traceably.

**The launch principle.** Thursday is the *confirmation* of an
already-rehearsed known-good environment — not the first time deployment
failures are discovered.

---

## Phase order

1. **Pre-T+00 implementation** — every required implementation blocker closed.
2. **Deployment Readiness / Preflight Remediation** — mandatory, after phase 1
   and before T+00. Run the complete real preflight against the actual
   deployment environment **without starting T+00**.
3. **The Protocol v2 §"Preflight" conditions no gate checks** — eleven of its
   twenty, including the contradiction-audit PASS, which is Protocol v2's
   FIRST condition and the binding one.
4. **T+00** — a final complete clean preflight immediately before, then start.

A partial pass at any phase authorises nothing.

**"Each phase gates the next" was the rule here until 2026-10-02, and it was
wrong in one specific way.** Phase 3 item 1 — the C-04a realistic multi-cycle
verification — is fixture-bound by audit C-04a's own words and needs no
secrets, no GitHub access and no C-20a(C) decision. Sequencing it behind
Phase 2 deferred a check that depends on nothing in Phase 2. It runs in
parallel; its real precondition is C-04's gate join, which is a Phase 1 item.
Phases 2 and 4 remain genuinely distinct from each other: Phase 2 is the
remediation loop that produces the first all-green `preflight.json`, Phase 4
step 4 is a freshness re-run on a quiet host immediately before start.

---

## Phase 1 — Pre-T+00 implementation blockers

Status as verified 2026-10-01 (rows unchanged since 2026-09-30 are marked so).
Each row's C-number row in `CONTRADICTION-AUDIT.md` is authoritative.

| Item | Status | Remaining action |
|---|---|---|
| C-05.3a security dispatch | **Implemented and COMMITTED** — row corrected 2026-10-01 | The "UNCOMMITTED" status was stale: the work landed in `6880229` and is tracked and CI-visible. `git status --porcelain` over `control/` and `tests/` is empty. Its one open defect — `security_claim_is_valid` raising `TypeError` on an unhashable `verdict` or `claim_state` instead of returning a diagnostic (handover §36.5 D1) — was repaired 2026-10-01 in this module, policy unchanged, with safe denial tested and both guards mutation-proved |
| C-18 transaction boundary | **OPEN — all 7 stages and the annunciate residual now implemented; the rehearsal and T+00 clauses keep it open** | Stage 1 (`route_prs` GitHub observation moved outside T1) and stage 2 (notification delivery moved behind a durable intent queue and a bounded post-commit drain, corrected 2026-10-01 so the drain limits are per tick, the send budget is measured after the fence, and a transport-level ambiguous outcome is no longer retried as a failure) implemented and mutation-checked 2026-10-01. Stage 3 (`workers.allocate_port` split into a no-bind under-lock selection half and an external probe) implemented and mutation-checked 2026-10-01 — but the dispatch call sites were **not** migrated, so port binds have **not** left T1 yet. **Stage 4 (builder) landed 2026-10-01** on a shared dispatch harness — plan writes only state inside T1, execute runs lockless after it commits, commit/fail re-verify the claim — which is what makes stages 5 and 6 genuinely parallel from here. **Stages 5 (reviewer) and 6 (fixer) landed 2026-10-01**, built in parallel on separate branches and meeting at integration in one adjacent-line conflict. Stage 5 also SHA-binds the review ledger events and repairs a KeyError in `on_dispatch_failure`. **STAGE 7 AND THE ANNUNCIATE RESIDUAL BOTH LANDED 2026-10-01** under explicit operator delegation. Stage 7 bounds five post-T1 phases that previously declared nothing at all, each from the sum of its OWN external timeouts rather than from the worker's lease - the proposed table priced builder execution at 3600 s, the lease, when the phase only spawns and returns, which would have let a wedged `workmux add` look healthy for an hour against a 120 s threshold. `execute_merges` was missing from the proposal entirely and is included. The annunciate residual is closed by the notification amendment: the Supervisor's path queues a durable intent and the drain delivers it, while the Watchdog's path still sends immediately because it has no drain and exists for the case where the Supervisor is not ticking. Row C-18: "REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended stress test, and T+00" — the **5-hour clause is waived** by human decision 2026-10-01 (audit row C-18a); the other two clauses stand. **WHICH stage, named 2026-10-02 instead of left as "one stage":** stage 3. Audit row C-18 closes with "no numbered stage and no named residual remains unimplemented", and that is true — stage 3 IS implemented. What is not done is the call-site migration this same cell records above: `workers.allocate_port` was split, but the dispatch call sites were never moved onto the split, so **port binds have not left T1**. Implemented and adopted are different claims, and the unnamed "one stage" let them read as the same one |
| C-19 Jev implementation is not Jev | **OPEN — `worker_health` path implemented and live-verified 2026-10-01; governance items remain** | `control/jev.py` now posts to `/api/alpha/decisions` with `typesafe/jev-1.13`, records requested and returned model identifiers separately, and refuses the four kinds whose `criteria` are unapproved before any HTTP call. Budget denial prevents the call from **both** the Supervisor and `gate_jev`, through a durable reservation committed before the request leaves and settled after it (governed bound $0.002688/call, basis in `config/experiment.json`). A request lost in flight — a crash between sending and settling — is marked `ABANDONED` and that one logical consultation is never re-sent, so neither a Supervisor restart nor a re-run of `ctl preflight` can re-buy an answer that may already have been paid for; the identity is the task plus its attempt, worker, progress marker and state (§25.4), so a new attempt or real progress is a new question that proceeds, while polling and the clock are not (§25; the earlier claim that exposure arithmetic alone did this was wrong and is withdrawn). Verified by mocked transport only: 1595/1595 tests, 8/8 plus 8/8 plus 6/6 mutations detected. **Live-verified 2026-10-01**: one budget-gated `worker_health` request returned `HEALTHY` from `typesafe/jev-1.13-20260917` for $0.000022764, settling its reservation to zero exposure (§27). **C-19's `worker_health` integration is complete and live-verified**; the endpoint remains alpha, which is a standing risk rather than an open task. The four governance questions about the kinds that are NOT wired moved to audit row **C-19a** on 2026-10-01 and are explicitly **not a T+00 blocker** — they concern kinds with no call site and imply no code change. Record in `C05-3a-SESSION-HANDOVER.md` §24 (implementation), §25 (crash/restart correction) and §27 (live verification) |
| C-05.3b accessibility dispatch | **BOTH HALVES NOW GENUINELY REACH THE TICK 2026-10-01; C-02a resolved** | `WAITING_EVIDENCE` has a governed exit (`advance_if_evidence_complete`), reached from the security ingest AND from `route_evidence` itself. The AUTOMATED half runs G3's five phases on G2/G9's claim/execute/commit lifecycle with every external service injected; each phase gets `min(own bound, what remains of 1080 s)`, teardown is a `finally`, and the port returns only once the listener is observed gone. The QUALITATIVE half parses and adjudicates the reviewer's block against the canonical registry, with a SHA-bound worker name and G1's lease. The tick plans, executes outside every transaction, and commits with re-verification. **CORRECTION TO THIS ROW'S EARLIER WORDING:** "both halves built and wired" overstated it. The automated half was tick-connected but gated on a services factory that was `None`, so it never planned; the qualitative half was never called from `route_evidence` at all. Both are now genuinely connected. **C-02a is RESOLVED** - the frozen example reads `ACC-DOD-VISIBLE_FOCUS` under a recorded freeze amendment, and the remaining sixteen identifiers reach the reviewer through the `{{evidence}}` substitution rather than through any further frozen-file change or parser alias. **The services factory now exists** (`control/accessibility_services.py`), so the automated half is no longer inert: G3's clean isolated checkout at the trusted SHA, process-group teardown, and disposal after the verdict is durable. **The qualitative dispatch now exists**: claim in T1, spawn outside it, confirm in its own transaction, ingest through the ordinary reaper. **Remaining, and unchanged:** no real accessibility scan has ever run - no product exists, every service in every test is injected, and the dispatch has never faced a real PR or URL |
| C-04 live merge-gate composition | **OPEN — composition layer built and now exercised end to end through the C-04a fixture; never run against real GitHub, and not wired into the Supervisor** | The CI-result and reviewer-identity adapters landed 2026-10-01 (57/57 tests, 17/17 mutations), and `ci.yml` now runs the apparatus suite and the validator — it previously ran **no Node test at all**. The composition layer now computes a live decision and is driven end to end by the fixture harness, which caught the irrelevant-security-surface `sha` defect that made the gate unopenable. Still missing: any exercise against real GitHub, and the join to the control plane — `control/supervisor.py` calls `control/routing.py::evaluate_merge`, never `live-gate.js`, so two merge gates exist in two languages and only one is reachable from a tick |
| C-02 accessibility severity floor integration | **OPEN — registry built, wired, and now readable from the Python control plane** | The 17-entry requirement registry is derived from the frozen `product/ACCESSIBILITY.md` and derivation-checked, so drift fails the build; the frozen `SEVERITY_POLICY_FILES` hash is unchanged and `control/severity.py` is untouched. **New 2026-10-01:** the registry is serialised to a generated JSON that `control/accessibility_registry.py` reads at import, closing handover §36.5 D2 — until then it was JavaScript-only and no Python caller could supply `known_requirement_ids`, so every accessibility FAILURE rated INVALID whatever it cited. All 17 `ACC-DOD-*`/`ACC-COG-*` identifiers are asserted to rate through the real policy; the pre-C-02 kebab-case form still fails closed. A recognised citation is still not a confirmed one. **Still open: G6**, the nine `check_id` → requirement map, which is policy and keeps the automated-FAIL path inert-but-safe. Remains coupled to C-04 |
| C-04a realistic multi-cycle preflight | **Fixture harness now drives the PRODUCTION composition 2026-10-01; the scenario AND the composition layer are proven, the integrated system is not** | The stub decider is replaced by `apparatus/pr-evidence/live-gate.js` with all four C-04 adapters real and only external services injected. The full lifecycle completes: ≥2 review cycles, exact-SHA invalidation, P2 non-blocking, draft→ready→merge with no human actor, dependents unblocked. **It exposed a defect that would have stalled every product PR at T+00** — `live-gate.js` demanded a `sha` on irrelevant security surfaces the schema exempts, so the gate could never open; repaired. **Remaining:** the Supervisor never calls `live-gate.js` (`control/routing.py::evaluate_merge` is the Python-side gate and the two are unjoined), and nothing has run against real GitHub. **No longer coupled to the endurance rehearsal**, which is waived (C-18a); it remains a prerequisite in its own right |
| Runtime merge gate requires evidence | **CLOSED 2026-10-01** | `routing.evaluate_merge` consulted no evidence at all: a PR with `REVIEW_PASS`, a current approval, matching head and diff, green CI and `CLEAN` returned "all merge gates satisfied" with **no security and neither accessibility leg** — reproduced, not inferred, and reachable in ordinary routing because a reviewer is dispatched from `PR_OPEN` and `REVIEW` is not an evidence state. It now calls `review_gate_fires` and denies `EVIDENCE_INCOMPLETE`. 18 tests across 5 modules were passing for the wrong reason and now carry real evidence. **Still open: `control/severity.py::apply_severity_policy` has no caller in `control/`**, so the deterministic P0/P1 floor and registry validity check run only in `apparatus/`, which the runtime never invokes — blocked on C-05.3b's ingest. Handover §38 |
| C-10.2 live closure evidence | **PARTIAL** | Arrives during a real-merge rehearsal; must not be manufactured |
| C-12 evidence preservation | **NOT_IMPLEMENTED** — row corrected 2026-10-01 | This row read PARTIAL while audit row C-12 reads `OPEN / NOT_IMPLEMENTED`. By this document's own rule the audit wins, so the status is corrected to match. The judgement is unchanged and is NOT a launch blocker: the obligation lands at T+24 and `FREEZE-PROTOCOL.md` permits governed post-T+00 apparatus work. The audit's own caveat stands - that only holds if raw evidence is captured incrementally, which depends on C-04/C-05 |
| C-13 immutable manifest | **PARTIAL** — row corrected 2026-10-01 | This row read READY while audit row C-13 reads `PARTIAL — IMPLEMENTATION COMPLETE / CLOSURE EVIDENCE PENDING`. The audit wins. Implementation is complete and `manifest.readiness_check()` was re-run individually 2026-10-01: PASS, "all frozen-input hashes computed successfully". The pending half is closure evidence that only a real `ctl preflight` run and a legitimate T+00 can produce, so it folds into the preflight blocker rather than standing alone |
| `skills/README.md` | **IMPORTED** 2026-10-01 | Copied byte-for-byte from Run 001 commit `0876aa2`, blob `efa42493`; destination SHA-256 `46d3833e…329c`, 1569 bytes, verified. Provenance recorded in `experiment/imported-source.sha256`; `sha256sum -c` passes. No frozen-input manifest hash changed. Nothing further |
| `experiment/LAUNCH-CHECKLIST.md` | **This document** | Complete when the register above stabilises |

---

## Phase 2 — Deployment Readiness / Preflight Remediation

Mandatory. Run **after** phase 1 and **before** T+00, against the actual
deployment environment — not against historical results and not against
unit-test evidence.

### Procedure

1. Run the complete real preflight. Do **not** start T+00.
2. Enumerate every failing gate and environmental prerequisite **concretely** —
   gate name, observed value, governed threshold.
3. For each failure: **FAIL → diagnose → remediate → rerun → PASS.**
4. Carry **no** known pre-T+00 failure into deployment day.
5. Weaken **no** threshold or gate to obtain a PASS.

### The 24 governed gates

`control/preflight.py::GATES`, in order:

`protocol_present` · `task_graph_valid` · `manifest_readiness` ·
`host_headroom` · `required_secrets` · `control_plane_self_tests` ·
`clean_baseline` · `github_remote` · `github_main_protection` ·
`ci_baseline_passing` · `ledger_append_read` · `budget_configured_by_human` ·
`migration_lock_free` · `provider_and_cost_state` ·
`supervisor_watchdog_healthy` · `tmux_master_session` · `workmux_lifecycle` ·
`claude_worker_heartbeat` · `codex_readonly_review` · `grok_bounded_observer` ·
`jev_minimal_decision` · `langfuse_otel_trace` · `supabase_health` ·
`discord_delivery`

`ctl start` refuses unless **every** gate has run and passed
(`control/cli.py:106-122`). A gate skipped by an operator is recorded as skipped
and is not a pass.

### Gate status as verified 2026-10-01

**Eight gates were EXECUTED individually on 2026-10-01**, by calling their
own methods directly. Only gates with no side effect, no network call and
no billable operation were run: `gate_protocol`, `gate_task_graph`,
`gate_manifest_readiness`, `gate_host_headroom`, `gate_secrets`,
`gate_clean_baseline`, `gate_budget`, `gate_migration_lock`. Seven returned
`ok=True`; `required_secrets` returned `ok=False`. Nothing was written, no
ledger event was appended, no tmux session or worktree was created, no
provider was called. **This is still not a `preflight.json`** - see the
bottom row.

**RE-EXECUTED 2026-10-02 in §46, same eight, same results.** Seven `ok=True`;
`required_secrets` `ok=False` with the identical seven names absent and
`~/.config/run-002/` still not existing. `host_headroom` `ok=True` at
inotify instances **48/128 = 37.5%** and watches 36,558/524,288 = 7% -
**measured while three subagents were running**, so it is a figure taken
under load rather than on a quiet host, and the standing caveat is
unchanged. No aggregate preflight was run and no gate with a side effect,
network call or billable operation was touched. **The reports reconcile:
there is no contradiction between the recorded presence results and today's
- they agree name for name.**

| Gate | Status | Evidence |
|---|---|---|
| `protocol_present` | **PASS** — changed 2026-10-01 | `gate_protocol()` returns `ok=True`, `missing: []`. Both previously absent `SPEC_FILES` entries now exist: `experiment/LAUNCH-CHECKLIST.md` (this file) and `skills/README.md` (imported and hash-verified). Run directly; the gate is a pure filesystem presence check with no live or paid effect |
| `host_headroom` | **PASS when executed 2026-10-01** — but see the standing caveat, this is the most volatile gate in the set | inotify instances **77/128 = 60%** against the governed 50% ceiling (`hostcheck.INOTIFY_MAX_FRACTION`), up from 72/128 on 2026-09-30 and drifting upward between consecutive scans. Watches 47,883/524,288 = 9% PASS; CPU, RAM, disk, fd and ports all PASS. **Attribution, measured read-only with `hostcheck`'s own method, not assumed:** RepoQL owns **67 of 77 (87%)**. Three `rql serve` daemons hold 61 between them, and **two of those index unrelated projects** (`learning-companion`, `serina-learning`) for 41 instances of pure external load on a shared per-UID ceiling. `rql mcp` instances scale with concurrent agent sessions, which is why the count drifted during measurement — so a quiet host measures lower than a host running parallel agents, and any re-measurement for a launch decision must be taken on a quiet host. Editors/IDE hold 4, browser automation 2, shell daemon 1. **No leak or orphan was found**: every owner has a live parent and a plausible purpose. The failure is concurrency of tooling, not a defect. **Human decision required** — whether an unrelated project's indexer may be stopped is not the code's call; stopping the two unrelated `rql serve` daemons alone would drop 77 to 36 (28%). The ceiling must NOT be raised and the threshold must NOT be weakened. **RE-MEASURED BY EXECUTING THE GATE 2026-10-01: `gate_host_headroom()` returned `ok=True`, "all host-resource headroom checks passed", with inotify instances at 48/128 = 37.5%, comfortably under the 50% ceiling.** The unrelated `rql serve` daemons that held 41 instances are no longer running. **This does NOT close the item.** The count tracks concurrent tooling, as the measurement above already observed, so a PASS taken while few agent sessions are open says nothing about the host at launch. The human decision about unrelated indexers is deferred, not resolved: if those daemons return, the gate fails again. Any launch measurement must be taken on a quiet host, immediately before `ctl preflight`, and the ceiling still must not be raised. **RE-EXECUTED 2026-10-02: `ok=True`, inotify instances 48/128 = 37.5%, identical to 2026-10-01 - no further drift, and still not a statement about the host at launch** |
| `clean_baseline` | **PASS — gate EXECUTED 2026-10-01** | The recorded cause — "C-05.3a and C-18 stages 1–3 uncommitted" — was **stale**: that work is committed (`6880229`). The three remaining untracked paths were Python bytecode and `.claude/` session machinery, now in `.gitignore` (`f1f0878`). `git status --porcelain` is empty. The gate checks exactly that. **It has now been executed twice**: `ok=True` at "HEAD 01c0413601b2" (2026-10-01) and again at `b66944a` (2026-10-02), individually, not through an aggregate run. The earlier row correctly refused to call an unexecuted gate a PASS; it has been run, so it is one - but it is a PASS at a moment, not a durable record, and it must hold again when `ctl preflight` runs |
| `manifest_readiness` | **PASS** | `readiness_check()` ok. The four frozen-input hashes are unchanged by the `skills/README.md` import, verified before and after |
| `task_graph_valid` | **PASS** | 9 tasks, no errors |
| `control_plane_self_tests` | **PASS** | **2,688** Python tests plus **259** apparatus tests OK, verified 2026-10-02 at `8be6a97` (was 2,364/230 at `6c53540`, and 2,273/218 at `a0d57f6`). The §46 baseline was re-measured from the checkout rather than inherited: `ebfb3d1` was independently confirmed at 2,364/230 before any change. **A defect in this gate's own suite was found and fixed on 2026-10-02:** running it WROTE `.runtime/state.json` - 16 writes per run, through `watchdog.main()` in `test_watchdog_liveness` which stubbed the ledger, notifier and pid path but not the state store. A preflight gate that mutates the document T+00 starts from is not a read-only check. Fixed, guarded, and the guard proved red by mutation; the residue it left is reported in `experiment/evidence/LAUNCH-READINESS-2026-10-02.txt` §6 and was deliberately NOT reset. This row previously read 1852/197, which was the session-start baseline quoted as if it were the gate's current evidence; corrected. Growth: (1367 → 1404 after C-18 stage 1 → 1452 after stage 2 → 1477 after the stage 2 corrections → 1511 after stage 3 → 1569 after the C-19 `worker_health` repair → 1595 after the C-19 crash/restart correction → 1664 after the C-18 stage-4 harness → 1852 after C-18 stages 5-6, C-04 composition and C-05.3b foundations (1367 → 1404 after C-18 stage 1 → 1452 after stage 2 → 1477 after the stage 2 corrections → 1511 after stage 3 → 1569 after the C-19 `worker_health` repair → 1595 after the C-19 crash/restart correction → 1664 after the C-18 stage-4 harness → 1852 after C-18 stages 5-6, C-04 composition and C-05.3b foundations) |
| `budget_configured_by_human` | **PASS** | $25 in `BUDGET.md` matches `config.budget.total_usd` |
| `jev_minimal_decision` | **PASS** — live, 2026-10-01 | C-19's `worker_health` path exercised against the real provider for the first time: one request to `/api/alpha/decisions`, requested `typesafe/jev-1.13`, **returned `typesafe/jev-1.13-20260917`**, choice `HEALTHY` with `source == "jev"`, confidence 0.98 (advisory), provider-reported cost **$0.000022764** for 542 input tokens, 487.5 ms. The $0.002688 reservation settled as actual spend; exposure back to $0.000000. The reported cost confirms the §24.5 pricing basis exactly (542 × $0.000000042). Evidence: handover §27. **This is one gate, not launch readiness** — the full preflight has not been run and the other 23 gates remain NOT YET EVALUATED |
| `required_secrets` | **FAIL — gate EXECUTED 2026-10-01, this is now a measured failure rather than a prediction** | `~/.config/run-002/secrets.env` **does not exist**, and 7 of the 8 required names are absent from the environment: `DISCORD_WEBHOOK_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`. Only `OPENROUTER_API_KEY` is present — which is why the Jev gate could run at all. Checked by NAME only; no value was read. **Human action: provision all eight into `~/.config/run-002/secrets.env` at mode 600.** This gate blocks `langfuse_otel_trace`, `supabase_health` and `discord_delivery` outright, and supplies credentials the paid gates need. **Re-verified 2026-10-01 by executing `gate_secrets()`: `ok=False`, "not set: DISCORD_WEBHOOK_URL, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, SUPABASE_*".** Existence of the secrets file was tested with `test -f` and the eight names by presence only; no value was read, printed or logged at any point. **RE-EXECUTED 2026-10-02: unchanged, `ok=False`, the same seven names absent. The parent directory `~/.config/run-002/` does not exist either.** Full names, purposes and provisioning steps: `experiment/evidence/LAUNCH-READINESS-2026-10-02.txt` §3-4 |
| `migration_lock_free` | **PASS — gate EXECUTED 2026-10-01** | `gate_migration_lock()` returned `ok=True`, "migration lock initialised FREE". Pure state read, no side effect |
| All remaining 13 | **NOT YET EVALUATED** | Never executed. Three still require paid provider calls (`claude_worker_heartbeat`, `codex_readonly_review`, `grok_bounded_observer`); `jev_minimal_decision` is no longer among them. One (`discord_delivery`) sends a REAL notification to a phone; others start a tmux session, create and delete a probe worktree/branch, launch a worker process, or write a durable ledger event — none is safe to run casually |
| **No `preflight.json` exists** | **blocks `ctl start` regardless of any gate's truth** | `ctl start` reads `.runtime/preflight.json` and refuses unless every gate has run and passed (`control/cli.py:106-122`). No such file exists, so today `ctl start` would refuse on all 24 as never-run. The PASS rows above are documented manual observations, not durable machine records. **Re-confirmed absent 2026-10-02.** **Eleven of the 24 gates have now been individually executed** (the eight on record plus `control_plane_self_tests`, `provider_and_cost_state` and `supervisor_watchdog_healthy`); ten are network/paid and were not run; three - `ledger_append_read`, `tmux_master_session`, `workmux_lifecycle` - are local but SIDE-EFFECTING and were not run either, which the earlier count did not distinguish |

A passing `protocol_present` is a **presence** result only. It says every
`SPEC_FILES` entry exists; it says nothing about whether any of them is
correct, and it authorises nothing.

### Known environmental failure — inotify instances

This host fails the governed C-08c inotify-instance readiness threshold.

It must be **diagnosed and cleared** before deployment, and the governed 50%
threshold must **not** be weakened because this machine fails it. What actually
owns and consumes the instances must be established **from evidence rather than
assumed**. Candidate consumers to check: IDE and editor watchers; Claude and
Codex sessions; development servers; Node and other file-watcher processes; WSL
processes; stale or leaked processes; experiment processes.

Remediation may then mean stopping unnecessary watchers or processes, fixing a
resource leak, changing Run 002's own resource behaviour, or — **only** where
evidence and governance justify it — changing host configuration. **No
remediation is prescribed before diagnosis.**

---

## Phase 3 — Protocol v2 §"Preflight" conditions that no gate checks

Phase 2 runs the 24 gates in `control/preflight.py::GATES`. **Those gates
implement nine of the twenty conditions Protocol v2 §"Preflight" names.** This
phase is the other eleven: conditions that forbid T+00 just as absolutely, that
no gate evaluates, and that therefore cannot appear in `preflight.json` at all.

**This is not a rehearsal, and this document no longer calls it one.** Protocol
v2 contains no rehearsal requirement — the word appears nowhere in `protocol/`,
checked rather than assumed. The earlier title came from this checklist's own
required-content note (`experiment/PREFLIGHT-FINDINGS.md`, recorded
2026-09-26), four days before C-18 existed and five before `REHEARSAL-PLAN.md`
did, so the "complete rehearsal" it asked for was never the five-hour endurance
test and C-18a never reached it. There is nothing here to rerun, because
nothing here has ever run.

**The list of launch prerequisites formerly printed here has moved, because it
was already in Phase 2.** Every machine-checkable entry — headroom, manifest
and freeze integrity, provider and model readiness, budgets, GitHub access,
notification delivery, Supervisor and Watchdog readiness, repository
cleanliness — is a named gate in Phase 2's own enumeration above. Three entries
matched no gate and are carried into the table below rather than lost: resource
ownership and lifecycle, the deterministic secret and security controls, and
evidence and artifact paths.

> **A green `preflight.json` is not a statement that Protocol v2 §"Preflight"
> is satisfied.** It is a statement about nine of its twenty conditions. This
> phase is the rest, and no gate will refuse on its behalf.

### What this phase requires

| # | Condition | Governing source | Precondition | Status |
|---|---|---|---|---|
| 1 | Realistic Builder→PR→Review FAIL→Fix→CI→Accessibility/Security→fresh re-review→merge; **≥2 review cycles**; exact-SHA evidence invalidation/regeneration; P2 demonstrably non-blocking | Protocol v2 §"Preflight"; `BOOTSTRAP.md` deliverable 4; audit C-04a; C-18 clause 1 | **C-04's gate join — a Phase 1 item, NOT Phase 2.** See the note below | Fixture ran 2026-10-01 against the production composition; audit row is PARTIAL, not GREEN |
| 2 | **Contradiction audit proves all frozen documents can be simultaneously satisfied** | Protocol v2 §"Preflight", its FIRST condition; `CONTRADICTION-AUDIT.md`: "T+00 is blocked until … the audit is marked PASS" | every audit row RESOLVED, which requires C-22 | **OPEN.** The audit header says so. **This is the binding pre-T+00 condition, and until 2026-10-02 no phase of this document named it at all** |
| 3 | Deliberate stalled worker detected, healthy worker preserved · deliberate dead Supervisor recovered · long Observer does not create false death | Protocol v2 §"Preflight"; audit C-11 | — | **EVIDENCED** — Trial 0, 2026-09-28, `experiment/evidence/C-11-trial0-scenarios-A-B-C.txt`. Recorded here, not re-run |
| 4 | Run 001 isolation | Protocol v2 §"Preflight"; `BOOTSTRAP.md` deliverable 5 | — | No register row exists. Must be established, not assumed |
| 5 | Role capability matrix live | Protocol v2 §"Preflight"; `BOOTSTRAP.md` deliverable 6 | the four provider gates | Partially carried by those gates; the matrix itself is not evidenced |
| 6 | Worker awareness and independent reconciliation | Protocol v2 §"Preflight" | — | Unit-test evidence only |
| 7 | Fail-before/pass-after regression proof | Protocol v2 §"Preflight" | — | Not established |
| 8 | Fresh context reconstructs solely from durable state | Protocol v2 §"Preflight" | — | Not established |
| 9 | Jev **deterministic floor/override** — the half the gate does not reach | Protocol v2 §"Preflight" | C-05.3b ingest | `control/severity.py::apply_severity_policy` has **no caller in `control/`**, so the floor runs only in `apparatus/`, which the runtime never invokes. See the Phase 1 runtime-merge-gate row |
| 10 | Resource ownership and lifecycle · the deterministic secret and security controls · evidence and artifact paths | this checklist's required-content note; audit C-09 for the first | — | No gate covers any of the three. Carried forward rather than dropped |
| 11 | C-18a's durable-evidence obligations — see "What it adds" below | audit C-18a | — | Verified against what the apparatus records, `C05-3a-SESSION-HANDOVER.md` §37 |

**Item 1 does not wait for Phase 2.** Audit C-04a makes its pre-T+00 half
fixture-bound — "an isolated FIXTURE PR/worktree with real git mechanics and
real (not simulated) schema/validator/adapter calls," with a real product task
PR explicitly **not** required. It needs no secrets, no GitHub remote, no
branch protection and no C-20a(C) decision. What it needs is C-04's gate join:
`control/supervisor.py` calls `control/routing.py::evaluate_merge` and never
`live-gate.js`, so two merge gates exist in two languages and only one is
reachable from a tick. **So item 1 runs in parallel with Phase 2, not after
it** — "each phase gates the next" does not apply to it, and holding it behind
the two human actions delays a check that depends on neither.

**One sentence in audit C-04a pulls the other way, and is not resolved here.**
Requirement (1) says a real product PR is explicitly not required; the same
row's "STILL NOT CLOSED" paragraph lists "no run has occurred against real
GitHub" among what is missing. If that second reading is a closure condition
rather than context, item 1 acquires the eight secrets, C-20a(C) and real
GitHub access as preconditions and moves after Phase 2. **That reading is the
operator's to choose; this checklist does not choose it.**

### The five-hour endurance rehearsal is waived — amended 2026-10-01

`CONTRADICTION-AUDIT.md` C-18 named two steps in sequence, and until 2026-10-01
no authoritative rule permitted collapsing them. **Row C-18a is now that rule.**
Only the middle clause of C-18's "REQUIRED BEFORE" sentence was waived; that
sentence is left unedited in C-18 so the original requirement stays readable.

1. **C-04a realistic multi-cycle preflight** — Builder→PR→Review FAIL→Fix→CI→
   Accessibility/Security→fresh re-review→merge, **≥2 review cycles**, with
   exact-SHA evidence invalidation/regeneration and P2 demonstrably
   non-blocking. Protocol v2 §"Preflight" requires this directly. **STILL
   REQUIRED, and untouched by the amendment** — it is a separate prerequisite
   and must run against the production apparatus, not a stand-in. It is item 1
   above.
2. **Five-hour unattended endurance rehearsal** — **WAIVED as a pre-T+00
   requirement by human decision 2026-10-01 (audit row C-18a).** Run 002 itself
   now serves as the endurance experiment: how long the apparatus operates is an
   observed result, and its failures inform Run 003. The five-hour test must
   **not** be started. `experiment/REHEARSAL-PLAN.md` is retained as the record
   of what it would have been; its decisions D5–D10 are no longer blockers of
   that test and are reclassified in that document's **§9** — D6 only **partly**,
   because its evidence-retention and autonomous-versus-human half is re-homed
   onto the real run under C-18a and is live below.

**What the waiver does not touch.** Realistic multi-cycle verification through
the production apparatus, every required launch gate, independent review, the
accessibility and security checks, exact-SHA evidence, budget enforcement,
recovery checks and stop controls all stand exactly as before. The waiver is of
one duration, not of any safeguard. Protocol v2 §"Preflight" is not amended by
it — the five-hour figure originated in this repository's own C-18 row and
`REHEARSAL-PLAN.md`, never in Protocol v2.

**What it adds.** Because the run is now the experiment, its durable evidence
must let a reader reconstruct the first failure, elapsed runtime, task and PR
state, recovery attempts and the stopping reason, and must distinguish
autonomous recovery from human intervention. The run clock must not be reset or
extended to conceal downtime (Protocol v2: "The 24-hour clock never pauses"),
and an early stop must never be reported as a completed 24-hour run. Verified
against what the apparatus actually records in
`C05-3a-SESSION-HANDOVER.md` §37.

---

## Phase 4 — T+00

1. Confirm every phase-1 blocker closed.
2. Confirm phase-2 remediation complete, with no known failure carried forward.
3. Confirm phase-3 rehearsals passed.
4. Run a **final complete clean preflight immediately before T+00**.
5. T+00 begins **only after that final preflight passes**.

Freeze obligations at T+00 and T+24 are governed by
`experiment/FREEZE-PROTOCOL.md`, not by this document.

---

## Protocol v2 §"Preflight" — the governed list

Reproduced for traceability. T+00 is forbidden until all pass:

contradiction audit proves all frozen documents can be simultaneously satisfied ·
immutable manifest/hashes · Run 001 isolation ·
host CPU/RAM/disk/fd/inotify/port headroom · role capability matrix live ·
one Supervisor/Watchdog · worker awareness and independent reconciliation ·
deliberate stalled worker detected, healthy worker preserved ·
deliberate dead Supervisor recovered · long Observer does not create false
death · realistic Builder→PR→Review FAIL→Fix→CI→Accessibility/Security→fresh
re-review→merge · at least two review cycles · exact-SHA evidence
invalidation/regeneration · P2 demonstrably non-blocking ·
fail-before/pass-after regression proof · fresh context reconstructs solely from
durable state · Discord lifecycle delivery · Jev classification plus
deterministic floor/override · independent $25 budget ·
**no partial preflight can authorize start**.

Three of these are already evidenced (C-11 Trial 0, 2026-09-28): stalled-worker
detection, dead-Supervisor recovery, and long-Observer false-death avoidance.

---

## What this document is not

Not a preflight gate in itself — it satisfies one `SPEC_FILES` entry, which is a
presence requirement, not a correctness claim. Not a priority order. Not an
authority to launch. It resolves no C-number, and does not imply any item listed
here is closer to done than its own row in `CONTRADICTION-AUDIT.md` states.
