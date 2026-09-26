"""Control-plane command line.

  ctl init        create runtime state and load the locked task graph
  ctl preflight   run the deployment gates
  ctl start       record T+00 and hand implementation to the agent organisation
  ctl status      current experiment state
  ctl report      metrics summary from the raw ledger
  ctl verify-manifest  C-13: verify the frozen T+00 manifest has not drifted
  ctl freeze      force the T+24 freeze path
  ctl human-list        C-08b.1: list human interventions
  ctl human-acknowledge C-08b.1: acknowledge a human intervention
  ctl human-resolve     C-08b.2: record a human's resolution and execute its
                        governed condition-specific effect where one exists
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path

from . import (
    budget,
    clock,
    config,
    gh,
    intervention,
    ledger as ledger_mod,
    manifest,
    migration_lock,
    notify,
    preflight as preflight_mod,
    providers,
    redact,
    state as state_mod,
    supervisor as supervisor_mod,
    telemetry,
    workers,
)

PREFLIGHT_MAX_AGE_MINUTES = 90


def cmd_init(args) -> int:
    cfg = config.load()
    config.ensure_runtime_dirs()
    store = state_mod.Store(tz=cfg.timezone)
    if store.exists() and not args.force:
        print("state already initialised; pass --force to reset (pre-T+00 only)")
        return 1
    if store.exists():
        doc = store.read()
        if doc.get("started_at"):
            print("refusing to reset state after T+00: the protocol is frozen")
            return 2

    store.initialise(cfg.experiment_id, cfg.protocol_version)
    tasks = json.loads((config.REPO_ROOT / "config" / "tasks.json").read_text("utf-8"))
    with store.transaction() as doc:
        providers.ensure(doc)
        budget.ensure(doc, cfg.budget_usd)
        migration_lock.ensure(doc)
        for spec in tasks["tasks"]:
            task = state_mod.add_task(doc, spec["id"], spec["title"], spec["depends_on"],
                                      spec.get("kind", "feature"),
                                      spec.get("schema_changing", False), cfg.timezone)
            task["body"] = spec.get("body", "")

    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    ledger.append("CONTROL_PLANE_INITIALISED", outcome="OK",
                  activity_class="ORCHESTRATION",
                  metadata_redacted={"tasks": [t["id"] for t in tasks["tasks"]],
                                     "budget_usd": cfg.budget_usd})
    print(f"initialised {len(tasks['tasks'])} tasks, budget ${cfg.budget_usd:.2f}, "
          f"providers AVAILABLE, migration lock FREE")
    return 0


def cmd_preflight(args) -> int:
    runner = preflight_mod.Preflight(skip=tuple(args.skip or ()))
    summary = runner.run(only=tuple(args.only or ()))
    failed = [g["gate"] for g in summary["gates"] if not g["ok"]]
    print()
    if summary["all_passed"]:
        print("PREFLIGHT PASSED — all required gates green")
        return 0
    if summary.get("gates_not_run"):
        print(f"PREFLIGHT INCOMPLETE — not yet run: "
              f"{', '.join(summary['gates_not_run'])}")
    if failed:
        print(f"PREFLIGHT FAILED — {len(failed)} gate(s): {', '.join(failed)}")
    return 1


def cmd_start(args) -> int:
    cfg = config.load()
    store = state_mod.Store(tz=cfg.timezone)
    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    notifier = notify.Notifier(cfg.experiment_id)
    tele = telemetry.Telemetry(experiment_id=cfg.experiment_id)

    if not config.PREFLIGHT_PATH.exists():
        print("refusing to start: no preflight record. Run `ctl preflight` first.")
        return 1
    summary = json.loads(config.PREFLIGHT_PATH.read_text(encoding="utf-8"))
    recorded = {g["gate"] for g in summary.get("gates", [])}
    never_run = [name for name, _ in preflight_mod.Preflight.GATES if name not in recorded]
    if never_run:
        print(f"refusing to start: these gates have never run: {', '.join(never_run)}")
        return 1
    if not summary.get("all_passed"):
        failed = [g["gate"] for g in summary["gates"] if not g["ok"]]
        print(f"refusing to start: preflight gates failed: {', '.join(failed)}")
        return 1
    age_minutes = (clock.now(cfg.timezone) - clock.parse(summary["at"])).total_seconds() / 60
    if age_minutes > PREFLIGHT_MAX_AGE_MINUTES and not args.force:
        print(f"refusing to start: preflight is {age_minutes:.0f} minutes old; re-run it")
        return 1

    with store.transaction() as doc:
        if doc.get("started_at"):
            print(f"already started at {doc['started_at']}; the protocol is frozen")
            return 1

        head = gh.git(["rev-parse", "HEAD"], str(config.REPO_ROOT)).stdout
        dirty = gh.git(["status", "--porcelain"], str(config.REPO_ROOT)).stdout
        if dirty.strip():
            print("refusing to start: working tree is not clean")
            return 1

        started_at = clock.iso(clock.now(cfg.timezone))
        doc["started_at"] = started_at
        doc["baseline_sha"] = head

        baseline = {
            "experiment_id": cfg.experiment_id,
            "protocol_version": cfg.protocol_version,
            "t_zero": started_at,
            "timezone": cfg.timezone,
            "duration_hours": cfg.duration_hours,
            "baseline_sha": head,
            "github_repo": cfg.github_repo,
            "main_branch": cfg.main_branch,
            "required_checks": list(cfg.required_checks),
            "tool_versions": preflight_mod.tool_versions(),
            "providers_and_models": manifest.providers_and_models(cfg),
            **manifest.frozen_content_fields(),
            "budget": {"total_usd": cfg.budget_usd,
                       "configured_by": cfg.budget_configured_by,
                       "scope": "incremental metered OpenRouter spend only"},
            "concurrency": {"max_builders": cfg.max_builders,
                            "max_fixers": cfg.max_fixers,
                            "max_reviewers": cfg.max_reviewers,
                            "max_observers": cfg.max_observers,
                            "max_repair_cycles": cfg.max_repair_cycles},
            "human_bootstrap_actions": json.loads(
                (config.REPO_ROOT / "config" / "human-actions.json").read_text("utf-8")
            )["actions"],
            "preflight": {"at": summary["at"],
                          "gates": [{"gate": g["gate"], "ok": g["ok"]}
                                    for g in summary["gates"]]},
            "environmental_limitations": [
                "A watchdog on the experiment host cannot detect or notify during a "
                "complete host or WSL shutdown; recovery is measured from durable Git "
                "and ledger state once the runtime returns."
            ],
        }
        manifest.atomic_write_json(config.BASELINE_PATH, baseline)

        ledger.append("EXPERIMENT_STARTED", outcome="T+00",
                      activity_class="ORCHESTRATION", state_after="RUNNING",
                      metadata_redacted=baseline)
        tele.span("experiment.started",
                  {"run002.t_zero": started_at, "run002.baseline_sha": head,
                   "run002.protocol": cfg.protocol_version})

        for task in doc["tasks"].values():
            if state_mod.dependencies_met(doc, task) and task["state"] == "QUEUED":
                state_mod.transition(doc, task["id"], "READY", "dependency-ready at T+00",
                                     cfg.timezone)

        ready = [t["id"] for t in doc["tasks"].values() if t["state"] == "READY"]

    notifier.send(
        notify.INFO, "EXPERIMENT_STARTED — T+00 recorded",
        f"Run 002, Protocol {cfg.protocol_version}. Baseline {head[:12]}. "
        f"24-hour clock started {started_at}. "
        f"Dependency-ready now: {', '.join(ready) or 'none'}. "
        f"Budget ceiling ${cfg.budget_usd:.2f} metered.",
        clock_label="T+00:00",
    )
    print(f"T+00 recorded at {started_at}; baseline {head[:12]}; ready: {', '.join(ready)}")
    return 0


def cmd_status(args) -> int:
    cfg = config.load()
    store = state_mod.Store(tz=cfg.timezone)
    doc = store.read()
    cs = None
    if doc.get("started_at"):
        cs = clock.ClockState(clock.parse(doc["started_at"]), clock.now(cfg.timezone),
                              cfg.duration_hours)
    payload = {
        "experiment_id": doc["experiment_id"],
        "protocol": doc["protocol_version"],
        "started_at": doc.get("started_at"),
        "frozen_at": doc.get("frozen_at"),
        "clock": {"label": cs.label(), "phase": cs.phase,
                  "remaining_hours": round(cs.remaining_hours, 2)} if cs else "PRE_T0",
        "tasks": {t["id"]: t["state"] for t in doc["tasks"].values()},
        "review_queue_depth": state_mod.review_queue_depth(doc),
        "builder_limit": state_mod.builder_limit(
            state_mod.review_queue_depth(doc), cfg.max_builders),
        "providers": {p: r["state"] for p, r in doc["providers"].items()},
        "budget": budget.summary(doc),
        "migration_lock": doc["migration_lock"],
        "counters": doc["counters"],
        "workers": list(doc["workers"]),
        "human_interventions_open": intervention.simultaneous_open_count(doc),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_report(args) -> int:
    cfg = config.load()
    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    events = list(ledger.read())
    by_type: dict[str, int] = {}
    by_activity: dict[str, int] = {}
    actual = 0.0
    estimated = 0.0
    for event in events:
        by_type[event.get("event_type", "?")] = by_type.get(event.get("event_type", "?"), 0) + 1
        activity = event.get("activity_class")
        if activity:
            by_activity[activity] = by_activity.get(activity, 0) + 1
        if event.get("actual_cost_usd"):
            actual += float(event["actual_cost_usd"])
        if event.get("estimated_cost_usd"):
            estimated += float(event["estimated_cost_usd"])
    print(json.dumps({
        "ledger_events": len(events),
        "by_event_type": dict(sorted(by_type.items())),
        "by_activity_class": dict(sorted(by_activity.items())),
        "metered_actual_cost_usd": round(actual, 6),
        "estimated_cost_usd_label_only": round(estimated, 6),
        "note": "Subscription usage for Claude, Codex and Grok is not a metered dollar "
                "cost and is never inferred as one.",
    }, indent=2))
    return 0


def cmd_verify_manifest(args) -> int:
    """C-13 frozen validation: does every frozen input still recompute to
    what `ctl start` recorded? Only meaningful after T+00 - refuses to run
    against a baseline that doesn't exist yet."""
    cfg = config.load()
    if not config.BASELINE_PATH.exists():
        print("refusing to verify: no baseline artifact yet (T+00 has not occurred).")
        return 1
    baseline = json.loads(config.BASELINE_PATH.read_text(encoding="utf-8"))
    result = manifest.frozen_check(baseline, cfg)
    print(result.detail)
    return 0 if result.ok else 1


def cmd_freeze(args) -> int:
    supervisor = supervisor_mod.Supervisor()
    with supervisor.store.transaction() as doc:
        supervisor.freeze(doc)
    print("EXPERIMENT_FROZEN emitted")
    return 0


def cmd_resurrect(args) -> int:
    result = workers.resurrect(dry_run=args.dry_run)
    print(result.stdout or result.stderr)
    return 0 if result.ok else 1


def cmd_supervisor(args) -> int:
    return supervisor_mod.main()


def _ledger_has_event(ledger, event_type: str, intervention_id: str) -> bool:
    """Whether the ledger already carries this event for this intervention.

    The ledger is append-only evidence, so its absence has to be asked about
    rather than inferred from state: a state document can commit and the
    following ledger append still fail. Matching is on
    metadata_redacted["intervention_id"], the one field every human-intervention
    event carries.
    """
    for event in ledger.events(event_type):
        metadata = event.get("metadata_redacted") or {}
        if metadata.get("intervention_id") == intervention_id:
            return True
    return False


def cmd_human_list(args) -> int:
    """C-08b.1: read-only listing. Never opens a transaction and never logs.

    active_human_minutes is added here, at the reporting boundary, exactly as
    control/intervention.py intends - the durable record keeps whole seconds.
    """
    cfg = config.load()
    store = state_mod.Store(tz=cfg.timezone)
    doc = store.read()

    records = list(doc.get("interventions", {}).values())
    if args.status != "ALL":
        records = [r for r in records if r.get("status") == args.status]
    records.sort(key=lambda r: (r.get("requested_at") or "", r.get("id") or ""))

    print(json.dumps({
        "status_filter": args.status,
        "count": len(records),
        "interventions": [
            dict(r, active_human_minutes=intervention.active_human_minutes(r))
            for r in records
        ],
    }, indent=2))
    return 0


def cmd_human_acknowledge(args) -> int:
    """C-08b.1: record a human's acknowledgement.

    State first, ledger second, deliberately. The durable record commits inside
    the transaction; the append happens after it closes. That ordering makes the
    ledger the only part that can lag, and a lag is repairable - rerunning the
    same command appends the missing event and nothing else.
    """
    cfg = config.load()
    # Secret rejection runs before the transaction opens, so a refused command
    # cannot have touched state or the ledger. It runs after config.load()
    # deliberately: that call loads the operator secrets file into the
    # environment, which is what lets contains_secret() match known values and
    # not merely secret-shaped patterns. Only operator-supplied --by is checked
    # - getpass.getuser() is the OS's answer, not free text, and is never
    # rejected for merely matching a broad pattern.
    if args.by is not None and redact.contains_secret(args.by):
        print(f"refusing to acknowledge {args.intervention_id}: --by appears to "
              f"contain a secret; the supplied value is deliberately not shown. "
              f"If it is a live credential, rotate it and revoke the old one "
              f"server-side.")
        return 1

    store = state_mod.Store(tz=cfg.timezone)

    with store.transaction() as doc:
        record = doc.get("interventions", {}).get(args.intervention_id)
        if record is None:
            print(f"unknown intervention {args.intervention_id}")
            return 1

        status = record.get("status")
        if status == "OPEN":
            actor = args.by or getpass.getuser()
            try:
                intervention.acknowledge(doc, args.intervention_id, by=actor,
                                         tz=cfg.timezone)
            except intervention.InterventionError as exc:
                print(f"refusing to acknowledge {args.intervention_id}: {exc}")
                return 1
        elif status == "ACKNOWLEDGED":
            # Idempotent repair. The stored actor is authoritative: an omitted
            # --by is never filled in from whoever happens to be rerunning this,
            # or a repair would quietly rewrite who actually responded.
            if args.by is not None and args.by != record["acknowledged_by"]:
                print(f"refusing to acknowledge {args.intervention_id}: already "
                      f"acknowledged by {record['acknowledged_by']}, not {args.by}")
                return 1
        elif status == "RESOLVED":
            print(f"refusing to acknowledge {args.intervention_id}: already RESOLVED")
            return 1
        else:
            print(f"refusing to acknowledge {args.intervention_id}: unrecognised "
                  f"status {status!r}")
            return 1

        committed = dict(record)

    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)
    if _ledger_has_event(ledger, "HUMAN_INTERVENTION_ACKNOWLEDGED", committed["id"]):
        print(f"{committed['id']} acknowledged by {committed['acknowledged_by']} at "
              f"{committed['acknowledged_at']} (ledger evidence already present)")
        return 0

    try:
        ledger.append(
            "HUMAN_INTERVENTION_ACKNOWLEDGED",
            task_id=committed["task_id"],
            outcome="ACKNOWLEDGED",
            activity_class="ESCALATION",
            human_intervention=True,
            metadata_redacted={
                "intervention_id": committed["id"],
                "intervention_type": committed["type"],
                "scope": committed["scope"],
                "condition_code": committed["condition_code"],
                "requested_at": committed["requested_at"],
                "acknowledged_at": committed["acknowledged_at"],
                "acknowledged_by": committed["acknowledged_by"],
            },
        )
    except OSError as exc:
        print(f"STATE COMMITTED BUT LEDGER EVIDENCE FAILED for {committed['id']}: {exc}\n"
              f"The acknowledgement is durable in state; the ledger event is missing. "
              f"Rerun this same command to append it.")
        return 1

    print(f"{committed['id']} acknowledged by {committed['acknowledged_by']} at "
          f"{committed['acknowledged_at']}")
    return 0


# ------------------------------------------------------- C-08b.2 resolution
#
# The governed (condition_code, outcome) pairs. Fail-closed: a condition code
# this table does not know permits NO_ACTION only, so an effectful outcome can
# never resolve as a silent no-op. FREEZE appears nowhere - the freeze
# side-effect bundle stays with `ctl freeze`, never inside a resolve
# transaction. RESUME appears nowhere before C-14.3.

CONDITION_OUTCOMES: dict[str, frozenset[str]] = {
    "repair_cycle_limit": frozenset({"RETRY", "FAIL", "NO_ACTION"}),
    "dispatch_failure_limit": frozenset({"RETRY", "FAIL", "NO_ACTION"}),
    "builder_attempts_exhausted": frozenset({"RETRY", "NO_ACTION"}),
    "fixer_failed": frozenset({"RETRY", "FAIL", "NO_ACTION"}),
    "review_unusable": frozenset({"RETRY", "FAIL", "NO_ACTION"}),
    "p0_finding": frozenset({"RETRY", "FAIL", "NO_ACTION"}),
    "red_guardrail": frozenset({"CLEAR_GUARDRAIL", "NO_ACTION"}),
    "budget_hard_stop": frozenset({"NO_ACTION"}),
    "supervisor_crash_loop": frozenset({"NO_ACTION"}),
    "supervisor_restart_failed": frozenset({"NO_ACTION"}),
    "merge_invariant_violation": frozenset({"FAIL", "NO_ACTION"}),
    "worker_state_invariant": frozenset({"FAIL", "NO_ACTION"}),
}

UNKNOWN_CONDITION_OUTCOMES = frozenset({"NO_ACTION"})


class ResolutionRefused(Exception):
    """Raised inside the resolve transaction to refuse a resolution whole.

    Raising - rather than returning - is what aborts Store.transaction's
    write, so a refusal can never persist a half-applied resolution: the
    lifecycle mutation and any state effect roll back together.
    """


def _pr_record(doc: dict, task: dict, task_id: str) -> dict:
    pr = task.get("pr")
    record = (doc.get("prs") or {}).get(str(pr)) if pr is not None else None
    if record is None:
        raise ResolutionRefused(f"{task_id} has no pull request record to act on")
    return record


def _fail_task(doc: dict, cfg, task: dict, record: dict) -> None:
    """The one human FAIL executor. FROZEN exits through the documented
    FROZEN -> HUMAN_REQUIRED -> FAILED path; delivered work is never falsified;
    a lock the task owns is released so the FAIL cannot strand it.

    An executed lock release is recorded as the finite identifier
    MIGRATION_LOCK_RELEASED in the intervention record's executed_effects,
    inside this same transaction. That intervention-owned evidence - not the
    lock's mutable last_release_reason, which any later legitimate lock
    activity overwrites - is what the ledger-repair path derives a missed
    MIGRATION_LOCK_RELEASED append from (see cmd_human_resolve)."""
    task_id = task["id"]
    current = task["state"]
    if current in ("MERGED", "COMPLETE"):
        raise ResolutionRefused(
            f"{task_id} is {current}; marking delivered work FAILED would "
            f"falsify history")
    if current == "FAILED":
        raise ResolutionRefused(f"{task_id} is already FAILED")
    reason = f"human FAIL resolution of {record['id']}"
    if current == "FROZEN":
        state_mod.transition(doc, task_id, "HUMAN_REQUIRED", reason, cfg.timezone)
    state_mod.transition(doc, task_id, "FAILED", reason, cfg.timezone)
    if migration_lock.owner(doc) == task_id:
        migration_lock.release(doc, task_id, reason)
        record.setdefault("executed_effects", []).append("MIGRATION_LOCK_RELEASED")


def _apply_resolution_effects(doc: dict, cfg, record: dict,
                              outcome: str) -> None:
    """Execute the governed effect for an already-validated pair. Explicit
    branches, deliberately - C-08b.2 is intervention integration, not a
    resolution framework. Raises ResolutionRefused when the current task state
    cannot legally honour the outcome, aborting the whole transaction."""
    if outcome == "NO_ACTION":
        # For worker_state_invariant this is the governed meaning: the human
        # deliberately leaves the task FROZEN for the remainder of this run.
        return
    if outcome == "CLEAR_GUARDRAIL":  # governed only for red_guardrail
        doc["red_guardrail"] = None
        return
    condition = record["condition_code"]
    task_id = record["task_id"]
    iid = record["id"]
    task = (doc.get("tasks") or {}).get(task_id)
    if task is None:
        raise ResolutionRefused(f"task {task_id!r} not found in state")
    try:
        if outcome == "FAIL":
            _fail_task(doc, cfg, task, record)
            return
        # outcome == "RETRY"
        if condition == "repair_cycle_limit":
            # Exactly one additional repair attempt: the dispatch guard is
            # `>= max_repair_cycles` and each dispatch increments by one, so
            # max - 1 buys one dispatch. Zero would buy a full new allowance,
            # which no governance supports. dispatch_failures stays untouched.
            state_mod.transition(doc, task_id, "FIX_REQUIRED",
                                 f"human RETRY of {iid}: one additional "
                                 f"repair attempt", cfg.timezone)
            _pr_record(doc, task, task_id)["repair_cycles"] = \
                cfg.max_repair_cycles - 1
        elif condition == "dispatch_failure_limit":
            state_mod.transition(doc, task_id, "PR_OPEN",
                                 f"human RETRY of {iid}: apparatus repaired",
                                 cfg.timezone)
            _pr_record(doc, task, task_id)["dispatch_failures"] = 0
        elif condition == "builder_attempts_exhausted":
            # The documented exception-state exit: FAILED -> HUMAN_REQUIRED
            # -> READY. `attempts` stays at the limit, so one more builder
            # failure re-escalates rather than looping.
            reason = f"human RETRY of {iid}: one additional build attempt"
            state_mod.transition(doc, task_id, "HUMAN_REQUIRED", reason,
                                 cfg.timezone)
            state_mod.transition(doc, task_id, "READY", reason, cfg.timezone)
        elif condition == "fixer_failed":
            state_mod.transition(doc, task_id, "FIX_REQUIRED",
                                 f"human RETRY of {iid}: redispatch the fixer",
                                 cfg.timezone)
        elif condition in ("review_unusable", "p0_finding"):
            # Only the stale verdict is cleared - enough for routing to
            # dispatch a genuinely fresh review; counters stay untouched.
            state_mod.transition(doc, task_id, "PR_OPEN",
                                 f"human RETRY of {iid}: fresh review",
                                 cfg.timezone)
            pr = _pr_record(doc, task, task_id)
            pr["review_verdict"] = None
            pr["approval_current"] = False
        else:
            raise ResolutionRefused(
                f"no RETRY effect is defined for condition {condition}")
    except state_mod.TransitionError as exc:
        raise ResolutionRefused(str(exc)) from exc


def cmd_human_resolve(args) -> int:
    """Record a human's resolution and execute its governed effect (C-08b.2).

    A RESOLVED intervention is not an error here, but it is only ever a
    ledger-repair retry: every supplied field must match what is already stored,
    the record is never touched again, and no state effect is ever re-executed.
    That is what makes rerunning the exact same command safe after a ledger
    failure, while a command differing in any way is refused rather than
    quietly reinterpreted.

    The (condition_code, outcome) pair is validated against CONDITION_OUTCOMES
    before the lifecycle mutation, and every refusal - including an effect the
    current task state cannot legally honour - aborts the transaction whole.
    """
    cfg = config.load()
    # Before the transaction, for the same reason as cmd_human_acknowledge: a
    # refused command must leave neither state nor ledger touched. On a repair
    # retry this also runs first, so a secret-shaped --by is rejected here
    # rather than reaching the stored-actor equality check below.
    if args.by is not None and redact.contains_secret(args.by):
        print(f"refusing to resolve {args.intervention_id}: --by appears to "
              f"contain a secret; the supplied value is deliberately not shown. "
              f"If it is a live credential, rotate it and revoke the old one "
              f"server-side.")
        return 1
    if args.note is not None and redact.contains_secret(args.note):
        print(f"refusing to resolve {args.intervention_id}: --note appears to "
              f"contain a secret; the matched value is deliberately not shown. "
              f"Remove it and rerun. If it is a live credential, rotate it and "
              f"revoke the old one server-side.")
        return 1

    store = state_mod.Store(tz=cfg.timezone)

    try:
        return _resolve_in_transaction(args, cfg, store)
    except ResolutionRefused as refusal:
        print(f"refusing to resolve {args.intervention_id}: {refusal}")
        return 1


def _resolve_in_transaction(args, cfg, store) -> int:
    with store.transaction() as doc:
        record = doc.get("interventions", {}).get(args.intervention_id)
        if record is None:
            print(f"unknown intervention {args.intervention_id}")
            return 1

        status = record.get("status")
        if status == "OPEN":
            print(f"refusing to resolve {args.intervention_id}: it must be "
                  f"acknowledged before it can be resolved")
            return 1
        elif status == "ACKNOWLEDGED":
            allowed = CONDITION_OUTCOMES.get(record["condition_code"],
                                             UNKNOWN_CONDITION_OUTCOMES)
            if args.outcome not in allowed:
                raise ResolutionRefused(
                    f"outcome {args.outcome} is not governed for condition "
                    f"{record['condition_code']}; allowed: "
                    f"{', '.join(sorted(allowed))}")
            actor = args.by or getpass.getuser()
            try:
                intervention.resolve(doc, args.intervention_id, outcome=args.outcome,
                                     by=actor, tz=cfg.timezone, note=args.note)
            except intervention.InterventionError as exc:
                raise ResolutionRefused(str(exc)) from exc
            # Effects run after the lifecycle mutation on purpose: both live in
            # this one transaction, and any ResolutionRefused from here rolls
            # the resolution back with the effect - never one without the other.
            _apply_resolution_effects(doc, cfg, record, args.outcome)
        elif status == "RESOLVED":
            if args.outcome != record["resolution"]:
                print(f"refusing to resolve {args.intervention_id}: already RESOLVED "
                      f"as {record['resolution']}, not {args.outcome}")
                return 1
            if args.note is not None and args.note != record["resolution_note"]:
                print(f"refusing to resolve {args.intervention_id}: the supplied note "
                      f"differs from the stored one")
                return 1
            if args.by is not None and args.by != record["resolved_by"]:
                print(f"refusing to resolve {args.intervention_id}: already resolved by "
                      f"{record['resolved_by']}, not {args.by}")
                return 1
        else:
            print(f"refusing to resolve {args.intervention_id}: unrecognised status "
                  f"{status!r}")
            return 1

        committed = dict(record)

    ledger = ledger_mod.Ledger(tz=cfg.timezone, experiment_id=cfg.experiment_id)

    # An executed lock release is recorded in the intervention's own
    # executed_effects, committed atomically with the resolution - so it
    # survives any later, unrelated migration-lock activity. Deriving the
    # append from it here - on the fresh path AND the ledger-repair retry -
    # means a failed MIGRATION_LOCK_RELEASED append is restored by rerunning
    # the identical command, without re-executing any state effect and without
    # duplicating the event when it already landed. Records predating the
    # field (or without an executed effect) simply derive nothing.
    if ("MIGRATION_LOCK_RELEASED" in (committed.get("executed_effects") or [])
            and not _ledger_has_event(ledger, "MIGRATION_LOCK_RELEASED",
                                      committed["id"])):
        try:
            ledger.append(
                "MIGRATION_LOCK_RELEASED", task_id=committed["task_id"],
                outcome="FREE", activity_class="ORCHESTRATION",
                metadata_redacted={"intervention_id": committed["id"],
                                   "released_by": "human_fail_resolution"})
        except OSError as exc:
            print(f"STATE COMMITTED BUT LEDGER EVIDENCE FAILED for "
                  f"{committed['id']}: {exc}\n"
                  f"The resolution is durable in state; ledger evidence is "
                  f"missing. Rerun this same resolution command to append it.")
            return 1

    if _ledger_has_event(ledger, "HUMAN_INTERVENTION_RESOLVED", committed["id"]):
        print(f"{committed['id']} already resolved as {committed['resolution']} at "
              f"{committed['resolved_at']} (ledger evidence already present)")
        return 0

    try:
        ledger.append(
            "HUMAN_INTERVENTION_RESOLVED",
            task_id=committed["task_id"],
            outcome=committed["resolution"],
            activity_class="ESCALATION",
            human_intervention=True,
            metadata_redacted={
                "intervention_id": committed["id"],
                "intervention_type": committed["type"],
                "scope": committed["scope"],
                "condition_code": committed["condition_code"],
                "requested_at": committed["requested_at"],
                "acknowledged_at": committed["acknowledged_at"],
                "acknowledged_by": committed["acknowledged_by"],
                "resolved_at": committed["resolved_at"],
                "resolved_by": committed["resolved_by"],
                "resolution": committed["resolution"],
                "active_human_seconds": committed["active_human_seconds"],
                "active_human_minutes": intervention.active_human_minutes(committed),
            },
        )
    except OSError as exc:
        print(f"STATE COMMITTED BUT LEDGER EVIDENCE FAILED for {committed['id']}: {exc}\n"
              f"The resolution is durable in state; the ledger event is missing. "
              f"Rerun this same resolution command to append it.")
        return 1

    print(f"{committed['id']} resolved as {committed['resolution']} by "
          f"{committed['resolved_by']} at {committed['resolved_at']} "
          f"({committed['active_human_seconds']}s active human time)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctl", description="Run 002 control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialise runtime state and task graph")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_pre = sub.add_parser("preflight", help="run deployment gates")
    p_pre.add_argument("--only", nargs="*", help="run only these gates")
    p_pre.add_argument("--skip", nargs="*", help="mark these gates as skipped (fails)")
    p_pre.set_defaults(func=cmd_preflight)

    p_start = sub.add_parser("start", help="record T+00 and emit EXPERIMENT_STARTED")
    p_start.add_argument("--force", action="store_true",
                         help="accept an older preflight record")
    p_start.set_defaults(func=cmd_start)

    sub.add_parser("status", help="print current state").set_defaults(func=cmd_status)
    sub.add_parser("report", help="ledger metrics summary").set_defaults(func=cmd_report)
    sub.add_parser("verify-manifest",
                   help="C-13: verify the frozen T+00 manifest has not drifted"
                   ).set_defaults(func=cmd_verify_manifest)
    sub.add_parser("freeze", help="force the freeze path").set_defaults(func=cmd_freeze)
    sub.add_parser("supervisor", help="run the supervisor loop in the foreground"
                   ).set_defaults(func=cmd_supervisor)

    p_res = sub.add_parser("resurrect", help="restore worker windows after a crash")
    p_res.add_argument("--dry-run", action="store_true")
    p_res.set_defaults(func=cmd_resurrect)

    p_hlist = sub.add_parser("human-list", help="C-08b.1: list human interventions")
    p_hlist.add_argument("--status", default="OPEN",
                         choices=["OPEN", "ACKNOWLEDGED", "RESOLVED", "ALL"],
                         help="lifecycle status to list (default: OPEN)")
    p_hlist.set_defaults(func=cmd_human_list)

    p_hack = sub.add_parser("human-acknowledge",
                            help="C-08b.1: acknowledge a human intervention")
    p_hack.add_argument("intervention_id")
    p_hack.add_argument("--by", default=None,
                        help="acknowledging actor; defaults to the invoking user on a "
                             "first acknowledgement and is never inferred on a repair")
    p_hack.set_defaults(func=cmd_human_acknowledge)

    p_hres = sub.add_parser("human-resolve",
                            help="C-08b.2: record a human's resolution and execute "
                                 "its governed effect where one exists")
    p_hres.add_argument("intervention_id")
    p_hres.add_argument("--outcome", required=True,
                        choices=sorted(intervention.RESOLUTION_OUTCOMES))
    p_hres.add_argument("--note", default=None)
    p_hres.add_argument("--by", default=None,
                        help="resolving actor; defaults to the invoking user on a first "
                             "resolution and is never inferred on a repair")
    p_hres.set_defaults(func=cmd_human_resolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
