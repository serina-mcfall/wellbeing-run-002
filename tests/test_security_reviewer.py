"""B2a: Security Reviewer role/prompt/config exist as an identifiable,
independent evidence producer — distinct from Codex Reviewer, and not
itself an acceptance/merge authority."""

import unittest
from pathlib import Path

from control import config, prompts

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSecurityRoleConfig(unittest.TestCase):
    def test_security_role_exists_in_real_config(self):
        cfg = config.load()
        self.assertIn("security", cfg.roles)
        role = cfg.roles["security"]
        self.assertTrue(role.provider)

    def test_security_is_its_own_configured_role(self):
        """Security is a separately-keyed role entry, not an alias or a
        second copy of reviewer's config under another name. Whether the
        two happen to share a provider/model is not the independence
        property — dispatch/evidence provenance tests (later) prove that."""
        cfg = config.load()
        self.assertIn("security", cfg.roles)
        self.assertIn("reviewer", cfg.roles)
        self.assertIsNot(cfg.roles["security"], cfg.roles["reviewer"])


class TestSecurityPromptRendering(unittest.TestCase):
    def _task(self):
        return {"id": "TASK-004", "title": "Support"}

    def test_security_prompt_renders_with_no_placeholders_left(self):
        text = prompts.security(self._task(), 12, "task/task-004", "org/repo", 1,
                                evidence="- none gathered for this commit")
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)
        self.assertIn("TASK-004", text)
        self.assertIn("PR #12", text)

    def test_security_prompt_identifies_itself_as_security_reviewer(self):
        text = prompts.security(self._task(), 12, "task/task-004", "org/repo", 1)
        self.assertIn("Security Reviewer", text)

    def test_security_prompt_uses_its_own_verdict_vocabulary(self):
        text = prompts.security(self._task(), 12, "task/task-004", "org/repo", 1)
        self.assertIn("SECURITY_PASS", text)
        self.assertIn("SECURITY_FAIL", text)
        # Must not reuse Codex's verdict vocabulary, which would blur the
        # two producers' identities together.
        self.assertNotIn("REVIEW_PASS", text)
        self.assertNotIn("REVIEW_FAIL", text)

    def test_security_prompt_states_it_is_not_the_merge_authority(self):
        text = prompts.security(self._task(), 12, "task/task-004", "org/repo", 1)
        self.assertIn("not an independent merge or acceptance authority",
                     " ".join(text.split()))


class TestSecurityRoleContractDoc(unittest.TestCase):
    def _doc_text(self):
        return (REPO_ROOT / "agents" / "SECURITY.md").read_text(encoding="utf-8")

    def test_canonical_severity_semantics_are_present(self):
        text = self._doc_text()
        self.assertIn("P0 and P1 findings are blocking", text)
        self.assertIn("do not independently block merge", text)
        self.assertIn("Deterministic security floors may raise", text)
        self.assertIn("fails closed independently of severity", text)

    def test_it_is_not_described_as_an_acceptance_authority(self):
        text = self._doc_text()
        self.assertIn("not an independent merge or acceptance authority",
                     " ".join(text.split()))
        self.assertNotIn("is the acceptance authority", text)


if __name__ == "__main__":
    unittest.main()
