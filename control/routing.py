"""PR routing and the merge gate.

Codex is the acceptance authority; the deterministic supervisor executes the
merge. A merge happens only when every gate in `evaluate_merge` is satisfied.

"Material diff" is the content of the pull request's diff, not its head SHA:
reconciling with main always moves the head commit, but only a change in the
diff itself invalidates a review.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import gh

SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})

REVIEW_PASS = "REVIEW_PASS"
REVIEW_FAIL = "REVIEW_FAIL"
REVIEW_UNPARSEABLE = "REVIEW_UNPARSEABLE"

# The canonical review dimensions, in prompts/reviewer.md's own order. Gate keys
# and a finding's category are drawn from this one vocabulary — that shared
# vocabulary is what makes attribution checkable at all. A test asserts this
# tuple still matches the prompt, so a twelfth dimension cannot be added to one
# without the other.
KNOWN_GATES = (
    "SCOPE", "ACCEPTANCE_CRITERIA", "CORRECTNESS", "TESTS", "ACCESSIBILITY",
    "COGNITIVE_LOAD", "SENSORY_LOAD", "SECURITY", "PRIVACY", "ARCHITECTURE",
    "OVERENGINEERING",
)

# A gate is graded or it is not. An unrecognised value must fail closed rather
# than slip past the FAIL branch as an accidental third state.
GATE_VALUES = frozenset({"PASS", "FAIL"})

REQUIRED_UI_GATES = ("OVERENGINEERING", "COGNITIVE_LOAD", "SENSORY_LOAD")

_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class Review:
    verdict: str
    findings: list[dict] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    raw_excerpt: str = ""

    @property
    def blocking(self) -> list[dict]:
        return [f for f in self.findings if f.get("severity") in BLOCKING_SEVERITIES]

    @property
    def critical(self) -> list[dict]:
        return [f for f in self.findings if f.get("severity") == "P0"]

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "gates": self.gates,
            "summary": self.summary[:500],
            "finding_ids": [f.get("id") for f in self.findings],
            "counts": {s: sum(1 for f in self.findings if f.get("severity") == s)
                       for s in SEVERITIES},
        }


def _normalised(value) -> str:
    """Upper-cased, stripped token. Never None, so a consumer using
    .get(field, "") is unaffected by a blank the reviewer sent."""
    return "" if value is None else str(value).strip().upper()


def parse_review(text: str) -> Review:
    """Extract the reviewer's machine-readable verdict block.

    An unparseable review is never treated as a pass.
    """
    excerpt = (text or "")[-1500:]
    candidates = _BLOCK.findall(text or "")
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "verdict" not in data:
            continue
        verdict = str(data["verdict"]).upper().strip()
        if verdict not in (REVIEW_PASS, REVIEW_FAIL):
            continue
        findings = [f for f in data.get("findings", []) if isinstance(f, dict)]
        for index, finding in enumerate(findings, start=1):
            finding.setdefault("id", f"F{index}")
            # Normalise in place, but never invent a value. A missing severity
            # used to default to "P2", which silently made an unlabelled
            # finding non-blocking; absence must survive to
            # review_is_consistent so it can fail closed. Only keys the
            # reviewer actually sent are rewritten, so consumers that call
            # .get(field, "") keep their own defaults for an absent field.
            for field in ("severity", "category"):
                if field in finding:
                    finding[field] = _normalised(finding[field])
        gates = {str(k).upper(): str(v).upper()
                 for k, v in (data.get("gates") or {}).items()}
        return Review(verdict, findings, gates, str(data.get("summary", "")), excerpt)
    return Review(REVIEW_UNPARSEABLE, raw_excerpt=excerpt)


def review_is_consistent(review: Review, touches_ui: bool) -> tuple[bool, str]:
    """Whether a claimed REVIEW_PASS is supportable by the evidence it carries.

    C-10: a gate graded FAIL for a correctly-attributed P2/P3 finding no longer
    prevents a pass. Protocol v2 "Severity --- single source of truth" makes
    only P0/P1 independently blocking; the previous gate-level check ignored
    severity entirely and so reproduced RUN001-F1, where a genuine,
    correctly-classified P2 became merge-blocking.

    Everything else fails closed: a missing or unrecognised severity, a missing
    or unrecognised category, an unrecognised gate value, and a FAIL gate that
    no finding accounts for. Malformed evidence is checked before it is
    interpreted, so it can never be read as a lesser finding.

    REVIEW_FAIL is never upgraded here. The reviewer is the acceptance
    authority; this only asks whether a claimed PASS holds up.
    """
    if review.verdict != REVIEW_PASS:
        return True, ""

    ungraded = sorted({v for v in review.gates.values() if v not in GATE_VALUES})
    if ungraded:
        return False, f"unrecognised gate value(s): {', '.join(ungraded)}"

    for finding in review.findings:
        name = finding.get("id") or "(unidentified finding)"
        if finding.get("severity") not in SEVERITIES:
            return False, f"{name} has a missing or unrecognised severity"
        if finding.get("category") not in KNOWN_GATES:
            return False, f"{name} has a missing or unrecognised category"

    if review.blocking:
        return False, "verdict PASS contradicted by P0/P1 findings"

    # A gate key outside KNOWN_GATES needs no separate rule: no finding can
    # carry that category, so a FAIL on it is unbacked and caught here, while a
    # PASS on it changes nothing.
    for gate in sorted(g for g, value in review.gates.items() if value == "FAIL"):
        if not any(f.get("category") == gate for f in review.findings):
            return False, f"gate {gate} FAIL with no attributable finding"

    if touches_ui:
        missing = [g for g in REQUIRED_UI_GATES if g not in review.gates]
        if missing:
            return False, f"UI review missing required gates: {', '.join(missing)}"
    return True, ""


def material_diff_hash(repo: str, number: int) -> str | None:
    result = gh.run(["gh", "pr", "diff", str(number), "--repo", repo])
    if not result.ok:
        return None
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def touches_ui(repo: str, number: int) -> bool:
    result = gh.run(["gh", "pr", "diff", str(number), "--repo", repo, "--name-only"])
    if not result.ok:
        return True  # assume UI when unknown, so the stricter gate applies
    names = result.stdout.splitlines()
    return any(
        name.endswith((".tsx", ".jsx", ".css", ".html"))
        or "/components/" in name
        or "/app/" in name
        for name in names
    )


@dataclass(frozen=True)
class MergeDecision:
    allowed: bool
    reason: str
    invalidate_approval: bool = False


def evaluate_merge(pr: dict, record: dict, required_checks: tuple[str, ...],
                   red_guardrail_active: bool, current_diff_hash: str | None) -> MergeDecision:
    """The six conditions from Protocol v1.0 "Merge execution"."""
    number = pr.get("number")

    if red_guardrail_active:
        return MergeDecision(False, "RED guardrail active")

    if record.get("review_verdict") != REVIEW_PASS:
        return MergeDecision(False, "no current REVIEW_PASS from Codex")

    if not record.get("approval_current", False):
        return MergeDecision(False, "approval is not current")

    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        return MergeDecision(False, f"PR #{number} is not an open, ready pull request")

    reviewed_hash = record.get("reviewed_diff_hash")
    if not reviewed_hash or not current_diff_hash:
        return MergeDecision(False, "material diff could not be verified",
                             invalidate_approval=True)
    if reviewed_hash != current_diff_hash:
        return MergeDecision(False, "material diff changed since review",
                             invalidate_approval=True)

    checks_ok, checks_detail = gh.checks_state(pr, required_checks)
    if not checks_ok:
        return MergeDecision(False, f"required CI not satisfied ({checks_detail})")

    merge_state = (pr.get("mergeStateStatus") or "").upper()
    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        return MergeDecision(False, "branch conflicts with main", invalidate_approval=True)
    if merge_state == "BEHIND":
        return MergeDecision(False, "branch is behind main and needs reconciliation")
    if merge_state in ("DIRTY", "BLOCKED"):
        return MergeDecision(False, f"merge state {merge_state}")

    return MergeDecision(True, "all merge gates satisfied")


def blank_pr_record(number: int, task_id: str, branch: str) -> dict:
    return {
        "number": number,
        "task_id": task_id,
        "branch": branch,
        "review_verdict": None,
        "approval_current": False,
        "reviewed_diff_hash": None,
        "review_cycles": 0,
        "repair_cycles": 0,
        "open_finding_ids": [],
        "last_review_at": None,
        "reconciled": False,
        "merged": False,
    }
