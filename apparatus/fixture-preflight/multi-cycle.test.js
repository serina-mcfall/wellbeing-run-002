// C-04a: the realistic multi-cycle FIXTURE preflight.
//
// Protocol v2 "Preflight" requires, as a pre-T+00 gate:
//   "realistic Builder→PR→Review FAIL→Fix→CI→Accessibility/Security→
//    fresh re-review→merge; at least two review cycles; exact-SHA
//    evidence invalidation/regeneration; P2 demonstrably non-blocking"
// and CONTRADICTION-AUDIT.md C-04a requires that to be "exercised
// against an isolated FIXTURE PR/worktree with real git mechanics and
// real (not simulated) schema/validator/adapter calls."
//
// Real here: the git repository, its branches, its commits, its head
// SHAs, its registered worktree; apparatus/adapters/git-head.js;
// apparatus/adapters/task-record.js against the committed
// config/tasks.json; apparatus/pr-evidence/validate.js; Ajv over
// protocol/PR-EVIDENCE-V2.schema.json; apparatus/severity/
// severity-floor.js.
//
// Stubbed here: GitHub (an injected in-memory fake — no network, no
// gh CLI, no real pull request) and the merge-eligibility COMPOSITION
// in ./merge-eligibility.js, pending the parallel work stream that owns
// the production composition layer.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const { createFixtureRepo, SHA_RE } = require('./fixture-repo.js');
const { createFakeGitHub } = require('./fake-github.js');
const { buildEvidencePackage } = require('./evidence.js');
const { decideMergeEligibility } = require('./merge-eligibility.js');
const { resolveTrustedHeadSha } = require('../adapters/git-head.js');
const { checkOfflinePolicy } = require('../pr-evidence/validate.js');
const {
  runMultiCycleScenario,
  unblockDependents,
  TASK_BRANCH,
  RUN_002_REPO_ROOT,
  NONBLOCKING_CODE_FINDING,
  NONBLOCKING_SECURITY_FINDING,
  NONBLOCKING_A11Y_FINDING,
} = require('./scenario.js');

function withFixture(fn) {
  const repo = createFixtureRepo();
  try {
    return fn(repo, createFakeGitHub());
  } finally {
    repo.destroy();
  }
}

function runScenario(fn) {
  return withFixture((repo, gh) => fn(runMultiCycleScenario({ repo, gh }), repo, gh));
}

// --- the git mechanics are genuinely real ---------------------------

test('fixture repo produces real, distinct, git-resolvable head SHAs', () => {
  withFixture((repo) => {
    repo.createBranch(TASK_BRANCH);
    const first = repo.commit('one', { 'a.txt': '1\n' });
    const second = repo.commit('two', { 'a.txt': '2\n' });
    assert.match(first, SHA_RE);
    assert.match(second, SHA_RE);
    assert.notEqual(first, second);
    // Resolved independently by the real adapter, not by the fixture.
    const resolved = resolveTrustedHeadSha({ kind: 'branch', ref: TASK_BRANCH }, { repoRoot: repo.root });
    assert.equal(resolved.ok, true);
    assert.equal(resolved.sha, second);
    // And the commit really exists as a git object.
    assert.equal(repo.git(['cat-file', '-t', second]), 'commit');
  });
});

test('the real git-head adapter resolves a real registered worktree in the fixture repo', () => {
  withFixture((repo) => {
    repo.createBranch(TASK_BRANCH);
    const sha = repo.commit('worktree subject', { 'b.txt': 'x\n' });
    repo.checkout('main');
    const wtPath = repo.addWorktree('task-001', TASK_BRANCH);
    assert.equal(fs.existsSync(path.join(wtPath, 'b.txt')), true);
    const resolved = resolveTrustedHeadSha({ kind: 'worktree', path: wtPath }, { repoRoot: repo.root });
    assert.equal(resolved.ok, true, JSON.stringify(resolved));
    assert.equal(resolved.sha, sha);
  });
});

// --- the scenario itself --------------------------------------------

test('review cycle 1 FAILS and the PR is not merge-eligible', () => {
  runScenario((r) => {
    assert.equal(r.cycle1Decision.eligible, false);
    assert.ok(r.cycle1Decision.reasons.includes('OFFLINE_POLICY_FAILED'));
    assert.ok(r.cycle1Decision.reasons.includes('NO_APPROVING_REVIEW_AT_HEAD'));
    assert.equal(r.mergeResult !== null, true, 'the scenario should still reach a merge after the fix');
  });
});

test('AT LEAST TWO review cycles happen, at two different real SHAs', () => {
  runScenario((r) => {
    const reviews = r.events.filter((e) => e.type === 'review_submitted');
    assert.ok(reviews.length >= 2, 'expected >= 2 review cycles, got ' + reviews.length);
    const shas = new Set(reviews.map((e) => e.sha));
    assert.ok(shas.size >= 2, 'the two review cycles must be against different SHAs, got ' + [...shas].join(','));
    assert.equal(reviews[0].verdict, 'REVIEW_FAIL');
    assert.equal(reviews[reviews.length - 1].verdict, 'REVIEW_PASS');
    // The re-review must be FRESH: submitted after the fix commit moved
    // the head, and bound to the post-fix SHA.
    const push = r.events.find((e) => e.type === 'head_pushed');
    assert.ok(push, 'the fixer commit must have moved the PR head');
    assert.ok(reviews[reviews.length - 1].seq > push.seq, 're-review must come after the fix, not before');
    assert.equal(reviews[reviews.length - 1].sha, r.sha2);
  });
});

test('CI and the accessibility/security evidence are regenerated at the post-fix SHA', () => {
  runScenario((r) => {
    const ciRuns = r.events.filter((e) => e.type === 'check_reported');
    assert.ok(ciRuns.some((e) => e.sha === r.sha1), 'CI should have run on the pre-fix SHA');
    assert.ok(ciRuns.some((e) => e.sha === r.sha2), 'CI must be re-run on the post-fix SHA');
    for (const section of ['ci', 'review', 'accessibility', 'security']) {
      assert.equal(r.cycle2Evidence[section].sha, r.sha2, section + ' evidence must be bound to the post-fix SHA');
    }
    for (const check of r.cycle2Evidence.accessibility.checks) {
      assert.equal(check.sha, r.sha2);
    }
    assert.equal(r.cycle2Evidence.accessibility.checks.length, 10, 'all ten Protocol v2 a11y checks must be present');
  });
});

test('EXACT-SHA INVALIDATION: an otherwise-clean evidence set bound to the old SHA is refused', () => {
  // This is the isolating case. The cycle-1 package is also refused, but
  // it carries a P1 and a REVIEW_FAIL, so it cannot prove staleness was
  // what stopped it. Here the ONLY thing wrong is the SHA.
  runScenario((r, repo, gh) => {
    const cleanButStale = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: r.sha1,
      cycle: 2,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [],
      securityFindings: [],
      a11yFindings: [],
    });
    // Proof it is clean on its own terms: the real validator passes it.
    assert.equal(checkOfflinePolicy(cleanButStale).policyValid, true);

    const decision = decideMergeEligibility({
      repoRoot: repo.root,
      identity: { kind: 'branch', ref: TASK_BRANCH },
      evidence: cleanButStale,
      pullRequest: gh.getPullRequest(r.prNumber),
    });
    assert.equal(decision.eligible, false, 'a stale-SHA evidence set must never be accepted');
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_STALE'), JSON.stringify(decision.reasons));
    assert.ok(
      !decision.reasons.includes('OFFLINE_POLICY_FAILED'),
      'the refusal must be attributable to staleness alone, not to a policy defect'
    );
    assert.equal(decision.trustedHeadSha, r.sha2);
  });
});

test('EXACT-SHA REGENERATION: the same evidence rebuilt at the current head is accepted', () => {
  runScenario((r) => {
    assert.equal(r.staleDecision.eligible, false);
    assert.ok(r.staleDecision.reasons.includes('EVIDENCE_SHA_STALE'));
    assert.equal(r.cycle2Decision.eligible, true, JSON.stringify(r.cycle2Decision.reasons));
    assert.equal(r.cycle2Decision.trustedHeadSha, r.sha2);
    assert.deepEqual(r.cycle2Decision.reasons, []);
  });
});

test('P2 IS DEMONSTRABLY NON-BLOCKING: three open P2 findings and the merge still succeeds', () => {
  runScenario((r) => {
    const code = r.cycle2Evidence.review.findings;
    const sec = r.cycle2Evidence.security.findings;
    const a11y = r.cycle2Evidence.accessibility.findings;
    assert.equal(code.length, 1);
    assert.equal(code[0].severity, 'P2');
    assert.equal(sec.length, 1);
    assert.equal(sec[0].severity, 'P2');
    assert.equal(a11y.length, 1);
    assert.equal(a11y[0].jev_severity, 'P2');

    // The real validator — not this harness — is what declines to block.
    const policy = checkOfflinePolicy(r.cycle2Evidence);
    assert.equal(policy.policyValid, true, JSON.stringify(policy.errors));

    assert.equal(r.cycle2Decision.eligible, true);
    assert.ok(r.mergeResult && r.mergeResult.merged === true);
    assert.equal(r.mergeResult.mergedSha, r.sha2);
  });
});

test('CONTROL: the same scenario with a P1 instead of a P2 does NOT merge', () => {
  // Without this, "P2 did not block" would be indistinguishable from
  // "nothing ever blocks".
  withFixture((repo, gh) => {
    repo.createBranch(TASK_BRANCH);
    const sha = repo.commit('p1 control', { 'c.txt': '1\n' });
    const prNumber = gh.createPullRequest({
      title: 'control',
      headRef: TASK_BRANCH,
      headSha: sha,
      baseRef: 'main',
      draft: false,
    });
    gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha, verdict: 'REVIEW_PASS' });
    const evidence = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: sha,
      cycle: 1,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [{ severity: 'P1', status: 'OPEN' }],
      securityFindings: [NONBLOCKING_SECURITY_FINDING],
      a11yFindings: [NONBLOCKING_A11Y_FINDING],
    });
    const decision = decideMergeEligibility({
      repoRoot: repo.root,
      identity: { kind: 'branch', ref: TASK_BRANCH },
      evidence: evidence,
      pullRequest: gh.getPullRequest(prNumber),
    });
    assert.equal(decision.eligible, false, 'a P1 must block');
    assert.ok(decision.reasons.includes('OFFLINE_POLICY_FAILED'));
    assert.ok(
      checkOfflinePolicy(evidence).errors.some((e) => e.startsWith('UNRESOLVED_BLOCKING_FINDING')),
      'the real validator must be the thing that blocks on P1'
    );
  });
});

test('CONTROL: the P2 code finding is the only difference between merge and no-merge', () => {
  // Same package, severity flipped, nothing else touched.
  withFixture((repo, gh) => {
    repo.createBranch(TASK_BRANCH);
    const sha = repo.commit('severity isolation', { 'd.txt': '1\n' });
    const base = {
      taskId: 'TASK-001',
      sha: sha,
      cycle: 1,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      securityFindings: [],
      a11yFindings: [],
    };
    const withP2 = buildEvidencePackage(Object.assign({}, base, { codeFindings: [NONBLOCKING_CODE_FINDING] }));
    const withP1 = buildEvidencePackage(Object.assign({}, base, { codeFindings: [{ severity: 'P1', status: 'OPEN' }] }));
    assert.equal(checkOfflinePolicy(withP2).policyValid, true);
    assert.equal(checkOfflinePolicy(withP1).policyValid, false);
  });
});

// --- dependent-task unblocking uses the real task graph -------------

test('dependent tasks are resolved from the REAL committed task graph', () => {
  const result = unblockDependents(['TASK-001'], ['TASK-002', 'TASK-003'], RUN_002_REPO_ROOT);
  assert.deepEqual(result.unblocked, ['TASK-002', 'TASK-003']);
  assert.deepEqual(result.unresolved, []);
});

test('a task with an unmet dependency is NOT unblocked, and an unknown task fails closed', () => {
  // TASK-002 and TASK-003 both depend only on TASK-001, so merging
  // TASK-003 alone leaves nothing of theirs unblocked.
  const noneMerged = unblockDependents(['TASK-003'], ['TASK-002'], RUN_002_REPO_ROOT);
  assert.deepEqual(noneMerged.unblocked, []);

  const bogus = unblockDependents(['TASK-001'], ['TASK-999'], RUN_002_REPO_ROOT);
  assert.deepEqual(bogus.unblocked, []);
  assert.equal(bogus.unresolved.length, 1);
  assert.equal(bogus.unresolved[0].reason, 'NO_SUCH_TASK');
});
