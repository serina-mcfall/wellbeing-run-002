"""The deterministic supervisor.

Not an LLM. It owns task state, dependencies, provider state, budget,
concurrency, guardrails, PR routing, deadline phases, notifications and the
ledger. Claude, Codex, Grok and Jev advise or execute work; this process decides
and records.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from . import (
    accessibility_contract,
    accessibility_evidence,
    budget,
    clock,
    config,
    debt,
    evidence,
    gate_evidence,
    gh,
    intervention,
    jev,
    ledger as ledger_mod,
    merge_invariant,
    migration_lock,
    notify,
    proc,
    prompts,
    providers,
    redact,
    routing,
    security_contract,
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

# ====================================================================
# C-18 stage 7: the declare_busy bounds for the post-T1 phases
# ====================================================================
#
# WHY EVERY POST-T1 PHASE NEEDS ONE. The heartbeat is written inside T1.
# `watchdog.heartbeat_fresh` is an OR: fresh if the last beat is younger
# than `heartbeat_stale_seconds` (120) OR if now is inside a declared
# `busy_until`. `declare_busy` does NOT refresh `at` - it only adds the
# window. So the 120 s of ordinary freshness is spent by EVERYTHING after
# T1 together: dispatch execution, security execution, accessibility
# execution, the merges, the drains, Jev and the Observer. A phase that
# can outlast what is left of that window and does not declare one is a
# healthy supervisor that looks dead.
#
# Because the test is an OR, a declaration can only ever EXTEND freshness.
# It cannot shorten it, so declaring never makes detection stricter than
# the plain 120 s - which is why adding these is safe in the direction
# that matters.
#
# THE BOUND IS THE PHASE'S OWN WORST CASE, NOT THE WORKER'S LEASE. This is
# the correction that matters, and an earlier proposal got it wrong: it
# priced builder execution at `timeouts.builder` (3600). The builder lease
# is how long the WORKER may run. `_execute_builder_dispatch` writes a
# prompt, creates a worktree, probes ports, writes a job file and calls
# `workers.start_job`, which `Popen`s and returns - it never waits for the
# worker. Bounding it at 3660 s would let a wedged `workmux add` look
# healthy for an hour against a 120 s threshold, which is precisely the
# failure declare_busy exists to prevent. Every bound below is the sum of
# the external timeouts the phase can actually incur, read from the
# constants those calls use, so raising a timeout raises its bound too.
#
# THEY ARE PER ITEM, AND EVERY PHASE IS A BATCH. `execute_dispatches`,
# `execute_security`, `execute_accessibility` and `execute_merges` each
# loop. A single fixed number would under-bound a batch of two. The bound
# is `per item x count + margin`, and it stays finite because the item
# count is capped by the governed concurrency limits.

# One dispatch or security execution: `git worktree list` inside
# acquire_worktree, a `workmux add`, a `workmux path`, and start_job's
# `tmux list-panes` + `tmux send-keys`.
DISPATCH_EXECUTE_BOUND_PER_PLAN = (
    gh.TIMEOUT                      # 120 - git worktree list
    + workers.WORKMUX_TIMEOUT       # 180 - workmux add
    + workers.WORKMUX_PATH_TIMEOUT  # 30  - workmux path
    + 2 * workers.TMUX_TIMEOUT      # 40  - list-panes, send-keys
)                                   # = 370

# One merge candidate: route_prs' pr_view, then attempt_merge's
# update_branch, pr_view, merge and the post-merge pr_view.
MERGE_EXECUTE_BOUND_PER_CANDIDATE = 6 * gh.TIMEOUT   # = 720

# One notification drain. DRAIN_BUDGET_SECONDS bounds when a send may
# START, not when the drain returns: `drain_notifications`' own docstring
# records the honest bound as "the budget, plus at most one in-flight
# send", because http.post_json's timeout is per socket operation rather
# than per request. That is what this adds, and it is the stated honest
# bound rather than a hard cap - tightening it needs a different
# transport, not a smaller number here.
DRAIN_BOUND_SECONDS = 10.0 + 20.0   # DRAIN_BUDGET_SECONDS + http.post_json default


def batch_bound(per_item: float, count: int) -> float:
    """A declared bound for a phase that loops: per item, plus one margin.

    Never open-ended, and never zero-width: a batch of nothing is not
    declared at all by the call sites, so `count` is always at least one
    where this is used.
    """
    return per_item * max(count, 1) + BUSY_MARGIN_SECONDS


# ====================================================================
# C-18: the dispatch harness
# ====================================================================
#
# THE PROBLEM. Every dispatch used to write a job file, run git, open a
# socket and spawn a process while the exclusive state lock was held, so one
# slow subprocess stalled every task in the run. C-18's remediation is the
# shape C-14.1 already uses for merges and C-05.3a already uses for security
# evidence: CLAIM in state, do the external work with NO lock held, COMMIT
# the result in a separate transaction that re-verifies what it acted on.
#
# THE INTERFACE, for stages 5 (reviewer) and 6 (fixer). Each role supplies
# exactly three methods and ONE entry in `Supervisor.DISPATCH_HANDLERS`.
# Nothing else in the pass changes - that is the whole point of the table.
#
#   1. PLAN, inside T1, STATE ONLY.
#      Build a `state.new_dispatch_claim(...)`, store it at
#      `task["dispatch_claims"][role]`, then call
#      `self._plan_dispatch(self.dispatch_plan(claim))`.
#      `_plan_dispatch` is an accumulator rather than a return value
#      precisely because reviewer and fixer plans originate inside
#      `reap_workers` callbacks, which cannot return anything to `tick`.
#
#   2. EXECUTE, after T1 commits, NO LOCK HELD.
#      `execute(plan: DispatchPlan) -> DispatchResult`.
#      May do anything external. MUST NOT touch the state document: by the
#      time it runs, the document T1 read has committed and moved on.
#      Everything it needs travels on the frozen `DispatchPlan`.
#
#   3. COMMIT / FAIL, inside a state transaction, STATE ONLY.
#      `commit(doc, plan, result)` and `fail(doc, plan, result)`.
#      Both run under a lock - `confirm_dispatches`' own per-result
#      transaction on the ordinary path, and T1 itself on the recovery
#      path - so they must be state-only and re-entrant. Both MUST re-verify
#      identity with `_current_dispatch_claim` before changing anything, and
#      both MUST clear the claim they acted on.
#
# RECOVERY is generic and already written. `observe_dispatch_claims` reads
# each claim's job file before the lock; `resume_dispatch_claims` then either
# commits a claim whose job file proves it spawned, or re-plans one that
# never got that far. A role gets this for free by storing a claim.
#
# WHAT A CLAIM BUYS. It reserves the role's slot durably, so
# `route_awaiting_dispatch` (through `has_worker`) and `dispatchable` both
# see a planned-but-unspawned dispatch and refuse to start a second one.


@dataclass(frozen=True)
class DispatchPlan:
    """One claimed dispatch, as it crosses OUT of the state transaction.

    Frozen, and carrying only immutable values, for the same reason
    `routing.SecurityPlan` is: the execute phase runs after T1 committed, so
    a reference into that transaction's document would be read - or mutated -
    with no lock held. `context` is a read-only mapping of the role-specific
    facts only that role's own three methods interpret.
    """
    role: str
    task_id: str
    pr: int | None
    worker: str
    branch: str
    port_candidates: tuple[int, ...] = ()
    observed_head: str | None = None
    context: Mapping = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class DispatchObservation:
    """What the filesystem says about one claim, read BEFORE the lock.

    `job_file` is the spawn evidence, exactly as it is in C-05.3a: the job
    file is written immediately before `start_job`, so its presence means the
    external phase got far enough that a worker may exist and must never be
    started again. `worktree` and `port` are read back from that same file,
    which is why a commit lost to a crash can still be reconstructed.

    `worker_live` is None when the /proc scan itself failed - unknown, never
    "not running".
    """
    task_id: str
    role: str
    worker: str
    job_file: bool
    worktree: str | None = None
    port: int | None = None
    worker_live: bool | None = None


# C-18 stage 5. The FINITE diagnostic vocabulary of the pre-dispatch
# re-verification. Every reason a review may be deferred immediately before
# dispatch is one of these codes and nothing else - never an exception string,
# which is the C-16 rule that exception prose must not reach durable evidence.
# `_review_preflight` returns one of these, or "" to mean "proceed".
REVIEW_PREFLIGHT_CODES = (
    "PR_NOT_FOUND",             # GitHub returned nothing for this number
    "PR_NOT_OPEN",              # closed or merged since the observation
    "PR_ASSOCIATION_MISMATCH",  # the pull request no longer tracks this branch
    "HEAD_MOVED",               # the commit the claim is bound to is superseded
    "READY_FOR_REVIEW_REFUSED",  # the draft-to-ready call was refused
)


@dataclass(frozen=True)
class ReviewObservation:
    """What GitHub says about one pull request, read BEFORE the lock.

    C-18 stage 5. `head` is the commit a review dispatched now would be cut
    from and `diff_hash` the material diff at that same instant; both used to
    be fetched from inside T1, which is the defect. Either may be None when
    GitHub could not answer - a `None` head is a dispatch failure, never a
    reason to review something else.

    The pair is deliberately taken at ONE moment: the diff hash describes the
    head beside it, and the head is re-proved unchanged before any worker
    starts, so a committed `reviewed_diff_hash` always describes the
    committed `reviewed_head`.
    """
    pr_number: int
    head: str | None
    diff_hash: str | None


@dataclass(frozen=True)
class DispatchResult:
    """What the external phase actually achieved, for the commit phase."""
    plan: DispatchPlan
    ok: bool
    worktree: str | None = None
    port: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class _DispatchHandler:
    """One role's three methods, by name.

    Names rather than bound methods so the table can be a class attribute
    that a subclass or a test may override, and so `Supervisor` does not have
    to build it in `__init__`.
    """
    execute: str
    commit: str
    fail: str


def jev_consultation_identity(task: dict) -> str:
    """Which worker_health consultation this is - C-19, §25.4.

    This is the key the duplicate guard uses, so what it includes decides two
    things at once: a lost consultation must be recognisable when it is
    retried, and a consultation that is genuinely a NEW question must not be
    mistaken for that retry.

    Four durable task fields, each covering a change the others miss:

    - `attempts`   a re-dispatched BUILDER. The builder's worker id is
                   `<task>-builder` for every attempt, so nothing else moves.
    - `worker`     a re-dispatched fixer or reviewer, whose worker id carries
                   its own cycle number while `attempts` stays put.
    - `progress_marker`  the agent's output byte count at its last observed
                   progress, written by `reap_workers` ONLY when it differs
                   from the stored one. This is the record's one durable
                   marker of the worker having actually done something.
    - `state`      a durable transition. ACTIVE and REVIEW are different
                   questions about the same worker, and the criteria say so.

    Deliberately absent, because the question is unchanged when they move:
    `minutes_since_progress` (recomputed from the clock on every tick),
    `last_progress_at` (the timestamp of the same event `progress_marker`
    already identifies, without being a clock reading), `review_queue_depth`
    (another part of the run entirely), and `updated_at`.

    The consequence worth naming: a STALLED worker emits no new output, so its
    marker does not move and the lost consultation stays suppressed - which is
    exactly the case the suppression exists for. A worker that is genuinely
    progressing produces a different question, and gets one.
    """
    worker = task.get("worker")
    marker = task.get("progress_marker")
    return (f"jev:worker_health:{task['id']}"
            f":a{task.get('attempts')}"
            f":w{worker if worker is not None else 'none'}"
            f":p{marker if marker is not None else 'none'}"
            f":{task.get('state')}")


class _DrainAllowance:
    """One tick's total notification-delivery allowance.

    The governed limits are PER TICK, and a tick drains twice. Holding the
    deadline and the attempt count here - rather than recreating them inside
    drain_notifications - is what stops the two drains between them spending
    twice the approved time and twice the approved attempts.
    """

    __slots__ = ("deadline", "attempts_left")

    def __init__(self, budget_seconds: float, max_attempts: int) -> None:
        self.deadline = time.monotonic() + budget_seconds
        self.attempts_left = max_attempts

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

# Why a security attempt's worktree sits in a task's retained_worktrees map.
# The map's contract is "this path has a durable owner"; this value says which
# lifecycle owns it, so Phase F can tell its own entries from a builder's.
SECURITY_WORKTREE_WHY = "SECURITY_ATTEMPT"


def _reported_pid() -> int | None:
    """The pid the incumbent last reported, for the refusal record only.

    Never used to decide exclusion - that is the flock's job. This exists so
    a refused start names who holds the lock instead of saying only that
    someone does.
    """
    try:
        return int(config.PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


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
        # C-18a: WHY the loop stopped, for the durable SUPERVISOR_STOPPED
        # event. Run 002 is now itself the endurance experiment, so the
        # stopping reason is evidence rather than a log line - an early
        # stop must be reconstructible as an early stop, and must never be
        # readable as a completed 24-hour run. None until a signal
        # arrives; see _stop_reason for what each outcome means.
        self._stop_signal: int | None = None
        # C-05.3b. The factory that supplies one attempt's external
        # services - install, build, serve, scan, tear down. It is None
        # here and that is the honest state: no product exists to build
        # before the first product PR, so nothing can run an attempt yet.
        # The planning path refuses to claim while it is None rather than
        # minting claims that could never be executed, which would leak a
        # port per tick. Injecting a factory is what turns the automated
        # half on, and it is also how every test drives it.
        self.accessibility_services_factory = None
        # Accumulated inside T1 by plan_accessibility_auto, executed after
        # it commits. Reset per tick, like _dispatch_plans.
        self._accessibility_plans: list = []
        # C-18: this tick's planned dispatches, accumulated inside T1 by
        # _plan_dispatch and executed after it commits. Reset at the top of
        # every tick - the durable claim, not this list, survives a crash.
        self._dispatch_plans: list[DispatchPlan] = []
        # Held open for the process lifetime once run() takes it. Closing it,
        # or letting it be collected, releases the singleton lock.
        self._singleton_lock = None
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
        """Queue one notification. C-18 stage 2: this NO LONGER DELIVERS.

        The send used to happen right here, synchronously, on whatever
        transaction the caller held - which is the defect C-18 names. The
        intent is now committed with the state change that produced it and
        delivered later by drain_notifications(), holding no lock.

        What is deliberately unchanged: the caller's content, the
        human-intervention count (incremented here, inside the caller's
        transaction, so the count can never disagree with the state change
        that earned it), and Protocol v2 §Discord's "ledger append succeeds
        before Discord notification" - which this strengthens, since the
        intent is durable at least one commit before any send.

        The return value reports QUEUING, not delivery. Nothing in the
        control plane reads it; claiming {"ok": True} would be a delivery
        claim this method can no longer make.
        """
        intent_id = f"NTF-{secrets.token_hex(8)}"
        try:
            intent = notify.new_intent(
                severity=severity, title=title, body=body,
                no_human_action_needed=no_human_action_needed,
                clock_label=self.label(doc), created_at=clock.iso(self.now()))
        except ValueError as exc:
            # Refusing to PERSIST is new; refusing to SEND is not. Notifier
            # .send already blocks a secret-bearing message, but it never had
            # to consider durability because nothing was stored. Only the
            # finite reason is recorded - never the offending text.
            reason = str(exc) if str(exc) == "BLOCKED_SECRET_IN_NOTIFICATION" \
                else "INTENT_INVALID"
            self.log("NOTIFICATION_BLOCKED", activity_class="ORCHESTRATION",
                     outcome=reason,
                     metadata_redacted={"severity": severity,
                                        "intent_id": intent_id})
            return {"queued": False, "reason": reason}

        notify.queue(doc)[intent_id] = intent
        self.log(
            "NOTIFICATION_QUEUED",
            activity_class="ORCHESTRATION",
            outcome="QUEUED",
            human_intervention=severity == notify.HUMAN_REQUIRED,
            metadata_redacted={"severity": severity, "title": intent["title"],
                               "intent_id": intent_id},
        )
        if severity == notify.HUMAN_REQUIRED:
            doc["counters"]["human_interventions"] += 1
        return {"queued": True, "intent_id": intent_id}

    # ------------------------------------------- C-18 stage 2: the drain

    # A tick may spend at most this long delivering, and attempt at most this
    # many sends. Both bind: the time budget is what actually bounds a hanging
    # destination (a count of 20 against a 20s HTTP timeout is 400 seconds),
    # and the count is what stops a large cleared backlog monopolising a tick
    # of fast successes. The supervisor polls every 30s and the watchdog calls
    # a heartbeat stale at 120s, so 10s leaves room for the rest of the tick.
    DRAIN_BUDGET_SECONDS = 10.0
    DRAIN_MAX_ATTEMPTS = 20
    # Below this there is not enough budget left for a send to mean anything,
    # and starting one would overrun the budget rather than respect it.
    MIN_SEND_SECONDS = 0.5

    def new_drain_allowance(self) -> "_DrainAllowance":
        """One tick's TOTAL delivery allowance.

        The governed limits are per tick, not per call, and a tick drains
        twice - once after the merges and once after the slow provider work.
        Both share this object, so the two drains cannot between them spend
        twice the approved time or twice the approved attempts.
        """
        return _DrainAllowance(self.DRAIN_BUDGET_SECONDS, self.DRAIN_MAX_ATTEMPTS)

    def drain_notifications(self, allowance: "_DrainAllowance | None" = None) -> dict:
        """Deliver queued intents. NO STATE LOCK IS HELD DURING ANY SEND.

        `allowance` is the tick's shared budget. Omitting it - which is what a
        standalone `ctl freeze` does - mints a fresh one, because a one-shot
        command has no tick to share with.

        WHAT THE BUDGET ACTUALLY BOUNDS, precisely. It bounds when a new send
        may START: no send begins once the allowance is spent, and each send
        is given only the time that remains, recomputed after the fence
        transaction rather than before it. It does NOT guarantee the drain
        returns within the budget. `http.post_json` passes its timeout to
        `urllib.request.urlopen`, which applies it to each individual socket
        operation - connect, and each read - not to the request as a whole,
        so a response that trickles in slowly can exceed it in total. The
        honest bound is therefore: the budget, plus at most one in-flight
        send. Tightening that further needs a different transport, not a
        different number here.
        """
        if not self.store.exists():
            return {"attempted": 0, "delivered": 0, "failed": 0,
                    "pending": 0, "oldest_pending_age_seconds": None}
        allowance = allowance or self.new_drain_allowance()
        self._converge_notifications()

        attempted = delivered = failed = skipped = 0
        while allowance.attempts_left > 0:
            if allowance.remaining() < self.MIN_SEND_SECONDS:
                break
            # The durable pre-send fence, one short transaction per intent.
            # Fencing lazily rather than in a batch keeps the window between
            # authorising a send and making it as small as it can be.
            fenced = self._fence_next_notification()
            if fenced is None:
                break
            allowance.attempts_left -= 1

            # Recomputed AFTER the fence: that transaction waits on the state
            # lock and can itself consume the remainder. Passing the
            # pre-fence figure would hand a send more time than the tick has.
            remaining = allowance.remaining()
            if remaining < self.MIN_SEND_SECONDS:
                # Fenced but never sent. We KNOW no external effect exists,
                # so this must not be left suppressed under the ambiguity
                # rule - it is released back to PENDING, durably, and
                # retried on the next tick.
                self._release_unsent_notification(fenced)
                skipped += 1
                break
            attempted += 1
            if self._deliver_notification(fenced, timeout=remaining):
                delivered += 1
            else:
                failed += 1

        summary = {"attempted": attempted, "delivered": delivered,
                   "failed": failed, "skipped_no_budget": skipped}
        summary.update(self._notification_backlog())
        if attempted or skipped or summary["pending"]:
            self.log("NOTIFICATION_DRAIN", activity_class="ORCHESTRATION",
                     outcome="DRAINED" if not summary["pending"] else "BACKLOG",
                     metadata_redacted=dict(summary))
        return summary

    def _release_unsent_notification(self, fenced: dict) -> None:
        """Undo a fence whose send the allowance refused to start.

        The ledger append comes first and is what makes this restart-safe:
        if the state commit below is lost, convergence finds NOT_ATTEMPTED
        and releases the intent anyway. Recorded as its own outcome rather
        than as FAILED, because nothing failed - nothing was tried - so no
        backoff is applied and the intent is eligible again immediately.
        """
        self.log("NOTIFICATION", activity_class="ORCHESTRATION",
                 outcome="NOT_ATTEMPTED",
                 metadata_redacted={"intent_id": fenced["intent_id"],
                                    "attempt": fenced["attempt"],
                                    "reason": "DRAIN_BUDGET_EXHAUSTED"})
        try:
            with self.store.transaction() as doc:
                intent = notify.queue(doc).get(fenced["intent_id"])
                if intent and intent.get("status") == notify.ATTEMPTING \
                        and intent.get("attempt") == fenced["attempt"]:
                    intent["status"] = notify.PENDING
                    intent["last_outcome"] = "NOT_ATTEMPTED"
        except Exception:  # noqa: BLE001
            self.log("NOTIFICATION_DRAIN_ERROR", activity_class="ORCHESTRATION",
                     outcome="ERROR",
                     metadata_redacted={"phase": "RELEASE",
                                        "error_code": "RELEASE_FAILED"})

    def _converge_notifications(self) -> None:
        """Repair ATTEMPTING intents from durable ledger evidence.

        The state commit that records an outcome can be lost while the ledger
        append that proves it survives - that is the crash window between
        delivering and recording the delivery. The ledger is therefore the
        authority: a DELIVERED event settles the intent permanently, and a
        FAILED event for the attempt that is currently fenced makes it
        retryable. An intent with NEITHER stays ATTEMPTING, which is the
        ambiguous case, and stays suppressed.
        """
        try:
            with self.store.transaction() as doc:
                now = self.now()
                for intent_id, intent in notify.queue(doc).items():
                    if intent.get("status") != notify.ATTEMPTING:
                        continue
                    if self._notification_event(intent_id, "DELIVERED"):
                        intent["status"] = notify.DELIVERED
                        intent["last_outcome"] = "DELIVERED"
                    elif self._notification_event(intent_id, "NOT_ATTEMPTED",
                                                  attempt=intent.get("attempt")):
                        # Fenced, then the allowance ran out before the send
                        # began. No external effect exists, so this is freely
                        # retryable and earns no backoff.
                        intent["status"] = notify.PENDING
                        intent["last_outcome"] = "NOT_ATTEMPTED"
                    elif self._notification_event(intent_id, "FAILED",
                                                  attempt=intent.get("attempt")):
                        intent["status"] = notify.PENDING
                        intent["last_outcome"] = "FAILED"
                        intent["next_attempt_at"] = clock.iso(
                            now + timedelta(seconds=notify.backoff_seconds(
                                intent.get("attempt", 1))))
        except Exception:  # noqa: BLE001 - convergence is best-effort repair
            # Finite structural metadata only, for the same reason
            # watchdog.annunciate_orphans gives: runtime prose can carry
            # anything and no external effect came from this transaction.
            self.log("NOTIFICATION_DRAIN_ERROR", activity_class="ORCHESTRATION",
                     outcome="ERROR",
                     metadata_redacted={"phase": "CONVERGE",
                                        "error_code": "CONVERGE_FAILED"})

    def _notification_event(self, intent_id: str, outcome: str,
                            attempt: int | None = None) -> bool:
        """Whether the ledger durably records this outcome for this intent."""
        inspection = self.ledger.inspect(event_types=("NOTIFICATION",))
        for event in inspection.events:
            meta = event.get("metadata_redacted") or {}
            if meta.get("intent_id") != intent_id:
                continue
            if event.get("outcome") != outcome:
                continue
            if attempt is not None and meta.get("attempt") != attempt:
                continue
            return True
        return False

    def _fence_next_notification(self) -> dict | None:
        """Commit ATTEMPTING for the oldest due intent, and return a copy.

        The copy is plain scalars: the document it came from is stale the
        moment this transaction commits, and the send happens after that.
        """
        try:
            with self.store.transaction() as doc:
                now = self.now()
                for intent_id, intent in notify.due_intents(doc, now):
                    intent["status"] = notify.ATTEMPTING
                    intent["attempt"] = intent.get("attempt", 0) + 1
                    intent["last_attempt_at"] = clock.iso(now)
                    return {"intent_id": intent_id,
                            "severity": intent["severity"],
                            "title": intent["title"],
                            "body": intent.get("body", ""),
                            "no_human_action_needed": bool(
                                intent.get("no_human_action_needed")),
                            "clock_label": intent.get("clock_label"),
                            "attempt": intent["attempt"]}
                return None
        except Exception:  # noqa: BLE001
            self.log("NOTIFICATION_DRAIN_ERROR", activity_class="ORCHESTRATION",
                     outcome="ERROR",
                     metadata_redacted={"phase": "FENCE",
                                        "error_code": "FENCE_FAILED"})
            return None

    def _deliver_notification(self, fenced: dict, *, timeout: float) -> bool:
        """One send, with no lock held, plus its durable outcome.

        Returns whether it was delivered. An exception after the send began
        is AMBIGUOUS: the message may have gone out, so no failure is
        recorded and the intent stays ATTEMPTING - suppressed rather than
        resent. Only a durably recorded failure earns another attempt.
        """
        intent_id = fenced["intent_id"]
        send_started = False
        try:
            send_started = True
            result = self.notifier.send(
                fenced["severity"], fenced["title"], fenced["body"],
                no_human_action_needed=fenced["no_human_action_needed"],
                clock_label=fenced["clock_label"],
                timeout=timeout) or {}
        except Exception:  # noqa: BLE001
            if send_started:
                self.log("NOTIFICATION", activity_class="ORCHESTRATION",
                         outcome="AMBIGUOUS",
                         metadata_redacted={"intent_id": intent_id,
                                            "attempt": fenced["attempt"],
                                            "error_code": "SEND_OUTCOME_UNKNOWN"})
            return False

        ok = bool(result.get("ok"))
        # A transport that could not establish what happened is NOT a failure.
        # A read timeout means the webhook may have been delivered in full and
        # only the answer lost; recording that as FAILED would retry it and
        # deliver twice. Only the transport can tell these apart, so it says
        # so and this honours it.
        if not ok and result.get("ambiguous"):
            self.log("NOTIFICATION", activity_class="ORCHESTRATION",
                     outcome="AMBIGUOUS",
                     metadata_redacted={"intent_id": intent_id,
                                        "attempt": fenced["attempt"],
                                        "error_code": "SEND_OUTCOME_UNKNOWN",
                                        "status": result.get("status")})
            return False        # stays ATTEMPTING, and so stays suppressed

        # The ledger append is what survives a lost state commit, so it comes
        # first and the state update is only a cache of it.
        self.log(
            "NOTIFICATION", activity_class="ORCHESTRATION",
            outcome="DELIVERED" if ok else "FAILED",
            human_intervention=fenced["severity"] == notify.HUMAN_REQUIRED,
            metadata_redacted={"intent_id": intent_id,
                               "attempt": fenced["attempt"],
                               "severity": fenced["severity"],
                               "title": fenced["title"],
                               "delivery": redact.scrub(result)},
        )
        self._record_notification_outcome(intent_id, fenced["attempt"], ok)
        return ok

    def _record_notification_outcome(self, intent_id: str, attempt: int,
                                     ok: bool) -> None:
        """Cache the ledger's verdict in state. Losing this commit is
        survivable - _converge_notifications rebuilds it from the ledger."""
        try:
            with self.store.transaction() as doc:
                intent = notify.queue(doc).get(intent_id)
                if not intent or intent.get("status") != notify.ATTEMPTING \
                        or intent.get("attempt") != attempt:
                    return          # superseded; the ledger still governs
                now = self.now()
                if ok:
                    intent["status"] = notify.DELIVERED
                    intent["last_outcome"] = "DELIVERED"
                    intent["delivered_at"] = clock.iso(now)
                else:
                    intent["status"] = notify.PENDING
                    intent["last_outcome"] = "FAILED"
                    intent["next_attempt_at"] = clock.iso(
                        now + timedelta(seconds=notify.backoff_seconds(attempt)))
        except Exception:  # noqa: BLE001
            self.log("NOTIFICATION_DRAIN_ERROR", activity_class="ORCHESTRATION",
                     outcome="ERROR",
                     metadata_redacted={"phase": "RECORD",
                                        "error_code": "RECORD_FAILED"})

    def _notification_backlog(self) -> dict:
        """Pending count and oldest pending age. Nothing is trimmed in this
        stage, so these are how a growing queue becomes visible."""
        try:
            return notify.backlog(self.store.read(), self.now())
        except Exception:  # noqa: BLE001
            return {"pending": None, "oldest_pending_age_seconds": None}

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

    def request_intervention(self, doc: dict, *, type_: str, condition_code: str,
                             reason: str, title: str, body: str,
                             task_id: str | None = None, pr_id: int | None = None,
                             notify_when_new: bool = True) -> dict:
        """C-08b.2: the one escalation sequence every S1-S8 site repeats.

        Durable record first, ledger evidence second, notification last. A
        deduplicated recurrence returns the already-open record and appends
        no second REQUESTED event and no second notification. `reason` must be
        structurally generated (fixed template + canonical/bounded values);
        free text - stderr, provider output, reviewer prose - stays in the
        notification body only, never in the durable reason.
        """
        record, is_new = intervention.request(
            doc, type_=type_, scope="task" if task_id else "systemic",
            task_id=task_id, reason=reason, condition_code=condition_code,
            tz=self.tz)
        if is_new:
            self.log("HUMAN_INTERVENTION_REQUESTED", task_id=task_id, pr_id=pr_id,
                     outcome="REQUESTED", activity_class="ESCALATION",
                     human_intervention=True,
                     metadata_redacted={
                         "intervention_id": record["id"],
                         "intervention_type": record["type"],
                         "scope": record["scope"],
                         "condition_code": condition_code,
                         "requested_at": record["requested_at"],
                         "reason": record["reason"],
                         "reason_withheld": record["reason_withheld"],
                     })
            if notify_when_new:
                self.notify_out(doc, notify.HUMAN_REQUIRED, title,
                                body + intervention.notification_suffix(record))
        return record

    # --------------------------------------------------------------- guardrails

    def red_guardrail_active(self, doc: dict) -> bool:
        return bool(doc.get("red_guardrail"))

    def raise_red(self, doc: dict, guardrail: str, detail: str) -> None:
        doc["red_guardrail"] = {"guardrail": guardrail, "detail": detail,
                                "at": clock.iso(self.now())}
        doc["counters"]["guardrail_activations"] += 1
        self.log("GUARDRAIL_RED", guardrail=guardrail, outcome="BLOCKED",
                 activity_class="ESCALATION", metadata_redacted={"detail": detail})
        # S7: record the governance obligation durably, but never let dedup
        # rate-limit a RED alert - a second guardrail raised while the first
        # intervention is still open must still be announced. The durable
        # reason is constant: `guardrail` is caller-supplied and unvalidated
        # at this boundary, so it stays in the GUARDRAIL_RED evidence and the
        # notification, never in the persisted reason.
        record = self.request_intervention(
            doc, type_="HUMAN_GOVERNANCE_DECISION", condition_code="red_guardrail",
            reason="Experiment-wide RED guardrail requires a human governance "
                   "decision",
            title=f"RED guardrail: {guardrail}", body=detail, notify_when_new=False)
        self.notify_out(doc, notify.HUMAN_REQUIRED, f"RED guardrail: {guardrail}",
                        detail + intervention.notification_suffix(record))

    # ------------------------------------------------------------- dispatching

    def dispatchable(self, doc: dict, cs: clock.ClockState) -> list[dict]:
        """Dependency-ready tasks permitted by provider policy and backpressure."""
        if cs.expired or doc.get("frozen_at"):
            return []
        if not providers.may(doc, "new_builds") or providers.safe_hold(doc):
            return []

        depth = state_mod.review_queue_depth(doc)
        limit = state_mod.builder_limit(depth, self.cfg.max_builders)
        # C-18: a builder claim is a committed reservation that becomes a
        # builder process, so it consumes a concurrency slot exactly as an
        # ASSIGNED task does. Without this, max_builders would bound only the
        # dispatches that have already committed and the claims in flight
        # would be free - the governed limit would not be a limit.
        claimed = [t for t in doc["tasks"].values()
                   if state_mod.dispatch_claim_active(
                       state_mod.dispatch_claim(t, "builder"))]
        running = len([t for t in doc["tasks"].values()
                       if t["state"] in ("ASSIGNED", "ACTIVE")]) + len(claimed)
        slots = limit - running
        if slots <= 0:
            return []

        claimed_ids = {t["id"] for t in claimed}
        ready: list[dict] = []
        for task in doc["tasks"].values():
            if task["state"] not in ("QUEUED", "READY", "WAITING_PROVIDER_RESET",
                                     "WAITING_DB_LOCK"):
                continue
            if task["id"] in claimed_ids:
                # Already reserved; resume_dispatch_claims drives it, and
                # offering it here would only consume a slot for a task
                # dispatch_builder is going to refuse anyway.
                continue
            if not state_mod.dependencies_met(doc, task):
                continue
            ready.append(task)

        ready.sort(key=lambda t: t["id"])
        return ready[:slots]

    # ------------------------------------------------ C-18 dispatch harness

    #: role -> the three methods that role supplies. Stage 4 added the
    #: builder, stage 5 the reviewer and stage 6 the fixer, each as ONE
    #: entry here plus its own three methods; nothing else in
    #: `execute_dispatches`, `apply_dispatch_result`, `confirm_dispatches`
    #: or `resume_dispatch_claims` changed for any of them. That is the
    #: whole point of the table: stages 5 and 6 were built in parallel in
    #: separate branches and met here in one adjacent-line conflict.
    DISPATCH_HANDLERS: dict[str, _DispatchHandler] = {
        "builder": _DispatchHandler(execute="_execute_builder_dispatch",
                                    commit="_commit_builder_dispatch",
                                    fail="_fail_builder_dispatch"),
        "reviewer": _DispatchHandler(execute="_execute_reviewer_dispatch",
                                     commit="_commit_reviewer_dispatch",
                                     fail="_fail_reviewer_dispatch"),
        "fixer": _DispatchHandler(execute="_execute_fixer_dispatch",
                                  commit="_commit_fixer_dispatch",
                                  fail="_fail_fixer_dispatch"),
    }

    def _plan_dispatch(self, plan: DispatchPlan) -> None:
        """Accumulate one planned dispatch for execution after T1 commits.

        An accumulator rather than a return value because reviewer and fixer
        plans originate inside `reap_workers`' role-finished callbacks, which
        are nested in T1 and have no way to return anything to `tick`. The
        list is reset at the top of every tick, so a plan can never leak from
        one cycle into the next - the durable claim, not this list, is what
        survives a crash.
        """
        self._dispatch_plans.append(plan)

    @staticmethod
    def dispatch_plan(claim: dict) -> DispatchPlan:
        """Copy a committed claim out of T1 as an immutable plan."""
        return DispatchPlan(
            role=claim["role"],
            task_id=claim["task_id"],
            pr=claim.get("pr"),
            worker=claim["worker"],
            branch=claim["branch"],
            port_candidates=tuple(claim.get("port_candidates") or ()),
            observed_head=claim.get("observed_head"),
            context=MappingProxyType(dict(claim.get("context") or {})),
        )

    def observe_dispatch_claims(self, snapshot: dict,
                                entries: dict | None) -> dict:
        """Phase A for dispatch. Read-only, OUTSIDE the state lock.

        Returns {(task_id, role): DispatchObservation} for every ACTIVE
        claim. Reading the job file here rather than in T1 is what keeps the
        recovery path state-only: the transaction decides from an observation
        someone else took, and never touches a file itself.
        """
        observations: dict[tuple[str, str], DispatchObservation] = {}
        for task in (snapshot.get("tasks") or {}).values():
            task_id = task.get("id")
            if not isinstance(task_id, str):
                continue
            claims = task.get(state_mod.DISPATCH_CLAIMS_KEY) or {}
            if not isinstance(claims, dict):
                continue
            for role, claim in claims.items():
                if not state_mod.dispatch_claim_active(claim):
                    continue
                worker = claim.get("worker")
                if not isinstance(worker, str) or not worker:
                    continue
                job_path = config.WORKER_LOG_DIR / f"{worker}.job.json"
                present = job_path.exists()
                worktree = port = None
                if present:
                    try:
                        job = json.loads(job_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        job = {}
                    if not isinstance(job, dict):
                        job = {}
                    if isinstance(job.get("worktree"), str):
                        worktree = job["worktree"]
                    if isinstance(job.get("port"), int) and \
                            not isinstance(job.get("port"), bool):
                        port = job["port"]
                observations[(task_id, role)] = DispatchObservation(
                    task_id=task_id, role=role, worker=worker,
                    job_file=present, worktree=worktree, port=port,
                    worker_live=(worker in entries)
                    if entries is not None else None)
        return observations

    def observe_review_heads(self, snapshot: dict, open_prs: list[dict]) -> dict:
        """Phase A for the reviewer. Read-only, OUTSIDE the state lock.

        Returns {pr_number: ReviewObservation} for every open pull request a
        reviewer could be dispatched against this tick. The `gh.pr_diff_sha`
        and `routing.material_diff_hash` calls `dispatch_reviewer` used to
        make under the lock are made here instead.

        Narrowed the same way the security heads are: a pull request whose
        task is not in a routing state, is already merged, already has a
        reviewer or fixer (worker OR claim), or whose verdict routes it to a
        fixer rather than a reviewer, is not asked about at all. A tick with
        nothing to review therefore adds no GitHub round trip.

        The snapshot is non-authoritative. Everything here is a question, not
        a decision: T1 re-reads under the lock and re-checks the task, the
        record and the verdict before it plans anything. A snapshot whose
        worker map cannot be read yields no observations, so every pull
        request defers rather than being dispatched from an unreadable
        document.
        """
        if not isinstance(snapshot.get("workers"), dict):
            return {}
        open_numbers = {pr.get("number") for pr in open_prs}
        records = snapshot.get("prs") or {}
        wanted: set[int] = set()
        for task in (snapshot.get("tasks") or {}).values():
            if not isinstance(task, dict):
                continue
            pr_number = task.get("pr")
            if not isinstance(pr_number, int) or pr_number not in open_numbers:
                continue
            if task.get("state") not in self.ROUTING_STATES:
                continue
            record = records.get(str(pr_number))
            if not isinstance(record, dict) or record.get("merged"):
                continue
            if task.get("state") == "REVIEW" and \
                    record.get("review_verdict") == routing.REVIEW_PASS and \
                    record.get("approval_current"):
                # route_prs makes this one a merge candidate and never reaches
                # route_awaiting_dispatch at all.
                continue
            if record.get("review_verdict") == routing.REVIEW_FAIL and \
                    record.get("pending_findings"):
                # route_awaiting_dispatch sends this one to the fixer. Asking
                # GitHub for a head nothing will use is a round trip for work
                # nobody is waiting on.
                continue
            if self.has_worker(snapshot, pr_number, ("reviewer", "fixer")):
                continue
            wanted.add(pr_number)

        observations: dict[int, ReviewObservation] = {}
        for pr_number in sorted(wanted):
            head = gh.pr_diff_sha(self.cfg.github_repo, pr_number)
            observations[pr_number] = ReviewObservation(
                pr_number=pr_number, head=head,
                diff_hash=(routing.material_diff_hash(self.cfg.github_repo,
                                                      pr_number)
                           if head else None))
        return observations

    def resume_dispatch_claims(self, doc: dict, observations: dict) -> None:
        """T1. STATE ONLY. Converge claims left behind by a lost commit.

        Two outcomes, and the job file decides between them:

          * The job file EXISTS. The external phase reached the spawn, so a
            worker may be running that nothing owns. Commit it from the claim
            and the job file's own record of the worktree and port - the same
            commit the ordinary path would have made. Re-spawning is refused
            by construction, because this path never plans anything.
          * The job file does NOT exist. Nothing was ever started under this
            claim, so it is re-planned for this tick's execute phase. The
            identity is unchanged: same worker name, same branch, same
            reservation. `workmux add --open-if-exists` makes a repeated
            worktree creation a no-op rather than an error.

        A claim with no observation bound to it is left exactly as it is.
        Nothing may be concluded about a spawn that was never looked at, and
        a claim costs only the role's slot until the next tick observes it.
        """
        for task in sorted(doc.get("tasks", {}).values(),
                           key=lambda t: t.get("id") or ""):
            for role in state_mod.DISPATCH_ROLES:
                claim = state_mod.dispatch_claim(task, role)
                if not state_mod.dispatch_claim_active(claim):
                    continue
                observation = observations.get((task.get("id"), role))
                if observation is None or \
                        observation.worker != claim.get("worker"):
                    self.log("DISPATCH_RESUME_DEFERRED", task_id=task.get("id"),
                             role=role, pr_id=claim.get("pr"),
                             activity_class="ORCHESTRATION",
                             outcome="OBSERVATION_MISSING",
                             metadata_redacted={"worker": claim.get("worker")})
                    continue
                plan = self.dispatch_plan(claim)
                if observation.job_file:
                    self.log("DISPATCH_RECOVERED", task_id=plan.task_id,
                             role=role, pr_id=plan.pr, agent_id=plan.worker,
                             activity_class="ORCHESTRATION",
                             outcome="SPAWN_EVIDENCE_FOUND")
                    self.apply_dispatch_result(
                        doc, DispatchResult(plan=plan, ok=True,
                                            worktree=observation.worktree,
                                            port=observation.port,
                                            reason="RECOVERED_FROM_JOB_FILE"))
                    continue
                if observation.worker_live:
                    # No job file but a live entry: the two disagree, so
                    # nothing is concluded and nothing is re-spawned.
                    self.log("DISPATCH_RESUME_DEFERRED", task_id=plan.task_id,
                             role=role, pr_id=plan.pr, agent_id=plan.worker,
                             activity_class="ORCHESTRATION",
                             outcome="WORKER_LIVE_WITHOUT_JOB_FILE")
                    continue
                self.log("DISPATCH_RESUMED", task_id=plan.task_id, role=role,
                         pr_id=plan.pr, agent_id=plan.worker,
                         activity_class="ORCHESTRATION", outcome="REPLANNED")
                self._plan_dispatch(plan)

    def _dispatch_block(self, doc: dict, role: str) -> str:
        """Why a claimed dispatch may not spawn RIGHT NOW, or "" if it may.

        Re-read after T1 has committed, because the decision that matters is
        the one true at the instant a paid worker would start. A `ctl freeze`
        or a provider entering COOLDOWN lands in the document between the two.

        Every control here already governs this dispatch somewhere else:
        `frozen_at` and `safe_hold` through `dispatchable`, and
        `providers.may` through `dispatchable` (builder), `dispatch_fixer`
        ("new_builds") and `dispatch_reviewer` ("review"). None is invented.
        A document whose controls cannot be read is not a document that
        permits spending.
        """
        if doc.get("frozen_at"):
            return "RUN_FROZEN"
        capability = "review" if role == "reviewer" else "new_builds"
        try:
            if not providers.may(doc, capability):
                return "PROVIDER_PAUSED"
            if role == "builder" and providers.safe_hold(doc):
                return "SAFE_HOLD"
        except (KeyError, TypeError, AttributeError):
            return "CONTROLS_UNREADABLE"
        return ""

    def execute_dispatches(self, plans: list, snapshot: dict | None = None):
        """Phase C for dispatch. External work, with NO state lock held.

        Returns one `DispatchResult` per plan that was actually attempted. A
        plan held by a control is NOT returned: its claim stays exactly as it
        is, reserving the same worker name and branch, and the next tick's
        `resume_dispatch_claims` re-plans it.
        """
        results: list[DispatchResult] = []
        if not plans:
            return results
        if snapshot is None:
            snapshot = self.store.read() if self.store.exists() else {}
        for plan in plans:
            handler = self.DISPATCH_HANDLERS.get(plan.role)
            if handler is None:
                self.log("DISPATCH_EXECUTION_HELD", task_id=plan.task_id,
                         role=plan.role, pr_id=plan.pr,
                         activity_class="ORCHESTRATION", outcome="NO_HANDLER")
                continue
            if self.stopping:
                # A stop signal arrived after T1 committed. The claim is
                # durable and resumes on the next start; spawning a worker
                # the shutdown is about to orphan is not recoverable.
                self.log("DISPATCH_EXECUTION_HELD", task_id=plan.task_id,
                         role=plan.role, pr_id=plan.pr,
                         activity_class="ORCHESTRATION",
                         outcome="SUPERVISOR_STOPPING")
                break
            blocked = self._dispatch_block(snapshot, plan.role)
            if blocked:
                self.log("DISPATCH_EXECUTION_HELD", task_id=plan.task_id,
                         role=plan.role, pr_id=plan.pr,
                         activity_class="ORCHESTRATION", outcome=blocked)
                continue
            results.append(getattr(self, handler.execute)(plan))
        return results

    def apply_dispatch_result(self, doc: dict, result: DispatchResult) -> None:
        """Commit one dispatch outcome. STATE ONLY.

        Separate from `confirm_dispatches` because the same commit is reached
        from two places - its own transaction on the ordinary path, and T1 on
        the recovery path - and both must run exactly the same code.
        """
        handler = self.DISPATCH_HANDLERS.get(result.plan.role)
        if handler is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=result.plan.task_id,
                     role=result.plan.role, pr_id=result.plan.pr,
                     activity_class="ORCHESTRATION", outcome="NO_HANDLER")
            return
        getattr(self, handler.commit if result.ok else handler.fail)(
            doc, result.plan, result)

    def confirm_dispatches(self, results: list) -> None:
        """Phase D for dispatch. ONE TRANSACTION PER RESULT, deliberately.

        A single shared transaction would mean one task's fail-closed abort
        (C-15 raises rather than persist false release evidence) discarding
        another task's commit - and that other task's worker has already been
        spawned, so the state would be lost while the process ran on. Per
        result, the blast radius is the one dispatch that went wrong, and
        anything uncommitted is recovered from its claim and job file on the
        next tick.
        """
        for result in results:
            with self.store.transaction() as doc:
                self.apply_dispatch_result(doc, result)

    def _current_dispatch_claim(self, doc: dict, plan: DispatchPlan):
        """(task, claim) only if the claim is still the exact one acted on.

        Returns (task, None) when the task is still there but the claim has
        been superseded, and (None, None) when the task itself is gone.
        """
        task = doc.get("tasks", {}).get(plan.task_id)
        if task is None:
            return None, None
        claim = state_mod.dispatch_claim(task, plan.role)
        if not state_mod.dispatch_claim_active(claim):
            return task, None
        if claim.get("worker") != plan.worker or claim.get("pr") != plan.pr:
            return task, None
        return task, claim

    @staticmethod
    def _clear_dispatch_claim(task: dict, role: str) -> None:
        state_mod.dispatch_claims(task).pop(role, None)

    @staticmethod
    def _snapshot_claims(task) -> list:
        """A task's claims, read defensively from a NON-authoritative read.

        The snapshot is whatever is on disk, so the map may be absent or the
        wrong type entirely. A tick must not fail on that - T1 re-reads under
        the lock and decides from there.
        """
        if not isinstance(task, dict):
            return []
        claims = task.get(state_mod.DISPATCH_CLAIMS_KEY)
        return list(claims.values()) if isinstance(claims, dict) else []

    def dispatch_builder(self, doc: dict, task: dict) -> None:
        lock_newly_acquired = False
        if state_mod.dispatch_claim_active(
                state_mod.dispatch_claim(task, "builder")):
            # A claim already reserves this task's builder. Re-planning would
            # allocate a second worker name for one reservation; the existing
            # claim is resumed by resume_dispatch_claims instead.
            return
        if task.get("schema_changing"):
            # C-15: fresh-vs-re-entrant ownership decides what a dispatch
            # failure may do with the lock. Read inside this same transaction,
            # so the answer cannot race.
            owner_before = migration_lock.owner(doc)
            if not migration_lock.acquire(doc, task["id"], self.tz):
                if task["state"] != "WAITING_DB_LOCK":
                    self.transition(doc, task["id"], "WAITING_DB_LOCK",
                                    f"migration lock held by {migration_lock.owner(doc)}")
                return
            lock_newly_acquired = owner_before is None
            self.log("MIGRATION_LOCK_ACQUIRED", task_id=task["id"],
                     activity_class="ORCHESTRATION", outcome="HELD")

        worker = f"{task['id'].lower()}-builder"
        branch = f"task/{task['id'].lower()}"

        if task["state"] != "READY":
            self.transition(doc, task["id"], "READY", "dependencies met")

        # C-18 stage 4: the ports this state permits, chosen under the lock.
        # NO BIND happens here - `select_port_candidates` is the half of
        # allocation stage 3 proved performs no network operation. The probe
        # that proves one of them binds runs in the execute phase.
        candidates, port_why = workers.select_port_candidates(doc)
        if not candidates:
            self.log("PORT_ALLOCATION_FAILED", task_id=task["id"], role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"why": port_why})
            # Still inside the transaction that acquired the lock, so the
            # in-memory freshness answer is exactly as durable as the acquire
            # it came from. This is the one failure path that does not cross
            # the transaction boundary.
            self.on_builder_dispatch_failure(doc, task, lock_newly_acquired,
                                             f"no governed port: {port_why}")
            return

        try:
            claim = state_mod.new_dispatch_claim(
                role="builder", task_id=task["id"], worker=worker,
                branch=branch, claimed_at=clock.iso(self.now()),
                lease_expires_at=self._lease_expires("builder"),
                port_candidates=candidates,
                context={
                    # The prompt is rendered outside the lock, so the fields
                    # it reads travel as a frozen copy. Handing out the task
                    # dict would let the execute phase read a value that has
                    # since changed.
                    "task_title": task.get("title") or task["id"],
                    "task_body": task.get("body") or "",
                    "depends_on": list(task.get("depends_on") or []),
                    "schema_changing": bool(task.get("schema_changing")),
                    "port_exhaustion_reason": port_why,
                    # C-15 evidence, recorded under the same lock that did
                    # the acquire. `_migration_lock_release_permitted`
                    # re-derives the fresh-vs-re-entrant answer from these
                    # facts AND the committed document, never from either
                    # alone - see its docstring.
                    "lock_newly_acquired": lock_newly_acquired,
                    "lock_acquired_at":
                        (doc.get("migration_lock") or {}).get("acquired_at"),
                })
        except ValueError as exc:
            # A claim state would refuse is worse than no claim: the slot
            # would be reserved by something that can never spawn.
            self.log("DISPATCH_CLAIM_REFUSED", task_id=task["id"], role="builder",
                     activity_class="FAILED_WORK", outcome="CLAIM_REFUSED",
                     metadata_redacted={"error": str(exc)})
            self.on_builder_dispatch_failure(doc, task, lock_newly_acquired,
                                             "builder claim refused")
            return

        state_mod.dispatch_claims(task)["builder"] = claim
        self.log("DISPATCH_CLAIMED", task_id=task["id"], role="builder",
                 branch=branch, agent_id=worker, activity_class="BUILD",
                 outcome="CLAIMED",
                 metadata_redacted={"port_candidates": len(candidates),
                                    "lease_expires_at": claim["lease_expires_at"]})
        self._plan_dispatch(self.dispatch_plan(claim))

    def _execute_builder_dispatch(self, plan: DispatchPlan) -> DispatchResult:
        """C-18 stage 4, Phase C. NO STATE LOCK IS HELD.

        Every call here used to run inside T1: the prompt file write, the
        workmux/git subprocess that creates the worktree, the workmux call
        that resolves its path, the real 127.0.0.1 bind, the job file write
        and the process spawn. The ledger appends stay here too - they are
        not transactional, so a failure is annunciated at the moment it
        happens rather than one commit later.
        """
        role_cfg = self.cfg.roles["builder"]
        prompt_text = prompts.builder(
            {"id": plan.task_id,
             "title": plan.context.get("task_title") or plan.task_id,
             "body": plan.context.get("task_body") or "",
             "depends_on": list(plan.context.get("depends_on") or []),
             "schema_changing": bool(plan.context.get("schema_changing"))},
            plan.branch, self.cfg.github_repo, self.cfg.main_branch)
        prompt_path = prompts.write(plan.worker, prompt_text)

        created = workers.create_worker(plan.worker, plan.branch, prompt_path,
                                        self.cfg.tmux_session,
                                        self.cfg.main_branch)
        if not created.ok:
            self.log("WORKER_CREATE_FAILED", task_id=plan.task_id, role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"stderr": created.stderr[:500]})
            return DispatchResult(plan=plan, ok=False,
                                  reason="worktree creation failed")

        path = workers.worktree_path(plan.worker)
        if path is None:
            return DispatchResult(plan=plan, ok=False,
                                  reason="worktree path not resolvable")

        port = next((candidate for candidate in plan.port_candidates
                     if workers.probe_port(candidate)), None)
        if port is None:
            why = plan.context.get("port_exhaustion_reason") or \
                "no candidate port bound"
            self.log("PORT_ALLOCATION_FAILED", task_id=plan.task_id, role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"why": why})
            return DispatchResult(plan=plan, ok=False,
                                  reason=f"no governed port: {why}")

        job = workers.write_job(
            plan.worker, "builder", role_cfg.provider, role_cfg.model,
            plan.task_id, path, prompt_path, self.tz,
            hard_timeout_seconds=self.cfg.extra["timeouts"]["builder"],
            fallback_model=role_cfg.escalation_model, port=port,
        )
        started = workers.start_job(plan.worker, job, path)
        if not started.ok:
            self.log("WORKER_START_FAILED", task_id=plan.task_id, role="builder",
                     activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"stderr": started.stderr[:500]})
            return DispatchResult(plan=plan, ok=False,
                                  reason="worker start failed")
        return DispatchResult(plan=plan, ok=True, worktree=str(path), port=port)

    def _commit_builder_dispatch(self, doc: dict, plan: DispatchPlan,
                                 result: DispatchResult) -> None:
        """C-18 stage 4, commit side. STATE ONLY.

        Everything here used to commit in the same transaction as the spawn:
        the attempts increment, the ASSIGNED transition, the worker record
        and the retained-worktree handover. They now commit after the spawn
        has actually happened, which is also what makes `attempts` honest -
        it still counts exactly one per real dispatch.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     role="builder", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="TASK_GONE")
            return
        if doc.get("workers", {}).get(plan.worker):
            # Already committed - a lost confirm converged by recovery, or a
            # recovery that raced its own confirm. Idempotent by design.
            self._clear_dispatch_claim(task, "builder")
            return
        if claim is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     role="builder", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="CLAIM_SUPERSEDED")
            return

        role_cfg = self.cfg.roles["builder"]
        assigned_at = clock.iso(self.now())
        task["worker"] = plan.worker
        task["branch"] = plan.branch
        task["attempts"] += 1
        task["assigned_at"] = assigned_at
        task["last_progress_at"] = assigned_at
        task["progress_marker"] = 0
        self.transition(doc, plan.task_id, "ASSIGNED", "builder dispatched")
        doc["workers"][plan.worker] = state_mod.new_worker_record(
            "builder", plan.task_id, plan.branch, assigned_at,
            worktree=result.worktree, port=result.port,
            lease_expires_at=self._lease_expires("builder"))
        if result.worktree:
            task.setdefault("retained_worktrees", {}).pop(result.worktree, None)
        self._clear_dispatch_claim(task, "builder")
        self.log("TASK_DISPATCHED", task_id=plan.task_id, role="builder",
                 provider=role_cfg.provider, model=role_cfg.model,
                 branch=plan.branch, agent_id=plan.worker,
                 activity_class="BUILD", outcome="DISPATCHED")

    def _fail_builder_dispatch(self, doc: dict, plan: DispatchPlan,
                               result: DispatchResult) -> None:
        """C-18 stage 4, failure side. STATE ONLY.

        The claim is released first, so the task is free to be dispatched
        again, and then C-15's decision is taken from durable evidence.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None or claim is None:
            self.log("DISPATCH_FAILURE_REFUSED", task_id=plan.task_id,
                     role="builder", agent_id=plan.worker,
                     activity_class="ORCHESTRATION",
                     outcome="TASK_GONE" if task is None else "CLAIM_SUPERSEDED")
            return
        permitted = self._migration_lock_release_permitted(doc, plan)
        self._clear_dispatch_claim(task, "builder")
        self.on_builder_dispatch_failure(doc, task, permitted, result.reason)

    def _migration_lock_release_permitted(self, doc: dict,
                                          plan: DispatchPlan) -> bool:
        """C-15 fresh-vs-re-entrant, RE-DERIVED from the committed document.

        Before C-18 this was a local boolean: `owner_before is None`, read a
        few statements earlier in the SAME transaction that would act on it.
        Once the dispatch failure can be handled after T1 has committed, that
        boolean is no longer evidence of anything - the document it described
        is gone, and between the two transactions the lock can be released,
        re-acquired, or acquired by another task.

        So the answer is rebuilt here from four durable facts, ALL of which
        must hold before the lock may be auto-released:

          1. This plan acquired the lock rather than inheriting it
             (`lock_newly_acquired`, written under the lock that did it).
          2. This task still owns the lock.
          3. It is still the SAME ownership episode. `acquired_at` is set on
             a fresh acquisition, left untouched by a re-acquire from the
             same owner, and cleared on release - so an episode that ended
             and began again has a different value, and this plan knows
             nothing about the new one.
          4. No builder has been dispatched under that episode.
             `assigned_at` is written only by a committed builder dispatch,
             so `assigned_at >= acquired_at` is exactly C-15's "a previous
             builder ran while it held the lock", read from state instead of
             inferred from control flow.

        Every disagreement answers False, which RETAINS the lock. That is the
        safe direction: retaining a lock no builder ever used costs a human
        decision, while releasing one a builder mutated schema under is the
        corruption C-15 exists to prevent.

        Timestamps are compared as datetimes, never as strings: Pacific/
        Auckland changes offset twice a year, and across that boundary a
        later instant can sort earlier lexicographically. An unparseable
        timestamp answers False.
        """
        context = plan.context
        if not context.get("lock_newly_acquired"):
            return False
        acquired_at = context.get("lock_acquired_at")
        if not acquired_at:
            return False
        lock = doc.get("migration_lock") or {}
        if lock.get("owner_task") != plan.task_id:
            return False
        if lock.get("acquired_at") != acquired_at:
            return False
        assigned_at = (doc.get("tasks", {}).get(plan.task_id) or {}).get(
            "assigned_at")
        if assigned_at:
            try:
                if clock.parse(assigned_at) >= clock.parse(acquired_at):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def on_builder_dispatch_failure(self, doc: dict, task: dict,
                                    lock_newly_acquired: bool, why: str) -> None:
        """C-15: a builder dispatch failed at a proven pre-execution point.

        Every call site fails before any builder process exists for THIS
        dispatch, so ownership history decides what may happen to the
        migration lock. `lock_newly_acquired` means "this dispatch acquired
        the lock and no builder has run under that ownership" - C-18 stage 4
        moved most failures out of the acquiring transaction, so the caller
        on that path derives it with `_migration_lock_release_permitted`
        rather than remembering it:

          * FRESH owner (this dispatch newly acquired it): no builder has ever
            run under this ownership, so no schema mutation is attributable to
            it - release automatically, atomically with the BLOCKED transition
            and a task-owned durable marker the evidence pass converges later.
            General BLOCKED recovery stays out of C-15's scope.
          * RE-ENTRANT owner (a previous builder ran while it held the lock):
            shared-schema mutation is possible, so never auto-release - the
            task goes to HUMAN_REQUIRED with a builder_dispatch_failed
            intervention instead of invisible BLOCKED, and a human decides
            RETRY (retain), FAIL (note-guarded release) or NO_ACTION (park).
        """
        if migration_lock.owner(doc) == task["id"]:
            if lock_newly_acquired:
                existing = task.get("migration_lock_auto_release")
                if existing and not existing.get("evidenced"):
                    # A fresh acquisition over an unevidenced prior release
                    # claim contradicts the one-marker cardinality this
                    # design proves; failing closed aborts the transaction
                    # rather than silently overwriting evidence.
                    raise RuntimeError(
                        f"{task['id']}: unevidenced migration_lock_auto_release "
                        f"marker already present; refusing to overwrite")
                if not migration_lock.release(doc, task["id"],
                                              "dispatch failed before any builder ran"):
                    # Supposed to be impossible for the current owner; a False
                    # here means the lock and this code disagree - fail closed
                    # so BLOCKED plus false release evidence never persists.
                    raise RuntimeError(
                        f"{task['id']}: migration lock release refused for "
                        f"the current owner")
                task["migration_lock_auto_release"] = {
                    "at": clock.iso(self.now()),
                    "why": "DISPATCH_FAILED_BEFORE_BUILDER_STARTED",
                    "evidenced": False,
                }
                self.transition(doc, task["id"], "BLOCKED", why)
                return
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            f"builder dispatch failed while owning the "
                            f"migration lock: {why}")
            self.request_intervention(
                doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                condition_code="builder_dispatch_failed",
                task_id=task["id"],
                reason=f"Builder dispatch failed for {task['id']} while it "
                       f"owns the migration lock",
                title=f"{task['id']}: builder dispatch failed while owning "
                      f"the migration lock",
                body=f"{why}. A previous builder ran under this ownership, so "
                     f"the migration lock is retained pending a human decision.")
            return
        self.transition(doc, task["id"], "BLOCKED", why)

    AUTO_RELEASE_BY = "dispatch_failed_before_builder_started"

    def reconcile_auto_release_evidence(self, doc: dict) -> None:
        """C-15: converge task-owned auto-release markers to ledger evidence.

        The release itself commits with state only; this pass derives the
        MIGRATION_LOCK_RELEASED event from the marker, at most once, guarded
        by the ledger itself. Its only state change is evidenced False -> True.
        It never releases anything, never reads the mutable lock fields, and
        never transitions a task - so unrelated later lock activity cannot
        confuse it. A failed append aborts this transaction and a later tick
        retries; an append that landed while the evidenced flip was lost is
        found by the guard and not duplicated.
        """
        for task in doc["tasks"].values():
            marker = task.get("migration_lock_auto_release")
            if not marker or marker.get("evidenced"):
                continue
            if not self._auto_release_event_exists(task["id"]):
                self.log("MIGRATION_LOCK_RELEASED", task_id=task["id"],
                         outcome="FREE", activity_class="ORCHESTRATION",
                         metadata_redacted={
                             "released_by": self.AUTO_RELEASE_BY,
                             "released_at": marker["at"],
                             "why": marker["why"],
                         })
            marker["evidenced"] = True

    def _auto_release_event_exists(self, task_id: str) -> bool:
        for event in self.ledger.events("MIGRATION_LOCK_RELEASED"):
            metadata = event.get("metadata_redacted") or {}
            if (event.get("task_id") == task_id
                    and metadata.get("released_by") == self.AUTO_RELEASE_BY):
                return True
        return False

    def dispatch_reviewer(self, doc: dict, task: dict, pr_number: int,
                          observation: "ReviewObservation | None") -> None:
        """C-18 stage 5, planning half. STATE ONLY - this runs inside T1.

        `observation` is `observe_review_heads`' answer for this pull request,
        taken before the lock. It is REQUIRED rather than defaulted so a
        caller that forgets it defers loudly instead of silently reviewing
        nothing, and there is deliberately no fallback fetch: a GitHub call
        under the lock is the defect this stage removes.

        Everything external - the evidence collection, the prompt, the
        worktree, the job file and the spawn - happens in
        `_execute_reviewer_dispatch` after T1 commits. All this half does is
        reserve the reviewer slot with a durable claim bound to the observed
        head.
        """
        if not providers.may(doc, "review"):
            self.transition(doc, task["id"], "WAITING_PROVIDER_RESET",
                            "reviewer provider paused")
            return
        if state_mod.dispatch_claim_active(
                state_mod.dispatch_claim(task, "reviewer")):
            # A claim already reserves this pull request's reviewer. Planning
            # again would mint a second worker name for one reservation;
            # resume_dispatch_claims drives the existing one instead.
            return
        record = doc["prs"][str(pr_number)]

        if observation is None or observation.pr_number != pr_number:
            # No pre-transaction observation was taken for this pull request,
            # or the answer failed its binding check. Defer to the next tick.
            self.log("REVIEW_DISPATCH_DEFERRED", task_id=task["id"],
                     pr_id=pr_number, role="reviewer", outcome="OBSERVATION_MISSING",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={
                         "reason": "no pre-transaction GitHub observation"})
            return
        if observation.head is None:
            self.on_dispatch_failure(doc, task, pr_number, "reviewer",
                                     "could not resolve the pull request head")
            return

        # Each cycle reviews in its own worktree, on its own branch, cut from the
        # pull request's observed head. A per-cycle branch cannot collide with an
        # earlier cycle's checkout, and basing it on the observed head guarantees
        # the re-review sees the Fixer's changes rather than superseded code.
        branch = task["branch"]
        cycle = record["review_cycles"] + 1
        worker = f"{task['id'].lower()}-review-{cycle}"
        try:
            claim = state_mod.new_dispatch_claim(
                role="reviewer", task_id=task["id"], worker=worker, branch=branch,
                claimed_at=clock.iso(self.now()),
                lease_expires_at=self._lease_expires("reviewer"),
                pr=pr_number, observed_head=observation.head,
                context={
                    "cycle": cycle,
                    # Cycle segment first. `review/<branch>/c2` would nest a
                    # ref under the existing `review/<branch>` ref, which git
                    # stores as a file and cannot also be a directory.
                    # `review/c2/<branch>` cannot collide with any other review
                    # branch by construction.
                    "review_branch": f"review/c{cycle}/{branch}",
                    # Observed in the SAME breath as the head, so the hash
                    # committed below always describes the head committed
                    # beside it. The execute phase proves that head has not
                    # moved before either is used.
                    "diff_hash": observation.diff_hash,
                    # The prompt is rendered outside the lock, so the one field
                    # it reads travels as a frozen copy rather than a reference
                    # into this transaction's document.
                    "task_title": task.get("title") or task["id"],
                })
        except ValueError as exc:
            # A claim state would refuse is worse than no claim: the reviewer
            # slot would be reserved by something that can never spawn.
            self.log("DISPATCH_CLAIM_REFUSED", task_id=task["id"], pr_id=pr_number,
                     role="reviewer", activity_class="FAILED_WORK",
                     outcome="CLAIM_REFUSED",
                     metadata_redacted={"error": str(exc)})
            self.on_dispatch_failure(doc, task, pr_number, "reviewer",
                                     "reviewer claim refused")
            return

        state_mod.dispatch_claims(task)["reviewer"] = claim
        self.log("DISPATCH_CLAIMED", task_id=task["id"], pr_id=pr_number,
                 role="reviewer", branch=branch, agent_id=worker,
                 activity_class="REVIEW", outcome="CLAIMED",
                 metadata_redacted={"head": observation.head, "review_cycle": cycle,
                                    "lease_expires_at": claim["lease_expires_at"]})
        self._plan_dispatch(self.dispatch_plan(claim))

    def _review_preflight(self, plan: DispatchPlan) -> str:
        """Re-verify a pull request immediately before its review is
        dispatched, and take it out of draft if it is one.

        Returns "" when the review may proceed, or ONE finite code from
        `REVIEW_PREFLIGHT_CODES`. Never an exception string.

        GOVERNANCE. The approved authority amendment reads, verbatim:

            "The Supervisor may mark a draft PR for a governed task ready for
            review before independent review dispatch. This action grants no
            approval or merge eligibility."

        That grant is narrow and the four operator conditions are load-bearing:

          1. RE-VERIFY FIRST. Association and head are re-derived from GitHub
             in this one `pr_view`, not read off the plan. The plan was built
             inside T1 earlier in this tick - or, on the recovery path, in a
             previous tick - and marking a pull request ready on bookkeeping
             that stale is exactly the mistake the amendment forbids. The plan
             supplies only what the answer is checked AGAINST.
          2. FAIL BY DEFERRING. Every disagreement returns a code, which fails
             this dispatch; `_fail_reviewer_dispatch` releases the claim and
             `route_awaiting_dispatch` re-routes the task next tick against a
             freshly observed head. Nothing retries inside this call, and the
             existing dispatch-failure limit bounds the re-routing.
          3. IT GRANTS NOTHING, structurally. This method runs in the execute
             phase, which holds no state document and no lock - there is no
             `approval_current`, `review_verdict` or merge record in scope for
             it to touch. The only thing it can return to the control plane is
             a `DispatchResult`, and the commit half of a successful dispatch
             sets `approval_current = False` and `review_verdict = None`
             unconditionally. Marking ready can therefore only ever move the
             pull request AWAY from merge eligibility, never towards it.
          4. The draft path is EXCEPTIONAL, not routine: the builder opens
             pull requests ready (`prompts` passes no `--draft` anywhere), so
             a draft here means a human or an external tool made one.
        """
        repo = self.cfg.github_repo
        view = gh.pr_view(repo, plan.pr)
        if not isinstance(view, dict) or not view:
            return self._review_deferred(plan, "PR_NOT_FOUND")
        if (view.get("state") or "").upper() != "OPEN":
            return self._review_deferred(plan, "PR_NOT_OPEN",
                                         state=view.get("state"))
        if view.get("headRefName") != plan.branch:
            # The task/PR association, re-derived: this pull request must still
            # be the one tracking this task's branch.
            return self._review_deferred(plan, "PR_ASSOCIATION_MISMATCH",
                                         branch=view.get("headRefName"))
        if view.get("headRefOid") != plan.observed_head:
            return self._review_deferred(plan, "HEAD_MOVED",
                                         observed=view.get("headRefOid"))

        if not view.get("isDraft"):
            return ""
        result = gh.mark_ready(repo, plan.pr)
        if not result.ok:
            return self._review_deferred(plan, "READY_FOR_REVIEW_REFUSED")
        self.log("PR_MARKED_READY_FOR_REVIEW", task_id=plan.task_id,
                 pr_id=plan.pr, role="reviewer", branch=plan.branch,
                 activity_class="ORCHESTRATION", outcome="READY",
                 metadata_redacted={
                     "head": plan.observed_head,
                     "grants": "NO_APPROVAL_NO_MERGE_ELIGIBILITY"})
        return ""

    def _review_deferred(self, plan: DispatchPlan, code: str, **detail) -> str:
        """Annunciate one finite preflight diagnostic and return it."""
        self.log("REVIEW_PREFLIGHT_DEFERRED", task_id=plan.task_id,
                 pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                 activity_class="REVIEW", outcome=code,
                 metadata_redacted={"head": plan.observed_head, **detail})
        return code

    def _execute_reviewer_dispatch(self, plan: DispatchPlan) -> DispatchResult:
        """C-18 stage 5, Phase C. NO STATE LOCK IS HELD.

        Every call here used to run inside T1: the head resolution, the two
        GitHub calls `evidence.collect` makes, the prompt file write, the
        workmux/git worktree acquisition, the job file write and the spawn.

        THE HEAD-MOVED RULE lives in `_review_preflight`, and it is the point
        of the stage. The head was observed before T1; by the time this runs,
        T1 has committed and - on the recovery path - the claim may have been
        planned a whole tick ago. A review cut from a superseded commit would
        still write `reviewed_head` as if it had reviewed the current one, and
        the merge gate would then approve code no reviewer ever saw. So the
        head is re-resolved and must still equal the one the claim is bound to.
        Anything else, INCLUDING an unresolvable head, invalidates the plan:
        a head that cannot be proved unchanged is not a head that has been
        proved unchanged.
        """
        role_cfg = self.cfg.roles["reviewer"]
        head = plan.observed_head
        blocked = self._review_preflight(plan)
        if blocked:
            return DispatchResult(plan=plan, ok=False, reason=blocked)

        # Evidence Codex cannot reach from a read-only, no-network sandbox, bound
        # to this exact head. Agent self-assertions are deliberately excluded.
        items = evidence.collect(self.cfg.github_repo, head, self.ledger)
        self.log("REVIEW_EVIDENCE_GATHERED", task_id=plan.task_id, pr_id=plan.pr,
                 activity_class="REVIEW", outcome="COLLECTED",
                 metadata_redacted={"head": head,
                                    "items": [i.as_dict() for i in items],
                                    "applicable": sum(1 for i in items if i.applies)})

        cycle = plan.context.get("cycle") or 1
        prompt_text = prompts.reviewer(
            {"id": plan.task_id,
             "title": plan.context.get("task_title") or plan.task_id},
            plan.pr, plan.branch, self.cfg.github_repo, cycle,
            evidence.render(items, head))
        prompt_path = prompts.write(plan.worker, prompt_text)
        path, why = workers.acquire_worktree(
            plan.worker, plan.context.get("review_branch")
            or f"review/c{cycle}/{plan.branch}",
            head, self.cfg.tmux_session, prompt_path, reuse_if_checked_out=False,
        )
        if path is None:
            return DispatchResult(plan=plan, ok=False, reason=why)

        job = workers.write_job(
            plan.worker, "reviewer", role_cfg.provider, role_cfg.model,
            plan.task_id, path, prompt_path, self.tz, pr=plan.pr,
            hard_timeout_seconds=self.cfg.extra["timeouts"]["reviewer"],
            effort=role_cfg.effort,
        )
        started = workers.start_job(plan.worker, job, path)
        if not started.ok:
            self.log("WORKER_START_FAILED", task_id=plan.task_id, pr_id=plan.pr,
                     role="reviewer", activity_class="FAILED_WORK", outcome="FAILED",
                     metadata_redacted={"stderr": started.stderr[:500]})
            return DispatchResult(
                plan=plan, ok=False,
                reason=f"worker did not start: {started.stderr[:200]}")
        return DispatchResult(plan=plan, ok=True, worktree=str(path))

    def _commit_reviewer_dispatch(self, doc: dict, plan: DispatchPlan,
                                  result: DispatchResult) -> None:
        """C-18 stage 5, commit side. STATE ONLY.

        `review_cycles`, the cleared approval, the worker record and the REVIEW
        transition used to commit in the same transaction as the spawn. They
        now commit after it, which is what makes `review_cycles` honest - it
        still counts exactly one per review that actually started.

        `reviewed_head` is written from the claim's `observed_head`, never from
        a fresh fetch, because that is the commit the worktree was cut from and
        the commit the reviewer is actually reading. The identity re-check
        below includes it: a claim whose head differs is a DIFFERENT
        reservation that happens to share a worker name - the same cycle
        re-planned at a newer head - and committing this result against it
        would bind the new reservation's record to the old reservation's SHA.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="TASK_GONE")
            return
        if doc.get("workers", {}).get(plan.worker):
            # Already committed - a lost confirm converged by recovery, or a
            # recovery that raced its own confirm. Idempotent by design.
            self._clear_dispatch_claim(task, "reviewer")
            return
        if claim is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="CLAIM_SUPERSEDED")
            return
        if claim.get("observed_head") != plan.observed_head:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="HEAD_REBOUND",
                     metadata_redacted={"head": plan.observed_head,
                                        "observed": claim.get("observed_head")})
            return
        if task.get("pr") != plan.pr or task.get("branch") != plan.branch:
            # The state-side half of the association re-check the preflight
            # makes against GitHub: a task re-attached to a different pull
            # request or branch between plan and commit must not have this
            # review's cycle, head and worker written onto it.
            self._clear_dispatch_claim(task, "reviewer")
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION",
                     outcome="PR_ASSOCIATION_MISMATCH",
                     metadata_redacted={"branch": task.get("branch"),
                                        "pr": task.get("pr")})
            return
        record = (doc.get("prs") or {}).get(str(plan.pr))
        if record is None:
            # The pull request record went away between plan and commit. The
            # claim is released so nothing holds the slot, and no record is
            # invented to write a review cycle into.
            self._clear_dispatch_claim(task, "reviewer")
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="PR_RECORD_GONE")
            return

        role_cfg = self.cfg.roles["reviewer"]
        started_at = clock.iso(self.now())
        record["reviewed_head"] = plan.observed_head
        record["reviewed_diff_hash"] = plan.context.get("diff_hash")
        record["review_cycles"] += 1
        record["approval_current"] = False
        record["review_verdict"] = None
        task["worker"] = plan.worker
        worker_record = state_mod.new_worker_record(
            "reviewer", plan.task_id, plan.branch, started_at, pr=plan.pr,
            worktree=result.worktree,
            lease_expires_at=self._lease_expires("reviewer"))
        # The SHA this specific worker is reviewing, bound to the worker rather
        # than to the mutable per-PR field a later cycle overwrites. It is what
        # REVIEW_RESULT attributes the verdict to.
        worker_record["head"] = plan.observed_head
        doc["workers"][plan.worker] = worker_record
        if result.worktree:
            task.setdefault("retained_worktrees", {}).pop(result.worktree, None)
        self._clear_dispatch_claim(task, "reviewer")
        self.transition(doc, plan.task_id, "REVIEW", f"dispatched review cycle "
                                                     f"{record['review_cycles']}")
        # `head_sha` is passed as a BARE KWARG, not inside metadata_redacted.
        # `ledger.FIELDS` has no `head_sha`, and `Ledger.append` merges every
        # unrecognised kwarg into `metadata_redacted` - so this lands at
        # `metadata_redacted.head_sha` with no change to ledger.py and no
        # top-level spelling. The C-04 composition layer reads both places and
        # treats a disagreement between them as fatal, so it is emitted once
        # and only once. `agent_id` names the worker, which is what reviewer
        # identity verification checks the SHA-bound result against.
        self.log("REVIEW_DISPATCHED", task_id=plan.task_id, pr_id=plan.pr,
                 role="reviewer", provider=role_cfg.provider, model=role_cfg.model,
                 agent_id=plan.worker, branch=plan.branch,
                 activity_class="REVIEW", outcome="DISPATCHED",
                 head_sha=plan.observed_head,
                 metadata_redacted={"review_cycle": record["review_cycles"]})

    def _fail_reviewer_dispatch(self, doc: dict, plan: DispatchPlan,
                                result: DispatchResult) -> None:
        """C-18 stage 5, failure side. STATE ONLY.

        The claim is released first, so `route_awaiting_dispatch` is free to
        re-route the task on the next tick, and then the shared failure
        accounting runs exactly as it did before C-18.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None or claim is None:
            self.log("DISPATCH_FAILURE_REFUSED", task_id=plan.task_id,
                     pr_id=plan.pr, role="reviewer", agent_id=plan.worker,
                     activity_class="ORCHESTRATION",
                     outcome="TASK_GONE" if task is None else "CLAIM_SUPERSEDED")
            return
        self._clear_dispatch_claim(task, "reviewer")
        self.on_dispatch_failure(doc, task, plan.pr, "reviewer", result.reason)

    def dispatch_fixer(self, doc: dict, task: dict, pr_number: int,
                       findings: list[dict]) -> None:
        """C-18 stage 6, PLANNING HALF. Inside T1, STATE ONLY.

        Writes a durable claim and hands it to `_plan_dispatch`. The prompt
        write, the worktree acquisition, the 127.0.0.1 bind, the job file and
        the spawn all happen in `_execute_fixer_dispatch` after T1 commits;
        `repair_cycles`, `open_finding_ids`, the worker record and the
        FIX_REQUIRED transition commit afterwards in `_commit_fixer_dispatch`.
        """
        if state_mod.dispatch_claim_active(
                state_mod.dispatch_claim(task, "fixer")):
            # A claim already reserves this task's fixer. Re-planning would
            # allocate a second worker name for one reservation; the existing
            # claim is resumed by resume_dispatch_claims instead.
            return
        if not providers.may(doc, "new_builds"):
            self.transition(doc, task["id"], "WAITING_PROVIDER_RESET",
                            "fixer provider paused")
            return
        record = doc["prs"][str(pr_number)]
        if record["repair_cycles"] >= self.cfg.max_repair_cycles:
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            "repair cycle limit reached")
            self.request_intervention(
                doc, type_="HUMAN_PRODUCT_DECISION",
                condition_code="repair_cycle_limit",
                task_id=task["id"], pr_id=pr_number,
                reason=f"Repair cycle limit reached for {task['id']} "
                       f"PR #{pr_number} after {record['repair_cycles']} "
                       f"repair cycles",
                title=f"{task['id']} PR #{pr_number}: repair limit reached",
                body=f"{record['repair_cycles']} repair cycles without a pass.")
            return

        worker = f"{task['id'].lower()}-fixer-{record['repair_cycles'] + 1}"

        # C-18 stage 6: the ports this state permits, chosen under the lock.
        # NO BIND happens here - stage 3 proved `select_port_candidates`
        # performs no network operation. The probe that proves one of them
        # binds runs in the execute phase, with no lock held.
        candidates, port_why = workers.select_port_candidates(doc)
        if not candidates:
            self.log("PORT_ALLOCATION_FAILED", task_id=task["id"], role="fixer",
                     pr_id=pr_number, activity_class="FAILED_WORK",
                     outcome="FAILED", metadata_redacted={"why": port_why})
            # Decided in T1 and handled in T1: no claim was written, so there
            # is nothing to recover and no external effect to undo.
            self.on_dispatch_failure(doc, task, pr_number, "fixer",
                                     f"no governed port: {port_why}")
            return

        try:
            claim = state_mod.new_dispatch_claim(
                role="fixer", task_id=task["id"], worker=worker,
                branch=task["branch"], claimed_at=clock.iso(self.now()),
                lease_expires_at=self._lease_expires("fixer"),
                pr=pr_number, port_candidates=candidates,
                context={
                    # The prompt is rendered outside the lock, so the findings
                    # it reads travel as a copy one level deep - a reference
                    # into `record["pending_findings"]` would let the execute
                    # phase read a list a later transaction had changed.
                    "findings": [dict(f) if isinstance(f, dict) else f
                                 for f in (findings or [])],
                    "port_exhaustion_reason": port_why,
                })
        except ValueError as exc:
            # A claim state would refuse is worse than no claim: the fixer
            # slot would be reserved by something that can never spawn.
            self.log("DISPATCH_CLAIM_REFUSED", task_id=task["id"], role="fixer",
                     pr_id=pr_number, activity_class="FAILED_WORK",
                     outcome="CLAIM_REFUSED",
                     metadata_redacted={"error": str(exc)})
            self.on_dispatch_failure(doc, task, pr_number, "fixer",
                                     "fixer claim refused")
            return

        state_mod.dispatch_claims(task)["fixer"] = claim
        self.log("DISPATCH_CLAIMED", task_id=task["id"], role="fixer",
                 pr_id=pr_number, branch=task["branch"], agent_id=worker,
                 activity_class="FIX", outcome="CLAIMED",
                 metadata_redacted={"port_candidates": len(candidates),
                                    "lease_expires_at": claim["lease_expires_at"]})
        self._plan_dispatch(self.dispatch_plan(claim))

    def _dispatch_claim_still_current(self, plan: DispatchPlan) -> bool:
        """Is the claim that authorised this plan STILL the current one?

        Read from the COMMITTED document, read-only and with no lock held -
        the same thing `execute_dispatches` does for its controls, and for
        the same reason: the decision that matters is the one true at the
        instant a worker would start, not the one T1 took.

        Fail-closed. An unreadable or absent document cannot prove the claim
        survived, and the cost of the two answers is not symmetric: refusing
        costs one retry, spawning an agent into a worktree somebody else now
        owns writes into a shared branch nothing can take back.
        """
        try:
            if not self.store.exists():
                return False
            snapshot = self.store.read()
        except (OSError, ValueError):
            return False
        if not isinstance(snapshot, dict):
            return False
        _, claim = self._current_dispatch_claim(snapshot, plan)
        return claim is not None

    def _execute_fixer_dispatch(self, plan: DispatchPlan) -> DispatchResult:
        """C-18 stage 6, Phase C. NO STATE LOCK IS HELD.

        Every call here used to run inside T1: the prompt file write, the
        worktree acquisition (a `git worktree list` and possibly a workmux
        subprocess), the real 127.0.0.1 bind, the job file write and the
        process spawn.

        THE REUSED WORKTREE IS WHY THE FIXER WAS SEQUENCED LAST.
        `reuse_if_checked_out=True` means this role does not get a private
        checkout - it works in whichever worktree currently holds the PR
        branch, which is the Builder's. That worktree is shared mutable
        state, and before C-18 the claim that authorised using it was taken
        in the same transaction as the spawn, so it could not go stale
        between the two. It can now. The claim is therefore re-verified
        against the committed document immediately before anything
        irreversible happens, and the dispatch is abandoned if it has moved
        on. See `_dispatch_claim_still_current`.
        """
        role_cfg = self.cfg.roles["fixer"]
        findings = list(plan.context.get("findings") or [])
        prompt_text = prompts.fixer({"id": plan.task_id}, plan.pr, plan.branch,
                                    self.cfg.github_repo, findings)
        prompt_path = prompts.write(plan.worker, prompt_text)

        # The Fixer commits to the Builder's PR branch, so it reuses the worktree
        # already holding that branch. It is still a fresh Claude context: a new
        # process, a new prompt, and only the reported findings in scope.
        path, why = workers.acquire_worktree(
            plan.worker, plan.branch, self.cfg.main_branch,
            self.cfg.tmux_session, prompt_path, reuse_if_checked_out=True,
        )
        if path is None:
            return DispatchResult(plan=plan, ok=False, reason=why)

        # Everything above this line is repeatable: the prompt write
        # overwrites its own path, and acquisition either reuses or runs
        # `workmux add --open-if-exists`. Everything below it is not - the
        # job file is the durable spawn evidence `resume_dispatch_claims`
        # reads, and `start_job` launches a paid agent into that worktree.
        if not self._dispatch_claim_still_current(plan):
            self.log("DISPATCH_EXECUTION_ABANDONED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION",
                     outcome="CLAIM_NO_LONGER_CURRENT",
                     metadata_redacted={"worktree": str(path)})
            return DispatchResult(
                plan=plan, ok=False,
                reason="fixer claim no longer current; refused to spawn into "
                       "the shared branch worktree")

        port = next((candidate for candidate in plan.port_candidates
                     if workers.probe_port(candidate)), None)
        if port is None:
            port_why = plan.context.get("port_exhaustion_reason") or \
                "no candidate port bound"
            self.log("PORT_ALLOCATION_FAILED", task_id=plan.task_id, role="fixer",
                     pr_id=plan.pr, activity_class="FAILED_WORK",
                     outcome="FAILED", metadata_redacted={"why": port_why})
            return DispatchResult(plan=plan, ok=False,
                                  reason=f"no governed port: {port_why}")

        job = workers.write_job(
            plan.worker, "fixer", role_cfg.provider, role_cfg.model,
            plan.task_id, path, prompt_path, self.tz, pr=plan.pr,
            hard_timeout_seconds=self.cfg.extra["timeouts"]["fixer"], port=port,
        )
        started = workers.start_job(plan.worker, job, path)
        if not started.ok:
            return DispatchResult(
                plan=plan, ok=False,
                reason=f"worker did not start: {started.stderr[:200]}")
        return DispatchResult(plan=plan, ok=True, worktree=str(path), port=port)

    def _commit_fixer_dispatch(self, doc: dict, plan: DispatchPlan,
                               result: DispatchResult) -> None:
        """C-18 stage 6, commit side. STATE ONLY.

        `repair_cycles` is incremented HERE and not at plan time, for the
        same reason the builder's `attempts` is: it counts repair cycles that
        actually started, and a dispatch that never spawned must not consume
        one of the governed `max_repair_cycles`.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="TASK_GONE")
            return
        if doc.get("workers", {}).get(plan.worker):
            # Already committed - a lost confirm converged by recovery, or a
            # recovery that raced its own confirm. Idempotent by design.
            self._clear_dispatch_claim(task, "fixer")
            return
        if claim is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="CLAIM_SUPERSEDED")
            return
        record = (doc.get("prs") or {}).get(str(plan.pr))
        if record is None:
            self.log("DISPATCH_COMMIT_REFUSED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="PR_RECORD_GONE")
            self._clear_dispatch_claim(task, "fixer")
            return

        role_cfg = self.cfg.roles["fixer"]
        findings = list(plan.context.get("findings") or [])
        record["repair_cycles"] += 1
        record["open_finding_ids"] = [f.get("id") if isinstance(f, dict) else None
                                      for f in findings]
        task["worker"] = plan.worker
        doc["workers"][plan.worker] = state_mod.new_worker_record(
            "fixer", plan.task_id, plan.branch, clock.iso(self.now()),
            pr=plan.pr, worktree=result.worktree, port=result.port,
            lease_expires_at=self._lease_expires("fixer"))
        if result.worktree:
            task.setdefault("retained_worktrees", {}).pop(result.worktree, None)
        self._clear_dispatch_claim(task, "fixer")
        self.transition(doc, plan.task_id, "FIX_REQUIRED",
                        f"fix cycle {record['repair_cycles']}")
        self.log("FIX_DISPATCHED", task_id=plan.task_id, pr_id=plan.pr,
                 role="fixer", provider=role_cfg.provider, model=role_cfg.model,
                 agent_id=plan.worker, activity_class="FIX", outcome="DISPATCHED",
                 metadata_redacted={"finding_ids": record["open_finding_ids"]})
        self.notify_out(doc, notify.INFO,
                        f"{plan.task_id} PR #{plan.pr}: fixer dispatched",
                        f"Repair cycle {record['repair_cycles']}.")

    def _fail_fixer_dispatch(self, doc: dict, plan: DispatchPlan,
                             result: DispatchResult) -> None:
        """C-18 stage 6, failure side. STATE ONLY.

        The claim is released first, so `route_awaiting_dispatch` is free to
        try again on the next tick, and then the unchanged annunciation runs.
        A claim that has already been superseded is NOT counted as a failure
        of this dispatch: some other outcome already owns the fixer slot.
        """
        task, claim = self._current_dispatch_claim(doc, plan)
        if task is None or claim is None:
            self.log("DISPATCH_FAILURE_REFUSED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION",
                     outcome="TASK_GONE" if task is None else "CLAIM_SUPERSEDED")
            return
        self._clear_dispatch_claim(task, "fixer")
        if str(plan.pr) not in (doc.get("prs") or {}):
            self.log("DISPATCH_FAILURE_REFUSED", task_id=plan.task_id,
                     role="fixer", pr_id=plan.pr, agent_id=plan.worker,
                     activity_class="ORCHESTRATION", outcome="PR_RECORD_GONE")
            return
        self.on_dispatch_failure(doc, task, plan.pr, "fixer", result.reason)

    def on_dispatch_failure(self, doc: dict, task: dict, pr_number: int, role: str,
                            reason: str) -> None:
        """A dispatch that fails must say so and stay recoverable.

        The deadlock this replaces was an unlogged early `return`: the task sat in
        REVIEW with no worker, stale detection does not cover REVIEW, and nothing
        would ever pick it up again. Now the failure is recorded, counted, and
        left in a state the supervisor retries on the next tick - and once the
        failures reach the existing repair-cycle limit it escalates to a human
        rather than retrying forever.

        SHARED BY THE REVIEWER AND THE FIXER, and reached two ways. Before
        C-18 the only caller was a dispatch that had failed inside T1, where
        the pull request record it just read was certain to be there. A
        harness role now also reaches it from the commit transaction that runs
        AFTER T1 has committed, and in that window the record can legitimately
        be gone - an externally merged pull request completes its task and the
        record goes with it. A missing record is therefore refused and logged,
        never invented and never allowed to abort the transaction with a
        KeyError that would also discard the claim release beside it. With the
        record present, every line below is exactly what it was.
        """
        record = (doc.get("prs") or {}).get(str(pr_number))
        if record is None:
            self.log("DISPATCH_FAILURE_REFUSED", task_id=task["id"],
                     pr_id=pr_number, role=role, activity_class="ORCHESTRATION",
                     outcome="PR_RECORD_GONE",
                     metadata_redacted={"reason": reason})
            return
        record["dispatch_failures"] = record.get("dispatch_failures", 0) + 1
        failures = record["dispatch_failures"]
        self.log("DISPATCH_FAILED", task_id=task["id"], pr_id=pr_number, role=role,
                 activity_class="FAILED_WORK", outcome="FAILED",
                 metadata_redacted={"reason": reason, "dispatch_failures": failures,
                                    "state": task["state"]})
        if failures >= self.cfg.max_repair_cycles:
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            f"{role} dispatch failed {failures} times")
            # The free-text failure reason stays in the notification body; the
            # durable intervention reason is structural only.
            self.request_intervention(
                doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                condition_code="dispatch_failure_limit",
                task_id=task["id"], pr_id=pr_number,
                reason=f"{role} dispatch failed {failures} times for "
                       f"{task['id']} PR #{pr_number}",
                title=f"{task['id']} PR #{pr_number}: {role} dispatch keeps failing",
                body=f"{failures} consecutive dispatch failures. "
                     f"Last reason: {reason}")

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

    # ------------------------------------------- C-05.3a security evidence

    def plan_security(self, doc: dict, task: dict, pr_number: int,
                      head_sha: str,
                      observation: routing.SecurityObservation,
                      *, slots_free: bool = True):
        """Phase B: claim one security attempt. STATE ONLY.

        Nothing here creates a directory, writes a job file, runs git,
        calls GitHub, opens a socket or spawns a process. The observations
        it classifies were gathered before the lock was taken, and the
        materialisation and spawn happen after it is released - that split
        is the whole point, and extending C-18's defect into the new path
        is what it exists to prevent.

        The ORDINAL is chosen here, under the exclusive state lock, and
        nowhere else. That is what makes it safe: two ticks cannot both
        decide "attempt 3", because only one holds the lock. routing's
        claim builder receives the number already chosen and derives the
        attempt id and worker name from it.

        Returns identifiers only - (task_id, pr, sha, attempt_id, worker) -
        never the task, record or claim dicts, which belong to this
        transaction's document and are stale the moment it commits. That
        is the C-14.1 rule route_prs already follows. None means there is
        nothing for the caller to do.
        """
        record = doc["prs"].get(str(pr_number))
        if record is None:
            return None

        # The head the observations describe and the head the claim would
        # be written for must be the same commit. If they disagree, this
        # would classify one SHA's evidence and then allocate or reuse an
        # attempt for another - attributing a review to a commit it never
        # looked at. There is no authoritative head on the PR record to
        # fall back on: reviewed_head belongs to the reviewer's cycle, not
        # to evidence, and borrowing it here would silently substitute a
        # different commit again.
        if head_sha != observation.head_sha:
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="HEAD_MISMATCH",
                     metadata_redacted={"head": head_sha,
                                        "observed": observation.head_sha})
            return None

        claim = record.get("security_evidence")
        recovery = routing.security_recovery_state(claim, observation)
        uncommitted = routing.security_spawn_uncommitted(claim, observation)

        # A stale-head claim whose worker may still be running must not be
        # replaced. The recovery table reads it as NO_CLAIM - correct about
        # the HEAD, which has nothing claimed - but overwriting the record
        # would erase the only durable identity a live attempt has, leaving a
        # paid provider review running that nothing owns and nothing can ever
        # ingest. Both facts needed are already observed, and an unsuccessful
        # scan is treated exactly like a live worker.
        if (recovery == routing.SECURITY_RECOVERY_NO_CLAIM
                and isinstance(claim, dict)
                and routing.security_claim_is_valid(claim)[0]
                and claim["sha"] != head_sha
                and (observation.worker_live or not observation.scan_ok)):
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="STALE_HEAD_ATTEMPT_LIVE",
                     metadata_redacted={"head": head_sha,
                                        "attempt_id": claim["attempt_id"],
                                        "scan_ok": observation.scan_ok,
                                        "worker_live": observation.worker_live})
            return None

        if recovery not in routing.SECURITY_RECOVERY_DISPATCHABLE:
            # RUNNING, INDETERMINATE, UNKNOWN and COMPLETE each mean the
            # same thing here: no new external work. Only COMPLETE will
            # ever produce anything, and ingesting it is Phase E's job.
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=recovery,
                     metadata_redacted={
                         "head": head_sha,
                         "recovery": recovery,
                         "spawn_uncommitted": uncommitted,
                         # Finite and CLAIM_*-prefixed, never exception
                         # prose: an operator holding on malformed evidence
                         # needs to know which rule it broke, not only that
                         # something is wrong. "" when the claim is fine
                         # and the hold is for an ordinary reason.
                         "claim_diagnostic":
                             routing.security_claim_diagnostic(claim)})
            return None

        # Every remaining recovery state ends in a PROVIDER WORKER BEING
        # SPAWNED, so every one of them is gated. That includes the reuse
        # branch below: NOT_MATERIALIZED means nothing has ever run, and
        # MATERIALIZED_NOT_SPAWNED means a directory exists but no job file
        # was ever written - in both, Phase C acquires a worktree and starts
        # a paid worker. An earlier version of this method placed the check
        # after the reuse branch on the reasoning that resuming a claim is
        # "recovery, not new work". That reasoning was wrong and the gap was
        # reproduced: a PLANNED claim spawned a paid security worker while
        # the run was FROZEN and the provider was in COOLDOWN.
        #
        # An existing claim is a RESERVATION, not permission to spend. What
        # it preserves is identity - the same ordinal, the same attempt
        # directory - and that survives the hold unchanged, so the attempt
        # resumes under its own identity on a later tick once the control
        # clears. Nothing is abandoned and no second ordinal is allocated.
        #
        # Publication, ingestion and release are all reached from elsewhere
        # in the tick and none of them passes through here, so blocking
        # dispatch never blocks evidence or cleanup.
        blocked = self._security_dispatch_block(doc)
        if blocked:
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=blocked,
                     metadata_redacted={"head": head_sha, "reason": blocked,
                                        "recovery": recovery})
            return None

        if recovery in (routing.SECURITY_RECOVERY_NOT_MATERIALIZED,
                        routing.SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED):
            # The claim already exists and is still the right one; the work
            # outside the lock simply has not finished. Re-use it exactly -
            # allocating a second ordinal here would abandon a directory
            # that may already hold evidence.
            #
            # Deliberately NOT capacity-checked. This claim already holds its
            # slot: _security_slots_free counts it when any OTHER pull
            # request asks, so counting it again here would double-count one
            # reservation and strand every resumed attempt at
            # max_security = 1. The reservation is real either way - what is
            # refused is a SECOND slot, not the one already held.
            return self._security_plan(task, pr_number, head_sha, claim)

        # A fresh attempt from here on, so it consumes a NEW concurrency
        # slot - unlike the reuse branch above, which already holds one.
        if not slots_free:
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="AT_CAPACITY",
                     metadata_redacted={"head": head_sha,
                                        "max_security": self.cfg.max_security})
            return None

        # NO_CLAIM or PROVEN_NOT_RUNNING: a fresh attempt. The ordinal
        # advances only when the previous one is for THIS head and is
        # provably finished; a claim for a different head starts again at
        # one, because the attempt namespace is scoped per (task, SHA).
        previous = claim if isinstance(claim, dict) else {}
        ordinal = 1
        if (recovery == routing.SECURITY_RECOVERY_PROVEN_NOT_RUNNING
                and previous.get("sha") == head_sha):
            ordinal = previous.get("ordinal", 0) + 1
            self.log("SECURITY_ATTEMPT_ABANDONED", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=security_contract.SPAWN_OR_RUN_INCOMPLETE,
                     metadata_redacted={
                         "head": head_sha,
                         "attempt_id": previous.get("attempt_id"),
                         "reason": security_contract.SPAWN_OR_RUN_INCOMPLETE})
        if ordinal > routing.SECURITY_ORDINAL_MAX:
            # The namespace is four digits wide. Refusing here is the
            # fail-closed end of a runaway, not a case to design around.
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="ORDINAL_EXHAUSTED",
                     metadata_redacted={"head": head_sha, "ordinal": ordinal})
            return None

        try:
            fresh = routing.security_claim(
                task_id=task["id"], sha=head_sha, ordinal=ordinal,
                claimed_at=clock.iso(self.now()),
                lease_expires_at=self._lease_expires("security"))
        except ValueError:
            # A claim state would refuse is worse than no claim: the
            # attempt would look made and never be actionable. Finite
            # outcome only - no exception prose reaches the ledger.
            self.log("SECURITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="CLAIM_REFUSED",
                     metadata_redacted={"head": head_sha, "ordinal": ordinal})
            return None

        record["security_evidence"] = fresh
        if task["state"] == "PR_OPEN":
            self.transition(doc, task["id"], "WAITING_EVIDENCE",
                            f"security evidence claimed for {head_sha[:12]}")
        self.log("SECURITY_EVIDENCE_CLAIMED", task_id=task["id"],
                 pr_id=pr_number, activity_class="ORCHESTRATION",
                 outcome=recovery,
                 metadata_redacted={"head": head_sha,
                                    "attempt_id": fresh["attempt_id"],
                                    "ordinal": ordinal,
                                    "spawn_uncommitted": uncommitted,
                                    "lease_expires_at": fresh["lease_expires_at"]})
        return self._security_plan(task, pr_number, head_sha, fresh)

    # -- C-05.3a Step 6b: observe -> plan -> execute -> commit ---------------
    #
    # Phase A gathers, T1 claims, Phase C does the external work with no lock
    # held, Phase D caches, Phase E ingests evidence that is ALREADY durable.
    # Phase E is deliberately not a reap_workers callback: reap_workers runs
    # inside T1, so a callback there that read worker output and published
    # files would put exactly the external work C-18 documents back inside the
    # transaction. Reading and publishing happen in Phase A/E-publish, outside
    # the lock; only the finished record crosses into T1.

    def observe_security(self, snapshot: dict, heads: dict,
                         entries: dict | None):
        """Phase A. Read-only, OUTSIDE the state lock.

        `snapshot` is a non-authoritative read of the document - T1 re-reads
        it under the lock before deciding anything. `entries` is one
        proc.worker_entry_processes scan for the whole tick; None means the
        scan failed and nothing about liveness may be concluded.

        Returns {pr_number: (head_sha, SecurityObservation, outcome_record,
        provenance_record)}. The outcome is read HERE so Phase E can ingest it
        inside T1 without the transaction touching the filesystem, and the
        provenance is read here for the same reason: T1 registers the
        attempt's worktree from it, so ownership survives a crash between
        acquisition and Phase D without the lock ever reaching a file.
        """
        observations: dict[int, tuple] = {}
        now = clock.iso(self.now())
        for task in snapshot.get("tasks", {}).values():
            pr_number = task.get("pr")
            if pr_number is None or task.get("state") not in self.EVIDENCE_STATES:
                continue
            head = heads.get(pr_number)
            if not head:
                continue
            record = snapshot.get("prs", {}).get(str(pr_number)) or {}
            claim = record.get("security_evidence")
            attempt_dir = job_file = outcome = provenance = None
            live = publishable = False
            if isinstance(claim, dict):
                try:
                    attempt_dir = gate_evidence.security_attempt_dir(
                        task["id"], claim.get("sha") or "",
                        claim.get("attempt_id") or "")
                except (ValueError, TypeError):
                    attempt_dir = None
                worker = claim.get("worker")
                if isinstance(worker, str) and entries is not None:
                    live = worker in entries
                if isinstance(worker, str):
                    job_file = config.WORKER_LOG_DIR / f"{worker}.job.json"
                if attempt_dir is not None:
                    provenance = gate_evidence.read_security_provenance(
                        attempt_dir)
                if claim.get("sha") == head:
                    outcome = gate_evidence.read_security_outcome(
                        task["id"], pr_number, head,
                        claim.get("attempt_id") or "")
                    # Is there a finished answer still waiting to be written?
                    # Asked with the SAME provenance gate the publisher uses,
                    # so the classifier can never hold for a publication the
                    # publisher would refuse to attempt.
                    publishable = (outcome is None and not live
                                   and entries is not None
                                   and self._security_output_pending(
                                       task["id"], pr_number, head, claim,
                                       attempt_dir, provenance))
            observations[pr_number] = (head, routing.SecurityObservation(
                head_sha=head, now=now, scan_ok=entries is not None,
                worker_live=live,
                attempt_dir_exists=bool(attempt_dir and attempt_dir.is_dir()),
                job_file_exists=bool(job_file and job_file.is_file()),
                outcome_present=outcome is not None,
                output_readable=publishable), outcome, provenance)
        return observations

    def _security_output_pending(self, task_id, pr_number, head, claim,
                                 attempt_dir, provenance) -> bool:
        """Terminal, provenance-verified provider output exists and is not
        published. Read-only; no lock held.

        Provenance is checked FIRST and failure is closed. Status and output
        are addressed by worker NAME, and a name is only this attempt's if
        something durable says so - without that, a directory left by an
        earlier attempt could answer for this one."""
        if not routing.security_claim_is_valid(claim)[0]:
            return False
        if not self._provenance_matches(task_id, pr_number, head, claim,
                                        provenance):
            return False
        if attempt_dir is None or not attempt_dir.is_dir():
            return False
        status = workers.read_status(claim["worker"]) or {}
        if status.get("phase") not in ("DONE", "FAILED", "TIMEOUT"):
            return False
        return True

    @staticmethod
    def _provenance_matches(task_id, pr_number, head, claim,
                            provenance) -> bool:
        """Does the durable provenance record name exactly this attempt?

        Missing provenance is a refusal, not a pass. An attempt that
        materialised but never acquired a worktree writes none, which is what
        makes that path fail closed instead of reading whatever artefacts
        happen to sit under the worker name.
        """
        if not isinstance(provenance, dict):
            return False
        return (provenance.get("task_id") == task_id
                and provenance.get("pr") == pr_number
                and provenance.get("sha") == head
                and provenance.get("attempt_id") == claim.get("attempt_id")
                and provenance.get("worker") == claim.get("worker"))

    EVIDENCE_STATES = ("PR_OPEN", "WAITING_EVIDENCE")

    # G4. The condition code the ledger and the intervention record key on,
    # so durable evidence does not have to be parsed as English.
    EVIDENCE_WAIT_EXHAUSTED = "evidence_wait_exhausted"

    def _evidence_wait_allowance(self) -> float | None:
        """The governed cumulative allowance, or None if it is not governed.

        None is NOT "no limit". `state_mod.evidence_wait_exhausted` treats
        an unusable allowance as exhausted, because an ungoverned bound is
        precisely the condition this check exists to prevent.
        """
        return self.cfg.extra["timeouts"].get("waiting_evidence_total_seconds")

    def _evidence_wait_exhausted(self, task: dict) -> bool:
        return state_mod.evidence_wait_exhausted(
            task, self.now(), self._evidence_wait_allowance())

    def _escalate_evidence_wait(self, doc: dict, task: dict,
                                pr_number: int) -> None:
        """G4 exhaustion: the governed HUMAN_REQUIRED path, once.

        Already in HUMAN_REQUIRED means a previous tick escalated; the
        intervention record deduplicates, but transitioning again would
        append a second history entry per tick forever.
        """
        if task["state"] == "HUMAN_REQUIRED":
            return
        waited = state_mod.evidence_wait_seconds(task, self.now())
        allowance = self._evidence_wait_allowance()
        # Bounded integers and fixed template only - C-08b.2 requires the
        # durable reason be structurally generated, never free text.
        self.transition(
            doc, task["id"], "HUMAN_REQUIRED",
            f"cumulative evidence wait exhausted after "
            f"{int(min(waited, 10 ** 9))}s")
        self.log("EVIDENCE_WAIT_EXHAUSTED", task_id=task["id"], pr_id=pr_number,
                 activity_class="ORCHESTRATION", outcome="HUMAN_REQUIRED",
                 metadata_redacted={
                     "waited_seconds": int(min(waited, 10 ** 9)),
                     "allowance_seconds": allowance
                     if isinstance(allowance, (int, float)) else None})
        self.request_intervention(
            doc, type_="HUMAN_PRODUCT_DECISION",
            condition_code=self.EVIDENCE_WAIT_EXHAUSTED,
            task_id=task["id"], pr_id=pr_number,
            reason=f"{task['id']} spent the governed cumulative evidence-wait "
                   f"allowance without every required evidence class passing",
            title=f"{task['id']}: evidence wait exhausted",
            body="The governed cumulative allowance is an experiment limit, "
                 "not a guarantee that all valid work fits inside it. Decide "
                 "whether to extend the allowance, reduce contention, or stop "
                 "this task.")

    def route_evidence(self, doc: dict, observations: dict) -> list:
        """T1. STATE ONLY - claim what needs claiming, ingest what is already
        durable, and return identifiers for the work to be done outside."""
        planned = []
        for task in sorted(doc["tasks"].values(), key=lambda t: t["id"]):
            pr_number = task.get("pr")
            if pr_number is None or task["state"] not in self.EVIDENCE_STATES:
                continue
            # G4. The governed cumulative evidence-wait allowance, checked
            # BEFORE anything else this task might do, and checked whether
            # or not an observation arrived - a task starved of
            # observations is exactly the one that would otherwise wait
            # forever, so gating this on `seen` would skip the case it
            # exists for.
            #
            # The allowance is an explicit experiment limit, not a claim
            # that all valid work fits inside it: a maximally contended
            # task can legitimately need about 4 h 06 m against an
            # allowance of 5 h. Exhaustion therefore escalates to a human
            # with a finite reason rather than failing the task.
            if task["state"] == state_mod.EVIDENCE_WAIT_STATE and \
                    self._evidence_wait_exhausted(task):
                self._escalate_evidence_wait(doc, task, pr_number)
                continue
            seen = observations.get(pr_number)
            if seen is None:
                continue
            head, observation, outcome, provenance = seen
            # Ownership before anything else, and regardless of what happens
            # next. The worktree named here physically exists; registering it
            # is what stops the Watchdog reporting a working attempt as an
            # orphan, and re-registering every tick is what covers a crash
            # between acquisition and Phase D. Idempotent by construction -
            # the map is keyed by path.
            self._own_security_worktree(doc, task, provenance)
            if outcome is not None:
                self.ingest_security(doc, task, pr_number, head, outcome)
                continue
            # The other way evidence completes: the security result landed
            # on an earlier tick and an accessibility leg finished on this
            # one. Without this the task would hold until the NEXT security
            # ingest - which for a passing security review never comes.
            if self.advance_if_evidence_complete(doc, task, pr_number, head):
                continue
            # C-05.3b. Planned in the SAME transaction as the security
            # claim, and accumulated rather than returned, because the
            # security plan below returns through a different path. Only
            # when a services factory exists - see __init__.
            # getattr, because the safe default is "do not plan". A
            # Supervisor built without __init__ - which several suites do
            # deliberately - must not start claiming ports it has no way
            # to execute against.
            if getattr(self, "accessibility_services_factory", None) is not None:
                a11y = self.plan_accessibility_auto(doc, task, pr_number, head)
                if a11y is not None:
                    self._accessibility_plans.append((task["id"], a11y))
            identifiers = self.plan_security(
                doc, task, pr_number, head, observation,
                slots_free=self._security_slots_free(doc, pr_number))
            if identifiers:
                planned.append(identifiers)
        return planned

    @staticmethod
    def _security_plan(task: dict, pr_number: int, head_sha: str,
                       claim: dict) -> routing.SecurityPlan:
        """Copy out the immutable facts Phase C needs, and nothing else.

        Every field is a scalar read here, under the lock, and frozen. No
        reference to the task, PR record or claim leaves this transaction:
        by the time Phase C runs, the document they came from has committed
        and may already have moved on, so a retained reference would be read
        - or mutated - outside the lock. SecurityPlan re-derives and
        re-validates the worker name, so a plan that disagreed with its own
        identity cannot reach the spawn.
        """
        return routing.SecurityPlan(
            task_id=task["id"], task_title=task["title"], pr=pr_number,
            sha=head_sha, attempt_id=claim["attempt_id"],
            worker=claim["worker"])

    def _security_dispatch_block(self, doc: dict) -> str:
        """Why a NEW security attempt may not start, or "" if it may.

        Every control here already exists and already governs some other
        dispatch site; none is invented for this path.

        * frozen_at - dispatchable() refuses new builders on it, and a freeze
          set by cli.py's human freeze survives into ticks where cs.expired is
          False, so tick's expiry return does not cover it.
        * stopping - a SIGTERM has been received. Claiming an attempt whose
          spawn the shutdown will skip leaves a PLANNED claim to recover, and
          spawning a paid worker during shutdown is worse.
        * providers.may(doc, "review") - the capability a security review IS.
          dispatch_reviewer gates on exactly this, and the ledger already
          classifies this work REVIEW.
        * providers.usable(doc, <security provider>) - that provider in
          COOLDOWN or UNAVAILABLE cannot serve the call being dispatched.

        NOT gated on budget, and deliberately not: `budget.hard_stop` governs
        metered OpenRouter/Jev spend via metered_call_allowed, which is its
        only consumer. No control anywhere in this repository gates
        subscription-provider worker dispatch on budget - builder, fixer,
        reviewer and observer are all ungated - so claiming a budget control
        here would be claiming one that does not exist. Recorded as a gap in
        the handover rather than invented.
        """
        if doc.get("frozen_at"):
            return "RUN_FROZEN"
        if self.stopping:
            return "SUPERVISOR_STOPPING"
        # Provider state that cannot be read is not provider state that
        # permits spending. providers.ensure runs at the top of every tick,
        # so an absent bucket means the document being consulted is not one
        # this control plane wrote - and a paid worker must not start on an
        # unreadable control.
        try:
            if not providers.may(doc, "review"):
                return "REVIEW_NOT_PERMITTED"
            if not providers.usable(doc, self.cfg.roles["security"].provider):
                return "PROVIDER_UNAVAILABLE"
        except (KeyError, TypeError, AttributeError):
            return "CONTROLS_UNREADABLE"
        return ""

    def _security_slots_free(self, doc: dict, pr_number: int) -> bool:
        """Is there capacity for a NEW security attempt on this PR?

        Counts every OTHER pull request holding an attempt that is not
        COMPLETE - which is deliberately the widest reading available:

        * a stale-head attempt still counts, because its worker may be
          running and its worktree certainly exists;
        * an attempt whose liveness could not be established counts, because
          unknown liveness must never free capacity. The count reads
          claim_state, not the /proc scan, so a failed scan cannot silently
          release a slot.

        This PR's own claim is excluded because resuming or replacing an
        attempt on this record consumes no additional concurrency - one PR
        record holds exactly one claim - and counting it would make a single
        PR unable to ever re-attempt at max_security = 1.
        """
        used = 0
        for key, record in (doc.get("prs") or {}).items():
            if key == str(pr_number):
                continue
            claim = record.get("security_evidence")
            if isinstance(claim, dict) and claim.get("claim_state") != "COMPLETE":
                used += 1
        return used < self.cfg.max_security

    def _own_security_worktree(self, doc: dict, task: dict,
                               provenance) -> None:
        """Assert durable ownership of an attempt's worktree. STATE ONLY.

        Uses the existing retained_worktrees map unchanged - same key, same
        two fields - because that map is already read by
        reconcile.detect_orphans as proof a worktree has an owner, which is
        exactly the claim being made. The attempt's own lifecycle detail
        lives in its durable provenance record, not here, so this map keeps
        the shape and meaning the rest of the control plane relies on.

        Deliberately NOT removed when the claim reaches COMPLETE: the
        directory still physically exists then, and ownership that ends
        before the resource does is how a real worktree becomes an orphan.
        Only a successful physical release drops it.
        """
        if not isinstance(provenance, dict):
            return
        path = provenance.get("worktree")
        if not isinstance(path, str) or not path:
            return
        retained = task.setdefault("retained_worktrees", {})
        if path not in retained:
            retained[path] = {"retained_at": clock.iso(self.now()),
                              "why": SECURITY_WORKTREE_WHY}

    def execute_security(self, planned: list) -> list:
        """Phase C. External work, with NO state lock held.

        Materialises the exact claimed attempt, writes the job file and
        spawns. Returns the identifiers that actually spawned, so Phase D
        caches only what happened.

        The controls are re-read HERE as well as in T1, because T1 has
        already committed and released the lock by the time this runs. A
        freeze arriving from `ctl freeze`, or a provider entering COOLDOWN,
        lands in the state document between the two - and the decision that
        matters is the one true at the instant a paid worker would start,
        not the one true when it was planned.

        The re-read uses the same non-transactional state observation Phase A
        uses (`store.read()`), so it takes no lock and performs no external
        work: no git, no GitHub, no process, no socket. A plan whose controls
        have since closed is dropped, and its claim stays exactly as it is -
        same ordinal, same attempt directory - to resume on a later tick.
        """
        spawned = []
        snapshot = self.store.read() if self.store.exists() else {}
        for plan in planned:
            if self.stopping:
                # A stop signal arrived after T1 committed. The claim is
                # durable and resumes on the next start; spawning a paid
                # worker the shutdown is about to orphan is not recoverable.
                self.log("SECURITY_EXECUTION_FAILED", task_id=plan.task_id,
                         pr_id=plan.pr, activity_class="ORCHESTRATION",
                         outcome="SUPERVISOR_STOPPING",
                         metadata_redacted={"attempt_id": plan.attempt_id})
                break
            blocked = self._security_dispatch_block(snapshot)
            if blocked:
                self.log("SECURITY_EXECUTION_FAILED", task_id=plan.task_id,
                         pr_id=plan.pr, activity_class="ORCHESTRATION",
                         outcome=blocked,
                         metadata_redacted={"attempt_id": plan.attempt_id,
                                            "phase": "EXECUTE"})
                continue
            task_id, pr_number, head = plan.task_id, plan.pr, plan.sha
            attempt_id, worker = plan.attempt_id, plan.worker
            try:
                attempt = gate_evidence.security_attempt_dir(task_id, head,
                                                             attempt_id)
                attempt.mkdir(parents=True, exist_ok=True)
            except (ValueError, OSError):
                self.log("SECURITY_EXECUTION_FAILED", task_id=task_id,
                         pr_id=pr_number, activity_class="ORCHESTRATION",
                         outcome="MATERIALISE_FAILED",
                         metadata_redacted={"attempt_id": attempt_id})
                continue
            role_cfg = self.cfg.roles["security"]
            # The real task title, carried immutably out of T1. Passing the
            # task id here instead rendered "Task: TASK-001" into the prompt
            # and told the reviewer nothing about what the task was for.
            # The branch reported is the one the worktree is actually on.
            branch = f"security/{attempt_id}/{head[:12]}"
            prompt_text = prompts.security(
                {"id": task_id, "title": plan.task_title}, pr_number,
                branch, self.cfg.github_repo, 1)
            prompt_path = prompts.write(worker, prompt_text)
            path, why = workers.acquire_worktree(
                worker, branch, head,
                self.cfg.tmux_session, prompt_path,
                reuse_if_checked_out=False)
            if path is None:
                self.log("SECURITY_EXECUTION_FAILED", task_id=task_id,
                         pr_id=pr_number, activity_class="ORCHESTRATION",
                         outcome="WORKTREE_UNAVAILABLE",
                         metadata_redacted={"attempt_id": attempt_id,
                                            "detail": why[:200]})
                continue
            # Provenance BEFORE the spawn, and before the job file. It binds
            # this worker name and this worktree to this exact (task, PR, SHA,
            # attempt), and the publisher refuses to read any artefact
            # without it. Written here rather than after start_job so a crash
            # between the two still leaves an owned, attributable attempt;
            # not written at all when acquisition failed above, which is what
            # makes that path fail closed rather than read whatever a
            # previous attempt left under the same name.
            if not gate_evidence.write_security_provenance(attempt, {
                    "task_id": task_id, "pr": pr_number, "sha": head,
                    "attempt_id": attempt_id, "worker": worker,
                    "worktree": str(path)}):
                self.log("SECURITY_EXECUTION_FAILED", task_id=task_id,
                         pr_id=pr_number, activity_class="ORCHESTRATION",
                         outcome="PROVENANCE_UNWRITABLE",
                         metadata_redacted={"attempt_id": attempt_id})
                continue
            job = workers.write_job(
                worker, "security", role_cfg.provider, role_cfg.model, task_id,
                path, prompt_path, self.tz, pr=pr_number,
                hard_timeout_seconds=self.cfg.extra["timeouts"]["security"],
                effort=role_cfg.effort)
            started = workers.start_job(worker, job, path)
            if not started.ok:
                self.log("SECURITY_EXECUTION_FAILED", task_id=task_id,
                         pr_id=pr_number, activity_class="ORCHESTRATION",
                         outcome="SPAWN_FAILED",
                         metadata_redacted={"attempt_id": attempt_id})
                continue
            self.log("SECURITY_DISPATCHED", task_id=task_id, pr_id=pr_number,
                     role="security", provider=role_cfg.provider,
                     model=role_cfg.model, agent_id=worker,
                     activity_class="REVIEW", outcome="DISPATCHED")
            spawned.append(plan)
        return spawned

    def publish_security_results(self, snapshot: dict,
                                 entries: dict | None) -> None:
        """Phase E, publish half. Reads worker output and writes the durable
        outcome - all OUTSIDE the state lock.

        Publication comes BEFORE ingestion commits, and a failure to publish
        leaves the claim exactly as it was. Nothing is marked COMPLETE, no
        findings are applied, the task does not advance, and no replacement
        attempt is launched merely because a write failed - the attempt's
        evidence stays on disk so the next tick can retry.

        Driven from DURABLE PROVENANCE, not from the current claim, and that
        is what lets a superseded attempt still be published. An attempt whose
        claim was replaced by a newer head's has no claim left to find, and a
        claim-driven publisher therefore never revisited it: its answer stayed
        unwritten and its worktree, which may only be released once
        publication is durable, was retained for the rest of the run. Every
        attempt that reached the point of having a worktree has a provenance
        record naming its exact task, PR, full SHA, attempt and worker, so
        that record is a complete and sufficient identity - and each outcome
        is published under it, never under whatever the record holds now.

        Publishing a superseded outcome is safe because ingestion is bound
        separately: _current_claim re-checks SHA and attempt id, so a stale
        outcome can never reach the current task's findings, debt or state.

        scan_ok is deliberately ignored here. An incomplete walk means some
        attempts were not seen, which costs a retry; it is not an assertion
        about absence, unlike release, where it must fail closed.
        """
        if entries is None:
            return  # the scan failed: no worker can be proven finished
        records, _ = gate_evidence.scan_security_attempts()
        for record in records:
            worker = record["worker"]
            if worker in entries:
                continue  # still running
            if gate_evidence.read_security_outcome(
                    record["task_id"], record["pr"], record["sha"],
                    record["attempt_id"]):
                continue  # already published; nothing to redo
            status = workers.read_status(worker) or {}
            if status.get("phase") not in ("DONE", "FAILED", "TIMEOUT"):
                continue  # not provably finished, and the lease still governs
            self._publish_one_security_result(record, status)

    # -------------------------------------------------- Phase F: release
    #
    # Physical cleanup, with NO state lock held. Split from publication
    # because releasing is the one irreversible step here: a worktree removed
    # while its worker is alive, or before its answer is durable, destroys
    # evidence that cannot be rebuilt.

    def release_security_worktrees(self, snapshot: dict,
                                   entries: dict | None) -> None:
        """Remove finished security worktrees. OUTSIDE any transaction.

        Three conditions, all required, none inferable from the others:

        1. liveness is ESTABLISHED and the worker is absent. entries is None
           means the scan failed, which proves nothing - so nothing is
           removed, rather than removed on an assumption;
        2. the attempt's outcome is durably published. Removing the worktree
           first would discard the run that produced an answer nothing has
           recorded yet;
        3. the claim that governs the attempt is COMPLETE, so ingestion has
           already taken what it needs.

        Driven from durable provenance rather than from the current claim, so
        a superseded attempt - whose claim has since been replaced by a newer
        head's - is still released rather than leaked.

        A failed removal changes nothing: ownership stays asserted, the
        failure is durable and finite in the ledger, and the next tick
        retries. Cleanup that fails silently is how a leak becomes invisible.
        """
        if entries is None:
            return
        records, scan_ok = gate_evidence.scan_security_attempts()
        if not scan_ok:
            return  # an incomplete walk cannot prove anything is releasable
        for record in records:
            worker = record["worker"]
            if worker in entries:
                continue  # still running
            if not gate_evidence.read_security_outcome(
                    record["task_id"], record["pr"], record["sha"],
                    record["attempt_id"]):
                continue  # no durable answer yet; publication retries first
            if not self._security_attempt_settled(snapshot, record):
                continue
            self._release_one_security_worktree(record)

    @staticmethod
    def _security_attempt_settled(snapshot: dict, record: dict) -> bool:
        """Has the claim governing this attempt finished with it?

        True when the attempt's own claim is COMPLETE, and equally when the
        record no longer holds that attempt at all - a superseded attempt is
        settled by definition, because nothing will ever ingest it again.
        """
        pr_record = (snapshot.get("prs") or {}).get(str(record["pr"])) or {}
        claim = pr_record.get("security_evidence")
        if not isinstance(claim, dict):
            return True
        if claim.get("attempt_id") != record["attempt_id"] or \
                claim.get("sha") != record["sha"]:
            return True  # superseded: this attempt is nobody's current work
        return claim.get("claim_state") == "COMPLETE"

    def _release_one_security_worktree(self, record: dict) -> None:
        path = record["worktree"]
        if not Path(path).exists():
            self._drop_security_worktree(record["task_id"], path)
            return
        result = workers.remove_worker(record["worker"])
        if not result.ok:
            self.log("SECURITY_WORKTREE_RELEASE_FAILED",
                     task_id=record["task_id"], pr_id=record["pr"],
                     agent_id=record["worker"], activity_class="ORCHESTRATION",
                     outcome="REMOVE_FAILED",
                     metadata_redacted={"attempt_id": record["attempt_id"]})
            return
        self._drop_security_worktree(record["task_id"], path)
        self.log("SECURITY_WORKTREE_RELEASED", task_id=record["task_id"],
                 pr_id=record["pr"], agent_id=record["worker"],
                 activity_class="ORCHESTRATION", outcome="RELEASED",
                 metadata_redacted={"attempt_id": record["attempt_id"]})

    def _drop_security_worktree(self, task_id: str, path: str) -> None:
        """Ownership ends only after the resource does. Its own small
        transaction, and only this lifecycle's own entries are touched."""
        with self.store.transaction() as doc:
            task = doc.get("tasks", {}).get(task_id)
            if not task:
                return
            retained = task.get("retained_worktrees") or {}
            entry = retained.get(path)
            if isinstance(entry, dict) and entry.get("why") == SECURITY_WORKTREE_WHY:
                retained.pop(path, None)

    def _publish_one_security_result(self, provenance: dict, status) -> None:
        """Adjudicate and durably publish ONE attempt, under its own identity.

        Every identifier comes from the attempt's provenance record - the task,
        PR, full SHA and attempt that actually produced this output - never
        from whatever the PR record holds now. A superseded attempt therefore
        publishes as itself, which is the only attribution that is true.
        """
        task_id, pr_number = provenance["task_id"], provenance["pr"]
        head, worker = provenance["sha"], provenance["worker"]
        attempt_id = provenance["attempt_id"]
        attempt = gate_evidence.security_attempt_dir(task_id, head, attempt_id)
        text = workers.worker_output(worker, 40000)
        last = config.WORKER_LOG_DIR / f"{worker}.last.txt"
        if last.exists():
            try:
                text = last.read_text(encoding="utf-8")
            except OSError:
                pass
        result = {"exit_code": status.get("exit_code"),
                  "timed_out": status.get("phase") == "TIMEOUT",
                  "duration_ms": status.get("duration_ms")}
        role_cfg = self.cfg.roles["security"]
        kwargs = dict(task_id=task_id, pr=pr_number, sha=head,
                      attempt_id=attempt_id, result=result,
                      provider=role_cfg.provider, model=role_cfg.model)
        if status.get("phase") == "TIMEOUT":
            record = gate_evidence.normalize_security_outcome(
                reason=security_contract.TIMED_OUT, **kwargs)
        elif status.get("phase") == "FAILED":
            record = gate_evidence.normalize_security_outcome(
                reason=security_contract.PROVIDER_FAILURE, **kwargs)
        elif not text.strip():
            record = gate_evidence.normalize_security_outcome(
                reason=security_contract.OUTPUT_MISSING, **kwargs)
        else:
            record = gate_evidence.normalize_security_outcome(
                review=routing.parse_security(text), **kwargs)
        published = gate_evidence.publish_security_outcome(attempt, record)
        self.log("SECURITY_OUTCOME_PUBLISHED", task_id=task_id,
                 pr_id=pr_number, activity_class="REVIEW",
                 outcome="PUBLISHED" if published else "PUBLISH_FAILED",
                 metadata_redacted={"head": head,
                                    "attempt_id": attempt_id,
                                    "status": record["status"],
                                    "reason": record["reason"]})

    def confirm_security_spawn(self, spawned: list) -> None:
        """Phase D. Cache only.

        Correctness does not rest on this committing: the recovery table
        already reads a claim with a job file as spawned, so a crash before
        this lands is recovered from the filesystem, not from the cache.
        """
        if not spawned:
            return
        with self.store.transaction() as doc:
            for plan in spawned:
                claim = self._current_claim(doc, plan.task_id, plan.pr,
                                            plan.sha, plan.attempt_id)
                if claim and claim["claim_state"] == "PLANNED":
                    claim["claim_state"] = "SPAWNED"

    @staticmethod
    def _current_claim(doc: dict, task_id: str, pr_number: int, head: str,
                       attempt_id: str):
        """The claim ONLY if it is still the exact one that was acted on.

        Task association, PR, head SHA and attempt identity all re-checked.
        A delayed result must never update a newer attempt for the same SHA,
        so the attempt id is compared too - the SHA alone would let attempt 2
        be overwritten by attempt 1's late answer.
        """
        task = doc.get("tasks", {}).get(task_id)
        if not task or task.get("pr") != pr_number:
            return None
        record = doc.get("prs", {}).get(str(pr_number)) or {}
        claim = record.get("security_evidence")
        if not isinstance(claim, dict):
            return None
        if claim.get("sha") != head or claim.get("attempt_id") != attempt_id:
            return None
        return claim

    def ingest_security(self, doc: dict, task: dict, pr_number: int,
                        head: str, outcome: dict) -> None:
        """Phase E. Commit an outcome that is ALREADY durably published.

        Idempotent: a claim already COMPLETE is left alone, so repeated ticks
        cannot duplicate findings or debt. Apparatus failure stays apparatus
        failure - it never becomes a reviewer SECURITY_FAIL.
        """
        claim = self._current_claim(doc, task["id"], pr_number, head,
                                    outcome.get("attempt_id") or "")
        if claim is None or claim["claim_state"] == "COMPLETE":
            return
        record = doc["prs"][str(pr_number)]
        status = outcome.get("status")
        verdict = outcome.get("verdict")

        if status != gate_evidence.COMPLETED:
            claim["claim_state"] = "COMPLETE"
            claim["reason"] = outcome.get("reason") or \
                security_contract.SPAWN_OR_RUN_INCOMPLETE
            self.log("SECURITY_RESULT", task_id=task["id"], pr_id=pr_number,
                     role="security", activity_class="REVIEW",
                     outcome=claim["reason"],
                     metadata_redacted={"head": head, "status": status,
                                        "attempt_id": claim["attempt_id"]})
            return

        findings = outcome.get("findings") or []
        blocking = [f for f in findings
                    if f.get("severity") in routing.BLOCKING_SEVERITIES]
        debt_entries = [f for f in findings
                        if f.get("severity") in ("P2", "P3")]
        claim["claim_state"] = "COMPLETE"
        claim["verdict"] = verdict
        record["security_debt"] = debt_entries
        self.log("SECURITY_RESULT", task_id=task["id"], pr_id=pr_number,
                 role="security", activity_class="REVIEW", outcome=verdict,
                 metadata_redacted={"head": head,
                                    "attempt_id": claim["attempt_id"],
                                    "counts": outcome.get("finding_counts"),
                                    "blocking": len(blocking),
                                    "debt": len(debt_entries)})
        if verdict == security_contract.SECURITY_FAIL:
            # Only P0/P1 block. P2/P3 are recorded debt and never
            # independently force remediation (C-05b).
            record["pending_findings"] = blocking
            self.transition(doc, task["id"], "FIX_REQUIRED",
                            f"security findings on {head[:12]}")
        # C-05.3b: a SECURITY_PASS no longer holds unconditionally.
        #
        # It used to, and the comment here said so: "SECURITY_PASS
        # deliberately does NOT advance to REVIEW: accessibility is a
        # required evidence class and is not implemented until C-05.3b, so
        # the task holds in WAITING_EVIDENCE." That hold WAS correct - but
        # it was also unconditional, and WAITING_EVIDENCE had no exit at
        # all, which is audit row C-20(a): every product PR deadlocked
        # there forever with no diagnostic saying why.
        #
        # The hold is now a GATE rather than a stop. routing.review_gate_fires
        # answers the question the hold was standing in for - is every
        # required evidence class a completed pass at THIS head - so a task
        # advances exactly when that is true and holds whenever it is not.
        # Until the accessibility legs are dispatched the gate cannot fire,
        # so behaviour is unchanged today; what changed is that the exit
        # exists and is governed by evidence rather than by a comment.
        self.advance_if_evidence_complete(doc, task, pr_number, head)

    # ============================================================
    # C-05.3b: the AUTOMATED accessibility half, on G2/G9's lifecycle.
    #
    # plan   -> T1, state only: pick a port WITHOUT binding, write the
    #           claim. The claim is the durable reservation, and it is
    #           what makes the port unavailable to a Builder.
    # execute-> outside every transaction: install, build, serve, scan,
    #           tear down. accessibility_evidence.run_attempt owns that
    #           sequence; nothing here performs it.
    # commit -> its own transaction, re-verifying the claim and the head
    #           before anything the attempt said is believed.
    # ============================================================

    def _accessibility_budget(self) -> accessibility_evidence.AttemptBudget:
        return accessibility_evidence.AttemptBudget.from_timeouts(
            self.cfg.extra["timeouts"])

    def _accessibility_auto_slots_free(self, doc: dict, pr_number: int) -> bool:
        """G7's bound, counting claims rather than processes.

        A claim that has not spawned yet still occupies the slot: counting
        live servers instead would let two attempts start between a plan
        and its execute.
        """
        used = 0
        for number, record in (doc.get("prs") or {}).items():
            if str(number) == str(pr_number):
                continue
            claim = record.get("accessibility_auto") \
                if isinstance(record, dict) else None
            if isinstance(claim, dict) and claim.get("port_released") is not True:
                used += 1
        return used < self.cfg.max_accessibility_auto

    def _accessibility_auto_dispatch_block(self, doc: dict) -> str:
        """Why a NEW automated attempt may not start, or "" if it may.

        DELIBERATELY NOT the security dispatch block, which also gates on
        `providers.may(doc, "review")` and on the security provider being
        usable. The automated half consults NO provider: it installs,
        builds, serves and drives a local browser. Gating it on a provider
        it never calls would hold accessibility evidence hostage to an
        unrelated outage, and would be claiming a control that does not
        apply rather than one that does.

        The two that DO apply are the two about this process:

        * frozen_at - a freeze set by cli.py's human freeze survives into
          ticks where the clock has not expired, so tick's expiry return
          does not cover it;
        * stopping - a SIGTERM has arrived. Starting a build and a server
          that the shutdown will not tear down is how a port leaks past
          the run.
        """
        if doc.get("frozen_at"):
            return "RUN_FROZEN"
        if self.stopping:
            return "SUPERVISOR_STOPPING"
        return ""

    @staticmethod
    def _next_accessibility_attempt_id(claim) -> str | None:
        """attempt-NNNN for the next attempt, or None if the space is spent.

        Derived from the existing claim rather than from a new counter
        field, so a record written before C-05.3b needs no migration.
        """
        if not isinstance(claim, dict):
            return routing.ACCESSIBILITY_ATTEMPT_FMT.format(1)
        current = claim.get("attempt_id")
        if not isinstance(current, str) or \
                not routing._ACCESSIBILITY_ATTEMPT_RE.match(current):
            return routing.ACCESSIBILITY_ATTEMPT_FMT.format(1)
        ordinal = int(current.split("-")[1]) + 1
        if ordinal > routing.ACCESSIBILITY_ORDINAL_MAX:
            # Four digits wide, so 9999 is the last attempt that can be
            # NAMED. Minting a five-digit id would produce a claim its own
            # validator refuses - worse than refusing to mint one.
            return None
        return routing.ACCESSIBILITY_ATTEMPT_FMT.format(ordinal)

    def plan_accessibility_auto(self, doc: dict, task: dict, pr_number: int,
                                head: str):
        """T1. Claim a port and an attempt, or return None. STATE ONLY.

        Performs no bind, no network operation and no filesystem read -
        `select_port_candidates` is the no-bind half C-18 stage 3 split
        out precisely so this can run under the lock.
        """
        record = doc["prs"].get(str(pr_number))
        if not isinstance(record, dict):
            return None
        claim = record.get("accessibility_auto")
        if isinstance(claim, dict):
            # An unreleased claim for THIS head is still in flight; one for
            # another head is stale and may be replaced, but only once its
            # port is back - otherwise a head move would leak a server.
            if claim.get("port_released") is not True:
                return None
            if claim.get("sha") == head and claim.get("claim_state") == "COMPLETE" \
                    and claim.get("verdict") is not None:
                return None  # already answered for this head
        if not self._accessibility_auto_slots_free(doc, pr_number):
            return None
        blocked = self._accessibility_auto_dispatch_block(doc)
        if blocked:
            self.log("ACCESSIBILITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=blocked, metadata_redacted={"head": head})
            return None

        attempt_id = self._next_accessibility_attempt_id(claim)
        if attempt_id is None:
            self.log("ACCESSIBILITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="ORDINAL_EXHAUSTED",
                     metadata_redacted={"head": head})
            return None
        candidates, why = workers.select_port_candidates(doc)
        if not candidates:
            self.log("ACCESSIBILITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="NO_PORT", metadata_redacted={"head": head})
            return None

        fresh = {
            "sha": head,
            "attempt_id": attempt_id,
            "claim_state": "PLANNED",
            "claimed_at": clock.iso(self.now()),
            "port": candidates[0],
            "port_released": False,
            "verdict": None,
            "reason": "",
        }
        ok, diagnostic = routing.accessibility_auto_claim_is_valid(fresh)
        if not ok:
            # A claim its own validator refuses is worse than no claim:
            # the attempt would look made and never be actionable.
            self.log("ACCESSIBILITY_EVIDENCE_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=diagnostic, metadata_redacted={"head": head})
            return None
        record["accessibility_auto"] = fresh
        self.log("ACCESSIBILITY_EVIDENCE_CLAIMED", task_id=task["id"],
                 pr_id=pr_number, activity_class="ORCHESTRATION",
                 outcome="PLANNED",
                 metadata_redacted={"head": head, "attempt_id": attempt_id,
                                    "port": fresh["port"]})
        return accessibility_evidence.AccessibilityPlan(
            task_id=task["id"], pr=pr_number, sha=head,
            attempt_id=attempt_id, port=fresh["port"])

    def ingest_accessibility_auto(self, doc: dict, task: dict, pr_number: int,
                                  head: str, plan, outcome) -> None:
        """Commit one attempt's answer, re-verifying what it acted on.

        G9: claim ownership AND the exact head are revalidated before any
        of this is believed. A result for another attempt, another head or
        a claim that has moved on is DISCARDED - it is evidence about a
        world that no longer exists.
        """
        record = doc["prs"].get(str(pr_number))
        if not isinstance(record, dict):
            return
        claim = record.get("accessibility_auto")
        if not isinstance(claim, dict):
            return
        if claim.get("sha") != head or \
                claim.get("attempt_id") != plan.attempt_id or \
                claim.get("port") != plan.port:
            self.log("ACCESSIBILITY_RESULT_DISCARDED", task_id=task["id"],
                     pr_id=pr_number, activity_class="REVIEW",
                     outcome="CLAIM_MOVED",
                     metadata_redacted={"head": head,
                                        "attempt_id": plan.attempt_id})
            return
        if claim.get("claim_state") == "COMPLETE" and \
                claim.get("port_released") is True:
            return  # idempotent: a repeated tick cannot duplicate findings

        claim["claim_state"] = "COMPLETE"
        claim["port_released"] = bool(outcome.port_released)
        if outcome.status != accessibility_evidence.COMPLETED:
            # An apparatus failure is NOT a product verdict. A run that did
            # not happen is not evidence that the page is inaccessible, so
            # this never routes to FIX_REQUIRED (gate_evidence's rule 2).
            claim["verdict"] = None
            claim["reason"] = outcome.reason
            self.log("ACCESSIBILITY_RESULT", task_id=task["id"],
                     pr_id=pr_number, role="accessibility",
                     activity_class="REVIEW", outcome=outcome.reason,
                     metadata_redacted={"head": head, "phase": outcome.phase,
                                        "attempt_id": plan.attempt_id,
                                        "port_released": claim["port_released"]})
            return

        claim["verdict"] = outcome.verdict
        claim["reason"] = ""
        findings = accessibility_evidence.findings_for(outcome)
        blocking = [f for f in findings
                    if routing.accessibility_findings_block_merge([f])]
        record["accessibility_findings"] = findings
        self.log("ACCESSIBILITY_RESULT", task_id=task["id"], pr_id=pr_number,
                 role="accessibility", activity_class="REVIEW",
                 outcome=outcome.verdict,
                 metadata_redacted={"head": head,
                                    "attempt_id": plan.attempt_id,
                                    "findings": len(findings),
                                    "blocking": len(blocking),
                                    "port_released": claim["port_released"]})
        if outcome.verdict == accessibility_contract.ACCESSIBILITY_AUTO_FAIL:
            record["pending_findings"] = blocking
            self.transition(doc, task["id"], "FIX_REQUIRED",
                            f"accessibility findings on {head[:12]}")
            return
        self.advance_if_evidence_complete(doc, task, pr_number, head)

    def execute_accessibility(self, plans: list) -> list:
        """Phase C. Run each planned attempt with NO transaction held.

        G9: the install, build, serve, scan and teardown all happen here,
        after T1 has committed. Returns (task_id, plan, outcome) triples
        for the commit phase; it writes no state itself, so a crash in
        here leaves the durable claim exactly as T1 left it and the next
        tick can see an attempt that never produced an answer.
        """
        results = []
        budget = self._accessibility_budget()
        for task_id, plan in plans:
            started = time.monotonic()
            services = self.accessibility_services_factory(plan)
            outcome = accessibility_evidence.run_attempt(
                plan, budget, services,
                lambda: time.monotonic() - started)
            results.append((task_id, plan, outcome, services))
        return results

    def commit_accessibility(self, results: list) -> None:
        """Phase E. One transaction, re-verifying every claim it touches."""
        if not results:
            return
        with self.store.transaction() as doc:
            for task_id, plan, outcome, services in results:
                task = doc["tasks"].get(task_id)
                record = doc["prs"].get(str(plan.pr))
                if task is None or not isinstance(record, dict):
                    continue
                claim = record.get("accessibility_auto")
                if isinstance(claim, dict) and \
                        claim.get("attempt_id") == plan.attempt_id:
                    # G2: the port returns to the pool only once the
                    # listener has been observed gone.
                    outcome = accessibility_evidence.confirm_release(
                        outcome, claim, services)
                self.ingest_accessibility_auto(doc, task, plan.pr, plan.sha,
                                               plan, outcome)

    # ============================================================
    # C-05.3b: the QUALITATIVE accessibility half.
    #
    # An agent, unlike the automated half - so it DOES consume a provider
    # (config roles: accessibility -> codex) and is gated on provider
    # state, and it gets a lease from G1's timeouts.accessibility.
    # ============================================================

    def _accessibility_review_slots_free(self, doc: dict, pr_number: int) -> bool:
        used = 0
        for number, record in (doc.get("prs") or {}).items():
            if str(number) == str(pr_number):
                continue
            claim = record.get("accessibility_review") \
                if isinstance(record, dict) else None
            if isinstance(claim, dict) and claim.get("claim_state") != "COMPLETE":
                used += 1
        return used < self.cfg.max_accessibility_review

    def plan_accessibility_review(self, doc: dict, task: dict, pr_number: int,
                                  head: str):
        """T1. Claim one qualitative review attempt, or return None."""
        record = doc["prs"].get(str(pr_number))
        if not isinstance(record, dict):
            return None
        claim = record.get("accessibility_review")
        if isinstance(claim, dict):
            if claim.get("sha") == head and claim.get("claim_state") != "COMPLETE":
                return None  # in flight for this head
            if claim.get("sha") == head and claim.get("verdict") is not None:
                return None  # already answered for this head
        if not self._accessibility_review_slots_free(doc, pr_number):
            return None
        # This half IS an agent, so the provider controls apply to it in
        # full - unlike the automated half, which consults none.
        blocked = self._security_dispatch_block(doc)
        if blocked:
            self.log("ACCESSIBILITY_REVIEW_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=blocked, metadata_redacted={"head": head})
            return None

        ordinal = 1
        if isinstance(claim, dict) and claim.get("sha") == head:
            previous = claim.get("ordinal")
            ordinal = (previous + 1) if isinstance(previous, int) else 1
        if ordinal > routing.ACCESSIBILITY_ORDINAL_MAX:
            self.log("ACCESSIBILITY_REVIEW_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome="ORDINAL_EXHAUSTED",
                     metadata_redacted={"head": head})
            return None

        fresh = {
            "sha": head,
            "ordinal": ordinal,
            "attempt_id": routing.ACCESSIBILITY_REVIEW_ATTEMPT_FMT.format(ordinal),
            "worker": routing.accessibility_review_worker_name(
                task["id"], head, ordinal),
            "claim_state": "PLANNED",
            "claimed_at": clock.iso(self.now()),
            # G1: timeouts.accessibility = 1800, the agent lease, kept
            # separate from the automated browser deadlines on purpose.
            "lease_expires_at": self._lease_expires("accessibility"),
            "verdict": None,
            "reason": "",
        }
        ok, diagnostic = routing.accessibility_review_claim_is_valid(fresh)
        if not ok:
            self.log("ACCESSIBILITY_REVIEW_HELD", task_id=task["id"],
                     pr_id=pr_number, activity_class="ORCHESTRATION",
                     outcome=diagnostic, metadata_redacted={"head": head})
            return None
        record["accessibility_review"] = fresh
        self.log("ACCESSIBILITY_REVIEW_CLAIMED", task_id=task["id"],
                 pr_id=pr_number, role="accessibility",
                 activity_class="ORCHESTRATION", outcome="PLANNED",
                 metadata_redacted={"head": head,
                                    "attempt_id": fresh["attempt_id"],
                                    "worker": fresh["worker"]})
        return accessibility_evidence.AccessibilityPlan(
            task_id=task["id"], pr=pr_number, sha=head,
            attempt_id=fresh["attempt_id"], port=0)

    def ingest_accessibility_review(self, doc: dict, task: dict, pr_number: int,
                                    head: str, plan, text: str) -> None:
        """Commit one qualitative review, re-verifying what it reviewed."""
        record = doc["prs"].get(str(pr_number))
        if not isinstance(record, dict):
            return
        claim = record.get("accessibility_review")
        if not isinstance(claim, dict):
            return
        if claim.get("sha") != head or \
                claim.get("attempt_id") != plan.attempt_id:
            self.log("ACCESSIBILITY_REVIEW_DISCARDED", task_id=task["id"],
                     pr_id=pr_number, activity_class="REVIEW",
                     outcome="CLAIM_MOVED",
                     metadata_redacted={"head": head,
                                        "attempt_id": plan.attempt_id})
            return
        if claim.get("claim_state") == "COMPLETE":
            return

        review = routing.parse_accessibility(text)
        consistent, why = routing.accessibility_is_consistent(review)
        claim["claim_state"] = "COMPLETE"
        if not consistent:
            # An incoherent review is NOT a FAIL. It is the absence of a
            # usable judgement, so it never routes to FIX_REQUIRED and
            # never records findings the reviewer did not establish.
            claim["verdict"] = None
            claim["reason"] = why
            self.log("ACCESSIBILITY_REVIEW_RESULT", task_id=task["id"],
                     pr_id=pr_number, role="accessibility",
                     activity_class="REVIEW", outcome=why,
                     metadata_redacted={"head": head,
                                        "attempt_id": plan.attempt_id})
            return

        claim["verdict"] = review.verdict
        claim["reason"] = ""
        blocking = [f for f in review.findings
                    if routing.adjudicate_accessibility_finding(f)["merge_blocked"]]
        self.log("ACCESSIBILITY_REVIEW_RESULT", task_id=task["id"],
                 pr_id=pr_number, role="accessibility",
                 activity_class="REVIEW", outcome=review.verdict,
                 metadata_redacted={"head": head,
                                    "attempt_id": plan.attempt_id,
                                    "findings": len(review.findings),
                                    "blocking": len(blocking)})
        if review.verdict == accessibility_contract.ACCESSIBILITY_FAIL:
            record["pending_findings"] = blocking
            self.transition(doc, task["id"], "FIX_REQUIRED",
                            f"accessibility review findings on {head[:12]}")
            return
        self.advance_if_evidence_complete(doc, task, pr_number, head)

    def advance_if_evidence_complete(self, doc: dict, task: dict,
                                     pr_number: int, head: str) -> bool:
        """WAITING_EVIDENCE -> REVIEW, when every class passes at `head`.

        The exit C-20(a) recorded as missing. Returns whether it fired.

        `review_gate_fires` is pure over (record, head) and compares every
        leg to the head observed THIS tick, never to another leg - so a
        unanimously stale evidence set, three claims agreeing perfectly
        with each other about a superseded commit, does not advance.

        FALSE IS NOT A FAILURE. It covers absent, stale, PLANNED, failed
        and malformed alike, and none of those is a reason to route the
        task anywhere; it simply keeps waiting, bounded by G4's cumulative
        allowance rather than by nothing.
        """
        if task["state"] != state_mod.EVIDENCE_WAIT_STATE:
            return False
        record = doc["prs"].get(str(pr_number))
        if not isinstance(record, dict):
            return False
        if not routing.review_gate_fires(record, head):
            return False
        self.transition(doc, task["id"], "REVIEW",
                        f"every required evidence class passed on {head[:12]}")
        self.log("EVIDENCE_COMPLETE", task_id=task["id"], pr_id=pr_number,
                 activity_class="ORCHESTRATION", outcome="REVIEW",
                 metadata_redacted={
                     "head": head,
                     "classes": [key for key, _ in routing.REVIEW_GATE_LEGS]})
        return True

    # ---------------------------------------------------------- worker reaping

    def _lease_expires(self, role: str) -> str:
        """C-09 lease: the worker's own enforced hard deadline plus the
        governed grace (config timeouts.lease_grace_seconds) covering
        dispatch->entry clock skew, the entry's kill-loop granularity and
        kill->exit lag. A process alive past this instant is a violation."""
        seconds = (self.cfg.extra["timeouts"][role]
                   + int(self.cfg.extra["timeouts"]["lease_grace_seconds"]))
        return clock.iso(self.now() + timedelta(seconds=seconds))

    def _retain_worktree(self, doc: dict, meta: dict, why: str,
                         *, all_roles: bool = False) -> None:
        """C-09 ACTIVE -> RETAINED transfer: the physical worktree outlives
        the worker record (workmux close keeps it), so ownership moves to
        the task's retained_worktrees map in the same transaction that
        drops the record. Reviewer worktrees are normally physically
        removed instead (release_review_worktree), so they transfer only
        in the freeze sweep, which closes rather than removes."""
        if not meta.get("worktree"):
            return
        if not all_roles and meta.get("role") not in ("builder", "fixer"):
            return
        task = doc["tasks"].get(meta.get("task_id"))
        if task is None:
            return
        task.setdefault("retained_worktrees", {})[meta["worktree"]] = {
            "retained_at": clock.iso(self.now()), "why": why,
        }

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

            if task and task["state"] == "HUMAN_REQUIRED":
                # C-09: this task is parked pending a human decision (the
                # only way a live record coexists with HUMAN_REQUIRED is
                # lease expiry). The role handler's transitions would
                # silently unpark it, so the record is reaped without
                # routing - the human decision alone moves the task. All
                # roles retain here: the skipped handler is also what would
                # have removed a reviewer's worktree, so nothing removes
                # any worktree on this path (same rationale as the freeze
                # sweep).
                self._retain_worktree(doc, meta, "TERMINAL_REAP",
                                      all_roles=True)
                doc["workers"].pop(worker, None)
                continue
            self._retain_worktree(doc, meta, "TERMINAL_REAP")
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
                # S3: the task stays FAILED - the intervention records the
                # human obligation without rewriting the escalation state.
                self.transition(doc, task["id"], "FAILED", "builder attempts exhausted")
                self.request_intervention(
                    doc, type_="HUMAN_PRODUCT_DECISION",
                    condition_code="builder_attempts_exhausted",
                    task_id=task["id"],
                    reason=f"Builder attempts exhausted for {task['id']} "
                           f"after {task['attempts']} attempts",
                    title=f"{task['id']} failed after {task['attempts']} attempts",
                    body="Evidence-based recovery attempts are exhausted.")
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
            self.request_intervention(
                doc, type_="HUMAN_PRODUCT_DECISION", condition_code="fixer_failed",
                task_id=task["id"], pr_id=pr_number,
                reason=f"Fixer failed for {task['id']} PR #{pr_number}",
                title=f"{task['id']} PR #{pr_number}: fixer failed",
                body="The fixer could not complete the listed findings.")
            return
        record = doc["prs"][str(pr_number)]
        record["approval_current"] = False
        record["review_verdict"] = None
        self.transition(doc, task["id"], "REVIEW", "fix complete; returning to reviewer")
        # C-18 stage 5: this runs inside T1, from reap_workers' callback, and
        # the head a review must be cut from is observed BEFORE the lock. No
        # observation exists for this pull request - at observation time the
        # fixer was still live - so the dispatch defers by design. The task is
        # left in REVIEW with its verdict cleared and no worker, which is
        # exactly what route_awaiting_dispatch picks up on the next tick,
        # against a head observed after the fix landed rather than before it.
        self.dispatch_reviewer(doc, task, pr_number, None)

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
        # C-10.2: the accepted P2/P3 set has to be constructible BEFORE the PR
        # becomes merge-eligible. A duplicate or malformed finding identity is
        # a pre-merge review failure, not a post-merge evidence loss: once
        # gh.merge() has run there is no way to refuse the debt without
        # stranding a task that has already merged.
        retained = (debt.retain_accepted(review)
                    if review.verdict == routing.REVIEW_PASS and consistent
                    else None)

        record["last_review_at"] = clock.iso(self.now())
        # C-18 stage 5: the verdict is bound to the exact commit it judged.
        # `meta["head"]` is written on THIS worker's record at dispatch, so it
        # cannot be overwritten by a later cycle the way `reviewed_head` can;
        # the per-PR field is the fallback for a worker record minted before
        # this binding existed. When neither is known the key is still emitted,
        # as null - an unbound review must be visibly unbound to anything
        # composing a merge gate, never silently absent.
        #
        # `head_sha` is a bare kwarg for the same reason as REVIEW_DISPATCHED:
        # `Ledger.append` merges it into `metadata_redacted`, one spelling
        # only. `agent_id` names the worker the SHA-bound result belongs to.
        self.log("REVIEW_RESULT", task_id=task["id"], pr_id=pr_number, role="reviewer",
                 provider=self.cfg.roles["reviewer"].provider, agent_id=worker,
                 activity_class="REVIEW", outcome=review.verdict,
                 head_sha=meta.get("head") or record.get("reviewed_head"),
                 metadata_redacted=review.as_dict())

        if (review.verdict == routing.REVIEW_UNPARSEABLE or not consistent
                or (retained is not None and not retained.ok)):
            record["approval_current"] = False
            record["review_verdict"] = routing.REVIEW_FAIL
            reason = (why or (retained.reason if retained else "")
                      or "unparseable verdict")
            self.log("REVIEW_REJECTED_BY_SUPERVISOR", task_id=task["id"], pr_id=pr_number,
                     outcome="REJECTED", activity_class="REVIEW",
                     metadata_redacted={"reason": reason})
            if record["review_cycles"] >= self.cfg.max_repair_cycles:
                self.transition(doc, task["id"], "HUMAN_REQUIRED",
                                "reviewer output unusable")
                # The parse-failure text stays in the notification body only.
                self.request_intervention(
                    doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                    condition_code="review_unusable",
                    task_id=task["id"], pr_id=pr_number,
                    reason=f"Review unusable for {task['id']} PR #{pr_number} "
                           f"after {record['review_cycles']} review cycles",
                    title=f"{task['id']} PR #{pr_number}: review unusable",
                    body=reason or "The reviewer did not return a valid verdict.")
            else:
                self.transition(doc, task["id"], "PR_OPEN", "review unusable; re-review")
            return

        if review.critical:
            record["review_verdict"] = routing.REVIEW_FAIL
            record["approval_current"] = False
            self.transition(doc, task["id"], "HUMAN_REQUIRED", "P0 finding")
            # Reviewer prose (finding summaries) stays in the notification body.
            self.request_intervention(
                doc, type_="HUMAN_PRODUCT_DECISION", condition_code="p0_finding",
                task_id=task["id"], pr_id=pr_number,
                reason=f"P0 finding requires a human product decision for "
                       f"{task['id']} PR #{pr_number}",
                title=f"{task['id']} PR #{pr_number}: P0 finding",
                body="; ".join(f.get("summary", "")[:120] for f in review.critical))
            return

        if review.verdict == routing.REVIEW_FAIL or review.blocking:
            record["review_verdict"] = routing.REVIEW_FAIL
            record["approval_current"] = False
            # Persist the findings so a retry dispatches the same repair work
            # instead of paying for another review to rediscover it.
            record["pending_findings"] = review.blocking or review.findings
            self.notify_out(doc, notify.INFO,
                            f"{task['id']} PR #{pr_number}: review found issues",
                            f"{len(record['pending_findings'])} finding(s) reported; "
                            "dispatching a fixer.")
            self.dispatch_fixer(doc, task, pr_number, record["pending_findings"])
            return

        record["review_verdict"] = routing.REVIEW_PASS
        record["approval_current"] = True
        # Validated and scrubbed at the approving review, because the reviewer's
        # output does not survive to merge time and must not be re-trusted then.
        record["accepted_findings"] = list(retained.entries)
        self.log("REVIEW_PASS_RECORDED", task_id=task["id"], pr_id=pr_number,
                 outcome="APPROVED", activity_class="REVIEW")
        self.notify_out(doc, notify.INFO, f"{task['id']} PR #{pr_number}: review passed",
                        "Awaiting merge eligibility.")

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
        self.notify_out(doc, notify.INFO, f"{task['id']}: PR #{pr_number} opened",
                        f"Branch {task['branch']}.")

    def observe_closed_prs(self, snapshot: dict, open_prs: list[dict]) -> dict:
        """`gh pr view` for every PR-bearing task whose PR is not open.

        C-18 stage 1. route_prs made this call itself, inside the state
        transaction; it is made here instead, with no lock held, and the
        answer is passed in.

        Only the pull requests route_prs would have asked about are observed -
        a task's own PR, with a record, not already merged, and absent from
        the open list - so this MOVES a GitHub call rather than adding one.

        The snapshot is read outside the lock and may be stale by the time T1
        runs. That cannot cause a wrong action: an observation route_prs does
        not find is deferred to the next tick, never assumed.
        """
        open_numbers = {pr.get("number") for pr in open_prs}
        records = snapshot.get("prs") or {}
        wanted: set[int] = set()
        for task in (snapshot.get("tasks") or {}).values():
            number = task.get("pr")
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            if number in open_numbers:
                continue
            record = records.get(str(number))
            if record is None or record.get("merged"):
                continue
            wanted.add(number)

        observations: dict[int, routing.ClosedPrObservation] = {}
        for number in sorted(wanted):
            view = gh.pr_view(self.cfg.github_repo, number)
            try:
                observations[number] = routing.ClosedPrObservation(
                    pr_number=number, observed_at=clock.iso(self.now()), view=view)
            except ValueError as exc:
                # An observation that cannot be bound to this pull request
                # proves nothing about it. Dropping it leaves route_prs with
                # no entry for the PR, which defers it - strictly safer than
                # routing on an answer that may describe a different PR.
                self.log("PR_OBSERVATION_REJECTED", pr_id=number,
                         outcome="OBSERVATION_INCONSISTENT",
                         activity_class="OBSERVATION",
                         metadata_redacted={"reason": str(exc)})
        return observations

    def route_prs(self, doc: dict, cs: clock.ClockState, prs: list[dict],
                  pr_observations: dict,
                  review_observations: dict | None = None) -> list[tuple[str, int]]:
        """Route every PR-bearing task, and return the merge-ready ones.

        C-14.1: routing decides, it does not merge. Candidates are returned as
        (task_id, pr_number) identifiers - never the task, record or PR dicts,
        which belong to this transaction's document and are stale the moment it
        commits. execute_merges() re-reads them from fresh state.

        C-18 stage 1: this runs inside T1 and therefore makes no GitHub call.
        pr_observations is observe_closed_prs()'s result for this tick, keyed
        by pull request number; it is required rather than defaulted so a
        caller that forgets it fails loudly instead of silently losing
        external-merge detection. A pull request with no entry is deferred,
        and there is no fallback fetch to fall back to.

        C-18 stage 5: `review_observations` is `observe_review_heads`' result
        for this tick, keyed the same way, and is passed straight through to
        `dispatch_reviewer`. It is plumbing only - nothing here reads it.
        """
        candidates: list[tuple[str, int]] = []
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
                observation = pr_observations.get(pr_number)
                if observation is None or observation.pr_number != pr_number:
                    # C-18 stage 1: no observation was taken for this pull
                    # request - the pre-lock snapshot did not name it, or the
                    # answer failed its binding check. Defer to the next tick.
                    # There is deliberately no fallback fetch here; a GitHub
                    # call under the lock is the defect being removed.
                    self.log("PR_ROUTING_DEFERRED", task_id=task["id"],
                             pr_id=pr_number, outcome="OBSERVATION_MISSING",
                             activity_class="ORCHESTRATION",
                             metadata_redacted={
                                 "reason": "no pre-transaction GitHub observation"})
                    continue
                merged_view = observation.view
                # C-14.2: a pull request merged out there while state says it
                # is not may be an ordinary human merge, or this control plane's
                # own merge whose commit was lost. Completing both the same way
                # converts a durable contradiction into a clean-looking task.
                if self.invariant_violated(doc, task, record, pr_number, merged_view):
                    continue
                merge_sha_observed = observation.observed
                merged_data = merged_view or {}
                if (merged_data.get("state") or "").upper() == "MERGED":
                    merged_sha = (merged_data.get("mergeCommit") or {}).get("oid")
                    record["merged"] = True
                    record["merged_sha"] = merged_sha
                    record["merge_sha_observed"] = merge_sha_observed
                    self.log(
                        "MERGED",
                        task_id=task["id"],
                        pr_id=pr_number,
                        branch=task["branch"],
                        outcome="MERGED",
                        activity_class="ORCHESTRATION",
                        metadata_redacted={
                            "reviewed_head": record.get("reviewed_head"),
                            "merged_sha": merged_sha,
                            "merge_sha_observed": merge_sha_observed,
                            "detected_externally": True,
                        },
                    )
                    self.complete_task(doc, task, pr_number)
                continue

            if task["state"] == "REVIEW" and record.get("review_verdict") == \
                    routing.REVIEW_PASS and record.get("approval_current"):
                candidates.append((task["id"], pr_number))
                continue

            self.route_awaiting_dispatch(doc, task, pr_number, record,
                                         review_observations)

        return candidates

    ROUTING_STATES = ("PR_OPEN", "REVIEW", "FIX_REQUIRED")

    def route_awaiting_dispatch(self, doc: dict, task: dict, pr_number: int,
                                record: dict,
                                review_observations: dict | None = None) -> None:
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
        self.dispatch_reviewer(doc, task, pr_number,
                               (review_observations or {}).get(pr_number))

    def has_worker(self, doc: dict, pr_number: int, roles: tuple[str, ...]) -> bool:
        """Whether this PR already has a worker, or a claim on one, in these roles.

        C-18: a dispatch claim counts. Between T1 planning a dispatch and the
        spawn being committed there is no worker record, and without this
        `route_awaiting_dispatch` would see "no live worker" on the next tick
        and dispatch a SECOND one - two reviewers on one pull request, or two
        fixers on one branch. The claim is the durable reservation that
        closes that window.
        """
        if any(meta.get("pr") == pr_number and meta.get("role") in roles
               for meta in doc["workers"].values()):
            return True
        return self.has_dispatch_claim(doc, pr_number, roles)

    @staticmethod
    def has_dispatch_claim(doc: dict, pr_number: int,
                           roles: tuple[str, ...]) -> bool:
        """Whether an ACTIVE dispatch claim in these roles holds this PR."""
        for task in (doc.get("tasks") or {}).values():
            claims = task.get(state_mod.DISPATCH_CLAIMS_KEY) or {}
            if not isinstance(claims, dict):
                continue
            for role, claim in claims.items():
                if role not in roles:
                    continue
                if not state_mod.dispatch_claim_active(claim):
                    continue
                if claim.get("pr") == pr_number:
                    return True
        return False

    def invariant_violated(self, doc: dict, task: dict, record: dict,
                           pr_number: int, pr_view: dict | None) -> bool:
        """C-14.2. True when this pull request must not be treated as an
        ordinary external merge. Detects and annunciates; never repairs."""
        github, state_view, ledger_view = merge_invariant.gather(
            task_id=task["id"], pr_number=pr_number, doc=doc, task=task,
            record=record, ledger=self.ledger, pr_view=pr_view,
            now_iso=clock.iso(self.now()),
        )
        verdict = merge_invariant.classify(
            task_id=task["id"], pr_number=pr_number, github=github,
            state_view=state_view, ledger_view=ledger_view,
        )
        if verdict is None or not verdict.dangerous:
            return False
        merge_invariant.annunciate(
            doc=doc, task=task, record=record, verdict=verdict, github=github,
            state_view=state_view, ledger_view=ledger_view, ledger=self.ledger,
            announce=self._queued_annunciation(doc),
            detector="supervisor", tz=self.tz,
        )
        return True

    def _queued_annunciation(self, doc: dict):
        """The Supervisor's annunciation sink: queue, never send.

        This is the notification amendment. `invariant_violated` is reached
        from `route_prs`, which runs inside T1, and this was the last
        synchronous outbound send under that lock - a hanging Discord call
        stalled the whole state transaction and with it every task in the
        run. The intent is now committed with the freeze and the
        intervention record that accompany it, and `drain_notifications`
        delivers it afterwards with bounded retries and no lock held.

        `delivered` is None, not False: no send was attempted here, so
        neither success nor failure is known yet. notify_out owns the
        human_interventions counter.
        """
        def announce(severity: str, title: str, body: str) -> dict:
            result = self.notify_out(doc, severity, title, body)
            return {"queued": bool(result.get("queued")),
                    "intent_id": result.get("intent_id"),
                    "delivered": None,
                    "status": "QUEUED" if result.get("queued")
                    else result.get("reason")}
        return announce

    @staticmethod
    def merge_no_longer_ready(task: dict | None, record: dict | None,
                              pr_number: int) -> str | None:
        """Why this candidate must not merge now, or None if it still may.

        route_prs's gate, re-applied to freshly read state. The lock is dropped
        between the two transactions and the Watchdog can freeze a task in that
        gap, so none of these may be assumed to still hold.
        """
        if task is None:
            return "task no longer exists"
        if task.get("pr") != pr_number:
            return "task is no longer attached to this pull request"
        if record is None:
            return "pull request record no longer exists"
        if record.get("merged"):
            return "pull request is already recorded as merged"
        if task["state"] != "REVIEW":
            return f"task is {task['state']}, not REVIEW"
        if record.get("review_verdict") != routing.REVIEW_PASS:
            return "no current REVIEW_PASS"
        if not record.get("approval_current"):
            return "approval is not current"
        return None

    def execute_merges(self, candidates: list[tuple[str, int]]) -> None:
        """C-14.1. One dedicated transaction per merge, entered only after the
        ordinary tick transaction has committed and left immediately once the
        merge is recorded.

        gh.merge() is irreversible and its MERGED event is durable the instant
        it is written. Nothing unrelated may follow it inside the transaction
        that records it, or an exception in that unrelated work rolls the merge,
        its debt and its completion out of state while GitHub and the ledger
        keep them. Candidates are therefore merged here, one transaction each,
        with nothing after attempt_merge() inside the block.

        The pull request is observed again under the lock this merge runs under:
        route_prs decided against a list taken before the lock was ever held.
        """
        for task_id, pr_number in candidates:
            with self.store.transaction() as doc:
                task = doc["tasks"].get(task_id)
                record = doc["prs"].get(str(pr_number))
                reason = self.merge_no_longer_ready(task, record, pr_number)
                pr = None
                if reason is None:
                    pr = gh.pr_view(self.cfg.github_repo, pr_number)
                    if pr is None:
                        reason = "pull request could not be observed"
                if reason is not None:
                    self.log("MERGE_BLOCKED", task_id=task_id, pr_id=pr_number,
                             outcome="BLOCKED", activity_class="ORCHESTRATION",
                             metadata_redacted={"reason": reason})
                    continue
                self.attempt_merge(doc, task, pr, record)

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
            # The finite condition code rides alongside the prose so a reader
            # can tell a draft pull request from a closed one without parsing
            # English. A draft is a reportable condition, not a silent stall.
            self.log("MERGE_BLOCKED", task_id=task["id"], pr_id=number, outcome="BLOCKED",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"reason": decision.reason,
                                        "condition": decision.condition})
            return

        result = gh.merge(repo, number)
        if not result.ok:
            self.log("MERGE_FAILED", task_id=task["id"], pr_id=number, outcome="FAILED",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"stderr": result.stderr[:400]})
            return

        record["merged"] = True

        merged_view = gh.pr_view(repo, number)
        merge_sha_observed = merged_view is not None
        merged_data = merged_view or {}
        merged_sha = (merged_data.get("mergeCommit") or {}).get("oid")

        record["merged_sha"] = merged_sha
        record["merge_sha_observed"] = merge_sha_observed

        # C-10.2. The merge above is irreversible, so this never raises and
        # never writes a partial set: either every accepted P2/P3 becomes debt
        # or none does, and the task completes either way.
        retained_entries = record.get("accepted_findings")
        debt_result = debt.accept(
            doc, task_id=task["id"], pr_number=number,
            accepted_findings=retained_entries,
            merged_sha=merged_sha, at=clock.iso(self.now()),
        )
        record["debt_recording_status"] = debt_result.status
        if debt_result.error:
            record["debt_recording_error"] = debt_result.error
        else:
            record.pop("debt_recording_error", None)
        if debt_result.status == debt.FAILED:
            self.log("DEBT_RECORDING_FAILED", task_id=task["id"], pr_id=number,
                     outcome="FAILED", activity_class="ESCALATION",
                     human_intervention=False,
                     metadata_redacted={
                         "code": debt_result.error,
                         "retained_count": (len(retained_entries)
                                            if isinstance(retained_entries, list) else 0),
                         "merged_sha": merged_sha,
                     })

        self.log(
            "MERGED",
            task_id=task["id"],
            pr_id=number,
            branch=task["branch"],
            outcome="MERGED",
            activity_class="ORCHESTRATION",
            metadata_redacted={
                "reviewed_head": record.get("reviewed_head"),
                "merged_sha": merged_sha,
                "merge_sha_observed": merge_sha_observed,
                "debt_recording_status": debt_result.status,
                "debt_ids": list(debt_result.debt_ids),
                # C-14.2 merge-origin contract: always present on both paths,
                # so absence of the key is never how local and external merges
                # are told apart.
                "detected_externally": False,
            },
        )

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

        reviewed_sha = (record or {}).get("reviewed_head")
        merged_sha = (record or {}).get("merged_sha")
        reviewed_sha_text = reviewed_sha or "unavailable"
        merged_sha_text = merged_sha or "not observed"
        self.notify_out(
            doc, notify.INFO, f"{task['id']} complete",
            f"{task['title']} merged as PR #{pr_number}. "
            f"Reviewed SHA {reviewed_sha_text}, merged SHA {merged_sha_text}. "
            "No human action required.",
        )

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

    LEASE_GOVERNED_STATES = frozenset({"ASSIGNED", "ACTIVE", "REVIEW",
                                       "FIX_REQUIRED"})

    def detect_lease_expiry(self, doc: dict) -> None:
        """C-09: a worker process alive past its own enforced deadline plus
        the governed grace means the enforcement mechanism itself failed.
        Escalates to HUMAN_REQUIRED with a durable C-08b intervention -
        never a kill, and ownership (record, worktree, port, any migration
        lock) is retained for the human decision. A process already gone is
        no obligation: the ordinary reap handles its terminal status.

        Alive means: a worker-entry process carrying this worker's job path
        in its cmdline, or the recorded agent identity-verified by
        (agent_pid, agent_start_ticks). An ambiguous identity is NOT
        treated as alive here - detection never over-claims; the governed
        resolution preconditions fail closed separately (control/cli.py).
        """
        entries: dict | None = None
        now = self.now()
        for worker, meta in list(doc["workers"].items()):
            task = doc["tasks"].get(meta.get("task_id"))
            if not task or task["state"] not in self.LEASE_GOVERNED_STATES:
                continue
            lease = meta.get("lease_expires_at")
            if not lease:
                continue
            try:
                if now <= clock.parse(lease):
                    continue
            except ValueError:
                continue  # malformed lease is our own bug; never guess
            if entries is None:
                entries = proc.worker_entry_processes(config.WORKER_LOG_DIR) or {}
            status = workers.read_status(worker) or {}
            alive = worker in entries or proc.verified_alive(
                status.get("agent_pid"), status.get("agent_start_ticks")) is True
            if not alive:
                continue
            self.transition(doc, task["id"], "HUMAN_REQUIRED",
                            f"lease expired at {lease} with the worker "
                            f"process still alive")
            self.request_intervention(
                doc, type_="HUMAN_APPARATUS_AUTHORISATION",
                condition_code="lease_expired", task_id=task["id"],
                reason=f"Lease expired for {task['id']} while worker "
                       f"{worker} is still alive",
                title=f"{task['id']}: worker lease expired while alive",
                body=f"Worker {worker}'s lease expired at {lease} but its "
                     "process is still running - its own hard-timeout "
                     "enforcement did not terminate it. No automatic kill "
                     "is performed; resolve via ctl human-resolve.")

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

        # C-19: the allowance is committed to durable state BEFORE the request
        # leaves, so a crash mid-flight leaves the exposure on disk instead of
        # erasing it. `reservation is None` is the budget refusing the call.
        allowance = config.jev_reservation_usd(self.cfg)
        purpose = jev_consultation_identity(task)
        with self.store.transaction() as doc:
            reservation = budget.reserve(doc, "openrouter", allowance,
                                         purpose=purpose,
                                         at=clock.iso(self.now()))
            # Why nothing was asked, where a human will look for it: a
            # suppressed duplicate is not the budget running out.
            suppressed = reservation is None and budget.duplicate_blocked(doc, purpose)

        decision = self.jev.decide(
            "worker_health",
            {
                "task": task["id"],
                "state": task["state"],
                "attempts": task["attempts"],
                "minutes_since_progress": self._minutes_since(task.get("last_progress_at")),
                "review_queue_depth": state_mod.review_queue_depth(snapshot),
            },
            allowed=reservation is not None,
        )

        self.log("JEV_DECISION", task_id=task["id"], provider="openrouter",
                 model=decision.requested_model, activity_class="ORCHESTRATION",
                 outcome=decision.choice, actual_cost_usd=decision.actual_cost_usd,
                 cost_source="provider" if decision.actual_cost_usd is not None else "none",
                 input_tokens=decision.input_tokens, output_tokens=decision.output_tokens,
                 duration_ms=decision.duration_ms,
                 metadata_redacted={"source": decision.source, "kind": decision.kind,
                                    "confidence": decision.confidence,
                                    "reason": decision.reason, "error": decision.error,
                                    "requested_model": decision.requested_model,
                                    "returned_model": decision.returned_model or "unknown",
                                    "billing_state": decision.billing_state,
                                    "reservation_usd": allowance if reservation else None,
                                    "duplicate_suppressed": suppressed})

        with self.store.transaction() as doc:
            crossed = budget.settle(doc, reservation, provider="openrouter",
                                    billing_state=decision.billing_state,
                                    actual_cost_usd=decision.actual_cost_usd)
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
        title = f"Budget threshold {name} ({summary['percent']}%)"
        spend = (f"Metered OpenRouter spend ${summary['spent_usd']:.4f} of "
                 f"${summary['total_usd']:.2f}. ")
        if name == "HARD_STOP":
            # S8: budget stays fail-closed. The intervention records the
            # governance obligation; NO_ACTION is its only governed resolution
            # and nothing here (or in human-resolve) clears budget.hard_stop.
            self.request_intervention(
                doc, type_="HUMAN_GOVERNANCE_DECISION",
                condition_code="budget_hard_stop",
                reason="Metered budget hard stop reached; further paid model "
                       "calls require a human governance decision",
                title=title,
                body=spend + "Further paid model calls need explicit human "
                             "approval; Jev falls back to deterministic "
                             "behaviour and the experiment continues.")
            return
        self.notify_out(
            doc, notify.ATTENTION, title,
            spend + "Escalation policy tightened; the experiment continues.",
            no_human_action_needed=True,
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
            # close keeps the worktree, so ownership transfers to the task
            # (C-09) - every role, since nothing removes worktrees here.
            self._retain_worktree(doc, doc["workers"][worker], "RUN_FROZEN",
                                  all_roles=True)
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

        C-14.1 splits the cycle in two. Ordinary work runs in one transaction
        which decides which pull requests are merge-ready but performs no merge.
        Each merge then runs in its own transaction, after that one has
        committed and before the slow provider work, so that no unrelated later
        failure can roll back a merge GitHub and the ledger already record.
        """
        open_prs = gh.list_open_prs(self.cfg.github_repo)
        merge_candidates: list[tuple[str, int]] = []
        planned_security: list = []
        # C-18: one list per tick. Reset here rather than only in __init__ so
        # a plan can never leak from one cycle into the next.
        self._dispatch_plans = []
        self._accessibility_plans = []

        # C-05.3a Phase A + Phase E-publish, and C-18 stage 1's pull-request
        # observation, all OUTSIDE the lock. The snapshot is
        # non-authoritative; T1 re-reads under the lock and re-checks task,
        # PR, SHA and attempt identity before acting.
        security_observations: dict = {}
        pr_observations: dict = {}
        dispatch_observations: dict = {}
        review_observations: dict = {}
        if self.store.exists():
            snapshot = self.store.read()
            # C-18 stage 1: the `gh pr view` route_prs used to make while
            # holding the lock. Taken here, passed in below.
            pr_observations = self.observe_closed_prs(snapshot, open_prs)
            # C-18 stage 5: the head and material diff hash dispatch_reviewer
            # used to fetch inside T1. Only for pull requests a reviewer could
            # actually be dispatched against this tick.
            review_observations = self.observe_review_heads(snapshot, open_prs)
            # Heads are resolved ONLY for pull requests whose task actually
            # needs evidence. gh.pr_diff_sha calls gh.pr_view, so asking for
            # every open PR every tick would add a GitHub round trip per PR
            # for work nothing is waiting on.
            wanted = {task.get("pr") for task in snapshot.get("tasks", {}).values()
                      if task.get("state") in self.EVIDENCE_STATES}
            eligible = [pr["number"] for pr in open_prs
                        if pr["number"] in wanted]
            # C-18 stage 4: one /proc scan serves both the security phases
            # and dispatch recovery. It is taken when either needs it and
            # skipped entirely when neither does, so a quiet tick adds no
            # scan it did not already perform.
            claims_present = any(
                state_mod.dispatch_claim_active(claim)
                for task in (snapshot.get("tasks") or {}).values()
                for claim in self._snapshot_claims(task))
            entries = None
            if eligible or claims_present:
                entries = proc.worker_entry_processes(config.WORKER_LOG_DIR)
            if claims_present:
                # C-18 stage 4 Phase A: the job file that proves whether a
                # claim ever spawned, read here so T1 touches no filesystem.
                dispatch_observations = self.observe_dispatch_claims(
                    snapshot, entries)
            if eligible:
                heads = {number: gh.pr_diff_sha(self.cfg.github_repo, number)
                         for number in eligible}
                self.publish_security_results(snapshot, entries)
                # Phase F, after publication and before T1: a worktree is
                # released only once its answer is durable, and releasing
                # before T1 keeps a removal and an ingest from contending
                # for the same attempt within one tick.
                self.release_security_worktrees(snapshot, entries)
                security_observations = self.observe_security(snapshot, heads,
                                                              entries)

        with self.store.transaction() as doc:
            providers.ensure(doc)
            budget.ensure(doc, self.cfg.budget_usd)
            migration_lock.ensure(doc)
            self.reconcile_auto_release_evidence(doc)

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

            # C-18 stage 4: converge dispatch claims BEFORE reaping, so a
            # worker recovered from its job file gets its record back in time
            # to be reaped in this same tick rather than the next one.
            self.resume_dispatch_claims(doc, dispatch_observations)
            self.reap_workers(doc)
            # C-05.3a T1: claim, and ingest evidence already durable on disk.
            # Deliberately not folded into reap_workers - that callback path
            # runs inside this transaction, and reading worker output or
            # publishing files from it would put external work back under the
            # lock. Only finished records cross this boundary.
            planned_security = self.route_evidence(doc, security_observations)
            merge_candidates = self.route_prs(doc, cs, open_prs, pr_observations,
                                              review_observations)
            self.detect_stale(doc)
            self.detect_lease_expiry(doc)

            for task in self.dispatchable(doc, cs):
                self.dispatch_builder(doc, task)

            depth = state_mod.review_queue_depth(doc)
            if state_mod.builder_limit(depth, self.cfg.max_builders) < self.cfg.max_builders:
                doc["counters"]["throttle_events"] += 1
                self.log("BUILDER_THROTTLED", outcome="THROTTLED",
                         activity_class="ORCHESTRATION",
                         metadata_redacted={"review_queue_depth": depth})

            self.checkpoints(doc, cs)

        # T1 has committed and the lock is released. C-18 stage 4's dispatch
        # execution runs FIRST among the post-transaction phases: it is the
        # direct continuation of the planning T1 just did, and a spawned
        # worker with no committed record is the one outcome worth shortening
        # the window on. It holds no transaction, so it cannot roll back a
        # merge and C-14.1's guarantee is unaffected.
        # C-18 stage 7: declared, because acquire_worktree, workmux and tmux
        # can together outlast what T1's heartbeat has left. The confirm is
        # outside the window - it is a fast transaction, not slow work.
        if self._dispatch_plans:
            self.confirm_dispatches(self.run_declared(
                "dispatch",
                batch_bound(DISPATCH_EXECUTE_BOUND_PER_PLAN,
                            len(self._dispatch_plans)),
                lambda: self.execute_dispatches(self._dispatch_plans)))

        # C-05.3a Phase C runs next - materialise the claimed attempt, write
        # the job file, spawn - with no state lock held, then Phase D caches
        # the spawn in its own transaction.
        #
        # Placed before execute_merges as directed. C-14.1's reason for
        # running merges early is ROLLBACK safety: each merge gets its own
        # transaction so no later failure can undo one GitHub already
        # recorded. Phase C holds no transaction, so it cannot roll a merge
        # back and that guarantee is unaffected. It does add worktree and
        # spawn latency ahead of merges - see the report.
        if planned_security:
            # Same shape and the same external calls as a dispatch, so the
            # same per-plan bound.
            self.confirm_security_spawn(self.run_declared(
                "security",
                batch_bound(DISPATCH_EXECUTE_BOUND_PER_PLAN,
                            len(planned_security)),
                lambda: self.execute_security(planned_security)))

        # C-05.3b Phase C + E. Outside every transaction, like the security
        # spawn above and for the same reason: an install, a build, a
        # server and a browser run are minutes of work, and holding the
        # state lock across them would stall every other task.
        if self._accessibility_plans:
            # THE ONE THAT WAS ALWAYS MANDATORY, not stage-7 polish. A single
            # attempt may legitimately run the whole governed G3 budget -
            # install+build 600, readiness 120, scan 120/300, teardown 60,
            # 1080 in total - which is nine times the 120 s staleness
            # threshold. Undeclared, every accessibility run would have made
            # a healthy supervisor look dead. It has been harmless only
            # because no services factory existed to make it run at all.
            self.commit_accessibility(self.run_declared(
                "accessibility",
                batch_bound(self._accessibility_budget().total,
                            len(self._accessibility_plans)),
                lambda: self.execute_accessibility(self._accessibility_plans)))

        # T1 has committed. Each merge now gets its own transaction, before the
        # slow provider work so a merge never waits on a provider call.
        # C-18 stage 7. NOT in the proposed table, and it belongs there: each
        # candidate makes up to six `gh` calls at 120 s each, so two
        # candidates can exceed the staleness threshold twelvefold. This is
        # also the phase where a false-positive Watchdog kill is least
        # acceptable, because gh.merge is irreversible.
        if merge_candidates:
            self.run_declared(
                "merges",
                batch_bound(MERGE_EXECUTE_BOUND_PER_CANDIDATE,
                            len(merge_candidates)),
                lambda: self.execute_merges(merge_candidates))

        # C-18 stage 2. Every notification this tick queued - from T1, from
        # Phase D and from each merge's own transaction - is delivered here,
        # with no lock held and under a bounded budget. Placed BEFORE the
        # slow provider work deliberately: the Observer alone may run for
        # minutes, and a HUMAN_REQUIRED message must not wait behind it.
        #
        # ONE allowance, shared with the second drain below. The governed
        # limits are per tick, so two drains must not each get a full one.
        drain_allowance = self.new_drain_allowance()
        # C-18 stage 7. Declared PER CALL, not once for both: each drain is
        # a separate stretch of wall clock with Jev and the Observer between
        # them, and a declaration re-published at the second drain is what
        # keeps a wedge there detectable within one drain's bound rather
        # than one tick's.
        self.run_declared("notifications", DRAIN_BOUND_SECONDS,
                          lambda: self.drain_notifications(drain_allowance))

        # Outside the lock: a slow provider must never stall the control cycle.
        # Each is declared as bounded busy work so a healthy supervisor is not
        # mistaken for a dead one while it waits on a provider.
        self.run_declared("jev", JEV_BOUND_SECONDS, self.consult_jev)
        observer_bound = (self.cfg.observer["hard_timeout_seconds"]
                          * (2 if self.cfg.observer.get("retry_once_fresh") else 1)
                          + BUSY_MARGIN_SECONDS)
        self.run_declared("observer", observer_bound, self.run_observer)

        # A second bounded drain, because the provider work above has its own
        # transaction and can queue an escalation - consult_jev records spend
        # and a crossed budget threshold raises HUMAN_REQUIRED. Without this
        # the hard-stop message would wait for the next tick, behind an
        # Observer that may legitimately run for minutes. Costs nothing when
        # nothing was queued: the drain finds an empty queue and returns.
        #
        # Deliberately the SAME allowance the first drain used: whatever it
        # spent is already gone, and this one may only use what is left.
        self.run_declared("notifications", DRAIN_BOUND_SECONDS,
                          lambda: self.drain_notifications(drain_allowance))

    def run_declared(self, what: str, bound_seconds: float, action):
        """Run bounded slow work with its deadline published in the heartbeat.

        Returns whatever `action` returned, so a phase whose result feeds a
        commit step can be declared without being split in two. The commit
        step itself stays OUTSIDE the declaration: it is a fast state
        transaction, and leaving the window open across it would overstate
        how long the slow work may legitimately take.
        """
        self.declare_busy(what, bound_seconds)
        try:
            return action()
        finally:
            self.clear_busy()

    def _acquire_singleton(self) -> bool:
        """Take the process-lifetime singleton lock, or refuse to start.

        The handle is kept on the instance so the file description - and with
        it the lock - survives for the life of the Supervisor. Letting it fall
        out of scope would close the descriptor and silently release the lock
        while the process kept running.

        The descriptor is marked non-inheritable so a spawned worker never
        carries the lock into its own process. Python marks new descriptors
        non-inheritable by default (PEP 446) and subprocess closes them anyway,
        but ownership of a singleton is not something to leave resting on two
        defaults holding at once.
        """
        try:
            handle = open(config.SINGLETON_LOCK_PATH, "a+", encoding="utf-8")
        except OSError:
            self.log("SUPERVISOR_START_REFUSED", outcome="LOCK_UNAVAILABLE",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"this_pid": os.getpid()})
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # BlockingIOError for a held lock; any other OSError is equally
            # "this process did not get exclusive ownership", and starting
            # without it is the one outcome that must not happen.
            handle.close()
            self.log("SUPERVISOR_START_REFUSED", outcome="ALREADY_RUNNING",
                     activity_class="ORCHESTRATION",
                     metadata_redacted={"incumbent_pid": _reported_pid(),
                                        "this_pid": os.getpid()})
            return False
        os.set_inheritable(handle.fileno(), False)
        self._singleton_lock = handle
        return True

    def run(self) -> int:
        config.ensure_runtime_dirs()

        # Exactly one supervisor. Two would both dispatch, and the state lock
        # only serialises their writes - it does not stop them duplicating work.
        #
        # This is an ATOMIC exclusion, and it replaces a check-then-write that
        # was not one: reading the pid file, testing liveness and then writing
        # left a window in which two starting processes could both observe no
        # incumbent and both proceed. Measured at 16 duplicate starts in 40
        # trials against the previous sequence.
        #
        # flock is the same primitive state.py and ledger.py already use. The
        # lock lives on the open file description, so it is held for exactly as
        # long as this process keeps the handle - and the kernel releases it on
        # exit OR on a crash, which is why no stale-lock cleanup exists here
        # and none is needed. The inode is created once and never unlinked:
        # replacing it would hand a second process an independent lock.
        if not self._acquire_singleton():
            return 1

        # Reporting only, and deliberately still written: watchdog.supervisor_pid
        # and preflight read this to identify and liveness-check the incumbent.
        # It no longer carries any exclusion duty.
        config.PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

        def stop(signum, _frame):
            # First signal wins. A second SIGTERM while the current tick
            # finishes must not rewrite why the stop began.
            if self._stop_signal is None:
                self._stop_signal = signum
            self.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        self.log("SUPERVISOR_STARTED", outcome="RUNNING", activity_class="ORCHESTRATION",
                 metadata_redacted={"pid": os.getpid()})
        while not self.stopping:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive its own bugs.
                # C-16: fixed finite structural metadata only. Runtime
                # exception prose can carry paths, document fragments and
                # env-derived text the static secret scan cannot vet, so
                # nothing exception-derived is durable ledger evidence.
                self.log("SUPERVISOR_ERROR", outcome="ERROR", activity_class="FAILED_WORK",
                         metadata_redacted={"phase": "SUPERVISOR_TICK",
                                            "error_code": "TICK_FAILED"})
            self._interruptible_sleep(self.cfg.poll_seconds)
        self.log("SUPERVISOR_STOPPED", outcome="STOPPED",
                 activity_class="ORCHESTRATION",
                 metadata_redacted={"stop_reason": self._stop_reason()})
        return 0

    # The complete vocabulary. Fixed and finite, per C-16: no runtime prose
    # reaches durable evidence, so this maps signal numbers to names rather
    # than formatting whatever arrived.
    STOP_REASONS = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
    }

    def _stop_reason(self) -> str:
        """Why the supervisor loop ended, for durable evidence.

        C-18a attaches this: with Run 002 itself serving as the endurance
        experiment, "when and why did it stop" is a result, not an
        operational detail. An early stop must be reconstructible as one.

        THE THREE REACHABLE OUTCOMES, and the one that is not:

        * SIGTERM / SIGINT - the ordinary governed stop. The loop finishes
          its current tick and exits.
        * LOOP_EXITED_WITHOUT_SIGNAL - the loop condition went false with
          no signal recorded. Unreachable today, because `stopping` is set
          only by the handler; it is named rather than omitted so that a
          future stop path which forgets to record itself produces a
          visible token instead of a confident "SIGTERM".
        * UNKNOWN_SIGNAL_<n> - a signal was handled that this table does
          not name. Recorded as unknown rather than silently dropped.

        NOT COVERED, and deliberately so: SIGKILL, a power loss, or an
        exception escaping run() leave NO SUPERVISOR_STOPPED event at all.
        The absence is itself the evidence - a SUPERVISOR_STARTED with no
        matching SUPERVISOR_STOPPED means the process died rather than
        stopped - and the Watchdog's own SUPERVISOR_RESTARTED /
        SUPERVISOR_RESTART_FAILED events are what distinguish an
        autonomous recovery from a run that simply ended there. No event
        this process writes could cover its own SIGKILL.
        """
        # getattr, not self._stop_signal, and the reason is not defensive
        # habit: SUPERVISOR_STOPPED is the LAST thing this process writes,
        # and C-18a makes it load-bearing evidence. A reason-reporter that
        # raises would take the whole stop event down with it and leave a
        # clean shutdown indistinguishable from a SIGKILL - destroying
        # exactly the distinction it exists to record. It must answer for
        # any Supervisor object, however constructed.
        stop_signal = getattr(self, "_stop_signal", None)
        if stop_signal is None:
            return "LOOP_EXITED_WITHOUT_SIGNAL"
        return self.STOP_REASONS.get(stop_signal,
                                     f"UNKNOWN_SIGNAL_{stop_signal}")

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep in short steps so a stop signal is honoured promptly."""
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(1)


def main() -> int:
    return Supervisor().run()


if __name__ == "__main__":
    raise SystemExit(main())
