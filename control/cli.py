"""Control-plane command line.

  ctl init        create runtime state and load the locked task graph
  ctl preflight   run the deployment gates
  ctl start       record T+00 and hand implementation to the agent organisation
  ctl status      current experiment state
  ctl report      metrics summary from the raw ledger
  ctl verify-manifest  C-13: verify the frozen T+00 manifest has not drifted
  ctl freeze      force the T+24 freeze path
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import (
    budget,
    clock,
    config,
    gh,
    ledger as ledger_mod,
    manifest,
    migration_lock,
    notify,
    preflight as preflight_mod,
    providers,
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
