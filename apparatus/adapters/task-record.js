// Real, local task-record adapter (C-04 live-gate adapter, one of four).
//
// Reads config/tasks.json — an actual, already-committed source already
// in this repo, not an external system — and returns the real declared
// record for a task_id, or fails closed if it does not exist.
//
// LIMITATION: config/tasks.json does not encode per-task accessibility/
// security applicability. This adapter can confirm a task_id is real and
// return its declared title/kind/depends_on/schema_changing fields; it
// cannot yet answer "is accessibility/security applicable for this PR" —
// that needs either a schema extension to config/tasks.json (not
// possible; it is a frozen imported source that must stay byte-for-byte
// unchanged) or a new, separate supplementary mapping document, in the
// same spirit as protocol/TASK-ACCEPTANCE-MAPPING-V2.md. Until one
// exists, PR-evidence applicability claims cannot be cross-checked
// against this adapter's output.

const fs = require('fs');
const path = require('path');

function loadTaskGraph(repoRoot) {
  const tasksPath = path.join(repoRoot, 'config', 'tasks.json');
  return JSON.parse(fs.readFileSync(tasksPath, 'utf8'));
}

function resolveTaskRecord(taskId, options) {
  options = options || {};
  const repoRoot = options.repoRoot;
  if (typeof repoRoot !== 'string' || repoRoot.trim().length === 0) {
    return { ok: false, reason: 'INVALID_OPTIONS', detail: 'options.repoRoot is required.' };
  }
  if (typeof taskId !== 'string' || taskId.trim().length === 0) {
    return { ok: false, reason: 'INVALID_TASK_ID', detail: 'taskId must be a non-empty string.' };
  }

  let graph;
  try {
    graph = loadTaskGraph(repoRoot);
  } catch (err) {
    return { ok: false, reason: 'NO_TASK_GRAPH', detail: 'could not read config/tasks.json.' };
  }

  const tasks = Array.isArray(graph.tasks) ? graph.tasks : [];
  const task = tasks.find(function (t) {
    return t && t.id === taskId;
  });
  if (!task) {
    return { ok: false, reason: 'NO_SUCH_TASK', detail: 'task_id "' + taskId + '" is not in config/tasks.json.' };
  }

  return {
    ok: true,
    task: {
      id: task.id,
      title: task.title,
      kind: task.kind,
      dependsOn: Array.isArray(task.depends_on) ? task.depends_on.slice() : [],
      schemaChanging: task.schema_changing === true,
    },
  };
}

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');

function resolveRun002TaskRecord(taskId) {
  return resolveTaskRecord(taskId, { repoRoot: RUN_002_REPO_ROOT });
}

module.exports = { resolveTaskRecord, resolveRun002TaskRecord };
