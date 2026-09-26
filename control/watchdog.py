"""Watchdog. Minimal, non-LLM, single-purpose.

Checks the supervisor's PID and heartbeat. If it is dead or stale, restarts it
once and logs the restart. If the restart fails, sends a HUMAN_REQUIRED Discord
alert. It does nothing else.

Environmental limitation: a watchdog running on the experiment host cannot
detect or notify during a complete host or WSL shutdown. Recovery is measured
from durable Git and ledger state once the runtime returns.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import clock, config, gh, ledger as ledger_mod, merge_invariant, notify
from . import proc, reconcile
from . import state as state_mod

CHECK_SECONDS = 30

# Restarting forever is worse than escalating: a supervisor that dies repeatedly
# is a fault a human must see.
CRASH_LOOP_LIMIT = 3
CRASH_LOOP_WINDOW_SECONDS = 600


def supervisor_pid() -> int | None:
    if not config.PID_PATH.exists():
        return None
    try:
        return int(config.PID_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def alive(pid: int | None) -> bool:
    """A zombie is not alive. See control/proc.py for why this matters here."""
    return proc.is_running(pid)


def _heartbeat() -> dict | None:
    if not config.HEARTBEAT_PATH.exists():
        return None
    try:
        payload = json.loads(config.HEARTBEAT_PATH.read_text(encoding="utf-8"))
        payload["_at"] = datetime.fromisoformat(payload["at"])
        payload["_pid"] = int(payload["pid"])
        return payload
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
        return None


def heartbeat_fresh(tz: str, stale_seconds: int) -> bool:
    """Fresh, or legitimately busy inside a bounded window it declared.

    Approved slow work - a bounded Observer job, a provider call - runs outside
    the state lock but still inside the tick, so no heartbeat is written while it
    runs. Without this the supervisor looks dead every time it observes.

    This does not blunt failure detection. The declared window has a deadline
    derived from the operation's own timeout; overrun it and the supervisor is
    stale again. And a dead process is detected by its PID regardless of any
    window it declared before dying.
    """
    beat = _heartbeat()
    if beat is None:
        return False
    now = datetime.now(ZoneInfo(tz))
    if now - beat["_at"] < timedelta(seconds=stale_seconds):
        return True
    busy_until = beat.get("busy_until")
    if busy_until:
        try:
            return now <= datetime.fromisoformat(busy_until)
        except ValueError:
            return False
    return False


def busy_with(tz: str) -> str | None:
    beat = _heartbeat()
    if not beat or not beat.get("busy_until"):
        return None
    try:
        if datetime.now(ZoneInfo(tz)) <= datetime.fromisoformat(beat["busy_until"]):
            return beat.get("busy_with")
    except ValueError:
        return None
    return None


def start_supervisor() -> int | None:
    script = config.REPO_ROOT / "bin" / "supervisor.sh"
    with open(config.SUPERVISOR_LOG, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            ["bash", str(script)], cwd=str(config.REPO_ROOT),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    time.sleep(5)
    return process.pid if process.poll() is None else None


def reconcile_merge_invariants(cfg, ledger, notifier, doc) -> None:
    """C-14.2: the Watchdog's own three-source merge check.

    Independent by construction. The Supervisor is the component whose merge
    claims are in doubt, so a check only it runs proves nothing; this pass
    makes its own GitHub observation and reads the ledger itself.

    A sibling of reconcile.reconcile(), not an extension of it: that function
    stays pure, worker-scoped and bounded to a single git call. This one is
    also the only path that can see a divergence on an already-COMPLETE task,
    because the Supervisor's routing skips pull requests state records as
    merged.

    Detects, annunciates and freezes where legal. Never repairs, and never
    raises the experiment-wide guardrail.
    """
    now_iso = clock.iso(clock.now(cfg.timezone))
    for task_id, task in sorted((doc.get("tasks") or {}).items()):
        pr_number = task.get("pr")
        if pr_number is None:
            continue
        record = (doc.get("prs") or {}).get(str(pr_number))
        pr_view = gh.pr_view(getattr(cfg, "github_repo", ""), pr_number)
        github, state_view, ledger_view = merge_invariant.gather(
            task_id=task_id, pr_number=pr_number, doc=doc, task=task,
            record=record, ledger=ledger, pr_view=pr_view, now_iso=now_iso,
        )
        verdict = merge_invariant.classify(
            task_id=task_id, pr_number=pr_number, github=github,
            state_view=state_view, ledger_view=ledger_view,
        )
        if verdict is None or not verdict.dangerous:
            continue
        merge_invariant.annunciate(
            doc=doc, task=task, record=record, verdict=verdict, github=github,
            state_view=state_view, ledger_view=ledger_view, ledger=ledger,
            notifier=notifier, detector="watchdog", tz=cfg.timezone,
        )


def reconcile_worker_state(cfg, ledger, notifier, *, store=None) -> None:
    """Deterministic policy acting on control/reconcile.py's facts: one
    dangerous finding is sufficient to freeze the task it names. Never
    repairs bookkeeping, never restarts or kills the worker, never
    unfreezes a task - only a human decision from HUMAN_REQUIRED does
    that (see control/state.py's FROZEN comment). Independent of whether
    Supervisor itself is currently healthy: a healthy Supervisor process
    can still be carrying stale/wrong bookkeeping from earlier.

    This freeze is the Watchdog's own autonomous control-plane action, not
    a human doing anything - human_intervention is False here, matching
    the existing convention (supervisor.py: human_intervention is True
    only when the notification severity is HUMAN_REQUIRED; this uses
    CRITICAL). A later human decision that resolves FROZEN ->
    HUMAN_REQUIRED, or whatever a human does after that, is recorded as
    human intervention separately, when it actually happens.

    `store` is injectable for testing; production callers omit it and get
    the real experiment state.
    """
    store = store or state_mod.Store(tz=cfg.timezone)
    if not store.exists():
        return  # no experiment state yet (e.g. pre-T+00) - nothing to reconcile

    try:
        with store.transaction() as doc:
            reconcile_merge_invariants(cfg, ledger, notifier, doc)
            findings = reconcile.reconcile(doc, tz=cfg.timezone)
            already_handled: set[str] = set()
            for finding in findings:
                if not finding.dangerous or finding.task_id is None:
                    continue
                if finding.task_id in already_handled:
                    continue  # an earlier finding this pass already froze this task
                task = doc["tasks"].get(finding.task_id)
                if task is None or task.get("state") not in reconcile.RECONCILABLE_STATES:
                    continue  # state moved on since reconcile() read it; act on current fact
                state_before = task["state"]
                try:
                    state_mod.transition(
                        doc, finding.task_id, "FROZEN",
                        f"STATE_INVARIANT_VIOLATION: {finding.check_id}: {finding.evidence}",
                        cfg.timezone,
                    )
                except state_mod.TransitionError:
                    continue
                already_handled.add(finding.task_id)
                ledger.append(
                    "STATE_INVARIANT_VIOLATION", task_id=finding.task_id,
                    state_before=state_before, state_after="FROZEN",
                    human_intervention=False, activity_class="ESCALATION", outcome="FROZEN",
                    metadata_redacted={"check_id": finding.check_id, "worker": finding.worker,
                                       "evidence": finding.evidence},
                )
                notifier.send(
                    notify.CRITICAL, f"{finding.task_id} frozen: state invariant violation",
                    f"{finding.check_id}: {finding.evidence}",
                )
    except Exception as exc:  # noqa: BLE001 - a bug here must not take down the watchdog's
        # primary supervisor-liveness duty; it must also never fail silently.
        ledger.append("RECONCILE_ERROR", outcome="ERROR", activity_class="ORCHESTRATION",
                      metadata_redacted={"error": str(exc)})


def main() -> int:
    cfg = config.load()
    config.ensure_runtime_dirs()
    config.WATCHDOG_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    notifier = notify.Notifier(cfg.experiment_id)

    ledger.append("WATCHDOG_STARTED", outcome="RUNNING", activity_class="ORCHESTRATION",
                  metadata_redacted={"pid": os.getpid()})

    restarts: list[float] = []

    while True:
        proc.reap_children()
        reconcile_worker_state(cfg, ledger, notifier)
        pid = supervisor_pid()
        healthy = alive(pid) and heartbeat_fresh(cfg.timezone, cfg.heartbeat_stale_seconds)

        if not healthy:
            now = time.monotonic()
            restarts = [t for t in restarts if now - t < CRASH_LOOP_WINDOW_SECONDS]
            if len(restarts) >= CRASH_LOOP_LIMIT:
                ledger.append("SUPERVISOR_CRASH_LOOP", outcome="HUMAN_REQUIRED",
                              human_intervention=True, activity_class="ESCALATION",
                              metadata_redacted={"restarts": len(restarts),
                                                 "window_seconds": CRASH_LOOP_WINDOW_SECONDS})
                notifier.send(notify.HUMAN_REQUIRED, "Supervisor is crash-looping",
                              f"{len(restarts)} restarts within "
                              f"{CRASH_LOOP_WINDOW_SECONDS // 60} minutes. The watchdog has "
                              "stopped restarting it and needs a human.")
                return 1
            restarts.append(now)

            ledger.append("SUPERVISOR_UNHEALTHY", outcome="RESTARTING",
                          activity_class="ORCHESTRATION",
                          metadata_redacted={"pid": pid, "alive": alive(pid)})
            new_pid = start_supervisor()
            if new_pid and heartbeat_wait(cfg, new_pid):
                ledger.append("SUPERVISOR_RESTARTED", outcome="RECOVERED",
                              activity_class="ORCHESTRATION",
                              metadata_redacted={"pid": new_pid})
                notifier.send(notify.ATTENTION, "Supervisor restarted by watchdog",
                              "Liveness check failed; the supervisor was restarted and is "
                              "healthy again.", no_human_action_needed=True,
                              clock_label=clock.iso(clock.now(cfg.timezone)))
                continue

            # A start can be refused because the singleton guard found a live
            # incumbent. That is evidence the supervisor is alive, not evidence
            # that recovery failed - and killing the watchdog over it removes
            # liveness protection for the rest of the run. Re-check before
            # escalating; only a genuinely absent supervisor is a failed recovery.
            incumbent = supervisor_pid()
            if alive(incumbent) and heartbeat_fresh(cfg.timezone,
                                                    cfg.heartbeat_stale_seconds):
                if restarts:
                    restarts.pop()  # a false alarm must not count toward crash-looping
                ledger.append("SUPERVISOR_STAND_DOWN", outcome="INCUMBENT_ALIVE",
                              activity_class="ORCHESTRATION",
                              metadata_redacted={"incumbent_pid": incumbent,
                                                 "busy_with": busy_with(cfg.timezone),
                                                 "note": "restart refused or unnecessary; "
                                                         "the incumbent is healthy, so "
                                                         "watchdog protection continues"})
                continue

            ledger.append("SUPERVISOR_RESTART_FAILED", outcome="HUMAN_REQUIRED",
                          human_intervention=True, activity_class="ESCALATION",
                          metadata_redacted={"incumbent_pid": incumbent,
                                             "incumbent_alive": alive(incumbent)})
            notifier.send(notify.HUMAN_REQUIRED, "Supervisor restart failed",
                          "The watchdog could not bring the supervisor back and no "
                          "healthy supervisor is present. The control plane is down "
                          "and needs a human.")
            return 1

        time.sleep(CHECK_SECONDS)


def heartbeat_wait(cfg, expected_pid: int, attempts: int = 8) -> bool:
    """Recovery is proven by a fresh heartbeat carrying the NEW process's PID.

    Freshness alone is not enough: the dead supervisor's last heartbeat can still
    sit inside the staleness window, which would confirm a recovery that never
    happened. Identity is the sound signal - each supervisor stamps its own PID -
    and the timestamps are only second-resolution, so comparing them against the
    restart instant is unreliable within the same second.
    """
    for _ in range(attempts):
        beat = _heartbeat()
        if beat is not None:
            if beat["_pid"] == expected_pid and alive(expected_pid) and heartbeat_fresh(
                cfg.timezone, cfg.heartbeat_stale_seconds
            ):
                return True
        time.sleep(5)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
