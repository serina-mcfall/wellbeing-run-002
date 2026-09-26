# Run 002 — Concurrency and Operational Metrics

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` §"Concurrency metrics",
§"Human intervention taxonomy", §"Operational rules", and §"Preflight" (host
resource headroom). `experiment/CONTRADICTION-AUDIT.md`, C-08 (OPEN /
NOT_IMPLEMENTED, subsidiary decision C-08d RESOLVED) and C-09 (OPEN /
NOT_IMPLEMENTED). Those are authoritative; this file is a status matrix and
must not invent capability they do not document as existing.

Two independent dimensions are tracked for every row:

**Capability** — whether the measuring mechanism exists in code:
- IMPLEMENTED — mechanism exists and is exercised by tests
- PARTIAL — some underlying data exists but is not assembled into the
  required measurement
- NOT_IMPLEMENTED — no code produces this measurement at all
- UNRESOLVED — interpretation of the requirement itself is genuinely open,
  not merely unimplemented

**Runtime data** — whether a real value can exist right now:
- NO_VALUE_YET — capability is IMPLEMENTED, but T+00 has not occurred, so
  only zero/default values exist
- NOT_APPLICABLE — capability is PARTIAL, NOT_IMPLEMENTED, or UNRESOLVED,
  so no meaningful runtime-data question applies yet

T+00 has not occurred (see `experiment/TIMELINE.md`). After T+00, actual
evidence/value availability can be recorded in this column without changing
the Capability column.

## Provider, model and spend metrics

| Metric | Capability | Runtime data | Evidence |
|---|---|---|---|
| Provider quota/cooldowns | IMPLEMENTED | NO_VALUE_YET | `control/providers.py`: per-provider `state`, `cooldown_until`, `consecutive_failures`, `cooldown_count`. |
| Model usage | IMPLEMENTED | NO_VALUE_YET | `control/ledger.py`'s per-event `model`/`provider`/`input_tokens`/`cached_tokens`/`output_tokens` fields; `control/jev.py::DecisionService` records the same per decision. |
| Jev spend | IMPLEMENTED | NO_VALUE_YET | `control/budget.py`'s `spent_usd`/`estimated_usd`/`thresholds_crossed`, fed by `jev.py`'s `actual_cost_usd`. Tested: `tests/test_control_plane.py::TestBudget`. |
| Metered API spend | IMPLEMENTED | NO_VALUE_YET | Same mechanism, scoped to `METERED_PROVIDERS = {"openrouter"}`. |
| Shared Run 001/Run 002 provider-quota contention | IMPLEMENTED (per C-08d effects-based interpretation) | NO_VALUE_YET | `control/providers.py` cooldown mechanics + `control/supervisor.py`'s `PROVIDER_STATE_CHANGE` ledger events at cooldown-start (`is_limit_error`-gated) and cooldown-end (`refresh()`), sufficient to reconstruct when/how-long a contention event occurred, per C-08d. Two documented non-blocking caveats: `safe_hold` overlap is derived, not stored directly; a cooldown open at T+24 is bounded by the freeze event, not a separate end event. See C-08d. |

## Runtime/host metrics — C-08a

| Metric | Capability | Runtime data | Evidence |
|---|---|---|---|
| CPU | NOT_IMPLEMENTED | NOT_APPLICABLE | No code reads `/proc/loadavg` or any CPU signal. See C-08. |
| RAM | NOT_IMPLEMENTED | NOT_APPLICABLE | No code reads `/proc/meminfo` or any memory signal. See C-08. |
| Disk | NOT_IMPLEMENTED | NOT_APPLICABLE | No code calls `shutil.disk_usage` or any disk signal. See C-08. |
| inotify usage | NOT_IMPLEMENTED | NOT_APPLICABLE | Zero occurrences anywhere in `control/*.py`. See C-08. |
| Port contention (metric) | PARTIAL | NOT_APPLICABLE | `control/state.py::new_worker_record` carries a `port` field, but nothing assigns it a real value and no aggregate contention metric exists. The allocator/ownership mechanism itself is tracked separately as C-09. See C-08. |
| Process count | PARTIAL | NOT_APPLICABLE | Per-worker PID liveness is tracked (`control/workers.py`, `control/proc.py`), but nothing aggregates this into a "process count" metric. See C-08. |
| Worktree count | PARTIAL | NOT_APPLICABLE | `control/workers.py::list_worktrees()` exists as an operational helper; not surfaced as a tracked metric. See C-08. |
| Browser count | NOT_IMPLEMENTED | NOT_APPLICABLE | No coordination exists between the Python control plane and the Node/Playwright accessibility runner (`apparatus/accessibility/`) to report this. See C-08. |

## Human-intervention measurement — C-08b

C-08b.1's lifecycle foundation and C-08b.2's production integration are both
implementation complete (C-08b.2: 2026-09-27). The twelve production
escalation conditions S1–S12 — six task-scoped Supervisor sites, the RED
guardrail path, the budget hard stop, the Watchdog crash-loop and
restart-failure paths, the C-14.2 merge-invariant obligation and the D2
worker-state freeze — now feed durable intervention records, joined by
C-15's builder_dispatch_failed condition (a re-entrant migration-lock owner
whose builder dispatch fails pre-execution). Runtime data
remains NOT_APPLICABLE only because T+00 has not started. Notification counts
and intervention-open counts are distinct metrics and are never summed:
C-14.2 re-annunciation may legitimately produce multiple notifications for one
still-open obligation while it remains a single open intervention record, and
Watchdog alerts do not increment the Supervisor notification counter — so
intervention records are the authoritative open-human-obligation surface.
See C-08.

| Metric | Capability | Runtime data | Evidence |
|---|---|---|---|
| Simultaneous open `HUMAN_REQUIRED` events | IMPLEMENTED (C-08b.1); FED BY PRODUCTION (C-08b.2) | NOT_APPLICABLE (pre-T+00) | `control/intervention.py::simultaneous_open_count` counts records whose status is OPEN or ACKNOWLEDGED; surfaced as `human_interventions_open` by `ctl status` and listable via `ctl human-list --status`. Fails closed: an unrecognised status raises rather than returning a misleading count. This counts *intervention records*, not tasks in the `HUMAN_REQUIRED` task state; the C-08b.2 production sites S1–S12 — including the C-14.2 merge-invariant obligation and the D2 worker-state freeze — now feed it, as does C-15's `builder_dispatch_failed` condition. The pre-existing `doc["counters"]["human_interventions"]` lifetime counter is unchanged and is still not a substitute. See C-08. |
| `requested_at` | IMPLEMENTED (C-08b.1); FED BY PRODUCTION (C-08b.2) | NOT_APPLICABLE (pre-T+00) | Set by `control/intervention.py::request` at creation. A deduplicated recurrence reuses the existing record and does not re-stamp it. See C-08. |
| `acknowledged_at` | IMPLEMENTED (C-08b.1); FED BY PRODUCTION (C-08b.2) | NOT_APPLICABLE (pre-T+00) | Set by `control/intervention.py::acknowledge` on the OPEN → ACKNOWLEDGED transition only; a repeat acknowledgement never re-stamps it or overwrites the stored actor. Recorded via `ctl human-acknowledge`. See C-08. |
| `resolved_at` | IMPLEMENTED (C-08b.1); FED BY PRODUCTION (C-08b.2) | NOT_APPLICABLE (pre-T+00) | Set by `control/intervention.py::resolve` on the ACKNOWLEDGED → RESOLVED transition. Acknowledgement is mandatory first, so OPEN → RESOLVED is refused. Recorded via `ctl human-resolve`. See C-08. |
| Active human minutes | IMPLEMENTED (C-08b.1); FED BY PRODUCTION (C-08b.2) | NOT_APPLICABLE (pre-T+00) | Stored as exact integer `active_human_seconds` spanning `acknowledged_at` → `resolved_at`; a negative span is refused rather than stored. `control/intervention.py::active_human_minutes` derives minutes to one decimal at the reporting boundary only, never in durable state. Always computable, because acknowledgement is mandatory before resolution. See C-08. |

## Pre-T+00 host-resource headroom gate — C-08c

| Signal | Capability | Real-host result (2026-09-25) | Evidence |
|---|---|---|---|
| CPU | IMPLEMENTED | PASS | `control/hostcheck.py::cpu_ok`. Tested: `tests/test_hostcheck.py::TestCpu`. |
| RAM | IMPLEMENTED | PASS | `control/hostcheck.py::ram_ok`. Tested: `TestRam`. |
| Disk | IMPLEMENTED | PASS | `control/hostcheck.py::disk_ok`. Tested: `TestDisk`. |
| fd | IMPLEMENTED | PASS (weakly discriminating on this WSL2 host — file-max is 2^63-1) | `control/hostcheck.py::fd_ok`, uses max_handles - allocated_handles per proc_sys_fs(5), never the legacy second field. Tested: `TestFd`. |
| inotify watches | IMPLEMENTED | PASS (10,595/524,288, ~2.0%) | `control/hostcheck.py::inotify_watches_ok`. Tested: `TestInotifyThresholds`. |
| inotify instances | IMPLEMENTED | **FAIL** (113/128 real-UID instances, exceeding the governed 50% ceiling of 64) | `control/hostcheck.py::inotify_instances_ok`. Tested: `TestInotifyThresholds`. |
| Ports | IMPLEMENTED | PASS (97/100 candidate ports bindable, threshold 20) | `control/hostcheck.py::ports_ok`. Tested: `TestPorts`. |
| Overall gate (`gate_host_headroom`) | IMPLEMENTED | **FAIL**, solely due to inotify instances | `control/preflight.py::gate_host_headroom`. |

T+00 remains NOT_STARTED (see `experiment/TIMELINE.md`). This table records one
point-in-time real-host preflight observation. It is not runtime telemetry and
does not predict the host state at the eventual T+00 preflight. Re-run
gate_host_headroom at the actual preflight; this snapshot is evidence that the
implemented gate currently detects a genuine host-readiness failure, not
evidence about future host state.

## Worker resource lifecycle — C-09

Lifecycle stages: allocate → assign → track → lease → release → detect orphan.

| Resource | Allocate | Assign | Track | Lease | Release | Detect orphan |
|---|---|---|---|---|---|---|
| Worktree | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED | NOT_IMPLEMENTED | IMPLEMENTED | NOT_IMPLEMENTED |
| Process | IMPLEMENTED | PARTIAL (PID durable only in an ephemeral status file, not the durable state document) | IMPLEMENTED | NOT_IMPLEMENTED | IMPLEMENTED for the tmux-pane path; unverified for the detached-subprocess fallback | NOT_IMPLEMENTED |
| Browser | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED |
| Port | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED | NOT_IMPLEMENTED |

Runtime data: NOT_APPLICABLE for every cell above — even the IMPLEMENTED
stages (worktree/process allocate-assign-track-release) have no data to
report before T+00; nothing has been dispatched yet.

Evidence: `control/workers.py` (allocate/track/release primitives),
`control/supervisor.py` (`release_review_worktree`, `complete_task`, the
stale-task-recovery path, the T+24 freeze sweep — all four release call
sites verified directly), `control/reconcile.py` (independent worktree/PID
claim-verification — confirmed one-directional: checks that claimed
resources exist, never checks whether existing resources are unclaimed,
which is what orphan detection requires). Claim verification is not orphan
detection. See C-09.

## What this document is not

This is a status matrix, not a design document and not a new source of
requirements. It records what the control plane currently measures and what
it does not; it does not decide how the gaps should be closed, when, or in
what order. Any implementation of a NOT_IMPLEMENTED or PARTIAL row is
tracked and authorised through C-08 or C-09 in
`experiment/CONTRADICTION-AUDIT.md`, not through an edit to this document.
The provider-contention interpretation is governed by C-08d and is not
redefined here. Any change to which metrics Protocol v2 requires is a
Protocol v2 change and belongs in `protocol/RUN-002-PROTOCOL-v2.0.md`, not
here.
