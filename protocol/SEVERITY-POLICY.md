# Severity policy — Run 002 (C-02 proposal)

Status: PROPOSED. This becomes RESOLVED only when the contradiction audit
checks this rule against its tested implementation (the PR evidence
schema/validator work, C-04) — a document alone does not close C-02.

Implementation: control/severity.py (canonical, Python control plane),
ported 1:1 from the original apparatus/severity/severity-floor.js.
Tests: tests/test_severity.py (15/15 pass, including three explicit
RUN001-F1 regression properties — see that module's docstring).

## Source of the floor

product/ACCESSIBILITY.md (imported, byte-for-byte frozen, not edited by
this resolution) states: "Accessibility failures are P1 unless more
severe." Protocol v2 ("Severity — single source of truth") defines P0–P3
generally and states that "deterministic floors may raise a minimum
severity," which Jev can never downgrade below.

protocol/IMPORTED-SPEC-INTERPRETATION.md (status: DRAFT, itself subject to
this audit) already states the distinction this rule formalizes: "An
actual accessibility failure inherits the minimum P1 severity... A
recommendation that does not identify a failed requirement may be P2 or
P3. The reviewer supplies evidence; deterministic policy applies the
floor and decides merge eligibility." This file makes that distinction an
explicit, checkable rule; it does not introduce a new one.

## Rule

1. **Actual accessibility failure** — evidence shows a stated requirement
   in product/ACCESSIBILITY.md's definition of done or cognitive
   accessibility list is not met at the reviewed SHA (for example: an
   interactive control has no visible focus indicator, a form error is
   colour-only, or an axe violation maps to one of those stated
   requirements). Deterministic floor: **minimum P1**. A reviewer may
   still classify a failure as P0 where warranted; the floor only raises
   a classification, it does not cap one.
2. **Non-failure accessibility recommendation** — the evidence
   affirmatively identifies which product/ACCESSIBILITY.md requirements
   were considered and explains why none is unmet. Ordinary P2/P3
   grading applies only when that affirmative explanation is present.
   Omission of the explanation is not evidence of case 2.
3. **Missing or ambiguous classification** — if the Accessibility
   Reviewer's evidence record does not clearly satisfy case 1 (a cited
   unmet requirement) or case 2 (an affirmative not-unmet explanation),
   the accessibility evidence for that finding is **INVALID**. It does
   not default to P2/P3, and it does not default to the P1 floor either
   — invalid evidence is a missing-evidence state, not a severity choice.
   Invalid evidence cannot satisfy the PR contract's independent
   Accessibility box (Protocol v2: independent boxes cannot be satisfied
   by self-attestation, and the Definition of Done requires current
   Accessibility evidence/review to *exist* where relevant). It blocks
   the merge gate until an independent reviewer supplies a clarified,
   valid classification.
4. Ambiguous or high-impact qualitative judgement may escalate to a
   human per Protocol v2 ("Accessibility gate"). That escalation is the
   resolution path out of an INVALID classification, not an alternative
   route around it.
5. Deterministic policy applies the floor, evaluates evidence validity,
   and decides merge eligibility. Jev may classify severity (see
   protocol/JEV-CONTROL-PLANE-V2.md) but never overrides or removes the
   floor, and cannot itself resolve an INVALID classification. Per
   Protocol v2's no-self-attestation invariant, Builder/Fixer
   self-attestation is not evidence for any case.
