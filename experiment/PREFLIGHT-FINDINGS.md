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
| C-08b | Human-intervention timing (`requested_at`/`acknowledged_at`/`resolved_at`/active human minutes) and simultaneous-open-`HUMAN_REQUIRED` count not implemented anywhere. | `CONTRADICTION-AUDIT.md` C-08 |
| C-08c | No preflight gate for host CPU/RAM/disk/fd/inotify/port headroom, despite Protocol v2 naming it explicitly. | `CONTRADICTION-AUDIT.md` C-08 |
| C-09 | Worker resource lifecycle (allocate→assign→track→lease→release→detect orphan) incomplete for every resource type; lease expiry and orphan detection absent everywhere; browser and port lifecycles entirely unimplemented. | `CONTRADICTION-AUDIT.md` C-09 |
| C-10 | Reviewer/gate aggregation can still block `REVIEW_PASS` on a P2/P3-only finding (RUN001-F1 reproduction). Governance decision recorded; implementation pending. Closure also requires a P2/P3 debt register, which does not exist. | `CONTRADICTION-AUDIT.md` C-10 |
| C-11 | Required live demonstrations of stalled-worker detection, dead-Supervisor recovery, and long-Observer false-death avoidance are missing. Underlying mechanisms are implemented; only unit/mock evidence exists for the first two, and the Observer case has no committed passing execution of the specific scenario. | `CONTRADICTION-AUDIT.md` C-11 |
| C-13 | Immutable T+00 manifest/hashes not generated or verified. `protocol/experiment-manifest.template.json` still carries placeholder values for every hash field; no code anywhere computes them or gates on them. Protocol v2 names this explicitly as a Preflight requirement. | `CONTRADICTION-AUDIT.md` C-13 |
| `github.repo` (transitive effect only — see HUMAN_REQUIRED for the assignment itself) | Absence transitively blocks `gate_remote`, `gate_branch_protection`, `gate_ci_baseline`. | `config/experiment.json` |
| `skills/README.md` (preflight consequence only — see SOURCE_RETRIEVAL_REQUIRED for why it cannot currently be resolved) | Absence prevents `gate_protocol` from passing. Not resolvable by inventing the file. | `control/preflight.py::SPEC_FILES` |
| `experiment/PREFLIGHT-FINDINGS.md` | This document — being authored as part of closing this gap. | `control/preflight.py::SPEC_FILES` |
| `experiment/LAUNCH-CHECKLIST.md` | Deliberately deferred until the rest of this register stabilises — cannot honestly represent unfinished C-04/C-05/C-10/C-11/C-13 work yet. | `control/preflight.py::SPEC_FILES` |

## OPEN REQUIRED EXPERIMENT CAPABILITY

Requirements that must be satisfied by their Protocol-defined point in the
experiment, but are not themselves an explicit T+00 Preflight gate.

| Item | Summary | Pre-T+00 dependency | Source |
|---|---|---|---|
| C-12 | T+24 evidence preservation and final-report discoverability are incomplete: `supervisor.freeze()` snapshots task/PR/provider/budget state but not the evidence corpus or an index of it; `cmd_report` produces only aggregate counts, not a per-task/per-PR evidence index. | **Critical dependency, and this part genuinely is pre-T+00:** durable raw-evidence capture (the C-04/C-05 evidence-persistence gaps) must exist *before* evidence-producing activity begins, because evidence never captured cannot be reconstructed after the fact. The T+24 snapshot/report *rendering* code itself is a T+24 obligation and may be improved post-T+00 under Protocol v2's governed-deviation process — but only if the raw material to render already exists. | `CONTRADICTION-AUDIT.md` C-12 |

## OPEN NON-BLOCKING FINDING

| Item | Summary | Source |
|---|---|---|
| Dispatch-observability finding | A task excluded from `dispatchable()` for any reason (no slot, unmet dependency, provider hold, or formerly phase) produces no signal distinguishing why. | Recorded during C-07's implementation. |
| `EvidenceItem` vs. `$defs.provenance` field gap | `control/evidence.py::EvidenceItem` (`source, kind, head, applies, detail`) doesn't implement `created_at`/`producer`/`scope`/`result`/`artifact_reference`. Recorded as the same gap as C-04, not duplicated. | `experiment/LEDGER-SCHEMA.md` |
| `clock.py` stale docstring | `# Deadline phases from experiment/EXPERIMENT.md` no longer describes anything true, post-C-07. Harmless; never corrected; out of C-07's bounded scope. | `control/clock.py` line 13 |

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
