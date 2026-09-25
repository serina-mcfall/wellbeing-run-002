# Run 002 — Ledger Schema

Canonical source: `protocol/RUN-002-PROTOCOL-v2.0.md`, §"Discord" (ledger
append succeeds before Discord notification) and §"Evidence provenance".
Implemented by: `control/ledger.py` (event schema, append mechanics).

This document covers the **ledger event schema only**. Per-review evidence
provenance is a separate, distinct concept with its contract defined by
`protocol/PR-EVIDENCE-V2.schema.json` and offline validation in
`apparatus/pr-evidence/validate.js`; its live producer/composition path is
not yet implemented. It is described separately below — the two are not the
same thing and must not be conflated.

## What the ledger is

An append-only JSONL file. Events are appended under an exclusive file
lock, flushed, and fsynced; lines are never rewritten. This is the
mechanism as implemented in `control/ledger.py`, not a policy choice made by
this document.

## Base fields, present on every event

- `timestamp` — ISO 8601, millisecond precision, in the experiment timezone
- `experiment_id`
- `event_type`

## Named optional fields

Present when supplied by the caller; a field not supplied is simply absent,
not null:

`activity_class, agent_id, role, provider, model, task_id, pr_id, branch,
state_before, state_after, input_tokens, cached_tokens, output_tokens,
actual_cost_usd, estimated_cost_usd, cost_source, duration_ms, outcome,
guardrail, human_intervention, metadata_redacted`

`activity_class`, when present, is one of: `BUILD, REVIEW, FIX, DEBUG, TEST,
ORCHESTRATION, OBSERVATION, DOCUMENTATION, ESCALATION, FAILED_WORK`.

## Overflow

Any keyword argument passed to `Ledger.append()` that is not one of the
named fields above is folded into `metadata_redacted` rather than dropped or
rejected.

## Secret handling — fail closed, not fail open

Every event is scrubbed before being written. If a scrubbed line still
contains something that looks like a secret, the real event is **not**
written; a substitute `GUARDRAIL_RED` / `SECRET_IN_LEDGER_EVENT_BLOCKED`
event is written in its place, naming only the blocked event's type. The
ledger never silently drops an event and never writes an unscrubbed one.

## Ordering guarantee this schema exists to satisfy

Protocol v2, §"Discord": ledger append must succeed before the corresponding
Discord notification is sent. This document records that requirement; it
does not itself verify that every call site honours it — that is a
call-site-by-call-site property of `control/notify.py` and its callers, not
a property of the schema, and is not demonstrated here.

## Per-review evidence provenance — a separate concept

Protocol v2, §"Evidence provenance", requires every evidence item to record
`source, SHA, created_at, kind, producer, scope, result, artifact reference`.
This is **not** implemented by the ledger. It is implemented, field-for-field
and under those exact names, by `protocol/PR-EVIDENCE-V2.schema.json`'s
`$defs.provenance` JSON Schema definition (validated offline by
`apparatus/pr-evidence/validate.js`).

A separate Python module, `control/evidence.py::EvidenceItem`, exists for a
different purpose — gathering CI/human-verification facts to hand to the
Codex reviewer — and has its own fields: `source, kind, head, applies,
detail`. It overlaps with `$defs.provenance` only in field name, on `source`
and `kind`. Nothing in either module's code defines `head` as equivalent to
`sha`, or `applies`/`detail` as equivalent to `result`/`artifact_reference`;
no such mapping is assumed or claimed here.

No code currently assembles a live `$defs.provenance`-shaped object from
`EvidenceItem`, ledger data, or any other live source and validates it
against `PR-EVIDENCE-V2.schema.json`. This appears to be the same gap
already tracked as C-04 in `experiment/CONTRADICTION-AUDIT.md` ("no
composition layer computing a real live-gate decision exists yet") rather
than a new contradiction; it is recorded here as a cross-check finding, not
resolved, and not duplicated as a new audit row without further review.

This file is not a place to add new event fields, change scrubbing
behaviour, or resolve the `EvidenceItem`/`provenance` gap. Any change to the
ledger's actual schema is a code change to `control/ledger.py`, reflected
here only after it happens. Any resolution of the provenance-composition
gap belongs in `experiment/CONTRADICTION-AUDIT.md`.
