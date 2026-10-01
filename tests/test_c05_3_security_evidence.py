"""C-05.3a: the security verdict contract, parsed and adjudicated.

Security is a QUALITATIVE reviewer role, not a deterministic scanner. Its
output is prose from a provider, and the only thing standing between that
prose and a routing decision is this parser. So the parser's job is to be
faithful, and a separate function's job is to decide whether what was
parsed is a coherent contract. Collapsing the two is how a malformed
review quietly becomes a passing one: drop the finding that will not
validate, and the review that carried it looks clean.

The governed contract is C-05b (2026-09-30):

  SECURITY_PASS  all twelve surfaces valid, zero P0/P1, and MAY carry
                 P2/P3 as non-blocking security debt
  SECURITY_FAIL  all twelve surfaces valid, at least one P0/P1, may also
                 carry P2/P3

PASS_WITH_FINDINGS does not exist, and these tests assert its absence: it
encoded the rule C-05b reverses, and a code that says "a pass carried
findings" would re-introduce it by the back door.

Two things fail closed everywhere below. A missing severity is NEVER
defaulted - defaulting it silently makes an unlabelled finding
non-blocking, the exact defect parse_review documents having fixed - and
malformed output NEVER becomes a pass, whatever else is in the text.

This file covers the parser and its consistency rules only. Durable
persistence of the normalized outcome is Step 3.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import debt, gate_evidence, routing  # noqa: E402

ALL_PASS = {s: "PASS" for s in routing.SECURITY_SURFACES}


def finding(**over):
    base = {"id": "S1", "severity": "P1", "surface": "SECRETS",
            "file": "app/api/route.ts", "summary": "names the defect",
            "evidence": "what in the diff proves it",
            "required_change": "what must change"}
    base.update(over)
    return base


def block(verdict="SECURITY_PASS", surfaces=None, findings=(), summary="s"):
    payload = {"verdict": verdict,
               "surfaces": ALL_PASS if surfaces is None else surfaces,
               "findings": list(findings), "summary": summary}
    return "prose before\n\n```json\n" + json.dumps(payload) + "\n```\n"


def adjudicate(text):
    review = routing.parse_security(text)
    ok, reason = routing.security_is_consistent(review)
    return review, ok, reason


class SecurityVocabularyCase(unittest.TestCase):

    def test_the_twelve_surfaces_match_the_prompt(self):
        # The parser's vocabulary and prompts/security.md are one contract.
        # A thirteenth surface added to one without the other would make
        # every review SURFACES_INCOMPLETE, or silently stop requiring one.
        text = (Path(__file__).resolve().parent.parent
                / "prompts" / "security.md").read_text(encoding="utf-8")
        self.assertEqual(len(routing.SECURITY_SURFACES), 12)
        for surface in routing.SECURITY_SURFACES:
            self.assertIn(f'"{surface}"', text, surface)

    def test_pass_with_findings_does_not_exist(self):
        # C-05b reversed the rule this code encoded. Its absence is part of
        # the contract, not an oversight.
        self.assertFalse(hasattr(routing, "PASS_WITH_FINDINGS"))
        self.assertNotIn("PASS_WITH_FINDINGS", dir(routing))

    def test_security_parsing_is_separate_from_review_parsing(self):
        self.assertIsNot(routing.parse_security, routing.parse_review)
        self.assertIsNot(routing.security_is_consistent,
                         routing.review_is_consistent)


class SecurityPassCase(unittest.TestCase):

    def test_a_pass_with_no_findings_is_consistent(self):
        review, ok, reason = adjudicate(block())
        self.assertEqual(review.verdict, routing.SECURITY_PASS)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_a_pass_may_carry_p2_debt(self):
        review, ok, reason = adjudicate(
            block(findings=[finding(severity="P2")]))
        self.assertTrue(ok, reason)
        self.assertEqual(review.debt, review.findings)
        self.assertEqual(review.blocking, [])

    def test_a_pass_may_carry_p3_debt(self):
        review, ok, reason = adjudicate(
            block(findings=[finding(severity="P3")]))
        self.assertTrue(ok, reason)
        self.assertEqual(len(review.debt), 1)

    def test_a_pass_carrying_a_p0_is_refused(self):
        _, ok, reason = adjudicate(block(findings=[finding(severity="P0")]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.PASS_WITH_BLOCKING_FINDINGS)

    def test_a_pass_carrying_a_p1_is_refused(self):
        _, ok, reason = adjudicate(block(findings=[finding(severity="P1")]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.PASS_WITH_BLOCKING_FINDINGS)


class SecurityFailCase(unittest.TestCase):

    def test_a_fail_with_a_p0_is_consistent(self):
        review, ok, reason = adjudicate(
            block("SECURITY_FAIL", findings=[finding(severity="P0")]))
        self.assertTrue(ok, reason)
        self.assertEqual(len(review.blocking), 1)

    def test_a_fail_may_carry_blocking_and_debt_together(self):
        review, ok, reason = adjudicate(block("SECURITY_FAIL", findings=[
            finding(id="S1", severity="P1"),
            finding(id="S2", severity="P2", surface="DEPENDENCY_RISK"),
            finding(id="S3", severity="P3", surface="ERROR_LEAKAGE")]))
        self.assertTrue(ok, reason)
        self.assertEqual([f["id"] for f in review.blocking], ["S1"])
        self.assertEqual([f["id"] for f in review.debt], ["S2", "S3"])

    def test_a_fail_carrying_only_debt_is_refused(self):
        # P2/P3 are non-blocking by governance, so they cannot be the thing
        # that failed the review. Entering FIX_REQUIRED on them would make
        # non-blocking debt block.
        _, ok, reason = adjudicate(block("SECURITY_FAIL", findings=[
            finding(severity="P2"), finding(id="S2", severity="P3")]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FAIL_WITHOUT_BLOCKING_FINDINGS)

    def test_a_fail_with_no_findings_at_all_is_refused(self):
        _, ok, reason = adjudicate(block("SECURITY_FAIL"))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FAIL_WITHOUT_BLOCKING_FINDINGS)


class SecuritySurfacesCase(unittest.TestCase):

    def test_every_one_of_the_twelve_surfaces_is_required(self):
        for omitted in routing.SECURITY_SURFACES:
            with self.subTest(omitted=omitted):
                partial = {k: v for k, v in ALL_PASS.items() if k != omitted}
                _, ok, reason = adjudicate(block(surfaces=partial))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.SURFACES_INCOMPLETE)

    def test_the_three_graded_values_are_accepted(self):
        for value in ("PASS", "FAIL", "NOT_RELEVANT"):
            with self.subTest(value=value):
                surfaces = dict(ALL_PASS, DEPENDENCY_RISK=value)
                _, ok, reason = adjudicate(block(surfaces=surfaces))
                self.assertTrue(ok, reason)

    def test_an_unrecognised_surface_value_is_refused(self):
        for value in ("SKIPPED", "", "UNKNOWN", "MAYBE"):
            with self.subTest(value=value):
                surfaces = dict(ALL_PASS, SECRETS=value)
                _, ok, reason = adjudicate(block(surfaces=surfaces))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.SURFACES_INCOMPLETE)


class SecurityFindingFieldsCase(unittest.TestCase):

    def test_a_finding_with_no_severity_is_refused_never_defaulted(self):
        # The regression that matters. Defaulting an absent severity to P2
        # would move this finding silently into non-blocking debt and turn
        # an invalid review into a passing one.
        raw = finding()
        del raw["severity"]
        review, ok, reason = adjudicate(block(findings=[raw]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)
        # Parsed faithfully: absent stays absent, and it is in neither list.
        self.assertNotIn("severity", review.findings[0])
        self.assertEqual(review.blocking, [])
        self.assertEqual(review.debt, [])

    def test_an_unknown_severity_is_refused(self):
        for value in ("P4", "HIGH", "", "CRITICAL", "P1!"):
            with self.subTest(severity=value):
                _, ok, reason = adjudicate(
                    block(findings=[finding(severity=value)]))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_finding_with_no_surface_is_refused(self):
        raw = finding(severity="P2")
        del raw["surface"]
        review, ok, reason = adjudicate(block(findings=[raw]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)
        self.assertNotIn("surface", review.findings[0])

    def test_an_unknown_surface_on_a_finding_is_refused(self):
        for value in ("SQL_INJECTION", "", "SECRET"):
            with self.subTest(surface=value):
                _, ok, reason = adjudicate(
                    block(findings=[finding(severity="P2", surface=value)]))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_malformed_finding_is_never_dropped_to_rescue_the_review(self):
        # One valid P2 alongside one severity-less finding. Dropping the bad
        # one would yield a clean PASS; the whole review must be refused.
        raw = finding(id="S2")
        del raw["severity"]
        review, ok, reason = adjudicate(
            block(findings=[finding(severity="P2"), raw]))
        self.assertEqual(len(review.findings), 2, "nothing was dropped")
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_case_and_whitespace_are_normalised_but_values_not_invented(self):
        # routing._normalised upper-cases and strips, so these are the SAME
        # tokens, not lesser ones. Normalising a value the reviewer sent is
        # not the same as inventing one it did not.
        review, ok, reason = adjudicate(
            block(findings=[finding(severity=" p2 ", surface="secrets")]))
        self.assertTrue(ok, reason)
        self.assertEqual(review.findings[0]["severity"], "P2")
        self.assertEqual(review.findings[0]["surface"], "SECRETS")

    def test_a_whitespaced_blocking_severity_still_blocks_a_pass(self):
        # " p1 " normalises to a real P1; it must not slip past the blocking
        # check just because it arrived untidy.
        _, ok, reason = adjudicate(block(findings=[finding(severity=" p1 ")]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.PASS_WITH_BLOCKING_FINDINGS)


class SecurityMalformedContainerCase(unittest.TestCase):
    """The containers, before anything inside them.

    A parser that sanitizes away what it cannot understand is a parser that
    turns malformed output into a clean pass: drop the one entry that will
    not validate and the review carrying it looks fine. And a container of
    the wrong type must never raise provider-shaped output out of the
    parser - the control plane has to survive whatever a provider emits.
    """

    def raw(self, **over):
        payload = {"verdict": "SECURITY_PASS", "surfaces": ALL_PASS,
                   "findings": [], "summary": "s"}
        payload.update(over)
        return "```json\n" + json.dumps(payload) + "\n```\n"

    def test_a_non_object_finding_entry_invalidates_the_review(self):
        _, ok, reason = adjudicate(self.raw(findings=["not-an-object"]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_findings_object_instead_of_an_array_is_refused(self):
        _, ok, reason = adjudicate(self.raw(findings={"id": "S1"}))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_malformed_entry_cannot_disappear_and_leave_a_pass(self):
        # The exact fail-open shape: one unusable entry beside one valid P2.
        # Filtering the bad one out would yield a consistent SECURITY_PASS.
        review, ok, reason = adjudicate(
            self.raw(findings=[{}, finding(severity="P2")]))
        self.assertFalse(ok, "a malformed entry was sanitized away")
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_non_object_entry_beside_a_valid_one_still_refuses(self):
        _, ok, reason = adjudicate(
            self.raw(findings=["oops", finding(severity="P2")]))
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_missing_findings_key_is_refused(self):
        # security.md asks for "exactly this shape", and that shape always
        # carries a findings array. Reading its absence as "no findings"
        # would let output that never reported them adjudicate as clean.
        payload = {"verdict": "SECURITY_PASS", "surfaces": ALL_PASS,
                   "summary": "s"}
        _, ok, reason = adjudicate(
            "```json\n" + json.dumps(payload) + "\n```\n")
        self.assertFalse(ok)
        self.assertEqual(reason, routing.FINDING_FIELDS_INVALID)

    def test_a_non_mapping_surfaces_container_never_raises(self):
        for value in ([], ["SECRETS"], "text", 7, True, None):
            with self.subTest(surfaces=value):
                review, ok, reason = adjudicate(self.raw(surfaces=value))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.SURFACES_INCOMPLETE)
                self.assertEqual(review.surfaces, {})

    def test_the_parser_is_total_over_arbitrary_json_shapes(self):
        shapes = [
            {"verdict": "SECURITY_PASS"},
            {"verdict": None, "surfaces": None, "findings": None},
            {"verdict": "SECURITY_PASS", "surfaces": ALL_PASS,
             "findings": [[{"severity": "P2"}]]},
            {"verdict": "SECURITY_FAIL", "surfaces": {"SECRETS": ["PASS"]},
             "findings": [finding(severity="P0")]},
            {"verdict": ["SECURITY_PASS"], "surfaces": ALL_PASS,
             "findings": []},
            {"verdict": "SECURITY_PASS", "surfaces": ALL_PASS,
             "findings": [{"severity": {"nested": "P2"}, "surface": 5}]},
            {"verdict": "SECURITY_PASS", "surfaces": {5: 7}, "findings": []},
        ]
        for shape in shapes:
            with self.subTest(shape=str(shape)[:60]):
                text = "```json\n" + json.dumps(shape) + "\n```\n"
                review = routing.parse_security(text)      # must not raise
                ok, reason = routing.security_is_consistent(review)
                self.assertIsInstance(review, routing.SecurityReview)
                self.assertFalse(ok)
                self.assertIn(reason, {
                    routing.OUTPUT_UNPARSEABLE, routing.VERDICT_UNRECOGNISED,
                    routing.SURFACES_INCOMPLETE,
                    routing.FINDING_FIELDS_INVALID,
                    routing.PASS_WITH_BLOCKING_FINDINGS,
                    routing.FAIL_WITHOUT_BLOCKING_FINDINGS})

    def test_no_malformed_shape_ever_adjudicates_as_a_pass(self):
        for findings in (["x"], {"id": "S1"}, [{}], [None], [1, 2],
                         [finding(severity="P2"), "x"]):
            with self.subTest(findings=str(findings)[:40]):
                _, ok, _ = adjudicate(self.raw(findings=findings))
                self.assertFalse(ok)


class SecurityParsingCase(unittest.TestCase):

    def test_an_unrecognised_verdict_is_refused_distinctly(self):
        for value in ("PASS", "SECURITY_MAYBE", "APPROVED", ""):
            with self.subTest(verdict=value):
                _, ok, reason = adjudicate(block(verdict=value))
                self.assertFalse(ok)
                self.assertEqual(reason, routing.VERDICT_UNRECOGNISED)

    def test_output_with_no_block_is_unparseable_not_a_pass(self):
        for text in ("", "no fenced block here at all",
                     "```json\nnot json\n```",
                     "```json\n{\"surfaces\": {}}\n```"):   # no verdict key
            with self.subTest(text=text[:30]):
                review, ok, reason = adjudicate(text)
                self.assertEqual(review.verdict, routing.SECURITY_UNPARSEABLE)
                self.assertFalse(ok)
                self.assertEqual(reason, routing.OUTPUT_UNPARSEABLE)

    def test_truncated_json_never_becomes_a_pass(self):
        review, ok, _ = adjudicate(
            'the scan passed cleanly\n```json\n{"verdict": "SECURITY_PASS"')
        self.assertNotEqual(review.verdict, routing.SECURITY_PASS)
        self.assertFalse(ok)

    def test_reassuring_prose_around_a_broken_block_never_passes(self):
        review, ok, _ = adjudicate(
            "SECURITY_PASS - everything looks fine, no issues found.\n"
            "```json\n{ broken\n```\n")
        self.assertEqual(review.verdict, routing.SECURITY_UNPARSEABLE)
        self.assertFalse(ok)

    def test_the_last_block_wins_by_reverse_scan(self):
        text = block("SECURITY_PASS") + block(
            "SECURITY_FAIL", findings=[finding(severity="P0")])
        review, ok, reason = adjudicate(text)
        self.assertEqual(review.verdict, routing.SECURITY_FAIL)
        self.assertTrue(ok, reason)

    def test_a_malformed_last_block_is_not_superseded_by_an_earlier_pass(self):
        # security.md asks for the block as the last thing emitted, so the
        # final one is the reviewer's answer. Falling back to an earlier
        # PASS would promote a superseded verdict over a broken final word.
        text = block("SECURITY_PASS") + block("SECURITY_MAYBE")
        _, ok, reason = adjudicate(text)
        self.assertFalse(ok)
        self.assertEqual(reason, routing.VERDICT_UNRECOGNISED)


class SecurityPromptExampleCase(unittest.TestCase):
    """The C-05b-corrected example in prompts/security.md must itself be a
    valid instance - it is read by an autonomous reviewer and copied."""

    def _example(self, name):
        text = (Path(__file__).resolve().parent.parent
                / "prompts" / name).read_text(encoding="utf-8")
        start = text.index("```json")
        return text[start:text.index("```", start + 7) + 3]

    def test_the_security_prompt_example_parses_and_is_consistent(self):
        review, ok, reason = adjudicate(self._example("security.md"))
        self.assertEqual(review.verdict, routing.SECURITY_PASS)
        self.assertTrue(ok, f"the prompt's own example is invalid: {reason}")
        self.assertEqual([f["severity"] for f in review.findings], ["P2"])
        self.assertEqual(review.blocking, [])

    def test_the_reviewer_parser_is_untouched_by_this_work(self):
        # parse_review must still read the reviewer prompt's own corrected
        # example as a consistent REVIEW_PASS carrying only P2 debt.
        review = routing.parse_review(self._example("reviewer.md"))
        self.assertEqual(review.verdict, routing.REVIEW_PASS)
        self.assertEqual([f["severity"] for f in review.findings], ["P2"])
        self.assertEqual(review.blocking, [])


class SecurityReviewShapeCase(unittest.TestCase):

    def test_as_dict_is_finite_and_excludes_raw_prose(self):
        review = routing.parse_security(
            block(findings=[finding(severity="P2")], summary="x" * 900))
        payload = review.as_dict()
        self.assertEqual(set(payload),
                         {"verdict", "surfaces", "summary", "finding_ids",
                          "counts"})
        self.assertNotIn("raw_excerpt", payload)
        self.assertNotIn("evidence", json.dumps(payload))
        self.assertLessEqual(len(payload["summary"]), 500)
        self.assertEqual(payload["counts"], {"P0": 0, "P1": 0, "P2": 1, "P3": 0})


SHA40 = "a" * 40
CANARY = "sk-proj-" + "A" * 30


class SecurityAttemptPathCase(unittest.TestCase):
    """Addressing only. The ordinal is chosen under the state lock; this
    layer says where the claim lives and validates every component."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_canonical_path_is_built_from_structured_inputs(self):
        path = gate_evidence.security_attempt_dir(
            "TASK001", SHA40, "security-attempt-0003", root=self.root)
        self.assertEqual(
            path, self.root / "TASK001" / SHA40 / "security-attempt-0003")

    def test_an_attempt_id_that_is_not_canonical_is_refused(self):
        for bad in ("security-attempt-3", "security-attempt-00003", "attempt-0001",
                    "security-attempt-0001/x", "../security-attempt-0001",
                    "security-attempt-000a", "", "security-attempt-0001 "):
            with self.subTest(attempt_id=bad):
                with self.assertRaises(ValueError):
                    gate_evidence.security_attempt_dir(
                        "TASK001", SHA40, bad, root=self.root)

    def test_traversal_in_any_component_is_refused(self):
        with self.assertRaises(ValueError):
            gate_evidence.security_attempt_dir(
                "../../etc", SHA40, "security-attempt-0001", root=self.root)
        with self.assertRaises(ValueError):
            gate_evidence.security_attempt_dir(
                "TASK001", "../" + "a" * 37, "security-attempt-0001",
                root=self.root)
        for bad_sha in ("A" * 40, "a" * 39, "", "/" + "a" * 39):
            with self.subTest(sha=bad_sha):
                with self.assertRaises(ValueError):
                    gate_evidence.security_attempt_dir(
                        "TASK001", bad_sha, "security-attempt-0001",
                        root=self.root)

    def test_the_security_namespace_is_invisible_to_browser_scanning(self):
        # scan_sidecars walks attempt-* only. A security attempt must not
        # appear as a browser attempt, and must not spoil walk completeness.
        (self.root / "TASK001" / SHA40 / "security-attempt-0001"
         ).mkdir(parents=True)
        browser = self.root / "TASK001" / SHA40 / "attempt-0001"
        (browser / "accessibility").mkdir(parents=True)
        records, walk_ok = gate_evidence.scan_sidecars(self.root)
        self.assertTrue(walk_ok)
        self.assertEqual([r["attempt_dir"] for r in records], [str(browser)])

    def test_no_lifecycle_markers_are_created_for_security(self):
        path = gate_evidence.security_attempt_dir(
            "TASK001", SHA40, "security-attempt-0001", root=self.root)
        path.mkdir(parents=True)
        self.assertIsNone(gate_evidence.lifecycle(path))


class SecurityOutcomeCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.attempt = gate_evidence.security_attempt_dir(
            "TASK001", SHA40, "security-attempt-0001", root=self.root)
        self.attempt.mkdir(parents=True)
        self.result = {"exit_code": 0, "timed_out": False, "duration_ms": 12.5}

    def normalize(self, text=None, **over):
        kwargs = dict(task_id="TASK001", pr=12, sha=SHA40,
                      attempt_id="security-attempt-0001", result=self.result,
                      provider="codex", model=None)
        if text is not None:
            kwargs["review"] = routing.parse_security(text)
        kwargs.update(over)
        return gate_evidence.normalize_security_outcome(**kwargs)

    # ------------------------------------------------------- COMPLETED

    def test_a_pass_with_debt_persists_as_completed(self):
        record = self.normalize(block(findings=[
            finding(severity="P2", file="app/api/route.ts"),
            finding(id="S2", severity="P3", surface="ERROR_LEAKAGE",
                    file="app/lib/log.ts")]))
        self.assertEqual(record["status"], gate_evidence.COMPLETED)
        self.assertEqual(record["reason"], "")
        self.assertEqual(record["verdict"], routing.SECURITY_PASS)
        self.assertEqual(set(record["surfaces"]),
                         set(routing.SECURITY_SURFACES))
        self.assertEqual(len(record["findings"]), 2, "P2/P3 must be kept")
        self.assertEqual(record["finding_counts"],
                         {"P0": 0, "P1": 0, "P2": 1, "P3": 1})

    def test_a_fail_is_completed_not_an_apparatus_failure(self):
        record = self.normalize(block("SECURITY_FAIL", findings=[
            finding(severity="P0", file="app/api/route.ts"),
            finding(id="S2", severity="P2", surface="DEPENDENCY_RISK",
                    file="package.json")]))
        self.assertEqual(record["status"], gate_evidence.COMPLETED)
        self.assertEqual(record["verdict"], routing.SECURITY_FAIL)
        self.assertEqual(record["finding_counts"]["P0"], 1)
        self.assertEqual(len(record["findings"]), 2)

    def test_every_finding_carries_the_security_category_for_the_fixer(self):
        record = self.normalize(block("SECURITY_FAIL", findings=[
            finding(severity="P1", file="a.ts")]))
        self.assertEqual(record["findings"][0]["category"], "SECURITY")
        self.assertIn("SECURITY", routing.KNOWN_GATES)

    # ---------------------------------------------------------- FAILED

    def test_every_finite_reason_is_accepted_and_nulls_the_verdict(self):
        self.assertEqual(len(gate_evidence.SECURITY_FAILURE_REASONS), 11)
        for reason in sorted(gate_evidence.SECURITY_FAILURE_REASONS):
            with self.subTest(reason=reason):
                record = self.normalize(reason=reason)
                self.assertEqual(record["status"], gate_evidence.FAILED)
                self.assertEqual(record["reason"], reason)
                self.assertIsNone(record["verdict"])
                self.assertIsNone(record["surfaces"])
                self.assertIsNone(record["finding_counts"])

    def test_an_unknown_reason_is_refused(self):
        for reason in ("PASS_WITH_FINDINGS", "BROKEN", "", None):
            with self.subTest(reason=reason):
                with self.assertRaises((ValueError, TypeError)):
                    self.normalize(reason=reason or "UNKNOWN_CODE")

    def test_an_inconsistent_review_is_downgraded_to_failed(self):
        record = self.normalize(block(findings=[finding(severity="P0")]))
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["reason"], routing.PASS_WITH_BLOCKING_FINDINGS)
        self.assertIsNone(record["verdict"])

    def test_malformed_output_never_normalizes_to_a_pass(self):
        for text in ("", "no block", "```json\n{ broken\n```"):
            with self.subTest(text=text[:20]):
                record = self.normalize(text)
                self.assertEqual(record["status"], gate_evidence.FAILED)
                self.assertIsNone(record["verdict"])
                self.assertEqual(record["reason"], routing.OUTPUT_UNPARSEABLE)

    # -------------------------------------------------- free-text hygiene

    def test_reviewer_evidence_prose_is_never_persisted(self):
        record = self.normalize(block(findings=[finding(
            severity="P2", evidence="secret diff quote " + CANARY,
            file="app/api/route.ts")]))
        blob = json.dumps(record)
        self.assertNotIn("evidence", record["findings"][0])
        self.assertNotIn("raw_excerpt", blob)
        self.assertNotIn(CANARY, blob)

    def test_every_retained_string_is_scrubbed(self):
        record = self.normalize(block(
            summary="leak " + CANARY,
            findings=[finding(severity="P2", file="app/a.ts",
                              summary="s " + CANARY,
                              required_change="rc " + CANARY)]))
        blob = json.dumps(record)
        self.assertNotIn(CANARY, blob)
        self.assertIn("redacted", blob)

    def test_every_retained_string_is_bounded(self):
        record = self.normalize(block(
            summary="x" * 4000,
            findings=[finding(severity="P2", file="app/" + "d" * 4000 + ".ts",
                              summary="y" * 4000,
                              required_change="z" * 4000)]))
        self.assertLessEqual(len(record["summary"]), 500)
        found = record["findings"][0]
        self.assertLessEqual(len(found["summary"]), debt.SUMMARY_MAX)
        self.assertLessEqual(len(found["required_change"]), 1000)
        self.assertLessEqual(len(found["file"]), 512)

    def test_an_absolute_host_path_invalidates_the_finding(self):
        for bad in ("/home/serina/app/a.ts", "/tmp/x.ts", "~/a.ts",
                    "C:\\app\\a.ts", "\\\\server\\share"):
            with self.subTest(file=bad):
                record = self.normalize(
                    block(findings=[finding(severity="P2", file=bad)]))
                self.assertEqual(record["status"], gate_evidence.FAILED)
                self.assertEqual(record["reason"],
                                 routing.FINDING_FIELDS_INVALID)
                self.assertNotIn("/home/", json.dumps(record))

    def test_a_traversing_file_path_invalidates_the_finding(self):
        for bad in ("../../etc/passwd", "app/../../x.ts", ".."):
            with self.subTest(file=bad):
                record = self.normalize(
                    block(findings=[finding(severity="P2", file=bad)]))
                self.assertEqual(record["status"], gate_evidence.FAILED)
                self.assertEqual(record["reason"],
                                 routing.FINDING_FIELDS_INVALID)

    def test_a_blank_finding_id_invalidates_the_finding(self):
        raw = finding(severity="P2", file="a.ts")
        raw["id"] = "   "
        record = self.normalize(block(findings=[raw]))
        self.assertEqual(record["status"], gate_evidence.FAILED)
        self.assertEqual(record["reason"], routing.FINDING_FIELDS_INVALID)

    def test_unknown_reviewer_keys_are_dropped(self):
        record = self.normalize(block(findings=[finding(
            severity="P2", file="a.ts", exploit_code="rm -rf /",
            attacker_note=CANARY)]))
        self.assertEqual(set(record["findings"][0]),
                         {"id", "severity", "surface", "category", "file",
                          "summary", "required_change"})
        self.assertNotIn(CANARY, json.dumps(record))

    # ----------------------------------------------- publish and read back

    def test_a_published_outcome_reads_back_identically(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))
        self.assertTrue(
            gate_evidence.publish_security_outcome(self.attempt, record))
        back = gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root)
        self.assertEqual(back, record)

    def test_publication_is_write_once(self):
        first = self.normalize(block())
        second = self.normalize(reason=gate_evidence.TIMED_OUT)
        self.assertTrue(
            gate_evidence.publish_security_outcome(self.attempt, first))
        self.assertFalse(
            gate_evidence.publish_security_outcome(self.attempt, second))
        back = gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root)
        self.assertEqual(back["status"], gate_evidence.COMPLETED)
        self.assertEqual(list(self.attempt.glob("*.tmp")), [])

    def test_a_symlinked_outcome_is_refused(self):
        record = self.normalize(block())
        elsewhere = self.root / "outside.json"
        elsewhere.write_text(json.dumps(record), encoding="utf-8")
        (self.attempt / gate_evidence.SECURITY_OUTCOME_NAME).symlink_to(elsewhere)
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))

    def test_an_absent_or_garbled_outcome_reads_as_none(self):
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))
        (self.attempt / gate_evidence.SECURITY_OUTCOME_NAME).write_text(
            '{"status": "COMP', encoding="utf-8")
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))

    def test_a_record_violating_the_invariants_is_refused(self):
        record = self.normalize(block())
        for broken in (dict(record, status="COMPLETED", verdict=None),
                       dict(record, status="COMPLETED", surfaces=None),
                       dict(record, status="FAILED", reason="NOPE"),
                       dict(record, status="FAILED", verdict="SECURITY_PASS"),
                       dict(record, status="WHATEVER")):
            with self.subTest(status=broken.get("status")):
                path = self.attempt / gate_evidence.SECURITY_OUTCOME_NAME
                path.write_text(json.dumps(broken), encoding="utf-8")
                self.assertIsNone(gate_evidence.read_security_outcome(
                    "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))
                path.unlink()

    def test_a_record_describing_another_identity_is_refused(self):
        record = self.normalize(block())
        for wrong in (dict(record, task_id="TASK999"),
                      dict(record, sha="b" * 40),
                      dict(record, attempt_id="security-attempt-0009")):
            with self.subTest(differs=wrong["task_id"] + wrong["sha"][:4]):
                path = self.attempt / gate_evidence.SECURITY_OUTCOME_NAME
                path.write_text(json.dumps(wrong), encoding="utf-8")
                self.assertIsNone(gate_evidence.read_security_outcome(
                    "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))
                path.unlink()

    def test_a_stale_sha_is_not_current_evidence(self):
        # The artifact survives under its own SHA; it is simply not evidence
        # about a head that has since moved.
        record = self.normalize(block())
        self.assertTrue(
            gate_evidence.publish_security_outcome(self.attempt, record))
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK001", 12, "b" * 40, "security-attempt-0001", root=self.root))
        self.assertTrue((self.attempt /
                         gate_evidence.SECURITY_OUTCOME_NAME).is_file())

    def test_a_record_naming_a_different_pr_is_refused(self):
        # Two pull requests can legitimately reach the same commit, so the
        # evidence tree's task/SHA key does not distinguish them on its own.
        record = self.normalize(block())
        self.assertTrue(
            gate_evidence.publish_security_outcome(self.attempt, record))
        self.assertIsNotNone(gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))
        for wrong_pr in (13, 1, 999):
            with self.subTest(pr=wrong_pr):
                self.assertIsNone(gate_evidence.read_security_outcome(
                    "TASK001", wrong_pr, SHA40, "security-attempt-0001",
                    root=self.root))

    def test_a_non_canonical_read_identity_is_refused_not_raised(self):
        for pr in (0, -1, True, "12", None, 1.0):
            with self.subTest(pr=pr):
                self.assertIsNone(gate_evidence.read_security_outcome(
                    "TASK001", pr, SHA40, "security-attempt-0001",
                    root=self.root))

    def test_normalize_refuses_a_non_canonical_identity(self):
        # A record written under an identity the reader will refuse would
        # publish, look complete, and never be ingestible.
        for bad in ({"task_id": "../x"}, {"pr": 0}, {"pr": True}, {"pr": "12"},
                    {"sha": "A" * 40}, {"attempt_id": "attempt-0001"},
                    {"attempt_id": "security-attempt-1"}):
            with self.subTest(**bad):
                with self.assertRaises(ValueError):
                    self.normalize(block(), **bad)

    def test_a_forged_record_must_reproduce_the_boring_fields_too(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))
        forgeries = [
            ("extra key", dict(record, attacker_note="x")),
            ("missing key", {k: v for k, v in record.items() if k != "summary"}),
            ("stdout renamed", dict(record, stdout_name="out.txt")),
            ("stderr renamed", dict(record, stderr_name="err.txt")),
            ("timed_out not bool", dict(record, timed_out="no")),
            ("exit_code bool", dict(record, exit_code=True)),
            ("duration negative", dict(record, duration_ms=-1)),
            ("duration bool", dict(record, duration_ms=True)),
            ("provider unbounded", dict(record, provider="p" * 400)),
            ("summary unbounded", dict(record, summary="s" * 4000)),
            ("findings not a list", dict(record, findings={"id": "S1"})),
        ]
        for label, forged in forgeries:
            with self.subTest(forgery=label):
                self.assertFalse(gate_evidence._outcome_invariants_hold(forged),
                                 label)

    def test_counts_that_disagree_with_the_findings_are_refused(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))
        for counts in ({"P0": 0, "P1": 0, "P2": 0, "P3": 0},
                       {"P0": 0, "P1": 0, "P2": 2, "P3": 0},
                       {"P0": 0, "P1": 0, "P2": True, "P3": 0},
                       {"P0": 0, "P1": 0, "P2": -1, "P3": 0},
                       {"P0": 0, "P1": 0, "P2": 1}):
            with self.subTest(counts=str(counts)):
                self.assertFalse(gate_evidence._outcome_invariants_hold(
                    dict(record, finding_counts=counts)))

    def test_a_stored_finding_must_satisfy_the_writer_rules(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))

        def with_finding(**over):
            found = dict(record["findings"][0])
            found.update(over)
            return dict(record, findings=[found])

        for label, forged in [
            ("category wrong", with_finding(category="REVIEW")),
            ("category missing", dict(record, findings=[{
                k: v for k, v in record["findings"][0].items()
                if k != "category"}])),
            ("extra finding key", with_finding(evidence="diff quote")),
            ("absolute file", with_finding(file="/home/serina/a.ts")),
            ("traversing file", with_finding(file="../../etc/passwd")),
            ("unbounded file", with_finding(file="a/" + "b" * 900)),
            ("blank id", with_finding(id="   ")),
            ("unbounded id", with_finding(id="i" * 200)),
            ("unknown severity", with_finding(severity="P9")),
            ("unknown surface", with_finding(surface="SQLI")),
            ("unbounded summary", with_finding(summary="s" * 4000)),
            ("unbounded required_change",
             with_finding(required_change="r" * 4000)),
            ("finding not a dict", dict(record, findings=["S1"])),
        ]:
            with self.subTest(forgery=label):
                self.assertFalse(gate_evidence._outcome_invariants_hold(forged),
                                 label)

    def test_a_stored_verdict_must_agree_with_its_own_findings(self):
        # A hand-edited PASS carrying a blocker, or a FAIL carrying none.
        passing = self.normalize(block(findings=[finding(severity="P2",
                                                         file="a.ts")]))
        blocker = dict(passing["findings"][0], severity="P0")
        self.assertFalse(gate_evidence._outcome_invariants_hold(dict(
            passing, findings=[blocker],
            finding_counts={"P0": 1, "P1": 0, "P2": 0, "P3": 0})))
        failing = self.normalize(block("SECURITY_FAIL", findings=[
            finding(severity="P0", file="a.ts")]))
        debt_only = dict(failing["findings"][0], severity="P3")
        self.assertFalse(gate_evidence._outcome_invariants_hold(dict(
            failing, findings=[debt_only],
            finding_counts={"P0": 0, "P1": 0, "P2": 0, "P3": 1})))

    def test_a_failed_record_must_be_the_canonical_failed_shape(self):
        record = self.normalize(reason=gate_evidence.TIMED_OUT)
        self.assertTrue(gate_evidence._outcome_invariants_hold(record))
        for label, forged in [
            ("findings present", dict(record, findings=[{"id": "S1"}])),
            ("summary present", dict(record, summary="something")),
            ("verdict present", dict(record, verdict="SECURITY_PASS")),
            ("surfaces present", dict(record, surfaces=ALL_PASS)),
            ("counts present", dict(
                record, finding_counts={"P0": 0, "P1": 0, "P2": 0, "P3": 0})),
            ("unknown reason", dict(record, reason="PASS_WITH_FINDINGS")),
        ]:
            with self.subTest(forgery=label):
                self.assertFalse(gate_evidence._outcome_invariants_hold(forged),
                                 label)

    def test_a_forged_record_cannot_smuggle_an_unsanitized_secret(self):
        # Length is not the test. Every one of these is a perfectly legal
        # size; what makes them forgeries is that the writer would have
        # redacted them, so they are not values this module ever stored.
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))

        def with_finding(**over):
            found = dict(record["findings"][0])
            found.update(over)
            return dict(record, findings=[found])

        for label, forged in [
            ("finding summary", with_finding(summary="leak " + CANARY)),
            ("required_change", with_finding(required_change="do " + CANARY)),
            ("finding id", with_finding(id=CANARY)),
            ("file", with_finding(file="app/" + CANARY + ".ts")),
            ("review summary", dict(record, summary="leak " + CANARY)),
            ("provider", dict(record, provider=CANARY)),
            ("model", dict(record, model=CANARY)),
        ]:
            with self.subTest(field=label):
                self.assertIn(CANARY, json.dumps(forged), "fixture is wrong")
                self.assertFalse(gate_evidence._outcome_invariants_hold(forged),
                                 label)

    def test_a_forged_record_cannot_carry_uncanonical_whitespace(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))

        def with_finding(**over):
            found = dict(record["findings"][0])
            found.update(over)
            return dict(record, findings=[found])

        for label, forged in [
            ("id leading space", with_finding(id="  S1")),
            ("id trailing space", with_finding(id="S1  ")),
            ("file leading space", with_finding(file=" app/a.ts")),
            ("file trailing space", with_finding(file="app/a.ts ")),
        ]:
            with self.subTest(field=label):
                self.assertFalse(gate_evidence._outcome_invariants_hold(forged),
                                 label)

    def test_a_forged_record_cannot_exceed_what_the_writer_would_store(self):
        record = self.normalize(block(findings=[finding(severity="P2",
                                                        file="a.ts")]))
        found = dict(record["findings"][0], summary="s" * 4000)
        self.assertFalse(gate_evidence._outcome_invariants_hold(
            dict(record, findings=[found])))

    def test_the_reader_repairs_nothing_it_refuses(self):
        # A record needing repair is not evidence this module wrote, and
        # repairing it on read would launder a forgery into trusted state.
        record = self.normalize(block())
        forged = dict(record, summary="leak " + CANARY)
        path = self.attempt / gate_evidence.SECURITY_OUTCOME_NAME
        path.write_text(json.dumps(forged), encoding="utf-8")
        self.assertIsNone(gate_evidence.read_security_outcome(
            "TASK001", 12, SHA40, "security-attempt-0001", root=self.root))
        # Untouched on disk: the reader refused it, it did not rewrite it.
        self.assertIn(CANARY, path.read_text(encoding="utf-8"))

    def test_every_record_the_writer_produces_satisfies_the_reader(self):
        produced = [
            self.normalize(block()),
            self.normalize(block(findings=[finding(severity="P2",
                                                   file="a.ts")])),
            self.normalize(block(findings=[
                finding(severity="P3", surface="ERROR_LEAKAGE",
                        file="app/log.ts", summary="s " + CANARY,
                        required_change="r " + CANARY)])),
            self.normalize(block("SECURITY_FAIL", findings=[
                finding(severity="P0", file="a.ts"),
                finding(id="S2", severity="P2", surface="DEPENDENCY_RISK",
                        file="package.json")])),
            self.normalize(block(summary="x" * 4000)),
            self.normalize(block(), provider="codex", model="o4" * 80),
        ]
        produced += [self.normalize(reason=r)
                     for r in sorted(gate_evidence.SECURITY_FAILURE_REASONS)]
        for record in produced:
            with self.subTest(status=record["status"],
                              reason=record["reason"] or record["verdict"]):
                self.assertTrue(gate_evidence._outcome_invariants_hold(record))
                self.assertNotIn(CANARY, json.dumps(record))

    def test_malformed_runtime_metadata_fails_before_publication(self):
        for label, result in [
            ("exit_code bool", {"exit_code": True, "timed_out": False,
                                "duration_ms": 1.0}),
            ("exit_code str", {"exit_code": "0", "timed_out": False,
                               "duration_ms": 1.0}),
            ("duration bool", {"exit_code": 0, "timed_out": False,
                               "duration_ms": True}),
            ("duration negative", {"exit_code": 0, "timed_out": False,
                                   "duration_ms": -1}),
            ("duration NaN", {"exit_code": 0, "timed_out": False,
                              "duration_ms": float("nan")}),
            ("duration +inf", {"exit_code": 0, "timed_out": False,
                               "duration_ms": float("inf")}),
            ("duration -inf", {"exit_code": 0, "timed_out": False,
                               "duration_ms": float("-inf")}),
            ("duration str", {"exit_code": 0, "timed_out": False,
                              "duration_ms": "12"}),
            ("result not a mapping", ["exit_code"]),
        ]:
            with self.subTest(malformed=label):
                with self.assertRaises(ValueError):
                    self.normalize(block(), result=result)
                with self.assertRaises(ValueError):
                    self.normalize(reason=gate_evidence.TIMED_OUT,
                                   result=result)

    def test_a_non_bool_timed_out_is_rejected_never_coerced(self):
        # Coercion is the quietest kind of wrong: bool("false") is True,
        # bool(0) is False, and the record would then state as fact that
        # the attempt did or did not time out when nothing ever said so.
        malformed = (None, 0, 1, -1, "true", "false", "", [], {}, 1.0,
                     "True", object())
        for value in malformed:
            with self.subTest(timed_out=repr(value)):
                result = {"exit_code": 0, "timed_out": value,
                          "duration_ms": 1.0}
                with self.assertRaises(ValueError):
                    self.normalize(block(), result=result)
                with self.assertRaises(ValueError):
                    self.normalize(reason=gate_evidence.TIMED_OUT,
                                   result=result)

    def test_a_missing_timed_out_is_rejected(self):
        result = {"exit_code": 0, "duration_ms": 1.0}
        with self.assertRaises(ValueError):
            self.normalize(block(), result=result)
        with self.assertRaises(ValueError):
            self.normalize(reason=gate_evidence.TIMED_OUT, result=result)

    def test_both_bool_values_survive_exactly(self):
        for flag in (True, False):
            with self.subTest(timed_out=flag):
                result = {"exit_code": 0, "timed_out": flag,
                          "duration_ms": 1.0}
                completed = self.normalize(block(), result=result)
                failed = self.normalize(reason=gate_evidence.TIMED_OUT,
                                        result=result)
                for record in (completed, failed):
                    self.assertIs(record["timed_out"], flag)
                    self.assertTrue(
                        gate_evidence._outcome_invariants_hold(record))

    def test_absent_runtime_bounds_are_legitimate(self):
        # An attempt that never launched has no exit code and no duration;
        # that is not the same as exiting zero in zero milliseconds.
        record = self.normalize(
            reason=gate_evidence.SPAWN_OR_RUN_INCOMPLETE,
            result={"exit_code": None, "timed_out": False,
                    "duration_ms": None})
        self.assertIsNone(record["exit_code"])
        self.assertIsNone(record["duration_ms"])
        self.assertTrue(gate_evidence._outcome_invariants_hold(record))

    def test_finite_non_negative_runtime_values_are_accepted(self):
        for exit_code, duration in ((0, 0), (1, 0.0), (127, 12.5), (-1, 3)):
            with self.subTest(exit_code=exit_code, duration_ms=duration):
                record = self.normalize(
                    block(), result={"exit_code": exit_code, "timed_out": True,
                                     "duration_ms": duration})
                self.assertIs(record["timed_out"], True)
                self.assertTrue(
                    gate_evidence._outcome_invariants_hold(record))

    def test_the_outcome_key_set_is_closed_and_matches_the_writer(self):
        record = self.normalize(block())
        self.assertEqual(set(record), set(gate_evidence.SECURITY_OUTCOME_KEYS))
        failed = self.normalize(reason=gate_evidence.PROVIDER_FAILURE)
        self.assertEqual(set(failed), set(gate_evidence.SECURITY_OUTCOME_KEYS))

    def test_the_accessibility_publisher_is_unchanged(self):
        # Both names publish through one algorithm, into one directory,
        # without either claiming the other's file.
        acc = {"status": "COMPLETED", "reason": ""}
        self.assertTrue(gate_evidence._publish_outcome(self.attempt, acc))
        self.assertTrue(gate_evidence.publish_security_outcome(
            self.attempt, self.normalize(block())))
        self.assertTrue((self.attempt / gate_evidence.OUTCOME_NAME).is_file())
        self.assertTrue((self.attempt /
                         gate_evidence.SECURITY_OUTCOME_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
