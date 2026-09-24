# Run 002 --- Protocol v2.0

## Purpose

Run 002 is a corrected replication of Run 001: same
neurodivergent-friendly wellbeing MVP, same 24 wall-clock hours, same
TypeScript/Next.js/React/Tailwind/Supabase product direction, same
calming low-stimulation dyslexia/AuDHD-conscious design requirement, and
the same hard rule that **overengineering is a scope violation**.

Run 002 is not a continuation or rescue of Run 001. It starts from the
original product challenge and may incorporate only changes justified by
Run 001 evidence.

## Research questions

1.  Can the corrected organisation deliver the same MVP in 24 hours?
2.  Can exact-SHA automated accessibility evidence remove the
    human-speed verification bottleneck?
3.  Can independent code, accessibility and security review work without
    self-attestation?
4.  Can Jev handle fast triage while deterministic policy retains
    authority?
5.  How often are Sonnet/standard models sufficient versus
    evidence-backed escalation?
6.  Can durable state survive provider cooldown and context loss?
7.  Can Supervisor/Watchdog know what every worker is doing at any time?
8.  What resource/human effects occur while Run 001 and Run 002 run
    concurrently?

## Canonical invariants

1.  No silent failure.
2.  No self-attestation as independent evidence.
3.  No stale evidence.
4.  No policy defined twice when one canonical source can be referenced.
5.  No complexity without Run 001 evidence.
6.  Workers report facts/evidence; they never decide their own
    acceptance.
7.  The 24-hour clock never pauses.

## Policy precedence

1.  Security/privacy/experiment-integrity guardrails.
2.  Canonical Protocol v2 policy.
3.  Role contracts.
4.  Task acceptance criteria.
5.  PR/evidence presentation.
6.  Prompts.

Unresolved contradiction =\> HUMAN_REQUIRED. Lower layers may not
redefine severity, merge authority, budgets, evidence validity or
dependencies.

## Severity --- single source of truth

-   **P0 Critical:** immediate escalation; always blocks merge.
-   **P1 High:** must repair; always blocks merge.
-   **P2 Moderate:** recorded/tracked; does **not independently block
    merge**.
-   **P3 Low/hardening:** recorded/tracked; non-blocking.

Severity semantics are immutable after T+00. Deterministic floors may
raise a minimum severity. Jev can never downgrade below a floor.

## Definition of Done

A task becomes COMPLETE only when frozen scope is satisfied; CI is green
on the current exact SHA; zero unresolved P0/P1; current Codex review
exists; current Accessibility evidence/review exists where relevant;
current Security evidence/review exists where relevant; branch is
reconciled with current main; evidence is regenerated after material SHA
change; no RED guardrail exists; deterministic merge gate passes; PR is
actually merged.

## Agent organisation

-   **Supervisor:** deterministic authority for routing, state, evidence
    readiness, budget and merge.
-   **Watchdog:** independent reconciliation/recovery; never blindly
    trusts Supervisor state.
-   **Builder:** fresh context, implements one task.
-   **Fixer:** fresh context, fixes only routed stable finding IDs.
-   **Codex Reviewer:** independent read-only engineering review.
-   **Accessibility Worker/Reviewer:** real browser, automated exact-SHA
    evidence plus independent qualitative accessibility review.
-   **Security Reviewer:** independent read-only security review.
-   **Grok Observer:** bounded read-only observer; no authority.
-   **Jev:** fast structured classification/recommendation; no direct
    authority.
-   **Human:** governance/privilege/exception boundary, not routine
    router or product coder.

## Model assignment

### Claude

Default: Sonnet-class installed model for Builders, Fixers, routine
debugging/tests/docs/implementation.

Escalate one tier only with recorded evidence: repeated blocking
failure, genuine cross-system complexity, security-sensitive ambiguity,
or a task frozen as HIGH_REASONING.

Highest Claude tier (user shorthand "Fable"; resolve exact installed
model identifier before freeze) is exceptional:
emergency/release-critical blocker, explicitly highest-reasoning task,
or demonstrated lower-tier failure.

Context pressure is not escalation: persist durable state and start a
fresh same-tier context.

### Codex

Use standard capable review tier by default. Escalate only for
evidence-backed complexity/ambiguity. File count alone is not
complexity.

### Grok

Use one fixed bounded Observer model for Run 002. No escalation ladder
without evidence.

### Jev

Jev handles fast structured: - finding severity P0/P1/P2/P3; - worker
health HEALTHY/SLOW/STALLED/WAITING; - incident classification; - queue
priority among already-legal tasks; - model routing
STAY_TIER/ESCALATE_ONE_TIER/HUMAN_REQUIRED.

**Jev classifies/recommends; deterministic policy acts.** Jev cannot
merge, change task state directly, bypass dependencies, waive findings,
change policy, expose secrets or grant extra attempts.

Every Jev decision logs input category, output, confidence, latency,
cost, deterministic override and eventual outcome.

## Worker awareness

Supervisor must know at all times: worker_id, role, task, PR, SHA,
phase, PID, worktree, branch, tmux/browser/port resources, started_at,
last process activity, last meaningful progress, waiting_on, lease
expiry and attempt.

Frozen phases: STARTING, READING, PLANNING, IMPLEMENTING, TESTING,
COMMITTING, PUSHING, WAITING_CI, REVIEWING, FIXING,
ACCESSIBILITY_TESTING, SECURITY_REVIEWING, COMPLETED, FAILED.

Track separately: - **process activity:** heartbeat/stream/output; -
**meaningful progress:** file/test/commit/PR/finding/browser/review
evidence.

Watchdog independently reconciles Supervisor claims with observable
reality. Dangerous mismatch freezes the affected task and emits
STATE_INVARIANT_VIOLATION.

## State machine

QUEUED → READY → ASSIGNED → ACTIVE → PR_OPEN →
WAITING_CI/WAITING_EVIDENCE → REVIEW → FIX_REQUIRED → REVIEW →
MERGE_READY → MERGED → COMPLETE.

Exception states: HUMAN_REQUIRED, BLOCKED, FAILED, FROZEN. Every BLOCKED
state carries a structured reason.

## Operational rules

-   one Supervisor and one Watchdog;
-   every dispatch returns a resource or explicit reason;
-   consequential operations have idempotency keys;
-   active resources are never destroyed/mutated underneath workers;
-   orphan processes/worktrees/browsers/ports are detected;
-   deterministic port allocator;
-   CI/evidence readiness before final review;
-   deterministic merge ordering;
-   new SHA regenerates required automated evidence;
-   provider states AVAILABLE/THROTTLED/COOLDOWN/UNAVAILABLE;
-   cooldown queues work rather than failing tasks;
-   durable state must survive context loss.

## Accessibility gate

For each relevant exact PR SHA: - 375px responsive validation; -
horizontal overflow/clipping/overlap; - touch targets; - keyboard
Tab/Shift+Tab/Enter/Space; - logical focus order, visible focus and no
traps; - labels and text-based errors; - reduced motion/no
flashing/pulsing/autoplay; - axe scan; - screenshots/artifacts; -
cognitive/sensory review: calmness, low density, readable hierarchy, one
obvious primary action per area.

Evidence regenerates for every relevant new SHA. Ambiguous/high-impact
qualitative judgement may escalate to a human.

## Security protocol

Protect authentication/session integrity, cross-user isolation,
mood/journal/private wellbeing data, credentials/secrets, database
access and AI companion boundaries.

Review relevant surfaces for secrets, auth/session, authorization,
failed sign-out, redirects/malformed URLs, XSS/unsafe HTML,
injection/path/command risks where relevant, error leakage, Supabase
RLS/least privilege, server/client boundaries, dependency risk and
unsafe AI input/output handling.

No secrets in prompts, logs, Discord, Langfuse, screenshots, PR text or
client code. Security controls remain proportionate to the MVP; security
overengineering is still overengineering.

## AI companion boundary

Wellbeing support only; not professional medical/mental-health diagnosis
or treatment. User content is untrusted input and never becomes system
instruction. Model output is untrusted content and not executable/unsafe
HTML by default.

## Evidence provenance

Every evidence item records source, SHA, created_at, kind, producer,
scope, result and artifact reference.

Builder/Fixer assertions are not independent evidence.

Simple v2 rule: new SHA =\> regenerate required automated evidence. Do
not build change-impact inheritance yet.

## PR contract

A frozen PR template must contain: task/scope/out-of-scope; acceptance
criteria; exact head SHA; CI; Accessibility; Security; Codex verdict and
P0/P1/P2/P3; repair history; remaining debt; deterministic merge gate.

Agents cannot satisfy independent boxes by self-attestation.

A machine-readable PR evidence schema is authoritative for enforcement;
Markdown is human-readable presentation. CI validates the schema.

## Stable finding IDs

Use globally stable IDs such as TASK003-R2-SEC-001. Finding lifecycle:
OPEN, FIXED, VERIFIED, SUPERSEDED, ACCEPTED_NONBLOCKING.

P2/P3 that merge enter a debt register; non-blocking does not mean
forgotten.

## Regression-test integrity

Where practical, a bug regression test must fail against the defective
behaviour and pass after the repair.

## Task graph

Copy the same product tasks/acceptance criteria from Run 001 before
freeze, then validate actual technical dependencies: - no dependency
added merely to serialize; - no dependency removed merely to avoid
blocking; - no cycles/impossible dependencies; - failure blocks only the
true dependent subtree; - independent tasks continue when safe; -
per-task builder/review/repair/wall-time limits frozen before T+00.

## Human intervention taxonomy

HUMAN_PRIVILEGED_ACTION; HUMAN_APPARATUS_AUTHORISATION;
HUMAN_VERIFICATION; HUMAN_GOVERNANCE_DECISION; HUMAN_PRODUCT_DECISION;
HUMAN_PRODUCT_IMPLEMENTATION.

Record requested_at, acknowledged_at, resolved_at and active human
minutes.

## Discord

Ledger append succeeds before Discord notification.

INFO (no acknowledgement): PR_OPENED, recoverable REVIEW_FAIL,
FIXER_DISPATCHED, REVIEW_PASS, ACCESSIBILITY result, SECURITY result,
PR_MERGED, TASK_COMPLETE, scheduled checkpoints.

ATTENTION: autonomous recovery, provider cooldown, unusual recoverable
condition.

HUMAN REQUIRED: exact authority/action needed and type.

CRITICAL: privacy/security/experiment-integrity emergency.

Autonomous merge notifications include task, PR, reviewed SHA, merged
SHA and "No human action required."

## Budget

Independent Run 002 OpenRouter/Jev hard ceiling: **USD \$25**. -
\$12.50: 50% attention if early. - \$18.75: 75% restrict expensive
metered escalation. - \$22.50: 90% release-critical/product-essential
metered calls only. - \$25.00: hard stop on further Run 002 metered
spend without human approval.

Claude/Codex/Grok subscription usage is measured separately.

## Concurrency metrics

Track provider quota/cooldowns, model usage, CPU/RAM/disk,
process/worktree/browser counts, inotify usage, port contention,
simultaneous HUMAN_REQUIRED events, human minutes, Jev spend and metered
API spend.

Run 002 may not modify Run 001. Shared provider quota contention is
allowed and measured.

## Anti-cheating

Prohibited: - weakening gates after seeing results; - meaningless
commits; - changing tests merely because implementation fails; -
stale/wrong-SHA evidence; - dependency changes to bypass failure; -
Reviewer fixing own findings; - Builder/Fixer self-attestation; - secret
leakage; - unapproved apparatus changes; - model escalation without
evidence; - speculative architecture.

Repeated failure of the same invariant after an approved repair triggers
an experiment-integrity HUMAN_REQUIRED checkpoint rather than endless
apparatus evolution.

## Preflight --- realistic, not synthetic

T+00 is forbidden until all pass: - contradiction audit proves all
frozen documents can be simultaneously satisfied; - immutable
manifest/hashes; - Run 001 isolation; - host
CPU/RAM/disk/fd/inotify/port headroom; - role capability matrix live; -
one Supervisor/Watchdog; - worker awareness and independent
reconciliation; - deliberate stalled worker detected, healthy worker
preserved; - deliberate dead Supervisor recovered; - long Observer does
not create false death; - realistic Builder→PR→Review
FAIL→Fix→CI→Accessibility/Security→fresh re-review→merge; - at least two
review cycles; - exact-SHA evidence invalidation/regeneration; - P2
demonstrably non-blocking; - fail-before/pass-after regression proof; -
fresh context reconstructs solely from durable state; - Discord
lifecycle delivery; - Jev classification plus deterministic
floor/override; - independent \$25 budget; - no partial preflight can
authorize start.

## Run 001 → Run 002 audit mapping

Run 002 corrections are justified by: Supervisor concurrency/lock
failure; buffered progress; worktree collisions; silent dispatch;
missing reviewer evidence; human accessibility evidence invalidation; P2
prompt contradiction; Watchdog/Observer heartbeat seam; unrealistic
preflight; missing seam tests; PID-as-health; reused finding IDs; sparse
Discord progress; ambiguous human counts; context risk; host inotify
exhaustion; over-gating task DAG; CI race; regression-test quality;
policy drift; underused Jev; finite model quotas; missing PR contract;
and late security discovery.

## Freeze

At T+00 freeze protocol, precedence, MVP, task graph, acceptance
criteria, severity, PR contract/schema, roles, model policy, evidence
rules, budgets and merge gate.

Post-T+00 apparatus changes require explicit human authority,
append-only deviation record, narrow scope, regression evidence,
protected PR/CI landing and unchanged T+24.

At T+24: stop new dispatch, freeze state, flush ledger, snapshot
evidence/cost/provider state, emit EXPERIMENT_FROZEN, send final Discord
checkpoint and generate the final report without rewriting history.
