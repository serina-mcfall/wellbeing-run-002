# Task Graph
All tasks inherit AGENTS.md and role contracts. Every task includes mobile/accessibility/error/empty/loading checks where applicable. No adjacent features.

## TASK-001 Foundation
Next.js TypeScript app; Tailwind; Supabase wiring; minimal auth; PWA shell; navigation; CI/test scripts; Vercel-ready. No product features beyond shell.
Dependencies: none.

## TASK-002 Check-in
Mood, energy, sensory load, social battery, optional note; persistence/history. Minimal migration/RLS required.
Depends: 001.

## TASK-003 Journal
Plain-text create/edit/delete/list; timestamps; confirmation; persistence/RLS. No AI/tags/search/rich text/photos.
Depends: 001.

## TASK-004 Support
Static meditation/movement options; user-started four soundscapes; simple timers. No mixer/streaming/CMS.
Depends: 001.

## TASK-005 Companion
CompanionService/OpenRouter, text UI, persistence if required, safe prompt/boundaries, graceful outage. No RAG/voice/diagnosis.
Depends: 001.

## TASK-006 Jev Product Integration
DecisionService using OpenRouter Decisions API; approved six choices; schema validation; deterministic fallback. No diagnosis/high-stakes decisions.
Depends: 002,004.

## TASK-007 Insights
Deterministic SQL/TS counts, mood history/distribution, activity/support usage, simple Recharts visualisation + accessible summaries. No AI-calculated facts/correlation engine.
Depends: 002,003,004.

## TASK-008 Integration & Demo Journey
Connect Home/check-in/support/companion/journal/insights into coherent low-stimulation flow; clearly labelled synthetic demo data only if needed.
Depends: 002–007.

## TASK-009 Release Stabilisation
E2E core journey; a11y; mobile 375px; desktop; failure modes; deployment; docs. No new features.
Depends: 008.
