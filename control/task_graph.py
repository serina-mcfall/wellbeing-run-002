"""Task graph validation (Protocol v2 "Task graph").

Reads config/tasks.json (or accepts an in-memory fixture for testing) and
checks: every depends_on entry references a real task id (no dangling
dependency); no task depends on itself; no cycle exists; no duplicate
task id.

Ported from the Run 002 apparatus/task-graph/validate.js implementation
(same checks, same error codes) so the Python control plane does not need
a Node dependency to run this gate. Does NOT judge whether a dependency is
"necessary" — Protocol v2 also requires "no dependency added merely to
serialize" and "no dependency removed merely to avoid blocking," which
need human/product judgement this module does not have.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_all_tasks(repo_root: str) -> list:
    tasks_path = Path(repo_root) / "config" / "tasks.json"
    graph = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks = graph.get("tasks")
    return tasks if isinstance(tasks, list) else []


def validate_task_graph(tasks: list | None = None, repo_root: str | None = None) -> dict:
    if tasks is None:
        if not repo_root or not str(repo_root).strip():
            return {"ok": False,
                    "errors": ["INVALID_OPTIONS: repo_root (or tasks) is required."],
                    "task_count": 0}
        try:
            tasks = _load_all_tasks(repo_root)
        except (OSError, ValueError):
            return {"ok": False,
                    "errors": ["NO_TASK_GRAPH: could not read config/tasks.json."],
                    "task_count": 0}

    if len(tasks) == 0:
        return {"ok": False,
                "errors": ["EMPTY_TASK_GRAPH: no tasks found — this may indicate a "
                           "malformed or missing tasks array, not a genuinely empty graph."],
                "task_count": 0}

    errors: list[str] = []
    by_id: dict[str, dict] = {}
    for t in tasks:
        task_id = t.get("id") if isinstance(t, dict) else None
        if not isinstance(task_id, str):
            errors.append("MALFORMED_TASK: an entry has no string id.")
            continue
        if task_id in by_id:
            errors.append(f'DUPLICATE_TASK_ID: "{task_id}" appears more than once.')
        by_id[task_id] = t

    for task_id, t in by_id.items():
        depends_on = t.get("depends_on")
        if not isinstance(depends_on, list):
            errors.append(f'MALFORMED_DEPENDENCIES: "{task_id}".depends_on is missing '
                           "or not a list.")
            continue
        for dep in depends_on:
            if not isinstance(dep, str) or not dep.strip():
                errors.append(f'MALFORMED_DEPENDENCY_ENTRY: "{task_id}" has a non-string '
                              "or empty dependency entry.")
                continue
            if dep == task_id:
                errors.append(f'SELF_DEPENDENCY: "{task_id}" depends on itself.')
            elif dep not in by_id:
                errors.append(f'DANGLING_DEPENDENCY: "{task_id}" depends on unknown '
                              f'task "{dep}".')

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in by_id}

    def visit(task_id: str, stack: list[str]) -> None:
        color[task_id] = GRAY
        stack.append(task_id)
        t = by_id.get(task_id)
        deps = t.get("depends_on") if isinstance(t, dict) else None
        deps = deps if isinstance(deps, list) else []
        for dep in deps:
            if dep not in by_id:
                continue  # already reported as dangling
            if color.get(dep) == GRAY:
                cycle_start = stack.index(dep)
                errors.append("CYCLE: " + " -> ".join(stack[cycle_start:] + [dep]))
            elif color.get(dep) == WHITE:
                visit(dep, stack)
        stack.pop()
        color[task_id] = BLACK

    for task_id in list(by_id.keys()):
        if color.get(task_id) == WHITE:
            visit(task_id, [])

    return {"ok": len(errors) == 0, "errors": errors, "task_count": len(by_id)}


def validate_run002_task_graph() -> dict:
    from . import config
    return validate_task_graph(repo_root=str(config.REPO_ROOT))
