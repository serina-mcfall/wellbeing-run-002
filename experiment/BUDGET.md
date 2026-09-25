# Run 002 — Budget

Canonical source: `protocol/RUN-002-PROTOCOL-v2.0.md`, §"Budget".
Implemented by: `control/budget.py` (`THRESHOLDS`, `METERED_PROVIDERS`).
That protocol section is authoritative; this file restates it for the
`experiment/` index and must not diverge from it.

Governance record: this budget — amount and thresholds — was reviewed and
approved unchanged by the human operator on 2026-09-25, as the Run 002
independent metered ceiling. No amount or threshold was altered by that
approval or by this document.

## Scope

Independent Run 002 OpenRouter/Jev hard ceiling: **USD $25.00**.

Scope: Run 002 metered OpenRouter/Jev spend governed by Protocol v2 and
`control/budget.py`. Claude, Codex, and Grok subscription usage is measured
separately and is never counted against this ceiling or inferred as a
dollar cost.

## Thresholds

| USD spent | % of ceiling | Effect |
| --- | --- | --- |
| $12.50 | 50% | ATTENTION — flagged if crossed early |
| $18.75 | 75% | RESTRICT_ESCALATION — restrict expensive metered escalation |
| $22.50 | 90% | RELEASE_CRITICAL_ONLY — release-critical/product-essential metered calls only |
| $25.00 | 100% | HARD_STOP — hard stop on further Run 002 metered spend without human approval |

This file is not a place to change the amount, add thresholds, or record
actual spend. Actual spend and threshold-crossing events belong in the
ledger (`experiment/LEDGER-SCHEMA.md`) and runtime state, not here. Any
change to the amount or thresholds themselves is a new governance decision,
not an edit to this document.
