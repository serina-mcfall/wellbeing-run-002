# Security threat model — Run 002 (B2b)

Status: FROZEN Wave 2 — assets, trust boundaries and invariants only. No
deterministic security severity floor exists (see below); this is a
recorded governance outcome, not an unfinished implementation.

This is Run 002's actual MVP surfaces only, sourced directly from
`product/ARCHITECTURE.md`, `product/PRIVACY.md`, `product/MVP.md` and
`config/tasks.json` — not a generic security framework. "Security
controls remain proportionate to the MVP; security overengineering is
still overengineering" (`agents/SECURITY.md`).

## Assets and surfaces actually present

- **Session/auth**: minimal Supabase Auth only. No custom auth, no OAuth
  providers (`product/MVP.md` "Out"). `AUTH_SESSION`/`SIGN_OUT` review how
  the app wires Supabase's own session mechanics, not a bespoke auth
  implementation.
- **Sensitive data**: `profiles`, `check_ins`, `journal_entries`,
  `activities`, `conversations`, `messages` (`product/ARCHITECTURE.md`
  "Minimal schema target"). Mood, journal, and companion-conversation
  content are named sensitive by `product/PRIVACY.md`.
- **Cross-user isolation**: there is no separate app-layer authorization
  service — "No microservices, queues, Redis, GraphQL, custom auth...
  machinery" (`product/ARCHITECTURE.md`). Row-Level Security is the sole
  enforcement point: "Database access defaults closed; use RLS and
  justified policies" (`product/PRIVACY.md`). In this architecture,
  `AUTHORIZATION` and `RLS_LEAST_PRIVILEGE` are the same enforcement
  point observed from two angles.
- **Secrets**: OpenRouter API key(s) and any Supabase service-role key.
  "Secret/server credentials never enter client code" (`product/PRIVACY.md`);
  "No secrets in prompts, logs, Discord, Langfuse, screenshots, PR text or
  client code" (`protocol/RUN-002-PROTOCOL-v2.0.md` "Security protocol").
- **AI companion boundary**: CompanionService over OpenRouter. "User
  content is untrusted input and never becomes system instruction. Model
  output is untrusted content and not executable/unsafe HTML by default"
  (`protocol/RUN-002-PROTOCOL-v2.0.md` "AI companion boundary").
- **Server/client boundary**: Next.js is UI and application backend in
  one deployment; "Approved boundaries only: CompanionService,
  DecisionService, database access, deterministic analytics"
  (`product/ARCHITECTURE.md`). `SERVER_CLIENT_BOUNDARIES` reviews that
  secrets and database access stay server-side within this single app,
  not that a separate service tier is respected — there isn't one.

## No deterministic security severity floor is source-authorised

Every frozen Run 002/product document was checked specifically for a
*severity* statement about a security category — not a requirement, not
a prohibition, a severity statement, the way `product/ACCESSIBILITY.md`
explicitly states "Accessibility failures are P1 unless more severe" for
`protocol/SEVERITY-POLICY.md`'s deterministic P1 floor. None exists:

- Protocol v2's "No secrets... anywhere" is an absolute *prohibition*
  that the behaviour must not occur. It does not state what severity a
  violation receives.
- `product/PRIVACY.md`'s "Database access defaults closed; use RLS" is a
  *requirement* for how access control works, not a severity statement
  about an RLS defect.
- `agents/SECURITY.md`'s "Deterministic security floors may raise a
  finding's minimum severity; they never lower one" authorises the
  *mechanism* to exist and constrains its direction if one is ever
  defined. It supplies no floor value itself.
- `protocol/JEV-CONTROL-PLANE-V2.md`'s incident-classification enum
  (`CREDENTIAL`, `SECURITY`, `PRIVACY`, ...) cannot supply a severity
  either: that document states classification "is independent of, and
  never derived from or substituted for... severity (how serious it
  is)... none is computed from another."
- Discord's "CRITICAL: privacy/security/experiment-integrity emergency"
  is a *notification urgency* tier, never equated anywhere to a P0
  finding severity.

Inferring a floor from the importance or absoluteness of a requirement,
rather than from an explicit severity statement, would be the same
category of unauthorised invention this threat model exists to prevent.

**Decision, recorded 2026-09-25 (human governance, Serina):** Run 002
defines no deterministic security severity floor at this time. Security
findings continue through ordinary Jev/reviewer P0–P3 classification
based on actual evidence and impact, exactly as `agents/SECURITY.md`
already states: P0/P1 block merge, valid P2/P3 are tracked non-blocking
debt, and invalid or missing security evidence fails closed independently
of severity. `apply_security_floor()` is deliberately not implemented.
`protocol/SEVERITY-POLICY.md`'s accessibility P1 floor is unaffected — it
has its own explicit source grounding and continues to apply.

Should a future document ever state an explicit security severity floor,
or should a human explicitly authorise specific floor values, this
section is where that decision is recorded before any code implements it.
