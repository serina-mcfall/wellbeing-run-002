"""The severity floor, applied by the CONTROL PLANE — C-02 integration.

`control/severity.py::apply_severity_policy` implements
protocol/SEVERITY-POLICY.md. Until this work it had **no caller anywhere
in `control/`** — verified by grep, which found only comments referring to
it. The deterministic P1 accessibility floor, the requirement-registry
citation check and "INVALID evidence always blocks merge" therefore ran
only in `apparatus/severity/severity-floor.js`, which the runtime never
invokes. The control plane read whatever severity it was handed.

`routing.adjudicate_accessibility_finding` is the call. It supplies the
registry argument the policy has always taken, from the canonical C-02
registry. **No severity policy changed**: `control/severity.py` is a
frozen-hash input and is untouched.

G6 is still open, and these tests pin the consequence the operator
required: an automated check with no governed requirement mapping yields
an uncited FAILURE, which the policy rates INVALID and merge-blocking.
Unmapped is blocking, never waved through.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import accessibility_registry, manifest, routing, severity  # noqa: E402


class TheFloorIsAppliedByTheControlPlaneCase(unittest.TestCase):

    def test_a_recognised_citation_is_floored_to_p1_and_blocks(self):
        rated = routing.adjudicate_accessibility_finding({
            "classification": "FAILURE",
            "jev_severity": "P3",
            "unmet_requirement": "ACC-DOD-VISIBLE_FOCUS",
        })
        self.assertTrue(rated["valid"])
        self.assertEqual(rated["severity"], "P1")
        self.assertTrue(rated["floor_applied"])
        self.assertTrue(rated["merge_blocked"])

    def test_the_floor_raises_and_never_caps(self):
        rated = routing.adjudicate_accessibility_finding({
            "classification": "FAILURE",
            "jev_severity": "P0",
            "unmet_requirement": "ACC-DOD-KEYBOARD_OPERATION",
        })
        self.assertEqual(rated["severity"], "P0")
        self.assertFalse(rated["floor_applied"])
        self.assertTrue(rated["merge_blocked"])

    def test_an_invented_citation_is_invalid_and_blocks(self):
        rated = routing.adjudicate_accessibility_finding({
            "classification": "FAILURE",
            "jev_severity": "P1",
            "unmet_requirement": "ACC-DOD-ANYTHING_I_LIKE",
        })
        self.assertFalse(rated["valid"])
        self.assertIsNone(rated["severity"])
        self.assertTrue(rated["merge_blocked"])

    def test_the_registry_supplied_is_the_canonical_one(self):
        # If this ever stopped being the C-02 registry, every assertion
        # above would still pass against whatever replaced it.
        self.assertEqual(len(accessibility_registry.REQUIREMENT_IDS), 17)
        self.assertIn("ACC-DOD-VISIBLE_FOCUS",
                      accessibility_registry.REQUIREMENT_IDS)

    def test_every_registry_identifier_is_accepted_by_the_control_plane(self):
        for ident in sorted(accessibility_registry.REQUIREMENT_IDS):
            with self.subTest(requirement=ident):
                rated = routing.adjudicate_accessibility_finding({
                    "classification": "FAILURE", "jev_severity": "P2",
                    "unmet_requirement": ident})
                self.assertTrue(rated["valid"], ident)
                self.assertEqual(rated["severity"], "P1")


class UnmappedChecksStayBlockingCase(unittest.TestCase):
    """G6 is open, and that must fail closed rather than fail silent."""

    def test_the_check_requirement_map_is_still_empty(self):
        # Honest statement of where G6 stands. When it is answered this
        # assertion is the deliberate place to change.
        self.assertEqual(routing.CHECK_REQUIREMENT, {})

    def test_every_automated_check_currently_yields_an_uncited_failure(self):
        for check_id in routing.AUTOMATED_CHECK_IDS:
            with self.subTest(check=check_id):
                finding = routing.accessibility_auto_finding(check_id)
                self.assertIsNone(finding["unmet_requirement"])
                rated = routing.adjudicate_accessibility_finding(finding)
                self.assertFalse(rated["valid"])
                self.assertTrue(
                    rated["merge_blocked"],
                    f"{check_id} failed without a governed requirement and "
                    f"did not block the merge")

    def test_no_placeholder_citation_is_ever_substituted(self):
        # severity.py accepts any nonempty string that is in the registry.
        # A plausible-looking placeholder is exactly how an ungoverned
        # mapping would become policy by accident.
        for check_id in routing.AUTOMATED_CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertIsNone(
                    routing.accessibility_auto_finding(check_id)["unmet_requirement"])

    def test_any_mapping_that_IS_added_must_name_a_real_requirement(self):
        # THE G6 TRIPWIRE. The operator's constraint: reuse existing
        # requirement IDs where the violation genuinely supports it, and
        # add no new ones without an explicit amendment. A map entry
        # citing an identifier the frozen registry does not contain would
        # be a new requirement invented in code.
        for check_id, requirement in routing.CHECK_REQUIREMENT.items():
            with self.subTest(check=check_id, requirement=requirement):
                self.assertIn(check_id, routing.ACCESSIBILITY_CHECK_IDS)
                self.assertIn(
                    requirement, accessibility_registry.REQUIREMENT_IDS,
                    f"{check_id} maps to {requirement!r}, which is not in the "
                    f"frozen C-02 registry. G6 may reuse existing identifiers; "
                    f"it may not mint new ones without an amendment.")


class BlockingSetCase(unittest.TestCase):

    def test_a_malformed_findings_array_blocks(self):
        for findings in (None, {}, "findings", 7):
            with self.subTest(findings=repr(findings)):
                self.assertTrue(
                    routing.accessibility_findings_block_merge(findings))

    def test_an_empty_list_does_not_block(self):
        # No findings is the shape of a clean scan. Whether a scan HAPPENED
        # is the verdict's job, and review_gate_fires already refuses an
        # absent, incomplete or failed leg.
        self.assertFalse(routing.accessibility_findings_block_merge([]))

    def test_one_invalid_finding_among_valid_ones_still_blocks(self):
        good = {"classification": "NON_FAILURE", "jev_severity": "P3",
                "non_failure_rationale": "no criterion is unmet here"}
        bad = {"classification": "FAILURE", "jev_severity": "P2",
               "unmet_requirement": "NOT-A-REAL-ID"}
        self.assertFalse(routing.accessibility_findings_block_merge([good]))
        self.assertTrue(routing.accessibility_findings_block_merge([good, bad]))

    def test_p2_and_p3_non_failures_do_not_independently_block(self):
        for sev in ("P2", "P3"):
            with self.subTest(severity=sev):
                self.assertFalse(routing.accessibility_findings_block_merge([{
                    "classification": "NON_FAILURE", "jev_severity": sev,
                    "non_failure_rationale": "an affirmative explanation"}]))

    def test_a_malformed_single_finding_blocks_rather_than_raising(self):
        for finding in (None, [], "x", 7, {}, {"classification": {}}):
            with self.subTest(finding=repr(finding)):
                try:
                    blocked = routing.accessibility_findings_block_merge(
                        [finding])
                except Exception as exc:  # noqa: BLE001 - that is the point
                    self.fail(f"{finding!r} raised {type(exc).__name__}: {exc}")
                self.assertTrue(blocked)


class PolicyIsUnchangedCase(unittest.TestCase):

    def test_severity_py_is_still_a_frozen_hash_input(self):
        self.assertIn("control/severity.py", manifest.SEVERITY_POLICY_FILES)
        self.assertIn("protocol/SEVERITY-POLICY.md",
                      manifest.SEVERITY_POLICY_FILES)

    def test_the_blocking_severities_are_untouched(self):
        self.assertEqual(severity.BLOCKING_SEVERITIES, frozenset({"P0", "P1"}))
        self.assertEqual(severity.SEVERITIES, ("P0", "P1", "P2", "P3"))

    def test_supplying_no_registry_still_fails_closed(self):
        # The adjudicator must be supplying a registry, not relying on
        # the policy being lenient without one.
        rated = severity.apply_severity_policy(
            {"classification": "FAILURE", "jev_severity": "P1",
             "unmet_requirement": "ACC-DOD-VISIBLE_FOCUS"},
            known_requirement_ids=None)
        self.assertFalse(rated["valid"])
        self.assertTrue(rated["merge_blocked"])


if __name__ == "__main__":
    unittest.main()
