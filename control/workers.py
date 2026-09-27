"""tmux / workmux worker lifecycle and readiness gates.

Startup order matters: the master tmux session must exist before workmux creates
window-mode workers. A created worktree is not a ready worker - readiness needs
worktree + workspace trust + the expected process + prompt accepted + a fresh
heartbeat.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, gh, hostcheck, proc, redact

HEARTBEAT_STALE_SECONDS = 90


@dataclass(frozen=True)
class Readiness:
    worker: str
    worktree_present: bool
    trust_established: bool
    process_running: bool
    prompt_accepted: bool
    heartbeat_fresh: bool
    phase: str

    @property
    def ready(self) -> bool:
        return all(
            (
                self.worktree_present,
                self.trust_established,
                self.process_running,
                self.prompt_accepted,
                self.heartbeat_fresh,
            )
        )

    def as_dict(self) -> dict:
        return {
            "worker": self.worker,
            "worktree_present": self.worktree_present,
            "trust_established": self.trust_established,
            "process_running": self.process_running,
            "prompt_accepted": self.prompt_accepted,
            "heartbeat_fresh": self.heartbeat_fresh,
            "phase": self.phase,
            "ready": self.ready,
        }


def tmux(args: list[str], timeout: int = 20) -> gh.Result:
    return gh.run(["tmux", *args], timeout=timeout)


def ensure_session(name: str) -> bool:
    """Master tmux session must exist before any workmux window-mode worker."""
    if tmux(["has-session", "-t", name]).ok:
        return True
    created = tmux(["new-session", "-d", "-s", name, "-c", str(config.REPO_ROOT)])
    return created.ok


def session_healthy(name: str) -> bool:
    return tmux(["has-session", "-t", name]).ok


def workmux(args: list[str], timeout: int = 180) -> gh.Result:
    return gh.run(["workmux", *args], cwd=str(config.REPO_ROOT), timeout=timeout)


def list_worktrees() -> list[dict]:
    result = workmux(["list", "--json"], timeout=60)
    data = result.json()
    if isinstance(data, dict):
        return data.get("worktrees", [])
    return data or []


def worktree_path(name: str) -> Path | None:
    result = workmux(["path", name], timeout=30)
    if not result.ok or not result.stdout:
        return None
    path = Path(result.stdout.strip())
    return path if path.exists() else None


def create_worker(name: str, branch: str, prompt_path: Path, session: str,
                  base: str = "main") -> gh.Result:
    """Create the worktree and its tmux window with a plain shell.

    Pane commands are skipped deliberately: the supervisor starts the agent
    itself through `workmux run`, so the process it expects is the process it
    launched.
    """
    return workmux(
        [
            "add", branch,
            "--name", name,
            "--base", base,
            "--parent-session", session,
            "--prompt-file", str(prompt_path),
            "--prompt-file-only",
            "--background",
            "--no-pane-cmds",
            "--open-if-exists",
        ]
    )


def pane_for_worktree(worktree: Path) -> str | None:
    """The tmux pane whose working directory is this worktree.

    workmux decorates window names with a status icon, so matching by name is
    brittle; the pane's own path is exact.
    """
    listing = tmux(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} "
                                   "#{pane_current_path}"]
    )
    if not listing.ok:
        return None
    target = str(worktree.resolve())
    for line in listing.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == target:
            return parts[0]
    return None


def start_job(name: str, job_path: Path, worktree: Path) -> gh.Result:
    """Launch the worker inside its own tmux pane, so tmux owns the process.

    Falls back to a detached process if the pane cannot be found, because a
    missing pane must not silently mean a missing worker.
    """
    entry = config.REPO_ROOT / "bin" / "worker-entry.sh"
    command = f"{entry} {job_path}"

    target = pane_for_worktree(worktree)
    if target:
        sent = tmux(["send-keys", "-t", target, command, "C-m"])
        if sent.ok:
            return sent

    try:
        log_path = config.WORKER_LOG_DIR / f"{name}.entry.log"
        with open(log_path, "ab") as log:
            subprocess.Popen(
                ["bash", str(entry), str(job_path)], cwd=str(worktree),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        return gh.Result(True, f"started detached (no tmux pane for {worktree})", "", 0)
    except OSError as exc:
        return gh.Result(False, "", str(exc), 1)


def worktree_for_branch(branch: str) -> Path | None:
    """The existing git worktree that has this branch checked out, if any.

    Git allows a branch in exactly one worktree. A Fixer must commit to the
    Builder's PR branch, so it works in the worktree that already holds it
    rather than trying to check the branch out a second time.
    """
    result = gh.git(["worktree", "list", "--porcelain"], str(config.REPO_ROOT))
    if not result.ok:
        return None
    current: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.split(" ", 1)[1].strip())
        elif line.startswith("branch ") and current is not None:
            ref = line.split(" ", 1)[1].strip()
            if ref in (f"refs/heads/{branch}", branch) and current.exists():
                return current
    return None


def acquire_worktree(name: str, branch: str, base: str, session: str, prompt_path: Path,
                     *, reuse_if_checked_out: bool) -> tuple[Path | None, str]:
    """Obtain a worktree for one role dispatch, or say why it could not.

    This is the dispatch/worktree invariant in one place. Git allows a branch in
    exactly one worktree, so a role either works *on* an existing branch and must
    reuse the worktree already holding it (the Fixer, which commits to the PR
    branch), or needs its own checkout and must be given a branch nobody else
    holds (the Reviewer, which gets a fresh branch per cycle).

    Every caller gets a path or a reason. Returning neither - the silent failure
    that stranded TASK-001 twice - is not possible here.
    """
    if reuse_if_checked_out:
        existing = worktree_for_branch(branch)
        if existing is not None:
            return existing, ""

    holder = worktree_for_branch(branch)
    if holder is not None:
        return None, (f"branch {branch} is already checked out at {holder}; a branch "
                      f"cannot be checked out in two worktrees")

    created = create_worker(name, branch, prompt_path, session, base)
    path = worktree_path(name) if created.ok else None
    if path is not None:
        return path, ""
    return None, (f"no worktree for branch {branch} from base {base} "
                  f"(create ok={created.ok}): {created.stderr[:200]}")


def managed_worktree_root(repo_root: Path | None = None) -> Path:
    """The directory workmux creates this repository's worktrees in.

    workmux's default worktree_dir is the sibling directory
    '<project>__worktrees' (verified via `workmux config reference`; the
    global config sets no override and the repository carries none).
    Reverse orphan detection scopes to this root so a manual or unrelated
    git worktree of the repository is never classified as a Run 002 orphan.
    """
    root = Path(repo_root) if repo_root is not None else config.REPO_ROOT
    return root.parent / f"{root.name}__worktrees"


def _reserved_job_file_ports() -> set[int]:
    """Ports durably promised in job files whose worker may still be using
    them (C-09). The job file is written before spawn and is the port's
    durable pre-commit owner, so a Supervisor crash between spawn and state
    commit cannot orphan a port into reassignment. Fail-closed: a failed
    /proc scan, or a live-but-unverifiable recorded agent, retains the port.
    """
    entries = proc.worker_entry_processes(config.WORKER_LOG_DIR)
    reserved: set[int] = set()
    for job_path in config.WORKER_LOG_DIR.glob("*.job.json"):
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # unreadable job file names no port to protect
        port = job.get("port")
        if not isinstance(port, int):
            continue
        worker = job_path.name[: -len(".job.json")]
        if entries is None:  # /proc scan failed - cannot prove anything absent
            reserved.add(port)
            continue
        if worker in entries:  # live worker-entry still owns the job
            reserved.add(port)
            continue
        status_path = config.WORKER_LOG_DIR / f"{worker}.status.json"
        if not status_path.exists():
            # Entry dead before its first status flush: the agent is only
            # spawned after that flush, so nothing can be using the port.
            continue
        status = read_status(worker)
        if status is None:  # exists but unreadable - fail closed
            reserved.add(port)
            continue
        if status.get("phase") in ("DONE", "FAILED", "TIMEOUT"):
            continue
        alive = proc.verified_alive(status.get("agent_pid"),
                                    status.get("agent_start_ticks"))
        if alive is not False:  # True, or ambiguous - fail closed
            reserved.add(port)
    return reserved


def allocate_port(doc: dict, lo: int | None = None,
                  hi: int | None = None) -> tuple[int | None, str]:
    """One governed dev-server port, or an explicit reason (C-09).

    Excluded: ports owned by committed worker records, ports reserved by a
    job file whose worker-entry or recorded agent may still be live (see
    _reserved_job_file_ports), and ports that fail a real 127.0.0.1 bind
    right now. The bind-test-to-builder-bind TOCTOU window is unavoidable
    without a reservation daemon; a foreign listener taking the port in
    that window is surfaced by reverse listener detection.
    """
    if lo is None or hi is None:
        lo, hi = hostcheck.read_candidate_port_range()
    owned = {meta.get("port") for meta in doc.get("workers", {}).values()
             if isinstance(meta.get("port"), int)}
    reserved = _reserved_job_file_ports()
    for port in range(lo, hi + 1):
        if port in owned or port in reserved:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port, ""
    return None, (f"no bindable unowned port in [{lo}, {hi}] "
                  f"(owned={len(owned)}, reserved={len(reserved)})")


def remove_worker(name: str) -> gh.Result:
    return workmux(["remove", name, "--force"], timeout=120)


def close_worker(name: str) -> gh.Result:
    return workmux(["close", name], timeout=60)


def resurrect(dry_run: bool = False) -> gh.Result:
    args = ["resurrect"]
    if dry_run:
        args.append("--dry-run")
    return workmux(args, timeout=180)


def read_status(worker: str) -> dict | None:
    path = config.WORKER_LOG_DIR / f"{worker}.status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def process_alive(pid: int | None) -> bool:
    """Zombie-aware: a killed-but-unreaped process is not a working agent."""
    return proc.is_running(pid)


def trust_established(worktree: Path | None, role: str) -> bool:
    """Workspace trust for the role's agent in this directory.

    Claude runs headless with permissions bypassed inside an isolated worktree,
    so trust is established by construction. Codex records trust per project
    path in its own config; a parent trusted path covers descendants.
    """
    if worktree is None or not worktree.exists():
        return False
    if role in ("builder", "fixer"):
        return True
    if role == "reviewer":
        return _codex_trusts(worktree)
    return True


def _codex_trusts(worktree: Path) -> bool:
    """True when codex has a trusted project entry covering this path.

    Reads only the non-secret config.toml; the credential file is never touched.
    """
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return False
    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config means unproven trust
        return False
    projects = data.get("projects") or {}
    resolved = worktree.resolve()
    for raw_path, spec in projects.items():
        if (spec or {}).get("trust_level") != "trusted":
            continue
        candidate = Path(raw_path)
        if resolved == candidate or candidate in resolved.parents:
            return True
    return False


def readiness(worker: str, role: str, tz: str,
              stale_seconds: int = HEARTBEAT_STALE_SECONDS) -> Readiness:
    path = worktree_path(worker)
    status = read_status(worker) or {}
    heartbeat_at = status.get("heartbeat_at")
    fresh = False
    if heartbeat_at:
        try:
            seen = datetime.fromisoformat(heartbeat_at)
            fresh = datetime.now(ZoneInfo(tz)) - seen < timedelta(seconds=stale_seconds)
        except ValueError:
            fresh = False
    phase = status.get("phase", "UNKNOWN")
    running = process_alive(status.get("agent_pid")) or phase in (
        "DONE", "FAILED", "TIMEOUT"
    )
    return Readiness(
        worker=worker,
        worktree_present=path is not None,
        trust_established=trust_established(path, role),
        process_running=running,
        prompt_accepted=bool(status.get("prompt_accepted")),
        heartbeat_fresh=fresh or phase in ("DONE", "FAILED", "TIMEOUT"),
        phase=phase,
    )


def write_job(worker: str, role: str, provider: str, model: str | None, task_id: str | None,
              worktree: Path, prompt_path: Path, tz: str, *, pr: int | None = None,
              hard_timeout_seconds: int = 3600, effort: str | None = None,
              fallback_model: str | None = None, max_turns: int | None = None,
              port: int | None = None) -> Path:
    config.WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    job = {
        "worker": worker,
        "role": role,
        "provider": provider,
        "model": model,
        "effort": effort,
        "fallback_model": fallback_model,
        "task_id": task_id,
        "pr": pr,
        "worktree": str(worktree),
        "prompt_path": str(prompt_path),
        "status_path": str(config.WORKER_LOG_DIR / f"{worker}.status.json"),
        "output_path": str(config.WORKER_LOG_DIR / f"{worker}.out"),
        "last_message_path": str(config.WORKER_LOG_DIR / f"{worker}.last.txt"),
        "timezone": tz,
        "hard_timeout_seconds": hard_timeout_seconds,
        "port": port,
    }
    if max_turns:
        job["max_turns"] = max_turns
    path = config.WORKER_LOG_DIR / f"{worker}.job.json"
    path.write_text(json.dumps(redact.scrub(job), indent=2), encoding="utf-8")
    return path


def worker_output(worker: str, limit: int = 4000) -> str:
    path = config.WORKER_LOG_DIR / f"{worker}.out"
    if not path.exists():
        return ""
    try:
        return redact.scrub(path.read_bytes()[-limit:].decode("utf-8", errors="replace"))
    except OSError:
        return ""


def run_bounded(command: list[str], cwd: str, soft_timeout: int, hard_timeout: int,
                stdin_text: str = "") -> dict:
    """Run a disposable bounded job - used for the Grok Observer.

    Soft timeout is recorded; hard timeout kills. Observer failure never blocks
    development, so this reports rather than raises.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=cwd, input=stdin_text, capture_output=True, text=True,
            timeout=hard_timeout, check=False,
        )
        elapsed = time.monotonic() - started
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": redact.scrub(proc.stdout)[-8000:],
            "stderr": redact.scrub(proc.stderr)[-2000:],
            "duration_ms": round(elapsed * 1000, 1),
            "soft_timeout_exceeded": elapsed > soft_timeout,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "exit_code": 124, "stdout": "", "stderr": "hard timeout",
            "duration_ms": hard_timeout * 1000, "soft_timeout_exceeded": True,
            "timed_out": True,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "exit_code": 127, "stdout": "", "stderr": str(exc),
                "duration_ms": 0, "soft_timeout_exceeded": False, "timed_out": False}


def tooling_present() -> dict[str, bool]:
    return {name: shutil.which(name) is not None
            for name in ("tmux", "workmux", "claude", "codex", "grok", "gh", "git", "node",
                         "npm")}
