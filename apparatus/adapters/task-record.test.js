// Tests against the REAL config/tasks.json — safe because this adapter
// only reads it, never mutates it.

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const { resolveTaskRecord, resolveRun002TaskRecord } = require('./task-record.js');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

test('a real task_id resolves to its declared fields', () => {
  const result = resolveRun002TaskRecord('TASK-002');
  assert.equal(result.ok, true);
  assert.equal(result.task.id, 'TASK-002');
  assert.equal(result.task.title, 'Check-in');
  assert.deepEqual(result.task.dependsOn, ['TASK-001']);
  assert.equal(result.task.schemaChanging, true);
});

test('a task with multiple dependencies resolves them all', () => {
  const result = resolveRun002TaskRecord('TASK-008');
  assert.equal(result.ok, true);
  assert.deepEqual(result.task.dependsOn, ['TASK-002', 'TASK-003', 'TASK-004', 'TASK-005', 'TASK-006', 'TASK-007']);
  assert.equal(result.task.schemaChanging, false);
});

test('an unknown task_id fails closed as NO_SUCH_TASK', () => {
  const result = resolveRun002TaskRecord('TASK-999');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_SUCH_TASK');
});

test('missing repoRoot on the low-level function fails closed', () => {
  const result = resolveTaskRecord('TASK-002', {});
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'INVALID_OPTIONS');
});

test('an empty task_id fails closed', () => {
  const result = resolveTaskRecord('', { repoRoot: REPO_ROOT });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'INVALID_TASK_ID');
});
