"""Timezone-aware experiment clock and deadline phases.

The 24-hour development clock starts at T+00 and never pauses. Provider
cooldowns, host outages and human sleep do not stop it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Deadline phases from experiment/EXPERIMENT.md, as (start_hour, end_hour, name).
PHASES: tuple[tuple[float, float, str], ...] = (
    (0, 16, "NORMAL"),
    (16, 18, "NO_OPTIONAL_WORK"),
    (18, 21, "FEATURE_FREEZE"),
    (21, 23, "STABILISATION_P0_P1"),
    (23, 24, "RELEASE_BLOCKERS_ONLY"),
)

CHECKPOINT_HOURS: tuple[int, ...] = (6, 12, 18, 21, 24)


def now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class ClockState:
    started_at: datetime
    now: datetime
    duration_hours: int

    @property
    def elapsed(self) -> timedelta:
        return self.now - self.started_at

    @property
    def elapsed_hours(self) -> float:
        return self.elapsed.total_seconds() / 3600.0

    @property
    def remaining_hours(self) -> float:
        return self.duration_hours - self.elapsed_hours

    @property
    def phase(self) -> str:
        hours = self.elapsed_hours
        if hours >= self.duration_hours:
            return "FROZEN"
        for start, end, name in PHASES:
            if start <= hours < end:
                return name
        return "FROZEN"

    @property
    def expired(self) -> bool:
        return self.elapsed_hours >= self.duration_hours

    def label(self) -> str:
        total_minutes = int(self.elapsed.total_seconds() // 60)
        return f"T+{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def phase_allows(phase: str, kind: str) -> bool:
    """Whether a class of work may still be dispatched in the given phase.

    kind is one of: feature, optional, stabilisation, release_blocker.
    """
    if phase == "FROZEN":
        return False
    if phase == "NORMAL":
        return True
    if phase == "NO_OPTIONAL_WORK":
        return kind != "optional"
    if phase == "FEATURE_FREEZE":
        return kind in ("stabilisation", "release_blocker")
    if phase == "STABILISATION_P0_P1":
        return kind in ("stabilisation", "release_blocker")
    if phase == "RELEASE_BLOCKERS_ONLY":
        return kind == "release_blocker"
    return False
