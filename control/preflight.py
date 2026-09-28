"""Deployment preflight. Every gate in experiment/LAUNCH-CHECKLIST.md.

Each gate returns a Gate result; nothing here prints or returns a secret value,
only whether one is configured. The run refuses to reach T+00 unless every
required gate passes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    budget,
    clock,
    config,
    gh,
    hostcheck,
    jev,
    ledger as ledger_mod,
    manifest,
    migration_lock,
    notify,
    prompts,
    providers,
    redact,
    routing,
    state as state_mod,
    supabase_health,
    task_graph,
    telemetry,
    workers,
)


@dataclass
class Gate:
    name: str
    required: bool
    ok: bool
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"gate": self.name, "required": self.required, "ok": self.ok,
                "detail": self.detail[:800], "evidence": redact.scrub(self.evidence)}


SPEC_FILES = (
    "AGENTS.md", "BOOTSTRAP.md", "PROTOCOL-VERSION.md",
    "agents/SUPERVISOR.md", "agents/BUILDER.md", "agents/FIXER.md",
    "agents/REVIEWER.md", "agents/OBSERVER.md", "agents/WATCHDOG.md",
    "guardrails/GUARDRAILS.md", "product/MVP.md", "product/ARCHITECTURE.md",
    "product/DESIGN-SYSTEM.md", "product/ACCESSIBILITY.md", "product/PRIVACY.md",
    "product/AI.md", "product/FUTURE.md", "tasks/TASKS.md", "skills/README.md",
    "experiment/EXPERIMENT.md", "experiment/BUDGET.md", "experiment/METRICS.md",
    "experiment/LEDGER-SCHEMA.md", "experiment/FREEZE-PROTOCOL.md",
    "experiment/LAUNCH-CHECKLIST.md", "experiment/TIMELINE.md",
    "experiment/RESEARCH-QUESTIONS.md", "experiment/PREFLIGHT-FINDINGS.md",
)


class Preflight:
    def __init__(self, cfg=None, skip: tuple[str, ...] = ()) -> None:
        self.cfg = cfg or config.load()
        self.tz = self.cfg.timezone
        self.ledger = ledger_mod.Ledger(tz=self.tz, experiment_id=self.cfg.experiment_id)
        self.notifier = notify.Notifier(self.cfg.experiment_id)
        self.telemetry = telemetry.Telemetry(experiment_id=self.cfg.experiment_id)
        self.skip = set(skip)
        self.results: list[Gate] = []

    # ------------------------------------------------------------------ gates

    def gate_protocol(self) -> Gate:
        version = (config.REPO_ROOT / "PROTOCOL-VERSION.md").read_text(encoding="utf-8")
        missing = [name for name in SPEC_FILES
                   if not (config.REPO_ROOT / name).exists()]
        ok = "v2.0" in version and not missing
        return Gate("protocol_present", True, ok,
                    "Protocol v2.0 and all specification files present" if ok
                    else f"missing: {', '.join(missing) or 'version marker'}",
                    {"missing": missing})

    def gate_task_graph(self) -> Gate:
        result = task_graph.validate_task_graph(repo_root=str(config.REPO_ROOT))
        return Gate("task_graph_valid", True, result["ok"],
                    "no cycles, no dangling/self dependencies, no duplicates" if result["ok"]
                    else f"errors: {'; '.join(result['errors'])}",
                    {"task_count": result["task_count"], "errors": result["errors"]})

    def gate_manifest_readiness(self) -> Gate:
        """C-13: every frozen-input hash must be computable pre-T+00.

        Does not require baseline_sha - that is filled only at `ctl start`.
        This proves the hashing pipeline works now; it does not compare
        against a stored baseline, since none exists before T+00.
        """
        result = manifest.readiness_check(self.cfg)
        evidence = {k: v for k, v in result.fields.items() if k != "providers_and_models"}
        return Gate("manifest_readiness", True, result.ok, result.detail, evidence)

    def gate_host_headroom(self) -> Gate:
        """C-08c: pre-T+00 host CPU/RAM/disk/fd/inotify/port headroom.

        All threshold logic lives in control/hostcheck.py; this is a thin
        wrapper, matching gate_manifest_readiness's shape.
        """
        result = hostcheck.headroom_check()
        return Gate("host_headroom", True, result.ok, result.detail, result.fields)

    def gate_secrets(self) -> Gate:
        config.load_secrets_file()
        missing = config.missing_secrets()
        permissions = config.secrets_file_permissions_ok()
        detail = "all required secrets configured" if not missing else (
            f"not set: {', '.join(missing)}. Add them to {config.SECRETS_FILE} "
            "as KEY=VALUE lines (chmod 600)."
        )
        if permissions is False:
            detail += " WARNING: the secrets file is readable by other users."
        return Gate("required_secrets", True, not missing and permissions is not False,
                    detail,
                    {"required": list(config.REQUIRED_SECRETS), "missing": missing,
                     "secrets_file": str(config.SECRETS_FILE),
                     "secrets_file_present": config.SECRETS_FILE.exists(),
                     "secrets_file_owner_only": permissions})

    def gate_clean_baseline(self) -> Gate:
        status = gh.git(["status", "--porcelain"], str(config.REPO_ROOT))
        head = gh.git(["rev-parse", "HEAD"], str(config.REPO_ROOT))
        ok = status.ok and not status.stdout.strip() and head.ok
        return Gate("clean_baseline", True, ok,
                    f"HEAD {head.stdout[:12]}" if ok else
                    f"working tree not clean: {status.stdout[:300]}",
                    {"head": head.stdout, "dirty": status.stdout[:500]})

    def gate_remote(self) -> Gate:
        remote = gh.git(["remote", "get-url", "origin"], str(config.REPO_ROOT))
        exists = gh.repo_exists(self.cfg.github_repo)
        ahead = gh.git(["rev-list", "--count", f"origin/{self.cfg.main_branch}..HEAD"],
                       str(config.REPO_ROOT))
        pushed = ahead.ok and ahead.stdout.strip() == "0"
        ok = remote.ok and exists and pushed
        return Gate("github_remote", True, ok,
                    "origin configured, repository reachable, baseline pushed" if ok
                    else f"remote={remote.ok} repo={exists} pushed={pushed}",
                    {"repo": self.cfg.github_repo, "unpushed_commits": ahead.stdout})

    def gate_branch_protection(self) -> Gate:
        data = gh.protection(self.cfg.github_repo, self.cfg.main_branch)
        if not data:
            return Gate("github_main_protection", True, False,
                        "branch protection is not configured on main")
        checks = (data.get("required_status_checks") or {}).get("contexts") or []
        contexts = list(checks)
        if not contexts:
            nested = (data.get("required_status_checks") or {}).get("checks") or []
            contexts = [c.get("context") for c in nested]
        reviews = data.get("required_pull_request_reviews")
        force_push = (data.get("allow_force_pushes") or {}).get("enabled", True)
        admins = (data.get("enforce_admins") or {}).get("enabled", False)
        required_present = all(c in contexts for c in self.cfg.required_checks)
        ok = bool(reviews is not None and required_present and not force_push)
        return Gate(
            "github_main_protection", True, ok,
            "PR required, required checks enforced, force push blocked" if ok
            else "protection incomplete",
            {"required_contexts": contexts, "pull_request_reviews": reviews is not None,
             "force_push_allowed": force_push, "enforce_admins": admins},
        )

    def gate_tmux(self) -> Gate:
        created = workers.ensure_session(self.cfg.tmux_session)
        healthy = workers.session_healthy(self.cfg.tmux_session)
        return Gate("tmux_master_session", True, created and healthy,
                    f"session '{self.cfg.tmux_session}' healthy" if healthy
                    else "master tmux session could not be established")

    def gate_workmux(self) -> Gate:
        name = "preflight-probe"
        branch = "preflight/probe"
        prompt = config.WORKER_LOG_DIR / "preflight-probe.prompt.md"
        config.WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        prompt.write_text("Preflight probe. No agent runs here.\n", encoding="utf-8")
        created = workers.create_worker(name, branch, prompt, self.cfg.tmux_session,
                                        self.cfg.main_branch)
        path = workers.worktree_path(name)
        removed = workers.remove_worker(name)
        gh.git(["branch", "-D", branch], str(config.REPO_ROOT))
        ok = created.ok and path is not None and removed.ok
        return Gate("workmux_lifecycle", True, ok,
                    "worktree and tmux window created and removed cleanly" if ok
                    else f"create={created.ok} path={path is not None} remove={removed.ok}",
                    {"stderr": (created.stderr or removed.stderr)[:400]})

    def gate_claude_heartbeat(self) -> Gate:
        """A real headless Claude worker: worktree, trust, process, prompt, heartbeat."""
        name = "preflight-claude"
        branch = "preflight/claude"
        text = (
            "You are a preflight probe for Experiment Run 002. Do not change any file. "
            "Reply with exactly one line: PREFLIGHT_CLAUDE_OK"
        )
        prompt = config.WORKER_LOG_DIR / f"{name}.prompt.md"
        config.WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        prompt.write_text(text, encoding="utf-8")

        created = workers.create_worker(name, branch, prompt, self.cfg.tmux_session,
                                        self.cfg.main_branch)
        path = workers.worktree_path(name)
        if not created.ok or path is None:
            workers.remove_worker(name)
            return Gate("claude_worker_heartbeat", True, False,
                        f"worktree not ready: {created.stderr[:300]}")

        job = workers.write_job(name, "builder", "claude", self.cfg.roles["builder"].model,
                                None, path, prompt, self.tz, hard_timeout_seconds=300)
        workers.start_job(name, job, path)

        readiness = None
        saw_heartbeat = False
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(5)
            readiness = workers.readiness(name, "builder", self.tz)
            if readiness.heartbeat_fresh:
                saw_heartbeat = True
            status = workers.read_status(name) or {}
            if status.get("phase") in ("DONE", "FAILED", "TIMEOUT"):
                break

        status = workers.read_status(name) or {}
        output = workers.worker_output(name, 3000)
        responded = "PREFLIGHT_CLAUDE_OK" in output
        evidence = {
            "readiness": readiness.as_dict() if readiness else None,
            "phase": status.get("phase"),
            "outcome": status.get("outcome"),
            "saw_heartbeat": saw_heartbeat,
            "responded": responded,
            "output_tail": output[-400:],
        }
        workers.remove_worker(name)
        gh.git(["branch", "-D", branch], str(config.REPO_ROOT))

        ok = bool(
            readiness and readiness.worktree_present and readiness.trust_established
            and saw_heartbeat and status.get("prompt_accepted") and responded
        )
        return Gate("claude_worker_heartbeat", True, ok,
                    "worktree + trust + process + prompt accepted + heartbeat + response"
                    if ok else "readiness gates not all satisfied", evidence)

    def gate_codex_review(self) -> Gate:
        """Codex must review read-only and return a parseable verdict block."""
        prompt = (
            "You are the independent Reviewer for Experiment Run 002, read-only.\n"
            "This is a preflight self-check. Do not modify anything.\n"
            "Read PROTOCOL-VERSION.md in this repository and confirm it states "
            "Protocol v2.0.\n"
            "Emit exactly one fenced json block as your final output, of this shape:\n"
            '```json\n{"verdict": "REVIEW_PASS", "gates": {"SCOPE": "PASS"}, '
            '"findings": [], "summary": "preflight read-only check"}\n```\n'
            "Use REVIEW_FAIL with a finding if the protocol version is not v2.0."
        )
        last = config.WORKER_LOG_DIR / "preflight-codex.last.txt"
        config.WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        if last.exists():
            last.unlink()
        command = ["codex", "exec", "--sandbox", "read-only", "-C", str(config.REPO_ROOT),
                   "--skip-git-repo-check", "-o", str(last), "-"]
        result = workers.run_bounded(command, str(config.REPO_ROOT), 240, 420,
                                     stdin_text=prompt)
        text = last.read_text(encoding="utf-8") if last.exists() else result["stdout"]
        review = routing.parse_review(text)
        ok = result["ok"] and review.verdict == routing.REVIEW_PASS
        return Gate("codex_readonly_review", True, ok,
                    f"codex returned {review.verdict}" if result["ok"]
                    else f"codex failed: {result['stderr'][:300]}",
                    {"verdict": review.verdict, "duration_ms": result["duration_ms"],
                     "tail": text[-400:]})

    def _observer_evidence_snapshot(self) -> dict:
        """Representative pre-T+00 evidence for the Observer gate.

        Built to the same shape as Supervisor.observer_evidence so the
        gate feeds the Observer what production feeds it; the two key
        sets are test-pinned equal, so a field added to one and not the
        other fails loudly rather than quietly degrading the gate. Uses
        the real state document when one exists, and an initial document
        otherwise - preflight legitimately runs before `ctl init`.
        """
        store = state_mod.Store(tz=self.tz)
        doc = store.read() if store.exists() else state_mod.initial_document(
            self.cfg.experiment_id, self.cfg.protocol_version)
        return {
            "clock": "T-pre",
            "tasks": {t["id"]: {"state": t["state"], "attempts": t["attempts"],
                                "pr": t.get("pr")} for t in doc["tasks"].values()},
            "prs": {k: {"review_verdict": v.get("review_verdict"),
                        "review_cycles": v.get("review_cycles"),
                        "repair_cycles": v.get("repair_cycles"),
                        "merged": v.get("merged")} for k, v in doc["prs"].items()},
            "providers": {p: r["state"] for p, r in doc["providers"].items()},
            "budget": budget.summary(doc),
            "counters": doc["counters"],
            "migration_lock": doc["migration_lock"],
            "ledger_event_count": self.ledger.count(),
        }

    def gate_grok_observer(self) -> Gate:
        """C-17: exercise the REAL Observer path, not a substitute probe.

        Protocol v2 §"Preflight" is "realistic, not synthetic". This gate
        previously wrote its own trivial prompt ("State one FACT ... then
        stop"), which completes in one or two turns - so it proved the
        CLI, transport and timeout plumbing worked while saying nothing
        about the Observer the run actually uses, whose prompt requires
        several tool-using turns. It therefore PASSED in preflight while
        the production Observer failed with "max turns reached" (observed
        in Trial 0, Scenario C). The gate now renders exactly the prompt
        production renders, against a representative evidence snapshot.

        Fail-closed semantics are deliberately unchanged: a non-zero exit
        FAILs and a timeout FAILs, and neither the Observer's prompt
        content nor its timeout/turn bounds are relaxed here. If this
        gate now fails, the correct response is to fix the Observer, not
        to weaken the gate.
        """
        spec = self.cfg.observer
        config.OBSERVER_DIR.mkdir(parents=True, exist_ok=True)
        evidence_path = config.OBSERVER_DIR / "preflight-evidence.json"
        evidence_path.write_text(
            json.dumps(self._observer_evidence_snapshot(), indent=2),
            encoding="utf-8")
        prompt_path = config.OBSERVER_DIR / "preflight-prompt.md"
        prompt_path.write_text(prompts.observer(str(evidence_path), "T-pre"),
                               encoding="utf-8")
        command = ["grok", "--prompt-file", str(prompt_path), "--output-format", "json",
                   "--disable-web-search", "--max-turns", str(spec["max_turns"]),
                   "--cwd", str(config.REPO_ROOT)]
        result = workers.run_bounded(command, str(config.REPO_ROOT),
                                     spec["soft_timeout_seconds"],
                                     spec["hard_timeout_seconds"])
        if not result["ok"] and spec.get("retry_once_fresh"):
            result = workers.run_bounded(command, str(config.REPO_ROOT),
                                         spec["soft_timeout_seconds"],
                                         spec["hard_timeout_seconds"])
        ok = result["ok"] and not result["timed_out"]
        return Gate("grok_bounded_observer", True, ok,
                    f"bounded observation returned in {result['duration_ms']}ms" if ok
                    else f"observer failed or timed out: {result['stderr'][:200]}",
                    {"timed_out": result["timed_out"],
                     "soft_timeout_exceeded": result["soft_timeout_exceeded"],
                     "duration_ms": result["duration_ms"],
                     "tail": result["stdout"][-300:]})

    def gate_jev(self) -> Gate:
        service = jev.DecisionService(self.cfg.roles["jev"].model)
        decision = service.decide(
            "worker_health",
            {"task": "PREFLIGHT", "state": "ACTIVE", "attempts": 0,
             "minutes_since_progress": 0, "review_queue_depth": 0},
        )
        ok = decision.source == "jev" and decision.choice in jev.DECISIONS[
            "worker_health"]["options"]
        return Gate("jev_minimal_decision", True, ok,
                    f"Jev returned {decision.choice}" if ok
                    else f"Jev unavailable: {decision.error}",
                    {"choice": decision.choice, "source": decision.source,
                     "model": decision.model, "actual_cost_usd": decision.actual_cost_usd,
                     "input_tokens": decision.input_tokens,
                     "output_tokens": decision.output_tokens,
                     "duration_ms": decision.duration_ms, "error": decision.error})

    def gate_langfuse(self) -> Gate:
        result = self.telemetry.span(
            "preflight.trace",
            {"run002.gate": "langfuse_otel", "run002.phase": "PREFLIGHT"},
            duration_ms=1.0,
        )
        return Gate("langfuse_otel_trace", True, bool(result.get("ok")),
                    "OTLP span accepted" if result.get("ok")
                    else f"OTLP rejected: status {result.get('status')} "
                         f"{result.get('body', '')[:200]}",
                    {"status": result.get("status"), "trace_id": result.get("trace_id")})

    def gate_supabase(self) -> Gate:
        result = supabase_health.check()
        return Gate("supabase_health", True, bool(result.get("ok")),
                    "REST and Auth endpoints reachable" if result.get("ok")
                    else f"unreachable: {result}", result)

    def gate_discord(self) -> Gate:
        if not self.notifier.configured:
            return Gate("discord_delivery", True, False,
                        "DISCORD_WEBHOOK_URL is not set in the environment")
        result = self.notifier.send(
            notify.INFO, "Preflight delivery check",
            "Run 002 bootstrap is verifying phone delivery before T+00. "
            "No action needed.", clock_label="T-pre",
        )
        return Gate("discord_delivery", True, bool(result.get("ok")),
                    "webhook delivered" if result.get("ok")
                    else f"delivery failed: {result}", {"status": result.get("status")})

    def gate_ledger(self) -> Gate:
        before = self.ledger.count()
        written = self.ledger.append("PREFLIGHT_LEDGER_PROBE", outcome="OK",
                                     activity_class="ORCHESTRATION")
        after = self.ledger.count()
        last = self.ledger.last("PREFLIGHT_LEDGER_PROBE") or {}
        ok = after == before + 1 and last.get("timestamp") == written.get("timestamp")
        return Gate("ledger_append_read", True, ok,
                    f"append-only JSONL verified ({after} events)" if ok
                    else "ledger append or read-back failed",
                    {"events_before": before, "events_after": after})

    def gate_budget(self) -> Gate:
        text = (config.REPO_ROOT / "experiment" / "BUDGET.md").read_text(encoding="utf-8")
        match = re.search(r"\*\*USD \$([0-9]+(?:\.[0-9]+)?)\*\*", text)
        declared = float(match.group(1)) if match else None
        ok = declared is not None and abs(declared - self.cfg.budget_usd) < 1e-9
        return Gate("budget_configured_by_human", True, ok,
                    f"human-configured ceiling ${self.cfg.budget_usd:.2f} matches "
                    f"experiment/BUDGET.md" if ok
                    else f"configured ${self.cfg.budget_usd} does not match "
                         f"BUDGET.md {declared}",
                    {"budget_md_usd": declared, "config_usd": self.cfg.budget_usd,
                     "configured_by": self.cfg.budget_configured_by})

    def gate_migration_lock(self) -> Gate:
        store = state_mod.Store(tz=self.tz)
        doc = store.read()
        ok = doc["migration_lock"]["state"] == migration_lock.FREE
        return Gate("migration_lock_free", True, ok,
                    "migration lock initialised FREE" if ok else "migration lock is held",
                    doc["migration_lock"])

    def gate_provider_cost_state(self) -> Gate:
        store = state_mod.Store(tz=self.tz)
        doc = store.read()
        states = {p: r["state"] for p, r in doc["providers"].items()}
        ok = (set(states) == set(providers.PROVIDERS)
              and all(v == providers.AVAILABLE for v in states.values())
              and doc["budget"]["total_usd"] == self.cfg.budget_usd)
        return Gate("provider_and_cost_state", True, ok,
                    "provider states AVAILABLE and budget state initialised" if ok
                    else f"unexpected initial state: {states}",
                    {"providers": states, "budget": budget.summary(doc)})

    def gate_supervisor_watchdog(self) -> Gate:
        from . import watchdog as watchdog_mod

        pid = watchdog_mod.supervisor_pid()
        wd_pid = None
        if config.WATCHDOG_PID_PATH.exists():
            try:
                wd_pid = int(config.WATCHDOG_PID_PATH.read_text(encoding="utf-8").strip())
            except ValueError:
                wd_pid = None
        supervisor_ok = watchdog_mod.alive(pid) and watchdog_mod.heartbeat_fresh(
            self.tz, self.cfg.heartbeat_stale_seconds
        )
        watchdog_ok = watchdog_mod.alive(wd_pid)
        return Gate("supervisor_watchdog_healthy", True, supervisor_ok and watchdog_ok,
                    "supervisor heartbeat fresh and watchdog alive" if
                    (supervisor_ok and watchdog_ok)
                    else f"supervisor_ok={supervisor_ok} watchdog_ok={watchdog_ok}",
                    {"supervisor_pid": pid, "watchdog_pid": wd_pid})

    def gate_ci_baseline(self) -> Gate:
        status, conclusion = gh.latest_run_conclusion(self.cfg.github_repo,
                                                      self.cfg.main_branch)
        ok = status == "COMPLETED" and conclusion == "SUCCESS"
        return Gate("ci_baseline_passing", True, ok,
                    f"baseline CI {status}/{conclusion}",
                    {"status": status, "conclusion": conclusion})

    def gate_self_tests(self) -> Gate:
        result = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=str(config.REPO_ROOT), capture_output=True, text=True, check=False,
        )
        ok = result.returncode == 0
        return Gate("control_plane_self_tests", True, ok,
                    "control-plane unit tests pass" if ok
                    else redact.scrub(result.stderr)[-600:],
                    {"returncode": result.returncode})

    # ----------------------------------------------------------------- runner

    GATES = (
        ("protocol_present", "gate_protocol"),
        ("task_graph_valid", "gate_task_graph"),
        ("manifest_readiness", "gate_manifest_readiness"),
        ("host_headroom", "gate_host_headroom"),
        ("required_secrets", "gate_secrets"),
        ("control_plane_self_tests", "gate_self_tests"),
        ("clean_baseline", "gate_clean_baseline"),
        ("github_remote", "gate_remote"),
        ("github_main_protection", "gate_branch_protection"),
        ("ci_baseline_passing", "gate_ci_baseline"),
        ("ledger_append_read", "gate_ledger"),
        ("budget_configured_by_human", "gate_budget"),
        ("migration_lock_free", "gate_migration_lock"),
        ("provider_and_cost_state", "gate_provider_cost_state"),
        ("supervisor_watchdog_healthy", "gate_supervisor_watchdog"),
        ("tmux_master_session", "gate_tmux"),
        ("workmux_lifecycle", "gate_workmux"),
        ("claude_worker_heartbeat", "gate_claude_heartbeat"),
        ("codex_readonly_review", "gate_codex_review"),
        ("grok_bounded_observer", "gate_grok_observer"),
        ("jev_minimal_decision", "gate_jev"),
        ("langfuse_otel_trace", "gate_langfuse"),
        ("supabase_health", "gate_supabase"),
        ("discord_delivery", "gate_discord"),
    )

    def run(self, only: tuple[str, ...] = ()) -> dict:
        config.ensure_runtime_dirs()
        self.results = []
        for name, method in self.GATES:
            if only and name not in only:
                continue
            if name in self.skip:
                self.results.append(Gate(name, True, False, "skipped by operator"))
                continue
            started = time.monotonic()
            try:
                gate = getattr(self, method)()
            except Exception:  # noqa: BLE001 - a broken gate is a failed gate.
                # C-16: the gate's identity plus FAIL is the durable
                # evidence; exception prose is never persisted to the
                # ledger or preflight.json (finite code instead).
                gate = Gate(name, True, False,
                            "gate raised: GATE_EVALUATION_FAILED "
                            "(exception content withheld from durable evidence)",
                            {"error_code": "GATE_EVALUATION_FAILED"})
            duration_ms = round((time.monotonic() - started) * 1000, 1)
            self.results.append(gate)
            self.ledger.append("PREFLIGHT_GATE", outcome="PASS" if gate.ok else "FAIL",
                               activity_class="ORCHESTRATION", duration_ms=duration_ms,
                               guardrail=None if gate.ok else "GATE_FAILED",
                               metadata_redacted=gate.as_dict())
            print(f"[{'PASS' if gate.ok else 'FAIL'}] {gate.name}: {gate.detail}")

        # A partial run must never look like a full pass: `ctl start` reads this
        # file, and "every gate I chose to run was green" is not the same claim as
        # "every required gate is green".
        ran = {g.name for g in self.results}
        required = {name for name, _ in self.GATES}
        summary = {
            "at": clock.iso(clock.now(self.tz)),
            "partial": bool(only) or bool(required - ran),
            "gates_not_run": sorted(required - ran),
            "all_passed": all(g.ok for g in self.results if g.required)
            and not (required - ran),
            "gates": [g.as_dict() for g in self.results],
        }
        # A subset run updates the per-gate record without overwriting the full
        # verdict, so re-running one failed gate does not erase the others.
        if summary["partial"] and config.PREFLIGHT_PATH.exists():
            previous = json.loads(config.PREFLIGHT_PATH.read_text(encoding="utf-8"))
            merged = {g["gate"]: g for g in previous.get("gates", [])}
            merged.update({g["gate"]: g for g in summary["gates"]})
            gates = [merged[name] for name, _ in self.GATES if name in merged]
            not_run = [name for name, _ in self.GATES if name not in merged]
            summary = {
                "at": clock.iso(clock.now(self.tz)),
                "partial": bool(not_run),
                "gates_not_run": not_run,
                "all_passed": all(g["ok"] for g in gates if g["required"]) and not not_run,
                "gates": gates,
            }

        config.PREFLIGHT_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.ledger.append("PREFLIGHT_COMPLETE",
                           outcome="PASS" if summary["all_passed"] else "FAIL",
                           activity_class="ORCHESTRATION",
                           metadata_redacted={"passed": summary["all_passed"],
                                              "failed": [g.name for g in self.results
                                                         if not g.ok]})
        return summary


def tool_versions() -> dict:
    def version(args: list[str]) -> str:
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                 check=False)
            return redact.scrub((out.stdout or out.stderr).strip().splitlines()[0])
        except Exception:  # noqa: BLE001
            return "unavailable"

    return {
        "python": version(["python3", "--version"]),
        "node": version(["node", "--version"]),
        "npm": version(["npm", "--version"]),
        "git": version(["git", "--version"]),
        "tmux": version(["tmux", "-V"]),
        "workmux": version(["workmux", "--version"]),
        "claude": version(["claude", "--version"]),
        "codex": version(["codex", "--version"]),
        "grok": version(["grok", "--version"]),
        "gh": version(["gh", "--version"]),
        "os": version(["uname", "-sr"]),
    }
