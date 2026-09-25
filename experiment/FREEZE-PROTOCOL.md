# Run 002 — Freeze Protocol

Canonical source: `protocol/RUN-002-PROTOCOL-v2.0.md`, §"Freeze".
That document is authoritative; this file restates it for the `experiment/`
index and must not diverge from it.

Status: NOT_YET_FROZEN. T+00 has not occurred. Nothing in this document
describes a completed event.

## At T+00

Protocol, precedence, MVP, task graph, acceptance criteria, severity, PR
contract/schema, roles, model policy, evidence rules, budgets and merge
gate all freeze simultaneously.

## Post-T+00 apparatus changes

Any apparatus change after T+00 requires all of:
- explicit human authority;
- an append-only deviation record;
- narrow scope;
- regression evidence;
- protected PR/CI landing;
- unchanged T+24.

## At T+24

- stop new dispatch;
- freeze state;
- flush ledger;
- snapshot evidence/cost/provider state;
- emit `EXPERIMENT_FROZEN`;
- send final Discord checkpoint;
- generate the final report without rewriting history.

This file is not a place to record that freeze has happened, to add new
freeze conditions, or to interpret them. Any change to the freeze rules
themselves is a Protocol v2 change and belongs in
`protocol/RUN-002-PROTOCOL-v2.0.md`, not here. The actual T+00/T+24 events,
when they occur, belong in the ledger and in `experiment/TIMELINE.md`, not
in this procedural document.
