# Jev control-plane contract — Run 002 (C-01, RESOLVED)

Status: RESOLVED. Checked against its implementation (control/jev.py,
tests/test_jev.py, 10/10 pass) by experiment/CONTRADICTION-AUDIT.md.

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
| Incident classification | NONE, WORKER, PROVIDER, CI, EVIDENCE, SECURITY, PRIVACY, BUDGET, STATE_INVARIANT, APPARATUS, CREDENTIAL — frozen, see below. | Advisory only. Deterministic policy decides what action, if any, the classification triggers; Jev's output is never self-executing. |
| Queue priority | Ordering among tasks already legal to dispatch (dependencies satisfied, not blocked) | Jev cannot reorder past a dependency or an unresolved block; it only ranks within the legal set. |
| Model routing | STAY_TIER, ESCALATE_ONE_TIER, HUMAN_REQUIRED | Escalation requires recorded evidence per protocol/RUN-002-PROTOCOL-v2.0.md ("Model assignment"); Jev's recommendation is not itself the evidence. |

## Frozen: incident classification (human governance decision)

Protocol v2 ("Jev") names "incident classification" as one of Jev's
outputs. The enumeration below is now frozen — no longer Gate RED —
by explicit human decision, not invented by this contract.

**Enumeration:** NONE, WORKER, PROVIDER, CI, EVIDENCE, SECURITY, PRIVACY,
BUDGET, STATE_INVARIANT, APPARATUS, CREDENTIAL.

**Semantics.** Classification answers *what class* of incident
occurred. It is independent of, and never derived from or substituted
for:
- severity (how serious it is);
- worker health (worker-state assessment);
- queue priority (scheduling advice);
- model routing (model recommendation);
- deterministic action (what the Supervisor actually does).

Each of these five outputs is its own separately-invoked Decision;
none is computed from another.

**No subtype taxonomy.** Where additional specificity is genuinely
needed, use a bounded reason_code/evidence field, not another
top-level incident class. No such field's concrete values are defined
by this contract yet, since none were specified with this decision;
inventing bounded values without a stated need would repeat the
mistake this gate exists to prevent. The existing free-text `reason`
field DecisionService already returns is where that explanatory detail
lives until a bounded field is specifically required and defined.

**NONE is not an "unknown" bucket.** NONE means the evidence
affirmatively indicates no incident. It is not a catch-all for
ambiguous or unreachable classification.

**Uncertainty representation.** DecisionService's existing fallback
mechanism always returns one of the frozen enum values, and it already
tags every Decision with `source` ("jev" or "fallback"). That existing
distinction — not a new field or class — is the uncertainty signal:
`NONE` with `source == "fallback"` means Jev could not be reached or
returned an unparseable/off-schema answer, not that evidence confirmed
no incident. Only `NONE` with `source == "jev"` is a genuine
classification. A caller must check `source` before treating a `NONE`
choice as confirmed.

**Advisory only, unconditionally.** Jev must never directly restart
workers, freeze or unfreeze tasks, merge PRs, alter budgets, rotate
credentials, change severity policy, or contact a human. Deterministic
policy acts; Jev classifies.

Tracked as closed in experiment/CONTRADICTION-AUDIT.md (C-01).

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
