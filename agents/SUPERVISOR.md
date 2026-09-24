# Deterministic Supervisor
Python, not an LLM. Own task state/dependencies, provider state, budgets, concurrency, hard guardrails, PR routing, deadline phases, notifications, and ledger. LLMs/Jev advise; supervisor validates/acts.
Watchdog checks liveness and restarts supervisor; failed restart → HUMAN_REQUIRED.
Task states: QUEUED, READY, ASSIGNED, ACTIVE, PR_OPEN, WAITING_CI, WAITING_EVIDENCE, REVIEW, FIX_REQUIRED, MERGE_READY, MERGED, COMPLETE, BLOCKED, STALE, FAILED, WAITING_PROVIDER_RESET, WAITING_DB_LOCK, HUMAN_REQUIRED.
Alive process != progress. Track heartbeat plus meaningful progress. Worker-level phase (Protocol v2 "Worker awareness") is a third, distinct signal: it is only ever what a worker explicitly self-reports, never inferred from role plus a generic running/alive signal.

## Protocol v2.0 deterministic policies

### Review backpressure
0–1 queued review → builder_limit=3  
2 queued reviews → builder_limit=2  
3+ queued reviews → builder_limit=0 for new dispatch

### Merge state machine
PR_OPEN → WAITING_CI (closes the Run 001 CI-dispatch race) and/or WAITING_EVIDENCE (accessibility/security evidence not yet current) → REVIEW → FIX_REQUIRED → REVIEW → MERGE_READY → MERGED → COMPLETE.
`REVIEW_PASS` → reconcile with current main → run required CI → compare reviewed/material diff → if unchanged and clean, move to MERGE_READY, then Supervisor merges. Otherwise return to REVIEW.

### DB migration lock
Track owner task/PR and acquisition/release events. A task blocked only on schema ownership uses `WAITING_DB_LOCK`, while independent work may continue.

### Budget state
Track configured budget, spend, percentage, and threshold events at 50/75/90/100 percent. Never infer missing provider cost as actual cost.

### Runtime recovery
Persist enough state outside ephemeral worker context to reconstruct from Git + ledger. Use workmux resurrection where appropriate. Document that same-host watchdog cannot alert during total host/WSL outage.
