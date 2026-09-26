"""C-14.2: deterministic three-source merge-invariant classification.

C-14.1 stopped the control plane *creating* a merge/state divergence in the
ordinary case. It did not close the residual: if the dedicated merge
transaction's own state commit fails after gh.merge() has already succeeded,
GitHub, the ledger and state.json still disagree and nothing notices.

This module is the detector. It is deliberately pure: no network, no state
mutation, no ledger writes, no clock reads. Callers gather the three
observations and hand them in, which is what lets the Supervisor and the
Watchdog reach identical verdicts from independently-made observations - and
the Watchdog is the component that matters, because the Supervisor is the one
whose claims are under suspicion.

Three sources, none of them authoritative alone:

  GitHub   - what is true out there now.        Not who did it.
  Ledger   - what THIS control plane did.       Not whether bookkeeping landed.
  State    - what bookkeeping committed.        Nothing about the outside world.

Two rules that look like paranoia and are not:

  * accepted_findings is NOT an input. control/debt.py's "absence is not
    evidence of unrecorded debt" reasoning is correct on the live merge path
    and inverts here, because accepted_findings is itself state that a
    rollback may have destroyed. Debt expectations come from the ledger's
    recorded debt_ids, never from state.
  * A negative ("no local merge evidence") is only trustworthy when the
    ledger was wholly readable AND no blocked MERGED event could have been
    substituted inside this pull request's merge window. Otherwise the honest
    answer is UNPROVABLE, not "ordinary external merge".

C-14.3 - reconstruction, ledger repair and general idempotency keys - is out
of scope. This module detects; it never repairs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from . import clock, intervention
from . import state as state_mod

# ------------------------------------------------------------------ verdicts

ORDINARY_EXTERNAL = "ORDINARY_EXTERNAL"
LOST_LOCAL_COMMIT = "LOST_LOCAL_COMMIT"
DEBT_DIVERGENCE = "DEBT_DIVERGENCE"
UNPROVABLE = "UNPROVABLE"

VERDICTS = (ORDINARY_EXTERNAL, LOST_LOCAL_COMMIT, DEBT_DIVERGENCE, UNPROVABLE)

CHECK_IDS = {
    ORDINARY_EXTERNAL: "MERGE_ORDINARY_EXTERNAL",
    LOST_LOCAL_COMMIT: "MERGE_LOST_LOCAL_COMMIT",
    DEBT_DIVERGENCE: "MERGE_DEBT_DIVERGENCE",
    UNPROVABLE: "MERGE_UNPROVABLE",
}

# Finite, structured evidence vocabulary. Durable metadata carries these
# rather than prose: they reconstruct the reasoning, they are bounded, and
# they cannot smuggle uncontrolled text into the ledger.
GITHUB_UNOBSERVED = "GITHUB_UNOBSERVED"
GITHUB_MERGED = "GITHUB_MERGED"
GITHUB_NOT_MERGED = "GITHUB_NOT_MERGED"
STATE_MERGED = "STATE_MERGED"
STATE_UNMERGED = "STATE_UNMERGED"
LOCAL_MERGE_EVIDENCE = "LOCAL_MERGE_EVIDENCE"
NO_LOCAL_MERGE_EVIDENCE = "NO_LOCAL_MERGE_EVIDENCE"
LEDGER_INCOMPLETE = "LEDGER_INCOMPLETE"
BLOCKED_MERGED_IN_WINDOW = "BLOCKED_MERGED_IN_WINDOW"
NO_WINDOW_LOWER_BOUND = "NO_WINDOW_LOWER_BOUND"
DEBT_CLAIMED_RECORDED = "DEBT_CLAIMED_RECORDED"
DEBT_IDS_MISSING = "DEBT_IDS_MISSING"
DEBT_FAILED_WITHOUT_EVENT = "DEBT_FAILED_WITHOUT_EVENT"

# control/notify.py's own finite result vocabulary, reused rather than
# reinvented. Its HTTP path returns unbounded text, which never reaches
# durable metadata - it collapses to NOTIFICATION_SEND_FAILED.
NOTIFICATION_SEND_FAILED = "NOTIFICATION_SEND_FAILED"

DEBT_RECORDED = "RECORDED"
DEBT_NOT_REQUIRED = "NOT_REQUIRED"
DEBT_FAILED = "FAILED"


# -------------------------------------------------------------- observations


@dataclass(frozen=True)
class GithubObservation:
    observed: bool
    merged: bool = False
    merged_sha: str | None = None


@dataclass(frozen=True)
class StateView:
    """Everything the classifier may read from state. accepted_findings is
    deliberately not a member - see the module docstring."""
    task_state: str | None
    record_present: bool
    record_merged: bool
    debt_ids_present: frozenset = frozenset()
    annunciated_fingerprint: str | None = None


@dataclass(frozen=True)
class LedgerView:
    readable: bool
    unreadable_lines: int = 0
    local_merged: tuple = ()
    external_merged: tuple = ()
    claimed_debt_ids: frozenset = frozenset()
    claimed_debt_status: str | None = None
    debt_recording_failed: bool = False
    blocked_merged_in_window: int = 0
    window_lower: str | None = None
    window_lower_source: str | None = None


@dataclass(frozen=True)
class MergeVerdict:
    verdict: str
    dangerous: bool
    task_id: str
    pr_number: int
    check_id: str
    evidence_codes: tuple = ()
    missing_debt_ids: tuple = ()
    freezable: bool = False
    summary: str = ""        # human-readable, for notifications only.
                             # Never persisted, never fingerprinted.


# --------------------------------------------------------------- freezability


def freezable(task_state) -> bool:
    """Whether FROZEN is a legal outbound transition from this state.

    Derived from state.ALLOWED_TRANSITIONS rather than hard-coded, so the two
    cannot drift. MERGE_READY, MERGED and COMPLETE have no FROZEN edge;
    attempting one would raise TransitionError, which watchdog.py swallows -
    silently dropping the very violation this module exists to surface.
    """
    if not task_state:
        return False
    return "FROZEN" in state_mod.ALLOWED_TRANSITIONS.get(task_state, frozenset())


# ------------------------------------------------------------ window bounds


def _instant(value):
    """A timezone-aware datetime, or None if the value cannot be trusted.

    ISO strings must never be compared lexicographically here. clock.iso()
    writes seconds precision while Ledger.append() writes milliseconds, and
    Pacific/Auckland alternates +12:00 and +13:00 - its April fall-back
    repeats an hour with two different offsets, so string order and
    chronological order genuinely disagree. Comparing parsed instants is the
    only correct thing.

    Returns None rather than raising; callers decide how to fail closed, and
    no parse exception text is ever persisted.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = clock.parse(value)
    except (ValueError, TypeError):
        return None
    return moment if moment.tzinfo is not None else None


def window_lower_bound(task: dict | None, record: dict | None) -> tuple:
    """(timestamp, source) for the earliest instant a merge of this pull
    request could have been claimed, or (None, None) if none is reliable.

    Narrowest first. last_review_at is tightest: a local merge is only
    reachable through a review that set it. Failing that, the first PR_OPEN
    history entry - which is guaranteed present whenever task["pr"] is set,
    because supervisor.attach_pr assigns task["pr"] and transitions to
    PR_OPEN in the same committed operation.
    """
    if record and record.get("last_review_at"):
        return record["last_review_at"], "last_review_at"
    for entry in (task or {}).get("history") or ():
        if entry.get("to") == "PR_OPEN" and entry.get("at"):
            return entry["at"], "pr_open_history"
    return None, None


# --------------------------------------------------------------- classifier


def classify(*, task_id: str, pr_number: int, github: GithubObservation,
             state_view: StateView, ledger_view: LedgerView) -> MergeVerdict | None:
    """The deterministic verdict, or None when the three sources agree.

    First match wins; the ordering is the safety ordering. Every path that
    cannot *prove* its conclusion returns UNPROVABLE rather than the
    convenient answer.
    """
    codes: list[str] = []
    can_freeze = freezable(state_view.task_state)

    def verdict(name, *, dangerous=True, missing=(), summary=""):
        return MergeVerdict(
            verdict=name, dangerous=dangerous, task_id=task_id, pr_number=pr_number,
            check_id=CHECK_IDS[name], evidence_codes=tuple(codes),
            missing_debt_ids=tuple(missing),
            freezable=can_freeze if dangerous else False, summary=summary,
        )

    codes.append(GITHUB_MERGED if github.merged else
                 (GITHUB_NOT_MERGED if github.observed else GITHUB_UNOBSERVED))
    codes.append(STATE_MERGED if state_view.record_merged else STATE_UNMERGED)

    # 1. The ledger could not be read in full. This is NOT narrowed by a
    #    failed GitHub observation: an incomplete ledger is itself the loss of
    #    one of the three sources this module polices, and with it neither the
    #    presence nor the absence of a local merge claim can be established -
    #    so nothing can be safely concluded AND nothing can be safely deferred.
    if not ledger_view.readable:
        codes.append(LEDGER_INCOMPLETE)
        return verdict(UNPROVABLE,
                       summary="the ledger could not be read in full, so absent "
                               "local merge evidence proves nothing")

    has_local = bool(ledger_view.local_merged)
    codes.append(LOCAL_MERGE_EVIDENCE if has_local else NO_LOCAL_MERGE_EVIDENCE)

    # 2. Debt divergence is provable from the ledger and the debt register
    #    alone. It must not disappear merely because GitHub is momentarily
    #    unreachable, so it is judged before observability is considered.
    if has_local and state_view.record_merged:
        status = ledger_view.claimed_debt_status
        if status == DEBT_RECORDED and ledger_view.claimed_debt_ids:
            codes.append(DEBT_CLAIMED_RECORDED)
            missing = sorted(ledger_view.claimed_debt_ids - state_view.debt_ids_present)
            if missing:
                codes.append(DEBT_IDS_MISSING)
                return verdict(DEBT_DIVERGENCE, missing=missing,
                               summary="the ledger records accepted debt that the "
                                       "debt register does not contain")
            return None
        if status == DEBT_FAILED and not ledger_view.debt_recording_failed:
            codes.append(DEBT_FAILED_WITHOUT_EVENT)
            return verdict(UNPROVABLE,
                           summary="the ledger claims debt recording failed but "
                                   "carries no DEBT_RECORDING_FAILED event")
        return None

    # 3. A durable local merge claim that state never committed. This is a
    #    contradiction already established by two sources, so a failed GitHub
    #    observation makes it unverifiable, not benign.
    if has_local and not state_view.record_merged:
        if not github.observed:
            return verdict(UNPROVABLE,
                           summary="a local merge was recorded that state never "
                                   "committed, and GitHub cannot be observed to "
                                   "check it")
        if github.merged:
            return verdict(LOST_LOCAL_COMMIT,
                           summary="this control plane recorded a local merge that "
                                   "state never committed")
        return verdict(UNPROVABLE,
                       summary="a local merge was recorded but GitHub does not "
                               "report the pull request as merged")

    # 4. No local claim and nothing durable in contradiction. A failed GitHub
    #    observation here is simply insufficient evidence to run the classifier
    #    to a conclusion - not a violation, and deliberately not
    #    ORDINARY_EXTERNAL either, because we do not know whether it merged.
    #    Defer to the next observation. None is that answer; there is no fifth
    #    verdict.
    if not github.observed:
        return None

    # 5. GitHub agrees the pull request is not merged. All three sources agree.
    if not github.merged:
        return None

    # 6. Merged out there while state says otherwise, and no local claim - but
    #    a negative ledger conclusion is only trustworthy if the window it was
    #    drawn over can be bounded and holds no substituted MERGED event.
    if ledger_view.window_lower is None:
        codes.append(NO_WINDOW_LOWER_BOUND)
        return verdict(UNPROVABLE,
                       summary="no reliable lower bound exists for this pull "
                               "request's merge window")
    if ledger_view.blocked_merged_in_window:
        codes.append(BLOCKED_MERGED_IN_WINDOW)
        return verdict(UNPROVABLE,
                       summary="a blocked MERGED event inside this pull request's "
                               "window makes absent local evidence untrustworthy")

    return verdict(ORDINARY_EXTERNAL, dangerous=False,
                   summary="merged outside this control plane")


# --------------------------------------------------------------- fingerprint


def fingerprint(verdict: MergeVerdict, github: GithubObservation,
                state_view: StateView, ledger_view: LedgerView) -> str:
    """A deterministic identity for THIS finding on THIS evidence.

    Structured fields only. Free text is excluded so that rewording a message
    never re-annunciates, and the detector's identity is excluded so that the
    Supervisor and the Watchdog seeing the same thing annunciate once between
    them rather than once each.

    The blocked-event signal is a boolean, not a count: an unrelated blocked
    event landing in the window must not churn an otherwise identical finding.
    """
    payload = json.dumps(
        {
            "check_id": verdict.check_id,
            "verdict": verdict.verdict,
            "github_observed": github.observed,
            "github_merged": github.merged,
            "github_merged_sha": github.merged_sha,
            "state_merged": state_view.record_merged,
            "task_state": state_view.task_state,
            "claimed_debt_ids": sorted(ledger_view.claimed_debt_ids),
            "missing_debt_ids": sorted(verdict.missing_debt_ids),
            "ledger_readable": ledger_view.readable,
            "blocked_merged_ambiguity": ledger_view.blocked_merged_in_window > 0,
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def notification_status(result: dict | None) -> tuple:
    """(delivered, finite_code) from control/notify.py's result dict.

    notify.py already speaks a finite vocabulary on its refusal paths
    (BLOCKED_SECRET_IN_NOTIFICATION, DISCORD_WEBHOOK_URL_NOT_SET); those are
    reused verbatim. Its HTTP path returns unbounded upstream text, which is
    never persisted - it collapses to one code.
    """
    result = result or {}
    if result.get("ok"):
        return True, None
    reason = result.get("reason")
    if isinstance(reason, str) and reason:
        return False, reason
    return False, NOTIFICATION_SEND_FAILED


def gather(*, task_id, pr_number, doc, task, record, ledger, pr_view, now_iso):
    """Assemble the three observations. The only impure step, and it is the
    caller's own ledger and its own GitHub observation - which is what makes
    the Watchdog's verdict independent of the Supervisor's."""
    lower, lower_source = window_lower_bound(task, record)
    merged = ledger.inspect(task_id=task_id, pr_id=pr_number, event_types=("MERGED",))
    failed = ledger.inspect(task_id=task_id, pr_id=pr_number,
                            event_types=("DEBT_RECORDING_FAILED",))
    guard = ledger.inspect(event_types=("GUARDRAIL_RED",))

    github = GithubObservation(
        observed=pr_view is not None,
        merged=(pr_view or {}).get("state", "").upper() == "MERGED",
        merged_sha=((pr_view or {}).get("mergeCommit") or {}).get("oid"),
    )
    state_view = StateView(
        task_state=(task or {}).get("state"),
        record_present=record is not None,
        record_merged=bool((record or {}).get("merged")),
        debt_ids_present=frozenset((doc.get("debt") or {}).keys()),
        annunciated_fingerprint=(record or {}).get("invariant_annunciation_fingerprint"),
    )
    ledger_view = build_ledger_view(
        merged_events=merged.events, guardrail_events=guard.events,
        debt_failed_events=failed.events,
        readable=merged.complete and failed.complete and guard.complete,
        unreadable_lines=(merged.unreadable_lines + failed.unreadable_lines
                          + guard.unreadable_lines),
        window_lower=lower, window_lower_source=lower_source, window_upper=now_iso,
    )
    return github, state_view, ledger_view


def annunciate(*, doc, task, record, verdict, github, state_view, ledger_view,
               ledger, notifier, detector, tz):
    """Durably record a dangerous verdict, once per distinct finding.

    Freezes only where ALLOWED_TRANSITIONS permits it; a terminal task is
    annunciated without an illegal transition being attempted, because what is
    being withdrawn there is trust in the merge record, not execution of
    automation that already finished.

    Never repairs anything. Never raises the experiment-wide guardrail.
    Returns True when a new annunciation was made.
    """
    # The fingerprint includes task state, and freezing CHANGES task state.
    # Compared naively, our own freeze would make every finding look new and
    # suppression would never fire. The task state is therefore normalised
    # back to the value observed when this finding was first annunciated, so
    # only evidence we did not cause counts as a material change.
    prior_state = (record or {}).get("invariant_annunciation_state")
    basis = state_view
    if prior_state and state_view.task_state == "FROZEN" and prior_state != "FROZEN":
        basis = replace(state_view, task_state=prior_state)

    print_id = fingerprint(verdict, github, basis, ledger_view)
    if record is not None and \
            record.get("invariant_annunciation_fingerprint") == print_id:
        return False

    before = state_view.task_state
    froze = False
    if verdict.freezable and task is not None:
        try:
            state_mod.transition(doc, verdict.task_id, "FROZEN",
                                 f"{verdict.check_id}: {verdict.verdict}", tz)
            froze = True
        except state_mod.TransitionError:
            froze = False

    # C-08b.2 (S11): the durable human-verification obligation, shared by both
    # detectors through this one entry point. The reason is identity-only -
    # verdict names, check IDs and evidence stay in the STATE_INVARIANT_VIOLATION
    # event below. A re-annunciation on materially changed evidence reuses the
    # still-open intervention: no second REQUESTED event, and the identity rides
    # on the existing single notification rather than adding another.
    int_record, int_is_new = intervention.request(
        doc, type_="HUMAN_VERIFICATION", scope="task", task_id=verdict.task_id,
        reason=f"Merge integrity requires human verification for "
               f"{verdict.task_id} PR #{verdict.pr_number}",
        condition_code="merge_invariant_violation", tz=tz)
    if int_is_new:
        ledger.append(
            "HUMAN_INTERVENTION_REQUESTED",
            task_id=verdict.task_id, pr_id=verdict.pr_number,
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
                "detector": detector,
            })

    delivered, status_code = notification_status(notifier.send(
        "HUMAN_REQUIRED",
        f"{verdict.task_id} PR #{verdict.pr_number}: merge invariant violated",
        f"{verdict.verdict}. {verdict.summary}. Detected by the {detector}. "
        "No automatic repair has been attempted."
        + intervention.notification_suffix(int_record),
    ))

    if record is not None:
        record["invariant_annunciation_fingerprint"] = print_id
        record.setdefault("invariant_annunciation_state", before)
    doc["counters"]["human_interventions"] += 1

    ledger.append(
        "STATE_INVARIANT_VIOLATION",
        task_id=verdict.task_id, pr_id=verdict.pr_number,
        state_before=before, state_after=("FROZEN" if froze else before),
        human_intervention=False,      # the DETECTOR is automated
        activity_class="ESCALATION",
        outcome=("FROZEN" if froze else "ANNUNCIATED"),
        metadata_redacted={
            "check_id": verdict.check_id,
            "verdict": verdict.verdict,
            "detector": detector,
            "fingerprint": print_id,
            "freezable": verdict.freezable,
            "froze": froze,
            "evidence_codes": list(verdict.evidence_codes),
            "github_observed": github.observed,
            "github_merged": github.merged,
            "merged_sha": github.merged_sha,
            "state_merged": state_view.record_merged,
            "task_state": before,
            "local_merged_events": len(ledger_view.local_merged),
            "external_merged_events": len(ledger_view.external_merged),
            "claimed_debt_status": ledger_view.claimed_debt_status,
            "claimed_debt_ids": sorted(ledger_view.claimed_debt_ids),
            "missing_debt_ids": list(verdict.missing_debt_ids),
            "ledger_readable": ledger_view.readable,
            "ledger_unreadable_lines": ledger_view.unreadable_lines,
            "blocked_merged_in_window": ledger_view.blocked_merged_in_window,
            "window_lower_bound": ledger_view.window_lower,
            "window_lower_source": ledger_view.window_lower_source,
            "notification_delivered": delivered,
            "notification_status": status_code,
        },
    )
    return True


def build_ledger_view(*, merged_events, guardrail_events, debt_failed_events,
                      readable: bool, unreadable_lines: int,
                      window_lower: str | None, window_lower_source: str | None,
                      window_upper: str) -> LedgerView:
    """Pure assembly of a LedgerView from already-fetched events.

    A MERGED event counts as locally initiated when detected_externally is
    absent or false. Absence means local because the externally-detected
    branch and the field were introduced in the same commit (ddeca96), so no
    external MERGED event has ever been written without it.
    """
    local, external = [], []
    claimed_ids: set = set()
    claimed_status = None
    for event in merged_events:
        meta = event.get("metadata_redacted") or {}
        if meta.get("detected_externally") is True:
            external.append(event)
            continue
        local.append(event)
        if "debt_recording_status" in meta:
            claimed_status = meta.get("debt_recording_status")
        claimed_ids.update(meta.get("debt_ids") or ())

    # Both bounds must parse for the window to mean anything. An unparsable
    # bound is an unreliable bound, which the classifier already treats as
    # NO_WINDOW_LOWER_BOUND and fails closed on.
    lower_at, upper_at = _instant(window_lower), _instant(window_upper)
    if lower_at is None or upper_at is None:
        window_lower, window_lower_source = None, None

    blocked = 0
    if lower_at is not None and upper_at is not None:
        for event in guardrail_events:
            meta = event.get("metadata_redacted") or {}
            if meta.get("blocked_event_type") != "MERGED":
                continue
            at = _instant(event.get("timestamp"))
            # A blocked MERGED event whose own timestamp cannot be parsed
            # cannot be placed outside the window, and the guardrail
            # substitution has already discarded its task and PR identity.
            # Counting it in is the only reading that cannot fail open.
            if at is None or lower_at <= at <= upper_at:
                blocked += 1

    return LedgerView(
        readable=readable, unreadable_lines=unreadable_lines,
        local_merged=tuple(local), external_merged=tuple(external),
        claimed_debt_ids=frozenset(claimed_ids), claimed_debt_status=claimed_status,
        debt_recording_failed=bool(debt_failed_events),
        blocked_merged_in_window=blocked,
        window_lower=window_lower, window_lower_source=window_lower_source,
    )
