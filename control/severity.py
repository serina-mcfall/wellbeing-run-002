"""Implements protocol/SEVERITY-POLICY.md (C-02).

Scope: this module applies the deterministic P1 accessibility floor and
the evidence-shape rule (SEVERITY-POLICY.md rules 1-3) to a single
finding. It does NOT decide merge eligibility for a whole PR (that
belongs to control/routing.py's evaluate_merge) and it does NOT own the
canonical set of accessibility requirement identifiers — callers must
supply known_requirement_ids, the identifier registry the PR evidence
validator maintains. Without that registry, a citation cannot be
confirmed and the finding fails closed as INVALID: a nonempty arbitrary
string alone is insufficient for the final gate.

merge_blocked reflects Protocol v2 "Severity — single source of truth":
P0/P1 always block merge; P2/P3 do not independently block merge;
INVALID evidence always blocks merge (a missing-evidence state, not a
severity choice — SEVERITY-POLICY.md rule 3).

Ported from apparatus/severity/severity-floor.js (Run 002's original,
proven Node implementation) so the canonical Python control plane does
not depend on Node for merge-gate decisions.

RUN001-F1 context: Run 001 did not merge a wrongly-classified finding.
Its defect was that the deployed Reviewer required every named gate to
PASS, so a genuine, correctly-classified P2 finding indirectly became
merge-blocking even though canonical severity policy says only P0/P1
block. This module is the piece of that fix that belongs here: it
computes merge_blocked correctly per finding (P2/P3 non-blocking unless
an applicable deterministic floor raises them; P0/P1 always blocking;
invalid evidence always blocking, independent of severity). Whether a
Reviewer's own gate-aggregation logic still requires all-PASS
regardless of per-finding severity is a routing/evaluate_merge concern,
not something this module can fix by itself.
"""

from __future__ import annotations

SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})


def _is_nonempty_string(value) -> bool:
    return isinstance(value, str) and len(value.strip()) > 0


def _invalid(reason: str) -> dict:
    return {
        "valid": False,
        "severity": None,
        "merge_blocked": True,
        "floor_applied": False,
        "reason": reason,
    }


def apply_severity_policy(finding, known_requirement_ids=None) -> dict:
    if finding is None or not isinstance(finding, dict):
        return _invalid("INVALID: malformed finding (not a plain object).")

    jev_severity = finding.get("jev_severity")
    classification = finding.get("classification")
    unmet_requirement = finding.get("unmet_requirement")
    non_failure_rationale = finding.get("non_failure_rationale")

    if not isinstance(jev_severity, str) or jev_severity not in SEVERITIES:
        return _invalid(f"INVALID: unknown or missing jev_severity ({jev_severity!r}).")

    if classification not in ("FAILURE", "NON_FAILURE"):
        return _invalid(
            "INVALID: evidence does not clearly cite an unmet requirement "
            "(case 1) or affirmatively explain why no requirement is unmet "
            "(case 2). Escalate to an independent reviewer per Protocol v2 "
            '"Accessibility gate".'
        )

    has_citation = _is_nonempty_string(unmet_requirement)
    has_rationale = _is_nonempty_string(non_failure_rationale)

    if classification == "FAILURE" and has_rationale:
        return _invalid(
            "INVALID: FAILURE classification carries a non_failure_rationale; "
            "contradictory evidence."
        )
    if classification == "NON_FAILURE" and has_citation:
        return _invalid(
            "INVALID: NON_FAILURE classification carries an unmet_requirement; "
            "contradictory evidence."
        )

    if classification == "FAILURE":
        if not has_citation:
            return _invalid(
                "INVALID: FAILURE classification without an unmet_requirement citation."
            )

        registry = set(known_requirement_ids) if known_requirement_ids else None
        if not registry or unmet_requirement not in registry:
            return _invalid(
                f'INVALID: unmet_requirement "{unmet_requirement}" does not match '
                "an identifier in the accessibility requirement registry (owned by "
                "the PR evidence validator, C-04); a nonempty arbitrary string "
                "alone is insufficient for the final gate."
            )

        floor_rank = SEVERITIES.index("P1")
        raw_rank = SEVERITIES.index(jev_severity)
        final_rank = min(raw_rank, floor_rank)
        severity = SEVERITIES[final_rank]

        return {
            "valid": True,
            "severity": severity,
            "merge_blocked": severity in BLOCKING_SEVERITIES,
            "floor_applied": final_rank != raw_rank,
            "reason": None,
        }

    if not has_rationale:
        return _invalid("INVALID: NON_FAILURE classification without an affirmative rationale.")

    return {
        "valid": True,
        "severity": jev_severity,
        "merge_blocked": jev_severity in BLOCKING_SEVERITIES,
        "floor_applied": False,
        "reason": None,
    }
