// All git operations here run against ISOLATED temp fixture repositories
// under os.tmpdir(), never against the real Run 002 repository or its
// branches/worktrees. These fixtures are synthetic test data only — not
// live proof of anything about the real Run 002 repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { resolveTrustedHeadSha, TASK_BRANCH_PREFIX } = require('./git-head.js');

function git(cwd, args) {
  return execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' }).trim();
}

function makeRepo(dirPrefix) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), dirPrefix));
  git(dir, ['init', '--quiet']);
  git(dir, ['config', 'user.email', 'fixture@example.invalid']);
  git(dir, ['config', 'user.name', 'Fixture']);
  fs.writeFileSync(path.join(dir, 'README.md'), 'fixture\n');
  git(dir, ['add', 'README.md']);
  git(dir, ['commit', '--quiet', '-m', 'initial']);
  return dir;
}

let repoRoot;
let worktreeDir;
let taskWorktreePath;
let cleanupDirs;

test.before(() => {
  cleanupDirs = [];
  repoRoot = makeRepo('run002-git-adapter-repo-');
  cleanupDirs.push(repoRoot);

  worktreeDir = path.join(repoRoot, '.worktrees');
  fs.mkdirSync(worktreeDir);

  fs.mkdirSync(path.join(repoRoot, 'config'), { recursive: true });
  fs.writeFileSync(
    path.join(repoRoot, 'config', 'isolation.json'),
    JSON.stringify({ workspace: repoRoot, worktree_dir: '.worktrees' }, null, 2)
  );
  git(repoRoot, ['add', 'config/isolation.json']);
  git(repoRoot, ['commit', '--quiet', '-m', 'add fixture isolation config']);

  git(repoRoot, ['branch', TASK_BRANCH_PREFIX + 'task-abc']);
  taskWorktreePath = path.join(worktreeDir, 'task-abc');
  git(repoRoot, ['worktree', 'add', taskWorktreePath, TASK_BRANCH_PREFIX + 'task-abc']);
});

test.after(() => {
  for (const dir of cleanupDirs) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('positive: a real run-002/ branch resolves to the SHA git itself reports', () => {
  const expected = git(repoRoot, ['rev-parse', TASK_BRANCH_PREFIX + 'task-abc']);
  const result = resolveTrustedHeadSha({ kind: 'branch', ref: TASK_BRANCH_PREFIX + 'task-abc' }, { repoRoot });
  assert.equal(result.ok, true);
  assert.equal(result.sha, expected);
});

test('positive: a real registered worktree resolves to its own HEAD SHA', () => {
  const expected = git(taskWorktreePath, ['rev-parse', 'HEAD']);
  const result = resolveTrustedHeadSha(
    { kind: 'worktree', path: path.relative(repoRoot, taskWorktreePath) },
    { repoRoot }
  );
  assert.equal(result.ok, true);
  assert.equal(result.sha, expected);
});

test('a head change makes an earlier resolved SHA stale', () => {
  const before = resolveTrustedHeadSha({ kind: 'branch', ref: TASK_BRANCH_PREFIX + 'task-abc' }, { repoRoot });
  assert.equal(before.ok, true);

  fs.writeFileSync(path.join(taskWorktreePath, 'note.txt'), 'a change\n');
  git(taskWorktreePath, ['add', 'note.txt']);
  git(taskWorktreePath, ['commit', '--quiet', '-m', 'advance the head']);

  const after = resolveTrustedHeadSha({ kind: 'branch', ref: TASK_BRANCH_PREFIX + 'task-abc' }, { repoRoot });
  assert.equal(after.ok, true);
  assert.notEqual(after.sha, before.sha, 'evidence pinned to before.sha is now stale relative to the trusted head');
});

test('reject: "main" is not under the task-branch prefix', () => {
  const result = resolveTrustedHeadSha({ kind: 'branch', ref: 'main' }, { repoRoot });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'BRANCH_PREFIX_REQUIRED');
});

test('reject: an unknown ref under the prefix does not exist', () => {
  const result = resolveTrustedHeadSha({ kind: 'branch', ref: TASK_BRANCH_PREFIX + 'does-not-exist' }, { repoRoot });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_SUCH_REF');
});

test('reject: a symlink inside worktree_dir pointing outside it is an escape', () => {
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'run002-git-adapter-outside-'));
  cleanupDirs.push(outside);
  const linkPath = path.join(worktreeDir, 'escape-link');
  fs.symlinkSync(outside, linkPath);
  const result = resolveTrustedHeadSha(
    { kind: 'worktree', path: path.relative(repoRoot, linkPath) },
    { repoRoot }
  );
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'WORKTREE_OUTSIDE_ISOLATION_DIR');
});

test('reject: an ordinary subdirectory of worktree_dir that git never registered', () => {
  const plainDir = path.join(worktreeDir, 'not-a-worktree');
  fs.mkdirSync(plainDir);
  const result = resolveTrustedHeadSha(
    { kind: 'worktree', path: path.relative(repoRoot, plainDir) },
    { repoRoot }
  );
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NOT_A_REGISTERED_WORKTREE');
});

test('reject: a real git worktree that belongs to a different repository', () => {
  const foreignRepo = makeRepo('run002-git-adapter-foreign-');
  cleanupDirs.push(foreignRepo);
  const plantedPath = path.join(worktreeDir, 'foreign');
  fs.cpSync(foreignRepo, plantedPath, { recursive: true });
  const result = resolveTrustedHeadSha(
    { kind: 'worktree', path: path.relative(repoRoot, plantedPath) },
    { repoRoot }
  );
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NOT_A_REGISTERED_WORKTREE');
});
