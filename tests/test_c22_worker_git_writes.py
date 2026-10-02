"""Where a worker's git writes actually land. §6's ownership, measured.

WHY THIS EXISTS. Approval package §6 proposed `.git/` — refs and objects
as **read, not write** for `run002-wrk`. If that were applied, **no worker
could make a single commit**, and the factory would not run at all. This
file is the measurement that establishes it, on a disposable repository
created and destroyed by the test.

SIMULATED PERMISSION CHECKS, NOT A DEPLOYED-IDENTITY VERIFICATION. These
tests use `chmod` on a throwaway repository owned by the current user to
show which git operation needs which directory writable. They do **not**
run as `run002-sup`/`run002-wrk`, do not create users, and prove nothing
about behaviour under the eventual OS identities - notably, a process
running as root, or one whose group membership differs from this one's,
can behave differently. What they establish is the DEPENDENCY: which
paths git must write for a worker to commit. Confirming it under the real
identities is V-step work after action 1, not something a test here can do.
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


def git(*args, cwd, check=True):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def chmod_tree(root: Path, writable: bool) -> None:
    for path in [root, *root.rglob("*")]:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR if writable
                       else mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
        except OSError:
            pass


class WorkerGitWritesCase(unittest.TestCase):
    """A disposable repository with a worktree, as the factory uses one."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="run002-gitwrites-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", ".", cwd=cls.main)
        git("config", "user.email", "t@example.invalid", cwd=cls.main)
        git("config", "user.name", "t", cwd=cls.main)
        (cls.main / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "a.txt", cwd=cls.main)
        git("commit", "-qm", "base", cwd=cls.main)
        cls.wt = cls.tmp / "wt"
        git("worktree", "add", "-q", str(cls.wt), "-b", "run-002/task-001",
            cwd=cls.main)

    @classmethod
    def tearDownClass(cls):
        for sub in ("objects", "refs", "worktrees"):
            chmod_tree(cls.main / ".git" / sub, writable=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_worktree_gitdir_is_a_FILE_pointing_into_the_main_repo(self):
        """The shape the whole question turns on."""
        dot_git = self.wt / ".git"
        self.assertTrue(dot_git.is_file(),
                        "a linked worktree's .git should be a file")
        self.assertIn("gitdir:", dot_git.read_text(encoding="utf-8"))
        self.assertIn(".git/worktrees/", dot_git.read_text(encoding="utf-8"))

    def test_a_worker_commit_writes_into_the_SHARED_object_store(self):
        before = len(list((self.main / ".git" / "objects").rglob("*")))
        (self.wt / "b.txt").write_text("work\n", encoding="utf-8")
        git("add", "b.txt", cwd=self.wt)
        git("commit", "-qm", "work", cwd=self.wt)
        after = len(list((self.main / ".git" / "objects").rglob("*")))
        self.assertGreater(after, before,
                           "a commit in the worktree wrote no objects into "
                           "the main repository's shared store")

    def test_the_branch_ref_lands_under_the_main_repo_refs(self):
        ref = self.main / ".git" / "refs" / "heads" / "run-002" / "task-001"
        packed = (self.main / ".git" / "packed-refs")
        self.assertTrue(
            ref.exists() or (packed.exists()
                             and "run-002/task-001" in packed.read_text()),
            "the worker's branch ref is not in the main repository")

    def test_read_only_objects_makes_a_worker_commit_IMPOSSIBLE(self):
        """§6 as written would stop the factory before it started."""
        objects = self.main / ".git" / "objects"
        (self.wt / "c.txt").write_text("blocked\n", encoding="utf-8")
        chmod_tree(objects, writable=False)
        try:
            result = git("add", "c.txt", cwd=self.wt, check=False)
        finally:
            chmod_tree(objects, writable=True)
        self.assertNotEqual(result.returncode, 0,
                            "git add succeeded with the shared object store "
                            "read-only - this test proves nothing")
        self.assertIn("insufficient permission", result.stderr.lower())

    def test_read_only_refs_makes_a_worker_commit_IMPOSSIBLE(self):
        refs = self.main / ".git" / "refs"
        (self.wt / "d.txt").write_text("blocked\n", encoding="utf-8")
        git("add", "d.txt", cwd=self.wt)
        chmod_tree(refs, writable=False)
        try:
            result = git("commit", "-qm", "blocked", cwd=self.wt, check=False)
        finally:
            chmod_tree(refs, writable=True)
        self.assertNotEqual(result.returncode, 0,
                            "git commit succeeded with refs read-only")
        self.assertIn("cannot lock ref", result.stderr.lower())
        git("commit", "-qm", "unblocked", cwd=self.wt)

    def test_what_CAN_be_withheld_without_breaking_a_commit(self):
        """`.git/hooks` and another worktree's gitdir are not needed.

        This is the half that makes the corrected §6 arrangement workable:
        the directories a worker must write are narrower than `.git/`, so
        the ones that matter to the gate can still be withheld.
        """
        hooks = self.main / ".git" / "hooks"
        self.assertTrue(hooks.exists())
        (self.wt / "e.txt").write_text("fine\n", encoding="utf-8")
        chmod_tree(hooks, writable=False)
        try:
            git("add", "e.txt", cwd=self.wt)
            git("commit", "-qm", "hooks-readonly", cwd=self.wt)
        finally:
            chmod_tree(hooks, writable=True)

    def test_this_is_a_permission_simulation_not_an_identity_proof(self):
        """Stated as an assertion so it cannot be skimmed past.

        Everything above runs as ONE user. It establishes which paths git
        writes; it does not establish how `run002-wrk` behaves against
        files owned by `run002-sup`.
        """
        self.assertEqual(os.getuid(), os.geteuid(),
                         "this test makes no claim about a different uid")


if __name__ == "__main__":
    unittest.main()
