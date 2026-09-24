// Task graph validator (BOOTSTRAP deliverable 2, "task graph
// validation"). Reads config/tasks.json (or accepts an in-memory
// fixture for testing) and checks: every depends_on entry references a
// real task_id (no dangling dependency); no task depends on itself; no
// cycle exists (Protocol v2 "Task graph": "no cycles/impossible
// dependencies"); no duplicate task_id.
//
// Does NOT judge whether a dependency is "necessary" — Protocol v2 also
// requires "no dependency added merely to serialize" and "no dependency
// removed merely to avoid blocking," which need human/product judgement
// this module does not have and cannot substitute for.

const fs = require('fs');
const path = require('path');

function loadAllTasks(repoRoot) {
  const tasksPath = path.join(repoRoot, 'config', 'tasks.json');
  const graph = JSON.parse(fs.readFileSync(tasksPath, 'utf8'));
  return Array.isArray(graph.tasks) ? graph.tasks : [];
}

function validateTaskGraph(options) {
  options = options || {};

  let tasks;
  if (Array.isArray(options.tasks)) {
    tasks = options.tasks;
  } else {
    const repoRoot = options.repoRoot;
    if (typeof repoRoot !== 'string' || repoRoot.trim().length === 0) {
      return { ok: false, errors: ['INVALID_OPTIONS: repoRoot (or tasks) is required.'], taskCount: 0 };
    }
    try {
      tasks = loadAllTasks(repoRoot);
    } catch (err) {
      return { ok: false, errors: ['NO_TASK_GRAPH: could not read config/tasks.json.'], taskCount: 0 };
    }
  }

  if (tasks.length === 0) {
    return {
      ok: false,
      errors: ['EMPTY_TASK_GRAPH: no tasks found — this may indicate a malformed or missing tasks array, not a genuinely empty graph.'],
      taskCount: 0,
    };
  }

  const errors = [];
  const byId = new Map();
  for (const t of tasks) {
    if (!t || typeof t.id !== 'string') {
      errors.push('MALFORMED_TASK: an entry has no string id.');
      continue;
    }
    if (byId.has(t.id)) {
      errors.push('DUPLICATE_TASK_ID: "' + t.id + '" appears more than once.');
    }
    byId.set(t.id, t);
  }

  for (const t of byId.values()) {
    if (!Array.isArray(t.depends_on)) {
      errors.push('MALFORMED_DEPENDENCIES: "' + t.id + '".depends_on is missing or not an array.');
      continue;
    }
    for (const dep of t.depends_on) {
      if (typeof dep !== 'string' || dep.trim().length === 0) {
        errors.push('MALFORMED_DEPENDENCY_ENTRY: "' + t.id + '" has a non-string or empty dependency entry.');
        continue;
      }
      if (dep === t.id) {
        errors.push('SELF_DEPENDENCY: "' + t.id + '" depends on itself.');
      } else if (!byId.has(dep)) {
        errors.push('DANGLING_DEPENDENCY: "' + t.id + '" depends on unknown task "' + dep + '".');
      }
    }
  }

  const WHITE = 0;
  const GRAY = 1;
  const BLACK = 2;
  const color = new Map();
  for (const id of byId.keys()) {
    color.set(id, WHITE);
  }

  function visit(id, stack) {
    color.set(id, GRAY);
    stack.push(id);
    const t = byId.get(id);
    const deps = t && Array.isArray(t.depends_on) ? t.depends_on : [];
    for (const dep of deps) {
      if (!byId.has(dep)) continue; // already reported as dangling
      if (color.get(dep) === GRAY) {
        const cycleStart = stack.indexOf(dep);
        errors.push('CYCLE: ' + stack.slice(cycleStart).concat(dep).join(' -> '));
      } else if (color.get(dep) === WHITE) {
        visit(dep, stack);
      }
    }
    stack.pop();
    color.set(id, BLACK);
  }

  for (const id of byId.keys()) {
    if (color.get(id) === WHITE) {
      visit(id, []);
    }
  }

  return { ok: errors.length === 0, errors: errors, taskCount: byId.size };
}

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');

function validateRun002TaskGraph() {
  return validateTaskGraph({ repoRoot: RUN_002_REPO_ROOT });
}

module.exports = { validateTaskGraph, validateRun002TaskGraph };
