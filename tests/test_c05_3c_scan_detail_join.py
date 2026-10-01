"""The seam where run.js's `details` map reaches G6's mapping tables.

WHY THIS FILE EXISTS. `routing.accessibility_findings_for_check` reads
`check["detail"]`; `apparatus/accessibility/run.js` writes
`{checks: [...], details: {CHECK_ID: {...}}}` — two separate top-level
fields. `ProductServices.scan` returned `payload["checks"]` alone, so
every check reached `findings_for` with no detail, and G6's approved
composite sub-condition structure never fired on a single real run.

It was a downgrade rather than a hole: an uncited finding is INVALID and
blocks. That is precisely why no test caught it — every assertion about
blocking stayed true. These assert the thing that was NOT true: that a
composite failure cites the requirement the repository already knows.

No browser and no npm here. The join is pure, so it is tested as one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_evidence as ae
from control import accessibility_registry, accessibility_services
from control import accessibility_contract as ac
from control import gate_evidence, severity

# A real shape: this is what run.js actually wrote for the violations
# fixture, reduced to the checks that matter here.
REAL_PAYLOAD = {
    "checks": [
        {"check_id": "FOCUS_ORDER_VISIBLE_NO_TRAPS", "result": "FAIL",
         "sha": "a" * 40, "artifact_reference": "x.png"},
        {"check_id": "LABELS_AND_TEXT_ERRORS", "result": "FAIL",
         "sha": "a" * 40, "artifact_reference": "x.png"},
        {"check_id": "AXE_SCAN", "result": "FAIL",
         "sha": "a" * 40, "artifact_reference": "x.png"},
        {"check_id": "TOUCH_TARGETS", "result": "PASS",
         "sha": "a" * 40, "artifact_reference": "x.png"},
    ],
    "details": {
        "FOCUS_ORDER_VISIBLE_NO_TRAPS": {
            "forward": {"trapped": True, "visitedCount": 1},
            "backward": {"trapped": True},
            "focusableCount": 3,
            "noVisibleIndicatorCount": 0,
        },
        "LABELS_AND_TEXT_ERRORS": {
            "offenders": [{"reason": "NO_ACCESSIBLE_NAME"},
                          {"reason": "INVALID_WITHOUT_TEXT_ERROR"}],
        },
        "AXE_SCAN": {"violations": [{"id": "label", "impact": "critical",
                                     "help": "Form elements must have labels",
                                     "nodes": 1}]},
        "TOUCH_TARGETS": {"offenders": []},
    },
}


class JoinCase(unittest.TestCase):
    def test_each_check_receives_its_own_detail(self):
        checks = accessibility_services._with_details(
            [dict(c) for c in REAL_PAYLOAD["checks"]], REAL_PAYLOAD["details"])
        by_id = {c["check_id"]: c for c in checks}
        self.assertEqual(
            by_id["LABELS_AND_TEXT_ERRORS"]["detail"],
            REAL_PAYLOAD["details"]["LABELS_AND_TEXT_ERRORS"])
        self.assertEqual(
            by_id["AXE_SCAN"]["detail"]["violations"][0]["id"], "label")

    def test_a_check_with_no_detail_is_left_alone(self):
        """Absent must stay absent.

        An invented `{}` would read as "this check reported no
        sub-condition we recognise" instead of "this check reported
        nothing we could read" — the two deserve different handling, and
        collapsing them is how an unreadable scan starts looking tidy.
        """
        checks = accessibility_services._with_details(
            [{"check_id": "AXE_SCAN", "result": "FAIL"}], {})
        self.assertNotIn("detail", checks[0])

    def test_an_unusable_details_field_changes_nothing(self):
        for broken in (None, [], "details", 7):
            checks = accessibility_services._with_details(
                [{"check_id": "AXE_SCAN", "result": "FAIL"}], broken)
            self.assertNotIn("detail", checks[0])

    def test_a_non_dict_check_is_skipped_not_crashed_on(self):
        checks = accessibility_services._with_details(
            ["not a check", {"check_id": "AXE_SCAN"}], REAL_PAYLOAD["details"])
        self.assertEqual(checks[0], "not a check")
        self.assertIn("detail", checks[1])


class WhatTheJoinUnlocksCase(unittest.TestCase):
    """The point of the join: cited findings instead of uncited ones."""

    def _findings(self, checks):
        outcome = ae.AttemptOutcome(
            status=ae.COMPLETED, verdict=ac.ACCESSIBILITY_AUTO_FAIL,
            reason="", checks=checks, port_released=True, phase="SCAN",
            server_started=True)
        return ae.findings_for(outcome)

    def test_without_the_join_every_composite_failure_is_uncited(self):
        """The behaviour as it actually was. Kept as the contrast."""
        findings = self._findings([dict(c) for c in REAL_PAYLOAD["checks"]])
        composite = [f for f in findings
                     if f["check_id"] != "TOUCH_TARGETS"]
        self.assertTrue(composite)
        self.assertTrue(
            all(f["unmet_requirement"] is None for f in composite),
            "this test documents the OLD behaviour; if it fails the join "
            "has moved and this contrast is no longer meaningful")

    def test_with_the_join_the_composite_failures_cite_requirements(self):
        checks = accessibility_services._with_details(
            [dict(c) for c in REAL_PAYLOAD["checks"]], REAL_PAYLOAD["details"])
        cited = {f["unmet_requirement"] for f in self._findings(checks)}
        # A keyboard trap in both directions, and not every control
        # reachable — all three are the keyboard requirement.
        self.assertIn("ACC-DOD-KEYBOARD_OPERATION", cited)
        # Two distinct frozen phrases from ONE check, which is the whole
        # reason G6 has a composite structure at all.
        self.assertIn("ACC-DOD-MEANINGFUL_LABELS", cited)
        self.assertIn("ACC-DOD-SCREEN_READER_FORMS_ERRORS", cited)

    def test_every_cited_requirement_is_in_the_registry(self):
        """A citation the registry does not contain is still INVALID.

        The join must not become a route for inventing identifiers: what
        it produces has to survive the same severity policy as anything a
        reviewer writes.
        """
        checks = accessibility_services._with_details(
            [dict(c) for c in REAL_PAYLOAD["checks"]], REAL_PAYLOAD["details"])
        known = accessibility_registry.requirement_ids()
        for finding in self._findings(checks):
            requirement = finding["unmet_requirement"]
            if requirement is None:
                continue
            self.assertIn(requirement, known)
            rated = severity.apply_severity_policy(
                finding, known_requirement_ids=known)
            self.assertTrue(rated["valid"])
            self.assertEqual(rated["severity"], "P1")

    def test_an_unmapped_signal_still_blocks(self):
        """G6's "unmapped failures remain blocking", after the join.

        The join adds citations; it must not remove the blocking an
        uncited finding already had. `image-alt` is deliberately absent
        from AXE_RULE_REQUIREMENT — the frozen list states no
        text-alternative requirement — so it is the honest probe.
        """
        checks = accessibility_services._with_details(
            [{"check_id": "AXE_SCAN", "result": "FAIL", "sha": "a" * 40}],
            {"AXE_SCAN": {"violations": [{"id": "image-alt", "nodes": 2}]}})
        findings = self._findings(checks)
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0]["unmet_requirement"])
        rated = severity.apply_severity_policy(
            findings[0],
            known_requirement_ids=accessibility_registry.requirement_ids())
        self.assertFalse(rated["valid"])
        self.assertTrue(rated["merge_blocked"])
        # The raw diagnostic survives, so a human can see what fired.
        self.assertEqual(findings[0]["detail"]["violations"][0]["id"],
                         "image-alt")


class ThroughTheRealScanReaderCase(unittest.TestCase):
    """`ProductServices.scan` does the join on a real result.json.

    The runner is injected so no browser launches, but the file read, the
    JSON parse and the join are the production ones.
    """

    def test_scan_returns_checks_carrying_their_details(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = gate_evidence.allocate_attempt(
                "TASK-FIXTURE", "a" * 40, root=Path(root))
            ctx = gate_evidence.context(attempt, "a" * 40, "http://127.0.0.1:1/")
            Path(ctx.result_path).write_text(json.dumps(REAL_PAYLOAD),
                                             encoding="utf-8")
            services = accessibility_services.ProductServices(
                Path(root), ctx, runner=lambda *a, **k: (True, 0))
            checks = services.scan(1, "a" * 40, 10)
        by_id = {c["check_id"]: c for c in checks}
        self.assertIn("detail", by_id["AXE_SCAN"])
        self.assertEqual(by_id["AXE_SCAN"]["detail"]["violations"][0]["id"],
                         "label")

    def test_an_unreadable_result_is_still_None_not_an_empty_list(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = gate_evidence.allocate_attempt(
                "TASK-FIXTURE", "a" * 40, root=Path(root))
            ctx = gate_evidence.context(attempt, "a" * 40, "http://127.0.0.1:1/")
            Path(ctx.result_path).write_text("{not json", encoding="utf-8")
            services = accessibility_services.ProductServices(
                Path(root), ctx, runner=lambda *a, **k: (True, 0))
            self.assertIsNone(services.scan(1, "a" * 40, 10))

    def test_a_non_list_checks_field_is_still_None(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = gate_evidence.allocate_attempt(
                "TASK-FIXTURE", "a" * 40, root=Path(root))
            ctx = gate_evidence.context(attempt, "a" * 40, "http://127.0.0.1:1/")
            Path(ctx.result_path).write_text(
                json.dumps({"checks": {"a": 1}, "details": {}}),
                encoding="utf-8")
            services = accessibility_services.ProductServices(
                Path(root), ctx, runner=lambda *a, **k: (True, 0))
            self.assertIsNone(services.scan(1, "a" * 40, 10))


if __name__ == "__main__":
    unittest.main()
