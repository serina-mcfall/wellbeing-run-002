"""How a Supervisor-created worktree becomes writable by the worker. Measured.

WHY THIS EXISTS. `experiment/C-20a-APPROVAL-PACKAGE.md` §6 ships LINKED
WORKTREES, and a worker's worktree is created by the Supervisor, running as
`run002-sup`. The Supervisor cannot `chown` anything - that needs root - so
something must make the result writable by `run002-wrk`. Until 2026-10-02
nothing had been chosen, and `control/worker_git.py`'s "WHO MAKES THE CLONE"
note recorded only that the choice was open. Applied as it stood, A5's
`chmod -R 0750 <WS>` left every worker worktree readable and NOT writable,
so no worker could commit and the factory would not run.

THE ARRANGEMENT THESE TESTS LOCK, and it has exactly three moving parts:

  1. `git config core.sharedRepository group` on the main checkout, so git
     itself creates new object directories, refs and worktree gitdirs
     group-writable with the setgid bit set.
  2. A one-time `chmod` of the directories that ALREADY EXIST, because (1)
     is not retroactive - it governs what git creates from then on.
  3. `umask 0002` on the Supervisor, because the checked-out working files
     and the worktree's gitdir are created under the process umask and a
     default 022 silently drops the group write bit.

Miss any one and the failure is LATENT: commits work until the first time
git needs a fan-out directory, a reflog or a working file that the
Supervisor created under the wrong mask. That is an hour-nine-of-twenty-four
failure, which is why each part has a test that fails without it.

SIMULATED MODE CHECKS, NOT A DEPLOYED-IDENTITY VERIFICATION. Like
`test_c22_worker_git_writes.py`, these run as ONE user on a disposable
repository. They establish what MODES the arrangement produces. They do not
establish that `run002-wrk` can write a `run002-sup`-owned file - group
membership, supplementary groups and a root-owned process can all differ.
That is V14's job, under the real identities, in Stage 1. Nothing here may
be reported as proof of the deployed property.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GROUP_WRITE = stat.S_IWGRP
SETGID = stat.S_ISGID


def git(*args, cwd, check=True, umask=None):
    """Run git, optionally under a specific umask in the child process."""
    preexec = (lambda: os.umask(umask)) if umask is not None else None
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        preexec_fn=preexec,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def mode(path) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)


def object_dirs(repo: Path) -> list[Path]:
    """The two-hex-character fan-out directories under .git/objects."""
    return [p for p in (repo / ".git" / "objects").iterdir()
            if p.is_dir() and len(p.name) == 2]


class SharedDispatchModesCase(unittest.TestCase):
    """A disposable repository, committed into exactly as the factory does."""

    def setUp(self):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not available")
        self.tmp = Path(tempfile.mkdtemp(prefix="run002-shared-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main = self.tmp / "main"
        self.main.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.main)
        git("config", "user.email", "probe@invalid", cwd=self.main)
        git("config", "user.name", "probe", cwd=self.main)
        git("config", "commit.gpgsign", "false", cwd=self.main)
        self.commit("seed.txt", "seed")

    def commit(self, name, text, cwd=None, umask=None):
        where = Path(cwd) if cwd else self.main
        (where / name).write_text(text)
        git("add", name, cwd=where, umask=umask)
        git("commit", "-qm", f"add {name}", cwd=where, umask=umask)

    def share(self):
        git("config", "core.sharedRepository", "group", cwd=self.main)

    # ---------------------------------------------------- part 1: the config

    def test_without_shared_repository_new_object_dirs_are_not_group_writable(self):
        """The dependency. Without the config, a peer identity cannot add objects.

        This is the measurement that makes part 1 necessary rather than
        decorative: git's default fan-out directory is 0755.
        """
        before = {p.name for p in object_dirs(self.main)}
        self.commit("a.txt", "a")
        created = [p for p in object_dirs(self.main) if p.name not in before]
        self.assertTrue(created, "the commit created no new fan-out directory")
        for path in created:
            self.assertFalse(mode(path) & GROUP_WRITE,
                             f"{path} was group-writable without the config")

    def test_with_shared_repository_new_object_dirs_and_refs_are_group_writable(self):
        """Part 1 does its job for everything git creates FROM THEN ON."""
        self.share()
        before = {p.name for p in object_dirs(self.main)}
        self.commit("b.txt", "b")
        git("branch", "run-002/task-1", cwd=self.main)

        created = [p for p in object_dirs(self.main) if p.name not in before]
        self.assertTrue(created, "the commit created no new fan-out directory")
        for path in created:
            self.assertTrue(mode(path) & GROUP_WRITE, f"{path} not group-writable")
            self.assertTrue(mode(path) & SETGID, f"{path} is not setgid")

        ref = self.main / ".git" / "refs" / "heads" / "run-002" / "task-1"
        self.assertTrue(mode(ref) & GROUP_WRITE, "the new ref is not group-writable")
        self.assertTrue(mode(ref.parent) & GROUP_WRITE,
                        "refs/heads/run-002/ is not group-writable")

    def test_shared_repository_is_not_retroactive(self):
        """Part 2 exists because of this. The config fixes nothing already there.

        `.git/objects`, `.git/refs/heads` and `.git/logs` are created by
        `git init` at 0755 and STAY 0755. A5 must chmod them explicitly; a
        procedure that sets only the config leaves the worker unable to
        write the very directories every commit touches.
        """
        self.share()
        self.commit("c.txt", "c")
        for relative in ("objects", "refs/heads", "logs"):
            path = self.main / ".git" / relative
            self.assertTrue(path.exists(), f".git/{relative} is missing")
            self.assertFalse(
                mode(path) & GROUP_WRITE,
                f".git/{relative} became group-writable on its own - if this "
                f"ever passes, A5's chmod may be removable, but verify it on "
                f"the deployed git version first")

    def test_git_init_shared_group_does_not_repair_an_existing_repository(self):
        """Why the procedure must NOT use the obvious-looking one-liner.

        `git init --shared=group .` reads as the canonical fix. On an
        existing repository it leaves the existing directory modes alone AND
        rewrites the config value to the numeric form, so a later reader
        looking for `group` does not find it.
        """
        self.share()
        git("init", "-q", "--shared=group", ".", cwd=self.main)
        self.assertFalse(mode(self.main / ".git" / "objects") & GROUP_WRITE,
                         "git init --shared repaired the existing mode")
        value = git("config", "core.sharedRepository",
                    cwd=self.main).stdout.strip()
        self.assertNotEqual(value, "group",
                            "the config value survived as 'group'; if this "
                            "ever passes the procedure's warning can be "
                            "narrowed")

    # ------------------------------------------------- part 3: the umask

    def test_worktree_made_under_umask_022_is_not_writable_by_the_group(self):
        """Part 3 exists because of this, and it is the trap that bites late.

        The config is set, the repository is shared - and a worktree the
        Supervisor creates under a DEFAULT umask still hands the worker a
        gitdir and working files it cannot write. Commits in that worktree
        fail; commits elsewhere do not. That asymmetry is what makes it a
        late, confusing failure rather than an obvious one.
        """
        self.share()
        wt = self.tmp / "wt-022"
        git("worktree", "add", "-q", "-b", "probe/a", str(wt),
            cwd=self.main, umask=0o022)
        self.assertFalse(mode(wt / "seed.txt") & GROUP_WRITE,
                         "the checked-out file was group-writable under 022")
        self.assertFalse(mode(self.main / ".git" / "worktrees" / "wt-022") & GROUP_WRITE,
                         "the worktree gitdir was group-writable under 022")

    def test_worktree_made_under_umask_002_is_writable_by_the_group(self):
        """The arrangement, whole: config + setgid parent + umask 0002."""
        self.share()
        root = self.tmp / "worktrees"
        root.mkdir()
        root.chmod(0o2770)
        wt = root / "task-1"
        git("worktree", "add", "-q", "-b", "run-002/task-1", str(wt),
            cwd=self.main, umask=0o002)

        gitdir = self.main / ".git" / "worktrees" / "task-1"
        for path in (wt, wt / "seed.txt", gitdir, gitdir / "HEAD"):
            self.assertTrue(mode(path) & GROUP_WRITE,
                            f"{path} is not group-writable under the arrangement")

    # ------------------------------------------- the protected paths hold

    def test_the_clawed_back_paths_stay_closed_across_git_writes(self):
        """The arrangement must not re-open what §6 withholds.

        `core.sharedRepository` is a property of the repository, so the
        question is whether git re-widens a path after it has been narrowed.
        Measured against the three §6 withholds: `.git/config` (rewritten by
        every `git config`), `.git/hooks` (code the Supervisor's git runs),
        and a worktree gitdir standing in for the gate export's.
        """
        self.share()
        wt = self.tmp / "wt-protected"
        git("worktree", "add", "-q", "-b", "probe/protected", str(wt),
            cwd=self.main, umask=0o002)
        gitdir = self.main / ".git" / "worktrees" / "wt-protected"
        config = self.main / ".git" / "config"
        hooks = self.main / ".git" / "hooks"

        config.chmod(0o640)
        hooks.chmod(0o750)
        gitdir.chmod(0o700)

        # The writes that could plausibly re-open them.
        git("config", "remote.origin.url", "https://example.invalid/x.git",
            cwd=self.main)
        self.commit("d.txt", "d", umask=0o002)

        self.assertEqual(mode(config), 0o640, ".git/config was re-opened")
        self.assertEqual(mode(hooks), 0o750, ".git/hooks was re-opened")
        self.assertEqual(mode(gitdir), 0o700,
                         "a clawed-back worktree gitdir was re-opened")

    def test_a_commit_still_succeeds_with_the_protected_paths_closed(self):
        """Narrowing those three must not cost the factory a commit."""
        self.share()
        root = self.tmp / "worktrees-ok"
        root.mkdir()
        root.chmod(0o2770)
        wt = root / "task-2"
        git("worktree", "add", "-q", "-b", "run-002/task-2", str(wt),
            cwd=self.main, umask=0o002)
        (self.main / ".git" / "config").chmod(0o640)
        (self.main / ".git" / "hooks").chmod(0o750)

        self.commit("worker.txt", "worker output", cwd=wt, umask=0o002)

        head = git("log", "--oneline", "-1", cwd=wt).stdout
        self.assertIn("add worker.txt", head)


if __name__ == "__main__":
    unittest.main()
