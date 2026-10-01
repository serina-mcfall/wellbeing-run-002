# Run 002 — Launch Checklist

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` (§"Preflight"),
`control/preflight.py` (`GATES`, `SPEC_FILES`), `experiment/CONTRADICTION-AUDIT.md`
(all C-numbers), `experiment/PREFLIGHT-FINDINGS.md` (the open-item register),
`experiment/FREEZE-PROTOCOL.md`. Where any of those disagrees with this
document, **they win**. This checklist orders and enumerates; it decides nothing.

T+00 remains **NOT_STARTED** (`experiment/TIMELINE.md`). Nothing in this
document authorises launch, and no partial completion of it does either.

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
3. **Rehearsal** — rerun the complete rehearsal until it passes.
4. **T+00** — a final complete clean preflight immediately before, then start.

Each phase gates the next. A partial pass at any phase authorises nothing.

---

## Phase 1 — Pre-T+00 implementation blockers

Status as verified 2026-10-01 (rows unchanged since 2026-09-30 are marked so).
Each row's C-number row in `CONTRADICTION-AUDIT.md` is authoritative.

| Item | Status | Remaining action |
|---|---|---|
| C-05.3a security dispatch | **Implemented, UNCOMMITTED** | Review and commit the working tree; it is unprotected and invisible to CI |
| C-18 transaction boundary | **OPEN — 4 of 7 stages done** | Stage 1 (`route_prs` GitHub observation moved outside T1) and stage 2 (notification delivery moved behind a durable intent queue and a bounded post-commit drain, corrected 2026-10-01 so the drain limits are per tick, the send budget is measured after the fence, and a transport-level ambiguous outcome is no longer retried as a failure) implemented and mutation-checked 2026-10-01. Stage 3 (`workers.allocate_port` split into a no-bind under-lock selection half and an external probe) implemented and mutation-checked 2026-10-01 — but the dispatch call sites were **not** migrated, so port binds have **not** left T1 yet. **Stage 4 (builder) landed 2026-10-01** on a shared dispatch harness — plan writes only state inside T1, execute runs lockless after it commits, commit/fail re-verify the claim — which is what makes stages 5 and 6 genuinely parallel from here. Stages 5–7 remain: reviewer, fixer, `declare_busy`. Plus one item outside every numbered stage — `merge_invariant.annunciate` still sends synchronously under T1. Row C-18: "REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended stress test, and T+00" — **three stages do not satisfy that** |
| C-19 Jev implementation is not Jev | **OPEN — `worker_health` path implemented and live-verified 2026-10-01; governance items remain** | `control/jev.py` now posts to `/api/alpha/decisions` with `typesafe/jev-1.13`, records requested and returned model identifiers separately, and refuses the four kinds whose `criteria` are unapproved before any HTTP call. Budget denial prevents the call from **both** the Supervisor and `gate_jev`, through a durable reservation committed before the request leaves and settled after it (governed bound $0.002688/call, basis in `config/experiment.json`). A request lost in flight — a crash between sending and settling — is marked `ABANDONED` and that one logical consultation is never re-sent, so neither a Supervisor restart nor a re-run of `ctl preflight` can re-buy an answer that may already have been paid for; the identity is the task plus its attempt, worker, progress marker and state (§25.4), so a new attempt or real progress is a new question that proceeds, while polling and the clock are not (§25; the earlier claim that exposure arithmetic alone did this was wrong and is withdrawn). Verified by mocked transport only: 1595/1595 tests, 8/8 plus 8/8 plus 6/6 mutations detected. **Live-verified 2026-10-01**: one budget-gated `worker_health` request returned `HEALTHY` from `typesafe/jev-1.13-20260917` for $0.000022764, settling its reservation to zero exposure (§27). **C-19's `worker_health` integration is complete and live-verified**; the endpoint remains alpha, which is a standing risk rather than an open task. The four governance questions about the kinds that are NOT wired moved to audit row **C-19a** on 2026-10-01 and are explicitly **not a T+00 blocker** — they concern kinds with no call site and imply no code change. Record in `C05-3a-SESSION-HANDOVER.md` §24 (implementation), §25 (crash/restart correction) and §27 (live verification) |
| C-05.3b accessibility dispatch | **Design PROPOSED 2026-10-01, not approved, not implemented** | `WAITING_EVIDENCE` has no exit transition today; tasks stall there permanently |
| C-04 live merge-gate composition | **OPEN — 4 of 4 adapters now built** | The CI-result and reviewer-identity adapters landed 2026-10-01 (57/57 tests, 17/17 mutations), and `ci.yml` now runs the apparatus suite and the validator — it previously ran **no Node test at all**. Still missing: the composition layer computing a live decision, and any exercise against real GitHub |
| C-02 accessibility severity floor integration | **OPEN — registry built and wired** | The 17-entry requirement registry is derived from the frozen `product/ACCESSIBILITY.md` and derivation-checked, so drift fails the build; the frozen `SEVERITY_POLICY_FILES` hash is unchanged. A recognised citation is still not a confirmed one. Remains coupled to C-04 |
| C-04a realistic multi-cycle preflight | **Not run** | ≥2 review cycles, full Builder→merge path. A **separate prerequisite** from the endurance rehearsal |
| C-10.2 live closure evidence | **PARTIAL** | Arrives during a real-merge rehearsal; must not be manufactured |
| C-12 evidence preservation | **PARTIAL** | Pre-T+00 part is raw capture (exists); T+24 rendering is a T+24 obligation |
| C-13 immutable manifest | **READY** — verified 2026-09-30 | `manifest.readiness_check()` returns ok: "all frozen-input hashes computed successfully" |
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

| Gate | Status | Evidence |
|---|---|---|
| `protocol_present` | **PASS** — changed 2026-10-01 | `gate_protocol()` returns `ok=True`, `missing: []`. Both previously absent `SPEC_FILES` entries now exist: `experiment/LAUNCH-CHECKLIST.md` (this file) and `skills/README.md` (imported and hash-verified). Run directly; the gate is a pure filesystem presence check with no live or paid effect |
| `host_headroom` | **FAIL** — re-measured 2026-10-01, **worse** | inotify instances **77/128 = 60%** against the governed 50% ceiling (`hostcheck.INOTIFY_MAX_FRACTION`), up from 72/128 on 2026-09-30 and drifting upward between consecutive scans. Watches 47,883/524,288 = 9% PASS; CPU, RAM, disk, fd and ports all PASS. **Attribution, measured read-only with `hostcheck`'s own method, not assumed:** RepoQL owns **67 of 77 (87%)**. Three `rql serve` daemons hold 61 between them, and **two of those index unrelated projects** (`learning-companion`, `serina-learning`) for 41 instances of pure external load on a shared per-UID ceiling. `rql mcp` instances scale with concurrent agent sessions, which is why the count drifted during measurement — so a quiet host measures lower than a host running parallel agents, and any re-measurement for a launch decision must be taken on a quiet host. Editors/IDE hold 4, browser automation 2, shell daemon 1. **No leak or orphan was found**: every owner has a live parent and a plausible purpose. The failure is concurrency of tooling, not a defect. **Human decision required** — whether an unrelated project's indexer may be stopped is not the code's call; stopping the two unrelated `rql serve` daemons alone would drop 77 to 36 (28%). The ceiling must NOT be raised and the threshold must NOT be weakened |
| `clean_baseline` | **Cause removed 2026-10-01; gate not yet run** | The recorded cause — "C-05.3a and C-18 stages 1–3 uncommitted" — was **stale**: that work is committed (`6880229`). The three remaining untracked paths were Python bytecode and `.claude/` session machinery, now in `.gitignore` (`f1f0878`). `git status --porcelain` is empty. The gate checks exactly that, so it should pass, but **it has not been executed** and an unexecuted gate is not a PASS |
| `manifest_readiness` | **PASS** | `readiness_check()` ok. The four frozen-input hashes are unchanged by the `skills/README.md` import, verified before and after |
| `task_graph_valid` | **PASS** | 9 tasks, no errors |
| `control_plane_self_tests` | **PASS** | **1664** Python tests plus **127** apparatus tests OK, verified 2026-10-01 (1367 → 1404 after C-18 stage 1 → 1452 after stage 2 → 1477 after the stage 2 corrections → 1511 after stage 3 → 1569 after the C-19 `worker_health` repair → 1595 after the C-19 crash/restart correction → 1664 after the C-18 stage-4 harness) |
| `budget_configured_by_human` | **PASS** | $25 in `BUDGET.md` matches `config.budget.total_usd` |
| `jev_minimal_decision` | **PASS** — live, 2026-10-01 | C-19's `worker_health` path exercised against the real provider for the first time: one request to `/api/alpha/decisions`, requested `typesafe/jev-1.13`, **returned `typesafe/jev-1.13-20260917`**, choice `HEALTHY` with `source == "jev"`, confidence 0.98 (advisory), provider-reported cost **$0.000022764** for 542 input tokens, 487.5 ms. The $0.002688 reservation settled as actual spend; exposure back to $0.000000. The reported cost confirms the §24.5 pricing basis exactly (542 × $0.000000042). Evidence: handover §27. **This is one gate, not launch readiness** — the full preflight has not been run and the other 23 gates remain NOT YET EVALUATED |
| `required_secrets` | **WILL FAIL as things stand** — verified 2026-10-01 | `~/.config/run-002/secrets.env` **does not exist**, and 7 of the 8 required names are absent from the environment: `DISCORD_WEBHOOK_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`. Only `OPENROUTER_API_KEY` is present — which is why the Jev gate could run at all. Checked by NAME only; no value was read. **Human action: provision all eight into `~/.config/run-002/secrets.env` at mode 600.** This gate blocks `langfuse_otel_trace`, `supabase_health` and `discord_delivery` outright, and supplies credentials the paid gates need |
| All remaining 15 | **NOT YET EVALUATED** | Never executed. Three still require paid provider calls (`claude_worker_heartbeat`, `codex_readonly_review`, `grok_bounded_observer`); `jev_minimal_decision` is no longer among them. One (`discord_delivery`) sends a REAL notification to a phone; others start a tmux session, create and delete a probe worktree/branch, launch a worker process, or write a durable ledger event — none is safe to run casually |
| **No `preflight.json` exists** | **blocks `ctl start` regardless of any gate's truth** | `ctl start` reads `.runtime/preflight.json` and refuses unless every gate has run and passed (`control/cli.py:106-122`). No such file exists, so today `ctl start` would refuse on all 24 as never-run. The PASS rows above are documented manual observations, not durable machine records |

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

## Phase 3 — Rehearsal

Rerun the complete rehearsal until it passes.

The rehearsal must cover **every applicable governed launch prerequisite**:

CPU headroom · RAM headroom · disk headroom · file-descriptor headroom ·
inotify watches · inotify instances · port availability and contention ·
workers and processes · browser and resource availability · resource ownership
and lifecycle · immutable manifest and freeze integrity · provider and model
readiness · budgets · GitHub access · notification delivery · the deterministic
secret and security controls · evidence and artifact paths · Supervisor
readiness · Watchdog readiness · repository and configuration cleanliness and
freeze requirements · and every other applicable Protocol preflight gate.

### Two distinct rehearsals, not one

`CONTRADICTION-AUDIT.md` C-18 names them separately and in sequence. No
authoritative rule in this repository permits collapsing them.

1. **C-04a realistic multi-cycle preflight** — Builder→PR→Review FAIL→Fix→CI→
   Accessibility/Security→fresh re-review→merge, **≥2 review cycles**, with
   exact-SHA evidence invalidation/regeneration and P2 demonstrably
   non-blocking. Protocol v2 §"Preflight" requires this directly.
2. **Five-hour unattended endurance rehearsal** — proposed in
   `experiment/REHEARSAL-PLAN.md`, **not authorised**. Six decisions remain
   open there (D5–D10), of which D7 blocks all rehearsal work.

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
