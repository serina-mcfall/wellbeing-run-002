# Run 002 contradiction audit — OPEN

This audit compares the copied Run 001 product/task specifications with
Protocol v2. T+00 is blocked until each item has a recorded resolution,
the resulting files are checked together, and the audit is marked PASS.

| ID | Issue | Required resolution | Status |
| --- | --- | --- | --- |
| C-01 | product/AI.md gives Run 001 control-plane Jev labels and actions; Protocol v2 gives different Run 002 labels and authority. | Keep the copied file unchanged as historical product specification. Define Run 002 control-plane Jev outputs in a separate v2 contract governed by Protocol v2. Keep the six product suggestions unchanged. | OPEN |
| C-02 | product/ACCESSIBILITY.md says accessibility failures are at least P1; Protocol v2 permits P2 findings generally. | Encode the product accessibility minimum of P1 as a deterministic severity floor for actual accessibility failures. Distinguish non-failure suggestions from failures before freezing the rule. | OPEN |
| C-03 | product/MVP.md phrases the PWA clarification as “For Run 001”; Run 002 promises the same MVP. | Explicitly adopt that original PWA scope for Run 002 without editing the imported source. | OPEN |
| C-04 | The draft PR evidence schema permits absent accessibility/security evidence, mismatched SHAs, and missing provenance while Protocol v2 requires enforceable exact-SHA gates. | Replace or extend the enforcement contract and validate it with passing and failing examples. Applicability must be explicit, not inferred from an absent field. | OPEN |
| C-05 | tasks/TASKS.md inherits Run 001 AGENTS.md and role contracts, which were deliberately not imported. | Specify that Run 002 AGENTS.md, v2 roles, and the copied task bodies supply the Run 002 contract; verify acceptance criteria are complete before freeze. | OPEN |

Other launch blockers, including unassigned external project IDs and budget
identity, belong in the launch-gate register. They are not marked resolved
by this document.
