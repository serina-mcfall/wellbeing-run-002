// The C-04a scenario drivers.
//
// Two scenarios live here:
//
//   runMultiCycleScenario   — Protocol v2 "Preflight": Builder → PR →
//     Review FAIL → Fix → CI → Accessibility/Security → fresh re-review
//     → merge, with at least two review cycles, exact-SHA evidence
//     invalidation and regeneration, and a P2 that does not block.
//
//   runAutonomousLifecycle  — the operator-required acceptance scenario:
//     create PR → independent review → required evidence/checks → mark
//     ready if drafted → merge when eligible → confirm merge → unblock
//     dependent tasks, with no human step anywhere.
//
// Both drivers return a transcript. The transcript is the thing the
// tests assert against, so a driver that silently skips a step (for
// example, reusing a first-cycle review instead of re-reviewing) is
// visible as a missing or mis-SHA'd event rather than as a pass.

const path = require('path');
const { buildEvidencePackage } = require('./evidence.js');
const { decideMergeEligibility } = require('./merge-eligibility.js');
const { resolveTaskRecord } = require('../adapters/task-record.js');

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');
const TASK_BRANCH = 'run-002/fixture-task-001';

// --- Shared evidence shapes ----------------------------------------
// A P1 code finding: Protocol v2 says P0/P1 always block merge.
const BLOCKING_CODE_FINDING = { severity: 'P1', status: 'OPEN' };

// The P2 set: one in each of the three evidence streams, all left
// deliberately unfixed at merge time. If any of these blocks, the P2
// non-blocking requirement is not met.
const NONBLOCKING_CODE_FINDING = { severity: 'P2', status: 'ACCEPTED_NONBLOCKING' };
const NONBLOCKING_SECURITY_FINDING = { severity: 'P2', status: 'ACCEPTED_NONBLOCKING' };
const NONBLOCKING_A11Y_FINDING = {
  jev_severity: 'P2',
  classification: 'NON_FAILURE',
  non_failure_rationale:
    'Spacing is tighter than the house style prefers but no WCAG success criterion or Protocol v2 accessibility check is unmet.',
  status: 'ACCEPTED_NONBLOCKING',
};

function identityFor(ref) {
  return { kind: 'branch', ref: ref };
}

// The merge-eligibility decider the scenarios consult.
//
// `ctx.decide` lets a caller supply one. That is the swap handover
// section 35.4 anticipated and section 35.8 item 1 called the largest
// gap: with no override the scenarios run against the local STUB
// (merge-eligibility.js) and prove only the SCENARIO, and with
// production-gate.js's decider injected the very same scenarios are
// decided by the real C-04 composition layer and prove the SYSTEM.
//
// One scenario definition, two deciders. Keeping a single definition is
// the point - two copies would drift, and then "the production gate
// passes the scenario" would stop meaning the same scenario.
function deciderFor(ctx, repo, gh, identity) {
  if (typeof ctx.decide === 'function') {
    return (evidence, prNumber) => ctx.decide(evidence, prNumber, identity);
  }
  return (evidence, prNumber) =>
    decideMergeEligibility({
      repoRoot: repo.root,
      identity: identity,
      evidence: evidence,
      pullRequest: gh.getPullRequest(prNumber),
    });
}

// Uses the REAL task-record adapter against this repository's REAL
// config/tasks.json. Nothing about the dependency graph is invented.
function unblockDependents(mergedTaskIds, candidateTaskIds, repoRoot) {
  const merged = new Set(mergedTaskIds);
  const unblocked = [];
  const stillBlocked = [];
  const unresolved = [];
  for (const id of candidateTaskIds) {
    const record = resolveTaskRecord(id, { repoRoot: repoRoot || RUN_002_REPO_ROOT });
    if (!record.ok) {
      unresolved.push({ id: id, reason: record.reason });
      continue;
    }
    const deps = record.task.dependsOn;
    if (!deps.some((d) => merged.has(d))) {
      continue; // not a dependent of what just merged
    }
    if (deps.every((d) => merged.has(d))) {
      unblocked.push(id);
    } else {
      stillBlocked.push(id);
    }
  }
  return { unblocked: unblocked, stillBlocked: stillBlocked, unresolved: unresolved };
}

// --------------------------------------------------------------------
// Scenario 1: realistic multi-cycle preflight.
// --------------------------------------------------------------------
function runMultiCycleScenario(ctx) {
  const repo = ctx.repo;
  const gh = ctx.gh;
  const taskId = ctx.taskId || 'TASK-001';
  const steps = [];
  const identity = identityFor(TASK_BRANCH);
  const decide = deciderFor(ctx, repo, gh, identity);

  // 1. Builder produces a real commit on a real task branch.
  repo.createBranch(TASK_BRANCH);
  const sha1 = repo.commit('TASK-001: builder first pass', {
    'src/feature.txt': 'first pass\n',
  });
  steps.push({ step: 'builder_commit', sha: sha1 });

  // 2. PR opened at that real SHA.
  const prNumber = gh.createPullRequest({
    title: 'TASK-001: fixture feature',
    headRef: TASK_BRANCH,
    headSha: sha1,
    baseRef: 'main',
    draft: false,
  });
  steps.push({ step: 'pr_opened', pr: prNumber, sha: sha1 });

  // 3. CI at SHA1.
  gh.reportCheckRun(prNumber, { name: 'fixture-ci', sha: sha1, status: 'PASS' });

  // 4. Review cycle 1 FAILS on a P1.
  const cycle1Evidence = buildEvidencePackage({
    taskId: taskId,
    sha: sha1,
    cycle: 1,
    ciStatus: 'PASS',
    reviewVerdict: 'REVIEW_FAIL',
    codeFindings: [BLOCKING_CODE_FINDING],
    securityFindings: [],
    a11yFindings: [],
  });
  gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha1, verdict: 'REVIEW_FAIL' });
  const cycle1Decision = decide(cycle1Evidence, prNumber);
  steps.push({ step: 'review_cycle_1', sha: sha1, verdict: 'REVIEW_FAIL', decision: cycle1Decision });

  // 5. Fixer produces a real second commit. The head really moves.
  const sha2 = repo.commit('TASK-001: fix the P1 raised in review cycle 1', {
    'src/feature.txt': 'first pass\nfixed\n',
  });
  gh.pushHead(prNumber, sha2);
  steps.push({ step: 'fixer_commit', sha: sha2 });

  // 6. Exact-SHA INVALIDATION: the cycle-1 evidence set, unchanged, is
  //    re-decided against the new trusted head and must be refused.
  const staleDecision = decide(cycle1Evidence, prNumber);
  steps.push({ step: 'stale_evidence_recheck', evidenceSha: sha1, decision: staleDecision });

  // 7. REGENERATION: CI, accessibility and security re-run at SHA2, and
  //    a FRESH review cycle 2 at SHA2 passes. The P2 findings are left
  //    open on purpose.
  gh.reportCheckRun(prNumber, { name: 'fixture-ci', sha: sha2, status: 'PASS' });
  const cycle2Evidence = buildEvidencePackage({
    taskId: taskId,
    sha: sha2,
    cycle: 2,
    ciStatus: 'PASS',
    reviewVerdict: 'REVIEW_PASS',
    codeFindings: [NONBLOCKING_CODE_FINDING],
    securityFindings: [NONBLOCKING_SECURITY_FINDING],
    a11yFindings: [NONBLOCKING_A11Y_FINDING],
  });
  gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha2, verdict: 'REVIEW_PASS' });
  const cycle2Decision = decide(cycle2Evidence, prNumber);
  steps.push({ step: 'review_cycle_2', sha: sha2, verdict: 'REVIEW_PASS', decision: cycle2Decision });

  // 8. Merge only if the composition layer says so.
  let mergeResult = null;
  if (cycle2Decision.eligible) {
    mergeResult = gh.merge(prNumber, { expectedHeadSha: cycle2Decision.trustedHeadSha });
    steps.push({ step: 'merged', sha: mergeResult.mergedSha });
  }

  return {
    prNumber: prNumber,
    sha1: sha1,
    sha2: sha2,
    cycle1Evidence: cycle1Evidence,
    cycle2Evidence: cycle2Evidence,
    cycle1Decision: cycle1Decision,
    staleDecision: staleDecision,
    cycle2Decision: cycle2Decision,
    mergeResult: mergeResult,
    steps: steps,
    events: gh.events,
  };
}

// --------------------------------------------------------------------
// Scenario 2: the autonomous PR lifecycle (operator acceptance).
//
// ctx.draft            — open the PR as a draft
// ctx.changeHeadAfterApproval — simulate a push landing after the
//                        approving review, so a stale approval is the
//                        only thing authorizing the merge
// --------------------------------------------------------------------
function runAutonomousLifecycle(ctx) {
  const repo = ctx.repo;
  const gh = ctx.gh;
  const taskId = ctx.taskId || 'TASK-001';
  const branch = ctx.branch || TASK_BRANCH;
  const identity = identityFor(branch);
  const steps = [];
  const decide = deciderFor(ctx, repo, gh, identity);

  // create PR
  repo.createBranch(branch);
  const sha = repo.commit('TASK-001: autonomous lifecycle fixture change', {
    'src/lifecycle.txt': 'change\n',
  });
  const prNumber = gh.createPullRequest({
    title: 'TASK-001: autonomous lifecycle',
    headRef: branch,
    headSha: sha,
    baseRef: 'main',
    draft: ctx.draft === true,
  });
  steps.push({ step: 'pr_created', pr: prNumber, draft: ctx.draft === true, sha: sha });

  // required evidence/checks + independent review
  gh.reportCheckRun(prNumber, { name: 'fixture-ci', sha: sha, status: 'PASS' });
  let evidence = buildEvidencePackage({
    taskId: taskId,
    sha: sha,
    cycle: 1,
    ciStatus: 'PASS',
    reviewVerdict: 'REVIEW_PASS',
    codeFindings: [NONBLOCKING_CODE_FINDING],
    securityFindings: [NONBLOCKING_SECURITY_FINDING],
    a11yFindings: [NONBLOCKING_A11Y_FINDING],
  });
  gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha, verdict: 'REVIEW_PASS' });
  steps.push({ step: 'independent_review', sha: sha, verdict: 'REVIEW_PASS' });

  // a push lands after approval, if the scenario asks for it
  let headAfterPush = sha;
  if (ctx.changeHeadAfterApproval === true) {
    headAfterPush = repo.commit('TASK-001: an unreviewed change lands after approval', {
      'src/lifecycle.txt': 'change\nunreviewed\n',
    });
    gh.pushHead(prNumber, headAfterPush);
    steps.push({ step: 'unreviewed_push', sha: headAfterPush });
  }

  // decide; mark ready ONLY if being a draft is the sole obstacle
  let decision = decide(evidence, prNumber);
  steps.push({ step: 'eligibility_decision', phase: 'before_ready', decision: decision });

  if (!decision.eligible && decision.reasons.length === 1 && decision.reasons[0] === 'PR_IS_DRAFT') {
    gh.markReadyForReview(prNumber);
    steps.push({ step: 'marked_ready' });
    decision = decide(evidence, prNumber);
    steps.push({ step: 'eligibility_decision', phase: 'after_ready', decision: decision });
  }

  // merge when eligible
  let mergeResult = null;
  let mergeError = null;
  if (decision.eligible) {
    try {
      mergeResult = gh.merge(prNumber, { expectedHeadSha: decision.trustedHeadSha });
    } catch (err) {
      mergeError = err;
    }
  }

  // confirm merge by reading the forge back, not by trusting the call
  const confirmed = gh.getPullRequest(prNumber);
  const mergeConfirmed = confirmed.state === 'MERGED' && !!mergeResult && confirmed.headSha === mergeResult.mergedSha;
  steps.push({ step: 'merge_confirmation', confirmed: mergeConfirmed, state: confirmed.state });

  // unblock dependent tasks, from the REAL task graph
  const unblockResult = mergeConfirmed
    ? unblockDependents([taskId], ctx.candidateDependents || ['TASK-002', 'TASK-003', 'TASK-004'], RUN_002_REPO_ROOT)
    : { unblocked: [], stillBlocked: [], unresolved: [] };
  steps.push({ step: 'dependents', unblocked: unblockResult.unblocked });

  return {
    prNumber: prNumber,
    sha: sha,
    headAfterPush: headAfterPush,
    decision: decision,
    mergeResult: mergeResult,
    mergeError: mergeError,
    mergeConfirmed: mergeConfirmed,
    unblocked: unblockResult.unblocked,
    steps: steps,
    events: gh.events,
  };
}

module.exports = {
  runMultiCycleScenario,
  runAutonomousLifecycle,
  unblockDependents,
  TASK_BRANCH,
  RUN_002_REPO_ROOT,
  BLOCKING_CODE_FINDING,
  NONBLOCKING_CODE_FINDING,
  NONBLOCKING_SECURITY_FINDING,
  NONBLOCKING_A11Y_FINDING,
};
