# Run 002 — Timeline

Canonical sources: `protocol/RUN-002-PROTOCOL-v2.0.md` (`duration_hours: 24`;
invariant "the 24-hour clock never pauses"), `experiment/FREEZE-PROTOCOL.md`
(T+00/T+24 freeze governance), `experiment/CONTRADICTION-AUDIT.md` (C-07,
C-07a). Implemented by: `control/clock.py` (`ClockState`, `CHECKPOINT_HOURS`,
legacy `PHASES`/`phase_allows()`).

## Duration and boundaries

- Duration: exactly 24 wall-clock hours, start to finish.
- T+00: **NOT_STARTED.** No absolute date or time is recorded here, or
  invented by this document. T+00 is set only when the experiment actually
  begins.
- T+24: the real, fixed experiment freeze/expiration boundary. See
  `experiment/FREEZE-PROTOCOL.md` for what happens at T+00 and at T+24 —
  this document situates that governance on the clock; it does not
  redefine it.
- The 24-hour clock never pauses once started (Protocol v2 invariant 7):
  provider cooldowns, host outages, and human sleep do not stop it.

## Status

Experiment status: **NOT_STARTED.** Nothing in this document claims the
experiment has started, is in progress, has completed, or has frozen. No
event, checkpoint, or timestamp described below has actually occurred —
this document describes mechanism only.

## Checkpoint cadence (C-07a)

Run 002 sends INFO-level Discord checkpoint notifications at **T+06, T+12,
T+18, T+21, and T+24** (`control/clock.py`'s `CHECKPOINT_HOURS`, approved
unchanged by human governance decision C-07a — see
`experiment/CONTRADICTION-AUDIT.md`). These are observational notifications
only: they report elapsed time, completed tasks, and the current legacy
phase label (below). They do not affect task dispatch, task state,
severity, review verdicts, merge eligibility, or the clock itself.

## Legacy phase labels are observational only (C-07)

`control/clock.py` also computes a legacy intra-day label
(`ClockState.phase`): `NORMAL`, `NO_OPTIONAL_WORK`, `FEATURE_FREEZE`,
`STABILISATION_P0_P1`, `RELEASE_BLOCKERS_ONLY`, or `FROZEN`, depending on
elapsed hours. This schedule was inherited unreviewed from Run 001 and, per
human governance decision C-07 (`experiment/CONTRADICTION-AUDIT.md`), does
**not** gate task dispatch. Per C-07, these legacy phase labels do not
determine task-dispatch eligibility. Task dispatch remains governed by the
Run 002 control plane's authorised eligibility, dependency, resource,
provider and experiment-boundary rules.

The label may still be displayed (for example, in checkpoint notification
text or CLI status output) for human orientation. A displayed label such as
`FEATURE_FREEZE` is descriptive only and must never be read as active
dispatch policy.

## Protocol v2 Freeze vs. the legacy `FEATURE_FREEZE` label — not the same thing

Protocol v2's real "Freeze" — the governance event described in full in
`experiment/FREEZE-PROTOCOL.md` — is the only "freeze" with actual authority
over the experiment. The legacy `FEATURE_FREEZE` label above is an
unrelated, inherited display label naming one segment of the clock (hours
18–21 of `control/clock.py`'s `PHASES`). It shares a word with Protocol
v2's Freeze by coincidence of naming, not by shared meaning or authority.
This section exists to keep that distinction explicit, since the two are
easy to conflate.

## What this document is not

This is a mechanism/index document, not a live status report. It does not
assert that any date, timestamp, or event has occurred. Changes to the
24-hour duration or T+00/T+24 freeze governance belong to Protocol v2.
Changes to the checkpoint cadence belong to C-07a. Any proposal to restore
or alter legacy phase-based dispatch authority belongs to C-07. This
document reflects those canonical decisions; it does not redefine them.
