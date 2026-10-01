"""G6 — explicit reviewed check/rule -> requirement mappings.

Approved 2026-10-01 with four constraints, each of which has cases below:

  * use EXACT existing registry identifiers;
  * no wildcard mappings, no new requirement IDs, no severity-policy change;
  * do not invent findings from absent or ambiguous details;
  * unmapped failures remain blocking, with their raw diagnostic preserved.

The strongest case here is `test_every_mapped_requirement_is_in_the_frozen
_document`: it re-reads product/ACCESSIBILITY.md and checks that each
mapped identifier's own source phrase is literally in it. A mapping table
checked only against the registry would still pass if the registry itself
drifted; this checks the thing the registry is derived FROM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_registry, config, routing  # noqa: E402

SOURCE_DOC = config.REPO_ROOT / "product" / "ACCESSIBILITY.md"


def all_mappings():
    """Every (key, requirement) pair across the three maps."""
    for check, req in routing.WHOLE_CHECK_REQUIREMENT.items():
        yield ("whole", check), req
    for key, req in routing.CONDITION_REQUIREMENT.items():
        yield ("condition", key), req
    for rule, req in routing.AXE_RULE_REQUIREMENT.items():
        yield ("axe", rule), req


class MappingsAreGroundedCase(unittest.TestCase):

    def test_every_mapped_requirement_is_an_exact_registry_identifier(self):
        for key, requirement in all_mappings():
            with self.subTest(key=key, requirement=requirement):
                self.assertIn(requirement,
                              accessibility_registry.REQUIREMENT_IDS)

    def test_every_mapped_requirement_is_in_the_frozen_document(self):
        # Not "is in the registry" - is in the document the registry is
        # DERIVED FROM. A mapping checked only against the registry would
        # survive the registry itself drifting.
        text = SOURCE_DOC.read_text(encoding="utf-8")
        by_id = {r["id"]: r["source_phrase"]
                 for r in accessibility_registry.REQUIREMENTS}
        for key, requirement in all_mappings():
            with self.subTest(key=key, requirement=requirement):
                phrase = by_id[requirement]
                self.assertIn(phrase, text)

    def test_no_mapping_key_is_a_wildcard(self):
        # Explicitly reviewed rules only. A wildcard sweeps unreviewed
        # rules into a requirement nobody checked them against.
        keys = [str(k[1]) for k, _ in all_mappings()]
        for key in keys:
            with self.subTest(key=key):
                for metachar in ("*", "?", "[", "prefix:"):
                    self.assertNotIn(metachar, key)

    def test_every_check_id_named_is_a_real_check(self):
        for check in routing.WHOLE_CHECK_REQUIREMENT:
            self.assertIn(check, routing.ACCESSIBILITY_CHECK_IDS)
        for check, _signal in routing.CONDITION_REQUIREMENT:
            self.assertIn(check, routing.ACCESSIBILITY_CHECK_IDS)

    def test_a_check_is_not_mapped_both_ways(self):
        # Whole-check and per-condition keys for the same check would make
        # which requirement applies depend on lookup order.
        composite = {check for check, _ in routing.CONDITION_REQUIREMENT}
        self.assertEqual(composite & set(routing.WHOLE_CHECK_REQUIREMENT),
                         set())


class DeliberateAbsencesCase(unittest.TestCase):
    """What is NOT mapped, and why, pinned so it is not quietly added."""

    def test_image_alt_is_not_mapped(self):
        # The frozen list states no text-alternative requirement.
        # "meaningful labels" is about labels; "screen-reader
        # understandable forms/errors" is about forms. Mapping image-alt
        # would be inventing a requirement, not reusing one.
        self.assertNotIn("image-alt", routing.AXE_RULE_REQUIREMENT)

    def test_no_aria_or_landmark_rule_is_mapped(self):
        for rule in routing.AXE_RULE_REQUIREMENT:
            with self.subTest(rule=rule):
                self.assertFalse(rule.startswith("aria-"))
                self.assertFalse(rule.startswith("landmark-"))

    def test_flashing_and_autoplay_have_no_requirement(self):
        # run.js measures them, and no frozen phrase states them.
        for signal in ("RAPID_CYCLING", "AUTOPLAYING"):
            with self.subTest(signal=signal):
                self.assertNotIn(
                    ("REDUCED_MOTION_NO_FLASHING_AUTOPLAY", signal),
                    routing.CONDITION_REQUIREMENT)

    def test_screenshots_is_not_mapped(self):
        self.assertNotIn("SCREENSHOTS_ARTIFACTS",
                         routing.WHOLE_CHECK_REQUIREMENT)


class SignalExtractionCase(unittest.TestCase):
    """Sub-conditions come from finite fields, never from inference."""

    def test_focus_order_reports_each_signal_that_fired(self):
        signals = routing.accessibility_check_signals(
            "FOCUS_ORDER_VISIBLE_NO_TRAPS",
            {"focusableCount": 5,
             "forward": {"visitedCount": 5, "trapped": True},
             "backward": {"visitedCount": 5, "trapped": True},
             "noVisibleIndicatorCount": 3})
        self.assertEqual(set(signals),
                         {"FORWARD_TRAPPED", "BACKWARD_TRAPPED",
                          "NO_VISIBLE_INDICATOR"})

    def test_focus_order_reports_unreachability_separately(self):
        signals = routing.accessibility_check_signals(
            "FOCUS_ORDER_VISIBLE_NO_TRAPS",
            {"focusableCount": 5,
             "forward": {"visitedCount": 2, "trapped": False},
             "backward": {"visitedCount": 5, "trapped": False},
             "noVisibleIndicatorCount": 0})
        self.assertEqual(signals, ["NOT_ALL_REACHABLE"])

    def test_a_trapped_false_is_not_a_signal(self):
        signals = routing.accessibility_check_signals(
            "FOCUS_ORDER_VISIBLE_NO_TRAPS",
            {"focusableCount": 2,
             "forward": {"visitedCount": 2, "trapped": False},
             "backward": {"visitedCount": 2, "trapped": False},
             "noVisibleIndicatorCount": 0})
        self.assertEqual(signals, [])

    def test_labels_reports_distinct_offender_reasons_once_each(self):
        signals = routing.accessibility_check_signals(
            "LABELS_AND_TEXT_ERRORS",
            {"offenders": [{"reason": "NO_ACCESSIBLE_NAME"},
                           {"reason": "NO_ACCESSIBLE_NAME"},
                           {"reason": "INVALID_WITHOUT_TEXT_ERROR"}]})
        self.assertEqual(signals,
                         ["NO_ACCESSIBLE_NAME", "INVALID_WITHOUT_TEXT_ERROR"])

    def test_absent_or_malformed_detail_yields_no_signals(self):
        for detail in (None, {}, [], "detail", 7,
                       {"offenders": "no"}, {"offenders": [None, 7]},
                       {"forward": "no"}):
            with self.subTest(detail=repr(detail)):
                for check in ("FOCUS_ORDER_VISIBLE_NO_TRAPS",
                              "LABELS_AND_TEXT_ERRORS",
                              "REDUCED_MOTION_NO_FLASHING_AUTOPLAY"):
                    self.assertEqual(
                        routing.accessibility_check_signals(check, detail), [])


class FindingsCase(unittest.TestCase):

    def rate(self, findings):
        return [routing.adjudicate_accessibility_finding(f) for f in findings]

    def test_a_mapped_whole_check_yields_one_valid_blocking_finding(self):
        findings = routing.accessibility_findings_for_check(
            "TOUCH_TARGETS", {"offenders": [{"tag": "BUTTON"}]})
        self.assertEqual(len(findings), 1)
        rated = self.rate(findings)[0]
        self.assertTrue(rated["valid"])
        self.assertEqual(rated["severity"], "P1")
        self.assertTrue(rated["merge_blocked"])

    def test_a_composite_check_yields_one_finding_per_fired_condition(self):
        # Collapsing them would hide one of two different requirements.
        findings = routing.accessibility_findings_for_check(
            "LABELS_AND_TEXT_ERRORS",
            {"offenders": [{"reason": "NO_ACCESSIBLE_NAME"},
                           {"reason": "INVALID_WITHOUT_TEXT_ERROR"}]})
        self.assertEqual(
            [f["unmet_requirement"] for f in findings],
            ["ACC-DOD-MEANINGFUL_LABELS", "ACC-DOD-SCREEN_READER_FORMS_ERRORS"])
        self.assertTrue(all(r["valid"] for r in self.rate(findings)))

    def test_an_unmapped_axe_rule_blocks_and_keeps_its_rule_id(self):
        findings = routing.accessibility_findings_for_check(
            "AXE_SCAN", {"violations": [{"id": "image-alt"}]})
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["unmet_requirement"])
        self.assertEqual(findings[0]["condition"], "image-alt")
        rated = self.rate(findings)[0]
        self.assertFalse(rated["valid"])
        self.assertTrue(rated["merge_blocked"])

    def test_a_mapped_and_an_unmapped_axe_rule_both_appear(self):
        findings = routing.accessibility_findings_for_check(
            "AXE_SCAN", {"violations": [{"id": "color-contrast"},
                                        {"id": "aria-required-attr"}]})
        self.assertEqual([f["unmet_requirement"] for f in findings],
                         ["ACC-DOD-SUFFICIENT_CONTRAST", None])
        self.assertTrue(routing.accessibility_findings_block_merge(findings))

    def test_an_unrecognised_check_blocks_rather_than_disappearing(self):
        findings = routing.accessibility_findings_for_check(
            "SCREENSHOTS_ARTIFACTS", {"error": "unwritable"})
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["unmet_requirement"])
        self.assertTrue(routing.accessibility_findings_block_merge(findings))

    def test_a_failure_with_no_usable_detail_still_produces_a_finding(self):
        # Absent detail must not mean "nothing failed". It means we cannot
        # say WHICH requirement, which is uncited, which blocks.
        for detail in (None, {}, "nonsense"):
            with self.subTest(detail=repr(detail)):
                findings = routing.accessibility_findings_for_check(
                    "FOCUS_ORDER_VISIBLE_NO_TRAPS", detail)
                self.assertEqual(len(findings), 1)
                self.assertIsNone(findings[0]["unmet_requirement"])
                self.assertTrue(
                    routing.accessibility_findings_block_merge(findings))

    def test_the_raw_diagnostic_is_preserved_on_the_finding(self):
        detail = {"noVisibleIndicatorCount": 4, "focusableCount": 9}
        findings = routing.accessibility_findings_for_check(
            "FOCUS_ORDER_VISIBLE_NO_TRAPS", detail)
        self.assertEqual(findings[0]["detail"], detail)
        self.assertEqual(findings[0]["check_id"],
                         "FOCUS_ORDER_VISIBLE_NO_TRAPS")

    def test_every_finding_blocks_whether_mapped_or_not(self):
        # The whole point: a FAILING check never results in a merge,
        # regardless of whether G6 has a mapping for it yet.
        samples = [
            ("RESPONSIVE_375PX", {"scrollWidth": 2000}),
            ("AXE_SCAN", {"violations": [{"id": "image-alt"}]}),
            ("SCREENSHOTS_ARTIFACTS", {"error": "x"}),
            ("REDUCED_MOTION_NO_FLASHING_AUTOPLAY",
             {"violations": ["rapidCycling"]}),
        ]
        for check, detail in samples:
            with self.subTest(check=check):
                findings = routing.accessibility_findings_for_check(check, detail)
                self.assertTrue(
                    routing.accessibility_findings_block_merge(findings))


class SeverityPolicyUnchangedCase(unittest.TestCase):

    def test_g6_added_no_requirement_identifier(self):
        self.assertEqual(len(accessibility_registry.REQUIREMENT_IDS), 17)

    def test_g6_changed_no_severity_value(self):
        for key, _ in all_mappings():
            pass
        findings = routing.accessibility_findings_for_check(
            "TOUCH_TARGETS", {"offenders": [{"tag": "A"}]})
        # P1 asserted, floored by policy to P1 - the floor raises, never caps.
        self.assertEqual(findings[0]["jev_severity"], "P1")


if __name__ == "__main__":
    unittest.main()
