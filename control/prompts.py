"""Prompt assembly.

Prompts are files, not giant multiline shell strings - a preflight finding. Each
role template is filled with task context and written into the worker's own
prompt file before dispatch. Nothing secret is ever interpolated.
"""

from __future__ import annotations

from pathlib import Path

from . import config, redact


def _template(name: str) -> str:
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(name: str, **values) -> str:
    text = _template(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return redact.scrub(text)


def write(worker: str, text: str) -> Path:
    config.WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.WORKER_LOG_DIR / f"{worker}.prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def builder(task: dict, branch: str, repo: str, base: str, extra: str = "") -> str:
    return render(
        "builder",
        task_id=task["id"],
        task_title=task["title"],
        task_body=task.get("body", ""),
        branch=branch,
        repo=repo,
        base=base,
        depends_on=", ".join(task["depends_on"]) or "none",
        migration_note=(
            "This task may change the database schema and you HOLD the migration lock."
            if task.get("schema_changing") else
            "This task must not change the database schema."
        ),
        extra=extra,
    )


def fixer(task: dict, pr_number: int, branch: str, repo: str, findings: list[dict]) -> str:
    lines = []
    for finding in findings:
        lines.append(
            f"- [{finding.get('id')}] {finding.get('severity')} "
            f"{finding.get('category', '')} {finding.get('file', '')}: "
            f"{finding.get('summary', '')}"
        )
    return render(
        "fixer",
        task_id=task["id"],
        pr=pr_number,
        branch=branch,
        repo=repo,
        finding_ids=", ".join(str(f.get("id")) for f in findings),
        findings="\n".join(lines) or "- (none supplied)",
    )


def reviewer(task: dict, pr_number: int, branch: str, repo: str, cycle: int,
             evidence: str = "") -> str:
    return render(
        "reviewer",
        task_id=task["id"],
        task_title=task["title"],
        pr=pr_number,
        branch=branch,
        repo=repo,
        cycle=cycle,
        evidence=evidence or "- none gathered for this commit",
    )


def security(task: dict, pr_number: int, branch: str, repo: str, cycle: int,
             evidence: str = "") -> str:
    return render(
        "security",
        task_id=task["id"],
        task_title=task["title"],
        pr=pr_number,
        branch=branch,
        repo=repo,
        cycle=cycle,
        evidence=evidence or "- none gathered for this commit",
    )


def accessibility(task: dict, pr_number: int, branch: str, repo: str, cycle: int,
                  evidence: str = "") -> str:
    return render(
        "accessibility",
        task_id=task["id"],
        task_title=task["title"],
        pr=pr_number,
        branch=branch,
        repo=repo,
        cycle=cycle,
        evidence=evidence or "- none gathered for this commit",
    )


def observer(evidence_path: str, window_label: str) -> str:
    return render("observer", evidence_path=evidence_path, window=window_label)
