"""The QUALITATIVE accessibility half: parse, adjudicate, dispatch, commit.

The second of C-05.3b's two producers. Unlike the automated half this one
IS an agent - it consumes a provider, carries a lease from G1's
`timeouts.accessibility`, and its worker name is SHA-bound.

`AmendedPromptCase` covers C-02a. The FROZEN `prompts/accessibility.md`
used to show `"unmet_requirement": "visible-focus-indicator"` in its
required-output example - the pre-C-02 kebab-case form the landed registry
does NOT contain - so a reviewer following its own prompt produced a
finding the severity policy rated INVALID and the whole review was refused.
Fail-closed and safe, but the qualitative FAIL path could never produce a
usable finding. C-02a amended that one example to `ACC-DOD-VISIBLE_FOCUS`,
the registry entry whose source phrase is "visible focus" - the same
requirement, named canonically.

The fix is in the frozen artefact, NOT in the parser. `parse_accessibility`
still does not translate kebab-case into ACC-DOD-*: inventing that mapping
would be a second source of truth for what a requirement is called, which
is what C-02's registry exists to prevent. Both properties are asserted
below, so neither can be lost later.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control import accessibility_contract as ac  # noqa: E402
from control import config, notify, providers, routing  # noqa: E402
from control import ledger as ledger_mod  # noqa: E402
from control import state as state_mod  # noqa: E402
from control import supervisor as sv_mod  # noqa: E402
from mergeable_evidence import accessibility_auto_leg, security_leg  # noqa: E402

TZ = "Pacific/Auckland"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZoneInfo(TZ))
REAL_ID = "ACC-DOD-VISIBLE_FOCUS"
PROMPT_EXAMPLE_ID = "visible-focus-indicator"


def block(payload) -> str:
    return "noise\n```json\n" + json.dumps(payload) + "\n```\n"


def finding(requirement=REAL_ID, severity="P2", classification="FAILURE"):
    return {"id": "A1", "jev_severity": severity,
            "classification": classification,
            "unmet_requirement": requirement}


class ParseCase(unittest.TestCase):

    def test_a_pass_with_no_findings_is_coherent(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_PASS", "findings": [],
                   "summary": "clean"}))
        self.assertEqual(review.verdict, ac.ACCESSIBILITY_PASS)
        self.assertEqual(routing.accessibility_is_consistent(review), (True, ""))

    def test_a_fail_citing_a_real_requirement_is_coherent(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL", "findings": [finding()]}))
        self.assertEqual(routing.accessibility_is_consistent(review), (True, ""))

    def test_no_block_at_all_is_unparseable(self):
        review = routing.parse_accessibility("the reviewer said nothing useful")
        self.assertEqual(review.verdict, ac.ACCESSIBILITY_UNPARSEABLE)
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.OUTPUT_UNPARSEABLE)

    def test_the_last_block_is_the_answer(self):
        text = (block({"verdict": "ACCESSIBILITY_FAIL", "findings": [finding()]})
                + block({"verdict": "ACCESSIBILITY_PASS", "findings": []}))
        self.assertEqual(routing.parse_accessibility(text).verdict,
                         ac.ACCESSIBILITY_PASS)

    def test_an_unrecognised_verdict_stays_distinguishable_from_unreadable(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_MAYBE", "findings": []}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.VERDICT_UNRECOGNISED)

    def test_a_missing_findings_container_is_structural_not_empty(self):
        # Reading an absent array as "no findings" would let output that
        # never reported its findings adjudicate as though it had none.
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_PASS"}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.FINDING_FIELDS_INVALID)

    def test_a_malformed_finding_is_recorded_not_dropped(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_PASS",
                   "findings": ["not an object"]}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.FINDING_FIELDS_INVALID)

    def test_a_pass_hiding_a_blocking_finding_is_refused(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_PASS",
                   "findings": [finding(severity="P1")]}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.PASS_WITH_BLOCKING_FINDINGS)

    def test_a_fail_with_nothing_blocking_is_refused(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [{"id": "A1", "jev_severity": "P3",
                                 "classification": "NON_FAILURE",
                                 "non_failure_rationale": "nothing is unmet"}]}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.FAIL_WITHOUT_BLOCKING_FINDINGS)

    def test_an_uncited_failure_is_classification_invalid(self):
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [{"id": "A1", "jev_severity": "P1",
                                 "classification": "FAILURE"}]}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.CLASSIFICATION_INVALID)


class AmendedPromptCase(unittest.TestCase):
    """C-02a: the prompt's example now cites a real registry identifier.

    The amendment replaced the single pre-C-02 kebab-case example with
    `ACC-DOD-VISIBLE_FOCUS`, whose source phrase is "visible focus" - the
    same requirement the old example named. These cases assert the
    amendment landed AND that it did not weaken the registry check that
    made the contradiction visible in the first place.
    """

    def prompt_text(self) -> str:
        return (config.REPO_ROOT / "prompts" / "accessibility.md").read_text(
            encoding="utf-8")

    def test_every_identifier_the_prompt_cites_is_in_the_registry(self):
        # Mechanical, not a spot-check: whatever the prompt puts in an
        # `unmet_requirement` position must be a real registry identifier.
        # A future edit that reintroduces ANY unknown form fails here,
        # which is what makes this a drift guard rather than one assertion
        # about one line.
        from control import accessibility_registry
        cited = re.findall(r'"unmet_requirement"\s*:\s*"([^"]+)"',
                           self.prompt_text())
        self.assertTrue(cited, "the prompt must keep a worked example")
        for identifier in cited:
            self.assertIn(identifier, accessibility_registry.REQUIREMENT_IDS)

    def test_the_superseded_kebab_form_is_gone_from_the_prompt(self):
        self.assertNotIn(PROMPT_EXAMPLE_ID, self.prompt_text())

    def test_a_review_following_the_amended_example_is_accepted(self):
        # The point of the amendment: a reviewer that copies its own
        # prompt now produces a usable finding instead of being refused.
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(requirement=REAL_ID)]}))
        self.assertEqual(routing.accessibility_is_consistent(review), (True, ""))

    def test_an_unknown_identifier_is_still_refused(self):
        # The registry check is unchanged. The amendment fixed the
        # citation, not the enforcement.
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(requirement=PROMPT_EXAMPLE_ID)]}))
        self.assertEqual(routing.accessibility_is_consistent(review)[1],
                         ac.CLASSIFICATION_INVALID)

    def test_the_parser_still_does_not_translate_kebab_case(self):
        # C-02a was fixed in the frozen artefact, NOT by teaching the
        # parser a mapping. An alias table here would be a second source
        # of truth for what a requirement is called, which is exactly what
        # C-02's registry exists to prevent.
        review = routing.parse_accessibility(
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(requirement=PROMPT_EXAMPLE_ID)]}))
        self.assertEqual(review.findings[0]["unmet_requirement"],
                         PROMPT_EXAMPLE_ID)


class DispatchCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.sv = sv_mod.Supervisor.__new__(sv_mod.Supervisor)
        self.sv.cfg = SimpleNamespace(
            timezone=TZ, github_repo="o/r", max_security=1,
            max_accessibility_auto=1, max_accessibility_review=1,
            roles={"security": SimpleNamespace(provider="codex", model=None,
                                               effort="medium"),
                   "accessibility": SimpleNamespace(provider="codex",
                                                    model=None,
                                                    effort="medium")},
            extra={"timeouts": dict(config.load().extra["timeouts"])})
        self.sv.tz = TZ
        self.sv.stopping = False
        self.sv.store = state_mod.Store(path=root / "state.json", tz=TZ)
        self.sv.ledger = ledger_mod.Ledger(path=root / "ledger.jsonl", tz=TZ,
                                           experiment_id="run-002")
        self.sv.notifier = mock.Mock(spec=notify.Notifier)
        self.sv.now = mock.Mock(return_value=NOW)
        self.sv.request_intervention = mock.Mock(return_value={"id": "I-1"})
        self.logged = []
        self.sv.log = mock.Mock(
            side_effect=lambda e, **k: self.logged.append((e, k)))

    def doc_with(self, *, head=HEAD, security=True, auto=True):
        doc = state_mod.initial_document("run-002", "v2.0")
        providers.ensure(doc)
        task = state_mod.add_task(doc, "TASK-001", "T", [], "feature", False, TZ)
        task.update({"state": "WAITING_EVIDENCE", "pr": 7,
                     "branch": "run-002/task-001"})
        record = routing.blank_pr_record(7, "TASK-001", "run-002/task-001")
        if security:
            record["security_evidence"] = security_leg(head, "task-001")
        if auto:
            record["accessibility_auto"] = accessibility_auto_leg(head)
        doc["prs"]["7"] = record
        return doc, task, record

    def kinds(self):
        return [e for e, _ in self.logged]

    # ------------------------------------------------------------------

    def test_a_claim_is_minted_and_passes_its_own_validator(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.assertIsNotNone(plan)
        claim = record["accessibility_review"]
        self.assertEqual(routing.accessibility_review_claim_is_valid(claim),
                         (True, ""))
        self.assertTrue(claim["worker"].endswith(f"-a11y-{HEAD}-0001"))

    def test_the_lease_comes_from_g1_not_from_the_browser_deadlines(self):
        doc, task, record = self.doc_with()
        self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        claim = record["accessibility_review"]
        # 1800 + 60 grace, from timeouts.accessibility - not 120 or 300.
        self.assertNotEqual(claim["lease_expires_at"], claim["claimed_at"])

    def test_a_claim_in_flight_is_not_re_planned(self):
        doc, task, _ = self.doc_with()
        self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.assertIsNone(self.sv.plan_accessibility_review(doc, task, 7, HEAD))

    def test_a_frozen_run_plans_nothing(self):
        doc, task, _ = self.doc_with()
        doc["frozen_at"] = "2026-10-01T00:00:00+13:00"
        self.assertIsNone(self.sv.plan_accessibility_review(doc, task, 7, HEAD))
        self.assertIn("ACCESSIBILITY_REVIEW_HELD", self.kinds())

    def test_a_pass_completes_the_gate_and_reaches_review(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_PASS", "findings": []}))
        self.assertEqual(record["accessibility_review"]["verdict"],
                         ac.ACCESSIBILITY_PASS)
        self.assertEqual(task["state"], "REVIEW")

    def test_a_fail_routes_to_fix_required_with_its_blocking_findings(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(severity="P1")]}))
        self.assertEqual(task["state"], "FIX_REQUIRED")
        self.assertEqual(len(record["pending_findings"]), 1)

    def test_an_incoherent_review_is_not_a_fail(self):
        # It is the ABSENCE of a judgement. It must not route to
        # FIX_REQUIRED, or an unreadable reviewer would blame the product.
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(doc, task, 7, HEAD, plan,
                                            "nothing parseable here")
        self.assertEqual(task["state"], "WAITING_EVIDENCE")
        self.assertIsNone(record["accessibility_review"]["verdict"])
        self.assertEqual(record["accessibility_review"]["reason"],
                         ac.OUTPUT_UNPARSEABLE)
        # Nothing was recorded as a finding - an unreadable reviewer must
        # not put words in the product's mouth.
        self.assertFalse(record.get("pending_findings"))

    def test_a_review_citing_an_unknown_identifier_holds_rather_than_fails(self):
        # Still fail-closed after C-02a: an unrecognised citation is the
        # absence of a usable judgement, so the task waits rather than
        # routing to FIX_REQUIRED on a finding nobody can resolve.
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(requirement=PROMPT_EXAMPLE_ID)]}))
        self.assertEqual(record["accessibility_review"]["reason"],
                         ac.CLASSIFICATION_INVALID)
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_a_review_following_the_amended_prompt_reaches_fix_required(self):
        # C-02a's actual payoff, through the real Supervisor: a reviewer
        # copying its own prompt's example now produces a finding that
        # routes the task to repair instead of stalling it.
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_FAIL",
                   "findings": [finding(requirement=REAL_ID)]}))
        self.assertEqual(record["accessibility_review"]["reason"], "")
        self.assertEqual(task["state"], "FIX_REQUIRED")
        self.assertEqual(len(record["pending_findings"]), 1)

    def test_a_result_for_a_superseded_claim_is_discarded(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        record["accessibility_review"]["attempt_id"] = "a11y-attempt-0009"
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_PASS", "findings": []}))
        self.assertIn("ACCESSIBILITY_REVIEW_DISCARDED", self.kinds())

    def test_a_result_for_another_head_is_discarded(self):
        doc, task, _ = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, NEW_HEAD, plan,
            block({"verdict": "ACCESSIBILITY_PASS", "findings": []}))
        self.assertIn("ACCESSIBILITY_REVIEW_DISCARDED", self.kinds())
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_committing_twice_is_idempotent(self):
        doc, task, record = self.doc_with()
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        payload = block({"verdict": "ACCESSIBILITY_FAIL",
                         "findings": [finding(severity="P1")]})
        self.sv.ingest_accessibility_review(doc, task, 7, HEAD, plan, payload)
        first = list(record["pending_findings"])
        self.sv.ingest_accessibility_review(doc, task, 7, HEAD, plan, payload)
        self.assertEqual(record["pending_findings"], first)

    def test_the_gate_needs_all_three_classes(self):
        # Without the security leg, a passing qualitative review is not
        # enough - which is the composite gate doing its job.
        doc, task, _ = self.doc_with(security=False)
        plan = self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        self.sv.ingest_accessibility_review(
            doc, task, 7, HEAD, plan,
            block({"verdict": "ACCESSIBILITY_PASS", "findings": []}))
        self.assertEqual(task["state"], "WAITING_EVIDENCE")

    def test_concurrency_is_bounded_at_the_governed_one(self):
        doc, task, _ = self.doc_with()
        self.sv.plan_accessibility_review(doc, task, 7, HEAD)
        doc["prs"]["8"] = routing.blank_pr_record(8, "TASK-002", "b")
        other = state_mod.add_task(doc, "TASK-002", "T2", [], "feature", False, TZ)
        other.update({"state": "WAITING_EVIDENCE", "pr": 8})
        self.assertIsNone(self.sv.plan_accessibility_review(doc, other, 8, HEAD))


if __name__ == "__main__":
    unittest.main()
