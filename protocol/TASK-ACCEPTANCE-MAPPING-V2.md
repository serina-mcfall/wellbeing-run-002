# Run 002 task-acceptance mapping (C-05 evidence)

Status: PROPOSED, as part of C-05. This records product-level requirements
that inherit into specific imported tasks — tasks/TASKS.md and
config/tasks.json are imported, committed source copies; freeze pending
T+00 — without editing either file. This is the same mechanism
tasks/TASKS.md already uses for product/ACCESSIBILITY.md and
product/DESIGN-SYSTEM.md ("Every task includes mobile/accessibility/
error/empty/loading checks where applicable"), extended here to two
requirements a per-task cross-check found were not restated in every task
body they apply to.

Per Protocol v2's "no policy defined twice" invariant, a requirement
stated once at product level governs every task it applies to whether or
not that task's body repeats it. Neither entry below is a new rule or a
human decision — each cites a requirement already stated in an imported
source and applies it where it was already due.

## TASK-004 — soundscape ownership/licence/attribution

**Requirement (quoted exactly):** "The four MVP soundscapes must use
owned, public-domain, or appropriately licensed assets, with
attribution/license recorded where required." — product/MVP.md,
"Soundscape provenance".

**Applies to:** TASK-004 (Support).

**Basis:** config/tasks.json's TASK-004 body states this requirement
verbatim, and product/MVP.md's "Soundscape provenance" section states it
independently at product level. tasks/TASKS.md and config/tasks.json are
complementary acceptance sources — a human-readable narrative form and a
machine-readable form of the same task graph — and neither is
authoritative over the other; where one states a requirement the other
does not repeat, the union of both, read together with the product specs,
governs. tasks/TASKS.md's shorter TASK-004 wording does not remove a
requirement stated in product/MVP.md and in config/tasks.json. Both
imported source copies remain unedited; freeze is pending T+00.

**Status:** INHERITED — applies now.

## TASK-004 and TASK-005 — Row Level Security

**Requirement:** "Database access defaults closed; use RLS and justified
policies." — product/PRIVACY.md, applying MVP-wide to any table holding
mood/journal/companion/wellbeing data.

**Applies to:** TASK-004 (any support/activity-usage persistence) and
TASK-005 (conversations/messages persistence), both marked
`schema_changing: true` in config/tasks.json, and both named in
product/ARCHITECTURE.md's minimal schema target (`activities`,
`conversations`, `messages`).

**Basis:** product/PRIVACY.md states this rule once, for the whole MVP.
TASK-002 and TASK-003 restate it locally ("Minimal migration and RLS
required"; "persistence and RLS"); that restatement is a convenience, not
evidence that TASK-004/TASK-005 are exempt — every table holding user data
is covered by the same MVP-wide rule regardless of which task body
repeats it. Both imported source copies remain unedited; freeze is
pending T+00.

**Status:** INHERITED — applies now.

## Standing for C-05

This mapping is part of C-05's evidence, not a substitute for it. C-05
remains PROPOSED until: this mapping is cross-checked against the full
nine-task graph for any further cross-cutting inheritance gaps beyond
these two, and the remaining Run 002 role contracts (Supervisor, Watchdog,
Builder, Fixer, Codex Reviewer, Accessibility Worker/Reviewer, Security
Reviewer, Grok Observer, Human) required by
protocol/IMPORTED-SPEC-INTERPRETATION.md's "Task inheritance" section are
written.
