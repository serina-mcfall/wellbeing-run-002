// Fixtures here are synthetic policy examples ONLY — they are not live
// proof of a real commit, a real CI run, or a real reviewer. They test
// apparatus/pr-evidence/validate.js's offline policy logic in isolation.
// A passing fixture does not resolve C-04 and does not imply anything
// about the (separate, not-yet-built) trusted Git/CI/worker-registry/
// task-record adapters.

const test = require('node:test');
const assert = require('node:assert/strict');
const { checkOfflinePolicy } = require('./validate.js');

const HEAD_SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);

function provenance(sha, overrides) {
  return Object.assign(
    {
      source: 'fixture',
      sha: sha,
      created_at: '2026-09-24T00:00:00Z',
      kind: 'fixture-evidence',
      producer: 'Codex Reviewer',
      scope: 'pr',
      result: 'PASS',
      artifact_reference: 'https://example.invalid/artifact/1',
    },
    overrides || {}
  );
}

function basePkg() {
  return {
    task_id: 'TASK-002',
    head_sha: HEAD_SHA,
    ci: {
      sha: HEAD_SHA,
      status: 'PASS',
      source: 'github-actions',
      provenance: provenance(HEAD_SHA, { producer: 'GitHub Actions', kind: 'ci-run' }),
    },
    review: {
      sha: HEAD_SHA,
      verdict: 'REVIEW_PASS',
      findings: [],
      provenance: provenance(HEAD_SHA, { producer: 'Codex Reviewer', kind: 'code-review' }),
    },
    accessibility: {
      applicable: false,
      not_applicable_reason: 'No UI surface changed in this PR.',
      provenance: provenance(HEAD_SHA, { producer: 'Accessibility Reviewer', kind: 'accessibility-review' }),
    },
    security: {
      applicable: false,
      not_applicable_reason: 'No security-relevant surface changed in this PR.',
      provenance: provenance(HEAD_SHA, { producer: 'Security Reviewer', kind: 'security-review' }),
    },
  };
}

test('positive: a structurally clean package is policyValid with no errors', () => {
  const result = checkOfflinePolicy(basePkg());
  assert.equal(result.policyValid, true);
  assert.deepEqual(result.errors, []);
});

test('SCHEMA_INVALID: a required field missing entirely fails schema validation', () => {
  const pkg = basePkg();
  delete pkg.task_id;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('SCHEMA_INVALID')));
});

test('INTERNAL_SHA_MISMATCH: ci.sha disagreeing with head_sha is rejected', () => {
  const pkg = basePkg();
  pkg.ci.sha = OTHER_SHA;
  pkg.ci.provenance.sha = OTHER_SHA;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.includes('INTERNAL_SHA_MISMATCH') && e.includes('ci.sha')));
});

test('CI_NOT_PASS: a submitted ci.status other than PASS is rejected', () => {
  const pkg = basePkg();
  pkg.ci.status = 'FAIL';
  pkg.ci.provenance.result = 'FAIL';
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('CI_NOT_PASS')));
});

test('REVIEW_NOT_PASS: a submitted review.verdict other than REVIEW_PASS is rejected', () => {
  const pkg = basePkg();
  pkg.review.verdict = 'REVIEW_FAIL';
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('REVIEW_NOT_PASS')));
});

test('SELF_ATTESTED_OR_UNKNOWN_PRODUCER: a review producer named Builder is rejected', () => {
  const pkg = basePkg();
  pkg.review.provenance.producer = 'Builder';
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.includes('Builder')));
});

test('SELF_ATTESTED_OR_UNKNOWN_PRODUCER: an unrecognized producer name is rejected, not only Builder/Fixer', () => {
  const pkg = basePkg();
  pkg.review.provenance.producer = 'Some Made Up Reviewer';
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('SELF_ATTESTED_OR_UNKNOWN_PRODUCER')));
});

test('INFERRED_APPLICABILITY: accessibility.applicable missing entirely is rejected', () => {
  const pkg = basePkg();
  delete pkg.accessibility.applicable;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.includes('accessibility')));
});

test('INFERRED_APPLICABILITY: applicable=false without not_applicable_reason is rejected', () => {
  const pkg = basePkg();
  delete pkg.accessibility.not_applicable_reason;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.includes('not_applicable_reason')));
});

test('UNRESOLVED_BLOCKING_FINDING: an OPEN P1 review finding blocks', () => {
  const pkg = basePkg();
  pkg.review.findings = [{ id: 'TASK002-R1-CODE-001', severity: 'P1', status: 'OPEN' }];
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('UNRESOLVED_BLOCKING_FINDING')));
});

test('UNRESOLVED_BLOCKING_FINDING: a NON_FAILURE accessibility finding at P1 still blocks (no floor needed)', () => {
  const pkg = basePkg();
  pkg.accessibility.applicable = true;
  pkg.accessibility.sha = HEAD_SHA;
  pkg.accessibility.checks = [];
  pkg.accessibility.findings = [
    {
      id: 'TASK002-R1-A11Y-001',
      jev_severity: 'P1',
      classification: 'NON_FAILURE',
      non_failure_rationale: 'Reviewer judged this a P1-severity non-failure recommendation for this fixture.',
      status: 'OPEN',
    },
  ];
  delete pkg.accessibility.not_applicable_reason;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('UNRESOLVED_BLOCKING_FINDING')));
});

test('INVALID_ACCESSIBILITY_EVIDENCE: a FAILURE finding citing an identifier outside the registry fails closed', () => {
  const pkg = basePkg();
  pkg.accessibility.applicable = true;
  pkg.accessibility.sha = HEAD_SHA;
  pkg.accessibility.checks = [];
  pkg.accessibility.findings = [
    {
      id: 'TASK002-R1-A11Y-002',
      jev_severity: 'P2',
      classification: 'FAILURE',
      // Plausible-looking, nonempty, and NOT a registry identifier: the
      // registry spells this one ACC-DOD-VISIBLE_FOCUS.
      unmet_requirement: 'visible-focus-indicator',
      status: 'OPEN',
    },
  ];
  delete pkg.accessibility.not_applicable_reason;
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('INVALID_ACCESSIBILITY_EVIDENCE')));
});

// --- C-02 registry integration -------------------------------------------
// Before the registry was wired in, every FAILURE-classified accessibility
// finding was INVALID no matter what it cited. These tests pin the two
// sides of the fix: a real identifier is now recognised and graded, an
// unrecognised one still fails closed.

function withAccessibilityFinding(finding) {
  const pkg = basePkg();
  pkg.accessibility.applicable = true;
  pkg.accessibility.sha = HEAD_SHA;
  pkg.accessibility.checks = [];
  pkg.accessibility.findings = [finding];
  delete pkg.accessibility.not_applicable_reason;
  return pkg;
}

test('registry: a FAILURE citing a real registry identifier is no longer INVALID evidence', () => {
  const pkg = withAccessibilityFinding({
    id: 'TASK002-R1-A11Y-003',
    jev_severity: 'P3',
    classification: 'FAILURE',
    unmet_requirement: 'ACC-DOD-VISIBLE_FOCUS',
    status: 'OPEN',
  });
  const result = checkOfflinePolicy(pkg);
  assert.ok(
    !result.errors.some((e) => e.startsWith('INVALID_ACCESSIBILITY_EVIDENCE')),
    'a recognised citation must not be reported as invalid evidence: ' + JSON.stringify(result.errors)
  );
});

test('registry: the P1 floor still applies — a recognised P3 FAILURE is raised to P1 and blocks merge', () => {
  const { applySeverityPolicy } = require('../severity/severity-floor.js');
  const { REQUIREMENT_IDS } = require('../accessibility/requirement-registry.js');
  const floor = applySeverityPolicy(
    {
      id: 'TASK002-R1-A11Y-003',
      jev_severity: 'P3',
      classification: 'FAILURE',
      unmet_requirement: 'ACC-DOD-VISIBLE_FOCUS',
      status: 'OPEN',
    },
    REQUIREMENT_IDS
  );
  assert.equal(floor.valid, true);
  assert.equal(floor.severity, 'P1', 'the deterministic accessibility floor must raise P3 to P1');
  assert.equal(floor.floorApplied, true);
  assert.equal(floor.mergeBlocked, true);

  // ... and the validator must act on that floored severity, not the
  // reviewer-supplied P3: the package is blocked as P0/P1, not waved
  // through as a non-blocking recommendation.
  const pkg = withAccessibilityFinding({
    id: 'TASK002-R1-A11Y-003',
    jev_severity: 'P3',
    classification: 'FAILURE',
    unmet_requirement: 'ACC-DOD-VISIBLE_FOCUS',
    status: 'OPEN',
  });
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, false);
  assert.ok(result.errors.some((e) => e.startsWith('UNRESOLVED_BLOCKING_FINDING')));
});

test('registry: a P0 FAILURE citing a real identifier keeps P0 — the floor raises, it never caps', () => {
  const { applySeverityPolicy } = require('../severity/severity-floor.js');
  const { REQUIREMENT_IDS } = require('../accessibility/requirement-registry.js');
  const floor = applySeverityPolicy(
    {
      jev_severity: 'P0',
      classification: 'FAILURE',
      unmet_requirement: 'ACC-COG-CLEAR_BACK_EXIT_ROUTES',
    },
    REQUIREMENT_IDS
  );
  assert.equal(floor.valid, true);
  assert.equal(floor.severity, 'P0');
  assert.equal(floor.floorApplied, false);
});

test('registry: a near-miss citation (right requirement, wrong spelling) still fails closed', () => {
  for (const citation of ['acc-dod-visible_focus', 'ACC-DOD-VISIBLE-FOCUS', 'visible focus', 'ACC-DOD-VISIBLE_FOCUS ']) {
    const pkg = withAccessibilityFinding({
      id: 'TASK002-R1-A11Y-004',
      jev_severity: 'P1',
      classification: 'FAILURE',
      unmet_requirement: citation,
      status: 'OPEN',
    });
    const result = checkOfflinePolicy(pkg);
    assert.ok(
      result.errors.some((e) => e.startsWith('INVALID_ACCESSIBILITY_EVIDENCE')),
      'citation ' + JSON.stringify(citation) + ' must not be accepted by the registry'
    );
  }
});

test('registry: a P2 NON_FAILURE suggestion is still non-blocking and needs no registry identifier', () => {
  const pkg = withAccessibilityFinding({
    id: 'TASK002-R1-A11Y-005',
    jev_severity: 'P2',
    classification: 'NON_FAILURE',
    non_failure_rationale:
      'Keyboard operation, visible focus and heading structure were all reviewed and met; this is a polish suggestion only.',
    status: 'OPEN',
  });
  const result = checkOfflinePolicy(pkg);
  assert.equal(result.policyValid, true, 'unexpected errors: ' + JSON.stringify(result.errors));
  assert.equal(result.errors.length, 0);
});

test('registry: every identifier the registry publishes is actually accepted as a citation', () => {
  const { applySeverityPolicy } = require('../severity/severity-floor.js');
  const { REQUIREMENT_IDS } = require('../accessibility/requirement-registry.js');
  assert.ok(REQUIREMENT_IDS.length > 0, 'an empty registry would fail every citation closed');
  for (const id of REQUIREMENT_IDS) {
    const floor = applySeverityPolicy(
      { jev_severity: 'P1', classification: 'FAILURE', unmet_requirement: id },
      REQUIREMENT_IDS
    );
    assert.equal(floor.valid, true, id + ' is published by the registry but rejected by the severity policy');
    assert.equal(floor.severity, 'P1');
  }
});
