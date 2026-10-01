// REAL git mechanics for the C-04a fixture preflight.
//
// Nothing here is a mock of git. Every branch, commit, worktree and head
// SHA this module produces is created by invoking the real `git` binary
// against a throwaway repository under the OS temp directory. The SHAs it
// returns are real object names, which is what lets the C-04a scenario
// drive apparatus/adapters/git-head.js for real rather than feeding it
// invented strings.
//
// It is deliberately NOT created inside this repository's working tree:
// a fixture repo is scratch, and scratch does not belong in a checkout.
//
// The fixture writes its own config/isolation.json because
// git-head.js's worktree identity branch reads that file from the
// repoRoot it is given. That is the adapter's real contract, exercised
// here against a real file, not stubbed out.

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const COMMIT_ENV = {
  GIT_AUTHOR_NAME: 'Fixture Builder',
  GIT_AUTHOR_EMAIL: 'fixture@example.invalid',
  GIT_COMMITTER_NAME: 'Fixture Builder',
  GIT_COMMITTER_EMAIL: 'fixture@example.invalid',
  GIT_CONFIG_GLOBAL: '/dev/null',
  GIT_CONFIG_SYSTEM: '/dev/null',
};

const SHA_RE = /^[0-9a-f]{40}$/;

class FixtureRepo {
  constructor(root) {
    this.root = root;
  }

  git(args, cwd) {
    return execFileSync('git', ['-C', cwd || this.root].concat(args), {
      encoding: 'utf8',
      env: Object.assign({}, process.env, COMMIT_ENV),
      // stderr is captured rather than inherited so routine git chatter
      // ("Switched to a new branch") does not pollute test output.
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  }

  // Writes files and makes a real commit. Returns the real head SHA.
  commit(message, files) {
    for (const [relPath, contents] of Object.entries(files || {})) {
      const full = path.join(this.root, relPath);
      fs.mkdirSync(path.dirname(full), { recursive: true });
      fs.writeFileSync(full, contents);
      this.git(['add', '--', relPath]);
    }
    this.git(['commit', '--allow-empty', '-m', message]);
    const sha = this.headSha();
    if (!SHA_RE.test(sha)) {
      throw new Error('fixture repo produced a non-SHA head: ' + sha);
    }
    return sha;
  }

  createBranch(name) {
    this.git(['checkout', '-b', name]);
    return name;
  }

  checkout(ref) {
    this.git(['checkout', ref]);
  }

  headSha(cwd) {
    return this.git(['rev-parse', '--verify', 'HEAD'], cwd);
  }

  shaOfBranch(name) {
    return this.git(['rev-parse', '--verify', name]);
  }

  // Registers a real git worktree under the configured worktree_dir, so
  // git-head.js's worktree identity path can be exercised end to end.
  addWorktree(dirName, branchRef) {
    const target = path.join(this.root, '.worktrees', dirName);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    this.git(['worktree', 'add', target, branchRef]);
    return target;
  }

  destroy() {
    fs.rmSync(this.root, { recursive: true, force: true });
  }
}

function createFixtureRepo() {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'c04a-fixture-')));
  const repo = new FixtureRepo(root);
  repo.git(['init', '--initial-branch=main']);
  fs.mkdirSync(path.join(root, 'config'), { recursive: true });
  fs.writeFileSync(
    path.join(root, 'config', 'isolation.json'),
    JSON.stringify({ experiment_id: 'c04a-fixture', workspace: root, worktree_dir: '.worktrees' }, null, 2)
  );
  fs.writeFileSync(path.join(root, '.gitignore'), '.worktrees/\n');
  repo.commit('fixture: initial commit', {
    'README.md': '# C-04a fixture repository\n',
  });
  return repo;
}

module.exports = { createFixtureRepo, FixtureRepo, SHA_RE };
