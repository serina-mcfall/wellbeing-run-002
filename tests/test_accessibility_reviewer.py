"""B1: Accessibility Worker/Reviewer role/prompt/config exist as an
identifiable, independent evidence producer — distinct from Codex Reviewer
and Security Reviewer, not itself an acceptance/merge authority. Scoped to
the qualitative review only; automated Playwright+axe evidence is a
separate, not-yet-authorised dependency (see the recorded finding)."""

import unittest
from pathlib import Path

from control import config, prompts

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestAccessibilityRoleConfig(unittest.TestCase):
    def test_accessibility_role_exists_in_real_config(self):
        cfg = config.load()
        self.assertIn("accessibility", cfg.roles)
        role = cfg.roles["accessibility"]
        self.assertTrue(role.provider)

    def test_accessibility_is_its_own_configured_role(self):
        cfg = config.load()
        self.assertIn("accessibility", cfg.roles)
        self.assertIn("security", cfg.roles)
        self.assertIn("reviewer", cfg.roles)
        self.assertIsNot(cfg.roles["accessibility"], cfg.roles["security"])
        self.assertIsNot(cfg.roles["accessibility"], cfg.roles["reviewer"])


class TestAccessibilityPromptRendering(unittest.TestCase):
    def _task(self):
        return {"id": "TASK-002", "title": "Check-in"}

    def test_accessibility_prompt_renders_with_no_placeholders_left(self):
        text = prompts.accessibility(self._task(), 7, "task/task-002", "org/repo", 1,
                                     evidence="- none gathered for this commit")
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)
        self.assertIn("TASK-002", text)
        self.assertIn("PR #7", text)

    def test_accessibility_prompt_identifies_itself(self):
        text = prompts.accessibility(self._task(), 7, "task/task-002", "org/repo", 1)
        self.assertIn("Accessibility Reviewer", text)

    def test_accessibility_prompt_uses_its_own_verdict_vocabulary(self):
        text = prompts.accessibility(self._task(), 7, "task/task-002", "org/repo", 1)
        self.assertIn("ACCESSIBILITY_PASS", text)
        self.assertIn("ACCESSIBILITY_FAIL", text)
        self.assertNotIn("REVIEW_PASS", text)
        self.assertNotIn("SECURITY_PASS", text)

    def test_accessibility_prompt_states_it_is_not_the_merge_authority(self):
        text = " ".join(prompts.accessibility(
            self._task(), 7, "task/task-002", "org/repo", 1).split())
        self.assertIn("not an independent merge or acceptance authority", text)

    def test_accessibility_prompt_discloses_the_automation_gap(self):
        """Must not silently claim automated coverage it cannot yet perform."""
        text = prompts.accessibility(self._task(), 7, "task/task-002", "org/repo", 1)
        self.assertIn("not yet implemented", text)


class TestAccessibilityRoleContractDoc(unittest.TestCase):
    def _doc_text(self):
        return " ".join(
            (REPO_ROOT / "agents" / "ACCESSIBILITY.md").read_text(encoding="utf-8").split()
        )

    def test_canonical_severity_semantics_are_present(self):
        text = self._doc_text()
        self.assertIn("P0 and P1 findings are blocking", text)
        self.assertIn("do not independently block merge", text)
        self.assertIn("deterministic minimum P1 floor", text)
        self.assertIn("fails closed independently of severity", text)

    def test_it_is_not_described_as_an_acceptance_authority(self):
        text = self._doc_text()
        self.assertIn("not an independent merge or acceptance authority", text)
        self.assertNotIn("is the acceptance authority", text)


if __name__ == "__main__":
    unittest.main()
