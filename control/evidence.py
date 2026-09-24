"""Independently sourced verification evidence for a review.

Codex reviews in a read-only sandbox with no network, so it cannot fetch CI
results and cannot observe keyboard or mobile behaviour. Without them it must
refuse to accept work it cannot verify - correct, but it leaves acceptance
impossible for reasons that have nothing to do with the code.

This module gathers what Codex cannot reach and hands it over *labelled by
source*, so the reviewer weighs evidence rather than trusting a narrator:

  github-actions  a machine result read from GitHub's API by the supervisor
  human-authority a verification recorded by the experiment authority

Two rules make this safe to feed into an acceptance decision:

1. **Every item is bound to a commit SHA.** Evidence whose SHA does not match
   the head under review is never presented as applicable - it is reported as a
   mismatch, naming both SHAs, so stale evidence cannot authorise acceptance of
   different code.
2. **Agent assertions are never evidence.** Nothing a Builder, Fixer or any
   Claude role said about its own work is collected here. Those are claims, and
   the reviewer already has the diff.

Codex remains the sole authority for REVIEW_PASS and REVIEW_FAIL, and applies
every gate itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import gh

GITHUB = "github-actions"
HUMAN = "human-authority"


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    kind: str
    head: str | None
    applies: bool
    detail: str

    def as_dict(self) -> dict:
        return {"source": self.source, "kind": self.kind, "head": self.head,
                "applies": self.applies, "detail": self.detail[:400]}


def same_commit(a: str | None, b: str | None) -> bool:
    """SHA equality tolerant of abbreviation, but never of a different commit."""
    if not a or not b:
        return False
    shorter, longer = sorted((a.strip().lower(), b.strip().lower()), key=len)
    return len(shorter) >= 7 and longer.startswith(shorter)


def collect(repo: str, head_sha: str, ledger) -> list[EvidenceItem]:
    """Gather independent evidence for exactly this commit."""
    items: list[EvidenceItem] = []
    items.extend(_ci_for_sha(repo, head_sha))
    items.extend(_human_verification(head_sha, ledger))
    return items


def _ci_for_sha(repo: str, head_sha: str) -> list[EvidenceItem]:
    """Check runs reported by GitHub for this exact commit, not for the PR."""
    result = gh.run(["gh", "api", f"repos/{repo}/commits/{head_sha}/check-runs",
                     "--jq", ".check_runs[] | \"\\(.name)|\\(.status)|\\(.conclusion)|"
                             "\\(.head_sha)\""])
    if not result.ok:
        return [EvidenceItem(GITHUB, "ci", head_sha, False,
                             f"CI status could not be read: {result.stderr[:160]}")]
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return [EvidenceItem(GITHUB, "ci", head_sha, False,
                             "GitHub reports no check runs for this commit")]
    items = []
    for line in lines:
        parts = line.split("|")
        if len(parts) != 4:
            continue
        name, status, conclusion, sha = parts
        matches = same_commit(sha, head_sha)
        items.append(EvidenceItem(
            GITHUB, "ci", sha, matches,
            f"check '{name}': {status}/{conclusion}" if matches else
            f"check '{name}' belongs to {sha}, not the head under review {head_sha}",
        ))
    return items


def _human_verification(head_sha: str, ledger) -> list[EvidenceItem]:
    """Verification recorded by the experiment authority, bound to a SHA."""
    items: list[EvidenceItem] = []
    for event in ledger.events("HUMAN_VERIFICATION"):
        meta = event.get("metadata_redacted") or {}
        recorded = meta.get("verified_head")
        matches = same_commit(recorded, head_sha)
        summary = []
        for key in ("mobile_375px", "keyboard_focus", "primary_action_hierarchy"):
            block = meta.get(key)
            if isinstance(block, dict):
                summary.append(f"{key}={block.get('result')} ({block.get('items', '')})"
                               .replace(" ()", ""))
        detail = (f"verified by {meta.get('verified_by', 'human')} at {recorded}: "
                  + "; ".join(summary) if matches else
                  f"human verification recorded against {recorded}, which is NOT the "
                  f"head under review {head_sha}")
        if matches and meta.get("method"):
            detail += f". Method: {meta['method']}"
        items.append(EvidenceItem(HUMAN, "manual-verification", recorded, matches,
                                  detail))
    return items


def render(items: list[EvidenceItem], head_sha: str) -> str:
    """Format for the reviewer prompt. Applicability is stated, never implied."""
    applicable = [i for i in items if i.applies]
    mismatched = [i for i in items if not i.applies]

    lines = [
        f"The following evidence was gathered by the deterministic Supervisor for "
        f"commit `{head_sha}`, the exact head you are reviewing. Each item names its "
        f"source. Weigh it as evidence; you remain the sole authority for the verdict.",
        "",
    ]
    if applicable:
        lines.append("### Independent evidence for this exact commit")
        for item in applicable:
            lines.append(f"- **[{item.source}]** ({item.kind}) {item.detail}")
    else:
        lines.append("### Independent evidence for this exact commit")
        lines.append("- none available")

    if mismatched:
        lines.append("")
        lines.append("### Not applicable — do not rely on these")
        for item in mismatched:
            lines.append(f"- **[{item.source}]** ({item.kind}) {item.detail}")

    lines += [
        "",
        "No Builder, Fixer or other Claude assertion appears above; agent claims about "
        "their own work are not evidence. Anything not listed here remains unverified, "
        "and unverified is not the same as acceptable.",
    ]
    return "\n".join(lines)
