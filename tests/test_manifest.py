"""C-13: immutable T+00 manifest — hashing, readiness, and frozen-value checks."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import config, gh, manifest  # noqa: E402


def _role(provider, model, escalation_model=None, effort="medium"):
    return types.SimpleNamespace(provider=provider, model=model,
                                 escalation_model=escalation_model, effort=effort)


def _fake_cfg(**overrides):
    roles = {
        "builder": _role("claude", "claude-sonnet-5", "claude-opus-5"),
        "reviewer": _role("codex", None),
    }
    roles.update(overrides)
    return types.SimpleNamespace(roles=roles)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _build_fixture_root(tmp: Path) -> Path:
    """A minimal fixture repo tree with every frozen-input file present,
    isolated from the real repository."""
    for name in manifest.PRODUCT_SPEC_FILES:
        _write(tmp / name, f"content of {name}".encode())
    for name in manifest.SEVERITY_POLICY_FILES:
        _write(tmp / name, f"content of {name}".encode())
    _write(tmp / manifest.PR_TEMPLATE_FILE, b"pr template content")
    _write(tmp / manifest.TASK_GRAPH_FILE, b'{"tasks": []}')
    for name in manifest.REQUIRED_PROMPT_FILES:
        _write(tmp / "prompts" / name, f"prompt {name}".encode())
    return tmp


class TestAggregateHash(unittest.TestCase):
    def test_exact_serialization_and_sort_order(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root / "b.txt", b"second")
            _write(root / "a" / "one.txt", b"first")
            result = manifest.aggregate_hash(
                [root / "b.txt", root / "a" / "one.txt"], root
            )
            h_a = hashlib.sha256(b"first").hexdigest()
            h_b = hashlib.sha256(b"second").hexdigest()
            expected_blob = f"a/one.txt {h_a}\nb.txt {h_b}\n".encode("utf-8")
            expected = hashlib.sha256(expected_blob).hexdigest()
            self.assertEqual(result, expected)

    def test_order_of_input_list_does_not_matter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write(root / "x.txt", b"x")
            _write(root / "y.txt", b"y")
            forward = manifest.aggregate_hash([root / "x.txt", root / "y.txt"], root)
            backward = manifest.aggregate_hash([root / "y.txt", root / "x.txt"], root)
            self.assertEqual(forward, backward)


class TestPromptHashes(unittest.TestCase):
    def test_exactly_six_required_keys(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            result = manifest.prompt_hashes(root / "prompts")
            self.assertEqual(set(result.keys()), set(manifest.REQUIRED_PROMPT_FILES))

    def test_unrelated_extra_md_does_not_alter_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            before = manifest.prompt_hashes(root / "prompts")
            _write(root / "prompts" / "UNRELATED.md", b"not a role prompt")
            after = manifest.prompt_hashes(root / "prompts")
            self.assertEqual(before, after)
            self.assertNotIn("UNRELATED.md", after)

    def test_missing_required_prompt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            (root / "prompts" / "security.md").unlink()
            with self.assertRaises(OSError):
                manifest.prompt_hashes(root / "prompts")


class TestReadinessCheck(unittest.TestCase):
    def test_pass_when_all_frozen_inputs_present(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            result = manifest.readiness_check(_fake_cfg(), root=root)
            self.assertTrue(result.ok, result.detail)
            self.assertIn("providers_and_models", result.fields)
            self.assertIn("product_spec_sha", result.fields)

    def test_fails_when_a_required_prompt_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            (root / "prompts" / "builder.md").unlink()
            result = manifest.readiness_check(_fake_cfg(), root=root)
            self.assertFalse(result.ok)

    def test_fails_when_a_product_spec_file_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            (root / "product" / "PRIVACY.md").unlink()
            result = manifest.readiness_check(_fake_cfg(), root=root)
            self.assertFalse(result.ok)

    def test_does_not_require_or_touch_baseline_sha(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            result = manifest.readiness_check(_fake_cfg(), root=root)
            self.assertNotIn("baseline_sha", result.fields)


class TestFrozenCheck(unittest.TestCase):
    def _current_head(self) -> str:
        result = gh.git(["rev-parse", "HEAD"], str(config.REPO_ROOT))
        self.assertTrue(result.ok)
        return result.stdout.strip()

    def _baseline(self, root: Path, cfg, sha: str) -> dict:
        baseline = manifest.frozen_content_fields(root)
        baseline["providers_and_models"] = manifest.providers_and_models(cfg)
        baseline["baseline_sha"] = sha
        return baseline

    def test_pass_when_nothing_has_drifted(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, self._current_head())
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertTrue(result.ok, result.detail)

    def test_fails_on_content_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, self._current_head())
            _write(root / "product" / "MVP.md", b"changed after freeze")
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("product_spec_sha", result.detail)

    def test_fails_on_model_assignment_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, self._current_head())
            drifted_cfg = _fake_cfg(builder=_role("claude", "claude-opus-5"))
            result = manifest.frozen_check(baseline, drifted_cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("providers_and_models", result.detail)

    def test_fails_when_a_required_field_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, self._current_head())
            del baseline["pr_template_hash"]
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("pr_template_hash", result.detail)

    def test_fails_on_malformed_baseline_sha(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, "not-a-real-sha")
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("baseline_sha", result.detail)

    def test_fails_on_syntactically_valid_nonexistent_sha(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, "f" * 40)
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("does not name an existing commit", result.detail)

    def test_fails_on_valid_sha_naming_a_non_commit_object(self):
        tree_sha = gh.git(["rev-parse", "HEAD^{tree}"], str(config.REPO_ROOT)).stdout.strip()
        self.assertRegex(tree_sha, r"^[0-9a-f]{40}$")
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, tree_sha)
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)
            self.assertIn("does not name an existing commit", result.detail)

    def test_still_placeholder_baseline_sha_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = _build_fixture_root(Path(d))
            cfg = _fake_cfg()
            baseline = self._baseline(root, cfg, "TO_BE_FILLED_AT_FREEZE")
            result = manifest.frozen_check(baseline, cfg, root=root)
            self.assertFalse(result.ok)


class TestAtomicWriteJson(unittest.TestCase):
    def test_round_trip_writes_complete_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "baseline.json"
            data = {"a": 1, "b": {"c": [1, 2, 3]}}
            manifest.atomic_write_json(path, data)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)

    def test_overwrites_existing_file_completely(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "baseline.json"
            manifest.atomic_write_json(path, {"old": True})
            manifest.atomic_write_json(path, {"new": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": True})

    def test_no_temp_file_left_behind_on_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "baseline.json"
            manifest.atomic_write_json(path, {"x": 1})
            leftovers = [p for p in Path(d).iterdir() if p != path]
            self.assertEqual(leftovers, [])


class TestCmdStartFieldContract(unittest.TestCase):
    """Proves the exact shape cmd_start spreads into its baseline dict: no
    key collisions with cmd_start's other baseline fields, and exactly the
    five approved fields, no more, no fewer. This is a contract test
    against cmd_start's real assembly inputs, not a full end-to-end
    invocation of cmd_start itself (which would require mocking git
    status, tmux, the state store, the notifier, telemetry, and the
    preflight record together - disproportionate to this bounded unit;
    see the accompanying report)."""

    EXISTING_BASELINE_KEYS = {
        "experiment_id", "protocol_version", "t_zero", "timezone",
        "duration_hours", "baseline_sha", "github_repo", "main_branch",
        "required_checks", "tool_versions", "providers_and_models",
        "budget", "concurrency", "human_bootstrap_actions", "preflight",
        "environmental_limitations",
    }

    def test_frozen_content_fields_are_exactly_five_and_do_not_collide(self):
        fields = manifest.frozen_content_fields()
        self.assertEqual(
            set(fields.keys()),
            {"product_spec_sha", "task_graph_hash", "prompt_hashes",
             "pr_template_hash", "severity_policy_hash"},
        )
        self.assertEqual(set(fields.keys()) & self.EXISTING_BASELINE_KEYS, set())

    def test_providers_and_models_shape_matches_existing_baseline_field(self):
        cfg = _fake_cfg()
        result = manifest.providers_and_models(cfg)
        for spec in result.values():
            self.assertEqual(
                set(spec.keys()), {"provider", "model", "escalation_model", "effort"}
            )


if __name__ == "__main__":
    unittest.main()
