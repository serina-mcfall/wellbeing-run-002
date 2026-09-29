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
import secrets
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import clock, config, gh, intervention, ledger as ledger_mod, merge_invariant
from . import metrics, notify, proc, reconcile
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


def _record_systemic_intervention(cfg, ledger, *, condition_code: str,
                                  reason: str, store=None) -> tuple[dict | None, str]:
    """C-08b.2 (S9/S10): best-effort durable obligation for a Watchdog
    escalation. Returns (record, bookkeeping_code) where the code is one of
    the finite values OK / STATE_ABSENT / STATE_WRITE_FAILED /
    REQUESTED_EVENT_FAILED - never exception text. Every failure mode is
    swallowed into that code, which the caller folds into the EXISTING
    Watchdog failure event, so intervention bookkeeping can never suppress
    the crash-loop/restart-failure alert."""
    store = store or state_mod.Store(tz=cfg.timezone)
    if not store.exists():
        return None, "STATE_ABSENT"
    try:
        with store.transaction() as doc:
            record, is_new = intervention.request(
                doc, type_="HUMAN_APPARATUS_AUTHORISATION", scope="systemic",
                task_id=None, reason=reason, condition_code=condition_code,
                tz=cfg.timezone)
            committed = dict(record)
    except Exception:  # noqa: BLE001 - finite code instead, never the string
        return None, "STATE_WRITE_FAILED"
    if is_new:
        try:
            ledger.append(
                "HUMAN_INTERVENTION_REQUESTED", outcome="REQUESTED",
                activity_class="ESCALATION", human_intervention=True,
                metadata_redacted={
                    "intervention_id": committed["id"],
                    "intervention_type": committed["type"],
                    "scope": committed["scope"],
                    "condition_code": committed["condition_code"],
                    "requested_at": committed["requested_at"],
                    "reason": committed["reason"],
                    "reason_withheld": committed["reason_withheld"],
                })
        except Exception:  # noqa: BLE001 - the record is durable; only the
            return committed, "REQUESTED_EVENT_FAILED"  # event evidence lags
    return committed, "OK"


def _suffix(record: dict | None) -> str:
    return intervention.notification_suffix(record) if record else ""


def escalate_crash_loop(cfg, ledger, notifier, restart_count: int,
                        *, store=None) -> None:
    """S9. Ordering is deliberate: durable intervention first (best effort),
    the existing failure event second - now carrying the finite bookkeeping
    outcome - the existing HUMAN_REQUIRED alert last and unconditionally:
    dedup never rate-limits this safety alert."""
    record, bookkeeping = _record_systemic_intervention(
        cfg, ledger, store=store, condition_code="supervisor_crash_loop",
        reason=f"Supervisor crash loop: {restart_count} restarts within "
               f"{CRASH_LOOP_WINDOW_SECONDS} seconds requires human "
               f"apparatus authorisation")
    ledger.append("SUPERVISOR_CRASH_LOOP", outcome="HUMAN_REQUIRED",
                  human_intervention=True, activity_class="ESCALATION",
                  metadata_redacted={"restarts": restart_count,
                                     "window_seconds": CRASH_LOOP_WINDOW_SECONDS,
                                     "intervention_id": record["id"] if record else None,
                                     "intervention_bookkeeping": bookkeeping})
    notifier.send(notify.HUMAN_REQUIRED, "Supervisor is crash-looping",
                  f"{restart_count} restarts within "
                  f"{CRASH_LOOP_WINDOW_SECONDS // 60} minutes. The watchdog has "
                  "stopped restarting it and needs a human." + _suffix(record))


def escalate_restart_failed(cfg, ledger, notifier, incumbent: int | None,
                            *, store=None) -> None:
    """S10. Same guarded ordering as escalate_crash_loop."""
    record, bookkeeping = _record_systemic_intervention(
        cfg, ledger, store=store, condition_code="supervisor_restart_failed",
        reason="Supervisor restart failed and no healthy supervisor is "
               "present; human apparatus authorisation required")
    ledger.append("SUPERVISOR_RESTART_FAILED", outcome="HUMAN_REQUIRED",
                  human_intervention=True, activity_class="ESCALATION",
                  metadata_redacted={"incumbent_pid": incumbent,
                                     "incumbent_alive": alive(incumbent),
                                     "intervention_id": record["id"] if record else None,
                                     "intervention_bookkeeping": bookkeeping})
    notifier.send(notify.HUMAN_REQUIRED, "Supervisor restart failed",
                  "The watchdog could not bring the supervisor back and no "
                  "healthy supervisor is present. The control plane is down "
                  "and needs a human." + _suffix(record))


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
                # C-08b.2 (S12): the freeze and its human-verification
                # obligation commit in this same state transaction. The reason
                # is identity-only; check_id/worker/evidence stay in the
                # STATE_INVARIANT_VIOLATION event.
                int_record, int_is_new = intervention.request(
                    doc, type_="HUMAN_VERIFICATION", scope="task",
                    task_id=finding.task_id,
                    reason=f"Worker state integrity requires human "
                           f"verification for {finding.task_id}",
                    condition_code="worker_state_invariant", tz=cfg.timezone)
                ledger.append(
                    "STATE_INVARIANT_VIOLATION", task_id=finding.task_id,
                    state_before=state_before, state_after="FROZEN",
                    human_intervention=False, activity_class="ESCALATION", outcome="FROZEN",
                    metadata_redacted={"check_id": finding.check_id, "worker": finding.worker,
                                       "evidence": finding.evidence},
                )
                if int_is_new:
                    ledger.append(
                        "HUMAN_INTERVENTION_REQUESTED", task_id=finding.task_id,
                        outcome="REQUESTED", activity_class="ESCALATION",
                        human_intervention=True,
                        metadata_redacted={
                            "intervention_id": int_record["id"],
                            "intervention_type": int_record["type"],
                            "scope": int_record["scope"],
                            "condition_code": int_record["condition_code"],
                            "requested_at": int_record["requested_at"],
                            "reason": int_record["reason"],
                            "reason_withheld": int_record["reason_withheld"],
                        })
                notifier.send(
                    notify.CRITICAL, f"{finding.task_id} frozen: state invariant violation",
                    f"{finding.check_id}: {finding.evidence}"
                    + intervention.notification_suffix(int_record),
                )
    except Exception:  # noqa: BLE001 - a bug here must not take down the watchdog's
        # primary supervisor-liveness duty; it must also never fail silently.
        # Fixed finite structural metadata only (D2/C-14 amendment): runtime
        # exception prose can carry path/document/env-derived material the
        # static secret scan cannot vet, so nothing derived from the
        # exception - not even its class name - is persisted.
        ledger.append("RECONCILE_ERROR", outcome="ERROR", activity_class="ORCHESTRATION",
                      metadata_redacted={"phase": "RECONCILE_TRANSACTION",
                                         "error_code": "RECONCILIATION_FAILED"})


# ------------------------------------------------------ C-09 annunciation
#
# Deterministic policy over reconcile.detect_orphans' facts. Detection is
# not repair: nothing here (or anywhere) removes a worktree, kills a
# process, or closes a listener. The lifecycle per occurrence is
# OBSERVED -> ATTEMPTING -> DELIVERED with a durable pre-send fence:
#
#   * the occurrence (fingerprint, occurrence_id) and its ATTEMPTING fence
#     commit BEFORE any external effect, so a crash at any point leaves an
#     ambiguous attempt durably suppressed - at most one external send per
#     durably authorised attempt;
#   * delivery/failure evidence is an immediate occurrence-scoped ledger
#     append, which survives any number of lost state commits - it, not
#     the state flag, is what suppresses a resend after a success;
#   * a new attempt exists only after a durably known failure
#     (ORPHAN_ANNUNCIATION_FAILED for the current attempt);
#   * clearing happens only on an affirmatively complete scan showing the
#     resource absent - a failed scan never clears evidence;
#   * genuine disappearance + recurrence mints a new occurrence_id, so the
#     occurrence-scoped guards allow exactly one fresh notification.

ORPHAN_CLASS = {
    "ORPHAN_WORKTREE": "worktree",
    "ORPHAN_WORKER_PROCESS": "process",
    "ORPHAN_AGENT_PROCESS": "process",
    "FOREIGN_OR_ORPHAN_LISTENER": "port",
    "PORT_ASSIGNMENT_CONFLICT": "port",
    # C-05.2. All four browser findings share one class, because clearing
    # is governed by whether the BROWSER OBSERVATION was complete -
    # scan_ok["browser"] - and that one flag is what reconcile computes.
    # An unmapped check_id can never clear (ORPHAN_CLASS.get returns None
    # and scan_ok.get(None) is falsy), so leaving any of these out would
    # retain a resolved finding for the rest of the run.
    "BROWSER_EVIDENCE_UNREADABLE": "browser",
    "BROWSER_IDENTITY_UNVERIFIABLE": "browser",
    "BROWSER_TERMINALITY_UNKNOWN": "browser",
    "ORPHAN_BROWSER_PROCESS": "browser",
}


# Which findings assert that a resource HAS NO OWNER, as against ones
# that assert only that ownership could not be DETERMINED. Both deserve a
# human's attention and both are annunciated - but telling someone an
# unreadable sidecar is an orphan sends them hunting a leak that may not
# exist, and a detector that cries wolf gets muted, after which the real
# orphan goes unread too.
#
# An ALLOW-LIST, deliberately. A check_id added later and not listed here
# reads as an observation rather than an orphan - understating a claim,
# never overstating one. A deny-list would fail the other way.
ORPHAN_CLAIM_CHECKS = frozenset({
    "ORPHAN_WORKTREE", "ORPHAN_WORKER_PROCESS", "ORPHAN_AGENT_PROCESS",
    "FOREIGN_OR_ORPHAN_LISTENER", "PORT_ASSIGNMENT_CONFLICT",
    "ORPHAN_BROWSER_PROCESS",
})


def _annunciation(entry: dict) -> tuple[str, str]:
    """(subject, body) for one finding, truthful about which claim it is.

    Both forms are fixed control-plane text plus bounded identifiers - a
    check_id from a finite set, a resource_id, a hex occurrence - so
    nothing exception-derived can reach a notification.
    """
    if entry["check_id"] in ORPHAN_CLAIM_CHECKS:
        return (
            f"Orphan resource detected: {entry['check_id']}",
            f"{entry['resource_id']} has no durable owner "
            f"(occurrence {entry['occurrence_id']}). Detection only - "
            "no automatic removal, kill, or repair is performed.")
    return (
        f"Resource reconciliation finding: {entry['check_id']}",
        f"{entry['resource_id']} could not be reconciled "
        f"(occurrence {entry['occurrence_id']}). Ownership is UNKNOWN, not "
        "disproven - this is a gap in observation, not a confirmed leak. "
        "Detection only - no automatic removal, kill, or repair is "
        "performed.")


def _orphan_event_exists(ledger, event_type: str, occurrence_id: str,
                         attempt: int | None = None) -> bool:
    path = getattr(ledger, "path", None)
    if path is None or not Path(path).exists():
        return False
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != event_type:
            continue
        meta = event.get("metadata_redacted") or {}
        if meta.get("occurrence_id") != occurrence_id:
            continue
        if attempt is not None and meta.get("attempt") != attempt:
            continue
        return True
    return False


def annunciate_orphans(cfg, ledger, notifier, *, store=None) -> None:
    store = store or state_mod.Store(tz=cfg.timezone)
    if not store.exists():
        return
    to_send: list[dict] = []
    try:
        with store.transaction() as doc:
            entries = doc.setdefault("orphan_annunciations", {})

            # Converge ATTEMPTING from durable ledger evidence: DELIVERED
            # wins permanently; a durably known FAILED attempt makes the
            # occurrence retryable; neither leaves it suppressed.
            for entry in entries.values():
                if entry.get("status") != "ATTEMPTING":
                    continue
                if _orphan_event_exists(ledger, "ORPHAN_ANNUNCIATION_DELIVERED",
                                        entry["occurrence_id"]):
                    entry["status"] = "DELIVERED"
                elif _orphan_event_exists(ledger, "ORPHAN_ANNUNCIATION_FAILED",
                                          entry["occurrence_id"],
                                          attempt=entry.get("attempt")):
                    entry["status"] = "OBSERVED"

            findings, scan_ok = reconcile.detect_orphans(doc)
            current: set[str] = set()
            for finding in findings:
                fingerprint = f"{finding.check_id}:{finding.resource_id}"
                current.add(fingerprint)
                entries.setdefault(fingerprint, {
                    "occurrence_id": f"ORP-{secrets.token_hex(8)}",
                    "status": "OBSERVED",
                    "attempt": 0,
                    "first_observed_at": clock.iso(clock.now(cfg.timezone)),
                    "check_id": finding.check_id,
                    "resource_id": finding.resource_id,
                })

            for fingerprint in list(entries):
                cls = ORPHAN_CLASS.get(entries[fingerprint].get("check_id"))
                if scan_ok.get(cls) and fingerprint not in current:
                    entries.pop(fingerprint)  # affirmatively absent

            # The durable pre-send fence: commit ATTEMPTING before any
            # external effect may happen.
            for fingerprint, entry in entries.items():
                if entry["status"] == "OBSERVED":
                    entry["status"] = "ATTEMPTING"
                    entry["attempt"] += 1
                    to_send.append(dict(entry, fingerprint=fingerprint))
    except Exception:  # noqa: BLE001 - never take down the watchdog loop
        # Truly finite structural metadata only: runtime exception prose can
        # carry anything (paths, env fragments, secret-shaped material) and
        # the static secret scan cannot vet it - not even a dynamic class
        # name is persisted. The fixed pair below says everything the record
        # needs to: the observation transaction failed, and, because to_send
        # is only populated by a committed fence, no external notification
        # came from it.
        ledger.append("ORPHAN_ANNUNCIATION_ERROR", outcome="ERROR",
                      activity_class="ORCHESTRATION",
                      metadata_redacted={"phase": "OBSERVE_TRANSACTION",
                                         "error_code": "OBSERVATION_FAILED"})
        return

    for entry in to_send:
        send_started = False
        try:
            if not _orphan_event_exists(ledger, "ORPHAN_DETECTED",
                                        entry["occurrence_id"]):
                ledger.append(
                    "ORPHAN_DETECTED", outcome="DETECTED",
                    activity_class="ORCHESTRATION",
                    metadata_redacted={
                        "fingerprint": entry["fingerprint"],
                        "occurrence_id": entry["occurrence_id"],
                        "check_id": entry["check_id"],
                        "resource_id": entry["resource_id"],
                        "first_observed_at": entry["first_observed_at"],
                    })
            subject, body = _annunciation(entry)
            send_started = True
            result = notifier.send(notify.ATTENTION, subject, body)
            if result and result.get("ok"):
                ledger.append(
                    "ORPHAN_ANNUNCIATION_DELIVERED", outcome="DELIVERED",
                    activity_class="ORCHESTRATION",
                    metadata_redacted={
                        "fingerprint": entry["fingerprint"],
                        "occurrence_id": entry["occurrence_id"],
                    })
            else:
                ledger.append(
                    "ORPHAN_ANNUNCIATION_FAILED", outcome="FAILED",
                    activity_class="ORCHESTRATION",
                    metadata_redacted={
                        "fingerprint": entry["fingerprint"],
                        "occurrence_id": entry["occurrence_id"],
                        "attempt": entry["attempt"],
                    })
        except Exception:  # noqa: BLE001 - fail-closed handling below
            if send_started:
                # Ambiguous: the notification may have gone out. Remain
                # ATTEMPTING - delivery happening twice is worse than stale
                # bookkeeping, and the converge step repairs a durably
                # known outcome on a later pass.
                continue
            # The failure happened BEFORE notifier.send was reached (the
            # ORPHAN_DETECTED append), so no external effect exists and the
            # occurrence may safely become retryable. If the append actually
            # landed before raising, its existence guard prevents a
            # duplicate on the retry. If this reset itself fails, the entry
            # simply stays ATTEMPTING - fail closed against resend.
            try:
                with store.transaction() as doc:
                    stored = doc.get("orphan_annunciations", {}).get(
                        entry["fingerprint"])
                    if stored and stored.get("status") == "ATTEMPTING" and \
                            stored.get("occurrence_id") == entry["occurrence_id"]:
                        stored["status"] = "OBSERVED"
            except Exception:  # noqa: BLE001
                pass
            continue


def record_resource_sample(cfg, ledger, last_monotonic, *, store=None):
    """C-08a: append one RESOURCE_SAMPLE per governed interval.

    Cadence (governed 2026-09-28): sample immediately when the loop starts
    (last_monotonic None - a Watchdog restart therefore takes one extra
    immediate sample, which is preferred over a fresh blind interval),
    then no more often than every metrics.SAMPLE_INTERVAL_SECONDS. The
    returned value is the caller's next cadence point. A failed sampling
    attempt ALSO advances the cadence point, so a persistently broken
    reader produces one durable METRICS_SAMPLE_ERROR per interval, not an
    error storm on every 30-second pass. No state document yet means the
    run's evidence sources do not exist: defer without advancing (same
    early-return convention as the other Watchdog observers) rather than
    fabricating a sample with nothing behind it. Measurement only - no
    freeze, no threshold, no notification, no state mutation.
    """
    now = time.monotonic()
    if last_monotonic is not None and \
            now - last_monotonic < metrics.SAMPLE_INTERVAL_SECONDS:
        return last_monotonic
    store = store or state_mod.Store(tz=cfg.timezone)
    if not store.exists():
        return last_monotonic
    try:
        payload = metrics.sample(store.read())
        ledger.append("RESOURCE_SAMPLE", outcome="OBSERVED",
                      activity_class="OBSERVATION",
                      metadata_redacted=payload)
    except Exception:  # noqa: BLE001 - measurement must never take down the
        # watchdog loop, and nothing exception-derived may persist: the
        # fixed finite pair below is the entire durable record (same rule
        # as RECONCILE_ERROR and ORPHAN_ANNUNCIATION_ERROR).
        try:
            ledger.append("METRICS_SAMPLE_ERROR", outcome="ERROR",
                          activity_class="OBSERVATION",
                          metadata_redacted={"phase": "RUNTIME_SAMPLE",
                                             "error_code": "SAMPLE_FAILED"})
        except Exception:  # noqa: BLE001 - the durable sink itself is down,
            # so this interval's failure genuinely cannot be recorded, and
            # nothing here may pretend otherwise or invent another sink.
            # Measurement-only degradation: the watchdog's independent
            # liveness duty continues regardless.
            pass
    return now


def main() -> int:
    cfg = config.load()
    config.ensure_runtime_dirs()
    config.WATCHDOG_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    notifier = notify.Notifier(cfg.experiment_id)

    ledger.append("WATCHDOG_STARTED", outcome="RUNNING", activity_class="ORCHESTRATION",
                  metadata_redacted={"pid": os.getpid()})

    restarts: list[float] = []
    last_sample = None  # C-08a cadence point; None samples immediately

    while True:
        proc.reap_children()
        last_sample = record_resource_sample(cfg, ledger, last_sample)
        reconcile_worker_state(cfg, ledger, notifier)
        annunciate_orphans(cfg, ledger, notifier)
        pid = supervisor_pid()
        healthy = alive(pid) and heartbeat_fresh(cfg.timezone, cfg.heartbeat_stale_seconds)

        if not healthy:
            now = time.monotonic()
            restarts = [t for t in restarts if now - t < CRASH_LOOP_WINDOW_SECONDS]
            if len(restarts) >= CRASH_LOOP_LIMIT:
                escalate_crash_loop(cfg, ledger, notifier, len(restarts))
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

            escalate_restart_failed(cfg, ledger, notifier, incumbent)
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
