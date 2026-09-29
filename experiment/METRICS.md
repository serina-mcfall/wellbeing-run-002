# Run 002 — Concurrency and Operational Metrics

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` §"Concurrency metrics",
§"Human intervention taxonomy", §"Operational rules", and §"Preflight" (host
resource headroom). `experiment/CONTRADICTION-AUDIT.md`, C-08 (OPEN /
NOT_IMPLEMENTED, subsidiary decision C-08d RESOLVED) and C-09 (OPEN /
PARTIAL — core resource ownership implementation complete 2026-09-27; browser
control-plane feed implementation complete 2026-09-30 via C-05.2). Those are authoritative;
this file is a status matrix and must not invent capability they do not document as existing.

Implementation status is not runtime evidence. T+00 remains **NOT_STARTED**, so every
row below describes mechanisms that exist and are tested, never measurements taken
during a live run.

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

C-08a runtime capture is implemented (2026-09-28): the Watchdog appends one append-only `RESOURCE_SAMPLE` ledger event per governed 300-second interval (immediate first sample on Watchdog start/restart; monotonic gating; a failed attempt advances the cadence point; missing state defers without advancing), carrying a fixed finite 28-key metadata schema in which an unavailable observation class is `null` plus `*_observed=false`, never zero. A top-level sampling failure appends the fixed finite `METRICS_SAMPLE_ERROR` phase/error_code pair, and total ledger unavailability is contained rather than killing the Watchdog. Peaks and averages are not stored; they are derivable later from the append-only samples. Runtime data remains NOT_APPLICABLE only because T+00 has not started.

| Metric | Capability | Runtime data | Evidence |
|---|---|---|---|
| CPU | IMPLEMENTED | NOT_APPLICABLE | `control/metrics.py::sample` records system load1 (`/proc/loadavg` via the C-08c reader — a run-queue length, explicitly not a CPU-utilisation percentage) plus logical CPU count in every `RESOURCE_SAMPLE`. See C-08. |
| RAM | IMPLEMENTED | NOT_APPLICABLE | MemAvailable bytes via the C-08c `/proc/meminfo` reader in every `RESOURCE_SAMPLE`. See C-08. |
| Disk | IMPLEMENTED | NOT_APPLICABLE | Repository-filesystem free bytes via the C-08c `shutil.disk_usage` reader in every `RESOURCE_SAMPLE`. See C-08. |
| inotify usage | IMPLEMENTED | NOT_APPLICABLE | Per-real-UID instances/watches used plus the governed ceilings, via the C-08c readers, in every `RESOURCE_SAMPLE`. See C-08. |
| Port contention (metric) | IMPLEMENTED | NOT_APPLICABLE | The C-09 allocator (`control/workers.py::allocate_port`) now deterministically assigns builder/fixer ports into worker records and durable job files, and `control/reconcile.py::detect_orphans` observes in-range IPv4+IPv6 listeners — and `RESOURCE_SAMPLE` now records the levels — distinct worker-owned ports and LISTEN sockets inside the governed range via that same IPv4+IPv6 observation. Contention *occurrences* remain represented solely by the existing durable `PORT_ALLOCATION_FAILED` / `PORT_ASSIGNMENT_CONFLICT` / `FOREIGN_OR_ORPHAN_LISTENER` events — no second contention mechanism. See C-08. |
| Process count | IMPLEMENTED | NOT_APPLICABLE | Per-worker PID liveness plus C-09's run-scoped process observation — worker-entry cmdline identity and (agent_pid, agent_start_ticks) verification (`control/proc.py`) — now feed `RESOURCE_SAMPLE`'s deliberately unsummed truthful triple: committed worker records, cmdline-identified live worker-entry processes, and identity-verified surviving agents — never summed into one synthetic number. See C-08. |
| Worktree count | IMPLEMENTED | NOT_APPLICABLE | `control/workers.py::list_worktrees()` exists, and C-09 adds owned/retained worktree dispositions (worker records' `worktree` fields, task `retained_worktrees`, managed-root reverse detection) as underlying facts, and `RESOURCE_SAMPLE` now records the dispositions: registered-managed, ACTIVE, RETAINED and ORPHAN, with managed-root scoping identical to `detect_orphans`; a failed git observation leaves registered/orphan `null` rather than inferring a count. See C-08. |
| Browser count | IMPLEMENTED (C-05.2, 2026-09-30) | NOT_APPLICABLE | `RESOURCE_SAMPLE` now records a real `browser_count` derived from persisted C-05 evidence: `control/gate_evidence.py::scan_sidecars` walks the attempt tree and `control/metrics.py::_browser_levels` counts browsers whose `(pid, start_ticks)` identity `control/proc.py::verified_alive` confirms alive. Browsers are still launched entirely by the Node runner (`apparatus/accessibility/`); the control plane observes them, and never kills or closes one. The sample is **all-or-nothing and fail-closed**: an incomplete evidence walk, any sidecar that is MISSING/UNREADABLE/INVALID, an OPEN sidecar carrying no usable identity, or any identity `verified_alive` cannot settle, each collapses the whole sample to `browser_count: null` with `browser_observed: false` — a verified-live subset is an unknown count with some known members, not a smaller true count. Distinct identities are deduplicated, so duplicate evidence for one process cannot inflate the count. A CLOSED sidecar contributes zero and needs no identity check. A count of `0` is therefore only ever a complete observation, never a fabrication. See C-08. |

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
| Worktree | IMPLEMENTED | IMPLEMENTED (worker records carry the canonical path) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED (ACTIVE ⇄ RETAINED task/evidence ownership) | IMPLEMENTED (managed-root-scoped, bidirectional) |
| Process | IMPLEMENTED | IMPLEMENTED (durable pre-spawn job-file identity; agent (pid, start_ticks) in the status file) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED (both launch modes; identity-verified) | IMPLEMENTED (worker-entry cmdline scan + surviving-agent verification) |
| Browser | RUNNER-LOCAL COMPLETE (one launch per invocation; close on success and thrown error) | IMPLEMENTED (C-05.1 sidecar binds `(pid, start_ticks)` to one attempt, and the attempt to a task + SHA) | IMPLEMENTED (C-05.2 `scan_sidecars` + `RESOURCE_SAMPLE`) | PARTIAL (C-05.2 RUNNING/TERMINAL markers bound the window in which a browser may exist; there is no renewal or expiry, so this is an attempt-scoped bound rather than a lease) | RUNNER-LOCAL COMPLETE (try/finally) | IMPLEMENTED (C-05.2 `detect_orphans`; detection and annunciation only — no kill, close or cleanup) |
| Port | IMPLEMENTED (deterministic allocator, builder + fixer only) | IMPLEMENTED (durable job-file assignment before spawn; PORT env interface) | IMPLEMENTED | IMPLEMENTED | IMPLEMENTED (record removal; job-file reservation until the owner is provably gone) | IMPLEMENTED (IPv4+IPv6 listeners; assignment conflicts) |

Runtime data: NOT_APPLICABLE for every cell above — even the IMPLEMENTED
stages (worktree/process allocate-assign-track-release) have no data to
report before T+00; nothing has been dispatched yet.

Evidence: `control/workers.py` (allocator, managed worktree root, job-file
reservation), `control/proc.py` (start-ticks identity, worker-entry cmdline
scan, IPv4+IPv6 listener reader), `control/supervisor.py` (dispatch
population, lease population/expiry, ACTIVE ⇄ RETAINED transfers, the
HUMAN_REQUIRED reap guard), `control/reconcile.py::detect_orphans`
(reality → state detection with per-class fail-closed scan flags),
`control/watchdog.py::annunciate_orphans` (fenced annunciation) and
`control/cli.py` (governed lease_expired resolution). Reverse detection now
exists alongside the original claim-verification direction. See C-09.

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
