"""C-22 — a worker's own clone, and what it keeps out of `<main>/.git`.

THE PROBLEM THIS ANSWERS IS ALREADY MEASURED AND IS NOT REMEASURED HERE.
`tests/test_c22_worker_git_writes.py` established, on a disposable
repository, that a LINKED WORKTREE cannot commit unless the worker can
write `<main>/.git/objects` and `<main>/.git/refs`. Read that file first.
Its residual is the subject of this one:

    POSIX modes cannot scope write access PER REF. Every worker's branch
    lives in one `refs/heads/run-002/` directory, and the object store a
    worker must be able to write is the one every reader depends on. You
    cannot protect worker B's ref without taking away worker A's commit.

WHAT IS TESTED HERE. That `control/worker_git.py`'s arrangement - one
clone per worker, outside the checkout - removes the dependency entirely:
a worker commits with ALL of `<main>/.git` read-only, writes nothing into
it, and the Supervisor still obtains the commits.

SIMULATED PERMISSION CHECKS, NOT A DEPLOYED-IDENTITY VERIFICATION. Like
its predecessor, everything below runs as ONE uid and uses `chmod` on
throwaway repositories. It establishes which paths git touches. It
establishes NOTHING about how `run002-wrk` behaves against files owned by
`run002-sup`. `worker_git.DEPLOYMENT_CHECKS` carries the five checks only
the deployed identities can make, and V14 as currently written does not
cover this arrangement - V14a-V14e replace it.

NO NETWORK, NO `gh`, NO PUSH. Every repository here is created under
`tempfile.mkdtemp` and destroyed. `prepare_clone` sets `origin` to an
HTTPS URL and nothing in this file ever contacts it.

ONE PIECE OF EVIDENCE IS DELIBERATELY NOT REPRODUCED AS A TEST. On a
disposable fixture, a clone left with its default `origin` - the source
path - pushed a `run-002/` branch straight into the main repository's
refs and added three objects to its store. That is the whole reason
`origin_url` is required and must be HTTPS. It is not re-run here
because running it means running `git push`, and this suite does not
push. `test_a_local_clones_default_origin_is_the_source_repository`
pins the precondition instead.
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

from control import gate_invoker, worker_git, workers


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


def object_files(repo: Path) -> int:
    return sum(1 for p in (repo / ".git" / "objects").rglob("*") if p.is_file())


def head_refs(repo: Path) -> set[str]:
    root = repo / ".git" / "refs" / "heads"
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


HTTPS = "https://github.com/serina-mcfall/wellbeing-run-002.git"

# THE FIXTURES DELIBERATELY DO NOT USE THE GITHUB URL. This host's
# ~/.gitconfig carries `url.git@github.com:.insteadOf https://github.com/`,
# so a real `git remote set-url origin <the GitHub HTTPS URL>` resolves to
# SSH here and `prepare_clone` correctly refuses it - which would make
# every fixture below pass or fail according to whose machine ran it.
# A URL the rewrite cannot match keeps the fixtures host-independent; the
# rewrite itself is tested on its own, twice, with the mechanism
# constructed rather than borrowed from the host.
FIXTURE_ORIGIN = "https://example.invalid/serina-mcfall/wellbeing-run-002.git"


class CloneFixtureCase(unittest.TestCase):
    """A disposable main repository and two worker clones made from it."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not available")
        if os.geteuid() == 0:
            raise unittest.SkipTest("root ignores the modes this simulates")
        cls.tmp = Path(tempfile.mkdtemp(prefix="run002-clone-"))
        # Registered BEFORE anything can fail. A `tearDownClass` does not
        # run when `setUpClass` raises, and this fixture chmods trees
        # read-only partway through - observed leaking a 24MB directory
        # under a deliberate mutation, which is exactly when a fixture is
        # most likely to abort.
        cls.addClassCleanup(cls._dispose)
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=cls.main)
        git("config", "user.email", "t@example.invalid", cwd=cls.main)
        git("config", "user.name", "t", cwd=cls.main)
        (cls.main / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "a.txt", cwd=cls.main)
        git("commit", "-qm", "base", cwd=cls.main)
        cls.base_sha = git("rev-parse", "HEAD", cwd=cls.main).stdout.strip()

        # The gate export, exactly as approval action 7 creates it: a
        # DETACHED worktree whose gitdir HEAD is what
        # gate_invoker.export_revision reads to prove the pin.
        cls.export = cls.tmp / f"gate-{cls.base_sha}"
        git("worktree", "add", "--detach", "-q", str(cls.export), cls.base_sha,
            cwd=cls.main)

        cls.clones = cls.tmp / "clones"
        cls.results = {}
        for worker, branch in (("w-alpha", "run-002/task-001"),
                               ("w-beta", "run-002/task-002")):
            result = worker_git.prepare_clone(
                cls.main, cls.clones / worker, branch=branch,
                start_ref="refs/heads/main", origin_url=FIXTURE_ORIGIN,
                repo_root=cls.main)
            if not result.ok:
                raise AssertionError(f"fixture clone {worker}: {result.outcome}")
            git("config", "user.email", "t@example.invalid", cwd=result.path)
            git("config", "user.name", "t", cwd=result.path)
            cls.results[worker] = result
        cls.alpha = cls.results["w-alpha"].path
        cls.beta = cls.results["w-beta"].path

    @classmethod
    def _dispose(cls):
        for sub in (cls.tmp / "main" / ".git", cls.tmp / "clones"):
            if sub.exists():
                chmod_tree(sub, writable=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------- (a) it can work

    def test_a_worker_can_commit_in_its_own_clone(self):
        (self.alpha / "b.txt").write_text("alpha work\n", encoding="utf-8")
        git("add", "b.txt", cwd=self.alpha)
        git("commit", "-qm", "alpha work", cwd=self.alpha)
        self.assertEqual(
            git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.alpha).stdout.strip(),
            "run-002/task-001")

    def test_the_clone_is_a_real_repository_not_a_linked_worktree(self):
        """The shape the whole arrangement turns on, inverted.

        The predecessor file's first test asserts a worktree's `.git` is a
        FILE pointing into the main repository. Here it must be a
        DIRECTORY, because that is what makes the clone's objects and refs
        its own.
        """
        self.assertTrue((self.alpha / ".git").is_dir())
        self.assertFalse((self.alpha / ".git").is_file())

    def test_the_push_is_PREPARED_against_an_https_origin_never_the_source(self):
        for scope in ([], ["--push"]):
            self.assertEqual(
                git("remote", "get-url", *scope, "origin",
                    cwd=self.alpha).stdout.strip(),
                FIXTURE_ORIGIN)
        self.assertEqual(list(self.results["w-alpha"].push),
                         ["git", "push", "-u", "origin", "run-002/task-001"])

    def test_a_bare_git_push_has_an_upstream_to_resolve(self):
        """`prompts/fixer.md` finishes with a bare `git push`, which fails
        without an upstream. The clone never saw `origin/<branch>`, so it
        is configured directly."""
        branch = "run-002/task-001"
        self.assertEqual(
            git("config", f"branch.{branch}.remote", cwd=self.alpha).stdout.strip(),
            "origin")
        self.assertEqual(
            git("config", f"branch.{branch}.merge", cwd=self.alpha).stdout.strip(),
            f"refs/heads/{branch}")

    # --------------------------------- (b) the Supervisor's git metadata

    def test_a_worker_commit_writes_NOTHING_into_the_main_repository(self):
        """The property a linked worktree cannot have.

        `test_c22_worker_git_writes.py` measured that a worktree commit
        adds objects to `<main>/.git/objects` and a ref to
        `<main>/.git/refs/heads/run-002/`. Both counts must be unchanged
        here.
        """
        before_objects, before_refs = object_files(self.main), head_refs(self.main)
        (self.beta / "c.txt").write_text("beta work\n", encoding="utf-8")
        git("add", "c.txt", cwd=self.beta)
        git("commit", "-qm", "beta work", cwd=self.beta)
        self.assertEqual(object_files(self.main), before_objects,
                         "the clone's commit wrote objects into the main store")
        self.assertEqual(head_refs(self.main), before_refs,
                         "the clone's commit created a ref in the main repo")

    def test_the_clones_objects_share_no_inode_with_the_main_repository(self):
        """`--no-hardlinks` checked on the RESULT, not on the argv.

        Without it the clone's object files are the Supervisor's object
        files, one inode and one set of permissions, and handing the clone
        to another owner would re-own the Supervisor's store.
        """
        main_inodes = {p.stat().st_ino for p
                       in (self.main / ".git" / "objects").rglob("*")
                       if p.is_file()}
        clone_inodes = {p.stat().st_ino for p
                        in (self.alpha / ".git" / "objects").rglob("*")
                        if p.is_file()}
        self.assertTrue(main_inodes and clone_inodes)
        self.assertFalse(main_inodes & clone_inodes,
                         "the clone hardlinked the Supervisor's objects")

    def test_a_worker_commits_with_ALL_of_main_dot_git_READ_ONLY(self):
        """§6's ORIGINAL git row, now implementable.

        The approval package had to CORRECT `.git/` from "read, not write"
        to a narrower set because a worktree commit needs objects and refs
        writable. A clone needs neither, so the whole of `<main>/.git` can
        be withdrawn and the worker still commits.
        """
        (self.alpha / "d.txt").write_text("under lockdown\n", encoding="utf-8")
        chmod_tree(self.main / ".git", writable=False)
        try:
            git("add", "d.txt", cwd=self.alpha)
            git("commit", "-qm", "written with main .git read-only",
                cwd=self.alpha)
        finally:
            chmod_tree(self.main / ".git", writable=True)

    def test_the_gate_exports_gitdir_is_reachable_by_nothing_the_worker_runs(self):
        """`gate_invoker.export_revision` reads
        `<main>/.git/worktrees/gate-<SHA>/HEAD` to prove the export is at
        the pin. With that directory at mode 0000 the read fails - so it
        IS the protection surface - and the worker's commit is unaffected,
        so withholding it costs the factory nothing.

        PERMISSION SIMULATION: one uid, `chmod`. Under the deployed
        identities this is V14c.
        """
        gitdir = self.main / ".git" / "worktrees" / f"gate-{self.base_sha}"
        self.assertEqual(gate_invoker.export_revision(self.export), self.base_sha)
        (self.alpha / "e.txt").write_text("export sealed\n", encoding="utf-8")
        original = gitdir.stat().st_mode
        gitdir.chmod(0o000)
        try:
            self.assertIsNone(gate_invoker.export_revision(self.export),
                              "mode 0000 did not stop the HEAD read - this "
                              "test proves nothing")
            git("add", "e.txt", cwd=self.alpha)
            git("commit", "-qm", "export gitdir sealed", cwd=self.alpha)
        finally:
            gitdir.chmod(original)
        self.assertEqual(gate_invoker.export_revision(self.export), self.base_sha)

    # ------------------------------------- (c) another worker's own refs

    def test_one_workers_refs_live_only_in_its_own_clone(self):
        alpha_refs = head_refs(self.alpha)
        beta_refs = head_refs(self.beta)
        self.assertIn("run-002/task-001", alpha_refs)
        self.assertNotIn("run-002/task-002", alpha_refs)
        self.assertIn("run-002/task-002", beta_refs)
        self.assertNotIn("run-002/task-001", beta_refs)

    def test_a_peers_ref_cannot_be_moved_when_its_clone_is_not_writable(self):
        """WHAT SEPARATE CLONES MAKE POSSIBLE, AND THE EXACT LIMIT OF IT.

        With a read-only `refs/` in worker beta's clone, an `update-ref`
        aimed at beta's branch fails while alpha's own commits keep
        working. That combination is IMPOSSIBLE under linked worktrees:
        both branches are in one directory, and the predecessor file
        measured that making it read-only stops the owner committing too.

        PERMISSION SIMULATION, AND IT SIMULATES SOMETHING §6 DOES NOT
        DEPLOY. The read-only mode here stands in for beta's clone being
        owned by a DIFFERENT uid. §6 deploys ONE `run002-wrk`, under which
        alpha and beta share a uid and this separation does not exist -
        that is `DEPLOYMENT_CHECKS` V14e, which must be recorded as not
        applicable rather than as passed.
        """
        # A commit of alpha's own, made here rather than inherited from
        # whichever test ran first, so the SHA this tries to force onto
        # beta is certainly different from where beta's branch already
        # points. Reading those two as equal is exactly how this assertion
        # could pass while proving nothing.
        (self.alpha / "peer.txt").write_text("alpha only\n", encoding="utf-8")
        git("add", "peer.txt", cwd=self.alpha)
        git("commit", "-qm", "alpha only", cwd=self.alpha)
        alpha_sha = git("rev-parse", "HEAD", cwd=self.alpha).stdout.strip()
        beta_before = git("rev-parse", "refs/heads/run-002/task-002",
                          cwd=self.beta).stdout.strip()
        self.assertNotEqual(alpha_sha, beta_before)

        chmod_tree(self.beta / ".git" / "refs", writable=False)
        try:
            refused = git("update-ref", "refs/heads/run-002/task-002",
                          alpha_sha, cwd=self.beta, check=False)
            (self.alpha / "f.txt").write_text("still working\n", encoding="utf-8")
            git("add", "f.txt", cwd=self.alpha)
            git("commit", "-qm", "alpha unaffected", cwd=self.alpha)
        finally:
            chmod_tree(self.beta / ".git" / "refs", writable=True)
        self.assertNotEqual(refused.returncode, 0,
                            "beta's ref moved with its refs/ read-only - this "
                            "test proves nothing")
        self.assertIn("permission denied", refused.stderr.lower())
        self.assertEqual(
            git("rev-parse", "refs/heads/run-002/task-002",
                cwd=self.beta).stdout.strip(), beta_before)

    # ------------------- the Supervisor still obtains the worker's commits

    def test_the_supervisor_fetches_a_workers_commits_over_a_LOCAL_PATH(self):
        """What replaces the shared object store.

        The fetch is a filesystem path, so no network call and no
        credential is involved, and it lands outside `refs/heads/` so a
        worker's branch never becomes one of the Supervisor's.
        """
        (self.alpha / "g.txt").write_text("for the supervisor\n", encoding="utf-8")
        git("add", "g.txt", cwd=self.alpha)
        git("commit", "-qm", "supervisor fetches this", cwd=self.alpha)
        sha = git("rev-parse", "HEAD", cwd=self.alpha).stdout.strip()

        self.assertNotEqual(
            git("cat-file", "-e", f"{sha}^{{commit}}", cwd=self.main,
                check=False).returncode, 0,
            "the main repository already had the commit - the fetch under "
            "test cannot be what put it there")

        argv = worker_git.supervisor_fetch_argv(self.alpha, "run-002/task-001",
                                                "w-alpha")
        git(*argv[1:], cwd=self.main)

        self.assertEqual(
            git("rev-parse", "refs/run-002/workers/w-alpha/run-002/task-001",
                cwd=self.main).stdout.strip(), sha)
        self.assertNotIn("run-002/task-001", head_refs(self.main),
                         "a fetched worker branch became a Supervisor branch")

        # Exactly what accessibility_services.isolated_checkout does with a
        # PR head SHA. It is the only thing in this repository that needs a
        # worker's commit present locally.
        checkout = self.tmp / "scan-checkout"
        git("worktree", "add", "--detach", "-q", str(checkout), sha, cwd=self.main)
        self.assertEqual((checkout / "g.txt").read_text(encoding="utf-8"),
                         "for the supervisor\n")
        git("worktree", "remove", "--force", str(checkout), cwd=self.main)

    def test_the_supervisor_fetch_is_not_forced(self):
        """A worker that rewrote its branch cannot move the Supervisor's
        authoritative ref backwards in silence; the fetch fails instead."""
        argv = worker_git.supervisor_fetch_argv(self.alpha, "b", "w")
        self.assertNotIn("--force", argv)
        self.assertNotIn("-f", argv)
        self.assertFalse(argv[-1].startswith("+"), argv)


class MeasuredHazardCase(unittest.TestCase):
    """The two facts that forced `--no-hardlinks` and a required origin."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="run002-clonehaz-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=cls.main)
        git("config", "user.email", "t@example.invalid", cwd=cls.main)
        git("config", "user.name", "t", cwd=cls.main)
        (cls.main / "a.txt").write_text("base\n", encoding="utf-8")
        git("add", "a.txt", cwd=cls.main)
        git("commit", "-qm", "base", cwd=cls.main)

    def test_a_DEFAULT_local_clone_shares_object_inodes_with_its_source(self):
        """Why `clone_argv` forces `--no-hardlinks`.

        `chmod` on the clone's copy of an object changes the SOURCE's copy,
        because they are one inode. A deployment that chowned such a clone
        to `run002-wrk` would re-own the Supervisor's object store.
        """
        dest = self.tmp / "hardlinked"
        git("clone", "--quiet", str(self.main), str(dest), cwd=self.tmp)
        shared = [p for p in (self.main / ".git" / "objects").rglob("*")
                  if p.is_file()]
        self.assertTrue(shared, "no loose objects to compare")
        source = shared[0]
        mirrored = (dest / ".git" / "objects"
                    / source.relative_to(self.main / ".git" / "objects"))
        self.assertTrue(mirrored.exists())
        self.assertEqual(source.stat().st_ino, mirrored.stat().st_ino,
                         "a default local clone did not hardlink - the "
                         "hazard --no-hardlinks guards may have changed")
        original = source.stat().st_mode & 0o777
        mirrored.chmod(0o600)
        try:
            self.assertEqual(source.stat().st_mode & 0o777, 0o600,
                             "metadata did not cross the hardlink")
        finally:
            mirrored.chmod(original)

    def test_a_local_clones_default_origin_is_the_source_repository(self):
        """Why `origin_url` is required and must be HTTPS.

        A push to this `origin` writes the Supervisor's own refs and
        objects - measured on a fixture, not re-run here because this
        suite does not push.
        """
        dest = self.tmp / "origin-default"
        git("clone", "--no-hardlinks", "--quiet", str(self.main), str(dest),
            cwd=self.tmp)
        default = git("remote", "get-url", "origin", cwd=dest).stdout.strip()
        self.assertEqual(Path(default).resolve(), self.main.resolve())
        self.assertFalse(worker_git.origin_url_permitted(default),
                         "the source path passed the origin guard")

    def test_an_insteadOf_rewrite_HIDES_ITSELF_from_the_stored_config(self):
        """Why the effective URL is read back out of git.

        `git config remote.origin.url` reports what was written; `git
        remote get-url` reports what git will USE. With an `insteadOf`
        rewrite in scope they disagree, and the one that decides which
        credential a push presents is the second.

        The rewrite is configured HERE, in the fixture's own repository,
        rather than relying on the one this host happens to carry - so
        this test means the same thing on any machine.
        """
        dest = self.tmp / "rewritten"
        git("clone", "--no-hardlinks", "--quiet", str(self.main), str(dest),
            cwd=self.tmp)
        git("config", "url.ssh://git@example.invalid/.insteadOf",
            "https://example.invalid/", cwd=dest)
        git("remote", "set-url", "origin", "https://example.invalid/x/y.git",
            cwd=dest)

        self.assertEqual(
            git("config", "remote.origin.url", cwd=dest).stdout.strip(),
            "https://example.invalid/x/y.git",
            "the stored value changed - this test is measuring the wrong "
            "surface")
        for scope in ([], ["--push"]):
            self.assertEqual(
                git("remote", "get-url", *scope, "origin", cwd=dest).stdout.strip(),
                "ssh://git@example.invalid/x/y.git",
                "the rewrite did not take effect, so nothing here is proven")

    def test_prepare_clone_refuses_a_remote_git_will_rewrite(self):
        """End to end on a real repository: the refusal, not just the read.

        `prepare_clone` is given a destination whose global-ish rewrite it
        cannot see in advance; the read-back is what catches it, and the
        outcome is a refused dispatch rather than a worker pushing over
        SSH as `serina`.
        """
        dest = self.tmp / "clones" / "w-rewritten"
        rewrite = ("url.ssh://git@example.invalid/.insteadOf",
                   "https://example.invalid/")

        def runner(argv, cwd=None, timeout=300):
            ok, out = worker_git.run_git(argv, cwd, timeout)
            if ok and argv[1] == "clone":
                worker_git.run_git(["git", "config", *rewrite], argv[-1])
            return ok, out

        result = worker_git.prepare_clone(
            self.main, dest, branch="run-002/task-009",
            start_ref="refs/heads/main",
            origin_url="https://example.invalid/x/y.git",
            repo_root=self.main, runner=runner)
        self.assertEqual(result.outcome, worker_git.ORIGIN_REWRITTEN)
        self.assertFalse(result.ok)
        self.assertIsNone(result.path)
        self.assertEqual(
            git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest).stdout.strip(),
            "main", "the task branch was created despite the refusal")


class DerivationCase(unittest.TestCase):
    """The pure halves: paths, argv and refusals. No repository needed."""

    def test_clone_argv_always_carries_no_hardlinks(self):
        argv = worker_git.clone_argv("/src", "/dst")
        self.assertIn("--no-hardlinks", argv)
        self.assertEqual(argv[:2], ["git", "clone"])
        self.assertEqual(argv[-2:], ["/src", "/dst"])

    def test_clone_argv_never_shares_an_object_store(self):
        argv = worker_git.clone_argv("/src", "/dst")
        for flag in ("--shared", "-s", "--reference", "--reference-if-able",
                     "--dissociate", "--local", "-l"):
            self.assertNotIn(flag, argv)

    def test_only_https_may_be_a_worker_clones_origin(self):
        self.assertTrue(worker_git.origin_url_permitted(HTTPS))
        for refused in ("/tmp/main", "file:///tmp/main", "ssh://git@github.com/x",
                        "git@github.com:x/y.git", "http://github.com/x/y.git",
                        "https://", "", None, 7):
            self.assertFalse(worker_git.origin_url_permitted(refused), refused)

    def test_the_clone_root_is_outside_the_checkout_and_its_worktree_root(self):
        root = Path("/srv/run-002")
        clones = worker_git.worker_clone_root(root)
        self.assertNotEqual(clones, root)
        self.assertNotIn(root, clones.parents)
        self.assertNotEqual(clones, workers.managed_worktree_root(root))

    def test_a_destination_inside_the_checkout_is_refused(self):
        root = Path("/srv/run-002")
        self.assertTrue(worker_git.dest_permitted(
            worker_git.clone_path("w1", root), root))
        for refused in (root, root / "control" / "w1",
                        root.parent / "run-002__worktrees" / "w1",
                        "relative/path", "", None):
            self.assertFalse(worker_git.dest_permitted(refused, root), refused)

    def test_the_supervisor_ref_is_not_a_branch(self):
        ref = worker_git.supervisor_ref("w-alpha", "run-002/task-001")
        self.assertTrue(ref.startswith("refs/run-002/workers/"))
        self.assertFalse(ref.startswith("refs/heads/"))
        self.assertIn("w-alpha", ref)

    def test_the_https_origin_names_the_configured_repository(self):
        url = worker_git.https_origin_url("owner/repo")
        self.assertEqual(url, "https://github.com/owner/repo.git")
        self.assertTrue(worker_git.origin_url_permitted(url))


class PrepareRefusalCase(unittest.TestCase):
    """`prepare_clone` never raises, and never reports OK on a failure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run002-prep-"))
        self.root = self.tmp / "main"
        (self.root / ".git").mkdir(parents=True)
        self.dest = worker_git.clone_path("w1", self.root)
        self.calls: list[list[str]] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, fail_on=None, effective=None):
        """A git that records argv. `effective` is what `remote get-url`
        reports, which defaults to whatever `set-url` was last given -
        i.e. a git with no `insteadOf` rewrite in scope."""
        state = {"origin": None}

        def run(argv, cwd=None, timeout=300):
            self.calls.append(argv)
            if argv[0] == "git" and argv[1] == "clone":
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            if fail_on is not None and fail_on in argv:
                return False, ""
            if argv[1:3] == ["remote", "set-url"]:
                state["origin"] = argv[-1]
            if argv[1:3] == ["remote", "get-url"]:
                return True, effective if effective is not None else state["origin"]
            return True, ""
        return run

    def prepare(self, **kwargs):
        defaults = dict(branch="run-002/task-001", start_ref="refs/heads/main",
                        origin_url=HTTPS, repo_root=self.root,
                        runner=self.runner())
        defaults.update(kwargs)
        return worker_git.prepare_clone(self.root, self.dest, **defaults)

    def test_a_happy_preparation_reports_the_path_and_the_prepared_push(self):
        result = self.prepare()
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, worker_git.OK)
        self.assertEqual(result.path, self.dest)
        self.assertEqual(result.push[:4], ("git", "push", "-u", "origin"))

    def test_origin_is_rewritten_BEFORE_the_branch_exists(self):
        """No window in which a checkout holding a worker's branch also
        holds an `origin` pointing at the Supervisor's repository."""
        self.prepare()
        order = [" ".join(argv) for argv in self.calls]
        set_url = next(i for i, c in enumerate(order) if "remote set-url" in c)
        checkout = next(i for i, c in enumerate(order) if "checkout" in c)
        self.assertLess(set_url, checkout, order)

    def test_every_step_failure_has_its_own_outcome_and_none_is_OK(self):
        for fail_on, expected in (("clone", worker_git.CLONE_FAILED),
                                  ("set-url", worker_git.ORIGIN_FAILED),
                                  ("fetch", worker_git.SEED_FAILED),
                                  ("checkout", worker_git.BRANCH_FAILED),
                                  ("config", worker_git.UPSTREAM_FAILED)):
            with self.subTest(fail_on=fail_on):
                shutil.rmtree(self.dest, ignore_errors=True)
                self.calls = []
                result = self.prepare(runner=self.runner(fail_on=fail_on))
                self.assertFalse(result.ok)
                self.assertEqual(result.outcome, expected)
                self.assertIsNone(result.path)

    def test_an_origin_git_reports_differently_is_refused(self):
        """Both surfaces: `insteadOf` rewrites the fetch URL and
        `pushInsteadOf` rewrites only the push one, so a guard that read
        just `get-url` would miss the second."""
        for scope in ("both", "push-only"):
            with self.subTest(scope=scope):
                shutil.rmtree(self.dest, ignore_errors=True)
                self.calls = []
                seen = {"n": 0}

                def effective_for(argv_tail, seen=seen, scope=scope):
                    seen["n"] += 1
                    if scope == "both" or seen["n"] == 2:
                        return "git@github.com:serina-mcfall/wellbeing-run-002.git"
                    return HTTPS

                def run(argv, cwd=None, timeout=300):
                    self.calls.append(argv)
                    if argv[1] == "clone":
                        Path(argv[-1]).mkdir(parents=True, exist_ok=True)
                    if argv[1:3] == ["remote", "get-url"]:
                        return True, effective_for(argv)
                    return True, ""

                result = self.prepare(runner=run)
                self.assertEqual(result.outcome, worker_git.ORIGIN_REWRITTEN)
                self.assertFalse(
                    any(a[1] == "checkout" for a in self.calls),
                    "the branch was created after the origin was refused")

    def test_a_non_https_origin_is_refused_before_any_git_runs(self):
        result = self.prepare(origin_url=str(self.root))
        self.assertEqual(result.outcome, worker_git.ORIGIN_INVALID)
        self.assertEqual(self.calls, [])

    def test_a_destination_that_already_exists_is_refused_not_overwritten(self):
        self.dest.mkdir(parents=True)
        (self.dest / "keep.txt").write_text("existing work\n", encoding="utf-8")
        result = self.prepare()
        self.assertEqual(result.outcome, worker_git.DEST_EXISTS)
        self.assertEqual(self.calls, [])
        self.assertTrue((self.dest / "keep.txt").exists())

    def test_a_source_without_a_git_directory_is_refused(self):
        bare = self.tmp / "not-a-repo"
        bare.mkdir()
        result = worker_git.prepare_clone(
            bare, self.dest, branch="b", start_ref="refs/heads/main",
            origin_url=HTTPS, repo_root=self.root, runner=self.runner())
        self.assertEqual(result.outcome, worker_git.SOURCE_INVALID)
        self.assertEqual(self.calls, [])

    def test_an_option_shaped_branch_name_is_refused(self):
        result = self.prepare(branch="--upload-pack=touch /tmp/pwn")
        self.assertEqual(result.outcome, worker_git.BRANCH_INVALID)
        self.assertEqual(self.calls, [])

    def test_every_refusal_token_is_in_OUTCOMES(self):
        for name in ("OK", "DEST_INVALID", "DEST_EXISTS", "SOURCE_INVALID",
                     "ORIGIN_INVALID", "BRANCH_INVALID", "CLONE_FAILED",
                     "SEED_FAILED", "BRANCH_FAILED", "ORIGIN_FAILED",
                     "ORIGIN_REWRITTEN", "UPSTREAM_FAILED"):
            self.assertIn(getattr(worker_git, name), worker_git.OUTCOMES)


class WorkersEntryPointCase(unittest.TestCase):
    """`control/workers.py`'s clone path, with tmux stubbed out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run002-wk-"))
        self.root = self.tmp / "main"
        (self.root / ".git").mkdir(parents=True)
        self._tmux = workers.tmux
        self._pane = workers.pane_for_worktree
        self.tmux_calls: list[list[str]] = []
        workers.tmux = lambda args, timeout=0: (
            self.tmux_calls.append(args) or workers.gh.Result(True, "", "", 0))
        workers.pane_for_worktree = lambda path: None

    def tearDown(self):
        workers.tmux = self._tmux
        workers.pane_for_worktree = self._pane
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, ok=True):
        def run(argv, cwd=None, timeout=300):
            if argv[1] == "clone":
                if not ok:
                    return False, ""
                Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            if argv[1:3] == ["remote", "get-url"]:
                return True, HTTPS
            return ok, ""
        return run

    def test_a_worker_is_created_the_new_way_and_gets_a_window(self):
        path, reason = workers.create_worker_clone(
            "w1", "run-002/task-001", start_ref="refs/heads/main",
            origin_url=HTTPS, session="s", repo_root=self.root,
            runner=self.runner())
        self.assertEqual(reason, "")
        self.assertEqual(path, worker_git.clone_path("w1", self.root))
        self.assertTrue(any(a[0] == "new-window" for a in self.tmux_calls))

    def test_a_failed_clone_yields_a_reason_and_never_a_silent_none(self):
        path, reason = workers.create_worker_clone(
            "w1", "run-002/task-001", start_ref="refs/heads/main",
            origin_url=HTTPS, session="s", repo_root=self.root,
            runner=self.runner(ok=False))
        self.assertIsNone(path)
        self.assertIn(worker_git.CLONE_FAILED, reason)

    def test_an_existing_clone_is_reused_rather_than_rebuilt(self):
        existing = worker_git.clone_path("w1", self.root)
        existing.mkdir(parents=True)
        (existing / "earlier.txt").write_text("fixer work\n", encoding="utf-8")

        def explode(argv, cwd=None, timeout=300):
            raise AssertionError("an existing clone was rebuilt")

        path, reason = workers.create_worker_clone(
            "w1", "run-002/task-001", start_ref="refs/heads/main",
            origin_url=HTTPS, session="s", repo_root=self.root, runner=explode)
        self.assertEqual((path, reason), (existing, ""))
        self.assertTrue((existing / "earlier.txt").exists())

    def test_removal_refuses_a_name_that_escapes_the_clone_root(self):
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("not a clone\n", encoding="utf-8")
        ok, reason = workers.remove_worker_clone("../precious", self.root)
        self.assertFalse(ok)
        self.assertIn("not directly inside", reason)
        self.assertTrue((outside / "keep.txt").exists())

    def test_removal_deletes_the_clone_it_was_asked_for(self):
        target = worker_git.clone_path("w1", self.root)
        target.mkdir(parents=True)
        (target / "x.txt").write_text("x\n", encoding="utf-8")
        ok, reason = workers.remove_worker_clone("w1", self.root)
        self.assertEqual((ok, reason), (True, ""))
        self.assertFalse(target.exists())

    def test_the_checkout_path_is_derived_not_asked_of_workmux(self):
        """A worker whose tmux window died is still locatable."""
        self.assertIsNone(workers.worker_checkout_path("w1", self.root))
        worker_git.clone_path("w1", self.root).mkdir(parents=True)
        self.assertEqual(workers.worker_checkout_path("w1", self.root),
                         worker_git.clone_path("w1", self.root))


class DeploymentBoundaryCase(unittest.TestCase):
    """What this suite is NOT allowed to claim."""

    def test_this_is_a_permission_simulation_not_an_identity_proof(self):
        self.assertEqual(os.getuid(), os.geteuid(),
                         "this suite makes no claim about a different uid")

    def test_the_ownership_plan_performs_nothing_and_names_the_worker(self):
        plan = worker_git.ownership_plan("w-alpha", "run002-wrk",
                                         Path("/srv/run-002"))
        paths = [entry.path for entry in plan]
        self.assertIn(str(worker_git.clone_path("w-alpha", Path("/srv/run-002"))),
                      paths)
        self.assertIn("/srv/run-002/.git/hooks", paths)
        self.assertIn("/srv/run-002/.git/worktrees", paths)
        for entry in plan:
            self.assertTrue(entry.why, entry.path)
            self.assertRegex(entry.mode, r"^0[0-7]{3}$")
        # Nothing was created: the plan is data.
        self.assertFalse(Path("/srv/run-002").exists())

    def test_the_identity_level_checks_are_retained_not_dropped(self):
        ids = [check[0] for check in worker_git.DEPLOYMENT_CHECKS]
        self.assertEqual(ids, ["V14a", "V14b", "V14c", "V14d", "V14e", "V14f"])
        for _, command, expectation in worker_git.DEPLOYMENT_CHECKS:
            self.assertTrue(command.strip() and expectation.strip())
        joined = " ".join(c[2] for c in worker_git.DEPLOYMENT_CHECKS)
        self.assertIn("CANNOT PASS", joined,
                      "the one check a single run002-wrk makes impossible "
                      "must say so, or it will be recorded as passed")


if __name__ == "__main__":
    unittest.main()
