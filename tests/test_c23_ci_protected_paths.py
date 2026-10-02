"""The proposed trusted-CI protected path set — derivation CHECKED.

The set is only worth anything if it actually covers what CI executes, so
this re-reads `.github/workflows/ci.yml` and fails when a step references
something no prefix covers. That is the same discipline the accessibility
requirement registry uses: derivation checked, not asserted, so drift
fails the build instead of being discovered later.

APPROVED AND WIRED (C-23a). `ItIsWiredCase` pins that - it used to be
`NotYetWiredCase` and pinned the opposite, while the rule was still a
proposal. What the merge path does with the rule is proved in
`tests/test_c23_merge_path_protection.py`; this file stays about the
vocabulary and its derivation.
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


class ItIsWiredCase(unittest.TestCase):
    """The honest status, UPDATED: approved by the operator and in force.

    This class used to be `NotYetWiredCase` and asserted the exact
    opposite - that NO control module consulted the rule - because the rule
    was a proposal and wiring it was a governance change nobody had made.
    The operator has now approved it:

        "Ordinary product PRs cannot change trusted CI or its validation
         machinery; those changes require a separately reviewed apparatus
         amendment."

    So the pin is inverted rather than deleted. A rule that silently stops
    being wired is exactly as dangerous as one that is wired before it is
    approved, and this is the test that would notice either.
    """

    def test_the_merge_path_consults_this_rule(self):
        callers = set()
        for path in sorted((ROOT / "control").rglob("*.py")):
            if path.name == "ci_protected_paths.py":
                continue
            if "ci_protected_paths" in path.read_text(encoding="utf-8"):
                callers.add(path.name)
        self.assertIn("routing.py", callers,
                      "the merge gate module no longer consults the "
                      "protected-path rule")
        self.assertIn("supervisor.py", callers,
                      "supervisor.attempt_merge - the sole merge authority's "
                      "call site - no longer consults the protected-path rule")

    def test_the_refusal_is_reached_from_attempt_merge_before_the_gate(self):
        """Ordering, read from the source. The protected-path refusal must
        sit BEFORE `evaluate_merge`, or a pull request could be merged by a
        gate that never saw the file list."""
        text = (ROOT / "control" / "supervisor.py").read_text(encoding="utf-8")
        body = text[text.index("def attempt_merge("):]
        body = body[:body.index("def complete_task(")]
        self.assertIn("ci_paths_clear_for_merge", body)
        self.assertLess(body.index("ci_paths_clear_for_merge"),
                        body.index("routing.evaluate_merge("),
                        "the protected-path check runs after the merge gate")


if __name__ == "__main__":
    unittest.main()
