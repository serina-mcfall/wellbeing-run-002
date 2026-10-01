"""C-05.3b foundations: the accessibility vocabulary and the automated
runner's normalisation into it.

FOUNDATIONS ONLY. Nothing here exercises a dispatch path, a state
transition or an ingest, because none of those has been built - they
depend on open governance answers. These tests cover the two leaves that
do not: control/accessibility_contract.py and the pure normalisation in
control/routing.py.

As with test_c05_3_security_contract, string tokens are compared by VALUE
and never by `is`: CPython interns short literals, so an identity
assertion would pass over exactly the duplication it was meant to catch.
Source-level checks prove single definition; the frozensets, being real
re-exported objects, are the one place identity proves what it claims.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_contract as ac  # noqa: E402
from control import gate_evidence, routing, security_contract as sc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "control"

HEAD = "a" * 40
OTHER_HEAD = "b" * 40

VERDICT_TOKENS = ("ACCESSIBILITY_PASS", "ACCESSIBILITY_FAIL",
                  "ACCESSIBILITY_UNPARSEABLE", "ACCESSIBILITY_AUTO_PASS",
                  "ACCESSIBILITY_AUTO_FAIL")

# Defined in accessibility_contract itself.
OWN_REASON_TOKENS = ("CLASSIFICATION_INVALID", "CHECKS_INCOMPLETE",
                     "RESULT_MISSING", "RESULT_UNREADABLE", "RESULT_EMPTY",
                     "SHA_MISMATCH", "PRODUCT_SERVER_UNREADY")

# Aliased from security_contract - the same string means the same thing.
ALIASED_REASON_TOKENS = ("OUTPUT_UNPARSEABLE", "VERDICT_UNRECOGNISED",
                         "FINDING_FIELDS_INVALID",
                         "PASS_WITH_BLOCKING_FINDINGS",
                         "FAIL_WITHOUT_BLOCKING_FINDINGS",
                         "TIMED_OUT", "EXIT_NONZERO", "OUTPUT_MISSING",
                         "PROVIDER_FAILURE", "SPAWN_OR_RUN_INCOMPLETE")

ALL_REASON_TOKENS = OWN_REASON_TOKENS + ALIASED_REASON_TOKENS


def passing_checks(sha=HEAD, results=None):
    """The nine automated checks, all PASS unless `results` overrides."""
    results = results or {}
    return [{"check_id": cid,
             "result": results.get(cid, "PASS"),
             "sha": sha,
             "artifact_reference": "evidence/a11y"}
            for cid in routing.AUTOMATED_CHECK_IDS]


class VocabularyCase(unittest.TestCase):

    def test_every_token_is_its_own_name(self):
        for name in VERDICT_TOKENS + ALL_REASON_TOKENS:
            with self.subTest(token=name):
                self.assertEqual(getattr(ac, name), name)

    def test_the_seventeen_failure_reasons_are_exactly_these(self):
        self.assertEqual(len(ac.ACCESSIBILITY_FAILURE_REASONS), 17)
        self.assertEqual(
            ac.ACCESSIBILITY_FAILURE_REASONS,
            frozenset(getattr(ac, name) for name in ALL_REASON_TOKENS))

    def test_the_automated_subset_is_a_subset_and_omits_judgement_reasons(self):
        self.assertTrue(ac.ACCESSIBILITY_AUTO_FAILURE_REASONS
                        <= ac.ACCESSIBILITY_FAILURE_REASONS)
        # A machine run never makes a judgement, so it can never fail one.
        for token in ("OUTPUT_UNPARSEABLE", "VERDICT_UNRECOGNISED",
                      "CLASSIFICATION_INVALID", "PASS_WITH_BLOCKING_FINDINGS",
                      "FAIL_WITHOUT_BLOCKING_FINDINGS",
                      "FINDING_FIELDS_INVALID", "PROVIDER_FAILURE",
                      "OUTPUT_MISSING"):
            with self.subTest(token=token):
                self.assertNotIn(getattr(ac, token),
                                 ac.ACCESSIBILITY_AUTO_FAILURE_REASONS)

    def test_no_verdict_token_is_a_failure_reason(self):
        # The two vocabularies answer different questions. A record whose
        # reason could also be read as a verdict would be ambiguous.
        for name in VERDICT_TOKENS:
            with self.subTest(token=name):
                self.assertNotIn(getattr(ac, name),
                                 ac.ACCESSIBILITY_FAILURE_REASONS)

    def test_the_verdict_sets_hold_only_real_verdicts(self):
        self.assertEqual(ac.ACCESSIBILITY_VERDICTS,
                         frozenset({ac.ACCESSIBILITY_PASS,
                                    ac.ACCESSIBILITY_FAIL}))
        self.assertEqual(ac.ACCESSIBILITY_AUTO_VERDICTS,
                         frozenset({ac.ACCESSIBILITY_AUTO_PASS,
                                    ac.ACCESSIBILITY_AUTO_FAIL}))
        # UNPARSEABLE is the parser's answer, never a verdict a reviewer
        # emitted, and never a pass.
        self.assertNotIn(ac.ACCESSIBILITY_UNPARSEABLE,
                         ac.ACCESSIBILITY_VERDICTS)

    def test_the_two_verdict_families_do_not_overlap(self):
        # A record carrying only the automated half must never be readable
        # as the qualitative half's answer, or vice versa.
        self.assertEqual(ac.ACCESSIBILITY_VERDICTS
                         & ac.ACCESSIBILITY_AUTO_VERDICTS, frozenset())

    def test_surfaces_incomplete_is_absent(self):
        # prompts/accessibility.md has no surfaces block. Aliasing the name
        # in would make a reader believe a twelve-surface grid exists.
        self.assertFalse(hasattr(ac, "SURFACES_INCOMPLETE"))
        self.assertNotIn(sc.SURFACES_INCOMPLETE,
                         ac.ACCESSIBILITY_FAILURE_REASONS)


class SingleDefinitionCase(unittest.TestCase):
    """Source-level ownership: each literal exists once."""

    @staticmethod
    def _definitions(module_name, token):
        text = (CONTROL / module_name).read_text(encoding="utf-8")
        return re.findall(rf'^{token}\s*=\s*["\']', text, re.MULTILINE)

    def test_accessibility_contract_defines_its_own_tokens_exactly_once(self):
        for token in VERDICT_TOKENS + OWN_REASON_TOKENS:
            with self.subTest(token=token):
                defs = self._definitions("accessibility_contract.py", token)
                self.assertEqual(len(defs), 1, f"{token} defined {len(defs)}x")

    def test_the_shared_tokens_are_aliased_not_restated(self):
        for token in ALIASED_REASON_TOKENS:
            with self.subTest(token=token):
                self.assertEqual(
                    self._definitions("accessibility_contract.py", token), [],
                    f"accessibility_contract restates {token} instead of "
                    f"aliasing security_contract's definition")
                self.assertEqual(getattr(ac, token), getattr(sc, token))

    def test_no_sibling_module_defines_the_literals_independently(self):
        for module_name in ("routing.py", "gate_evidence.py", "state.py",
                            "supervisor.py", "reconcile.py", "watchdog.py"):
            for token in VERDICT_TOKENS + OWN_REASON_TOKENS:
                with self.subTest(module=module_name, token=token):
                    self.assertEqual(
                        self._definitions(module_name, token), [],
                        f"{module_name} defines {token} independently")

    def test_routing_re_exports_rather_than_restates(self):
        for name in ("ACCESSIBILITY_PASS", "ACCESSIBILITY_FAIL",
                     "ACCESSIBILITY_UNPARSEABLE", "ACCESSIBILITY_AUTO_PASS",
                     "ACCESSIBILITY_AUTO_FAIL", "CHECKS_INCOMPLETE",
                     "RESULT_EMPTY", "RESULT_UNREADABLE", "SHA_MISMATCH"):
            with self.subTest(token=name):
                self.assertEqual(getattr(routing, name), getattr(ac, name))
        self.assertIs(routing.ACCESSIBILITY_VERDICTS,
                      ac.ACCESSIBILITY_VERDICTS)

    def test_the_contract_depends_only_on_the_other_contract(self):
        # A vocabulary leaf that imported state, routing or gate_evidence
        # could not be depended on by them. The one permitted import is
        # security_contract, which itself imports nothing.
        text = (CONTROL / "accessibility_contract.py").read_text(encoding="utf-8")
        imports = re.findall(r"^from \. import (.+)$", text, re.MULTILINE)
        self.assertEqual(imports, ["security_contract"])
        self.assertNotIn("import os", text)
        self.assertNotIn("import subprocess", text)


class AdjudicateAgreementCase(unittest.TestCase):
    """gate_evidence._adjudicate's reasons are in this contract's union.

    _adjudicate emits its six reasons as BARE INLINE LITERALS with no
    module owning them (a drift this section records rather than repairs,
    because gate_evidence is owned elsewhere this cycle). Behavioural
    agreement is therefore pinned here instead: the real function is
    driven down each failure path and its answer checked against the
    contract. Rename either side and this goes red.
    """

    def _ctx(self, tmp, sha=HEAD):
        attempt = Path(tmp) / "attempt-0001"
        ctx = gate_evidence.context(attempt, sha, "http://127.0.0.1:3200/")
        ctx.out_dir.mkdir(parents=True)
        return ctx

    @staticmethod
    def _result(ok=True, timed_out=False):
        return {"ok": ok, "timed_out": timed_out, "exit_code": 0 if ok else 1,
                "soft_timeout_exceeded": False, "duration_ms": 1,
                "stdout": "", "stderr": ""}

    def _reason(self, write_result, *, ok=True, timed_out=False):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            write_result(ctx)
            record = gate_evidence._adjudicate(
                ctx, "TASK-001", self._result(ok=ok, timed_out=timed_out))
            return record["status"], record["reason"]

    def test_every_adjudicate_failure_reason_is_in_the_contract(self):
        cases = {
            "TIMED_OUT": (lambda c: None, {"timed_out": True}),
            "EXIT_NONZERO": (lambda c: None, {"ok": False}),
            "RESULT_MISSING": (lambda c: None, {}),
            "RESULT_UNREADABLE": (
                lambda c: c.result_path.write_text("{not json", "utf-8"), {}),
            "RESULT_EMPTY": (
                lambda c: c.result_path.write_text(
                    json.dumps({"checks": []}), "utf-8"), {}),
            "SHA_MISMATCH": (
                lambda c: c.result_path.write_text(
                    json.dumps({"checks": passing_checks(OTHER_HEAD)}),
                    "utf-8"), {}),
        }
        for expected, (write, kwargs) in cases.items():
            with self.subTest(reason=expected):
                status, reason = self._reason(write, **kwargs)
                self.assertEqual(status, gate_evidence.FAILED)
                self.assertEqual(reason, expected)
                self.assertIn(reason, ac.ACCESSIBILITY_AUTO_FAILURE_REASONS)

    def test_a_clean_run_adjudicates_completed_with_no_reason(self):
        status, reason = self._reason(
            lambda c: c.result_path.write_text(
                json.dumps({"checks": passing_checks()}), "utf-8"))
        self.assertEqual(status, gate_evidence.COMPLETED)
        self.assertEqual(reason, "")


class CheckIdCase(unittest.TestCase):
    """The ten check_ids are one vocabulary, shared with the schema."""

    def test_the_ten_match_the_protocol_schema_enum(self):
        schema = json.loads(
            (ROOT / "protocol" / "PR-EVIDENCE-V2.schema.json")
            .read_text(encoding="utf-8"))
        enum = schema["$defs"]["accessibilityCheck"]["properties"]["check_id"]["enum"]
        self.assertEqual(sorted(routing.ACCESSIBILITY_CHECK_IDS), sorted(enum))

    def test_the_nine_match_what_run_js_emits(self):
        text = (ROOT / "apparatus" / "accessibility" / "run.js").read_text(
            encoding="utf-8")
        emitted = re.findall(r"^\s*results\.([A-Z0-9_]+) = await ", text,
                             re.MULTILINE)
        self.assertEqual(emitted, list(routing.AUTOMATED_CHECK_IDS))

    def test_the_qualitative_check_is_not_in_the_automated_set(self):
        # Letting the machine be credited with COGNITIVE_SENSORY_REVIEW is
        # the exact collapse agents/ACCESSIBILITY.md:8 forbids.
        self.assertEqual(
            set(routing.AUTOMATED_CHECK_IDS)
            & set(routing.QUALITATIVE_CHECK_IDS), set())
        self.assertEqual(len(routing.AUTOMATED_CHECK_IDS), 9)
        self.assertEqual(len(routing.ACCESSIBILITY_CHECK_IDS), 10)


class RequirementMapCase(unittest.TestCase):
    """The FAIL path is inert-but-safe until G6 is answered."""

    def test_the_map_is_empty_pending_governance(self):
        self.assertEqual(routing.CHECK_REQUIREMENT, {})

    def test_an_automated_fail_fails_closed_through_the_severity_policy(self):
        # With no mapping there is no citation, so severity.py rates the
        # finding INVALID and blocks the merge. This is the designed
        # direction to be wrong in, and it is pinned so that filling
        # CHECK_REQUIREMENT is a deliberate act with a visible consequence.
        from control import severity
        for check_id in routing.AUTOMATED_CHECK_IDS:
            with self.subTest(check_id=check_id):
                finding = routing.accessibility_auto_finding(check_id)
                self.assertIsNone(finding["unmet_requirement"])
                verdict = severity.apply_severity_policy(
                    finding, known_requirement_ids=["ACC-DOD-VISIBLE_FOCUS"])
                self.assertFalse(verdict["valid"])
                self.assertTrue(verdict["merge_blocked"])

    def test_a_mapped_check_would_clear_the_policy_at_the_p1_floor(self):
        # Proves the STRUCTURE works, without asserting any mapping: the
        # map is passed in, not read from CHECK_REQUIREMENT.
        from control import severity
        finding = dict(routing.accessibility_auto_finding("KEYBOARD_OPERATION"),
                       unmet_requirement="ACC-DOD-KEYBOARD_OPERATION")
        verdict = severity.apply_severity_policy(
            finding, known_requirement_ids=["ACC-DOD-KEYBOARD_OPERATION"])
        self.assertTrue(verdict["valid"])
        self.assertEqual(verdict["severity"], "P1")
        self.assertTrue(verdict["merge_blocked"])


class NormalizeAutoCase(unittest.TestCase):
    """normalize_accessibility_auto: one check list, one deterministic answer."""

    def test_nine_passes_at_the_head_is_an_auto_pass(self):
        self.assertEqual(
            routing.normalize_accessibility_auto(passing_checks(), HEAD),
            (routing.ACCESSIBILITY_AUTO_PASS, ""))

    def test_one_failing_check_is_an_auto_fail(self):
        checks = passing_checks(results={"AXE_SCAN": "FAIL"})
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (routing.ACCESSIBILITY_AUTO_FAIL, ""))

    def test_a_check_bound_to_another_commit_is_refused(self):
        checks = passing_checks()
        checks[3] = dict(checks[3], sha=OTHER_HEAD)
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (None, ac.SHA_MISMATCH))

    def test_a_whole_run_at_another_commit_is_refused(self):
        self.assertEqual(
            routing.normalize_accessibility_auto(passing_checks(OTHER_HEAD),
                                                 HEAD),
            (None, ac.SHA_MISMATCH))

    def test_a_missing_check_is_incomplete_not_a_pass(self):
        checks = [c for c in passing_checks() if c["check_id"] != "AXE_SCAN"]
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (None, ac.CHECKS_INCOMPLETE))

    def test_a_duplicated_check_is_refused_rather_than_last_wins(self):
        checks = passing_checks()
        checks.append(dict(checks[0], result="FAIL"))
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (None, ac.RESULT_UNREADABLE))

    def test_an_unknown_check_id_is_refused(self):
        checks = passing_checks()
        checks[0] = dict(checks[0], check_id="COGNITIVE_SENSORY_REVIEW")
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (None, ac.RESULT_UNREADABLE))

    def test_an_unrecognised_result_value_is_refused(self):
        for value in ("pass", "SKIPPED", "", None, True, 1):
            with self.subTest(value=value):
                checks = passing_checks()
                checks[0] = dict(checks[0], result=value)
                self.assertEqual(
                    routing.normalize_accessibility_auto(checks, HEAD),
                    (None, ac.RESULT_UNREADABLE))

    def test_an_unhashable_result_is_refused_rather_than_raising(self):
        # `result in CHECK_RESULTS` hashes its left operand. Durable JSON
        # can carry a dict or a list there, and a normalisation whose whole
        # contract is to return a finite reason must return one rather
        # than raise one.
        for value in ({}, [], {"result": "PASS"}, ["PASS"]):
            with self.subTest(value=value):
                checks = passing_checks()
                checks[0] = dict(checks[0], result=value)
                self.assertEqual(
                    routing.normalize_accessibility_auto(checks, HEAD),
                    (None, ac.RESULT_UNREADABLE))

    def test_an_unhashable_check_id_is_refused_rather_than_raising(self):
        for value in ({}, [], {"check_id": "AXE_SCAN"}):
            with self.subTest(value=value):
                checks = passing_checks()
                checks[0] = dict(checks[0], check_id=value)
                self.assertEqual(
                    routing.normalize_accessibility_auto(checks, HEAD),
                    (None, ac.RESULT_UNREADABLE))

    def test_an_entry_that_is_not_an_object_is_refused(self):
        checks = passing_checks()
        checks[2] = "RESPONSIVE_375PX: PASS"
        self.assertEqual(routing.normalize_accessibility_auto(checks, HEAD),
                         (None, ac.RESULT_UNREADABLE))

    def test_a_container_that_is_not_a_list_is_refused(self):
        for value in (None, {}, "", {"checks": []}, 0):
            with self.subTest(value=value):
                self.assertEqual(
                    routing.normalize_accessibility_auto(value, HEAD),
                    (None, ac.RESULT_UNREADABLE))

    def test_an_empty_list_is_refused(self):
        self.assertEqual(routing.normalize_accessibility_auto([], HEAD),
                         (None, ac.RESULT_EMPTY))

    def test_an_uncheckable_head_can_never_be_matched(self):
        # Without this guard an empty head and an empty claim sha would
        # compare equal and every check would pass vacuously.
        for head in (None, "", "deadbeef", HEAD.upper(), 40 * "z"):
            with self.subTest(head=head):
                checks = passing_checks(sha=head if isinstance(head, str)
                                        else HEAD)
                self.assertEqual(
                    routing.normalize_accessibility_auto(checks, head),
                    (None, ac.SHA_MISMATCH))

    def test_every_refusal_reason_is_in_the_automated_contract(self):
        bad = [[], None, "x", [{"check_id": "AXE_SCAN"}], passing_checks()[:2],
               passing_checks(OTHER_HEAD)]
        for value in bad:
            with self.subTest(value=value):
                verdict, reason = routing.normalize_accessibility_auto(
                    value, HEAD)
                self.assertIsNone(verdict)
                self.assertIn(reason, ac.ACCESSIBILITY_AUTO_FAILURE_REASONS)

    def test_a_verdict_and_a_reason_are_never_both_present(self):
        for checks in (passing_checks(), passing_checks(results={"AXE_SCAN": "FAIL"}),
                       [], passing_checks()[:3]):
            with self.subTest(checks=len(checks)):
                verdict, reason = routing.normalize_accessibility_auto(
                    checks, HEAD)
                self.assertNotEqual(verdict is None, reason == "")


if __name__ == "__main__":
    unittest.main()
