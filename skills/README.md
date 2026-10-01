# Required Agent Skills
These are methods, not authority. Contract/spec always wins.

## scoped-implementation
Read task → inspect minimal relevant code → plan minimum change → implement → verify → scope-diff → report → stop. Do not code before understanding task/acceptance criteria.

## codebase-navigation
Map → locate → read only relevant files/imports/tests → modify. Do not repeatedly ingest whole repository. Maintain a lightweight CODEBASE-MAP.md if useful.

## verification
After implementation run applicable typecheck/lint/tests/build/a11y/E2E. Verify mobile/desktop, loading/empty/error. Perform scope diff. Never claim success without evidence.

## accessibility
Implement semantic, keyboard, focus, labels, contrast, reduced motion, touch targets, screen-reader and cognitive accessibility from the start, not as end polish.

## systematic-debugging
Observe → reproduce → isolate → hypothesis → test → root-cause fix → regression test → verify. Do not refactor while debugging. Max 3 bounded recovery attempts.

## handoff
Produce task, status, implemented, files, migrations/interfaces, tests, known issues, out-of-scope items, next expected state. Repository/task files are memory; do not rely on huge persistent contexts.

## jev-integration
Typed predefined choices only; validate response schema; log usage/cost; deterministic fallback; Jev advisory only.

## companion-ai
Minimise private context; enforce wellbeing boundaries; stable service interface; graceful provider failure; no factual analytics calculation.
