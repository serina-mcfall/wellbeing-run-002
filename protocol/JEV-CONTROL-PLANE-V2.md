# Jev control-plane contract — Run 002 (C-01 proposal)

Status: PROPOSED. This becomes RESOLVED only when the contradiction audit
checks this finished contract against its actual implementation — a
document alone does not close C-01.

This file governs control-plane classification only. It does not alter,
supersede, or duplicate the product Jev contract.

## Relationship to product/AI.md

product/AI.md is an imported, byte-for-byte frozen source file and is not
edited by this resolution.

- product/AI.md line 3 states the **product** Jev contract: `Jev via
  OpenRouter Decisions API behind DecisionService. Product choices:
  TALK/JOURNAL/MEDITATE/MOVE/SOUNDSCAPE/NOTHING.` It uses DecisionService,
  offers only those six approved suggestions, is schema-validated, and
  has a deterministic fallback. It remains unchanged and is implemented
  under TASK-006.
- product/AI.md line 5 states Run 001's control-plane vocabulary:
  `Control-plane Jev may advise task health HEALTHY/STRUGGLING/STALE/
  BLOCKED, event significance ROUTINE/NOTABLE/IMPORTANT/
  POTENTIAL_ESCALATION, next attention CONTINUE/RECHECK_SOON/REVIEW/
  FIXER/HUMAN_REVIEW.` This is historical documentation of Run 001 only.
  Protocol v2 defines a different Run 002 vocabulary (below). Run 002
  code must not implement the Run 001 labels.

Product and control-plane Jev calls use separate schemas and separate call
sites. A control-plane classification never feeds product suggestion
selection, and a product suggestion never feeds control-plane state.

## Run 002 control-plane Jev outputs

Per protocol/RUN-002-PROTOCOL-v2.0.md ("Jev"), Jev handles fast structured
classification/recommendation only. It has no direct authority: it cannot
merge, change task state, bypass dependencies, waive findings, change
policy, expose secrets, or grant extra attempts. Deterministic policy acts
on Jev's output; Jev's own output is never self-executing.

| Output | Enumeration | Deterministic policy behaviour |
| --- | --- | --- |
| Finding severity | P0, P1, P2, P3 | Deterministic floors (e.g. accessibility; see protocol/SEVERITY-POLICY.md) may raise Jev's classification. Jev can never downgrade below a floor. |
| Worker health | HEALTHY, SLOW, STALLED, WAITING | Watchdog independently reconciles against observable process/progress evidence; a dangerous mismatch freezes the task and emits STATE_INVARIANT_VIOLATION regardless of Jev's classification. |
| Incident classification | UNSPECIFIED — open pre-freeze decision, see below. | UNSPECIFIED — blocked on the decision below. |
| Queue priority | Ordering among tasks already legal to dispatch (dependencies satisfied, not blocked) | Jev cannot reorder past a dependency or an unresolved block; it only ranks within the legal set. |
| Model routing | STAY_TIER, ESCALATE_ONE_TIER, HUMAN_REQUIRED | Escalation requires recorded evidence per protocol/RUN-002-PROTOCOL-v2.0.md ("Model assignment"); Jev's recommendation is not itself the evidence. |

## Open pre-freeze decision: incident classification

Protocol v2 ("Jev") names "incident classification" as one of Jev's
outputs but defines no enumeration and no deterministic handling for it.
This proposal does not invent one. Protocol v2's Discord tiers (INFO/
ATTENTION/HUMAN REQUIRED/CRITICAL) are driven by named event types
(PR_OPENED, REVIEW_FAIL, etc.), not by an incident-classification value,
so no reuse of that mapping is assumed either.

**Gate: RED.** This output cannot be implemented or exercised until a
bounded output schema (a fixed enumeration, not free text) and its
deterministic handling (which policy layer consumes it and what that
layer does with each value) are defined and covered by passing and
failing tests. Tracked in experiment/CONTRADICTION-AUDIT.md.

## Decision-log fields (required, per Protocol v2 "Jev")

Every Jev decision must log all seven fields — none optional, none
inferred from absence:

1. input category
2. output
3. confidence
4. latency
5. cost
6. deterministic override (the value actually applied after floors/policy, if different from Jev's raw output)
7. eventual outcome
