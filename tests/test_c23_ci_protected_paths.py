"""The proposed trusted-CI protected path set — derivation CHECKED.

The set is only worth anything if it actually covers what CI executes, so
this re-reads `.github/workflows/ci.yml` and fails when a step references
something no prefix covers. That is the same discipline the accessibility
requirement registry uses: derivation checked, not asserted, so drift
fails the build instead of being discovered later.

PENDING APPROVAL AND NOT WIRED. `NotYetWiredCase` pins that. The rule is a
proposal for the operator, not a change to the merge gate.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import ci_protected_paths as protected

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


class TheDerivationIsCheckedCase(unittest.TestCase):
    """Every protected prefix traces to a step; every step is covered."""

    def test_every_prefix_names_the_step_that_makes_it_load_bearing(self):
        evidence = protected.derivation_evidence()
        self.assertEqual(sorted(evidence), sorted(protected.PROTECTED_PREFIXES),
                         "a prefix exists with no recorded derivation, or a "
                         "derivation exists for a prefix that was removed")
        for prefix, reason in evidence.items():
            self.assertTrue(reason and len(reason) > 20,
                            f"{prefix} has no usable derivation")

    def test_every_path_ci_executes_is_covered_by_a_prefix(self):
        """Re-reads the workflow. A new step that runs something uncovered
        fails here rather than silently widening what CI trusts."""
        text = CI_YML.read_text(encoding="utf-8")
        # Paths the workflow names in a `run:` step, as repo-relative refs.
        referenced = set(re.findall(r"\b((?:tests|scripts|apparatus|control|"
                                    r"protocol|evidence)/[A-Za-z0-9_./-]*)", text))
        self.assertTrue(referenced, "no paths found - the parse is broken, "
                                    "which would make this test vacuous")
        uncovered = sorted(
            p for p in referenced
            if not any(p.startswith(x) for x in protected.PROTECTED_PREFIXES)
            # `evidence/` holds SUBMITTED packages: it is the thing CI
            # judges, not part of what does the judging, and a product PR
            # adding one there is the normal case.
            and not p.startswith("evidence/"))
        self.assertEqual(uncovered, [],
                         f"ci.yml executes or reads {uncovered}, which no "
                         "protected prefix covers")

    def test_the_schema_validate_js_reads_is_inside_a_protected_prefix(self):
        """validate.js reads protocol/PR-EVIDENCE-V2.schema.json."""
        schema = "protocol/PR-EVIDENCE-V2.schema.json"
        self.assertTrue((ROOT / schema).exists(), "the schema moved")
        self.assertTrue(protected.touches_protected([schema])[0])


class WhatItRefusesCase(unittest.TestCase):

    def test_a_change_to_the_workflow_itself_is_refused(self):
        touched, offending = protected.touches_protected(
            [".github/workflows/ci.yml"])
        self.assertTrue(touched)
        self.assertEqual(offending, (".github/workflows/ci.yml",))

    def test_a_change_to_the_suite_ci_runs_is_refused(self):
        for path in ("tests/test_control_plane.py", "control/routing.py",
                     "scripts/check_no_secrets.py",
                     "apparatus/pr-evidence/validate.js",
                     "protocol/PR-EVIDENCE-V2.schema.json"):
            self.assertTrue(protected.touches_protected([path])[0], path)

    def test_the_diagnostic_names_the_remedy_not_just_the_refusal(self):
        reason = protected.amendment_required_reason(["control/routing.py"])
        self.assertIn("CI_TRUST_PATHS_MODIFIED", reason)
        self.assertIn("apparatus amendment", reason)
        self.assertIn("control/routing.py", reason)

    def test_an_unreadable_path_fails_closed(self):
        touched, offending = protected.touches_protected([None, 42, ""])
        self.assertTrue(touched, "a path that cannot be read was cleared")
        self.assertEqual(len(offending), 3)


class WhatItPermitsCase(unittest.TestCase):
    """A rule that refuses everything stops the factory instead of
    protecting it, so what it ALLOWS is tested too."""

    def test_ordinary_product_work_is_not_refused(self):
        touched, offending = protected.touches_protected([
            "package.json", "src/app/page.tsx", "src/components/Timer.tsx",
            "public/icon.png", "README.md", "evidence/TASK-001-R1.json",
        ])
        self.assertFalse(touched, f"ordinary product work was refused: "
                                  f"{offending}")

    def test_the_reason_is_none_for_ordinary_work(self):
        self.assertIsNone(
            protected.amendment_required_reason(["src/app/page.tsx"]))

    def test_an_empty_change_set_is_permitted(self):
        self.assertFalse(protected.touches_protected([])[0])
        self.assertFalse(protected.touches_protected(None)[0])

    def test_a_lookalike_prefix_is_not_refused(self):
        """`apparatus-notes/` is not `apparatus/`."""
        self.assertFalse(protected.touches_protected(
            ["apparatus-notes/x.md", "controller/y.py", "testsuite/z.py"])[0])


class NotYetWiredCase(unittest.TestCase):
    """The honest status: proposed, not in force."""

    def test_no_merge_path_consults_this_rule(self):
        callers = []
        for path in sorted((ROOT / "control").rglob("*.py")):
            if path.name == "ci_protected_paths.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "ci_protected_paths" in text:
                callers.append(path.name)
        self.assertEqual(callers, [],
                         f"{callers} consults the protected-path rule - it "
                         "is a PROPOSAL pending approval, and wiring it is "
                         "a governance change, not an implementation one")


if __name__ == "__main__":
    unittest.main()
