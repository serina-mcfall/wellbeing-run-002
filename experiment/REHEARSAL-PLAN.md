# Run 002 — Five-Hour Endurance Rehearsal Plan

> ## SUPERSEDED 2026-10-01 — DO NOT EXECUTE
>
> **The standalone five-hour endurance rehearsal was removed as a pre-T+00
> requirement by human decision on 2026-10-01, recorded at
> `CONTRADICTION-AUDIT.md` row C-18a.** Run 002 itself now serves as the
> endurance experiment: how long the apparatus operates is an observed result,
> and its failures inform Run 003.
>
> **This document is retained as the historical record of what the five-hour
> test would have been, and for the workload, isolation and safeguard analysis
> in sections 3–6, which remain useful.** It is no longer a plan to carry out.
>
> **Decisions D5–D10 are no longer rehearsal blockers.** D7, previously
> recorded here as blocking all rehearsal work, blocks nothing now — the work
> it gated does not happen. Any of D5–D10 that matters to the real run matters
> on its own terms, under its own C-number, not as a rehearsal prerequisite.
> Section 8's decision table is annotated accordingly.
>
> **What this waiver does NOT touch:** C-04a's realistic multi-cycle
> verification through the production apparatus, the required launch gates,
> independent review, accessibility and security checks, exact-SHA evidence,
> budget enforcement, recovery checks, and stop controls. One duration was
> waived; no safeguard was.

**Status: PROPOSED, NOT AUTHORISED, NOW SUPERSEDED.** Nothing here is created,
launched or spent. No external repository exists. No host setting is changed.
No agent runs.

Original Run 002 T+00 remains **NOT_STARTED**, its state, task graph, `main`
branch and `.runtime/` untouched by anything in this document.

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md`,
`experiment/CONTRADICTION-AUDIT.md`, `control/preflight.py` (`GATES`,
`SPEC_FILES`), `config/isolation.json`, `AGENTS.md`.

---

## 1. Purpose and boundary

**Goal.** Verify the agent factory operates reliably for five continuous hours
under representative agent work.

**What this rehearsal is NOT.** It is not Run 002. It is not T+00. It does not
satisfy C-04a's governed multi-cycle preflight, which remains a **separate
prerequisite** — `CONTRADICTION-AUDIT.md` C-18 lists "unattended multi-cycle
rehearsal" and "the 5-hour unattended stress test" as two distinct steps in
sequence, and no authoritative rule in this repository permits collapsing them.

**Isolation does not waive preflight.** Section 6 identifies which governed
gates apply and names every proposed governance change explicitly rather than
quietly skipping one.

---

## 2. Proposed identity and isolation

A separate checkout with a separate identity, so the original Run 002 is never
written to. Every value below is **proposed** and needs your approval.

| Item | Run 001 (live) | Run 002 (original) | **Rehearsal (proposed)** |
|---|---|---|---|
| Checkout | `~/wellbeing-agent-experiment/agent-run-001` | `…/agent-run-002` | `…/agent-run-002-rehearsal` |
| `experiment_id` | `run-001` | `run-002` | `run-002-rehearsal` |
| GitHub repo | — | `serina-mcfall/wellbeing-run-002` | `serina-mcfall/wellbeing-run-002-rehearsal` (disposable) |
| tmux socket | `run-001` | `run-002` | `run-002-rehearsal` |
| tmux session prefix | `run-001-` | `run-002-` | `run-002-rehearsal-` |
| Worktree root | `…/agent-run-001__worktrees` | `…/agent-run-002__worktrees` | `…/agent-run-002-rehearsal__worktrees` |
| Port range | — | 3200–3299 | **3300–3399** |
| Runtime dir | own | `agent-run-002/.runtime` | `agent-run-002-rehearsal/.runtime` |

### Why a separate checkout is structurally required

`control/config.py:16` sets `RUNTIME_DIR = REPO_ROOT / ".runtime"`, and
`REPO_ROOT` derives from the module's own location. Every runtime path — ledger,
state, heartbeat, PID, singleton lock, evidence, worker logs — hangs off it.
There is no environment override. A second identity in the same checkout would
therefore share `.runtime` with the original Run 002 and could overwrite its
state. A separate checkout is the only isolation the code actually supports.

The worktree root follows automatically: `workers.managed_worktree_root()`
returns the sibling `<project>__worktrees`, so a differently-named checkout
yields a differently-named worktree root with no configuration change.

### Isolation checks before any launch

1. `agent-run-001` untouched — no writes, signals, ports, tmux or worktrees.
2. Original `agent-run-002/.runtime/` unchanged (checksum before and after).
3. Original Run 002 `main` unchanged; the rehearsal pushes only to its own repo.
4. Port ranges disjoint: 3200–3299 vs 3300–3399.
5. tmux sockets distinct, so `tmux ls` on one cannot see the other.
6. Distinct worktree roots, so C-09 orphan detection scopes correctly.

---

## 3. Workload

The rehearsal needs representative agent work for five hours without weakening
any review or merge safeguard.

**Proposed: the real nine-task graph, in a disposable repository.** It is
already governed, already dependency-shaped, and exercises the real dispatch,
review, evidence and merge paths. Because the repository is disposable, merging
consumes nothing that the real experiment needs.

Expected shape, using governed timeouts:

| Window | Activity |
|---|---|
| 0:00–1:15 | TASK-001 Builder (≤3600 s) → PR opened |
| 1:15–1:45 | Reviewer (≤1800 s); FIX_REQUIRED plausible |
| 1:45–2:30 | Fixer (≤2400 s) → new head → evidence regenerated for the new SHA |
| 2:30–3:00 | Security evidence (≤1800 s), accessibility evidence, re-review, merge |
| 3:00–5:00 | TASK-002…005 unlock — up to `max_builders: 3` concurrently |

Concurrency ceilings, unchanged and governed: builders 3, fixers 1, reviewers 1,
security 1, observers 1, `max_repair_cycles` 3.

Continuous background load: Observer every 1800 s (~10 runs), Jev every 900 s
(~20 decisions), `RESOURCE_SAMPLE` every 300 s (~60 samples).

**Safeguards explicitly NOT relaxed:** the six `evaluate_merge` conditions,
the P0/P1-blocks / P2-P3-debt contract (C-05b), exact-SHA evidence
invalidation, and the accessibility P1 severity floor.

**Note:** the checkpoint cadence is T+06/12/18/21/24 (C-07a), so **no checkpoint
fires inside a five-hour window**. That path stays unexercised by this test.

---

## 4. Duration, shutdown and drain

| Item | Value | Source |
|---|---|---|
| Planned duration | 5 h 00 m continuous | this rehearsal's goal |
| Supervisor tick | 30 s | `supervisor.poll_seconds` |
| Watchdog check | 30 s | `watchdog.CHECK_SECONDS` |
| Heartbeat staleness | 120 s | `supervisor.heartbeat_stale_seconds` |

**Bounded shutdown.** At T+5:00 send `SIGTERM` to the Supervisor. `run()` sets
`stopping`, the current tick finishes, `_interruptible_sleep` returns within
1 s, and `SUPERVISOR_STOPPED` is appended. New security dispatch is already
refused while stopping (both the shared dispatch block and Phase C's explicit
guard), so no new paid worker starts during drain.

**Drain bound — MISSING DECISION D5.** No governed value says how long to wait
for in-flight workers before killing them. The longest governed worker timeout
is `builder: 3600 s`. Waiting a full hour past T+5:00 is one defensible reading;
a shorter bound is another. **I am not choosing this for you.**

**Abort conditions (any one ends the run early, and that is a result, not a
failure of the harness):** budget hard stop; crash-loop backstop (3 restarts in
600 s); any `STATE_INVARIANT_VIOLATION`; any governed host threshold breached.

---

## 5. Evidence, fault injection and pass/fail

### Evidence — existing mechanisms only, no new tooling

`.runtime/ledger.jsonl` (append-only, primary) · `.runtime/evidence/` per-attempt
trees · `.runtime/workers/*` · heartbeat history · `ctl report`, `ctl status` ·
`hostcheck.headroom_check()` snapshots before and after.

### Fault injections

Reusing the three techniques C-11 already proved in Trial 0, plus one for the
security path this session repaired.

| ~T+ | Injection | Expected |
|---|---|---|
| 1:00 | `SIGKILL` the Supervisor | Watchdog detects ≤30 s, identity-confirmed restart, no crash loop, work resumes |
| 2:00 | Stall a worker | `STALE_DETECTED` for that task only; healthy workers preserved |
| 3:00 | Long Observer run | No false death; `busy_until` honoured |
| 4:00 | `SIGKILL` a worker mid-attempt | Lease expiry → recovery → **no duplicate paid dispatch** |
| 4:30 | `ctl freeze` | **No new provider worker starts after the freeze**, including resumed claims |

The last one directly exercises the bypass repaired this session.

### Objective pass/fail

| # | Criterion | Threshold | Source |
|---|---|---|---|
| 1 | Heartbeat never stale beyond threshold except under declared busy | 120 s | `heartbeat_stale_seconds` |
| 2 | Exactly one Supervisor throughout | flock holds | C-05.3a repair 1 |
| 3 | `RESOURCE_SAMPLE` count | ≥ 55 of ~60 | `metrics.SAMPLE_INTERVAL_SECONDS` = 300 |
| 4 | Duplicate paid dispatch for one (task, SHA, attempt) | **0** | C-05.3a recovery table |
| 5 | Merge without current `REVIEW_PASS`, current approval and matching diff hash | **0** | `routing.evaluate_merge` |
| 6 | P2/P3 independently blocking | **0** | C-05b |
| 7 | Provider worker started while frozen / stopping / provider-blocked | **0** | this session's repair |
| 8 | Metered spend | within the allocation in §7; hard stop enforced if reached | `budget.py` |
| 9 | False orphan findings for live or owned resources | **0** | C-09, C-05.3a repair 5 |
| 10 | Worktrees leaked at shutdown | **0** with a published outcome unreleased | Phase F |
| 11 | Crash-loop backstop | not triggered | `CRASH_LOOP_LIMIT` 3 / 600 s |
| 12 | Governed host thresholds | none breached during the run | `hostcheck` |
| 13 | Fault injections | all five recover as specified | C-11 Trial 0 |

### Criteria that do NOT exist and must be decided — MISSING DECISION D6

- minimum **work completed** for the run to count as representative (merged
  tasks? PRs opened? review cycles?) — no governed figure exists and I will not
  invent one;
- acceptable number of `HUMAN_REQUIRED` interventions before the run is a fail;
- whether rehearsal evidence is retained as durable evidence or discarded.

---

## 6. Applicable preflight gates

Isolation does not waive preflight. Of the 24 gates in
`control/preflight.py::GATES`:

**Apply unchanged (20).** `task_graph_valid`, `manifest_readiness`,
`host_headroom`, `required_secrets`, `control_plane_self_tests`,
`clean_baseline`, `github_remote`, `github_main_protection`,
`ci_baseline_passing`, `ledger_append_read`, `migration_lock_free`,
`provider_and_cost_state`, `supervisor_watchdog_healthy`, `tmux_master_session`,
`workmux_lifecycle`, `claude_worker_heartbeat`, `codex_readonly_review`,
`grok_bounded_observer`, `jev_minimal_decision`, `langfuse_otel_trace`,
`supabase_health`, `discord_delivery`.

**Need an explicit governance answer (2) — MISSING DECISION D7:**

| Gate | Issue |
|---|---|
| `protocol_present` | **RESOLVED 2026-10-01** in the original checkout — both previously absent entries now exist (`experiment/LAUNCH-CHECKLIST.md` authored; `skills/README.md` imported from Run 001 and hash-verified), and `gate_protocol()` returns `ok=True`. A separate rehearsal checkout must still carry all 28 `SPEC_FILES` itself, since the gate reads `config.REPO_ROOT`. **Not waivable silently.** |
| `budget_configured_by_human` | Compares `config.budget_usd` against the `**USD $N**` figure in `experiment/BUDGET.md`. A rehearsal allocation different from $25 makes the gate fail unless a rehearsal BUDGET document is authored. See §7. |

**Structural consequence.** `ctl start` refuses unless **every** gate ran and
passed (`cli.py:106-122`). With `protocol_present` failing there is no governed
path to set the rehearsal's `started_at`, and without `started_at`
`Supervisor.tick` returns early and dispatches nothing (`supervisor.py:96-100`).
**So D7 must be answered before the rehearsal can do any work at all.**

The rehearsal's own `started_at` is to be set **only when its launch is
explicitly authorised** — not as part of preparing it.

---

## 7. Budget

**The existing $25 is the original experiment's ceiling, not authorisation to
spend another $25.** `experiment/BUDGET.md` and `config.budget.total_usd` define
a single Run 002 ceiling; `config.budget.scope` limits it to "incremental
metered OpenRouter spend only (Jev + product companion)".

### Metered versus subscription

| Kind | Providers | Governed by |
|---|---|---|
| **Metered** (real per-call money) | OpenRouter — Jev decisions + product companion | the $25 ceiling, thresholds 50/75/90/100% |
| **Subscription** (quota, not per-call money) | Claude, Codex, Grok | measured separately; `config.budget.excluded` |

A five-hour rehearsal's *metered* exposure is small — roughly 20 Jev decisions
at 900 s cadence plus companion calls. The material cost is **subscription
quota**: builders, fixers, reviewers and security reviewers all consume Claude
and Codex allowance that the real 24-hour run will also need. Quota exhaustion
during the rehearsal could delay the real experiment even though no extra money
is spent.

### Proposed allocation — MISSING DECISION D8, unapproved

Three shapes, none governed today:

| Option | Meaning | Trade-off |
|---|---|---|
| **(a) Carve-out** | e.g. $2.50 of the existing $25, leaving $22.50 for the experiment | No new money; reduces the experiment's headroom |
| **(b) Separate ceiling** | A distinct rehearsal ceiling, e.g. $5, additional to $25 | Experiment headroom intact; requires you to approve new spend |
| **(c) Zero metered** | Disable Jev/companion metered calls for the rehearsal | No metered spend at all, but leaves the Jev path unexercised — and Jev is one of the things worth testing |

**I recommend (b) with a small figure**, because it keeps the experiment's
governed ceiling intact and keeps the Jev path under test. It requires your
explicit approval of additional spend, which I am not assuming.

Whichever you choose, `experiment/BUDGET.md`'s figure and
`config.budget.total_usd` must agree for `budget_configured_by_human` to pass —
so the rehearsal checkout needs its own BUDGET document stating its own figure.

**Subscription quota — MISSING DECISION D9.** No governed reserve exists for how
much Claude/Codex allowance the rehearsal may consume before it threatens the
real run.

---

## 8. Remaining implementation blockers, in dependency order

| # | Blocker | Why mandatory here | Depends on |
|---|---|---|---|
| 1 | **Decisions D7, D8, D5, D6, D9** | D7 gates whether any work can start at all | you |
| 2 | **Commit C-05.3a** | `clean_baseline` and `ctl start` both refuse a dirty tree; 2,400+ uncommitted lines are unprotected across a 5-hour unattended run | none |
| 3 | **C-18** | `CONTRADICTION-AUDIT.md` C-18, verbatim: "REQUIRED BEFORE: unattended multi-cycle rehearsal, the 5-hour unattended stress test, and T+00" | — |
| 4 | **C-05.3b** | `WAITING_EVIDENCE` has no exit transition in `supervisor.py`; every task stalls there permanently, giving an idle run | C-05.3a committed |
| 5 | **C-04 (+C-02)** | Eight of nine tasks depend transitively on TASK-001 *merging*; two of four live-gate adapters are absent and `ci.yml` never invokes the validator | — |
| 6 | **Host headroom** | Verified 2026-09-30: inotify instances 72/128 = 56% against the governed 50% ceiling (`hostcheck.INOTIFY_MAX_FRACTION`). Threshold must not be weakened | diagnosis |
| 7 | **Rehearsal environment** | Checkout, disposable repo, isolation config, BUDGET document | D7, D8 |
| 8 | **C-04a short multi-cycle rehearsal** | A **separate prerequisite** per C-18's ordering; not satisfied by this test | 2–6 |

Items 3, 5 and 6 can proceed in parallel with each other. Items 2 → 4 are
sequential. Item 8 follows all of them.

**Not authorised by the prompt that produced this plan:** C-05.3b and unrelated
C-18 repairs are listed here as blockers, not as work in progress.

---

## 9. Decisions required

**MOOT AS REHEARSAL BLOCKERS, 2026-10-01 (audit row C-18a).** Every entry below
was a prerequisite of the five-hour rehearsal. That rehearsal is waived, so
none of them blocks anything any longer *in that capacity*. The "Blocks" column
is preserved as written; the "After C-18a" column records what, if anything,
survives on its own terms.

| ID | Decision | Blocks (as written) | After C-18a |
|---|---|---|---|
| **D5** | Drain bound after `SIGTERM` at T+5:00 | shutdown definition | **Moot as written** — there is no T+5:00 SIGTERM. The real run's stop controls and drain are governed by C-18 stage 2's notification drain and the Supervisor's own shutdown path, not by this |
| **D6** | Minimum representative work; acceptable intervention count; evidence retention | pass/fail | **Partly survives, re-homed.** The rehearsal pass/fail criteria are moot. *Evidence retention* and *distinguishing autonomous recovery from human intervention* are now binding on the real run under C-18a's evidence obligations, verified in `C05-3a-SESSION-HANDOVER.md` §37 |
| **D7** | `protocol_present` — retrieve/author the two missing spec files, or govern a rehearsal spec set | **everything; no work can start without it** | **RESOLVED and moot.** Both `SPEC_FILES` entries now exist — `experiment/LAUNCH-CHECKLIST.md` was authored and `skills/README.md` was imported and hash-verified — so `protocol_present` PASSES for the real run. There is no rehearsal spec set to govern |
| **D8** | Rehearsal budget: carve-out, separate ceiling, or zero metered | budget gate | **Moot.** No rehearsal, no carve-out. The real run's single governed $25 ceiling is unchanged and unshared |
| **D9** | Subscription-quota reserve protecting the real run | launch safety | **Moot as written** — there is no rehearsal consuming quota ahead of the run. Whether the real run needs a quota reserve is a live question, but it is not this one and has no C-number yet |
| **D10** | Whether the disposable repo may be created, and by whom | environment | **Moot.** No disposable repository is to be created |

None of these was answered in this document, and none should be inferred from
it. D7's resolution above is a statement of fact about the two files, not an
answer to the question as it was posed.
