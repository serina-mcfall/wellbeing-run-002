"""Severity policy (control/severity.py) — ported from the Run 002
apparatus/severity/severity-floor.js test suite; same cases.

Also groups three RUN001-F1 regression properties explicitly (see
control/severity.py's docstring for the corrected historical context):
  1. a valid P2/P3 finding with no applicable floor is non-blocking;
  2. an accessibility FAILURE citing a recognised requirement receives
     the deterministic P1 floor and blocks;
  3. invalid/missing evidence fails closed independently of severity.
"""

import unittest

from control import severity

KNOWN_REQUIREMENT_IDS = ["visible-focus-indicator", "keyboard-focus-trap"]


class TestMigratedSeverityFloorCases(unittest.TestCase):
    def test_no_classification_citation_rationale_is_invalid_and_blocks(self):
        result = severity.apply_severity_policy({"jev_severity": "P2"})
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])
        self.assertIsNone(result["severity"])

    def test_jev_p2_accessibility_failure_is_raised_to_p1_and_blocks(self):
        result = severity.apply_severity_policy(
            {"jev_severity": "P2", "classification": "FAILURE",
             "unmet_requirement": "visible-focus-indicator"},
            KNOWN_REQUIREMENT_IDS,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["severity"], "P1")
        self.assertTrue(result["floor_applied"])
        self.assertTrue(result["merge_blocked"])

    def test_a_failure_already_p0_stays_p0_floor_never_caps(self):
        result = severity.apply_severity_policy(
            {"jev_severity": "P0", "classification": "FAILURE",
             "unmet_requirement": "keyboard-focus-trap"},
            KNOWN_REQUIREMENT_IDS,
        )
        self.assertEqual(result["severity"], "P0")
        self.assertFalse(result["floor_applied"])
        self.assertTrue(result["merge_blocked"])

    def test_a_valid_non_failure_p3_recommendation_does_not_block(self):
        result = severity.apply_severity_policy({
            "jev_severity": "P3",
            "classification": "NON_FAILURE",
            "non_failure_rationale": "Checked the definition of done; contrast already meets the requirement.",
        })
        self.assertTrue(result["valid"])
        self.assertEqual(result["severity"], "P3")
        self.assertFalse(result["merge_blocked"])

    def test_a_valid_non_failure_p2_recommendation_does_not_block(self):
        result = severity.apply_severity_policy({
            "jev_severity": "P2",
            "classification": "NON_FAILURE",
            "non_failure_rationale": "Checked the cognitive accessibility list; nothing unmet, minor polish only.",
        })
        self.assertFalse(result["merge_blocked"])

    def test_contradictory_failure_with_non_failure_rationale_is_invalid(self):
        result = severity.apply_severity_policy(
            {"jev_severity": "P2", "classification": "FAILURE",
             "unmet_requirement": "visible-focus-indicator",
             "non_failure_rationale": "but also this is fine actually"},
            KNOWN_REQUIREMENT_IDS,
        )
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])

    def test_contradictory_non_failure_with_unmet_requirement_is_invalid(self):
        result = severity.apply_severity_policy({
            "jev_severity": "P3",
            "classification": "NON_FAILURE",
            "unmet_requirement": "visible-focus-indicator",
            "non_failure_rationale": "covering both bases",
        })
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])

    def test_an_arbitrary_citation_not_in_the_registry_is_invalid(self):
        result = severity.apply_severity_policy(
            {"jev_severity": "P2", "classification": "FAILURE",
             "unmet_requirement": "made-up-requirement"},
            KNOWN_REQUIREMENT_IDS,
        )
        self.assertFalse(result["valid"])
        self.assertIn("does not match an", result["reason"])

    def test_a_citation_fails_closed_when_no_registry_is_supplied(self):
        result = severity.apply_severity_policy({
            "jev_severity": "P2",
            "classification": "FAILURE",
            "unmet_requirement": "visible-focus-indicator",
        })
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])

    def test_fails_closed_on_unknown_classification_instead_of_throwing(self):
        result = severity.apply_severity_policy({"jev_severity": "P2", "classification": "MAYBE"})
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])

    def test_fails_closed_on_unknown_severity_instead_of_throwing(self):
        result = severity.apply_severity_policy({
            "jev_severity": "P9",
            "classification": "FAILURE",
            "unmet_requirement": "visible-focus-indicator",
        })
        self.assertFalse(result["valid"])
        self.assertTrue(result["merge_blocked"])

    def test_fails_closed_on_malformed_non_object_finding_instead_of_throwing(self):
        self.assertFalse(severity.apply_severity_policy(None)["valid"])
        self.assertFalse(severity.apply_severity_policy("not an object")["valid"])
        self.assertFalse(severity.apply_severity_policy(["array"])["valid"])
        self.assertTrue(severity.apply_severity_policy(None)["merge_blocked"])


class TestRun001F1RegressionProperties(unittest.TestCase):
    """RUN001-F1: a Reviewer requiring all gates PASS made a genuine,
    correctly-classified P2 finding indirectly merge-blocking, even
    though canonical policy says only P0/P1 block. These three
    properties are what this module must keep true so that specific
    miscomputation cannot recur here."""

    def test_property_1_unfloored_p2_p3_is_non_blocking(self):
        p2 = severity.apply_severity_policy({
            "jev_severity": "P2", "classification": "NON_FAILURE",
            "non_failure_rationale": "no unmet requirement identified",
        })
        p3 = severity.apply_severity_policy({
            "jev_severity": "P3", "classification": "NON_FAILURE",
            "non_failure_rationale": "no unmet requirement identified",
        })
        self.assertTrue(p2["valid"])
        self.assertFalse(p2["merge_blocked"])
        self.assertTrue(p3["valid"])
        self.assertFalse(p3["merge_blocked"])

    def test_property_2_recognised_failure_receives_p1_floor_and_blocks(self):
        result = severity.apply_severity_policy(
            {"jev_severity": "P2", "classification": "FAILURE",
             "unmet_requirement": "visible-focus-indicator"},
            KNOWN_REQUIREMENT_IDS,
        )
        self.assertEqual(result["severity"], "P1")
        self.assertTrue(result["merge_blocked"])

    def test_property_3_invalid_evidence_blocks_independently_of_severity(self):
        # Missing registry, contradictory evidence, and malformed input all
        # fail closed with merge_blocked True regardless of the jev_severity
        # they carried.
        no_registry = severity.apply_severity_policy({
            "jev_severity": "P3", "classification": "FAILURE",
            "unmet_requirement": "visible-focus-indicator",
        })
        contradictory = severity.apply_severity_policy({
            "jev_severity": "P3", "classification": "NON_FAILURE",
            "unmet_requirement": "visible-focus-indicator",
            "non_failure_rationale": "covering both bases",
        })
        self.assertFalse(no_registry["valid"])
        self.assertTrue(no_registry["merge_blocked"])
        self.assertFalse(contradictory["valid"])
        self.assertTrue(contradictory["merge_blocked"])


if __name__ == "__main__":
    unittest.main()
