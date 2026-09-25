# Run 002 — Experiment Identity

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` (§"Purpose"),
`config/experiment.json`, `BOOTSTRAP.md`. Those documents are authoritative;
this file is an identity index and must not diverge from them.

## Identity

- experiment_id: `run-002`
- protocol_version: `v2.0`
- timezone: `Pacific/Auckland`
- duration_hours: `24`
- github.repo: **HUMAN_REQUIRED** — not yet assigned. `config/experiment.json`
  currently holds the placeholder `"UNASSIGNED"`. Assigning a real repository
  is a human governance decision, not something derived, defaulted, or
  invented by an agent.

## Relationship to Run 001

Run 002 is a corrected replication of Run 001, not a continuation or rescue
of Run 001 (`protocol/RUN-002-PROTOCOL-v2.0.md`, §"Purpose").

Operational isolation follows `BOOTSTRAP.md`'s literal isolation
requirements.

Selected proven Run 001 control-plane source was imported into Run 002
before T+00 (commit `327f33a`).

The governance interpretation of that import — including its conditions and
the historical import's standing — is defined canonically by **C-06** in
`experiment/CONTRADICTION-AUDIT.md`. This identity document does not
redefine or reproduce that policy; refer to C-06 directly.

The historical 49-file provenance proof for that import remains
`SOURCE_RETRIEVAL_REQUIRED`, as recorded by C-06.

## Status

- T+00: **NOT_STARTED.** No preflight run has passed in full; no product
  dispatch has occurred.
- Open pre-T+00 items: see `experiment/CONTRADICTION-AUDIT.md` for the
  current unresolved-item register.
- This document does not assert that the C-04 live merge-gate composition,
  Accessibility/Security dispatch integration, or the realistic multi-cycle
  preflight are implemented. They are not, and remain open work tracked in
  `experiment/CONTRADICTION-AUDIT.md`.

## Related index documents

- Research questions: `experiment/RESEARCH-QUESTIONS.md`
- Budget: `experiment/BUDGET.md`
- Freeze mechanics: `experiment/FREEZE-PROTOCOL.md`
- Ledger schema: `experiment/LEDGER-SCHEMA.md`
- Contradiction/governance register: `experiment/CONTRADICTION-AUDIT.md`

## What this document is not

This is an identity index, not a status report, capability claim, or policy
document. Any change to `protocol_version`, `duration_hours`, or product
scope is a Protocol v2 change and belongs in
`protocol/RUN-002-PROTOCOL-v2.0.md`, not here. Any change to `github.repo`
or `experiment_id` is a `config/experiment.json` change, reflected here only
after it happens, never anticipated. Any change to the Run 001 import
policy is a C-06 change and belongs in
`experiment/CONTRADICTION-AUDIT.md`, not here.
