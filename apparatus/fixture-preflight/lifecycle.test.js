// C-04a acceptance scenario — the full AUTONOMOUS pull-request
// lifecycle, added by explicit operator requirement:
//
//   create PR → independent review → required evidence/checks →
//   mark ready if drafted → merge when eligible → confirm merge →
//   unblock dependent tasks
//
// with two distinct cases:
//   (a) a DRAFT pull request becoming ready and then merging, with no
//       human step anywhere in the transcript;
//   (b) a CHANGED HEAD, where a stale approval must NOT authorize a
//       merge.
//
// GitHub is an injected fake (./fake-github.js). Git is real. The
// schema, validator, severity floor, git-head adapter and task-record
// adapter are real. The merge-eligibility COMPOSITION is the local seam
// in ./merge-eligibility.js, pending the parallel work stream.

const test = require('node:test');
const assert = require('node:assert/strict');

const { createFixtureRepo } = require('./fixture-repo.js');
const { createFakeGitHub, FakeGitHubError } = require('./fake-github.js');
const { runAutonomousLifecycle } = require('./scenario.js');

function withFixture(fn) {
  const repo = createFixtureRepo();
  try {
    return fn(repo, createFakeGitHub());
  } finally {
    repo.destroy();
  }
}

function eventTypes(events) {
  return events.map((e) => e.type);
}

function seqOf(events, type) {
  const found = events.find((e) => e.type === type);
  return found ? found.seq : -1;
}

// ====================================================================
// (a) DRAFT → READY → MERGED, autonomously
// ====================================================================

test('(a) a DRAFT pull request is marked ready and merged with no human step', () => {
  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: true });

    // The draft really did start as a draft, and the eligibility layer
    // really did hold it.
    const beforeReady = r.steps.find((s) => s.step === 'eligibility_decision' && s.phase === 'before_ready');
    assert.ok(beforeReady, 'the lifecycle must decide eligibility before marking ready');
    assert.equal(beforeReady.decision.eligible, false);
    assert.deepEqual(beforeReady.decision.reasons, ['PR_IS_DRAFT']);

    // The ready step happened, and it happened BEFORE the merge.
    const types = eventTypes(r.events);
    assert.ok(types.includes('pr_marked_ready'), 'the autonomous lifecycle must perform the ready step itself');
    assert.ok(types.includes('pr_merged'), 'the lifecycle must reach a merge');
    assert.ok(
      seqOf(r.events, 'pr_marked_ready') < seqOf(r.events, 'pr_merged'),
      'the ready step must precede the merge'
    );

    // Merge confirmed by reading the forge back, not by trusting the call.
    assert.equal(r.mergeConfirmed, true);
    assert.equal(r.mergeError, null);
    assert.equal(r.mergeResult.mergedSha, r.sha);
    assert.equal(gh.getPullRequest(r.prNumber).state, 'MERGED');

    // Dependent tasks unblocked, from the real committed task graph.
    assert.deepEqual(r.unblocked, ['TASK-002', 'TASK-003', 'TASK-004']);

    // No human touched any of it.
    const humanEvents = r.events.filter((e) => e.actor === 'human');
    assert.deepEqual(humanEvents, [], 'the lifecycle must be fully autonomous');
  });
});

test('(a) the lifecycle visits every required stage, in order', () => {
  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: true });
    const order = [
      'pr_created',
      'check_reported',
      'review_submitted',
      'pr_marked_ready',
      'pr_merged',
    ];
    let last = -1;
    for (const type of order) {
      const seq = seqOf(r.events, type);
      assert.notEqual(seq, -1, 'missing lifecycle stage: ' + type);
      assert.ok(seq > last, 'lifecycle stage out of order: ' + type);
      last = seq;
    }
    const confirm = r.steps.find((s) => s.step === 'merge_confirmation');
    assert.equal(confirm.confirmed, true);
    const dependents = r.steps.find((s) => s.step === 'dependents');
    assert.ok(dependents.unblocked.length > 0, 'merging must unblock the tasks that depend on it');
  });
});

test('(a) a non-draft pull request merges without any ready step', () => {
  // Confirms the ready step is conditional, not unconditionally emitted.
  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: false });
    assert.equal(r.mergeConfirmed, true);
    assert.ok(!eventTypes(r.events).includes('pr_marked_ready'));
  });
});

test('(a) the forge itself refuses a draft merge, so "ready" is not decorative', () => {
  withFixture((repo, gh) => {
    const sha = repo.createBranch('run-002/fixture-draft') && repo.commit('draft subject', { 'e.txt': '1\n' });
    const prNumber = gh.createPullRequest({
      title: 'draft',
      headRef: 'run-002/fixture-draft',
      headSha: sha,
      baseRef: 'main',
      draft: true,
    });
    assert.throws(
      () => gh.merge(prNumber, { expectedHeadSha: sha }),
      (err) => err instanceof FakeGitHubError && err.code === 'DRAFT_NOT_MERGEABLE'
    );
    assert.equal(gh.getPullRequest(prNumber).state, 'OPEN');
  });
});

// ====================================================================
// (b) CHANGED HEAD — a stale approval must not authorize a merge
// ====================================================================

test('(b) a stale approval does NOT authorize a merge after the head changes', () => {
  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: false, changeHeadAfterApproval: true });

    assert.notEqual(r.headAfterPush, r.sha, 'the push must really have moved the head');

    assert.equal(r.decision.eligible, false, 'a stale approval must never authorize a merge');
    assert.ok(
      r.decision.reasons.includes('NO_APPROVING_REVIEW_AT_HEAD'),
      'expected the stale approval to be named: ' + JSON.stringify(r.decision.reasons)
    );
    assert.equal(r.decision.trustedHeadSha, r.headAfterPush);

    assert.equal(r.mergeResult, null);
    assert.equal(r.mergeConfirmed, false);
    assert.equal(gh.getPullRequest(r.prNumber).state, 'OPEN');
    assert.ok(!eventTypes(r.events).includes('pr_merged'));

    // Nothing downstream moved either.
    assert.deepEqual(r.unblocked, []);
  });
});

test('(b) the approval is only stale because of the SHA — a fresh review at the new head merges', () => {
  // Isolates the cause. Same PR, same evidence shape, re-reviewed at the
  // new head: it then merges. So the refusal above was the SHA binding,
  // not some unrelated defect.
  const { buildEvidencePackage } = require('./evidence.js');
  const { decideMergeEligibility } = require('./merge-eligibility.js');
  const {
    NONBLOCKING_CODE_FINDING,
    NONBLOCKING_SECURITY_FINDING,
    NONBLOCKING_A11Y_FINDING,
    TASK_BRANCH,
  } = require('./scenario.js');

  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: false, changeHeadAfterApproval: true });
    assert.equal(r.decision.eligible, false);

    gh.reportCheckRun(r.prNumber, { name: 'fixture-ci', sha: r.headAfterPush, status: 'PASS' });
    gh.submitReview(r.prNumber, { producer: 'Codex Reviewer', sha: r.headAfterPush, verdict: 'REVIEW_PASS' });
    const freshEvidence = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: r.headAfterPush,
      cycle: 2,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [NONBLOCKING_CODE_FINDING],
      securityFindings: [NONBLOCKING_SECURITY_FINDING],
      a11yFindings: [NONBLOCKING_A11Y_FINDING],
    });
    const fresh = decideMergeEligibility({
      repoRoot: repo.root,
      identity: { kind: 'branch', ref: TASK_BRANCH },
      evidence: freshEvidence,
      pullRequest: gh.getPullRequest(r.prNumber),
    });
    assert.equal(fresh.eligible, true, JSON.stringify(fresh.reasons));
    const merged = gh.merge(r.prNumber, { expectedHeadSha: fresh.trustedHeadSha });
    assert.equal(merged.mergedSha, r.headAfterPush);
  });
});

test('(b) regenerating the EVIDENCE at the new head does not substitute for re-reviewing', () => {
  // The dangerous shape, and the one that makes the approval-freshness
  // rule independently load-bearing: CI, accessibility and security are
  // all re-run at the new head and the package is clean, but nobody
  // re-reviewed. Staleness of the evidence is therefore NOT what stops
  // this; only the approval binding is.
  const { buildEvidencePackage } = require('./evidence.js');
  const { decideMergeEligibility } = require('./merge-eligibility.js');
  const {
    NONBLOCKING_CODE_FINDING,
    NONBLOCKING_SECURITY_FINDING,
    NONBLOCKING_A11Y_FINDING,
    TASK_BRANCH,
  } = require('./scenario.js');

  withFixture((repo, gh) => {
    const r = runAutonomousLifecycle({ repo, gh, draft: false, changeHeadAfterApproval: true });
    gh.reportCheckRun(r.prNumber, { name: 'fixture-ci', sha: r.headAfterPush, status: 'PASS' });
    const regeneratedEvidence = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: r.headAfterPush,
      cycle: 2,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [NONBLOCKING_CODE_FINDING],
      securityFindings: [NONBLOCKING_SECURITY_FINDING],
      a11yFindings: [NONBLOCKING_A11Y_FINDING],
    });
    const decision = decideMergeEligibility({
      repoRoot: repo.root,
      identity: { kind: 'branch', ref: TASK_BRANCH },
      evidence: regeneratedEvidence,
      pullRequest: gh.getPullRequest(r.prNumber),
    });
    assert.equal(decision.eligible, false, 'fresh evidence must not launder a stale approval');
    assert.deepEqual(
      decision.reasons,
      ['NO_APPROVING_REVIEW_AT_HEAD'],
      'the stale approval must be the ONLY thing left stopping this merge'
    );
    assert.equal(gh.getPullRequest(r.prNumber).state, 'OPEN');
  });
});

test('(b) the forge refuses a merge that names a head the PR no longer has', () => {
  // Belt as well as braces: even if the eligibility layer were wrong,
  // the merge call still carries the exact SHA it believes in.
  withFixture((repo, gh) => {
    repo.createBranch('run-002/fixture-moved');
    const first = repo.commit('first', { 'f.txt': '1\n' });
    const prNumber = gh.createPullRequest({
      title: 'moved',
      headRef: 'run-002/fixture-moved',
      headSha: first,
      baseRef: 'main',
      draft: false,
    });
    const second = repo.commit('second', { 'f.txt': '2\n' });
    gh.pushHead(prNumber, second);
    assert.throws(
      () => gh.merge(prNumber, { expectedHeadSha: first }),
      (err) => err instanceof FakeGitHubError && err.code === 'HEAD_MOVED'
    );
    assert.equal(gh.getPullRequest(prNumber).state, 'OPEN');
  });
});
