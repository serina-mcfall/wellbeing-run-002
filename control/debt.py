"""C-10.2: the accepted non-blocking debt register.

Protocol v2 §"Stable finding IDs": "P2/P3 that merge enter a debt register;
non-blocking does not mean forgotten." C-10.1 made a correctly-attributed
P2/P3 stop blocking a REVIEW_PASS; this module is the other half — the durable
record of what was accepted when such a pull request actually merged.

Two helpers, two different trust boundaries, same rules:

  * retain_accepted() validates FRESH reviewer output at the approving review,
    and is allowed to refuse. A refusal happens before the PR is ever
    merge-eligible, which is the only point at which refusing is still free.
  * accept() validates STORED state at merge time, and is NOT allowed to
    refuse in any way that raises. By then gh.merge() has already happened and
    cannot be undone; raising would roll the state transaction back, lose
    record["merged"], and leave the next tick looking at a closed pull request
    it can never complete. It fails closed by declining to write instead.

Debt records are not human interventions and never touch doc["interventions"].
This module creates ACCEPTED_NONBLOCKING records only; closing or superseding
them is later work, which is why `status` is a stored field rather than an
implied constant.

The debt id qualifies the reviewer's own finding id with task and PR. That is
deliberately NOT a Protocol-form globally stable finding ID: it resolves
cross-task and cross-PR collisions only, carries no review cycle, and cannot be
joined to a PR-evidence package. Cross-cycle collisions do not arise because
retention overwrites on each approving review, so only one accepted set ever
reaches a merge.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import redact, routing

# Only these may become accepted non-blocking debt. P0/P1 never can: C-10.1
# already refuses a REVIEW_PASS carrying one, and retain_accepted refuses again
# rather than silently dropping it.
ACCEPTABLE_SEVERITIES = frozenset({"P2", "P3"})

STATUS_ACCEPTED = "ACCEPTED_NONBLOCKING"
SUMMARY_MAX = 300

RECORDED = "RECORDED"
NOT_REQUIRED = "NOT_REQUIRED"
FAILED = "FAILED"

# A finite vocabulary. Only these strings are ever persisted to state or the
# ledger for a debt failure — never an exception message, which would put
# uncontrolled text into the one durable surface that does not redact.
ERROR_CODES = frozenset({
    "MALFORMED_RETAINED_SET",
    "INVALID_RETAINED_ENTRY",
    "DUPLICATE_FINDING_ID",
    "DEBT_KEY_CONFLICT",
    "MISSING_MERGE_CONTEXT",
    "UNEXPECTED",
})


@dataclass(frozen=True)
class Retention:
    """The outcome of validating an approving review's accepted findings.
    `reason` is human-readable and feeds the existing REVIEW_REJECTED_BY_SUPERVISOR
    `reason` metadata key, so a refusal reads the same as any other review
    rejection."""

    ok: bool
    entries: tuple[dict, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class DebtResult:
    """The outcome of accepting debt after a merge. `error` is always one of
    ERROR_CODES, or None."""

    status: str
    debt_ids: tuple[str, ...] = ()
    error: str | None = None


def debt_id(task_id: str, pr_number, finding_id: str) -> str:
    return f"{task_id}-PR{pr_number}-{finding_id}"


def _clean_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _safe_summary(value) -> str:
    """Scrub and bound a reviewer's summary.

    Scrubbed rather than rejected: this is model output, and the governed
    boundary treats model text differently from operator text. A redacted
    summary is still valid retained evidence and must never be the reason a
    merge is refused.

    No "uncoercible" branch exists because findings come from json.loads, so a
    value is only ever str/int/float/bool/None/list/dict — str() is total over
    all of those. A guard here could not be made to fail.
    """
    return redact.scrub(str("" if value is None else value))[:SUMMARY_MAX]


def retain_accepted(review) -> Retention:
    """The reduced, scrubbed set to store on the PR record at an approving
    review, or a refusal naming the first problem found.

    Everything needed to build debt later is validated now, so that a duplicate
    or malformed finding identity is caught while refusing is still possible.
    Only the reduced four fields are kept: the reviewer's other fields are
    unbounded free text with no use here, and this structure enters durable
    state.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    for index, finding in enumerate(review.findings, start=1):
        finding_id = _clean_id(finding.get("id"))
        if finding_id is None:
            return Retention(False, (), f"finding {index} has a missing or blank id")
        if finding_id in seen:
            return Retention(
                False, (), f"duplicate finding id {finding_id} in the approving review")
        seen.add(finding_id)

        severity = finding.get("severity")
        if severity not in ACCEPTABLE_SEVERITIES:
            return Retention(
                False, (),
                f"finding {finding_id} has severity {severity!r}, which cannot be "
                "accepted as non-blocking debt")

        category = finding.get("category")
        if category not in routing.KNOWN_GATES:
            return Retention(
                False, (), f"finding {finding_id} has an unrecognised category")

        entries.append({
            "id": finding_id,
            "severity": severity,
            "category": category,
            "summary": _safe_summary(finding.get("summary")),
        })

    return Retention(True, tuple(entries))


def _record(entry: dict, *, task_id: str, pr_number, merged_sha, at: str) -> dict:
    return {
        "id": debt_id(task_id, pr_number, entry["id"]),
        "finding_id": entry["id"],
        "task_id": task_id,
        "pr_number": pr_number,
        "severity": entry["severity"],
        "category": entry["category"],
        "summary": entry["summary"],
        "status": STATUS_ACCEPTED,
        "accepted_at": at,
        "merged_sha": merged_sha,
    }


def _comparable(record: dict) -> dict:
    """Everything but the timestamp. A genuine retry of the same acceptance
    differs only in when it ran, and must not read as a conflict."""
    return {k: v for k, v in record.items() if k != "accepted_at"}


def accept(doc: dict, *, task_id: str, pr_number, accepted_findings,
           merged_sha, at: str) -> DebtResult:
    """Record accepted non-blocking debt for a pull request that has merged.

    All or nothing: every record is built and checked before doc["debt"] is
    touched, so a failure leaves the register byte-identical. Never raises —
    see the module docstring for why that matters after gh.merge().
    Idempotent: re-running preserves the original accepted_at and still
    reports the ids.
    """
    try:
        return _accept(doc, task_id=task_id, pr_number=pr_number,
                       accepted_findings=accepted_findings,
                       merged_sha=merged_sha, at=at)
    except Exception:  # noqa: BLE001 - a bug here must not strand a merged task
        return DebtResult(FAILED, (), "UNEXPECTED")


def _accept(doc: dict, *, task_id: str, pr_number, accepted_findings,
            merged_sha, at: str) -> DebtResult:
    if accepted_findings is None:
        # A PR merged before this checkpoint existed, or one still in flight at
        # upgrade time. Absence is not evidence of unrecorded debt.
        return DebtResult(NOT_REQUIRED)
    if not isinstance(accepted_findings, list):
        return DebtResult(FAILED, (), "MALFORMED_RETAINED_SET")
    if not accepted_findings:
        return DebtResult(NOT_REQUIRED)

    if not _clean_id(task_id) or not isinstance(pr_number, int):
        return DebtResult(FAILED, (), "MISSING_MERGE_CONTEXT")

    built: dict[str, dict] = {}
    seen: set[str] = set()
    for entry in accepted_findings:
        if not isinstance(entry, dict):
            return DebtResult(FAILED, (), "MALFORMED_RETAINED_SET")

        finding_id = _clean_id(entry.get("id"))
        if finding_id is None:
            return DebtResult(FAILED, (), "INVALID_RETAINED_ENTRY")
        if finding_id in seen:
            return DebtResult(FAILED, (), "DUPLICATE_FINDING_ID")
        seen.add(finding_id)

        if entry.get("severity") not in ACCEPTABLE_SEVERITIES:
            return DebtResult(FAILED, (), "INVALID_RETAINED_ENTRY")
        if entry.get("category") not in routing.KNOWN_GATES:
            return DebtResult(FAILED, (), "INVALID_RETAINED_ENTRY")
        if not isinstance(entry.get("summary"), str):
            return DebtResult(FAILED, (), "INVALID_RETAINED_ENTRY")

        record = _record(entry, task_id=task_id, pr_number=pr_number,
                         merged_sha=merged_sha, at=at)
        built[record["id"]] = record

    existing = doc.get("debt", {})
    for key, record in built.items():
        prior = existing.get(key)
        if prior is not None and _comparable(prior) != _comparable(record):
            return DebtResult(FAILED, (), "DEBT_KEY_CONFLICT")

    # Nothing above this line has mutated doc. Only now, with every record
    # built and every conflict ruled out, is the register written.
    register = doc.setdefault("debt", {})
    for key, record in built.items():
        register.setdefault(key, record)

    return DebtResult(RECORDED, tuple(built))
