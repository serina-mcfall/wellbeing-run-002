// Trusted Git head-SHA adapter (C-04 live-gate adapter, one of four).
//
// git rev-parse HEAD on Run 002's own checkout is main's HEAD, which is
// NOT proof of any PR's current head. This adapter resolves the trusted
// current commit for a SPECIFIC Run 002 task identity only:
//   - a branch ref, which MUST start with TASK_BRANCH_PREFIX (this
//     rejects "main" and anything outside Run 002's task-branch
//     namespace by construction, not by convention);
//   - a worktree path, which must survive three checks in order: its
//     REAL (symlink-resolved) path must be inside the REAL, resolved
//     configured worktree_dir (config/isolation.json); `git worktree
//     list --porcelain`, run against this exact repository, must report
//     that real path as a registered worktree; only then is HEAD read.
//
// Fails closed on every branch: a missing/unknown ref, a wrong-prefix
// branch, a worktree path outside the configured directory (directly or
// via a symlink), an existing-but-unregistered directory (an ordinary
// subdirectory, or a worktree that belongs to a different repository),
// or a malformed identity all return { ok: false, reason, detail } —
// never a fabricated SHA and never a silent fallback to main.
//
// resolveTrustedHeadSha(identity, { repoRoot }) is the low-level,
// repo-agnostic function — repoRoot is required and NOT defaulted to
// process.cwd(), so a caller must say explicitly which repository it
// means. It exists to be unit-tested against an isolated fixture repo.
//
// resolveRun002TrustedHeadSha(identity) is the real entrypoint for Run
// 002 work: it derives repoRoot from this module's own file location
// (not from any caller-supplied value), reads config/isolation.json from
// there, and refuses to proceed unless that config's "workspace" field
// matches this module's own real location. A caller cannot point this
// function at a different repository (e.g. Run 001) at all.

const { execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const SHA_RE = /^[0-9a-f]{40}$/;
const TASK_BRANCH_PREFIX = 'run-002/';

function safeRealpath(candidatePath) {
  try {
    return fs.realpathSync.native(candidatePath);
  } catch (err) {
    return null;
  }
}

function loadWorktreeDir(repoRoot) {
  const isolationPath = path.join(repoRoot, 'config', 'isolation.json');
  const isolation = JSON.parse(fs.readFileSync(isolationPath, 'utf8'));
  return path.resolve(repoRoot, isolation.worktree_dir);
}

function listRegisteredWorktrees(repoRoot) {
  let output;
  try {
    output = execFileSync('git', ['-C', repoRoot, 'worktree', 'list', '--porcelain'], { encoding: 'utf8' });
  } catch (err) {
    return null;
  }
  const paths = [];
  for (const line of output.split('\n')) {
    if (line.startsWith('worktree ')) {
      const real = safeRealpath(line.slice('worktree '.length).trim());
      if (real) paths.push(real);
    }
  }
  return paths;
}

function resolveTrustedHeadSha(identity, options) {
  options = options || {};
  const repoRoot = options.repoRoot;
  if (typeof repoRoot !== 'string' || repoRoot.trim().length === 0) {
    return { ok: false, reason: 'INVALID_OPTIONS', detail: 'options.repoRoot is required.' };
  }
  if (!identity || typeof identity !== 'object') {
    return { ok: false, reason: 'INVALID_IDENTITY', detail: 'identity must be an object.' };
  }

  if (identity.kind === 'branch') {
    if (typeof identity.ref !== 'string' || identity.ref.trim().length === 0) {
      return { ok: false, reason: 'INVALID_IDENTITY', detail: 'branch identity requires a non-empty ref.' };
    }
    if (!identity.ref.startsWith(TASK_BRANCH_PREFIX)) {
      return {
        ok: false,
        reason: 'BRANCH_PREFIX_REQUIRED',
        detail: 'ref "' + identity.ref + '" does not start with the required "' + TASK_BRANCH_PREFIX + '" prefix.',
      };
    }
    let sha;
    try {
      sha = execFileSync('git', ['-C', repoRoot, 'rev-parse', '--verify', identity.ref], { encoding: 'utf8' }).trim();
    } catch (err) {
      return { ok: false, reason: 'NO_SUCH_REF', detail: 'git could not resolve ref "' + identity.ref + '".' };
    }
    if (!SHA_RE.test(sha)) {
      return { ok: false, reason: 'UNEXPECTED_GIT_OUTPUT', detail: 'git returned a non-SHA value.' };
    }
    return { ok: true, sha: sha, resolvedFrom: 'branch:' + identity.ref };
  }

  if (identity.kind === 'worktree') {
    if (typeof identity.path !== 'string' || identity.path.trim().length === 0) {
      return { ok: false, reason: 'INVALID_IDENTITY', detail: 'worktree identity requires a non-empty path.' };
    }
    let worktreeDir;
    try {
      worktreeDir = loadWorktreeDir(repoRoot);
    } catch (err) {
      return { ok: false, reason: 'NO_ISOLATION_CONFIG', detail: 'could not read config/isolation.json.' };
    }
    const realWorktreeDir = safeRealpath(worktreeDir);
    if (!realWorktreeDir) {
      return { ok: false, reason: 'NO_ISOLATION_CONFIG', detail: 'configured worktree_dir does not exist.' };
    }

    const candidatePath = path.resolve(repoRoot, identity.path);
    const realCandidatePath = safeRealpath(candidatePath);
    if (!realCandidatePath) {
      return { ok: false, reason: 'NO_SUCH_REF', detail: 'worktree path does not exist on disk.' };
    }

    const relative = path.relative(realWorktreeDir, realCandidatePath);
    const isInside = relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
    if (!isInside) {
      return {
        ok: false,
        reason: 'WORKTREE_OUTSIDE_ISOLATION_DIR',
        detail: 'real path of "' + identity.path + '" is not inside the configured worktree_dir (checked after resolving symlinks).',
      };
    }

    const registered = listRegisteredWorktrees(repoRoot);
    if (!registered) {
      return { ok: false, reason: 'GIT_WORKTREE_LIST_FAILED', detail: 'could not list registered worktrees.' };
    }
    if (!registered.includes(realCandidatePath)) {
      return {
        ok: false,
        reason: 'NOT_A_REGISTERED_WORKTREE',
        detail: 'git does not report this exact path as a registered worktree of this repository.',
      };
    }

    let sha;
    try {
      sha = execFileSync('git', ['-C', realCandidatePath, 'rev-parse', '--verify', 'HEAD'], { encoding: 'utf8' }).trim();
    } catch (err) {
      return { ok: false, reason: 'NO_SUCH_REF', detail: 'git could not resolve HEAD in that worktree.' };
    }
    if (!SHA_RE.test(sha)) {
      return { ok: false, reason: 'UNEXPECTED_GIT_OUTPUT', detail: 'git returned a non-SHA value.' };
    }
    return { ok: true, sha: sha, resolvedFrom: 'worktree:' + realCandidatePath };
  }

  return { ok: false, reason: 'INVALID_IDENTITY', detail: 'identity.kind must be "branch" or "worktree".' };
}

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');

function resolveRun002TrustedHeadSha(identity) {
  let isolation;
  try {
    isolation = JSON.parse(fs.readFileSync(path.join(RUN_002_REPO_ROOT, 'config', 'isolation.json'), 'utf8'));
  } catch (err) {
    return { ok: false, reason: 'NO_ISOLATION_CONFIG', detail: 'could not read config/isolation.json.' };
  }
  const configuredWorkspace = safeRealpath(isolation.workspace);
  const actualRoot = safeRealpath(RUN_002_REPO_ROOT);
  if (!configuredWorkspace || !actualRoot || configuredWorkspace !== actualRoot) {
    return {
      ok: false,
      reason: 'WORKSPACE_MISMATCH',
      detail: "config/isolation.json's workspace does not match this module's own real location.",
    };
  }
  return resolveTrustedHeadSha(identity, { repoRoot: actualRoot });
}

module.exports = { resolveTrustedHeadSha, resolveRun002TrustedHeadSha, TASK_BRANCH_PREFIX };
