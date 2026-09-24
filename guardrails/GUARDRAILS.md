# Guardrails
RED hard-block: secrets in commits/logs/prompts/traces; destructive DB/filesystem; force-push/main bypass; weakening tests/type/lint/a11y/security; unapproved MVP/architecture/privacy/safety changes; Builder/Fixer merge; self-review; destructive migration; protocol modification after T+00.

AMBER approval: new runtime dependency; shared interface change; unapproved schema direction; auth change; external service addition; unexpectedly large blast radius; architecture change.

GREEN: approved scoped implementation/tests/docs/a11y fixes/migrations/formatting/reversible bug fixes.

## Anti-overengineering
Overengineering is a scope violation. Prohibited unless an acceptance criterion cannot reasonably be met otherwise: microservices, extra backend, queues/event buses, Redis, Kubernetes, product containers, GraphQL, custom auth, unnecessary ORM/framework/DI/plugin architecture, speculative caching, premature optimisation, elaborate design system, unnecessary state library.
Prefer simple>clever, explicit>abstract, existing dependency>new, one service>extra service, direct implementation>framework, working MVP>future-proofing.
If task appears to require a new service/layer/runtime dependency, major schema redesign, cross-service communication, or far more files than expected: STOP and emit COMPLEXITY_ESCALATION.
Reviewer explicitly grades OVERENGINEERING PASS/FAIL.

## Anti-cheating
Never delete/disable/exclude failing tests, lower thresholds, suppress errors, loosen strictness, hard-code fake production responses, or edit evaluation rules merely to pass.

## Protocol v1.0 additions

### Merge authority
Only the deterministic Supervisor may execute merges, and only after current Codex approval plus required CI/reconciliation gates.

### Review backpressure
Do not spawn new Builders when 3 or more PRs await independent review.

### Migration single writer
Concurrent unmerged schema migrations are prohibited. Use the Supervisor-owned migration lock.

### Budget
At 100% configured monetary budget, block further paid model calls pending human approval.

### PWA anti-scope-creep
Installability does not authorize offline-first storage, background sync, service-worker caching architecture, or local-first replication.

### Audio provenance
Unverified/copyright-infringing audio assets are prohibited.
