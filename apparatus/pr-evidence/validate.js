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
// The accessibility requirement registry (severity-floor.js's
// knownRequirementIds) IS now wired in: this module owns it, as
// severity-floor.js always said the PR evidence validator would, and
// supplies apparatus/accessibility/requirement-registry.js — the
// identifier list derived from product/ACCESSIBILITY.md's stated
// requirements, per protocol/SEVERITY-POLICY.md rule 1. A FAILURE-
// classified accessibility finding citing a registry identifier can
// therefore now be VALID and receive its post-floor severity; one citing
// anything else still fails closed as INVALID. That is recognition of a
// citation, not confirmation of it: see requirement-registry.js's scope
// limit. It is not one of the four adapters above, and it does not make
// this checker a live merge gate.
//
// Why the registry is enforced HERE and not as a schema enum: this
// module's severity handling is deliberately layered the same way the
// schema's own accessibilityFinding description states ("POST-floor
// severity ... is computed by the validator, so the ... exclusion for
// these findings is a validator rule, not a schema rule"). An
// unrecognised citation must surface as INVALID_ACCESSIBILITY_EVIDENCE,
// whose documented resolution path is escalation to an independent
// reviewer (SEVERITY-POLICY.md rules 3-4); a schema enum would instead
// report SCHEMA_INVALID, which says "this package is malformed" and
// offers no such path. protocol/PR-EVIDENCE-V2.schema.json is also a
// frozen imported source this work must not edit.

const Ajv2020 = require('ajv/dist/2020');
const fs = require('fs');
const path = require('path');
const { applySeverityPolicy } = require('../severity/severity-floor.js');
const { REQUIREMENT_IDS } = require('../accessibility/requirement-registry.js');

const SCHEMA_PATH = path.join(__dirname, '..', '..', 'protocol', 'PR-EVIDENCE-V2.schema.json');
const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));
const ajv = new Ajv2020({ strict: true, allErrors: true });
const validateSchema = ajv.compile(schema);

const SHA_RE = /^[0-9a-f]{40}$/;
const BLOCKING_SEVERITIES = new Set(['P0', 'P1']);
// The one independent code-review role Protocol v2 "Agent organisation"
// actually names ("Codex Reviewer: independent read-only engineering
// review"). An allow-list, not a deny-list: any producer that is not
// this exact role is rejected, not just "Builder"/"Fixer" by name.
const ALLOWED_REVIEW_PRODUCERS = new Set(['Codex Reviewer']);

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
  if (!isNonEmptyString(producer) || !ALLOWED_REVIEW_PRODUCERS.has(producer)) {
    errors.push('SELF_ATTESTED_OR_UNKNOWN_PRODUCER: review.provenance.producer is "' + producer + '".');
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
    const floorResult = applySeverityPolicy(f, REQUIREMENT_IDS);
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

// CLI entry point so CI can actually run this checker over submitted
// evidence packages (Protocol v2 PR contract: "CI validates the schema").
// Fails closed: no paths, an unreadable file, or unparseable JSON are all
// non-zero exits, never a silent pass. A zero exit still means only what
// the header says — offline policy, not live merge eligibility.
if (require.main === module) {
  const paths = process.argv.slice(2);
  if (paths.length === 0) {
    process.stderr.write('usage: node apparatus/pr-evidence/validate.js <evidence-package.json> [...]\n');
    process.exit(2);
  }
  let failed = 0;
  for (const p of paths) {
    let pkg;
    try {
      pkg = JSON.parse(fs.readFileSync(p, 'utf8'));
    } catch (err) {
      process.stderr.write(p + ': UNREADABLE — ' + err.message + '\n');
      failed += 1;
      continue;
    }
    const result = checkOfflinePolicy(pkg);
    if (result.policyValid) {
      process.stdout.write(p + ': OFFLINE_POLICY_OK (not a live merge gate)\n');
    } else {
      failed += 1;
      process.stderr.write(p + ': OFFLINE_POLICY_FAILED\n');
      for (const e of result.errors) {
        process.stderr.write('  - ' + e + '\n');
      }
    }
  }
  process.exit(failed === 0 ? 0 : 1);
}
