# Run 002 — Preflight Findings Register

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md`, `experiment/CONTRADICTION-AUDIT.md`
(all C-numbers), `control/preflight.py` (`SPEC_FILES`, `GATES`). This document is a
point-in-time inventory of everything currently known to stand between the
repository and a legitimate T+00, organised by what kind of thing each item is —
it does not itself resolve, implement, or reprioritise any of them. Where an item
has a C-number, that row in `experiment/CONTRADICTION-AUDIT.md` is authoritative;
this document references it rather than restating its content.

T+00 remains **NOT_STARTED** (see `experiment/TIMELINE.md`). No partial state of
this register authorises launch.

## OPEN BLOCKER

Requirements Protocol v2 names explicitly as pre-T+00 gates, or whose absence
directly prevents a legitimate preflight PASS.

| Item | Summary | Source |
|---|---|---|
| C-02 | Accessibility P1 severity floor is implemented and unit-tested, but integration with the PR evidence validator and independent reviewer evidence remains incomplete; closure is coupled to C-04. | `CONTRADICTION-AUDIT.md` C-02 |
| C-04 | Live merge-gate composition incomplete: CI-result and reviewer-identity adapters unbuilt, no composition layer, and — newly confirmed — CI never invokes the PR-evidence validator at all, despite Protocol v2 stating "CI validates the schema." | `CONTRADICTION-AUDIT.md` C-04 |
| C-04a | Realistic multi-cycle fixture preflight (Builder→PR→Review FAIL→Fix→CI→Accessibility/Security→re-review→merge, ≥2 cycles) not yet run. | `CONTRADICTION-AUDIT.md` C-04a |
| C-05 | Accessibility/Security dispatch integration not built — newly confirmed, even once built, no artifact-persistence path exists (stdout-only results, gitignored SHA-only screenshots, overwrite-on-rerun, no persisting caller). | `CONTRADICTION-AUDIT.md` C-05 |
| C-08a | Runtime/concurrency metrics (CPU/RAM/disk, inotify, port contention, process/worktree/browser counts) not implemented or only partial. | `CONTRADICTION-AUDIT.md` C-08 |
| C-08c | IMPLEMENTATION COMPLETE / CURRENT HOST READINESS FAILING. control/hostcheck.py and gate_host_headroom in control/preflight.py's GATES tuple exist and are tested (tests/test_hostcheck.py, 49/49 pass). Real-host read-only execution (2026-09-25): CPU/RAM/disk/fd/inotify-watches/ports all PASS; inotify instances FAIL — 113/128 against the governed maximum of 64. | `CONTRADICTION-AUDIT.md` C-08 |
| C-09 | OPEN / PARTIAL — CORE RESOURCE OWNERSHIP IMPLEMENTATION COMPLETE / BROWSER CONTROL-PLANE FEED PENDING C-05 (2026-09-27). Core worktree/process/port/lease lifecycle — including bidirectional orphan detection with annunciation-only handling, lease-expiry HUMAN_REQUIRED escalation, and governed fail-closed lease_expired resolution — is implementation-complete and verified (C-09 lifecycle 67/67; mutations 26/26 detected; canonical 859/859; secret scan and git diff --check clean). Browser runner-local lifecycle is complete; the browser control-plane feed and its orphan detection remain pending C-05, so C-09 remains OPEN / PARTIAL and is not RESOLVED. | `CONTRADICTION-AUDIT.md` C-09 |
| C-10 | OPEN / PARTIAL. **C-10.1 consistency contract — IMPLEMENTATION COMPLETE (2026-09-26).** `control/routing.py` establishes a canonical 11-gate vocabulary (`KNOWN_GATES`, test-locked against `prompts/reviewer.md`), restricts gate values to `PASS`/`FAIL`, and requires every finding to carry an explicit severity validated as P0/P1/P2/P3 and an explicit category validated against the canonical gates — the former implicit missing-severity → `P2` default is removed. A `REVIEW_PASS` carrying any P0/P1 remains inconsistent; a FAIL gate backed only by valid P2/P3 no longer independently blocks it; an unbacked FAIL gate and any malformed severity, category or gate value fail closed. `REVIEW_FAIL` authority is never upgraded to PASS. `prompts/reviewer.md` now requires P2/P3 findings to be reported even on a pass, with `category` naming exactly one canonical dimension. Verified: `tests/test_control_plane.py` 81/81; canonical full suite 458/458; contradiction reproduced directly (attributed P2-backed FAIL → consistent; unattributed FAIL → inconsistent; P1-backed FAIL → inconsistent); 17/17 injected defects detected. **C-10.2 durable debt register — IMPLEMENTATION COMPLETE / LIVE CLOSURE EVIDENCE PENDING (2026-09-26).** `control/debt.py` retains a reduced, scrubbed, four-field `accepted_findings` set on the PR record at an approving PASS only, refusing duplicate or unusable finding ids, P0/P1 severities and unknown categories *before* the PR is merge-eligible. Debt is created only after `gh.merge()` succeeds: all-or-nothing, never raising, writing `ACCEPTED_NONBLOCKING` records keyed by the qualified temporary id `<task_id>-PR<pr_number>-<finding_id>`. Each merge records `debt_recording_status` of `RECORDED`/`NOT_REQUIRED`/`FAILED` plus a finite structured `debt_recording_error` on failure only — never exception text. A failed recording never strands a successfully merged task: completion proceeds, a `DEBT_RECORDING_FAILED` event is emitted (`ESCALATION`, `human_intervention=False`), and `MERGED.debt_ids` stays empty so no debt is falsely claimed; on success `MERGED` carries the exact ids. Two distinct cases are both `NOT_REQUIRED` and must not be conflated: an absent `accepted_findings` key (a legacy or in-flight record) and an explicit empty list (reviewed, no accepted P2/P3) — absence of debt never implies legacy state. Verified: `tests/test_debt.py` 57/57; canonical full suite 515/515; `git diff --check` clean; 15/15 injected defects detected; end-to-end harness demonstration (PASS retains the finding → no debt before merge → successful merge creates the record → task COMPLETE → `MERGED` carries the debt id). Limitations: no legitimate real PR has merged through this path yet, so closure evidence is pending and must not be manufactured before T+00; debt ids are qualified keys, not Protocol-form globally stable finding IDs; and Supervisor's pre-existing immediate-ledger/late-state-commit durability window remains, is being classified separately, and is not claimed solved here. C-10.2 owns durable debt; C-04 owns PR-contract/schema/CI surfacing. Not RESOLVED. Remains a pre-T+00 blocker. | `CONTRADICTION-AUDIT.md` C-10 |
| C-11 | Required live demonstrations of stalled-worker detection, dead-Supervisor recovery, and long-Observer false-death avoidance are missing. Underlying mechanisms are implemented; only unit/mock evidence exists for the first two, and the Observer case has no committed passing execution of the specific scenario. | `CONTRADICTION-AUDIT.md` C-11 |
| C-13 | Immutable T+00 manifest/hashes not generated or verified. `protocol/experiment-manifest.template.json` still carries placeholder values for every hash field; no code anywhere computes them or gates on them. Protocol v2 names this explicitly as a Preflight requirement. | `CONTRADICTION-AUDIT.md` C-13 |
| `github.repo` (transitive effect only — see HUMAN_REQUIRED for the assignment itself) | Absence transitively blocks `gate_remote`, `gate_branch_protection`, `gate_ci_baseline`. | `config/experiment.json` |
| `skills/README.md` (preflight consequence only — see SOURCE_RETRIEVAL_REQUIRED for why it cannot currently be resolved) | Absence prevents `gate_protocol` from passing. Not resolvable by inventing the file. | `control/preflight.py::SPEC_FILES` |
| `experiment/PREFLIGHT-FINDINGS.md` | This document — being authored as part of closing this gap. | `control/preflight.py::SPEC_FILES` |
| `experiment/LAUNCH-CHECKLIST.md` | Deliberately deferred until the rest of this register stabilises — cannot honestly represent unfinished C-04/C-05/C-10/C-11/C-13 work yet. **Required content recorded 2026-09-26 so it is not lost: the checklist must contain a mandatory Deployment Readiness / Preflight Remediation phase, run after the required pre-T+00 implementation work and before T+00.** Its governing rule is that failing gates are not made to pass by weakening the gates; the deployment environment is made to satisfy the governed gates. Before T+00: every required pre-T+00 implementation blocker must be closed; the complete real preflight must be run against the actual deployment environment without starting T+00, rather than relying on historical or unit-test evidence; every failing gate and environmental prerequisite must be enumerated concretely; each failure follows FAIL, diagnose, remediate, rerun, PASS; no known pre-T+00 failure may knowingly be carried into Thursday deployment; no threshold or gate may be weakened merely to obtain PASS; the complete rehearsal is rerun until it passes; and a final complete clean preflight is run on Thursday immediately before T+00, with T+00 beginning only after that final preflight passes. **Known current environmental failure:** this host fails the governed C-08c inotify-instance readiness threshold. It must be diagnosed and cleared before deployment, and the governed 50% threshold must not be weakened merely because the current machine fails it. What actually owns and consumes the inotify instances must be established from evidence rather than assumed; candidate consumers to check include IDE and editor watchers, Claude and Codex sessions, development servers, Node and other file-watcher processes, WSL processes, stale or leaked processes, and experiment processes. Remediation may then mean stopping unnecessary watchers or processes, fixing a resource leak, changing Run 002's own resource behaviour, or — only where evidence and governance justify it — changing host configuration. No remediation is prescribed before diagnosis. The rehearsal must cover every applicable governed launch prerequisite, including CPU headroom, RAM headroom, disk headroom, file-descriptor headroom, inotify watches, inotify instances, port availability and contention, workers and processes, browser and resource availability, resource ownership and lifecycle once C-09 lands, immutable manifest and freeze integrity, provider and model readiness, budgets, GitHub access, notification delivery, the deterministic secret and security controls, evidence and artifact paths, Supervisor readiness, Watchdog readiness, repository and configuration cleanliness and freeze requirements, and every other applicable Protocol preflight gate. The launch principle is that Thursday is confirmation of an already-rehearsed known-good environment, not the first time deployment failures are discovered. This entry records required content only: experiment/LAUNCH-CHECKLIST.md remains deliberately uncreated, this planning update must not make gate_protocol pass, and the actual checklist is written only once its existing prerequisites are sufficiently complete. | `control/preflight.py::SPEC_FILES` |

## OPEN REQUIRED EXPERIMENT CAPABILITY

Requirements that must be satisfied by their Protocol-defined point in the
experiment, but are not themselves an explicit T+00 Preflight gate.

| Item | Summary | Pre-T+00 dependency | Source |
|---|---|---|---|
| C-12 | T+24 evidence preservation and final-report discoverability are incomplete: `supervisor.freeze()` snapshots task/PR/provider/budget state but not the evidence corpus or an index of it; `cmd_report` produces only aggregate counts, not a per-task/per-PR evidence index. | **Critical dependency, and this part genuinely is pre-T+00:** durable raw-evidence capture (the C-04/C-05 evidence-persistence gaps) must exist *before* evidence-producing activity begins, because evidence never captured cannot be reconstructed after the fact. The T+24 snapshot/report *rendering* code itself is a T+24 obligation and may be improved post-T+00 under Protocol v2's governed-deviation process — but only if the raw material to render already exists. | `CONTRADICTION-AUDIT.md` C-12 |
| C-14 | Merge-durability / irreversible-effect transaction window. **C-14.1 commit boundary and C-14.2 independent three-source detection/annunciation — both IMPLEMENTATION COMPLETE (2026-09-26).** Irreversible merges now commit in their own revalidating transaction, and GitHub, ledger and state disagreement is classified into ORDINARY_EXTERNAL, LOST_LOCAL_COMMIT, DEBT_DIVERGENCE or UNPROVABLE by a pure classifier the Supervisor and the Watchdog each run over their own observations, with dangerous findings frozen where legal and annunciated as STATE_INVARIANT_VIOLATION otherwise. **C-14 no longer blocks T+00.** What remains is C-14.3 — reconstruction of lost state and debt, ledger repair or deduplication, and a general idempotency architecture — which is governed post-T+00 work: C-14.2 detects, annunciates and freezes, but never repairs. C-14 therefore stays OPEN / PARTIAL rather than RESOLVED. See `experiment/CONTRADICTION-AUDIT.md`. **D2 AMENDMENT (2026-09-27) — C-14-owned pre-T+00 blocker:** D2's reconciliation falsely assumes REVIEW/FIX_REQUIRED always carry a live worker record; routing legitimately allows those states to wait with none (human RETRY of repair_cycle_limit or fixer_failed, and reviewer/fixer dispatch-failure windows), so the Watchdog can emit a false MISSING_TASK_WORKER_REF / ORPHANED_TASK_WORKER_REF and freeze a healthy task — destructive while S12 has no RETRY/RESUME before C-14.3. The same deliverable also persists raw str(exc) in RECONCILE_ERROR, conflicting with the C-08b.2/C-09 finite-structural-metadata rule. See the C-14 audit-row amendment for both pending smallest corrections. | **Two pending pre-T+00 corrections under the D2 amendment:** narrow the missing-worker-reference reconciliation assertions to ASSIGNED/ACTIVE while retaining all record-present checks in every reconcilable state, and replace RECONCILE_ERROR's str(exc) with fixed finite metadata plus canary/mutation coverage. C-14.1 and C-14.2 remain complete; C-14.3 remains post-T+00 governed work and is not brought forward. | Protocol v2 §"Operational rules" (idempotency keys, durable state survives context loss), §"Worker awareness" (independent reconciliation, STATE_INVARIANT_VIOLATION), §"Preflight" (fresh context reconstructs solely from durable state) |

## OPEN NON-BLOCKING FINDING

| Item | Summary | Source |
|---|---|---|
| Dispatch-observability finding | A task excluded from `dispatchable()` for any reason (no slot, unmet dependency, provider hold, or formerly phase) produces no signal distinguishing why. | Recorded during C-07's implementation. |
| `EvidenceItem` vs. `$defs.provenance` field gap | `control/evidence.py::EvidenceItem` (`source, kind, head, applies, detail`) doesn't implement `created_at`/`producer`/`scope`/`result`/`artifact_reference`. Recorded as the same gap as C-04, not duplicated. | `experiment/LEDGER-SCHEMA.md` |
| `clock.py` stale docstring | `# Deadline phases from experiment/EXPERIMENT.md` no longer describes anything true, post-C-07. Harmless; never corrected; out of C-07's bounded scope. | `control/clock.py` line 13 |
| `config/isolation.json` `worktree_dir` declaration | Declares `worktree_dir: ".worktrees"`, but no runtime or preflight code consumes the field (only `candidate_tcp_port_range` is consumed, by C-08c's hostcheck), and actual workmux behaviour — verified via `workmux config reference` with no global or repository override — creates worktrees in the sibling `<project>__worktrees` directory. C-09's reverse worktree detection correctly follows actual workmux behaviour; operational isolation from Run 001 is intact (distinct sibling roots per repository). Declaration/runtime truthfulness to be reconciled later under the C-06 isolation decision's governance; non-blocking; the config file is deliberately unedited. | `CONTRADICTION-AUDIT.md` C-06 (surfaced during C-09 review) |

## HUMAN_REQUIRED

| Item | Summary |
|---|---|
| `github.repo` | `"UNASSIGNED"` in `config/experiment.json`. Assignment is explicitly reserved to you, not to be inferred or defaulted. |

## SOURCE_RETRIEVAL_REQUIRED

| Item | Summary |
|---|---|
| `skills/README.md` | Imported Run 001 requirement (commit `327f33a`), referenced in `prompts/builder.md`'s reading order, but the file itself was never carried over and no content definition exists anywhere in Run 002. |
| C-06's 49-file provenance manifest | The "0 mismatches across 49 files" claim exists only as commit-message prose; no persisted, independently-checkable hash record exists. A Run-002-only manifest would not provide the missing independent proof, per your prior decision not to create one. |

## UNKNOWN / NOT_YET_EVALUATED

| Item | Summary |
|---|---|
| `gate_secrets` | Required-secrets presence/permissions — not checked this session, per credential-handling caution. |
| `gate_supabase`, `gate_langfuse`, `gate_discord` | External service dependencies — no live check run this session. |

## UNRESOLVED_INTERPRETATION

| Item | Summary |
|---|---|
| `HUMAN_VERIFICATION` reader/no-writer ambiguity | `control/evidence.py` reads ledger events of type `HUMAN_VERIFICATION` as a possible independent evidence source; nothing anywhere writes one. Protocol v2 defines `HUMAN_VERIFICATION` only as a human-intervention classification type, not explicitly as a required independent evidence producer. Open question: is this a missing writer, or dead/aspirational code in `evidence.py`'s own design? Not resolved. |
| Apparatus-development evidence provenance scope | Whether Protocol v2's §"Evidence provenance" (source/SHA/created_at/kind/producer/scope/result/artifact_reference) applies to `experiment/evidence/*.txt` apparatus-development notes, or exclusively to per-SHA PR/review evidence (as `PR-EVIDENCE-V2.schema.json`'s `$defs.provenance` already implements). Textual evidence in Protocol v2 leans toward the latter (producer-not-Builder/Fixer and new-SHA-regenerates-evidence only make sense for PR evidence), but this is not decided here. |

## RESOLVED / CLOSED

| Item | Summary |
|---|---|
| C-01, C-01a | Jev control-plane vocabulary and incident-classification enum — RESOLVED. |
| C-03 | PWA scope adoption — RESOLVED. |
| C-06 | BOOTSTRAP-import governance interpretation — RESOLVED (the historical provenance-manifest follow-up remains SOURCE_RETRIEVAL_REQUIRED, tracked separately above, and does not reopen this). |
| C-07 | Phase-based dispatch gating removed — **RESOLVED and IMPLEMENTATION COMPLETE**, committed `e9944405d9a25f238831b01d0444eaebc7d75026`. Not a blocker. |
| C-07a | Checkpoint cadence (T+06/T+12/T+18/T+21/T+24) — RESOLVED. |
| C-08d | Shared provider-quota contention interpretation — RESOLVED; existing `providers.py`/ledger evidence sufficient for the core measurement. Not a blocker. |
| C-08b | Human-intervention lifecycle and production integration — **IMPLEMENTATION COMPLETE** (C-08b.1 foundation 2026-09-26; C-08b.2 production integration 2026-09-27). All twelve production escalation conditions S1–S12 — six task-scoped Supervisor sites, the RED guardrail path, the budget hard stop, the Watchdog crash-loop/restart-failure paths, the C-14.2 merge-invariant obligation and the D2 worker-state freeze — now create durable intervention records with deduplication, identity-carrying notifications, the production secret-reason boundary, and governed resolution execution in `ctl human-resolve`. Verified 2026-09-27: focused 397/397, canonical 761/761, mutation checks 11/11 detected, secret scan and `git diff --check` clean. No longer a pre-T+00 implementation blocker. Traceability: `CONTRADICTION-AUDIT.md` C-08, which remains OPEN overall — C-08a is unimplemented and C-08c currently fails host readiness on this host — and nothing here implies T+00 has started. |
| C-15 | Migration-lock abandonment lifecycle — **RESOLVED / IMPLEMENTATION COMPLETE** (2026-09-27). Fresh-owner pre-execution dispatch failures now release the lock automatically with task-owned durable evidence and tick-converged ledger repair; re-entrant owners go directly to HUMAN_REQUIRED with a `builder_dispatch_failed` intervention instead of invisible BLOCKED; `builder_attempts_exhausted` gains owner-conditional FAIL; every lock-releasing human FAIL requires a durable note attestation; NO_ACTION and every retry path retain ownership; the successful-merge and C-08b.2 human-FAIL releases are preserved. Verified: canonical 792/792, lifecycle 18/18, focused mutations 13/13 detected, secret scan and `git diff --check` clean. No longer a launch blocker. Traceability: `CONTRADICTION-AUDIT.md` C-15. |
| Budget identity | RESOLVED — `experiment/BUDGET.md` committed, matching Protocol v2 exactly. |
| Residual `run001`/`run-001` identity leaks (incl. OTLP key prefix) | CLOSED — found and fixed in the same evidence entry, commit `3dd928e`. |
| F1 — PR template validation | CLOSED — recorded passing, folded into C-04's PR-contract work. |

## What this document is not

This is a point-in-time register, not a design document, not a priority order,
and not itself a preflight gate. It does not resolve any C-number, does not
imply an implementation sequence, and must be re-verified against the
repository rather than trusted as still-accurate once significant time or
work has passed. Any resolution of an item listed here happens in
`experiment/CONTRADICTION-AUDIT.md` (for C-numbered items) or by direct human
action (for `HUMAN_REQUIRED`/`SOURCE_RETRIEVAL_REQUIRED` items), never by
editing this document alone.
