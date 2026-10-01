"""Paths and experiment configuration.

Configuration is data, not code: `config/experiment.json` is the single place a
human sets the budget, concurrency and model choices. Secrets are never stored
here - only the *names* of the environment variables that must be present.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / ".runtime"
CONFIG_PATH = REPO_ROOT / "config" / "experiment.json"

# Optional operator-supplied secrets, outside the repository and never committed.
# KEY=VALUE per line. Values are loaded into this process's environment and are
# never printed, logged, traced or passed to an agent prompt.
SECRETS_FILE = Path.home() / ".config" / "run-002" / "secrets.env"

LEDGER_PATH = RUNTIME_DIR / "ledger.jsonl"
STATE_PATH = RUNTIME_DIR / "state.json"
HEARTBEAT_PATH = RUNTIME_DIR / "supervisor-heartbeat.json"
PID_PATH = RUNTIME_DIR / "supervisor.pid"
# Exclusion, as against reporting. PID_PATH says which process to look at;
# this inode is what actually stops a second Supervisor, via a non-blocking
# flock held open for the process lifetime. It is created once and never
# unlinked or replaced during normal operation - a new inode under the same
# name would be a second, independent lock, which is how "atomic" exclusion
# quietly becomes no exclusion at all.
SINGLETON_LOCK_PATH = RUNTIME_DIR / "supervisor.lock"
WATCHDOG_PID_PATH = RUNTIME_DIR / "watchdog.pid"
BASELINE_PATH = RUNTIME_DIR / "baseline.json"
PREFLIGHT_PATH = RUNTIME_DIR / "preflight.json"
SUPERVISOR_LOG = RUNTIME_DIR / "supervisor.log"
WATCHDOG_LOG = RUNTIME_DIR / "watchdog.log"
WORKER_LOG_DIR = RUNTIME_DIR / "workers"
OBSERVER_DIR = RUNTIME_DIR / "observer"
# C-05: durable per-attempt accessibility/security evidence. Local only -
# no external publishing in C-05.
EVIDENCE_DIR = RUNTIME_DIR / "evidence"

PROMPTS_DIR = REPO_ROOT / "prompts"
WORKTREE_ROOT = REPO_ROOT.parent / "worktrees"

# Secrets required before T+00. Presence only - values are never read into logs.
REQUIRED_SECRETS: tuple[str, ...] = (
    "OPENROUTER_API_KEY",
    "DISCORD_WEBHOOK_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
)


@dataclass(frozen=True)
class RoleConfig:
    """How one agent role is invoked. Cheapest suitable model by default."""

    provider: str
    model: str
    effort: str = "medium"
    escalation_model: str | None = None


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    protocol_version: str
    timezone: str
    duration_hours: int
    budget_usd: float
    budget_configured_by: str
    max_builders: int
    max_fixers: int
    max_reviewers: int
    max_security: int
    # G7, human decision 2026-10-01. Both 1, matching every other
    # non-builder role. The automated half additionally drives a real
    # Chromium and a product build, which is the heaviest resource the run
    # uses, and C-08c inotify headroom is already failing on this host.
    max_accessibility_auto: int
    max_accessibility_review: int
    max_observers: int
    max_repair_cycles: int
    tmux_session: str
    github_repo: str
    main_branch: str
    required_checks: tuple[str, ...]
    roles: dict[str, RoleConfig]
    observer: dict
    poll_seconds: int
    stale_task_minutes: int
    heartbeat_stale_seconds: int
    cooldown_seconds: dict[str, int]
    extra: dict = field(default_factory=dict)
    jev_pricing: dict = field(default_factory=dict)


def load_secrets_file(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines into the environment. Returns the NAMES only.

    An existing environment variable always wins, so this can only fill gaps.
    """
    path = path or SECRETS_FILE
    if not path.exists():
        return []
    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip("'\"")
        if name and value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded


def secrets_file_permissions_ok(path: Path | None = None) -> bool | None:
    """True when only the owner can read it. None when the file is absent."""
    path = path or SECRETS_FILE
    if not path.exists():
        return None
    return (path.stat().st_mode & 0o077) == 0


def load() -> ExperimentConfig:
    load_secrets_file()
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    roles = {
        name: RoleConfig(
            provider=spec["provider"],
            model=spec["model"],
            effort=spec.get("effort", "medium"),
            escalation_model=spec.get("escalation_model"),
        )
        for name, spec in raw["roles"].items()
    }
    return ExperimentConfig(
        experiment_id=raw["experiment_id"],
        protocol_version=raw["protocol_version"],
        timezone=raw["timezone"],
        duration_hours=int(raw["duration_hours"]),
        budget_usd=float(raw["budget"]["total_usd"]),
        budget_configured_by=raw["budget"]["configured_by"],
        max_builders=int(raw["concurrency"]["max_builders"]),
        max_fixers=int(raw["concurrency"]["max_fixers"]),
        max_reviewers=int(raw["concurrency"]["max_reviewers"]),
        max_security=int(raw["concurrency"]["max_security"]),
        # Required, not defaulted. A missing governed concurrency bound must
        # fail loudly at load rather than silently become some number this
        # module chose - the same rule every sibling above follows.
        max_accessibility_auto=int(raw["concurrency"]["max_accessibility_auto"]),
        max_accessibility_review=int(
            raw["concurrency"]["max_accessibility_review"]),
        max_observers=int(raw["concurrency"]["max_observers"]),
        max_repair_cycles=int(raw["concurrency"]["max_repair_cycles"]),
        tmux_session=raw["tmux_session"],
        github_repo=raw["github"]["repo"],
        main_branch=raw["github"]["main_branch"],
        required_checks=tuple(raw["github"]["required_checks"]),
        roles=roles,
        observer=raw["observer"],
        poll_seconds=int(raw["supervisor"]["poll_seconds"]),
        stale_task_minutes=int(raw["supervisor"]["stale_task_minutes"]),
        heartbeat_stale_seconds=int(raw["supervisor"]["heartbeat_stale_seconds"]),
        cooldown_seconds=dict(raw["providers"]["cooldown_seconds"]),
        extra=raw,
        jev_pricing=dict(raw.get("jev_pricing") or {}),
    )


def jev_reservation_usd(cfg: ExperimentConfig) -> float | None:
    """The governed per-call allowance reserved before a paid Jev request.

    None means no defensible bound is configured, and the paid path is blocked
    rather than a number being guessed. The basis for the configured value is
    recorded in config/experiment.json alongside it.
    """
    value = (cfg.jev_pricing or {}).get("reservation_usd_per_call")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    if not math.isfinite(amount) or amount <= 0:
        return None
    return amount


def ensure_runtime_dirs() -> None:
    for path in (RUNTIME_DIR, WORKER_LOG_DIR, OBSERVER_DIR):
        path.mkdir(parents=True, exist_ok=True)


def missing_secrets() -> list[str]:
    """Names only - never values."""
    return [name for name in REQUIRED_SECRETS if not os.environ.get(name)]
