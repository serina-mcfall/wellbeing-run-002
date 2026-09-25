"""C-13: the immutable T+00 manifest — content hashes and frozen-value checks.

Protocol v2 requires "immutable manifest/hashes" verified before T+00 is
permitted (§"Preflight"), and that frozen inputs stay frozen afterward
(§"Freeze"). This module computes the required hashes and exposes two
distinct checks:

  readiness_check() - pre-freeze. Proves every frozen-input hash is
  computable right now. There is nothing to compare against yet, since no
  baseline artifact exists before `ctl start` runs; this never touches
  baseline_sha, which legitimately remains a placeholder until freeze.

  frozen_check() - post-freeze. Recomputes every frozen input live and
  compares it against the values recorded in the baseline artifact
  (config.BASELINE_PATH, written by `ctl start`). Every mismatch fails
  closed. baseline_sha is checked for presence, shape, and that it names a
  real commit object (via Git commit-peeling, not mere object existence) -
  never compared against current HEAD, which legitimately advances after
  T+00.

This module does not generate a second manifest artifact. Per governance
decision (2026-09-25), the existing `ctl start` / config.BASELINE_PATH
baseline *is* the concrete frozen manifestation of
protocol/experiment-manifest.template.json's declarative contract -
providers_and_models is that baseline's existing representation of the
template's conceptual "model_assignments" field; it is not renamed or
duplicated. The role-capability/liveness half of that requirement is
proven separately, by the existing gate_claude_heartbeat/gate_codex_review/
gate_grok_observer/gate_jev gates - this module records identifiers only,
never liveness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, gh

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

PRODUCT_SPEC_FILES: tuple[str, ...] = (
    "product/MVP.md",
    "product/ACCESSIBILITY.md",
    "product/AI.md",
    "product/ARCHITECTURE.md",
    "product/DESIGN-SYSTEM.md",
    "product/PRIVACY.md",
)

SEVERITY_POLICY_FILES: tuple[str, ...] = (
    "protocol/SEVERITY-POLICY.md",
    "control/severity.py",
)

PR_TEMPLATE_FILE = ".github/PULL_REQUEST_TEMPLATE.md"
TASK_GRAPH_FILE = "config/tasks.json"

# The frozen contract is exactly these six current role prompts - an
# explicit list, not a glob. An unrelated .md file later added to prompts/
# must never silently expand this mapping, and a missing required prompt
# must fail readiness rather than simply disappear from it.
REQUIRED_PROMPT_FILES: tuple[str, ...] = (
    "accessibility.md",
    "builder.md",
    "fixer.md",
    "observer.md",
    "reviewer.md",
    "security.md",
)


def hash_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate_hash(paths: list[Path], root: Path) -> str:
    """Deterministic aggregate over several files.

    Sorts by repo-relative path, then SHA-256s the exact byte sequence of
    "<relative-path><space><lowercase hex>\\n" per file, UTF-8, one line
    each, in sorted order.
    """
    rel_paths = sorted(p.relative_to(root).as_posix() for p in paths)
    lines = [f"{rel} {hash_bytes(root / rel)}\n" for rel in rel_paths]
    blob = "".join(lines).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def product_spec_sha(root: Path | None = None) -> str:
    root = root or config.REPO_ROOT
    return aggregate_hash([root / f for f in PRODUCT_SPEC_FILES], root)


def severity_policy_hash(root: Path | None = None) -> str:
    root = root or config.REPO_ROOT
    return aggregate_hash([root / f for f in SEVERITY_POLICY_FILES], root)


def pr_template_hash(root: Path | None = None) -> str:
    root = root or config.REPO_ROOT
    return hash_bytes(root / PR_TEMPLATE_FILE)


def task_graph_hash(root: Path | None = None) -> str:
    root = root or config.REPO_ROOT
    return hash_bytes(root / TASK_GRAPH_FILE)


def prompt_hashes(prompts_dir: Path | None = None) -> dict[str, str]:
    """Exactly the six required role prompts, by explicit name.

    Not a glob: an unrelated .md file added later to prompts/ must not
    alter this mapping, and a missing required prompt must raise (caught
    by the callers below as a readiness/frozen-check failure) rather than
    silently vanish from the result.
    """
    prompts_dir = prompts_dir or (config.REPO_ROOT / "prompts")
    return {name: hash_bytes(prompts_dir / name) for name in REQUIRED_PROMPT_FILES}


def frozen_content_fields(root: Path | None = None) -> dict:
    """The five precomputable content hashes, recomputed fresh, right now."""
    root = root or config.REPO_ROOT
    return {
        "product_spec_sha": product_spec_sha(root),
        "task_graph_hash": task_graph_hash(root),
        "prompt_hashes": prompt_hashes(root / "prompts"),
        "pr_template_hash": pr_template_hash(root),
        "severity_policy_hash": severity_policy_hash(root),
    }


def providers_and_models(cfg) -> dict:
    """The template's conceptual "model_assignments", concretely: the
    existing baseline artifact's providers_and_models field. Faithfully
    records deliberate nulls; never invents a resolved model to fill one."""
    return {
        role: {"provider": spec.provider, "model": spec.model,
               "escalation_model": spec.escalation_model, "effort": spec.effort}
        for role, spec in cfg.roles.items()
    }


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str
    fields: dict = field(default_factory=dict)


def readiness_check(cfg, root: Path | None = None) -> CheckResult:
    """Pre-freeze: every frozen-input hash computes successfully right now.

    Does not require or compute baseline_sha - "TO_BE_FILLED_AT_FREEZE" is
    the one permitted pre-freeze placeholder, filled only at `ctl start`.
    """
    try:
        fields = frozen_content_fields(root)
        fields["providers_and_models"] = providers_and_models(cfg)
    except OSError as exc:
        return CheckResult(False, f"could not compute frozen-input hashes: {exc}")
    return CheckResult(True, "all frozen-input hashes computed successfully", fields)


def frozen_check(baseline: dict, cfg, root: Path | None = None) -> CheckResult:
    """Post-freeze: does everything still recompute to the recorded baseline?

    Every disagreement fails closed and is named explicitly. baseline_sha is
    checked for presence, exact 40-hex shape, and that it names a real
    *commit* object - via Git commit-peeling (<sha>^{commit}), which fails
    if the SHA is well-formed but names a non-commit object (a blob or
    tree) or doesn't exist at all. Never compared against current HEAD,
    which legitimately advances after T+00.
    """
    problems: list[str] = []

    baseline_sha = baseline.get("baseline_sha")
    if not baseline_sha or baseline_sha == "TO_BE_FILLED_AT_FREEZE":
        problems.append("baseline_sha is missing or still the pre-freeze placeholder")
    elif not SHA_RE.match(baseline_sha):
        problems.append(f"baseline_sha {baseline_sha!r} is not a full 40-hex Git SHA")
    else:
        exists = gh.git(["cat-file", "-e", f"{baseline_sha}^{{commit}}"],
                        str(config.REPO_ROOT))
        if not exists.ok:
            problems.append(
                f"baseline_sha {baseline_sha!r} does not name an existing commit object"
            )

    try:
        live = frozen_content_fields(root)
    except OSError as exc:
        return CheckResult(False, f"could not recompute frozen-input hashes: {exc}")

    for name in ("product_spec_sha", "task_graph_hash", "pr_template_hash",
                 "severity_policy_hash"):
        recorded = baseline.get(name)
        if recorded is None:
            problems.append(f"{name} is missing from the recorded baseline")
        elif recorded != live[name]:
            problems.append(f"{name} drifted: recorded {recorded!r}, now {live[name]!r}")

    recorded_prompts = baseline.get("prompt_hashes")
    if recorded_prompts is None:
        problems.append("prompt_hashes is missing from the recorded baseline")
    elif recorded_prompts != live["prompt_hashes"]:
        problems.append("prompt_hashes drifted from the recorded baseline")

    live_models = providers_and_models(cfg)
    recorded_models = baseline.get("providers_and_models")
    if recorded_models is None:
        problems.append("providers_and_models is missing from the recorded baseline")
    elif recorded_models != live_models:
        problems.append(
            "providers_and_models (model assignments) drifted from the recorded baseline"
        )

    if problems:
        return CheckResult(False, "; ".join(problems))
    return CheckResult(True, "all frozen inputs match the recorded baseline")


def atomic_write_json(path: Path, data: dict) -> None:
    """Same durability pattern as control/state.py::Store._write: a
    same-directory temp file, flushed and fsynced, then renamed into place.
    A crash mid-write leaves the prior file intact, never a truncated one.

    Does not create path.parent. By the time cmd_start can reach this call,
    it has already required config.PREFLIGHT_PATH to exist, and
    Preflight.run() always calls config.ensure_runtime_dirs() before
    writing that file - so path.parent (RUNTIME_DIR) is already guaranteed
    to exist. Creating it defensively here would silently paper over that
    precondition ever being violated instead of surfacing it.
    """
    directory = path.parent
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=False, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)
