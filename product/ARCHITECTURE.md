# Locked Architecture
TypeScript; Next.js + React; Tailwind; Supabase PostgreSQL/Auth; Zod; Recharts; OpenRouter; Jev via OpenRouter Decisions API; Vitest/Testing Library; Playwright/axe; npm; GitHub Actions; Vercel.

No separate Python/FastAPI product backend. Next.js is UI + application backend.
Approved boundaries only: CompanionService, DecisionService, database access, deterministic analytics.
Failure independence: Jev outage→show choices; companion outage→rest works; analytics outage→raw history remains.
Minimal schema target: profiles, check_ins, journal_entries, activities, conversations, messages. Add only when acceptance criteria require. Single-writer migration lock.
No microservices, queues, Redis, GraphQL, custom auth, extra product deployments, or distributed-system machinery.
