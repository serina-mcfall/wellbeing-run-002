const test = require('node:test');
const assert = require('node:assert/strict');
const { validateTaskGraph, validateRun002TaskGraph } = require('./validate.js');

test('the real config/tasks.json graph is valid: 9 tasks, no errors', () => {
  const result = validateRun002TaskGraph();
  assert.equal(result.ok, true);
  assert.deepEqual(result.errors, []);
  assert.equal(result.taskCount, 9);
});

test('a dangling dependency is rejected', () => {
  const result = validateTaskGraph({
    tasks: [{ id: 'A', depends_on: ['DOES-NOT-EXIST'] }],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('DANGLING_DEPENDENCY')));
});

test('a self-dependency is rejected', () => {
  const result = validateTaskGraph({
    tasks: [{ id: 'A', depends_on: ['A'] }],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('SELF_DEPENDENCY')));
});

test('a two-node cycle is rejected', () => {
  const result = validateTaskGraph({
    tasks: [
      { id: 'A', depends_on: ['B'] },
      { id: 'B', depends_on: ['A'] },
    ],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('CYCLE')));
});

test('a duplicate task_id is rejected', () => {
  const result = validateTaskGraph({
    tasks: [
      { id: 'A', depends_on: [] },
      { id: 'A', depends_on: [] },
    ],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('DUPLICATE_TASK_ID')));
});

test('a valid linear chain passes with no errors', () => {
  const result = validateTaskGraph({
    tasks: [
      { id: 'A', depends_on: [] },
      { id: 'B', depends_on: ['A'] },
      { id: 'C', depends_on: ['A', 'B'] },
    ],
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.errors, []);
  assert.equal(result.taskCount, 3);
});

test('an empty task graph fails closed, not silently ok', () => {
  const result = validateTaskGraph({ tasks: [] });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('EMPTY_TASK_GRAPH')));
});

test('a missing depends_on field is rejected, not treated as dependency-free', () => {
  const result = validateTaskGraph({
    tasks: [{ id: 'A' }],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('MALFORMED_DEPENDENCIES')));
});

test('a non-array depends_on value is rejected, not treated as dependency-free', () => {
  const result = validateTaskGraph({
    tasks: [{ id: 'A', depends_on: 'TASK-001' }],
  });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((e) => e.startsWith('MALFORMED_DEPENDENCIES')));
});
