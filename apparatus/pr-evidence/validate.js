// Run 002 PR evidence OFFLINE POLICY checker (C-04 proposal).
//
// This module checks only what is computable from the submitted package
// data alone, with no external trust: JSON Schema structure
// (protocol/PR-EVIDENCE-V2.schema.json), the accessibility P1 severity
// floor, blocking (P0/P1) findings, and a few purely static rules (a
// producer literally named "Builder" or "Fixer" is rejected; every
// applicability field must be explicit, never inferred from an absent
// field).
//
// IT DOES NOT AND CANNOT DETERMINE LIVE MERGE ELIGIBILITY. Nothing here
// is proof that head_sha is the PR's real current commit, that CI truly
// passed, that a reviewer is genuinely independent, or that a task
// record truly agrees a check is (in)applicable — those require live
// Git, CI, worker-registry, and task-record ADAPTERS that do not exist
// in this repository yet and will be implemented and tested separately.
// Error codes are named to reflect that: INTERNAL_SHA_MISMATCH is
// self-consistency only, not "stale"; CI_NOT_PASS reads a submitted
// field, it does not confirm CI actually ran. Fixture inputs used to
// test this module are synthetic policy examples, not live proof of
// anything. policyValid: true is necessary but nowhere near sufficient
// for the live merge gate, and does not resolve C-04.
//
// The accessibility requirement registry (protocol/SEVERITY-POLICY.md /
// severity-floor.js's knownRequirementIds) is not wired in here either —
// it is not one of the four adapters above, but it is equally unbuilt.
// Every FAILURE-classified accessibility finding fails closed as INVALID
// regardless of its citation, by severity-floor.js's own design.

const Ajv2020 = require('ajv/dist/2020');
const fs = require('fs');
const path = require('path');
const { applySeverityPolicy } = require('../severity/severity-floor.js');

const SCHEMA_PATH = path.join(__dirname, '..', '..', 'protocol', 'PR-EVIDENCE-V2.schema.json');
const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));
const ajv = new Ajv2020({ strict: true, allErrors: true });
const validateSchema = ajv.compile(schema);

const SHA_RE = /^[0-9a-f]{40}$/;
const BLOCKING_SEVERITIES = new Set(['P0', 'P1']);
const DISALLOWED_PRODUCERS = new Set(['Builder', 'Fixer']);

function isValidSha(value) {
  return typeof value === 'string' && SHA_RE.test(value);
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function checkOfflinePolicy(pkg) {
  const errors = [];

  const schemaOk = validateSchema(pkg);
  if (!schemaOk) {
    for (const e of validateSchema.errors || []) {
      errors.push('SCHEMA_INVALID: ' + (e.instancePath || '(root)') + ' ' + e.message);
    }
  }

  if (pkg === null || typeof pkg !== 'object' || Array.isArray(pkg)) {
    return { policyValid: false, errors: errors };
  }

  const headSha = pkg.head_sha;
  if (!isValidSha(headSha)) {
    errors.push('MALFORMED_SHA: head_sha is missing, abbreviated, or malformed.');
  }
  if (pkg.ci && isValidSha(headSha) && pkg.ci.sha !== headSha) {
    errors.push('INTERNAL_SHA_MISMATCH: ci.sha does not match head_sha.');
  }
  if (pkg.review && isValidSha(headSha) && pkg.review.sha !== headSha) {
    errors.push('INTERNAL_SHA_MISMATCH: review.sha does not match head_sha.');
  }

  if (!pkg.ci || pkg.ci.status !== 'PASS') {
    errors.push('CI_NOT_PASS: ci.status is "' + (pkg.ci && pkg.ci.status) + '".');
  }
  if (!pkg.review || pkg.review.verdict !== 'REVIEW_PASS') {
    errors.push('REVIEW_NOT_PASS: review.verdict is "' + (pkg.review && pkg.review.verdict) + '".');
  }
  const producer = pkg.review && pkg.review.provenance && pkg.review.provenance.producer;
  if (isNonEmptyString(producer) && DISALLOWED_PRODUCERS.has(producer)) {
    errors.push('SELF_ATTESTED_PRODUCER: review.provenance.producer is "' + producer + '".');
  }

  let accessibilityFindings = [];
  const accApplicable =
    pkg.accessibility && typeof pkg.accessibility === 'object' ? pkg.accessibility.applicable : undefined;
  if (typeof accApplicable !== 'boolean') {
    errors.push('INFERRED_APPLICABILITY: accessibility.applicable is missing or not a boolean.');
  } else if (accApplicable === true) {
    accessibilityFindings = Array.isArray(pkg.accessibility.findings) ? pkg.accessibility.findings : [];
  } else if (!isNonEmptyString(pkg.accessibility.not_applicable_reason)) {
    errors.push('INFERRED_APPLICABILITY: accessibility.applicable=false without not_applicable_reason.');
  }

  let securityFindings = [];
  const secApplicable = pkg.security && typeof pkg.security === 'object' ? pkg.security.applicable : undefined;
  if (typeof secApplicable !== 'boolean') {
    errors.push('INFERRED_APPLICABILITY: security.applicable is missing or not a boolean.');
  } else if (secApplicable === true) {
    securityFindings = Array.isArray(pkg.security.findings) ? pkg.security.findings : [];
  } else if (!isNonEmptyString(pkg.security.not_applicable_reason)) {
    errors.push('INFERRED_APPLICABILITY: security.applicable=false without not_applicable_reason.');
  }

  const reviewFindings = pkg.review && Array.isArray(pkg.review.findings) ? pkg.review.findings : [];
  let blockingFindingFound = false;
  for (const f of reviewFindings) {
    if (f && BLOCKING_SEVERITIES.has(f.severity)) {
      blockingFindingFound = true;
    }
  }
  for (const f of securityFindings) {
    if (f && BLOCKING_SEVERITIES.has(f.severity)) {
      blockingFindingFound = true;
    }
  }
  for (const f of accessibilityFindings) {
    const floorResult = applySeverityPolicy(f, undefined);
    if (!floorResult.valid) {
      errors.push('INVALID_ACCESSIBILITY_EVIDENCE: ' + (f && f.id) + ' — ' + floorResult.reason);
      blockingFindingFound = true;
    } else if (BLOCKING_SEVERITIES.has(floorResult.severity)) {
      blockingFindingFound = true;
    }
  }
  if (blockingFindingFound) {
    errors.push('UNRESOLVED_BLOCKING_FINDING: at least one P0/P1 finding is present.');
  }

  return { policyValid: errors.length === 0, errors: errors };
}

module.exports = { checkOfflinePolicy: checkOfflinePolicy };
