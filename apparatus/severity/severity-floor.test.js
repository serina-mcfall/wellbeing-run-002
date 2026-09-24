const test = require('node:test');
const assert = require('node:assert/strict');
const { applySeverityPolicy } = require('./severity-floor.js');

// Placeholder fixture only — NOT the canonical accessibility requirement
// registry. That registry is owned by the PR evidence validator (C-04)
// and does not exist yet; this stands in for it so these tests can check
// registry-membership behaviour in isolation.
const KNOWN_REQUIREMENT_IDS = ['visible-focus-indicator', 'keyboard-focus-trap'];

test('no classification/citation/rationale at all is INVALID and blocks merge', () => {
  const result = applySeverityPolicy({ jev_severity: 'P2' });
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
  assert.equal(result.severity, null);
});

test('especially: Jev P2 accessibility failure is raised to P1 and blocks merge', () => {
  const result = applySeverityPolicy(
    { jev_severity: 'P2', classification: 'FAILURE', unmet_requirement: 'visible-focus-indicator' },
    KNOWN_REQUIREMENT_IDS
  );
  assert.equal(result.valid, true);
  assert.equal(result.severity, 'P1');
  assert.equal(result.floorApplied, true);
  assert.equal(result.mergeBlocked, true);
});

test('a failure already P0 stays P0 (floor raises, never caps) and blocks merge', () => {
  const result = applySeverityPolicy(
    { jev_severity: 'P0', classification: 'FAILURE', unmet_requirement: 'keyboard-focus-trap' },
    KNOWN_REQUIREMENT_IDS
  );
  assert.equal(result.severity, 'P0');
  assert.equal(result.floorApplied, false);
  assert.equal(result.mergeBlocked, true);
});

test('a valid NON_FAILURE P3 recommendation does not block merge', () => {
  const result = applySeverityPolicy({
    jev_severity: 'P3',
    classification: 'NON_FAILURE',
    non_failure_rationale: 'Checked the definition of done; contrast already meets the requirement.',
  });
  assert.equal(result.valid, true);
  assert.equal(result.severity, 'P3');
  assert.equal(result.mergeBlocked, false);
});

test('a valid NON_FAILURE P2 recommendation also does not independently block merge', () => {
  const result = applySeverityPolicy({
    jev_severity: 'P2',
    classification: 'NON_FAILURE',
    non_failure_rationale: 'Checked the cognitive accessibility list; nothing unmet, minor polish only.',
  });
  assert.equal(result.mergeBlocked, false);
});

test('contradictory: FAILURE with a non_failure_rationale is INVALID', () => {
  const result = applySeverityPolicy(
    {
      jev_severity: 'P2',
      classification: 'FAILURE',
      unmet_requirement: 'visible-focus-indicator',
      non_failure_rationale: 'but also this is fine actually',
    },
    KNOWN_REQUIREMENT_IDS
  );
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
});

test('contradictory: NON_FAILURE with an unmet_requirement is INVALID', () => {
  const result = applySeverityPolicy({
    jev_severity: 'P3',
    classification: 'NON_FAILURE',
    unmet_requirement: 'visible-focus-indicator',
    non_failure_rationale: 'covering both bases',
  });
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
});

test('an arbitrary nonempty citation not in the known registry is INVALID', () => {
  const result = applySeverityPolicy(
    { jev_severity: 'P2', classification: 'FAILURE', unmet_requirement: 'made-up-requirement' },
    KNOWN_REQUIREMENT_IDS
  );
  assert.equal(result.valid, false);
  assert.match(result.reason, /does not match an/);
});

test('a citation fails closed when no registry is supplied at all', () => {
  const result = applySeverityPolicy({
    jev_severity: 'P2',
    classification: 'FAILURE',
    unmet_requirement: 'visible-focus-indicator',
  });
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
});

test('fails closed on an unknown classification value instead of throwing', () => {
  const result = applySeverityPolicy({ jev_severity: 'P2', classification: 'MAYBE' });
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
});

test('fails closed on an unknown severity value instead of throwing', () => {
  const result = applySeverityPolicy({
    jev_severity: 'P9',
    classification: 'FAILURE',
    unmet_requirement: 'visible-focus-indicator',
  });
  assert.equal(result.valid, false);
  assert.equal(result.mergeBlocked, true);
});

test('fails closed on a malformed (non-object) finding instead of throwing', () => {
  assert.equal(applySeverityPolicy(null).valid, false);
  assert.equal(applySeverityPolicy('not an object').valid, false);
  assert.equal(applySeverityPolicy(['array']).valid, false);
  assert.equal(applySeverityPolicy(undefined).mergeBlocked, true);
});
