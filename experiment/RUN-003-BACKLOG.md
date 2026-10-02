# Run 003 backlog

Things found during Run 002 preparation that are **deliberately not being
fixed before this run**. Run 002 is a bounded experiment; its job is to
generate evidence, and each item below was judged against the launch-blocker
test: does it prevent a startup or core lifecycle operation, risk
uncontrolled spending / exposed credentials / unauthorised actions /
unreviewed merges, or violate an explicit governing requirement?

**Every item here answers NO to all four.** Each records its practical
impact so the retrospective can weigh it against what actually happened.

| # | Item | Practical impact during Run 002 |
|---|---|---|
| 1 | **Worker git isolation by separate clone.** Built and tested (`control/worker_git.py`, 41 tests) but NOT wired; the Supervisor still uses linked worktrees. | A worker can move another worker's local branch ref. It **cannot** cause an unreviewed merge — `evaluate_merge` denies `HEAD_SHA_CHANGED`/`HEAD_SHA_UNVERIFIABLE` against the head GitHub reports. Under the single `run002-wrk` identity, peers reach each other's files regardless, so wiring it would not deliver the property anyway. Wiring touches ten Supervisor call sites plus a Builder→Fixer fetch. |
| 2 | **Nothing in the control plane calls `live-gate.js`.** Two merge gates exist; only `routing.evaluate_merge` is reachable from a tick. | None for operation: the Python gate is the sole merge authority and carries the evidence gate, all six ledger provenance conditions, CI integrity and the CI-path rule. The JS gate's extra checks are proved in the fixture, not in the tick. Joining them is a governance decision about which gate decides merges. |
| 3 | **Per-role `gh` authentication has no call site** (`role=` exists; nothing passes it). | Until a call site passes it, the three-principal separation is a property of the manifests, not the running system. **Required before Stage 2/3**, not before Stage 1. Listed here only so it is not lost. |
| 4 | **Token renewal has no caller** (`gh.ensure_token` built, nothing invokes it). | An installation token lasts one hour; a 24-hour run outlives it. **Required before Stage 3** — listed here so the retrospective sees it was known, not discovered. |
| 5 | **`gh.merge` carries no expected-head.** §5 specifies REST `PUT .../merge` with `sha`. | A head that moves between the gate's verdict and the merge call is caught by `HEAD_SHA_CHANGED` on the next tick rather than refused by GitHub at the point of merge. Narrower window, not an open door. |
| 6 | **`intervention.request` reads `record["dedup_key"]` unguarded.** | A malformed intervention record in `state.json` raises `KeyError` out of the tick — the same permanent-DoS shape C-04c fixed for `outcome`. Reachable only by something that can already write `state.json`, which is C-22. |
| 7 | **The 300-file cap in the changed-path check refuses very large PRs.** | A genuine 300-file product PR needs a human. No Run 002 task is expected to produce one. |
| 8 | **Compare-API wire format is unverified.** No real `gh` call has ever been made from this repository. | Fails closed: an unexpected shape makes the file list *incomplete*, which refuses the merge. Costs a merge, never leaks one. First real exercise is Stage 1. |
| 9 | **`evidence/` artifact lifecycle** (C-05's binary-artifact architecture) and the **Jev secret-leak investigation**. | Both recorded in the audit as investigations, neither started. No operational impact pre-T+24. |
| 10 | **Three worktree-root definitions disagree** (`workers.py`, `isolation.json`, `config.py`). | `git-head.js`'s worktree-kind identity is unusable; the gate uses branch identities, which work. Cosmetic until something tries worktree identity. |
| 11 | **Residual `orphan_annunciations` entry** (attempt 586, OBSERVED, resource gone). | One stale key in `state.json`. Preserved deliberately. Appears in the launch document as test residue. |

## What is NOT on this list

C-22 — worker isolation from the records that decide their own merges —
is **not** deferred. It is an open launch blocker, and Stage 1 is what
closes it.
