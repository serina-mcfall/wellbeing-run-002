# Interpretation of imported Run 001 specifications for Run 002

Status: DRAFT — subject to the contradiction audit and preflight.

The eight imported files in product/, tasks/TASKS.md, and config/tasks.json
are byte-for-byte source copies. Keep them unchanged. Run 002 implements
their product scope and task acceptance criteria under Protocol v2.

## Product scope

The PWA clarification in product/MVP.md applies equally to Run 002:
installable responsive web app, valid manifest/icons, and usable
standalone presentation. Offline-first data and background sync remain
outside the MVP.

The six Jev product suggestions in product/AI.md remain
TALK/JOURNAL/MEDITATE/MOVE/SOUNDSCAPE/NOTHING. The separate Run 001
control-plane Jev labels in that file are historical only. Run 002's
control-plane classifier and deterministic authority come from
protocol/RUN-002-PROTOCOL-v2.0.md. Product decisions and control-plane
decisions must have separate schemas and call sites.

An actual accessibility failure inherits the minimum P1 severity stated
in product/ACCESSIBILITY.md, subject to a higher severity where warranted.
A recommendation that does not identify a failed requirement may be P2
or P3. The reviewer supplies evidence; deterministic policy applies the
floor and decides merge eligibility.

## Task inheritance

For Run 002, the opening sentence of tasks/TASKS.md refers to this
repository's AGENTS.md and Run 002 role contracts, once created. It does
not import or invoke Run 001's AGENTS.md, prompts, or runtime roles.
The copied task bodies and product specifications retain their original
acceptance scope. Any technical dependency change requires a recorded
pre-freeze justification and another graph audit.
