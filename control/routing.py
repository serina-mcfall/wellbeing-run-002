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
from types import MappingProxyType

from . import accessibility_contract, clock, gh, security_contract

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


# --------------------------------------------------------- C-05.3a security
#
# A DISTINCT parser, never an overload of parse_review. The two verdicts
# answer different questions, carry different vocabularies (surfaces vs
# gates) and have different consistency rules, and one function serving
# both would have to guess which contract it was reading.
#
# The verdict and reason tokens are ALIASES of control/security_contract,
# which owns the single string definition of each. gate_evidence and the
# PR-record claim validator need the same vocabulary, and a second copy
# here would be one rename away from silent drift. Behaviour - the
# surfaces, the parser, the consistency rules - stays in this module.
SECURITY_PASS = security_contract.SECURITY_PASS
SECURITY_FAIL = security_contract.SECURITY_FAIL
SECURITY_UNPARSEABLE = security_contract.SECURITY_UNPARSEABLE
SECURITY_VERDICTS = security_contract.SECURITY_VERDICTS

# The canonical security surfaces, in prompts/security.md's own order. A
# test asserts this tuple still matches the prompt, so a thirteenth surface
# cannot be added to one without the other.
SECURITY_SURFACES = (
    "SECRETS", "AUTH_SESSION", "AUTHORIZATION", "SIGN_OUT", "REDIRECTS_URLS",
    "XSS_UNSAFE_HTML", "INJECTION_PATH_COMMAND", "ERROR_LEAKAGE",
    "RLS_LEAST_PRIVILEGE", "SERVER_CLIENT_BOUNDARIES", "DEPENDENCY_RISK",
    "AI_INPUT_OUTPUT_HANDLING",
)

# A surface is graded or it is not relevant. Anything else fails closed
# rather than slipping past as an accidental fourth state.
SURFACE_VALUES = frozenset({"PASS", "FAIL", "NOT_RELEVANT"})

# Finite inconsistency reasons - aliases, defined once in
# security_contract. PASS_WITH_FINDINGS deliberately does not exist there:
# governance decision C-05b (2026-09-30) settled that P2/P3 are
# non-blocking security debt which MAY accompany a SECURITY_PASS.
OUTPUT_UNPARSEABLE = security_contract.OUTPUT_UNPARSEABLE
VERDICT_UNRECOGNISED = security_contract.VERDICT_UNRECOGNISED
SURFACES_INCOMPLETE = security_contract.SURFACES_INCOMPLETE
FINDING_FIELDS_INVALID = security_contract.FINDING_FIELDS_INVALID
PASS_WITH_BLOCKING_FINDINGS = security_contract.PASS_WITH_BLOCKING_FINDINGS
FAIL_WITHOUT_BLOCKING_FINDINGS = security_contract.FAIL_WITHOUT_BLOCKING_FINDINGS


@dataclass
class SecurityReview:
    """One parsed security review, faithfully - not yet adjudicated.

    `verdict` is whatever the reviewer actually emitted, uppercased: a
    recognised token, an unrecognised one, or SECURITY_UNPARSEABLE when no
    block carrying a verdict could be read at all. Deciding whether the
    contract is coherent is security_is_consistent's job, not this shape's.

    `raw_excerpt` is IN-MEMORY DIAGNOSTICS ONLY, mirroring Review. It is a
    tail of reviewer prose and is deliberately absent from as_dict(), so
    nothing durable can pick it up by accident - C-05.3a persists finite,
    scrubbed, bounded fields and never reviewer evidence prose.
    """
    verdict: str
    findings: list[dict] = field(default_factory=list)
    surfaces: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    raw_excerpt: str = ""
    # A structural defect found while parsing, as one finite code, or "".
    # RECORDED rather than repaired: a container of the wrong type, or an
    # entry that is not an object, cannot be normalised into a finding, and
    # quietly removing it would turn malformed output into a clean review -
    # drop the one finding that will not validate and the review carrying
    # it looks like a pass. The defect travels here so
    # security_is_consistent can refuse the whole review.
    structure: str = ""

    @property
    def blocking(self) -> list[dict]:
        """P0/P1 - the findings that require remediation."""
        return [f for f in self.findings if f.get("severity") in BLOCKING_SEVERITIES]

    @property
    def debt(self) -> list[dict]:
        """P2/P3 - recorded as non-blocking security debt, never routed as
        blocking. A finding with a missing or unrecognised severity is in
        NEITHER list: it is invalid, and security_is_consistent refuses the
        whole review rather than letting it fall quietly into debt."""
        return [f for f in self.findings
                if f.get("severity") in ("P2", "P3")]

    def as_dict(self) -> dict:
        """Finite and bounded, for a ledger event. No raw_excerpt, no
        reviewer evidence prose, no unbounded free text."""
        return {
            "verdict": self.verdict,
            "surfaces": dict(self.surfaces),
            "summary": self.summary[:500],
            "finding_ids": [f.get("id") for f in self.findings],
            "counts": {s: sum(1 for f in self.findings
                              if f.get("severity") == s) for s in SEVERITIES},
        }


def parse_security(text: str) -> SecurityReview:
    """Extract the security reviewer's machine-readable verdict block.

    Faithful, not adjudicating. Findings are returned EXACTLY as the
    reviewer sent them, normalised in case only - a malformed finding is
    never dropped to make the review look coherent, because dropping it
    would turn an invalid review into a passing one.

    Fenced blocks are scanned in REVERSE, the established convention: the
    prompt asks for the block as the last thing emitted, so the final one
    is the reviewer's answer. Unlike parse_review, a block whose verdict
    token is unrecognised is NOT skipped in favour of an earlier block -
    it is returned with that raw token, so an unrecognised verdict and
    unreadable output stay distinguishable finite conditions, and a
    malformed final word is never silently superseded by a superseded one.

    A missing severity is NEVER defaulted. An absent field stays absent so
    it reaches security_is_consistent and fails closed there; defaulting it
    would silently make an unlabelled finding non-blocking, which is the
    exact defect parse_review documents having fixed.
    """
    excerpt = (text or "")[-1500:]
    for raw in reversed(_BLOCK.findall(text or "")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "verdict" not in data:
            continue

        # The findings CONTAINER, before any entry in it. security.md asks
        # for "exactly this shape", and that shape always carries a findings
        # array - so an absent key, or one holding anything but a list, is a
        # structural defect rather than "no findings". Reading a missing
        # array as empty would let output that never reported its findings
        # adjudicate as though it had none.
        raw_findings = data.get("findings")
        findings: list[dict] = []
        structure = ""
        if not isinstance(raw_findings, list):
            structure = FINDING_FIELDS_INVALID
        else:
            for index, item in enumerate(raw_findings, start=1):
                if not isinstance(item, dict):
                    # Recorded, NOT skipped. Dropping it here is precisely
                    # how a malformed review becomes a clean pass.
                    structure = FINDING_FIELDS_INVALID
                    continue
                item.setdefault("id", f"S{index}")
                # Only keys the reviewer actually sent are rewritten, so a
                # consumer calling .get(field, "") keeps its own default and
                # an absent field survives to the consistency check.
                for key in ("severity", "surface"):
                    if key in item:
                        item[key] = _normalised(item[key])
                findings.append(item)

        # The surfaces CONTAINER. A truthy non-mapping - a list, a string -
        # has no .items(), and calling it would raise provider-shaped output
        # straight out of the parser and into the control plane. Anything
        # that is not a mapping simply yields no graded surfaces, which
        # fails closed as SURFACES_INCOMPLETE.
        raw_surfaces = data.get("surfaces")
        surfaces = ({str(k).upper(): str(v).upper()
                     for k, v in raw_surfaces.items()}
                    if isinstance(raw_surfaces, dict) else {})

        return SecurityReview(_normalised(data["verdict"]), findings, surfaces,
                              str(data.get("summary", "")), excerpt, structure)
    return SecurityReview(SECURITY_UNPARSEABLE, raw_excerpt=excerpt)


def security_is_consistent(review: SecurityReview) -> tuple[bool, str]:
    """Whether a parsed security review is a coherent C-05b contract.

    (True, "") or (False, one finite reason). Checked in a fixed
    precedence so one review always yields one deterministic reason:
    unreadable, then unrecognised verdict, then surfaces, then finding
    fields, then the verdict-vs-findings rules.

    C-05b: SECURITY_PASS needs all twelve surfaces valid and zero P0/P1,
    and MAY carry P2/P3 as non-blocking debt. SECURITY_FAIL needs all
    twelve surfaces valid and at least one P0/P1, and may also carry
    P2/P3. Neither may carry a finding whose severity or surface is
    missing or unrecognised - that invalidates the whole review rather
    than being read as a lesser finding, exactly as the prompt now states.
    """
    if review.verdict == SECURITY_UNPARSEABLE:
        return False, OUTPUT_UNPARSEABLE
    if review.verdict not in (SECURITY_PASS, SECURITY_FAIL):
        return False, VERDICT_UNRECOGNISED

    for surface in SECURITY_SURFACES:
        if review.surfaces.get(surface) not in SURFACE_VALUES:
            return False, SURFACES_INCOMPLETE

    # A structural defect the parser recorded rather than repaired: the
    # findings container was not a list, or an entry in it was not an
    # object. Neither can be normalised, and neither may be ignored.
    if review.structure:
        return False, review.structure

    for finding in review.findings:
        if finding.get("severity") not in SEVERITIES:
            return False, FINDING_FIELDS_INVALID
        if finding.get("surface") not in SECURITY_SURFACES:
            return False, FINDING_FIELDS_INVALID

    blocking = review.blocking
    if review.verdict == SECURITY_PASS and blocking:
        return False, PASS_WITH_BLOCKING_FINDINGS
    if review.verdict == SECURITY_FAIL and not blocking:
        return False, FAIL_WITHOUT_BLOCKING_FINDINGS
    return True, ""


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


# A finite condition vocabulary for the merge gate, so MERGE_BLOCKED carries a
# machine-readable fact and not only prose. Two refusals that read similarly in
# a sentence - a closed pull request and a draft one - are different conditions
# needing different human actions, and a reader of the ledger must be able to
# tell them apart without parsing English.
MERGE_OK = "MERGE_OK"
MERGE_GUARDRAIL_RED = "GUARDRAIL_RED"
MERGE_NO_REVIEW_PASS = "NO_REVIEW_PASS"
MERGE_APPROVAL_STALE = "APPROVAL_STALE"
MERGE_PR_NOT_OPEN = "PR_NOT_OPEN"
MERGE_PR_IS_DRAFT = "PR_IS_DRAFT"
MERGE_HEAD_UNVERIFIABLE = "HEAD_SHA_UNVERIFIABLE"
MERGE_HEAD_CHANGED = "HEAD_SHA_CHANGED"
MERGE_DIFF_UNVERIFIABLE = "DIFF_UNVERIFIABLE"
MERGE_DIFF_CHANGED = "DIFF_CHANGED"
MERGE_CI_NOT_SATISFIED = "CI_NOT_SATISFIED"
MERGE_CONFLICTING = "BRANCH_CONFLICTING"
MERGE_BEHIND = "BRANCH_BEHIND"
MERGE_STATE_BLOCKED = "MERGE_STATE_BLOCKED"


@dataclass(frozen=True)
class MergeDecision:
    allowed: bool
    reason: str
    invalidate_approval: bool = False
    # One of the MERGE_* codes above. Prose is for humans; this is what the
    # ledger and any report should key on.
    condition: str = ""


def evaluate_merge(pr: dict, record: dict, required_checks: tuple[str, ...],
                   red_guardrail_active: bool, current_diff_hash: str | None) -> MergeDecision:
    """The six conditions from Protocol v1.0 "Merge execution"."""
    number = pr.get("number")

    if red_guardrail_active:
        return MergeDecision(False, "RED guardrail active", condition=MERGE_GUARDRAIL_RED)

    if record.get("review_verdict") != REVIEW_PASS:
        return MergeDecision(False, "no current REVIEW_PASS from Codex",
                             condition=MERGE_NO_REVIEW_PASS)

    if not record.get("approval_current", False):
        return MergeDecision(False, "approval is not current",
                             condition=MERGE_APPROVAL_STALE)

    # Closed and draft are two different facts needing two different human
    # actions. They used to share one sentence, so the ledger could not tell a
    # pull request somebody closed from one nobody has taken out of draft - a
    # draft could therefore stall a task silently. Taking a pull request out of
    # draft is deliberately NOT done here: nothing in
    # protocol/RUN-002-PROTOCOL-v2.0.md governs draft status, so automating it
    # would amend a frozen specification by implication. See handover section 34.
    if pr.get("state") != "OPEN":
        return MergeDecision(False, f"PR #{number} is not open",
                             condition=MERGE_PR_NOT_OPEN)
    if pr.get("isDraft"):
        return MergeDecision(
            False,
            f"PR #{number} is a draft and has not been marked ready for review",
            condition=MERGE_PR_IS_DRAFT)

    # Protocol v2 "Evidence provenance": "new SHA => regenerate required
    # automated evidence". The head SHA is the thing the rule names, and
    # reviewed_head is already recorded at dispatch
    # (supervisor.dispatch_reviewer) - it was simply never compared here. The
    # diff-hash check below is NOT a substitute: a force-push can produce a
    # byte-identical diff from a different commit, which leaves the hash equal
    # while every piece of SHA-bound evidence - CI runs, accessibility and
    # security evidence, the review itself - belongs to a commit that is no
    # longer the head. Both checks stay; they catch different things.
    #
    # Absence is a denial, not an exemption. A record or observation that
    # carries no head cannot establish that the reviewed commit is the current
    # one, and "nothing to compare" must never read as "nothing changed".
    reviewed_head = record.get("reviewed_head")
    observed_head = pr.get("headRefOid")
    if not reviewed_head or not observed_head:
        return MergeDecision(False, "reviewed head SHA could not be verified",
                             invalidate_approval=True,
                             condition=MERGE_HEAD_UNVERIFIABLE)
    if reviewed_head != observed_head:
        return MergeDecision(False, "head SHA changed since review",
                             invalidate_approval=True,
                             condition=MERGE_HEAD_CHANGED)

    reviewed_hash = record.get("reviewed_diff_hash")
    if not reviewed_hash or not current_diff_hash:
        return MergeDecision(False, "material diff could not be verified",
                             invalidate_approval=True,
                             condition=MERGE_DIFF_UNVERIFIABLE)
    if reviewed_hash != current_diff_hash:
        return MergeDecision(False, "material diff changed since review",
                             invalidate_approval=True,
                             condition=MERGE_DIFF_CHANGED)

    checks_ok, checks_detail = gh.checks_state(pr, required_checks)
    if not checks_ok:
        return MergeDecision(False, f"required CI not satisfied ({checks_detail})",
                             condition=MERGE_CI_NOT_SATISFIED)

    merge_state = (pr.get("mergeStateStatus") or "").upper()
    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        return MergeDecision(False, "branch conflicts with main",
                             invalidate_approval=True, condition=MERGE_CONFLICTING)
    if merge_state == "BEHIND":
        return MergeDecision(False, "branch is behind main and needs reconciliation",
                             condition=MERGE_BEHIND)
    if merge_state in ("DIRTY", "BLOCKED"):
        return MergeDecision(False, f"merge state {merge_state}",
                             condition=MERGE_STATE_BLOCKED)

    return MergeDecision(True, "all merge gates satisfied", condition=MERGE_OK)


def blank_pr_record(number: int, task_id: str, branch: str) -> dict:
    return {
        "number": number,
        "task_id": task_id,
        "branch": branch,
        "review_verdict": None,
        "approval_current": False,
        # The head the reviewer was actually dispatched against. Written by
        # supervisor.dispatch_reviewer and compared at merge time by
        # evaluate_merge. Declared here as explicit absence, like the other
        # optional fields, so a record that never reached review carries "no
        # reviewed head" rather than no key at all.
        "reviewed_head": None,
        "reviewed_diff_hash": None,
        "review_cycles": 0,
        "repair_cycles": 0,
        "open_finding_ids": [],
        "last_review_at": None,
        "reconciled": False,
        "merged": False,
        # C-05.3a: the security-evidence claim for the CURRENT head, or
        # None for "no claim". Explicit absence, matching this module's
        # other optional fields. A record written before C-05.3a simply
        # lacks the key, which is equally "no claim" - readers use
        # .get("security_evidence"), never [].
        "security_evidence": None,
    }


# ----------------------------------------------- C-18 stage 1: PR observation
#
# route_prs used to call gh.pr_view itself, while the Supervisor held its
# exclusive state transaction, so a slow or hanging GitHub call stalled every
# task in the run. C-18's remediation is observe-then-route: the call is made
# with no lock held and its answer crosses INTO the transaction as an
# immutable value.


def _frozen_json(value):
    """A recursively read-only view of a decoded-JSON value."""
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_json(v) for v in value)
    return value


@dataclass(frozen=True)
class ClosedPrObservation:
    """One `gh pr view`, taken before the state lock, for one pull request.

    `observed` False means GitHub was asked and answered nothing - the same
    "could not be observed" that merge_invariant already classifies, and the
    same value route_prs used to get from a failed call of its own. It is NOT
    the same as having no observation at all: a pull request with no entry in
    a tick's observation map was never asked about, and route_prs defers it
    rather than feeding the classifier a fact nobody established.

    Validated on construction, so an observation that cannot be bound to the
    pull request it would be routed onto never reaches the transaction:

      * the view must carry the number it was requested for, so one PR's
        answer can never be applied to another PR's record;
      * headRefOid, when present, must be a full 40-character SHA - a
        truncated or malformed head is an inconsistent observation, not a
        head.

    The view is frozen because it crosses into a transaction, and a value the
    transaction can mutate is not an observation.

    Within-tick only, and deliberately not durable. It records the head that
    WAS observed, never proof of the head that is current now - every action
    that turns on the current remote head (execute_merges) re-observes it for
    itself.
    """

    pr_number: int
    observed_at: str
    view: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.pr_number, int) or isinstance(self.pr_number, bool) \
                or self.pr_number <= 0:
            raise ValueError("pr_number must be a positive int")
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("observed_at must be a non-empty string")
        if self.view is None:
            return
        if not isinstance(self.view, dict):
            raise ValueError("view must be a decoded JSON object or None")
        number = self.view.get("number")
        if isinstance(number, bool) or number != self.pr_number:
            raise ValueError(
                f"view is for pull request {number!r}, not {self.pr_number}")
        head = self.view.get("headRefOid")
        # _CLAIM_SHA_RE is this module's one full-SHA shape; it is defined
        # below and resolved when this runs, not when the class is built.
        if head is not None and not (isinstance(head, str)
                                     and _CLAIM_SHA_RE.match(head)):
            raise ValueError("headRefOid must be a full 40-character hex SHA")
        object.__setattr__(self, "view", _frozen_json(self.view))

    @property
    def observed(self) -> bool:
        """Whether GitHub answered at all."""
        return self.view is not None

    @property
    def head_sha(self) -> str | None:
        """The head this observation was taken at - not the current head."""
        return (self.view or {}).get("headRefOid")


# ------------------------------------------------- C-05.3a security claim
#
# The claim lives on the PR record and is written ONLY inside the
# Supervisor's state-only transaction. Nothing here allocates, increments
# or searches for an ordinal, touches the filesystem, calls GitHub, spawns
# anything or infers a SHA: the ordinal arrives already chosen under the
# state lock, and this turns it into a canonical record.
SECURITY_CLAIM_STATES = frozenset({"PLANNED", "SPAWNED", "COMPLETE"})
SECURITY_CLAIM_KEYS = frozenset({
    "sha", "ordinal", "attempt_id", "worker", "claim_state",
    "claimed_at", "lease_expires_at", "verdict", "reason",
})

# Both attempt namespaces are four digits wide (gate_evidence's
# SECURITY_ATTEMPT_RE and allocate_attempt alike), so 9999 is the last
# ordinal that can be NAMED. An ordinal past it must fail here rather than
# mint a five-digit id that security_attempt_dir would later refuse,
# stranding the claim with nowhere to write.
SECURITY_ORDINAL_MAX = 9999
SECURITY_ATTEMPT_FMT = "security-attempt-{:04d}"

# The narrowest rule the repository's own behaviour justifies. Every name
# supervisor.py generates - "<task-id lowercased>-builder",
# "-review-<n>", "-fixer-<n>" - satisfies it, and the longest this work can
# produce is "task-001-security-9999" at 22 characters. What it rules out
# is what matters: a name that is not a safe single path component. Worker
# names become <worker>.job.json, .status.json, .out, .last.txt and
# .entry.log inside WORKER_LOG_DIR, and are recovered by stripping those
# suffixes, so a separator, a parent reference or a leading dot would
# escape the directory or break the round trip.
SECURITY_WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CLAIM_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _canonical_moment(value):
    """The parsed datetime if `value` is a canonical Run 002 timestamp,
    else None.

    Three separate conditions, and the third is not implied by the others.
    Round-tripping through clock.iso/clock.parse rejects microseconds, a
    'Z' suffix, a space separator and minute precision - but a NAIVE
    timestamp round-trips perfectly well. An offset-less value would then
    reach the ordering comparison and raise TypeError rather than be
    refused, so tz-awareness is checked explicitly.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = clock.parse(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed if clock.iso(parsed) == value else None


def _valid_ordinal(value) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= SECURITY_ORDINAL_MAX)


# The worker name carries the SHA, and it has to.
#
# Every worker artefact in WORKER_LOG_DIR is keyed on this name -
# <worker>.job.json, .status.json, .out, .last.txt, .entry.log - and nothing
# in the dispatch path clears a stale one. The ordinal restarts at 1 for a new
# head (a prior SHA's evidence never carries forward), so a name built from
# task and ordinal alone REPEATS across a head change, and the second attempt
# then reads the first attempt's status and output. That was reproduced:
# SHA-A's SECURITY_FAIL, naming a file that exists only in SHA-A, published as
# SHA-B's durable outcome and ingested to FIX_REQUIRED, with no SHA-B review
# ever having run.
#
# Putting the SHA in the name makes that unreachable by construction rather
# than by comparison: there is no shared filename left to disagree about.
#
# The WHOLE sha, not a prefix. A twelve-character prefix is not globally
# unique, and a shortened one reintroduces the defect exactly: the attempt
# DIRECTORY is keyed by the full sha and so stays distinct, while the worker
# NAME collides - and the per-attempt provenance record cannot catch it,
# because each attempt's provenance truthfully names its own full sha. The
# gate compares provenance to the claim, never provenance to the artefact, so
# two commits sharing twelve characters put one commit's SECURITY_FAIL into
# the other's durable outcome. Reproduced before this was widened.
#
# Probability is not a correctness argument, so the name is made as unique as
# the directory it addresses. The longest this can produce is
# "<task>-security-" + 40 + "-9999"; for this run's eight-character task ids
# that is 63, inside SECURITY_WORKER_RE's 64. A task id long enough to
# overflow makes security_claim raise, which surfaces as CLAIM_REFUSED - a
# visible, fail-closed refusal rather than a name that cannot be trusted.
SECURITY_NAME_OVERHEAD = len("-security-") + 40 + len("-9999")
SECURITY_TASK_ID_MAX = 64 - SECURITY_NAME_OVERHEAD
_PLAN_ATTEMPT_RE = re.compile(r"^security-attempt-[0-9]{4}$")


def security_worker_name(task_id: str, sha: str, ordinal: int) -> str:
    """The one derivation. Used when minting a claim and when checking one."""
    return f"{task_id.lower()}-security-{sha}-{ordinal:04d}"


@dataclass(frozen=True)
class SecurityPlan:
    """One claimed attempt, as it crosses OUT of the state transaction.

    Frozen, and carrying only immutable scalars. T1 decides what to run;
    Phase C runs it after the lock is released, by which time the document
    T1 read is stale - so nothing here may be a reference into it. Handing
    out the task or PR dict would let Phase C read a value that has since
    changed, or worse mutate one outside the lock. That is the C-14.1 rule
    route_prs already follows, made explicit instead of implied by a tuple.

    task_title travels because the security prompt needs it. Passing the task
    id in its place - which is what the first implementation did - renders
    "Task: TASK-001" into the prompt and denies the reviewer the one sentence
    saying what the task was for.

    Validated on construction: every field is checked here, so an invalid
    plan cannot reach Phase C and become a half-made attempt.
    """
    task_id: str
    task_title: str
    pr: int
    sha: str
    attempt_id: str
    worker: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.task_title, str) or not self.task_title:
            raise ValueError("task_title must be a non-empty string")
        if not isinstance(self.pr, int) or isinstance(self.pr, bool) or self.pr <= 0:
            raise ValueError("pr must be a positive int")
        if not isinstance(self.sha, str) or not _CLAIM_SHA_RE.match(self.sha):
            raise ValueError("sha must be a full 40-character lowercase-hex git SHA")
        if not isinstance(self.attempt_id, str) or \
                not _PLAN_ATTEMPT_RE.match(self.attempt_id):
            raise ValueError("attempt_id must be exactly security-attempt-NNNN")
        if not isinstance(self.worker, str) or \
                not SECURITY_WORKER_RE.match(self.worker):
            raise ValueError("worker must be a safe single path component")
        # The worker name is not accepted, it is re-derived and compared. A
        # plan whose name disagreed with its own identity would address
        # another attempt's files.
        ordinal = int(self.attempt_id.rsplit("-", 1)[-1])
        if self.worker != security_worker_name(self.task_id, self.sha, ordinal):
            raise ValueError("worker does not derive from task, sha and ordinal")


def _worker_binding(claim: dict) -> str:
    """How a claim's worker name relates to the claim's own sha and ordinal.

    BOUND    - carries this claim's sha prefix and ordinal: provenance is
               established from the claim alone, no task_id needed.
    LEGACY   - the pre-SHA form "<task>-security-NNNN". Only reachable for a
               record written before this binding existed.
    MISMATCH - neither. The name cannot be trusted to address this attempt's
               artefacts, and nothing may be read under it.
    """
    worker, sha, ordinal = claim["worker"], claim["sha"], claim["ordinal"]
    if worker.endswith(f"-security-{sha}-{ordinal:04d}"):
        return "BOUND"
    # Any earlier form: the pre-SHA name, and the twelve-character-prefix name
    # that a distinct commit could collide with. Both are refused rather than
    # rewritten - see security_claim_is_valid.
    if worker.endswith(f"-security-{ordinal:04d}") or \
            worker.endswith(f"-{sha[:12]}-{ordinal:04d}"):
        return "LEGACY"
    return "MISMATCH"


def security_claim(*, task_id: str, sha: str, ordinal: int,
                   claimed_at: str, lease_expires_at: str) -> dict:
    """One PLANNED security-evidence claim, for the state transaction.

    `ordinal` is ALREADY CHOSEN - allocated under the exclusive state lock
    by the caller. This function never picks one, never increments one and
    never looks at the filesystem to discover one; doing any of those would
    put the choice outside the lock and let two ticks claim the same
    attempt.

    `attempt_id` and `worker` are DERIVED here rather than accepted, so a
    caller cannot supply an attempt_id that disagrees with its ordinal or a
    worker name that is not a safe path component.

    Raises ValueError on any non-canonical input: a claim that state would
    later refuse is worse than no claim, because the attempt would look
    made and never be actionable.
    """
    # task_id is not regex-checked separately: the derived worker name is,
    # and it is the thing that actually has to be safe. Any task_id that
    # would escape a directory or break suffix-stripping produces a worker
    # name SECURITY_WORKER_RE refuses, so a second pattern here would be a
    # duplicate rule to keep in sync for no extra guarantee.
    if not isinstance(task_id, str):
        raise ValueError("task_id must be a string")
    if not isinstance(sha, str) or not _CLAIM_SHA_RE.match(sha):
        raise ValueError("sha must be a full 40-character lowercase-hex git SHA")
    if not _valid_ordinal(ordinal):
        raise ValueError(
            f"ordinal must be an int in 1..{SECURITY_ORDINAL_MAX}")
    claimed = _canonical_moment(claimed_at)
    expires = _canonical_moment(lease_expires_at)
    if claimed is None or expires is None:
        raise ValueError("timestamps must be canonical tz-aware second-precision")
    if expires <= claimed:
        raise ValueError("lease_expires_at must be strictly after claimed_at")
    worker = security_worker_name(task_id, sha, ordinal)
    if not SECURITY_WORKER_RE.match(worker):
        raise ValueError("derived worker name is not a safe path component")
    return {
        "sha": sha,
        "ordinal": ordinal,
        "attempt_id": SECURITY_ATTEMPT_FMT.format(ordinal),
        "worker": worker,
        "claim_state": "PLANNED",
        "claimed_at": claimed_at,
        "lease_expires_at": lease_expires_at,
        "verdict": None,
        "reason": "",
    }


# ---------------------------------------------- C-05.3a restart recovery
#
# What a Supervisor restart should do about a claim it finds. Pure: this
# takes observations someone else gathered and returns one finite token.
# It reads no filesystem, opens no /proc, and calls nothing - so the
# state-only transaction may consult it without performing any external
# work, and every row below is testable without a fixture.
#
# The invariant the whole table exists to protect: a second security
# provider review may launch ONLY when the prior claimed attempt is PROVEN
# not running. Proven means three things at once - the /proc scan
# succeeded, the claimed worker was absent from it, and the governed lease
# has expired. Any one of them missing is "not yet observed", which is not
# the same as "gone", and must not authorise a duplicate review.
SECURITY_RECOVERY_NO_CLAIM = "NO_CLAIM"
SECURITY_RECOVERY_COMPLETE = "COMPLETE"
SECURITY_RECOVERY_NOT_MATERIALIZED = "NOT_MATERIALIZED"
SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED = "MATERIALIZED_NOT_SPAWNED"
SECURITY_RECOVERY_RUNNING = "RUNNING"
SECURITY_RECOVERY_INDETERMINATE = "INDETERMINATE"
SECURITY_RECOVERY_PROVEN_NOT_RUNNING = "PROVEN_NOT_RUNNING"
SECURITY_RECOVERY_UNKNOWN = "UNKNOWN"
# The attempt finished and its answer is still readable, but publication has
# not succeeded yet. Distinct from INDETERMINATE, which means "absent from a
# good scan but the lease still governs" - here the lease is irrelevant,
# because a review that produced an answer must never be re-run merely
# because a WRITE failed. Not dispatchable; the publisher retries every tick.
SECURITY_RECOVERY_PUBLICATION_PENDING = "PUBLICATION_PENDING"

SECURITY_RECOVERY_STATES = frozenset({
    SECURITY_RECOVERY_NO_CLAIM, SECURITY_RECOVERY_COMPLETE,
    SECURITY_RECOVERY_NOT_MATERIALIZED,
    SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED, SECURITY_RECOVERY_RUNNING,
    SECURITY_RECOVERY_INDETERMINATE, SECURITY_RECOVERY_PROVEN_NOT_RUNNING,
    SECURITY_RECOVERY_UNKNOWN, SECURITY_RECOVERY_PUBLICATION_PENDING,
})

# The classifications from which new external work may follow. Everything
# else means wait, ingest, or refuse - an allow-list, so a classification
# added later and not listed here dispatches nothing until someone decides
# it should.
SECURITY_RECOVERY_DISPATCHABLE = frozenset({
    SECURITY_RECOVERY_NO_CLAIM, SECURITY_RECOVERY_NOT_MATERIALIZED,
    SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED,
    SECURITY_RECOVERY_PROVEN_NOT_RUNNING,
})


def security_claim_diagnostic(claim) -> str:
    """One finite reason a persisted claim is unusable, or "" if it is fine.

    Absence is not a fault: a PR record written before C-05.3a has no
    security_evidence key, and an unclaimed one carries None. Everything
    else present gets a finite CLAIM_* diagnostic, so an operator reading
    the ledger can tell WHY evidence is being held rather than only that
    it is.
    """
    if claim is None:
        return ""
    if not isinstance(claim, dict):
        return "CLAIM_NOT_AN_OBJECT"
    ok, why = security_claim_is_valid(claim)
    return "" if ok else why


def security_spawn_uncommitted(claim, observation: "SecurityObservation") -> bool:
    """Is this the crash window between writing the job file and committing
    the spawn?

    The approved recovery table names this condition SPAWN_UNCOMMITTED and
    gives its action as "treat as SPAWNED - fail closed", which is what
    security_recovery_state does: such a claim flows into RUNNING /
    INDETERMINATE / PROVEN_NOT_RUNNING on its own merits, so an expired one
    can still be recovered rather than stranding the task forever.

    The condition stays separately observable here, because folding it into
    the classification would otherwise lose the one thing it tells an
    operator: the Supervisor died between two specific instructions.
    """
    return (isinstance(claim, dict)
            and claim.get("sha") == observation.head_sha
            and claim.get("claim_state") == "PLANNED"
            and observation.attempt_dir_exists
            and observation.job_file_exists)


@dataclass(frozen=True)
class SecurityObservation:
    """Everything the classifier needs, gathered BEFORE the state lock.

    scan_ok is whether proc.worker_entry_processes returned a mapping at
    all. False means /proc could not be enumerated, which proves nothing
    about any worker and must never read as "absent" - the same
    fail-closed rule workers._reserved_job_file_ports already applies.
    """
    head_sha: str
    now: str
    scan_ok: bool
    worker_live: bool
    attempt_dir_exists: bool
    job_file_exists: bool
    outcome_present: bool
    # Terminal, readable provider output exists for THIS attempt but is not
    # published yet. Gathered in Phase A from the same status and output read
    # the publisher uses, so the classifier and the publisher cannot disagree
    # about whether there is anything left to publish. Defaults False so an
    # observation built without it can never claim evidence it did not see.
    output_readable: bool = False


def security_recovery_state(claim, observation: SecurityObservation) -> str:
    """One finite classification for the claim found on this PR record.

    Precedence is fixed so one situation always yields one answer:

    1. No claim, or a claim for a different head - the current head has
       nothing claimed, whatever an older SHA's claim says. A prior SHA's
       evidence never carries forward.
    2. A durable outcome on disk - the attempt finished. Disk is
       authoritative and state is a cache, so this outranks whatever
       claim_state says and is never re-dispatched.
    3. A failed /proc scan - nothing about liveness can be proven, so
       nothing may be dispatched.
    4. PLANNED, by how far materialisation got.
    5. SPAWNED, by liveness and the governed lease.

    A claim whose job file exists is treated as SPAWNED even while
    claim_state still says PLANNED: the job file is written immediately
    before the spawn, so a crash between the two leaves exactly this, and
    reading it as "not spawned" would dispatch a second review over the
    top of a live one.

    Anything unclassified falls through to UNKNOWN, which dispatches
    nothing. A claim_state of COMPLETE with no outcome on disk lands here:
    the cache says finished while the evidence is gone, and neither
    re-dispatching nor trusting it is safe.
    """
    # Absence first, and ONLY absence. A PR record written before C-05.3a
    # has no security_evidence key at all, and a record that has never
    # been claimed carries None - both are legitimately "nothing claimed
    # here", and both may proceed to a fresh claim.
    if claim is None:
        return SECURITY_RECOVERY_NO_CLAIM

    # Anything else present is validated BEFORE its own fields are
    # believed. Reading the SHA first would let malformed evidence - a
    # string, a list, an empty object, a claim whose sha is missing or
    # garbled - answer "not this head" and so be classified as NO_CLAIM,
    # which authorises a fresh ordinal-1 claim. That is the one thing
    # malformed evidence must never do: attempt 1 may already exist on
    # disk with a review running against it, and the whole table exists to
    # stop a second dispatch. So the sha is only trusted once the claim
    # carrying it is known to be one this control plane wrote.
    if not isinstance(claim, dict) or not security_claim_is_valid(claim)[0]:
        return SECURITY_RECOVERY_UNKNOWN

    # A VALID claim for a different head: the approved stale-head rule.
    # The old claim still exists and is intact; it simply says nothing
    # about this commit, and a prior SHA's evidence never carries forward.
    if claim["sha"] != observation.head_sha:
        return SECURITY_RECOVERY_NO_CLAIM
    if observation.outcome_present:
        return SECURITY_RECOVERY_COMPLETE
    if not observation.scan_ok:
        return SECURITY_RECOVERY_UNKNOWN

    claim_state = claim.get("claim_state")
    if claim_state == "PLANNED":
        if not observation.attempt_dir_exists:
            return SECURITY_RECOVERY_NOT_MATERIALIZED
        if not observation.job_file_exists:
            return SECURITY_RECOVERY_MATERIALIZED_NOT_SPAWNED
        claim_state = "SPAWNED"  # job file written: the spawn may have happened

    if claim_state != "SPAWNED":
        return SECURITY_RECOVERY_UNKNOWN
    if observation.worker_live:
        return SECURITY_RECOVERY_RUNNING
    # Ahead of the lease, deliberately. A finished attempt whose answer is
    # still on disk is not a candidate for replacement at ANY point, because
    # the thing that failed is the write, not the review. Letting the lease
    # decide here is what turned a failed publication into a second paid
    # provider review: the worker was absent from a good scan and the lease
    # had run out, so the attempt was abandoned and re-dispatched while its
    # own output sat readable in WORKER_LOG_DIR.
    if observation.output_readable:
        return SECURITY_RECOVERY_PUBLICATION_PENDING
    lease = _canonical_moment(claim.get("lease_expires_at"))
    now = _canonical_moment(observation.now)
    if lease is None or now is None:
        return SECURITY_RECOVERY_UNKNOWN
    if now < lease:
        # Absent from a good scan, but the governed lease has not run out.
        # Absence here is "not yet observed", not "gone".
        return SECURITY_RECOVERY_INDETERMINATE
    return SECURITY_RECOVERY_PROVEN_NOT_RUNNING


def security_claim_is_valid(claim) -> tuple[bool, str]:
    """(True, "") or (False, one finite diagnostic).

    These diagnostics describe a malformed claim RECORD. They are
    deliberately NOT drawn from SECURITY_FAILURE_REASONS, which says why an
    ATTEMPT is not a completed review: a bookkeeping bug is not evidence
    about a security review, and filing it as one would put a control-plane
    mistake into the record as a finding about the product.
    """
    if not isinstance(claim, dict):
        return False, "CLAIM_NOT_AN_OBJECT"
    if set(claim) != SECURITY_CLAIM_KEYS:
        return False, "CLAIM_KEYS_INVALID"
    if not isinstance(claim["sha"], str) or not _CLAIM_SHA_RE.match(claim["sha"]):
        return False, "CLAIM_SHA_INVALID"
    if not _valid_ordinal(claim["ordinal"]):
        return False, "CLAIM_ORDINAL_INVALID"
    if claim["attempt_id"] != SECURITY_ATTEMPT_FMT.format(claim["ordinal"]):
        return False, "CLAIM_ATTEMPT_ID_MISMATCH"
    if not isinstance(claim["worker"], str) or \
            not SECURITY_WORKER_RE.match(claim["worker"]):
        return False, "CLAIM_WORKER_INVALID"
    # The name must address THIS attempt's artefacts and no other's. Both
    # rejections below are deliberately refusals rather than repairs: the
    # worker may be alive under the name the record carries, and rewriting an
    # identity out from under a live process would strand it - unreachable by
    # the control plane, still spending a provider budget, and owned by
    # nothing. A refused claim is classified UNKNOWN, which dispatches
    # nothing and ingests nothing, and its diagnostic reaches the operator in
    # SECURITY_EVIDENCE_HELD.
    binding = _worker_binding(claim)
    if binding == "LEGACY":
        return False, "CLAIM_WORKER_LEGACY_IDENTITY"
    if binding != "BOUND":
        return False, "CLAIM_WORKER_PROVENANCE_MISMATCH"
    # `in` hashes its left operand, so a claim_state holding a dict or a
    # list - both of which durable JSON can carry - raised TypeError out of
    # a function whose entire contract is to return a finite diagnostic. A
    # corrupt PR record crashed the tick instead of failing closed. Failing
    # closed means RETURNING a refusal, never raising one.
    #
    # The guard changes which branch produces the refusal, never whether
    # one is produced: every member of these frozensets is a string, so a
    # non-string could never have been a member. (A `set` value happened to
    # survive already - CPython retries a failed set lookup as a frozenset -
    # which is exactly the kind of accident that makes the hazard easy to
    # miss by testing only one unhashable type.)
    if not isinstance(claim["claim_state"], str) or \
            claim["claim_state"] not in SECURITY_CLAIM_STATES:
        return False, "CLAIM_STATE_INVALID"

    claimed = _canonical_moment(claim["claimed_at"])
    expires = _canonical_moment(claim["lease_expires_at"])
    if claimed is None or expires is None:
        return False, "CLAIM_TIMESTAMP_INVALID"
    if expires <= claimed:
        return False, "CLAIM_LEASE_NOT_AFTER_CLAIM"

    verdict, reason = claim["verdict"], claim["reason"]
    if not isinstance(reason, str):
        return False, "CLAIM_REASON_INVALID"
    if claim["claim_state"] in ("PLANNED", "SPAWNED"):
        # Nothing has been adjudicated yet, so neither field may say
        # anything. A verdict here would assert a result before the review
        # ran; a reason would assert a failure that has not happened.
        if verdict is not None:
            return False, "CLAIM_VERDICT_BEFORE_COMPLETE"
        return (True, "") if reason == "" else (False,
                                                "CLAIM_REASON_BEFORE_COMPLETE")

    # COMPLETE: exactly one of a verdict or a finite reason. Both would be
    # a contradiction; neither would be a completion that says nothing.
    if verdict is not None and reason != "":
        return False, "CLAIM_VERDICT_AND_REASON"
    if verdict is None and reason == "":
        return False, "CLAIM_COMPLETE_WITHOUT_OUTCOME"
    if verdict is not None:
        # The same unhashable hazard as claim_state above, and the same
        # resolution: an unrecognised verdict is refused, never raised.
        return ((True, "") if isinstance(verdict, str)
                and verdict in SECURITY_VERDICTS
                else (False, "CLAIM_VERDICT_UNRECOGNISED"))
    return ((True, "") if reason in security_contract.SECURITY_FAILURE_REASONS
            else (False, "CLAIM_REASON_UNRECOGNISED"))


# ===================================================================
# C-05.3b foundations - accessibility evidence
#
# FOUNDATIONS ONLY. Nothing below is wired into the Supervisor: no
# dispatch path, no state transition, no ingest. These are the pure
# leaves the wiring will stand on, built and proved first so that the
# wiring - which depends on open governance answers (G1, G3, G4, G7) -
# has something tested to call.
#
# The verdict and reason tokens are ALIASES of
# control/accessibility_contract, which owns the single string
# definition of each, for the same reason the security aliases above
# exist. Behaviour stays here; vocabulary stays there.
# ===================================================================

ACCESSIBILITY_PASS = accessibility_contract.ACCESSIBILITY_PASS
ACCESSIBILITY_FAIL = accessibility_contract.ACCESSIBILITY_FAIL
ACCESSIBILITY_UNPARSEABLE = accessibility_contract.ACCESSIBILITY_UNPARSEABLE
ACCESSIBILITY_VERDICTS = accessibility_contract.ACCESSIBILITY_VERDICTS
ACCESSIBILITY_AUTO_PASS = accessibility_contract.ACCESSIBILITY_AUTO_PASS
ACCESSIBILITY_AUTO_FAIL = accessibility_contract.ACCESSIBILITY_AUTO_FAIL

CHECKS_INCOMPLETE = accessibility_contract.CHECKS_INCOMPLETE
RESULT_EMPTY = accessibility_contract.RESULT_EMPTY
RESULT_UNREADABLE = accessibility_contract.RESULT_UNREADABLE
SHA_MISMATCH = accessibility_contract.SHA_MISMATCH

# The nine check_ids apparatus/accessibility/run.js produces, in its own
# emission order (run.js:454-462). A test asserts this tuple still matches
# both run.js and protocol/PR-EVIDENCE-V2.schema.json's accessibilityCheck
# enum, so a tenth machine check cannot be added to one without the other.
AUTOMATED_CHECK_IDS = (
    "RESPONSIVE_375PX",
    "NO_OVERFLOW_CLIPPING_OVERLAP",
    "TOUCH_TARGETS",
    "KEYBOARD_OPERATION",
    "FOCUS_ORDER_VISIBLE_NO_TRAPS",
    "LABELS_AND_TEXT_ERRORS",
    "REDUCED_MOTION_NO_FLASHING_AUTOPLAY",
    "AXE_SCAN",
    "SCREENSHOTS_ARTIFACTS",
)

# The tenth. A human-judgement check, produced by the qualitative
# reviewer and never by the machine. It is named separately rather than
# appended to the tuple above so that no loop over the automated results
# can accidentally expect it, and no qualitative review can be credited
# with having run a browser.
QUALITATIVE_CHECK_IDS = ("COGNITIVE_SENSORY_REVIEW",)

# All ten, the schema's enum. Protocol v2's accessibility gate requires
# every one for the exact PR SHA.
ACCESSIBILITY_CHECK_IDS = AUTOMATED_CHECK_IDS + QUALITATIVE_CHECK_IDS

# A check result is PASS or FAIL. Anything else fails closed rather than
# slipping past as an accidental third state - the same rule
# SURFACE_VALUES enforces for security surfaces.
CHECK_RESULTS = frozenset({"PASS", "FAIL"})


# Which stated product requirement a failing automated check proves unmet.
#
# DELIBERATELY EMPTY, and that is the current correct state rather than an
# omission. Filling it is a policy statement about what each machine check
# proves, it exists nowhere in this repository, and an implementer writing
# it would be writing accessibility policy. It is open governance question
# G6.
#
# The consequence is stated rather than hidden: with no mapping, an
# automated FAIL yields a finding with no unmet_requirement, which
# control/severity.py::apply_severity_policy rates INVALID with
# merge_blocked=True. The gate HOLDS. A task with a failing accessibility
# check never reaches REVIEW and never silently passes. The automated FAIL
# path is inert-but-safe until G6 is answered - the right direction to be
# wrong in, and a test pins it so that filling this map is a deliberate
# act with visible consequences rather than a quiet widening.
#
# Note for whoever answers G6: the registry that landed
# (apparatus/accessibility/requirement-registry.js) uses identifiers of
# the form ACC-DOD-VISIBLE_FOCUS and ACC-COG-PREDICTABLE_NAVIGATION, and
# it is a JavaScript module with no Python counterpart. Both facts
# contradict assumptions the C-05.3b design recorded; see handover
# section 36.
CHECK_REQUIREMENT: dict[str, str] = {}


def accessibility_auto_finding(check_id: str) -> dict:
    """The severity-policy finding one FAILING automated check asserts.

    Shaped for control/severity.py::apply_severity_policy and nothing
    else. `jev_severity` is P1 because severity.py applies a P1 floor to
    every FAILURE anyway (severity.py:104-107) - claiming anything lower
    here would be a claim the policy immediately overrides, and claiming
    P0 would assert a judgement a machine check cannot make.

    `unmet_requirement` is whatever CHECK_REQUIREMENT maps the check to,
    which today is nothing for every check. An absent citation is what
    makes the FAIL path fail closed; see CHECK_REQUIREMENT above. This
    function does NOT substitute a placeholder string, because
    severity.py accepts any nonempty string that is in the registry and a
    plausible-looking placeholder is exactly how an ungoverned mapping
    would become policy by accident.
    """
    return {
        "classification": "FAILURE",
        "jev_severity": "P1",
        "unmet_requirement": CHECK_REQUIREMENT.get(check_id),
        "check_id": check_id,
    }


def normalize_accessibility_auto(checks, head_sha: str) -> tuple[str | None, str]:
    """One automated run's check list, as (verdict, reason).

    Exactly one of the two is meaningful: (verdict, "") when the run is
    usable evidence about `head_sha`, or (None, one finite reason) when it
    is not. Never both, never neither.

    `checks` is the list run.js writes and gate_evidence republishes in the
    durable attempt outcome - objects of {check_id, result, sha,
    artifact_reference}. The whole OUTCOME record is deliberately not
    accepted here: a FAILED outcome already carries its own finite reason
    from gate_evidence, and re-deriving it would be a second opinion about
    something already adjudicated.

    Checked in a fixed precedence so one check list always yields one
    deterministic reason: unusable container, then wrong commit, then
    malformed entry, then completeness, then the results themselves.

    FAIL-CLOSED THROUGHOUT. Anything unrecognised is a reason, never a
    pass. A missing check is CHECKS_INCOMPLETE and not "nothing to
    report": an absent result is the absence of evidence, and reading it
    as a pass is the precise defect this normalisation exists to make
    unreachable.
    """
    if not isinstance(checks, list):
        return None, RESULT_UNREADABLE
    if not checks:
        return None, RESULT_EMPTY
    if not isinstance(head_sha, str) or not _CLAIM_SHA_RE.match(head_sha):
        # An uncheckable head cannot clear any check. Returning a pass
        # here would make every SHA comparison below vacuous.
        return None, SHA_MISMATCH

    seen: dict[str, str] = {}
    for check in checks:
        if not isinstance(check, dict):
            return None, RESULT_UNREADABLE
        check_id = check.get("check_id")
        result = check.get("result")
        # Evidence bound to a different commit is not evidence about this
        # one - gate_evidence._adjudicate's rule, re-applied here because
        # the claim is about the head observed THIS tick, which is not
        # necessarily the sha the attempt was started for.
        if check.get("sha") != head_sha:
            return None, SHA_MISMATCH
        # isinstance BEFORE the frozenset membership test, and not for
        # tidiness: `result in CHECK_RESULTS` hashes its left operand, so
        # a result holding a dict or a list - both of which durable JSON
        # can carry - would raise TypeError out of a function whose whole
        # contract is to return a finite reason. Failing closed means
        # returning one, never raising one.
        if check_id not in AUTOMATED_CHECK_IDS:
            return None, RESULT_UNREADABLE
        if not isinstance(result, str) or result not in CHECK_RESULTS:
            return None, RESULT_UNREADABLE
        # A repeated check_id is refused rather than last-wins: two
        # disagreeing results for one check is unresolved evidence, and
        # letting the later one win would make the order of a JSON array
        # decide a gate.
        if check_id in seen:
            return None, RESULT_UNREADABLE
        seen[check_id] = result

    if len(seen) != len(AUTOMATED_CHECK_IDS):
        return None, CHECKS_INCOMPLETE

    if any(result == "FAIL" for result in seen.values()):
        return ACCESSIBILITY_AUTO_FAIL, ""
    return ACCESSIBILITY_AUTO_PASS, ""


# ----------------------------------------------- the composite REVIEW gate

# The three evidence legs a task in WAITING_EVIDENCE must satisfy before
# it may advance to REVIEW, each with the verdict that leg must carry.
#
# All three, not two: agents/ACCESSIBILITY.md:8 forbids either
# accessibility half standing in for the other, and C-05.3a already
# landed the security half. A leg missing from this tuple is a gate that
# does not exist.
REVIEW_GATE_LEGS = (
    ("security_evidence", SECURITY_PASS),
    ("accessibility_auto", ACCESSIBILITY_AUTO_PASS),
    ("accessibility_review", ACCESSIBILITY_PASS),
)


# Leg key -> the claim validator that leg's claim must additionally
# satisfy. A leg absent from this mapping is checked STRUCTURALLY ONLY,
# which is strictly weaker, and the difference is invisible at the call
# site - so the absence is declared below rather than left to be noticed.
REVIEW_GATE_CLAIM_VALIDATORS = {
    "security_evidence": security_claim_is_valid,
}

# Leg key -> the function that MINTS that leg's claim. Only the first
# exists today. This mapping is what makes the rule above enforceable
# instead of advisory: the moment someone adds an accessibility claim
# builder, test_c05_3b_review_gate's registration invariant goes red
# until that leg's validator is registered in the same commit.
REVIEW_GATE_CLAIM_BUILDERS = {
    "security_evidence": "security_claim",
    "accessibility_auto": "accessibility_auto_claim",
    "accessibility_review": "accessibility_review_claim",
}

# The two legs whose claims do not exist yet, and which therefore have no
# validator to register. Declared explicitly so that "no validator" is a
# stated position with a test behind it rather than an omission nobody
# can see. C05-3a-SESSION-HANDOVER.md section 36.5, D4.
UNVALIDATED_REVIEW_GATE_LEGS = frozenset({
    "accessibility_auto",
    "accessibility_review",
})


def _leg_passes(claim, head_sha: str, expected_verdict: str) -> bool:
    """Whether one evidence leg is a completed pass AT `head_sha`.

    Structural, and deliberately so. The per-class claim validators for
    the two accessibility legs have not been built - they belong with the
    claims themselves - so this checks what can be checked about any leg:
    it is an object, it is COMPLETE, it is bound to this exact head, it
    carries the verdict this leg must carry, and it carries no failure
    reason.

    The `reason == ""` check is not redundant with the verdict check. A
    claim carrying both a verdict and a reason is a contradiction that
    security_claim_is_valid already refuses; for the legs whose validator
    does not exist yet, refusing it here is what stops a completion that
    says two things at once from being read as the one it is convenient
    to believe.
    """
    if not isinstance(claim, dict):
        return False
    if claim.get("claim_state") != "COMPLETE":
        return False
    if claim.get("sha") != head_sha:
        return False
    if claim.get("verdict") != expected_verdict:
        return False
    return claim.get("reason") == ""


def review_gate_fires(record, head_sha: str) -> bool:
    """Whether WAITING_EVIDENCE may advance to REVIEW for this PR record.

    PURE over (record, head_sha). No state is read, no file is touched, no
    claim is mutated, nothing is cleared. True means every evidence leg is
    a completed pass about the head observed THIS tick.

    EVERY LEG IS COMPARED TO `head_sha`, NEVER TO ANOTHER LEG. That is
    strictly stronger than pairwise agreement, which would accept three
    claims that agree perfectly with one another and all describe a commit
    that has since been superseded. It is the same rule plan_security
    enforces on observation.head_sha and gate_evidence._adjudicate
    enforces on the result file: evidence bound to a different commit is
    not evidence about this one.

    The consequence worth naming: A HEAD MOVE NEEDS NO RESET PATH. When
    the head moves, no claim's sha equals the new head, so the gate simply
    does not fire - nothing is cleared, nothing is invalidated, and there
    is no half-cleared record a crash could leave behind. state.py:106-113
    already states the governing rule: a prior SHA's pass never carries
    forward.

    FALSE IS NOT A VERDICT. It means "not all three completed passes at
    this head", which covers absent, stale, PLANNED, RUNNING, failed and
    malformed alike. Deciding what a FAIL routes to, and how long a hold
    may last before escalation, are the caller's and open governance
    question G4's respectively - not this predicate's.
    """
    if not isinstance(record, dict):
        return False
    # An unknown or malformed head can never be matched. Without this an
    # empty head and an empty claim sha would compare equal and every leg
    # would pass vacuously.
    if not isinstance(head_sha, str) or not _CLAIM_SHA_RE.match(head_sha):
        return False
    # Structural checks on all three legs FIRST, then every registered
    # claim validator. The order is kept for defence in depth: no leg's
    # fields reach a validator until they are known to be the scalars that
    # validator expects.
    #
    # It was originally load-bearing for a different reason -
    # security_claim_is_valid raised TypeError on a claim whose verdict was
    # unhashable, and a gate that raises mid-tick is worse than one that
    # holds, because a crash is not a decision. That defect is now FIXED in
    # security_claim_is_valid itself (it returns CLAIM_VERDICT_UNRECOGNISED
    # and CLAIM_STATE_INVALID instead of raising), so this ordering is no
    # longer the only thing standing between durable JSON and a crashed
    # tick. It stays because ordering is free and a validator should still
    # be handed scalars.
    if not all(_leg_passes(record.get(key), head_sha, verdict)
               for key, verdict in REVIEW_GATE_LEGS):
        return False
    # A structurally plausible claim that its own class validator refuses
    # must not clear this gate just because its visible fields read well -
    # a worker name addressing another attempt's artefacts is the case
    # that motivated the security validator in the first place.
    #
    # Iterating the registry rather than naming one validator is what lets
    # an accessibility claim's validator start being enforced here the
    # moment it is registered, with no edit to this function.
    return all(validate(record.get(key))[0]
               for key, validate in REVIEW_GATE_CLAIM_VALIDATORS.items())
