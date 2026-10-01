"""C-04b — the two merge gates, compared condition by condition.

THE SITUATION THIS MEASURES. Run 002 has two merge gates in two languages:

  control/routing.py::evaluate_merge        the gate the Supervisor calls
                                            (supervisor.py, attempt_merge)
  apparatus/pr-evidence/live-gate.js        composes the four adapters, the
                                            requirement registry and the
                                            severity floor; NO production
                                            caller, only the C-04a harness

Handover 43.5(b) recorded that nothing had ever compared their verdicts,
and that they were "not known to agree". This file is that comparison, and
the three denials it added to the Python gate are what the comparison
found.

WHAT IS AUTHORITATIVE, AND THIS IS THE DECISION, NOT A DESCRIPTION.
`evaluate_merge` is the sole authority for whether a Run 002 pull request
merges. `live-gate.js` is an OFFLINE/package-level policy checker for a
submitted PR-evidence package: it is the right tool when the thing being
judged is an untrusted document, which is a different question from the
one the Supervisor asks. They are kept deliberately separate — option
(iii) of the three the handover set out — because option (i), having the
Supervisor execute the JavaScript gate, cannot be specified until C-20a(C)
fixes WHICH REVISION that gate would execute from. That is the operator's
decision and it is still open.

Keeping them separate is only honest if the separation is MEASURED. That
is what the inventories below are: every condition each gate can report,
pinned, so a condition added to either one without a decision about the
other fails this file.

NOTHING WAS WEAKENED. Every change C-04b made to `evaluate_merge` adds a
denial. No denial was removed, no threshold lowered, no policy amended.

C-04c — ONE OF THE "ACCEPTED DIFFERENCES" WAS NOT ACCEPTABLE.
-------------------------------------------------------------
C-04b declined to port live-gate.js's REVIEW_PROVENANCE_* group, on the
reasoning quoted above: that `evaluate_merge` judges "the control plane's
OWN records, written by the Supervisor under its own lock", which is a
different question from judging an untrusted document.

That reasoning described a boundary THAT DOES NOT EXIST YET. Workers are
spawned with no setuid and no container, so they run as the same UID that
owns `.runtime/`, and every field the gate trusts is in a file they can
write. Measured, then reproduced: see ThreatModelCase in
tests/test_c04c_merge_attestation.py, where six field writes turn a pull
request with no review and no evidence into MERGE_OK.

All six provenance conditions are now matched on the Python side by
`routing.ledger_attests_merge`, enforced in `supervisor.attempt_merge`.

The sixth, WORKER_MISMATCH, was recorded in JS_ONLY as unmatchable because
the reviewer's worker name `<task>-review-<cycle>` is not SHA-bound. That
reason was about the NAME and the check does not depend on it: both
REVIEW_DISPATCHED and REVIEW_RESULT carry their own `head_sha`, so the
head binding comes from the events. `routing._review_worker_attests`
requires a dispatch bound to this head and requires every result at this
head to name a worker that dispatch names. The Python gate previously
required NO dispatch event at all, so a lone appended REVIEW_RESULT
attributed to nobody satisfied the whole review leg.

WHAT STAYS TRUE. The reviewer's name is still not SHA-bound, and that
asymmetry with `security_worker_name` and
`accessibility_review_worker_name` is unchanged - it is recorded in
`routing._review_worker_attests` rather than closed here.

WHAT THAT CHANGES, AND WHAT IT DOES NOT. A forged merge now requires
editing state.json AND appending four consistent events to the durable
evidence record. The ledger has no hash chain and the same UID can write
it, so this is TAMPER EVIDENCE, NOT PREVENTION. The gap it leaves is
C-22, an open launch blocker, and it must not be read as closed.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# This directory too, so the shared evidence fixture imports the same way
# under `unittest discover -s tests` and under `python -m unittest
# tests.<module>` — the same reason test_c04_merge_eligibility does it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import config, routing
from mergeable_evidence import complete_evidence  # noqa: E402

LIVE_GATE = config.REPO_ROOT / "apparatus" / "pr-evidence" / "live-gate.js"


# ---------------------------------------------------------------------
# What each gate can say. Pinned, so neither can grow a condition in
# silence while the other stands still.
# ---------------------------------------------------------------------

PYTHON_CONDITIONS = frozenset({
    "MERGE_OK",
    "GUARDRAIL_RED",
    "NO_REVIEW_PASS",
    "APPROVAL_STALE",
    "PR_NOT_OPEN",
    "PR_IS_DRAFT",
    "PR_DRAFT_STATE_UNKNOWN",        # C-04b
    "HEAD_SHA_UNVERIFIABLE",
    "HEAD_SHA_CHANGED",
    "EVIDENCE_INCOMPLETE",
    "DIFF_UNVERIFIABLE",
    "DIFF_CHANGED",
    "CI_NOT_SATISFIED",
    "BRANCH_CONFLICTING",
    "BRANCH_BEHIND",
    "MERGE_STATE_DIRTY",             # C-04b, split out of MERGE_STATE_BLOCKED
    "MERGE_STATE_BLOCKED",
    "MERGE_STATE_UNKNOWN",           # C-04b
    "MERGE_STATE_UNRECOGNISED",      # C-04b
    # C-04c. Enforced in supervisor.attempt_merge immediately BEFORE
    # evaluate_merge rather than inside it, because the gate is pure over
    # its arguments and reading the ledger is I/O that belongs at the call
    # site. They are listed here because this inventory is about what the
    # Python merge PATH can refuse, not about one function's return values
    # - and because leaving them out is exactly how a condition stops
    # being compared against the JavaScript gate.
    "LEDGER_ATTESTATION_MISSING",
    "LEDGER_UNREADABLE",
    "LEDGER_ATTESTATION_CONTRADICTED",
    # The sixth provenance condition, matched after C-04c recorded it as the
    # one that could not be. See SHARED and routing._review_worker_attests.
    "LEDGER_ATTESTATION_WORKER_MISMATCH",
})

JS_CONDITIONS = frozenset({
    "COMPOSITION_INPUT_INVALID",
    "OFFLINE_POLICY_FAILED",
    "HEAD_SHA_UNVERIFIED",
    "EVIDENCE_SHA_MISSING",
    "EVIDENCE_SHA_STALE",
    "EVIDENCE_SHA_UNBOUND",
    "PR_HEAD_UNOBSERVED",
    "PR_HEAD_MOVED",
    "TASK_RECORD_UNVERIFIED",
    "CI_UNVERIFIED",
    "REVIEWER_UNVERIFIED",
    "REVIEW_PROVENANCE_MISSING",
    "REVIEW_PROVENANCE_UNBOUND",
    "REVIEW_PROVENANCE_SHA_MISMATCH",
    "REVIEW_PROVENANCE_WORKER_MISMATCH",
    "REVIEW_PROVENANCE_CONFLICT",
    "REVIEW_PROVENANCE_UNREADABLE",
    "PR_UNOBSERVED",
    "PR_NOT_OPEN",
    "PR_IS_DRAFT",
    "PR_DRAFT_STATE_UNKNOWN",
    "PR_CONFLICTING",
    "PR_BEHIND_BASE",
    "PR_BLOCKED_BY_BRANCH_PROTECTION",
    "PR_MERGE_STATE_NOT_CLEAN",
})

# Policy BOTH gates enforce. These are the ones that must not drift apart.
SHARED = {
    "pull request is open":            ("PR_NOT_OPEN", "PR_NOT_OPEN"),
    "pull request is not a draft":     ("PR_IS_DRAFT", "PR_IS_DRAFT"),
    "draft status is known":           ("PR_DRAFT_STATE_UNKNOWN",
                                        "PR_DRAFT_STATE_UNKNOWN"),
    "branch does not conflict":        ("BRANCH_CONFLICTING", "PR_CONFLICTING"),
    "branch is not behind its base":   ("BRANCH_BEHIND", "PR_BEHIND_BASE"),
    "protection rules are satisfied":  ("MERGE_STATE_BLOCKED",
                                        "PR_BLOCKED_BY_BRANCH_PROTECTION"),
    "mergeability is actually known":  ("MERGE_STATE_UNKNOWN",
                                        "PR_MERGE_STATE_NOT_CLEAN"),
    "the reviewed head is the current head":
                                       ("HEAD_SHA_CHANGED", "PR_HEAD_MOVED"),
    "the head can be established at all":
                                       ("HEAD_SHA_UNVERIFIABLE",
                                        "PR_HEAD_UNOBSERVED"),
    "required evidence is complete and bound to this head":
                                       ("EVIDENCE_INCOMPLETE",
                                        "EVIDENCE_SHA_UNBOUND"),
    "required CI passed for this head":
                                       ("CI_NOT_SATISFIED", "CI_UNVERIFIED"),
    # C-04c. These three were JS_ONLY until the threat model behind that
    # classification was measured and found false — see ThreatModelCase in
    # tests/test_c04c_merge_attestation.py.
    "the durable record independently attests each leg at this head":
                                       ("LEDGER_ATTESTATION_MISSING",
                                        "REVIEW_PROVENANCE_MISSING"),
    "the durable record can be read at all":
                                       ("LEDGER_UNREADABLE",
                                        "REVIEW_PROVENANCE_UNREADABLE"),
    # CORRECTED 2026-10-02. These two codes were paired as one policy and
    # they are two different faults:
    #   LEDGER_ATTESTATION_CONTRADICTED  two events at one head disagree
    #                                    about the OUTCOME
    #   REVIEW_PROVENANCE_CONFLICT       ONE event names two different HEADS
    # Python now refuses the second as well - `_attested_head` returned the
    # first valid spelling, so `{"head_sha": A, "head": B}` silently
    # resolved to A, and it now returns None so the leg goes unattested -
    # but it arrives there through LEDGER_ATTESTATION_MISSING, not through
    # CONTRADICTED. Same refusal, different code, and the pairing is
    # written to say so rather than to imply a code-for-code match.
    "two attestations about one commit do not disagree about the outcome":
                                       ("LEDGER_ATTESTATION_CONTRADICTED",
                                        "REVIEW_PROVENANCE_CONFLICT"),
    "one attestation does not name two different heads":
                                       ("LEDGER_ATTESTATION_MISSING",
                                        "REVIEW_PROVENANCE_CONFLICT"),
    # The sixth, matched after C-04c recorded it as the one that could not
    # be. The reason recorded then was that the reviewer's worker name is
    # not SHA-bound, so "a check over it would be weaker than it looks".
    # That was true about the NAME and false about the CHECK: the head
    # binding does not have to come from the name, because both review
    # events carry their own head_sha. `routing._review_worker_attests`
    # requires a REVIEW_DISPATCHED bound to this head and requires every
    # REVIEW_RESULT at this head to name a worker that dispatch names.
    #
    # "EXACTLY THE COMPARISON live-gate.js MAKES" IS WHAT THIS COMMENT USED
    # TO SAY, AND IT WAS NOT TRUE. An independent review read both sides.
    # The two gates refuse the same ATTACK and they do not make the same
    # comparison, and an inventory whose whole job is to be honest about
    # differences must say which:
    #
    #   * live-gate.js takes the LAST REVIEW_DISPATCHED/REVIEW_RESULT for
    #     (task, pr, role) across the slice it is given. Python takes
    #     MEMBERSHIP over every dispatch at this head. A ledger carrying
    #     dispatch@H2 + pass@H2 and then a later-appended result@H1 is
    #     REVIEW_PROVENANCE_SHA_MISMATCH in JavaScript and attested in
    #     Python, because H1 is simply not this head.
    #   * live-gate.js compares the result's `agent_id` to the worker
    #     `reviewer-identity.js` independently resolves. Python compares it
    #     to the DISPATCH's own `agent_id`. Python's is the weaker
    #     anchoring where an independent resolution exists and the
    #     stronger where it does not.
    #   * live-gate.js filters on `role`; Python does not, and is safe only
    #     because `supervisor.attempt_merge` hands it an inspection already
    #     filtered by task and pull request.
    #
    # Neither rule admits an unreviewed merge, which is why this stays in
    # SHARED. The differences are recorded because the next person to read
    # "matched" should not have to re-derive that it means "refuses the
    # same thing", not "computes the same way".
    #
    # The limit is written into that function's docstring rather than
    # implied here: the name is still not SHA-bound, and the ledger is
    # still writable by the UID that runs the workers, so this is tamper
    # evidence and not prevention. C-22 stays open.
    "the verdict came from a worker dispatched against this head":
                                       ("LEDGER_ATTESTATION_WORKER_MISMATCH",
                                        "REVIEW_PROVENANCE_WORKER_MISMATCH"),
}

# Policy ONLY the Python gate has, with why the JavaScript one does not.
PYTHON_ONLY = {
    "GUARDRAIL_RED":
        "A run-wide RED guardrail. It is a property of the RUN, not of any "
        "pull request or evidence package, so a package-level checker has "
        "nothing to read it from.",
    "APPROVAL_STALE":
        "`record.approval_current`, a control-plane lifecycle flag. The "
        "evidence package has no equivalent and should not: approval "
        "currency is state the Supervisor owns.",
    "DIFF_UNVERIFIABLE":
        "The material-diff hash. It catches a force-push that produces a "
        "DIFFERENT commit with an IDENTICAL diff, which every SHA check "
        "passes. live-gate.js has no diff-hash concept at all.",
    "DIFF_CHANGED": "As DIFF_UNVERIFIABLE.",
    "NO_REVIEW_PASS":
        "Reached through the live reviewer worker's parsed verdict rather "
        "than through a submitted package field.",
    "MERGE_STATE_DIRTY":
        "A split of MERGE_STATE_BLOCKED, not new policy. live-gate.js "
        "collapses DIRTY into PR_MERGE_STATE_NOT_CLEAN.",
    "MERGE_STATE_UNRECOGNISED":
        "A value GitHub has not used yet. live-gate.js reaches the same "
        "denial through requiring CLEAN exactly.",
    "MERGE_OK": "The allow verdict, not a denial.",
}

# Policy ONLY the JavaScript gate has, with why it is NOT ported.
#
# EVERY ENTRY IS A DELIBERATE DECISION RECORDED HERE RATHER THAN A GAP
# NOBODY NOTICED. If one of these should be ported, it is a change of
# decision, and this dict is where the argument it has to beat lives.
JS_ONLY = {
    "COMPOSITION_INPUT_INVALID":
        "Structural validation of a submitted request object. The "
        "Supervisor builds its own arguments; there is no submitted "
        "request to be malformed.",
    "OFFLINE_POLICY_FAILED":
        "JSON-Schema validation of a PR-evidence PACKAGE. The Supervisor "
        "does not consume packages - it consumes claims it wrote itself, "
        "each validated by its own registered claim validator through "
        "review_gate_fires. The policy is enforced; the document it is "
        "enforced against is different.",
    "HEAD_SHA_UNVERIFIED":
        "An INDEPENDENT git resolution of the head, cross-checked against "
        "GitHub's. NOT PORTED, and this is the most substantive of these. "
        "RE-EXAMINED and still not ported, for a reason about the "
        "REQUIREMENT rather than about the two gates having different "
        "inputs. What git-head.js resolves is a LOCAL ref - a registered "
        "worktree's HEAD, or a run-002/ branch ref in this checkout "
        "(apparatus/adapters/git-head.js). Nothing in control/ ever "
        "fetches: `grep -rn 'git fetch' control/` is empty, and every "
        "gh.git call in the control plane is rev-parse, status, "
        "worktree or branch work on refs this repository already has. So "
        "a local ref is not a second observation of the same commit; it "
        "is a ref the control plane never updates. Two consequences, and "
        "the first is disqualifying on its own: `gh.update_branch` "
        "reconciles a BEHIND branch ON GITHUB, producing a head this "
        "checkout has never seen, so an unconditional cross-check would "
        "make every reconciled pull request permanently unmergeable - it "
        "would deny the reconciliation path the gate is required to "
        "allow. And the reviewer's own worktree, the one checkout that "
        "did hold the reviewed commit, is deliberately released at "
        "on_reviewer_finished so review cycles do not accumulate "
        "checkouts, so by merge time it is gone. A second opinion worth "
        "having would have to come from a DIFFERENT remote surface (the "
        "git protocol rather than the REST API), which is a new external "
        "call inside the one transaction C-14.1 requires shortest. "
        "Recorded as a known, accepted difference; the thing that would "
        "change the decision is a control plane that fetches.",
    "EVIDENCE_SHA_MISSING":
        "A package field. review_gate_fires compares EVERY leg's claim sha "
        "to the observed head directly, which is the same rule applied to "
        "the control plane's own records.",
    "EVIDENCE_SHA_STALE": "As EVIDENCE_SHA_MISSING.",
    "TASK_RECORD_UNVERIFIED":
        "Resolving a claimed task_id against config/tasks.json. The "
        "Supervisor reached this record THROUGH the task; there is no "
        "claimed identifier to resolve.",
    "REVIEWER_UNVERIFIED":
        "reviewer-identity.js's own refusal: no REVIEW_RESULT in the "
        "package's ledger slice resolves to a worker whose NAME matches "
        "the <task>-review-<cycle> shape. The reason recorded here before "
        "- that the identity is 'known by construction' because the "
        "Supervisor dispatched the worker under its own lock - is struck: "
        "that is the same sentence C-04c measured and found false, "
        "because a worker can write the state the construction rests on. "
        "The Python side now reaches BOTH of this condition's outcomes "
        "through the ledger instead: no reviewer resolves at this head is "
        "LEDGER_ATTESTATION_MISSING, and a verdict from a worker no "
        "dispatch at this head names is "
        "LEDGER_ATTESTATION_WORKER_MISMATCH. It stays listed here because "
        "the two gates reach it through one code rather than two, not "
        "because the policy is unmatched - and because the Python check "
        "compares against an actual dispatch for the head rather than "
        "against a name SHAPE, which is the stronger comparison.",
    "REVIEW_PROVENANCE_UNBOUND":
        "An attestation that names NO head. The Python side reaches the "
        "same refusal through LEDGER_ATTESTATION_MISSING: an event whose "
        "metadata carries no usable head simply does not match this head, "
        "so the leg is unattested. Same outcome, one fewer code.",
    "REVIEW_PROVENANCE_SHA_MISMATCH":
        "An attestation naming a DIFFERENT head. Same as "
        "REVIEW_PROVENANCE_UNBOUND - it does not match, so the leg is "
        "unattested and LEDGER_ATTESTATION_MISSING fires.",
    "PR_UNOBSERVED":
        "The Supervisor refuses before reaching the gate: execute_merges "
        "logs MERGE_BLOCKED and continues when gh.pr_view returns None, so "
        "evaluate_merge is never handed an absent observation.",
}

# The one place the two gates knowingly DISAGREE on the allow side.
#
# live-gate.js requires mergeStateStatus CLEAN exactly. evaluate_merge also
# allows UNSTABLE and HAS_HOOKS, because it separately verifies every
# REQUIRED check BY NAME through gh.checks_state - so UNSTABLE there means
# only "a check nobody required is red", which live-gate.js cannot
# distinguish because it has no required-check list to compare against.
#
# Recorded here and pinned by MergeStateDifferenceCase, so widening or
# narrowing it is a deliberate edit to a test that explains the trade
# rather than a quiet change of behaviour.
KNOWN_DISAGREEMENT = {
    "mergeStateStatus UNSTABLE": "Python allows, JavaScript denies",
    "mergeStateStatus HAS_HOOKS": "Python allows, JavaScript denies",
}


def _python_conditions_in_source() -> set[str]:
    """Every MERGE_* condition constant routing actually defines."""
    return {value for name, value in vars(routing).items()
            if name.startswith("MERGE_") and isinstance(value, str)}


def _js_conditions_in_source() -> set[str]:
    """Every condition code live-gate.js can actually emit.

    Read out of the source rather than by executing it: this assertion is
    about what the file CONTAINS, and running node to find out would make
    the drift guard depend on a toolchain it does not need.
    """
    text = LIVE_GATE.read_text(encoding="utf-8")
    found = set(re.findall(r"deny\(\s*'([A-Z_]+)'", text))
    found |= set(re.findall(r"code:\s*'([A-Z_]+)'", text))
    return found


class InventoryCase(unittest.TestCase):
    """Neither gate may grow a condition without a decision about the other."""

    def test_the_python_inventory_matches_the_source(self):
        in_source = _python_conditions_in_source()
        self.assertEqual(
            in_source, set(PYTHON_CONDITIONS),
            "routing's MERGE_* constants have changed. Update "
            "PYTHON_CONDITIONS and say in SHARED / PYTHON_ONLY what the new "
            "condition means for the JavaScript gate.")

    def test_the_javascript_inventory_matches_the_source(self):
        in_source = _js_conditions_in_source()
        self.assertEqual(
            in_source, set(JS_CONDITIONS),
            "live-gate.js's condition codes have changed. Update "
            "JS_CONDITIONS and say in SHARED / JS_ONLY what the new "
            "condition means for the Python gate.")

    def test_every_condition_is_classified_exactly_once(self):
        """No condition may sit in no bucket, or in two."""
        shared_py = {py for py, _ in SHARED.values()}
        shared_js = {js for _, js in SHARED.values()}
        self.assertEqual(
            PYTHON_CONDITIONS, shared_py | set(PYTHON_ONLY),
            "a Python condition is in neither SHARED nor PYTHON_ONLY")
        self.assertEqual(
            JS_CONDITIONS, shared_js | set(JS_ONLY),
            "a JavaScript condition is in neither SHARED nor JS_ONLY")
        self.assertFalse(shared_py & set(PYTHON_ONLY))
        self.assertFalse(shared_js & set(JS_ONLY))

    def test_every_unported_javascript_condition_carries_a_reason(self):
        """A gap with no stated reason is a gap nobody decided about."""
        for code, why in JS_ONLY.items():
            self.assertTrue(
                why and len(why) > 20,
                f"{code} is unported with no substantive reason recorded")


# ---------------------------------------------------------------------
# What the comparison actually FOUND. Each of these passed before C-04b.
# ---------------------------------------------------------------------

HEAD = "a" * 40
MOVED = "b" * 40
DIFF = "d" * 40


def a_pr(**over):
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "a" * 40,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "status": "COMPLETED",
                               "conclusion": "SUCCESS"}],
    }
    pr.update(over)
    return pr


def a_record(**over):
    record = routing.blank_pr_record(7, "TASK-001", "task/task-001")
    record.update({
        "review_verdict": routing.REVIEW_PASS,
        "approval_current": True,
        "reviewed_head": "a" * 40,
        "reviewed_diff_hash": DIFF,
    })
    # The three evidence legs, built by the shared fixture so they satisfy
    # the REAL claim validators. A hand-written claim does not: the first
    # draft of this file used one, the baseline denied for
    # EVIDENCE_INCOMPLETE, and every assertion below it was passing for a
    # reason that had nothing to do with what it claimed to test.
    record.update(complete_evidence("a" * 40))
    record.update(over)
    return record


def evaluate(pr=None, record=None):
    return routing.evaluate_merge(
        pr if pr is not None else a_pr(),
        record if record is not None else a_record(),
        ("ci",), False, DIFF)


class BaselineCase(unittest.TestCase):
    """The fixtures must reach the end of the gate, or nothing below means
    anything: a test that denies for an unrelated reason proves nothing
    about the reason it names."""

    def test_the_happy_fixture_is_actually_allowed(self):
        decision = evaluate()
        self.assertTrue(decision.allowed,
                        f"fixture denied for {decision.condition}: "
                        f"{decision.reason}")
        self.assertEqual(decision.condition, routing.MERGE_OK)


class ClosedHolesCase(unittest.TestCase):
    """Three states that returned "all merge gates satisfied" before C-04b."""

    def test_an_absent_merge_state_is_not_a_clean_one(self):
        pr = a_pr()
        del pr["mergeStateStatus"]
        decision = evaluate(pr=pr)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_STATE_UNKNOWN)

    def test_github_still_computing_mergeability_is_not_permission(self):
        decision = evaluate(pr=a_pr(mergeStateStatus="UNKNOWN"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_STATE_UNKNOWN)

    def test_a_merge_state_nobody_has_reasoned_about_denies(self):
        decision = evaluate(pr=a_pr(mergeStateStatus="SOME_FUTURE_STATE"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_STATE_UNRECOGNISED)

    def test_an_absent_draft_flag_is_not_ready_for_review(self):
        pr = a_pr()
        del pr["isDraft"]
        decision = evaluate(pr=pr)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition,
                         routing.MERGE_PR_DRAFT_STATE_UNKNOWN)

    def test_a_non_boolean_draft_flag_is_not_ready_for_review(self):
        for value in (None, "false", 0, [], {}):
            with self.subTest(value=value):
                decision = evaluate(pr=a_pr(isDraft=value))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.condition,
                                 routing.MERGE_PR_DRAFT_STATE_UNKNOWN)

    def test_a_real_draft_still_reports_as_a_draft(self):
        """The new unknown branch must not swallow the draft diagnostic.

        C-20a decision D: a draft is a reportable condition a human can act
        on, and collapsing it into "could not determine" would lose that.
        """
        decision = evaluate(pr=a_pr(isDraft=True))
        self.assertEqual(decision.condition, routing.MERGE_PR_IS_DRAFT)

    def test_dirty_and_blocked_are_now_distinguishable(self):
        dirty = evaluate(pr=a_pr(mergeStateStatus="DIRTY"))
        blocked = evaluate(pr=a_pr(mergeStateStatus="BLOCKED"))
        self.assertFalse(dirty.allowed)
        self.assertFalse(blocked.allowed)
        self.assertEqual(dirty.condition, routing.MERGE_STATE_DIRTY)
        self.assertEqual(blocked.condition, routing.MERGE_STATE_BLOCKED)
        self.assertNotEqual(dirty.condition, blocked.condition)

    def test_no_new_denial_invalidates_an_approval(self):
        """None of these is evidence that the review was wrong.

        Invalidating an approval sends the task back round a review cycle.
        A pull request GitHub has not finished assessing has not had
        anything change about its evidence, and treating it as if it had
        would burn a cycle for an observation delay.
        """
        for pr in (a_pr(mergeStateStatus="UNKNOWN"),
                   a_pr(mergeStateStatus="SOME_FUTURE_STATE"),
                   a_pr(mergeStateStatus="DIRTY"),
                   a_pr(isDraft=None)):
            with self.subTest(state=pr.get("mergeStateStatus"),
                              draft=pr.get("isDraft")):
                self.assertFalse(evaluate(pr=pr).invalidate_approval)


class MergeStateDifferenceCase(unittest.TestCase):
    """The one allow-side difference between the gates, pinned.

    live-gate.js requires CLEAN exactly; this gate also allows UNSTABLE and
    HAS_HOOKS, relying on gh.checks_state to verify every REQUIRED check by
    name. That is a real difference. It is pinned here so that widening it
    further, or narrowing it, is a deliberate edit to a test that explains
    the trade rather than a quiet change of behaviour.
    """

    def test_exactly_three_states_may_proceed(self):
        self.assertEqual(routing.MERGEABLE_STATES,
                         frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"}))

    def test_unstable_proceeds_only_while_required_checks_are_green(self):
        allowed = evaluate(pr=a_pr(mergeStateStatus="UNSTABLE"))
        self.assertTrue(allowed.allowed, allowed.reason)
        # The thing UNSTABLE is allowed to mean is "a check we did not
        # require is red". A REQUIRED check that is red must still deny,
        # and it must deny for the CI reason, not the merge-state one.
        red = evaluate(pr=a_pr(
            mergeStateStatus="UNSTABLE",
            statusCheckRollup=[{"name": "ci", "status": "COMPLETED",
                                "conclusion": "FAILURE"}]))
        self.assertFalse(red.allowed)
        self.assertEqual(red.condition, routing.MERGE_CI_NOT_SATISFIED)

    def test_a_required_check_that_was_never_reported_denies(self):
        decision = evaluate(pr=a_pr(statusCheckRollup=[]))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.condition, routing.MERGE_CI_NOT_SATISFIED)


class OrderingCase(unittest.TestCase):
    """The gate's order is part of its contract.

    Each denial must be reachable with everything before it satisfied,
    otherwise a later check can be dead code that nobody notices.
    """

    def test_evidence_is_checked_before_the_diff_hash(self):
        """Missing evidence must not be reported as a diff problem."""
        decision = routing.evaluate_merge(
            a_pr(), a_record(accessibility_review=None), ("ci",), False, None)
        self.assertEqual(decision.condition, routing.MERGE_EVIDENCE_INCOMPLETE)

    def test_the_head_is_checked_before_the_evidence(self):
        """A moved head is the reason; incomplete evidence is its symptom."""
        decision = evaluate(pr=a_pr(headRefOid="b" * 40))
        self.assertEqual(decision.condition, routing.MERGE_HEAD_CHANGED)
        self.assertTrue(decision.invalidate_approval)

    def test_the_guardrail_outranks_everything(self):
        decision = routing.evaluate_merge(
            a_pr(state="CLOSED"), a_record(review_verdict=None),
            ("ci",), True, None)
        self.assertEqual(decision.condition, routing.MERGE_GUARDRAIL_RED)


if __name__ == "__main__":
    unittest.main()
