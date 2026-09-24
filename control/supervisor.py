"""The deterministic supervisor.

Not an LLM. It owns task state, dependencies, provider state, budget,
concurrency, guardrails, PR routing, deadline phases, notifications and the
ledger. Claude, Codex, Grok and Jev advise or execute work; this process decides
and records.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timedelta

from . import (
    budget,
    clock,
    config,
    evidence,
    gh,
    jev,
    ledger as ledger_mod,
    migration_lock,
    notify,
    proc,
    prompts,
    providers,
    routing,
    state as state_mod,
    telemetry,
    workers,
)

OBSERVER_INTERVAL_SECONDS = 1800
JEV_INTERVAL_SECONDS = 900

# How long a declared bounded operation may take before it is genuinely stale.
# Derived from each operation's own timeout, never open-ended.
JEV_BOUND_SECONDS = 60
BUSY_MARGIN_SECONDS = 60


class Supervisor:
    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or config.load()
        self.tz = self.cfg.timezone
        self.ledger = ledger_mod.Ledger(tz=self.tz, experiment_id=self.cfg.experiment_id)
        self.store = state_mod.Store(tz=self.tz)
        self.notifier = notify.Notifier(self.cfg.experiment_id)
        self.telemetry = telemetry.Telemetry(experiment_id=self.cfg.experiment_id)
        self.jev = jev.DecisionService(self.cfg.roles["jev"].model)
        self.stopping = False
        # Start the observer interval now: the first cycles after a start or a
        # restart belong to getting Builders working, not to observing nothing.
        self._last_observer = time.monotonic()
        self._last_jev = 0.0

    # ------------------------------------------------------------------ helpers

    def now(self) -> datetime:
        return clock.now(self.tz)

    def log(self, event_type: str, **fields):
        return self.ledger.append(event_type, **fields)

    def clock_state(self, doc: dict) -> clock.ClockState | None:
        started = doc.get("started_at")
        if not started:
            return None
        return clock.ClockState(clock.parse(started), self.now(), self.cfg.duration_hours)

    def label(self, doc: dict) -> str:
        cs = self.clock_state(doc)
        return cs.label() if cs else "T-pre"

    def notify_out(self, doc: dict, severity: str, title: str, body: str = "",
                   no_human_action_needed: bool = False) -> dict:
        result = self.notifier.send(severity, title, body,
                                    no_human_action_needed=no_human_action_needed,
                                    clock_label=self.label(doc))
        self.log(
            "NOTIFICATION",
            activity_class="ORCHESTRATION",
            outcome="DELIVERED" if result.get("ok") else "FAILED",
            human_intervention=severity == notify.HUMAN_REQUIRED,
            metadata_redacted={"severity": severity, "title": title,
                               "delivery": result},
        )
        if severity == notify.HUMAN_REQUIRED:
            doc["counters"]["human_interventions"] += 1
        return result

    def heartbeat(self, doc: dict) -> None:
        cs = self.clock_state(doc)
        payload = {
            "pid": os.getpid(),
            "at": clock.iso(self.now()),
            "phase": cs.phase if cs else "PRE_T0",
            "elapsed_hours": round(cs.elapsed_hours, 3) if cs else 0.0,
            "tasks": {t["id"]: t["state"] for t in doc["tasks"].values()},
            "review_queue_depth": state_mod.review_queue_depth(doc),
            "budget": budget.summary(doc),
            "providers": {p: r["state"] for p, r in doc["providers"].items()},
            "migration_lock": doc["migration_lock"]["state"],
        }
        self._write_heartbeat(payload)

    def _write_heartbeat(self, payload: dict) -> None:
        tmp = config.HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, config.HEARTBEAT_PATH)

    def declare_busy(self, what: str, bound_seconds: float) -> None:
        """Announce a bounded piece of slow work before starting it.

        Approved slow work happens outside the state lock but still inside the
        tick, so no heartbeat is written while it runs. An Observer job can
        legitimately take minutes, which is longer than the watchdog's staleness
        threshold, and a perfectly healthy supervisor then looks dead.

        Declaring the work - and the deadline it must finish by - lets the
        watchdog distinguish "busy for a stated, bounded reason" from "wedged".
        It does not weaken detection: a dead process is still dead by its PID,
        and overrunning the declared bound is still stale.
        """
        try:
            payload = json.loads(config.HEARTBEAT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        until = self.now() + timedelta(seconds=bound_seconds)
        payload["busy_with"] = what
        payload["busy_until"] = clock.iso(until)
        payload["busy_declared_at"] = clock.iso(self.now())
        self._write_heartbeat(payload)

    def clear_busy(self) -> None:
        try:
            payload = json.loads(config.HEARTBEAT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key in ("busy_with", "busy_until", "busy_declared_at"):
            payload.pop(key, None)
        payload["at"] = clock.iso(self.now())
        self._write_heartbeat(payload)

    # --------------------------------------------------------------- guardrails

    def red_guardrail_active(self, doc: dict) -> bool:
        return bool(doc.get("red_guardrail"))

    def raise_red(self, doc: dict, guardrail: str, detail: str) -> None:
        doc["red_guardrail"] = {"guardrail": guardrail, "detail": detail,
                                "at": clock.iso(self.now())}
        doc["counters"]["guardrail_activations"] += 1
        self.log("GUARDRAIL_RED", guardrail=guardrail, outcome="BLOCKED",
                 activity_class="ESCALATION", metadata_redacted={"detail": detail})
        self.notify_out(doc, notify.HUMAN_REQUIRED, f"RED guardrail: {guardrail}", detail)

    # ------------------------------------------------------------- dispatching

    def dispatchable(self, doc: dict, cs: clock.ClockState) -> list[dict]:
        """Dependency-ready tasks permitted by phase, provider policy and backpressure."""
        if cs.expired or doc.get("frozen_at"):
            return []
        if not providers.may(doc, "new_builds") or providers.safe_hold(doc):
            return []

        depth = state_mod.review_queue_depth(doc)
        limit = state_mod.builder_limit(depth, self.cfg.max_builders)
        running = len([t for t in doc["tasks"].values() if t["state"] in ("ASSIGNED", "ACTIVE")])
        slots = limit - running
        if slots <= 0:
            return []

        ready: list[dict] = []
        for task in doc["tasks"].values():
            if task["state"] not in ("QUEUED", "READY", "WAITING_PROVIDER_RESET",
                                     "WAITING_DB_LOCK"):
                continue
            if not state_mod.dependencies_met(doc, task):
                continue
            if not clock.phase_allows(cs.phase, task.get("kind", "feature")):
                continue
            ready.append(task)

        ready.sort(key=lambda t: t["id"])
        return ready[:slots]

    def dispatch_builder(self, doc: dict, task: dict) -> None:
        if task.get("schema_changing"):
            if not migration_lock.acquire(doc, task["id"], self.tz):
                if task["state"] != "WAITING_DB_LOCK":
                    self.transition(doc, task["id"], "WAITING_DB_LOCK",
                                    f"migration lock held by {migration_lock.owner(doc)}")
                return
            self.log("MIGRATION_LOCK_ACQUIRED", task_id=task["id"],
                     activity_class="ORCHESTRATION", outcome="HELD")

        worker = f"{task['id'].lower()}-builder"
        branch = f"task/{task['id'].lower()}"
        role_cfg = self.cfg.roles["builder"]

        if task["state"] != "READY":
            self.transition(doc, task["id"], "READY", "dependencies met")

        prompt_text = prompts.builder(task, branch, self.cfg.github_repo,
                                      self.cfg.main_branch)
        prompt_path = prompts.write(worker, prompt_text)

        created = workers.create_worker(worker, branch, prompt_path,
                                        self.cfg.tmux_session, self.cfg.main_branch)
        if not created.ok:
            self.log("WORKER_CREATE_FAILED", task_id=task["id"], role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"stderr": created.stderr[:500]})
            self.transition(doc, task["id"], "BLOCKED", "worktree creation failed")
            return

        path = workers.worktree_path(worker)
        if path is None:
            self.transition(doc, task["id"], "BLOCKED", "worktree path not resolvable")
            return

        job = workers.write_job(
            worker, "builder", role_cfg.provider, role_cfg.model, task["id"], path,
            prompt_path, self.tz, hard_timeout_seconds=self.cfg.extra["timeouts"]["builder"],
            fallback_model=role_cfg.escalation_model,
        )
        started = workers.start_job(worker, job, path)
        if not started.ok:
            self.log("WORKER_START_FAILED", task_id=task["id"], role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"stderr": started.stderr[:500]})
            self.transition(doc, task["id"], "BLOCKED", "worker start failed")
            return

        task["worker"] = worker
        task["branch"] = branch
        task["attempts"] += 1
        task["assigned_at"] = clock.iso(self.now())
        task["last_progress_at"] = task["assigned_at"]
        task["progress_marker"] = 0
        self.transition(doc, task["id"], "ASSIGNED", "builder dispatched")
        doc["workers"][worker] = state_mod.new_worker_record(
            "builder", task["id"], branch, task["assigned_at"])
        self.log("TASK_DISPATCHED", task_id=task["id"], role="builder",
                 provider=role_cfg.provider, model=role_cfg.model, branch=branch,
                 agent_id=worker, activity_class="BUILD", outcome="DISPATCHED")

    def dispatch_reviewer(self, doc: dict, task: dict, pr_number: int) -> None:
        if not providers.may(doc, "review"):
            self.transition(doc, task["id"], "WAITING_PROVIDER_RESET",
                            "reviewer provider paused")
            return
        record = doc["prs"][str(pr_number)]
        worker = f"{task['id'].lower()}-review-{record['review_cycles'] + 1}"
        role_cfg = self.cfg.roles["reviewer"]

        # Each cycle reviews in its own worktree, on its own branch, cut from the
        # pull request's CURRENT head. A per-cycle branch cannot collide with an
        # earlier cycle's checkout, and basing it on the live head guarantees the
        # re-review sees the Fixer's changes rather than superseded code.
        branch = task["branch"]
        cycle = record["review_cycles"] + 1
        head = gh.pr_diff_sha(self.cfg.github_repo, pr_number)
        if head is None:
            self.on_dispatch_failure(doc, task, pr_number, "reviewer",
                                     "could not resolve the pull request head")
            return

        # Evidence Codex cannot reach from a read-only, no-network sandbox, bound
        # to this exact head. Agent self-assertions are deliberately excluded.
        items = evidence.collect(self.cfg.github_repo, head, self.ledger)
        self.log("REVIEW_EVIDENCE_GATHERED", task_id=task["id"], pr_id=pr_number,
                 activity_class="REVIEW", outcome="COLLECTED",
                 metadata_redacted={"head": head,
                                    "items": [i.as_dict() for i in items],
                                    "applicable": sum(1 for i in items if i.applies)})

        prompt_text = prompts.reviewer(task, pr_number, branch, self.cfg.github_repo,
                                       cycle, evidence.render(items, head))
        prompt_path = prompts.write(worker, prompt_text)
        # Cycle segment first. `review/<branch>/c2` would nest a ref under the
        # existing `review/<branch>` ref, which git stores as a file and cannot
        # also be a directory. `review/c2/<branch>` cannot collide with any other
        # review branch by construction, so the question never arises.
        path, why = workers.acquire_worktree(
            worker, f"review/c{cycle}/{branch}", head, self.cfg.tmux_session,
            prompt_path, reuse_if_checked_out=False,
        )
        if path is None:
            self.on_dispatch_failure(doc, task, pr_number, "reviewer", why)
            return

        record["reviewed_head"] = head
        record["reviewed_diff_hash"] = routing.material_diff_hash(
            self.cfg.github_repo, pr_number
        )
        job = workers.write_job(
            worker, "reviewer", role_cfg.provider, role_cfg.model, task["id"], path,
            prompt_path, self.tz, pr=pr_number,
            hard_timeout_seconds=self.cfg.extra["timeouts"]["reviewer"],
            effort=role_cfg.effort,
        )
        started = workers.start_job(worker, job, path)
        if not started.ok:
            self.on_dispatch_failure(doc, task, pr_number, "reviewer",
                                     f"worker did not start: {started.stderr[:200]}")
            return

        record["review_cycles"] += 1
        record["approval_current"] = False
        record["review_verdict"] = None
        task["worker"] = worker
        doc["workers"][worker] = state_mod.new_worker_record(
            "reviewer", task["id"], branch, clock.iso(self.now()), pr=pr_number)
        self.transition(doc, task["id"], "REVIEW", f"dispatched review cycle "
                                                   f"{record['review_cycles']}")
        self.log("REVIEW_DISPATCHED", task_id=task["id"], pr_id=pr_number, role="reviewer",
                 provider=role_cfg.provider, model=role_cfg.model, agent_id=worker,
                 activity_class="REVIEW", outcome="DISPATCHED")

    def dispatch_fixer(self, doc: dict, task: dict, pr_number: int,
                       findings: list[dict]) -> None:
        if not providers.may(doc, "new_builds"):
            self.transition(doc, task["id"], "WAITING_PROVIDER_RESET",
                            "fixer provider paused")
            return
        record = doc["prs"][str(pr_number)]
        if record["repair_cycles"] >= self.cfg.max_repair_cycles:
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            "repair cycle limit reached")
            self.notify_out(doc, notify.HUMAN_REQUIRED,
                            f"{task['id']} PR #{pr_number}: repair limit reached",
                            f"{record['repair_cycles']} repair cycles without a pass.")
            return

        worker = f"{task['id'].lower()}-fixer-{record['repair_cycles'] + 1}"
        role_cfg = self.cfg.roles["fixer"]
        prompt_text = prompts.fixer(task, pr_number, task["branch"],
                                    self.cfg.github_repo, findings)
        prompt_path = prompts.write(worker, prompt_text)

        # The Fixer commits to the Builder's PR branch, so it reuses the worktree
        # already holding that branch. It is still a fresh Claude context: a new
        # process, a new prompt, and only the reported findings in scope.
        path, why = workers.acquire_worktree(
            worker, task["branch"], self.cfg.main_branch, self.cfg.tmux_session,
            prompt_path, reuse_if_checked_out=True,
        )
        if path is None:
            self.on_dispatch_failure(doc, task, pr_number, "fixer", why)
            return

        job = workers.write_job(
            worker, "fixer", role_cfg.provider, role_cfg.model, task["id"], path,
            prompt_path, self.tz, pr=pr_number,
            hard_timeout_seconds=self.cfg.extra["timeouts"]["fixer"],
        )
        started = workers.start_job(worker, job, path)
        if not started.ok:
            self.on_dispatch_failure(doc, task, pr_number, "fixer",
                                     f"worker did not start: {started.stderr[:200]}")
            return

        record["repair_cycles"] += 1
        record["open_finding_ids"] = [f.get("id") for f in findings]
        task["worker"] = worker
        doc["workers"][worker] = state_mod.new_worker_record(
            "fixer", task["id"], task["branch"], clock.iso(self.now()), pr=pr_number)
        self.transition(doc, task["id"], "FIX_REQUIRED",
                        f"fix cycle {record['repair_cycles']}")
        self.log("FIX_DISPATCHED", task_id=task["id"], pr_id=pr_number, role="fixer",
                 provider=role_cfg.provider, model=role_cfg.model, agent_id=worker,
                 activity_class="FIX", outcome="DISPATCHED",
                 metadata_redacted={"finding_ids": record["open_finding_ids"]})

    def on_dispatch_failure(self, doc: dict, task: dict, pr_number: int, role: str,
                            reason: str) -> None:
        """A dispatch that fails must say so and stay recoverable.

        The deadlock this replaces was an unlogged early `return`: the task sat in
        REVIEW with no worker, stale detection does not cover REVIEW, and nothing
        would ever pick it up again. Now the failure is recorded, counted, and
        left in a state the supervisor retries on the next tick - and once the
        failures reach the existing repair-cycle limit it escalates to a human
        rather than retrying forever.
        """
        record = doc["prs"][str(pr_number)]
        record["dispatch_failures"] = record.get("dispatch_failures", 0) + 1
        failures = record["dispatch_failures"]
        self.log("DISPATCH_FAILED", task_id=task["id"], pr_id=pr_number, role=role,
                 activity_class="FAILED_WORK", outcome="FAILED",
                 metadata_redacted={"reason": reason, "dispatch_failures": failures,
                                    "state": task["state"]})
        if failures >= self.cfg.max_repair_cycles:
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            f"{role} dispatch failed {failures} times")
            self.notify_out(
                doc, notify.HUMAN_REQUIRED,
                f"{task['id']} PR #{pr_number}: {role} dispatch keeps failing",
                f"{failures} consecutive dispatch failures. Last reason: {reason}",
            )

    # ------------------------------------------------------------- transitions

    def transition(self, doc: dict, task_id: str, new_state: str, reason: str) -> None:
        try:
            before, after = state_mod.transition(doc, task_id, new_state, reason, self.tz)
        except state_mod.TransitionError as exc:
            self.log("INVALID_TRANSITION", task_id=task_id, outcome="REJECTED",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"error": str(exc), "requested": new_state})
            return
        if before != after:
            self.log("TASK_STATE_CHANGE", task_id=task_id, state_before=before,
                     state_after=after, activity_class="ORCHESTRATION",
                     outcome=after, metadata_redacted={"reason": reason})

    # ---------------------------------------------------------- worker reaping

    def reap_workers(self, doc: dict) -> None:
        for worker, meta in list(doc["workers"].items()):
            status = workers.read_status(worker)
            if not status:
                continue
            task_id = meta.get("task_id")
            task = doc["tasks"].get(task_id) if task_id else None
            phase = status.get("phase")

            if task and task["state"] == "ASSIGNED" and status.get("prompt_accepted"):
                self.transition(doc, task_id, "ACTIVE", "prompt accepted by agent")

            if task and phase in ("RUNNING", "PROMPT_ACCEPTED"):
                marker = status.get("output_bytes", 0)
                if marker != task.get("progress_marker"):
                    task["progress_marker"] = marker
                    task["last_progress_at"] = clock.iso(self.now())

            if phase not in ("DONE", "FAILED", "TIMEOUT"):
                continue

            self.log("WORKER_FINISHED", agent_id=worker, role=meta.get("role"),
                     task_id=task_id, pr_id=meta.get("pr"),
                     duration_ms=status.get("duration_ms"),
                     outcome=status.get("outcome"),
                     activity_class={"builder": "BUILD", "fixer": "FIX",
                                     "reviewer": "REVIEW"}.get(meta.get("role"),
                                                               "ORCHESTRATION"))
            self.telemetry.span(
                f"worker.{meta.get('role')}",
                {
                    "run002.task": task_id or "",
                    "run002.role": meta.get("role") or "",
                    "run002.outcome": status.get("outcome") or "",
                    "run002.worker": worker,
                },
                duration_ms=status.get("duration_ms") or 0,
                error=status.get("outcome") != "SUCCESS",
            )

            handler = {
                "builder": self.on_builder_finished,
                "fixer": self.on_fixer_finished,
                "reviewer": self.on_reviewer_finished,
                "observer": self.on_observer_finished,
            }.get(meta.get("role"))
            if handler:
                handler(doc, worker, meta, status)
            doc["workers"].pop(worker, None)

    def _handle_provider_failure(self, doc: dict, provider: str, status: dict,
                                 task: dict | None, note: str) -> bool:
        """Returns True when the failure was a provider limit (not a work failure)."""
        excerpt = status.get("error_excerpt") or ""
        if not providers.is_limit_error(excerpt):
            return False
        before, after = providers.record_failure(
            doc, provider, excerpt, self.cfg.cooldown_seconds.get(provider, 900), self.tz
        )
        self.log("PROVIDER_STATE_CHANGE", provider=provider, state_before=before,
                 state_after=after, activity_class="ORCHESTRATION", outcome=after,
                 metadata_redacted={"note": note})
        self.notify_out(doc, notify.ATTENTION,
                        f"{provider} entered {after}",
                        f"{note} The 24-hour clock continues; unaffected work proceeds.",
                        no_human_action_needed=True)
        if task:
            self.transition(doc, task["id"], "WAITING_PROVIDER_RESET",
                            f"{provider} {after}")
        return True

    def on_builder_finished(self, doc: dict, worker: str, meta: dict, status: dict) -> None:
        task = doc["tasks"].get(meta["task_id"])
        if not task:
            return
        provider = self.cfg.roles["builder"].provider
        if status.get("outcome") != "SUCCESS":
            if self._handle_provider_failure(doc, provider, status, task,
                                             "Builder hit a provider limit."):
                return
            if task["attempts"] >= self.cfg.max_repair_cycles:
                self.transition(doc, task["id"], "FAILED", "builder attempts exhausted")
                self.notify_out(doc, notify.HUMAN_REQUIRED,
                                f"{task['id']} failed after {task['attempts']} attempts",
                                "Evidence-based recovery attempts are exhausted.")
            else:
                self.transition(doc, task["id"], "READY", "builder failed; will retry")
            return

        providers.record_success(doc, provider, self.tz)
        pr_number = self.find_pr_for_branch(task["branch"])
        if pr_number is None:
            self.transition(doc, task["id"], "READY",
                            "builder reported success but opened no PR")
            self.log("NO_PR_AFTER_BUILD", task_id=task["id"], outcome="RETRY",
                     activity_class="FAILED_WORK")
            return
        self.attach_pr(doc, task, pr_number)

    def on_fixer_finished(self, doc: dict, worker: str, meta: dict, status: dict) -> None:
        task = doc["tasks"].get(meta["task_id"])
        pr_number = meta.get("pr")
        if not task or pr_number is None:
            return
        if status.get("outcome") != "SUCCESS":
            if self._handle_provider_failure(doc, self.cfg.roles["fixer"].provider, status,
                                             task, "Fixer hit a provider limit."):
                return
            self.transition(doc, task["id"], "HUMAN_REQUIRED", "fixer failed")
            self.notify_out(doc, notify.HUMAN_REQUIRED,
                            f"{task['id']} PR #{pr_number}: fixer failed",
                            "The fixer could not complete the listed findings.")
            return
        record = doc["prs"][str(pr_number)]
        record["approval_current"] = False
        record["review_verdict"] = None
        self.transition(doc, task["id"], "REVIEW", "fix complete; returning to reviewer")
        self.dispatch_reviewer(doc, task, pr_number)

    def on_reviewer_finished(self, doc: dict, worker: str, meta: dict, status: dict) -> None:
        task = doc["tasks"].get(meta["task_id"])
        pr_number = meta.get("pr")
        if not task or pr_number is None:
            return
        record = doc["prs"][str(pr_number)]
        if status.get("outcome") != "SUCCESS":
            if self._handle_provider_failure(doc, self.cfg.roles["reviewer"].provider,
                                             status, task,
                                             "Reviewer hit a provider limit."):
                return
            self.transition(doc, task["id"], "PR_OPEN", "review failed; will re-dispatch")
            return

        providers.record_success(doc, self.cfg.roles["reviewer"].provider, self.tz)
        last_message = config.WORKER_LOG_DIR / f"{worker}.last.txt"
        text = last_message.read_text(encoding="utf-8") if last_message.exists() else \
            workers.worker_output(worker, 20000)
        # The verdict has been read out of the finished worker, so its worktree and
        # per-cycle branch are obsolete. Releasing them here keeps review cycles
        # from accumulating checkouts that block a later cycle. Only this
        # reviewer's own finished worktree is removed; no live worker is touched,
        # and the PR branch's worktree is never a reviewer's.
        self.release_review_worktree(worker)
        review = routing.parse_review(text)
        consistent, why = routing.review_is_consistent(
            review, routing.touches_ui(self.cfg.github_repo, pr_number)
        )

        record["last_review_at"] = clock.iso(self.now())
        self.log("REVIEW_RESULT", task_id=task["id"], pr_id=pr_number, role="reviewer",
                 provider=self.cfg.roles["reviewer"].provider, agent_id=worker,
                 activity_class="REVIEW", outcome=review.verdict,
                 metadata_redacted=review.as_dict())

        if review.verdict == routing.REVIEW_UNPARSEABLE or not consistent:
            record["approval_current"] = False
            record["review_verdict"] = routing.REVIEW_FAIL
            self.log("REVIEW_REJECTED_BY_SUPERVISOR", task_id=task["id"], pr_id=pr_number,
                     outcome="REJECTED", activity_class="REVIEW",
                     metadata_redacted={"reason": why or "unparseable verdict"})
            if record["review_cycles"] >= self.cfg.max_repair_cycles:
                self.transition(doc, task["id"], "HUMAN_REQUIRED",
                                "reviewer output unusable")
                self.notify_out(doc, notify.HUMAN_REQUIRED,
                                f"{task['id']} PR #{pr_number}: review unusable",
                                why or "The reviewer did not return a valid verdict.")
            else:
                self.transition(doc, task["id"], "PR_OPEN", "review unusable; re-review")
            return

        if review.critical:
            record["review_verdict"] = routing.REVIEW_FAIL
            record["approval_current"] = False
            self.transition(doc, task["id"], "HUMAN_REQUIRED", "P0 finding")
            self.notify_out(doc, notify.HUMAN_REQUIRED,
                            f"{task['id']} PR #{pr_number}: P0 finding",
                            "; ".join(f.get("summary", "")[:120] for f in review.critical))
            return

        if review.verdict == routing.REVIEW_FAIL or review.blocking:
            record["review_verdict"] = routing.REVIEW_FAIL
            record["approval_current"] = False
            # Persist the findings so a retry dispatches the same repair work
            # instead of paying for another review to rediscover it.
            record["pending_findings"] = review.blocking or review.findings
            self.dispatch_fixer(doc, task, pr_number, record["pending_findings"])
            return

        record["review_verdict"] = routing.REVIEW_PASS
        record["approval_current"] = True
        self.log("REVIEW_PASS_RECORDED", task_id=task["id"], pr_id=pr_number,
                 outcome="APPROVED", activity_class="REVIEW")

    def release_review_worktree(self, worker: str) -> None:
        """Remove a finished reviewer's worktree. Never touches a live worker."""
        status = workers.read_status(worker) or {}
        if status.get("phase") not in ("DONE", "FAILED", "TIMEOUT"):
            return
        if workers.process_alive(status.get("agent_pid")):
            return
        result = workers.remove_worker(worker)
        self.log("REVIEW_WORKTREE_RELEASED", agent_id=worker, role="reviewer",
                 activity_class="ORCHESTRATION",
                 outcome="RELEASED" if result.ok else "FAILED",
                 metadata_redacted={"stderr": result.stderr[:200]})

    def on_observer_finished(self, doc: dict, worker: str, meta: dict, status: dict) -> None:
        self.log("OBSERVATION_COMPLETE", agent_id=worker, role="observer",
                 provider="grok", outcome=status.get("outcome"),
                 duration_ms=status.get("duration_ms"), activity_class="OBSERVATION")

    # --------------------------------------------------------------- PR routing

    def find_pr_for_branch(self, branch: str) -> int | None:
        for pr in gh.list_open_prs(self.cfg.github_repo):
            if pr.get("headRefName") == branch:
                return pr.get("number")
        return None

    def attach_pr(self, doc: dict, task: dict, pr_number: int) -> None:
        key = str(pr_number)
        if key not in doc["prs"]:
            doc["prs"][key] = routing.blank_pr_record(pr_number, task["id"],
                                                      task["branch"])
        task["pr"] = pr_number
        if task.get("schema_changing"):
            migration_lock.attach_pr(doc, task["id"], pr_number)
        self.transition(doc, task["id"], "PR_OPEN", f"PR #{pr_number} opened")
        self.log("PR_OPENED", task_id=task["id"], pr_id=pr_number, branch=task["branch"],
                 activity_class="BUILD", outcome="PR_OPEN")

    def route_prs(self, doc: dict, cs: clock.ClockState, prs: list[dict]) -> None:
        open_prs = {pr["number"]: pr for pr in prs}
        depth = state_mod.review_queue_depth(doc)
        doc["review_queue_depth_history"].append(
            {"at": clock.iso(self.now()), "depth": depth}
        )

        for task in sorted(doc["tasks"].values(), key=lambda t: t["id"]):
            pr_number = task.get("pr")
            if pr_number is None:
                continue
            record = doc["prs"].get(str(pr_number))
            if record is None or record.get("merged"):
                continue
            pr = open_prs.get(pr_number)

            if pr is None:
                merged = gh.pr_view(self.cfg.github_repo, pr_number) or {}
                if (merged.get("state") or "").upper() == "MERGED":
                    self.complete_task(doc, task, pr_number)
                continue

            if task["state"] == "REVIEW" and record.get("review_verdict") == \
                    routing.REVIEW_PASS and record.get("approval_current"):
                self.attempt_merge(doc, task, pr, record)
                continue

            self.route_awaiting_dispatch(doc, task, pr_number, record)

    ROUTING_STATES = ("PR_OPEN", "REVIEW", "FIX_REQUIRED")

    def route_awaiting_dispatch(self, doc: dict, task: dict, pr_number: int,
                                record: dict) -> None:
        """A PR-bearing task in a routing state with no live worker is waiting.

        This is the general recovery rule. A dispatch that cannot obtain its
        worktree logs the failure and returns, which leaves the task in a routing
        state with nobody working on it - invisible to stale detection, which
        covers only ASSIGNED and ACTIVE. Rather than special-casing each role that
        has collided so far, every such task is re-routed here on the next tick,
        by verdict:

          no current verdict  -> Codex must review; only Codex may approve
          REVIEW_FAIL         -> the recorded findings go back to a fresh Fixer

        Bounded by the existing repair-cycle limit through on_dispatch_failure,
        which escalates to a human rather than retrying forever.
        """
        if task["state"] not in self.ROUTING_STATES:
            return
        if self.has_worker(doc, pr_number, ("reviewer", "fixer")):
            return

        verdict = record.get("review_verdict")
        findings = record.get("pending_findings")
        # A first dispatch from PR_OPEN is ordinary routing, not a recovery.
        retry = (task["state"] != "PR_OPEN"
                 or record.get("dispatch_failures", 0) > 0
                 or record.get("review_cycles", 0) > 0)

        if verdict == routing.REVIEW_FAIL and findings:
            if retry:
                self.log("DISPATCH_RETRY", task_id=task["id"], pr_id=pr_number,
                         role="fixer", activity_class="ORCHESTRATION", outcome="RETRY",
                         metadata_redacted={
                             "from_state": task["state"],
                             "finding_ids": [f.get("id") for f in findings],
                             "dispatch_failures": record.get("dispatch_failures", 0)})
            self.dispatch_fixer(doc, task, pr_number, findings)
            return

        if retry:
            self.log("DISPATCH_RETRY", task_id=task["id"], pr_id=pr_number,
                     role="reviewer", activity_class="ORCHESTRATION", outcome="RETRY",
                     metadata_redacted={
                         "from_state": task["state"], "verdict": verdict,
                         "dispatch_failures": record.get("dispatch_failures", 0)})
        self.dispatch_reviewer(doc, task, pr_number)

    def has_worker(self, doc: dict, pr_number: int, roles: tuple[str, ...]) -> bool:
        """Whether a worker in any of these roles is already live on this PR."""
        return any(meta.get("pr") == pr_number and meta.get("role") in roles
                   for meta in doc["workers"].values())

    def attempt_merge(self, doc: dict, task: dict, pr: dict, record: dict) -> None:
        repo = self.cfg.github_repo
        number = pr["number"]

        if not providers.may(doc, "merge"):
            return

        merge_state = (pr.get("mergeStateStatus") or "").upper()
        if merge_state == "BEHIND" and not record.get("reconciled"):
            result = gh.update_branch(repo, number)
            record["reconciled"] = True
            self.log("BRANCH_RECONCILED", task_id=task["id"], pr_id=number,
                     outcome="OK" if result.ok else "FAILED",
                     activity_class="ORCHESTRATION")
            pr = gh.pr_view(repo, number) or pr

        current_hash = routing.material_diff_hash(repo, number)
        decision = routing.evaluate_merge(
            pr, record, self.cfg.required_checks, self.red_guardrail_active(doc),
            current_hash
        )

        if not decision.allowed:
            if decision.invalidate_approval:
                record["approval_current"] = False
                record["review_verdict"] = None
                record["reviewed_diff_hash"] = None
                doc["counters"]["merge_approvals_invalidated"] += 1
                self.log("APPROVAL_INVALIDATED", task_id=task["id"], pr_id=number,
                         outcome="INVALIDATED", activity_class="REVIEW",
                         metadata_redacted={"reason": decision.reason})
                self.transition(doc, task["id"], "PR_OPEN",
                                f"approval invalidated: {decision.reason}")
            self.log("MERGE_BLOCKED", task_id=task["id"], pr_id=number, outcome="BLOCKED",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"reason": decision.reason})
            return

        result = gh.merge(repo, number)
        if not result.ok:
            self.log("MERGE_FAILED", task_id=task["id"], pr_id=number, outcome="FAILED",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"stderr": result.stderr[:400]})
            return

        record["merged"] = True
        self.log("MERGED", task_id=task["id"], pr_id=number, branch=task["branch"],
                 outcome="MERGED", activity_class="ORCHESTRATION")
        self.complete_task(doc, task, number)

    def complete_task(self, doc: dict, task: dict, pr_number: int) -> None:
        record = doc["prs"].get(str(pr_number))
        if record:
            record["merged"] = True
        if task["state"] != "MERGED":
            self.transition(doc, task["id"], "MERGED", f"PR #{pr_number} merged")
        self.transition(doc, task["id"], "COMPLETE", "task delivered")
        if migration_lock.owner(doc) == task["id"]:
            migration_lock.release(doc, task["id"], "migration merged")
            self.log("MIGRATION_LOCK_RELEASED", task_id=task["id"], outcome="FREE",
                     activity_class="ORCHESTRATION")
        worker = task.get("worker")
        if worker:
            workers.close_worker(worker)
        self.notify_out(doc, notify.INFO, f"{task['id']} complete",
                        f"{task['title']} merged as PR #{pr_number}.")

    # ------------------------------------------------------- staleness and Jev

    def detect_stale(self, doc: dict) -> None:
        """Stale means no progress, not merely slow.

        Two distinct faults count. A worker whose agent process has vanished
        without a terminal status has died silently. A worker that is streaming
        output and then stops making progress has hung. A worker that has simply
        not emitted anything yet is neither: it is bounded by its own hard
        timeout, and killing it would destroy healthy work.
        """
        cutoff = self.cfg.stale_task_minutes * 60
        now = self.now()
        for task in doc["tasks"].values():
            if task["state"] not in ("ASSIGNED", "ACTIVE"):
                continue
            worker = task.get("worker")
            if not worker:
                continue
            status = workers.read_status(worker) or {}
            phase = status.get("phase")
            if phase in ("DONE", "FAILED", "TIMEOUT"):
                continue

            died_silently = bool(status) and not workers.process_alive(
                status.get("agent_pid")
            )
            reason = "agent process vanished without reporting an outcome"

            if not died_silently:
                if not status.get("prompt_accepted"):
                    # No progress signal yet; the worker's hard timeout bounds it.
                    continue
                last = task.get("last_progress_at")
                if not last or (now - clock.parse(last)).total_seconds() < cutoff:
                    continue
                reason = "no meaningful progress within the stale window"

            self.transition(doc, task["id"], "STALE", reason)
            self.log("STALE_DETECTED", task_id=task["id"], outcome="STALE",
                     activity_class="FAILED_WORK",
                     metadata_redacted={"reason": reason, "phase": phase})
            self.notify_out(doc, notify.ATTENTION, f"{task['id']} is stale",
                            "No meaningful progress within the stale window; "
                            "the supervisor will recycle the worker.",
                            no_human_action_needed=True)
            worker = task.get("worker")
            if worker:
                workers.remove_worker(worker)
                doc["workers"].pop(worker, None)
            self.transition(doc, task["id"], "READY", "recycled after stale detection")

    def consult_jev(self) -> None:
        """Advisory only, and deliberately outside the state transaction.

        A provider call can take tens of seconds; holding the state lock for that
        long leaves `state.json` stale and widens the window in which a crash
        discards work the ledger has already recorded.
        """
        if time.monotonic() - self._last_jev < JEV_INTERVAL_SECONDS:
            return
        self._last_jev = time.monotonic()

        snapshot = self.store.read()
        active = [t for t in snapshot["tasks"].values()
                  if t["state"] in ("ACTIVE", "REVIEW")]
        if not active:
            return
        task = active[0]

        decision = self.jev.decide(
            "task_health",
            {
                "task": task["id"],
                "state": task["state"],
                "attempts": task["attempts"],
                "minutes_since_progress": self._minutes_since(task.get("last_progress_at")),
                "review_queue_depth": state_mod.review_queue_depth(snapshot),
            },
            allowed=budget.metered_call_allowed(snapshot),
        )

        self.log("JEV_DECISION", task_id=task["id"], provider="openrouter",
                 model=decision.model, activity_class="ORCHESTRATION",
                 outcome=decision.choice, actual_cost_usd=decision.actual_cost_usd,
                 cost_source="provider" if decision.actual_cost_usd is not None else "none",
                 input_tokens=decision.input_tokens, output_tokens=decision.output_tokens,
                 duration_ms=decision.duration_ms,
                 metadata_redacted={"source": decision.source, "kind": decision.kind,
                                    "confidence": decision.confidence,
                                    "reason": decision.reason, "error": decision.error})

        with self.store.transaction() as doc:
            crossed = budget.record(doc, "openrouter", decision.actual_cost_usd, None)
            for name in crossed:
                self.on_budget_threshold(doc, name)

    def _minutes_since(self, iso_value: str | None) -> float:
        if not iso_value:
            return 0.0
        return round((self.now() - clock.parse(iso_value)).total_seconds() / 60.0, 1)

    def on_budget_threshold(self, doc: dict, name: str) -> None:
        summary = budget.summary(doc)
        self.log("BUDGET_THRESHOLD", outcome=name, activity_class="ORCHESTRATION",
                 metadata_redacted=summary)
        severity = notify.HUMAN_REQUIRED if name == "HARD_STOP" else notify.ATTENTION
        self.notify_out(
            doc, severity, f"Budget threshold {name} ({summary['percent']}%)",
            f"Metered OpenRouter spend ${summary['spent_usd']:.4f} of "
            f"${summary['total_usd']:.2f}. "
            + ("Further paid model calls need explicit human approval; Jev falls back to "
               "deterministic behaviour and the experiment continues."
               if name == "HARD_STOP" else
               "Escalation policy tightened; the experiment continues."),
            no_human_action_needed=name != "HARD_STOP",
        )

    # --------------------------------------------------------------- observer

    def run_observer(self) -> None:
        """Bounded, disposable, and outside the state transaction.

        An Observer job can burn its full timeout twice over. Observation must
        never hold the state lock, and Observer failure never blocks development.
        """
        if time.monotonic() - self._last_observer < OBSERVER_INTERVAL_SECONDS:
            return
        snapshot = self.store.read()
        if not providers.may(snapshot, "observation"):
            return
        self._last_observer = time.monotonic()
        spec = self.cfg.observer
        evidence = self.observer_evidence(snapshot)
        evidence_path = config.OBSERVER_DIR / "evidence.json"
        config.OBSERVER_DIR.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

        prompt_text = prompts.observer(str(evidence_path), self.label(snapshot))
        prompt_path = config.OBSERVER_DIR / "prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        command = ["grok", "--prompt-file", str(prompt_path), "--output-format", "json",
                   "--disable-web-search", "--max-turns", str(spec["max_turns"]),
                   "--cwd", str(config.REPO_ROOT)]
        if spec.get("model"):
            command += ["-m", spec["model"]]

        result = workers.run_bounded(command, str(config.REPO_ROOT),
                                     spec["soft_timeout_seconds"],
                                     spec["hard_timeout_seconds"])
        if not result["ok"] and not result["timed_out"]:
            result = workers.run_bounded(command, str(config.REPO_ROOT),
                                         spec["soft_timeout_seconds"],
                                         spec["hard_timeout_seconds"])

        report_path = config.OBSERVER_DIR / f"report-{int(time.time())}.json"
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if result["ok"]:
            with self.store.transaction() as doc:
                providers.record_success(doc, "grok", self.tz)
        self.log("OBSERVATION", role="observer", provider="grok",
                 model=spec.get("model"), duration_ms=result["duration_ms"],
                 outcome="SUCCESS" if result["ok"] else "FAILED",
                 activity_class="OBSERVATION",
                 metadata_redacted={"timed_out": result["timed_out"],
                                    "soft_timeout_exceeded": result["soft_timeout_exceeded"],
                                    "report": str(report_path)})

    def observer_evidence(self, doc: dict) -> dict:
        """Raw evidence only - never a builder's own summary of its work."""
        return {
            "clock": self.label(doc),
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

    # ----------------------------------------------------- deadline and freeze

    def checkpoints(self, doc: dict, cs: clock.ClockState) -> None:
        for hour in clock.CHECKPOINT_HOURS:
            if cs.elapsed_hours >= hour and hour not in doc["checkpoints_sent"]:
                doc["checkpoints_sent"].append(hour)
                completed = [t["id"] for t in doc["tasks"].values()
                             if t["state"] == "COMPLETE"]
                self.log("CHECKPOINT", outcome=f"T+{hour}",
                         activity_class="ORCHESTRATION",
                         metadata_redacted={"completed": completed,
                                            "budget": budget.summary(doc)})
                self.notify_out(
                    doc, notify.INFO, f"Checkpoint T+{hour}h",
                    f"Complete: {', '.join(completed) or 'none yet'}. "
                    f"Phase {cs.phase}. Review queue "
                    f"{state_mod.review_queue_depth(doc)}.",
                )

    def freeze(self, doc: dict) -> None:
        if doc.get("frozen_at"):
            return
        doc["frozen_at"] = clock.iso(self.now())
        snapshot = {
            "frozen_at": doc["frozen_at"],
            "tasks": {t["id"]: t["state"] for t in doc["tasks"].values()},
            "prs": {k: {"merged": v.get("merged"),
                        "review_verdict": v.get("review_verdict")}
                    for k, v in doc["prs"].items()},
            "providers": {p: r["state"] for p, r in doc["providers"].items()},
            "budget": budget.summary(doc),
            "counters": doc["counters"],
            "head_sha": gh.git(["rev-parse", "HEAD"], str(config.REPO_ROOT)).stdout,
        }
        (config.RUNTIME_DIR / "freeze-snapshot.json").write_text(
            json.dumps(snapshot, indent=2), encoding="utf-8"
        )
        for worker in list(doc["workers"]):
            workers.close_worker(worker)
        doc["workers"].clear()
        self.log("EXPERIMENT_FROZEN", outcome="FROZEN", activity_class="ORCHESTRATION",
                 metadata_redacted=snapshot)
        self.telemetry.span("experiment.frozen", {"run002.phase": "FROZEN"})
        self.notify_out(doc, notify.INFO, "EXPERIMENT_FROZEN — T+24 reached",
                        "Dispatch stopped, merges blocked, state snapshotted. "
                        "Unfinished work is left unfinished by design.")

    # ------------------------------------------------------------------- loop

    def tick(self) -> None:
        """One control cycle.

        The state transaction holds an exclusive lock and is rolled back if
        anything inside it raises, while ledger events are written immediately.
        Slow network work therefore stays outside it: GitHub is polled before the
        lock is taken, and Jev and the Observer run after it is released.
        """
        open_prs = gh.list_open_prs(self.cfg.github_repo)

        with self.store.transaction() as doc:
            providers.ensure(doc)
            budget.ensure(doc, self.cfg.budget_usd)
            migration_lock.ensure(doc)

            for provider, before, after in providers.refresh(doc, self.tz):
                self.log("PROVIDER_STATE_CHANGE", provider=provider, state_before=before,
                         state_after=after, activity_class="ORCHESTRATION", outcome=after)
                self.notify_out(doc, notify.ATTENTION, f"{provider} recovered to {after}",
                                "Cooldown elapsed; queued work resumes.",
                                no_human_action_needed=True)
                for task in doc["tasks"].values():
                    if task["state"] == "WAITING_PROVIDER_RESET":
                        self.transition(doc, task["id"], "READY", f"{provider} available")

            cs = self.clock_state(doc)
            self.heartbeat(doc)
            if cs is None:
                return

            if cs.expired:
                self.freeze(doc)
                return

            self.reap_workers(doc)
            self.route_prs(doc, cs, open_prs)
            self.detect_stale(doc)

            for task in self.dispatchable(doc, cs):
                self.dispatch_builder(doc, task)

            depth = state_mod.review_queue_depth(doc)
            if state_mod.builder_limit(depth, self.cfg.max_builders) < self.cfg.max_builders:
                doc["counters"]["throttle_events"] += 1
                self.log("BUILDER_THROTTLED", outcome="THROTTLED",
                         activity_class="ORCHESTRATION",
                         metadata_redacted={"review_queue_depth": depth})

            self.checkpoints(doc, cs)

        # Outside the lock: a slow provider must never stall the control cycle.
        # Each is declared as bounded busy work so a healthy supervisor is not
        # mistaken for a dead one while it waits on a provider.
        self.run_declared("jev", JEV_BOUND_SECONDS, self.consult_jev)
        observer_bound = (self.cfg.observer["hard_timeout_seconds"]
                          * (2 if self.cfg.observer.get("retry_once_fresh") else 1)
                          + BUSY_MARGIN_SECONDS)
        self.run_declared("observer", observer_bound, self.run_observer)

    def run_declared(self, what: str, bound_seconds: float, action) -> None:
        """Run bounded slow work with its deadline published in the heartbeat."""
        self.declare_busy(what, bound_seconds)
        try:
            action()
        finally:
            self.clear_busy()

    def run(self) -> int:
        config.ensure_runtime_dirs()

        # Exactly one supervisor. Two would both dispatch, and the state lock
        # only serialises their writes - it does not stop them duplicating work.
        if config.PID_PATH.exists():
            try:
                incumbent = int(config.PID_PATH.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                incumbent = None
            if incumbent and incumbent != os.getpid() and proc.is_running(incumbent):
                self.log("SUPERVISOR_START_REFUSED", outcome="ALREADY_RUNNING",
                         activity_class="ORCHESTRATION",
                         metadata_redacted={"incumbent_pid": incumbent,
                                            "this_pid": os.getpid()})
                return 1

        config.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

        def stop(_signum, _frame):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        self.log("SUPERVISOR_STARTED", outcome="RUNNING", activity_class="ORCHESTRATION",
                 metadata_redacted={"pid": os.getpid()})
        while not self.stopping:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive its own bugs
                self.log("SUPERVISOR_ERROR", outcome="ERROR", activity_class="FAILED_WORK",
                         metadata_redacted={"error": repr(exc)[:600]})
            self._interruptible_sleep(self.cfg.poll_seconds)
        self.log("SUPERVISOR_STOPPED", outcome="STOPPED", activity_class="ORCHESTRATION")
        return 0

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep in short steps so a stop signal is honoured promptly."""
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(1)


def main() -> int:
    return Supervisor().run()


if __name__ == "__main__":
    raise SystemExit(main())
