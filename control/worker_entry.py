"""Worker driver. Runs inside a worktree's tmux window; one job, then exits.

A live process is not progress, so this writes a status file carrying both a
heartbeat and a progress marker (bytes of agent output seen). The supervisor
reads that file rather than guessing from tmux.

The job file - not a giant multiline shell string - carries the prompt path and
role configuration, per the preflight finding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control import proc, redact  # noqa: E402  - path must be set before this import

HEARTBEAT_SECONDS = 10

# C-22 / approval-package action 6 — BUILT AND TESTED, DELIBERATELY NOT WIRED.
#
# `main` below still builds the child environment with `dict(os.environ)`,
# which hands every worker the Supervisor's entire environment. Once the three
# GitHub App principals exist, that pass-through is what defeats the boundary
# no matter what the file modes say: a worker would inherit the supervisor's
# `GH_TOKEN` and the private-key PATHS, and could act as the identity that
# merges. The approval package says the GitHub side cannot substitute for this.
#
# WHY THIS IS A FUNCTION AND NOT A CHANGE TO THE SPAWN. Replacing the
# pass-through is action 6 of the twelve actions that need the operator's
# approval, and it is the one change here that alters how a live worker starts.
# No worker has ever run - T+00 is NOT_STARTED - so which names a real `claude`
# or `codex` child genuinely needs cannot be verified locally. Switching it on
# blind risks a launch-day failure in the dispatch path; leaving the derivation
# unwritten risks approving an OS split that still leaks the credential. So the
# derivation is written and tested, to be switched on with the rest of action 6
# and verified during the rehearsal.
#
# AND SWITCHING IT ON IS NOT, BY ITSELF, THE FIX. `GH_TOKEN` is ON the
# allow-list, because a worker needs its OWN 1-hour installation token. If the
# Supervisor still holds ITS `GH_TOKEN` in `os.environ` when this filter is
# wired, every worker inherits the identity that merges - the precise leak the
# paragraph above describes, surviving the countermeasure. Action 6 is the
# filter PLUS the process/UID split that gives the worker a different token;
# the one-line call-site change on its own buys nothing.
#
# The names come from experiment/github-app/env-var-names.md §3. Everything in
# that document's §2 - every RUN002_*_APP_* id, installation and key PATH - is
# absent by construction, because this is an allow-list and not a deny-list: a
# name added to §2 tomorrow is dropped without anyone remembering to drop it.
WORKER_ENV_ALLOWED: frozenset[str] = frozenset({
    # Enough shell for a child process to run at all.
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TZ",
    # The worker's OWN gh identity - a 1-hour installation token minted for
    # `run-002-worker`, never the supervisor's and never a private key.
    "GH_TOKEN", "GH_CONFIG_DIR",
})


def worker_child_env(parent: dict, job: dict, *, secrets: tuple = ()) -> dict:
    """The environment ONE worker child may see. Allow-list, not deny-list.

    `parent` is the environment to filter (os.environ at the call site).
    `secrets` names the product secrets this role genuinely needs; the caller
    passes them explicitly rather than this function reaching for
    config.REQUIRED_SECRETS, because "which secrets does a worker need" is a
    policy question and policy does not belong in a filter.

    The three per-job names are SET here, never inherited, so a parent that
    happens to carry a stale RUN_001_TASK cannot leak one job's identity into
    another's.
    """
    env = {name: parent[name] for name in WORKER_ENV_ALLOWED if name in parent}
    for name in secrets:
        if name in parent:
            env[name] = parent[name]
    # Every one of these is `str()`-coerced. `RUN_001_ROLE` was not, and a
    # non-string role would have been rejected by `Popen` at spawn time -
    # a crash at the one moment a worker is being started, for a value the
    # job file supplies.
    env["RUN_001_ROLE"] = str(job["role"])
    env["RUN_001_TASK"] = str(job.get("task_id") or "")
    if job.get("port") is not None:
        env["PORT"] = str(job["port"])
    return env


def _now(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


def build_command(job: dict) -> list[str]:
    role = job["role"]
    model = job.get("model")
    worktree = job["worktree"]

    if role in ("builder", "fixer"):
        # Prompt arrives on stdin. Streaming output is not cosmetic: `--print`
        # with a plain text format buffers everything until the run ends, so the
        # supervisor's progress marker never moves and a healthy builder looks
        # stalled. stream-json emits a line per event, which is a real progress
        # signal.
        cmd = ["claude", "--print", "--permission-mode", "bypassPermissions",
               "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        if job.get("fallback_model"):
            cmd += ["--fallback-model", job["fallback_model"]]
        return cmd

    if role == "reviewer":
        cmd = ["codex", "exec", "--sandbox", "read-only", "-C", worktree,
               "--skip-git-repo-check", "-o", job["last_message_path"]]
        if model:
            cmd += ["-m", model]
        if job.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{job["effort"]}"']
        cmd.append("-")  # read the prompt from stdin
        return cmd

    if role == "observer":
        cmd = ["grok", "--prompt-file", job["prompt_path"], "--output-format", "json",
               "--disable-web-search", "--permission-mode", "default",
               "--max-turns", str(job.get("max_turns", 6)), "--cwd", worktree]
        if model:
            cmd += ["-m", model]
        return cmd

    raise ValueError(f"unknown role {role!r}")


def main(job_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    tz = job.get("timezone", "Pacific/Auckland")
    status_path = Path(job["status_path"])
    output_path = Path(job["output_path"])
    prompt_path = Path(job["prompt_path"])
    worktree = job["worktree"]

    status: dict = {
        "worker": job["worker"],
        "role": job["role"],
        "task_id": job.get("task_id"),
        "pr": job.get("pr"),
        "worktree": worktree,
        "provider": job["provider"],
        "model": job.get("model"),
        "phase": "STARTING",
        "pid": os.getpid(),
        "agent_pid": None,
        "started_at": _now(tz),
        "heartbeat_at": _now(tz),
        "output_bytes": 0,
        "prompt_accepted": False,
        "exit_code": None,
        "outcome": None,
        "error_excerpt": None,
        "duration_ms": None,
    }

    def flush() -> None:
        status["heartbeat_at"] = _now(tz)
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp, status_path)

    status_path.parent.mkdir(parents=True, exist_ok=True)
    flush()

    command = build_command(job)
    started = time.monotonic()
    env = dict(os.environ)
    env["RUN_001_ROLE"] = job["role"]
    env["RUN_001_TASK"] = str(job.get("task_id") or "")
    if job.get("port") is not None:
        # C-09: the governed dev-server port, durably assigned in the job
        # file before this process existed. The environment is the real
        # machine-readable interface - it inherits to the agent's children.
        env["PORT"] = str(job["port"])

    with open(output_path, "wb") as sink:
        stdin_data = prompt_path.read_bytes() if job["role"] != "observer" else b""
        process = subprocess.Popen(
            command,
            cwd=worktree,
            stdin=subprocess.PIPE,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
        )
        status["agent_pid"] = process.pid
        # C-09: (pid, start ticks) identifies THIS agent process for the
        # boot's lifetime; a reused PID cannot fake the pair.
        status["agent_start_ticks"] = proc.start_ticks(process.pid)
        status["phase"] = "RUNNING"
        flush()

        def feed() -> None:
            try:
                if process.stdin:
                    process.stdin.write(stdin_data)
                    process.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

        threading.Thread(target=feed, daemon=True).start()

        deadline = time.monotonic() + float(job.get("hard_timeout_seconds", 3600))
        killed_for_timeout = False
        while process.poll() is None:
            time.sleep(HEARTBEAT_SECONDS)
            try:
                size = output_path.stat().st_size
            except OSError:
                size = status["output_bytes"]
            if size > 0 and not status["prompt_accepted"]:
                status["prompt_accepted"] = True
                status["phase"] = "PROMPT_ACCEPTED"
            status["output_bytes"] = size
            flush()
            if time.monotonic() > deadline:
                killed_for_timeout = True
                process.kill()
                break

        exit_code = process.wait()

    status["exit_code"] = exit_code
    status["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    try:
        status["output_bytes"] = output_path.stat().st_size
    except OSError:
        pass
    if status["output_bytes"] > 0:
        status["prompt_accepted"] = True

    tail = ""
    try:
        raw = output_path.read_bytes()[-4000:]
        tail = raw.decode("utf-8", errors="replace")
    except OSError:
        pass

    if killed_for_timeout:
        status["phase"] = "TIMEOUT"
        status["outcome"] = "TIMEOUT"
    elif exit_code == 0:
        status["phase"] = "DONE"
        status["outcome"] = "SUCCESS"
    else:
        status["phase"] = "FAILED"
        status["outcome"] = "FAILED"
        status["error_excerpt"] = redact.scrub(tail[-1200:])

    flush()
    return 0 if status["outcome"] == "SUCCESS" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: worker_entry.py <job.json>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
