// THE SEAM. Local merge-eligibility composition for the C-04a fixture
// preflight — NOT the production composition layer.
//
// ============================ READ THIS ============================
// A parallel work stream owns the real merge-eligibility composition
// layer. This module exists so the C-04a fixture preflight can be driven
// end to end today without blocking on it, and so there is a written,
// executable statement of the interface this harness expects. When the
// real layer lands, this file should be deleted and the harness
// re-pointed at it; the tests in this directory are written against the
// INTERFACE below, not against this implementation, so that swap should
// be a one-line change in each test's setup.
//
// REAL vs STUBBED inside this function:
//   REAL   — resolveTrustedHeadSha (apparatus/adapters/git-head.js),
//            invoked against a real git repository.
//   REAL   — checkOfflinePolicy (apparatus/pr-evidence/validate.js),
//            which in turn really runs Ajv over
//            protocol/PR-EVIDENCE-V2.schema.json and really applies
//            apparatus/severity/severity-floor.js. P0/P1 blocking and
//            P2/P3 non-blocking are therefore decided by production
//            code, not by anything in this file.
//   STUBBED — the pull-request state rules below (draft, open, head
//            agreement, approval freshness) and the composition order.
//
// EXPECTED INTERFACE
//   decideMergeEligibility({
//     repoRoot,     string   — repository the git adapter resolves in
//     identity,     object   — git-head identity: { kind: 'branch', ref }
//                              or { kind: 'worktree', path }
//     evidence,     object   — a PR-EVIDENCE-V2 package
//     pullRequest,  object   — { number, state, draft, headSha,
//                                reviews: [{ producer, sha, verdict }] }
//   }) -> {
//     eligible:       boolean,
//     reasons:        string[]  — stable codes, empty iff eligible
//     trustedHeadSha: string|null
//   }
//
// Fails closed: anything it cannot establish is a reason, never a pass.

const { resolveTrustedHeadSha } = require('../adapters/git-head.js');
const { checkOfflinePolicy } = require('../pr-evidence/validate.js');

const APPROVING_VERDICT = 'REVIEW_PASS';

function decideMergeEligibility(request) {
  const reasons = [];
  request = request || {};
  const pr = request.pullRequest;
  const evidence = request.evidence;

  // --- REAL: trusted head SHA from real git -------------------------
  const head = resolveTrustedHeadSha(request.identity, { repoRoot: request.repoRoot });
  if (!head.ok) {
    return { eligible: false, reasons: ['GIT_HEAD_UNRESOLVED:' + head.reason], trustedHeadSha: null };
  }
  const trusted = head.sha;

  // --- REAL: offline policy, schema, severity floor -----------------
  const policy = checkOfflinePolicy(evidence);
  if (!policy.policyValid) {
    reasons.push('OFFLINE_POLICY_FAILED');
  }

  // --- Exact-SHA binding across EVERY evidence section --------------
  // The validator only cross-checks ci.sha and review.sha against
  // head_sha (apparatus/pr-evidence/validate.js lines 74-79); it never
  // compares accessibility.sha or security.sha, and it has no access to
  // the trusted head at all. Both gaps are closed here. See section 35
  // of experiment/C05-3a-SESSION-HANDOVER.md for the change that would
  // be needed in the validator itself.
  if (!evidence || typeof evidence !== 'object') {
    reasons.push('NO_EVIDENCE');
  } else {
    if (evidence.head_sha !== trusted) {
      reasons.push('EVIDENCE_SHA_STALE');
    }
    for (const section of ['ci', 'review', 'accessibility', 'security']) {
      const block = evidence[section];
      if (block && typeof block === 'object' && 'sha' in block && block.sha !== trusted) {
        reasons.push('EVIDENCE_SECTION_SHA_STALE:' + section);
      }
    }
  }

  // --- STUBBED: pull-request state rules ----------------------------
  if (!pr || typeof pr !== 'object') {
    reasons.push('NO_PULL_REQUEST');
    return { eligible: false, reasons: reasons, trustedHeadSha: trusted };
  }
  if (pr.state !== 'OPEN') {
    reasons.push('PR_NOT_OPEN');
  }
  if (pr.draft === true) {
    reasons.push('PR_IS_DRAFT');
  }
  if (pr.headSha !== trusted) {
    reasons.push('PR_HEAD_DIVERGED');
  }

  const reviews = Array.isArray(pr.reviews) ? pr.reviews : [];
  const approvalAtHead = reviews.some((r) => r && r.verdict === APPROVING_VERDICT && r.sha === trusted);
  if (!approvalAtHead) {
    reasons.push('NO_APPROVING_REVIEW_AT_HEAD');
  }

  return { eligible: reasons.length === 0, reasons: reasons, trustedHeadSha: trusted };
}

module.exports = { decideMergeEligibility, APPROVING_VERDICT };
