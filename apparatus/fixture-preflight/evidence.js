// Builds REAL PR-EVIDENCE-V2 packages bound to REAL fixture git SHAs.
//
// "Real" here means two specific things:
//   - every sha field is an actual commit object name produced by the
//     fixture repository, never a placeholder like 'a'.repeat(40);
//   - the packages are validated by the real validator
//     (apparatus/pr-evidence/validate.js) against the real schema
//     (protocol/PR-EVIDENCE-V2.schema.json) with no stand-in.
//
// The content of the checks is of course synthetic — no axe run, no CI
// job and no reviewer produced it. That is the agreed shape of a FIXTURE
// preflight (C-04a requirement 1): the git mechanics and the
// schema/validator/adapter calls are real; the subject under review is a
// fixture.

const A11Y_CHECK_IDS = [
  'RESPONSIVE_375PX',
  'NO_OVERFLOW_CLIPPING_OVERLAP',
  'TOUCH_TARGETS',
  'KEYBOARD_OPERATION',
  'FOCUS_ORDER_VISIBLE_NO_TRAPS',
  'LABELS_AND_TEXT_ERRORS',
  'REDUCED_MOTION_NO_FLASHING_AUTOPLAY',
  'AXE_SCAN',
  'SCREENSHOTS_ARTIFACTS',
  'COGNITIVE_SENSORY_REVIEW',
];

const SECURITY_CHECK_IDS = [
  'SECRETS',
  'AUTH_SESSION',
  'AUTHORIZATION',
  'SIGN_OUT',
  'REDIRECTS_URLS',
  'XSS_UNSAFE_HTML',
  'INJECTION_PATH_COMMAND',
  'ERROR_LEAKAGE',
  'RLS_LEAST_PRIVILEGE',
  'SERVER_CLIENT_BOUNDARIES',
  'DEPENDENCY_RISK',
  'AI_INPUT_OUTPUT_HANDLING',
];

// Finding identifiers use the schema's TASK000 form; the task graph uses
// TASK-000. One source of truth, derived, so a renamed task cannot drift
// the two apart silently.
function findingPrefix(taskId) {
  return taskId.replace(/-/g, '');
}

function provenance(sha, producer, kind, result) {
  return {
    source: 'c04a-fixture-preflight',
    sha: sha,
    created_at: '2026-10-01T00:00:00Z',
    kind: kind,
    producer: producer,
    scope: 'pr',
    result: result,
    artifact_reference: 'fixture://c04a/' + kind + '/' + sha.slice(0, 12),
  };
}

function a11yChecks(sha) {
  return A11Y_CHECK_IDS.map((id) => ({
    check_id: id,
    result: 'PASS',
    artifact_reference: 'fixture://c04a/a11y/' + id + '/' + sha.slice(0, 12),
    sha: sha,
  }));
}

function securityChecks(sha, relevantIds) {
  const relevant = new Set(relevantIds || ['SECRETS', 'INJECTION_PATH_COMMAND']);
  return SECURITY_CHECK_IDS.map((id) => {
    if (!relevant.has(id)) {
      return { check_id: id, relevant: false };
    }
    return {
      check_id: id,
      relevant: true,
      result: 'PASS',
      artifact_reference: 'fixture://c04a/security/' + id + '/' + sha.slice(0, 12),
      sha: sha,
    };
  });
}

// opts:
//   taskId, sha (REAL), cycle
//   ciStatus:        'PASS' | 'FAIL' | 'PENDING'
//   reviewVerdict:   'REVIEW_PASS' | 'REVIEW_FAIL' | 'PENDING'
//   codeFindings:    array of { severity, status } — ids are generated
//   a11yFindings:    array of { jev_severity, classification, status,
//                               non_failure_rationale, unmet_requirement }
//   securityFindings: array of { severity, status }
//   reviewProducer:  defaults to the one role Protocol v2 names
function buildEvidencePackage(opts) {
  const sha = opts.sha;
  const taskId = opts.taskId;
  const cycle = opts.cycle || 1;
  const prefix = findingPrefix(taskId) + '-R' + cycle;
  const ciStatus = opts.ciStatus || 'PASS';
  const reviewVerdict = opts.reviewVerdict || 'REVIEW_PASS';

  const codeFindings = (opts.codeFindings || []).map((f, i) =>
    Object.assign({ id: prefix + '-CODE-' + String(i + 1).padStart(3, '0') }, f)
  );
  const a11yFindings = (opts.a11yFindings || []).map((f, i) =>
    Object.assign({ id: prefix + '-A11Y-' + String(i + 1).padStart(3, '0') }, f)
  );
  const securityFindings = (opts.securityFindings || []).map((f, i) =>
    Object.assign({ id: prefix + '-SEC-' + String(i + 1).padStart(3, '0') }, f)
  );

  return {
    task_id: taskId,
    head_sha: sha,
    ci: {
      sha: sha,
      status: ciStatus,
      source: 'fixture-ci',
      provenance: provenance(sha, 'Fixture CI', 'ci-run', ciStatus === 'PASS' ? 'PASS' : 'FAIL'),
    },
    review: {
      sha: sha,
      verdict: reviewVerdict,
      findings: codeFindings,
      provenance: provenance(
        sha,
        opts.reviewProducer || 'Codex Reviewer',
        'code-review',
        reviewVerdict === 'REVIEW_PASS' ? 'PASS' : 'FAIL'
      ),
    },
    accessibility: {
      applicable: true,
      sha: sha,
      checks: a11yChecks(sha),
      findings: a11yFindings,
      provenance: provenance(sha, 'Accessibility Reviewer', 'accessibility-review', 'PASS'),
    },
    security: {
      applicable: true,
      sha: sha,
      checks: securityChecks(sha, opts.relevantSecurityChecks),
      findings: securityFindings,
      provenance: provenance(sha, 'Security Reviewer', 'security-review', 'PASS'),
    },
  };
}

module.exports = {
  buildEvidencePackage,
  findingPrefix,
  A11Y_CHECK_IDS,
  SECURITY_CHECK_IDS,
};
