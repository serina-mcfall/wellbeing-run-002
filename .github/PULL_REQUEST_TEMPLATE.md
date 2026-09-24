<!--
Protocol v2 "PR contract": this template is human-readable presentation
only. The machine-readable JSON block below, validated against
protocol/PR-EVIDENCE-V2.schema.json, is the authoritative enforcement
source. Agents cannot satisfy independent boxes (accessibility, security,
review) by self-attestation: every non-CI field must name an independent
producer in its provenance, never Builder or Fixer. A stale JSON block
(wrong head_sha) invalidates every field it contains; a new head SHA
requires the required automated evidence to be regenerated, not carried
over.
-->

## Task

**Task ID:** _fill in, e.g. TASK-004_
**Scope:** _what this PR does_
**Out of scope:** _what it deliberately does not do_

## Acceptance criteria

_copied from tasks/TASKS.md / config/tasks.json for this task, checked off_

- [ ]
- [ ]

## Exact head SHA

_full 40-character lowercase hex_

## CI

- Status: _PASS / FAIL / PENDING_
- Checked against the head SHA above, not a stale SHA

## Accessibility

- Applicable: _yes / no, with reason if no_
- Checks (Protocol v2 "Accessibility gate", all required where applicable):
  responsive 375px, no overflow/clipping/overlap, touch targets, keyboard
  operation, focus order/visible/no traps, labels and text errors, reduced
  motion/no flashing/autoplay, axe scan, screenshots/artifacts, cognitive/
  sensory review.
- Findings: _stable finding IDs, e.g. TASK004-R1-A11Y-001, with severity and status_
- Produced by: **Accessibility Worker/Reviewer** (independent; never Builder/Fixer/Supervisor)

## Security

- Applicable: _yes / no, with reason if no_
- Checks (Protocol v2 "Security protocol" surfaces relevant to this diff):
  secrets, auth/session, authorization, sign-out, redirects/URLs, XSS/unsafe
  HTML, injection/path/command, error leakage, RLS/least-privilege,
  server/client boundaries, dependency risk, AI input/output handling.
- Findings: _stable finding IDs, e.g. TASK004-R1-SEC-001, with severity and status_
- Produced by: **Security Reviewer** (independent; never Builder/Fixer/Supervisor, and
  never the same producer identity as the Codex code review below)

## Codex review

- Verdict: _REVIEW_PASS / REVIEW_FAIL_
- Findings: _stable finding IDs, e.g. TASK004-R1-CODE-001, with P0/P1/P2/P3 and status_

## Repair history

_each Fixer cycle: finding IDs routed, cycle number, outcome_

## Remaining debt

_P2/P3 findings that merge with status ACCEPTED_NONBLOCKING; non-blocking does not mean forgotten_

## Deterministic merge gate

Per canonical severity semantics: **P0 and P1 findings always block merge.
P2 and P3 are recorded/tracked debt and do not independently block merge.**
A deterministic floor may raise a finding's minimum severity; it can never
lower one below its floor.

Merge eligibility is **computed by the control plane**, not asserted here.
If this section claims an eligibility that disagrees with the computed
value, the computed value wins and the disagreement itself is recorded.

## Machine-readable evidence

Validated against `protocol/PR-EVIDENCE-V2.schema.json`.

```json
{
  "task_id": "",
  "head_sha": "",
  "ci": {
    "sha": "",
    "status": "PENDING",
    "source": "",
    "provenance": {}
  },
  "review": {
    "sha": "",
    "verdict": "PENDING",
    "findings": [],
    "provenance": {}
  },
  "accessibility": {
    "applicable": false,
    "not_applicable_reason": "",
    "provenance": {}
  },
  "security": {
    "applicable": false,
    "not_applicable_reason": "",
    "provenance": {}
  }
}
```
