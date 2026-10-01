// C-04a, THE SYSTEM — the same two scenarios, decided by PRODUCTION code.
//
// multi-cycle.test.js and lifecycle.test.js drive these scenarios through
// the local STUB (merge-eligibility.js) and therefore prove the SCENARIO.
// Handover section 35.8 item 1 named that "the single largest gap":
//
//   "The decision this harness proves correct is made by a stub, not by
//    shipping code. Until the parallel layer lands and the tests are
//    re-pointed at it, what is proven is the scenario, not the system."
//
// The layer landed (section 34). This file is the re-pointing. Identical
// scenario definitions, from the same scenario.js, with the production
// decider injected — so a difference in outcome is a difference between
// the stub and the real gate, never between two copies of a scenario.
//
// REAL, not simulated: the git repository and binary; all four C-04
// adapters; apparatus/pr-evidence/live-gate.js; Ajv over
// protocol/PR-EVIDENCE-V2.schema.json; the severity floor; the
// accessibility requirement registry; this repository's committed
// config/tasks.json.
//
// INJECTED: GitHub (in-memory fake), the ledger, the durable PR record,
// and GitHub's mergeStateStatus. No network, no gh CLI, no real pull
// request, no merge on a real forge, no paid call, no worker.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { createFixtureRepo } = require('./fixture-repo.js');
const { createFakeGitHub } = require('./fake-github.js');
const { makeProductionDecider } = require('./production-gate.js');
const { buildEvidencePackage } = require('./evidence.js');
const {
  runMultiCycleScenario,
  runAutonomousLifecycle,
  TASK_BRANCH,
} = require('./scenario.js');

// Every scenario runs on its own throwaway repository, destroyed after.
function withProductionGate(fn, options) {
  const repo = createFixtureRepo();
  try {
    const gh = createFakeGitHub();
    const decide = makeProductionDecider(
      Object.assign({ repo: repo, gh: gh, taskId: 'TASK-001' }, options || {})
    );
    return fn({ repo: repo, gh: gh, decide: decide });
  } finally {
    repo.destroy();
  }
}

function codes(decision) {
  return decision.reasons.slice().sort();
}

// --------------------------------------------------------------------
// Scenario 1 — Protocol v2 multi-cycle preflight, through production.
// --------------------------------------------------------------------

test('PRODUCTION: the full multi-cycle preflight reaches a merge', () => {
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);

    // Cycle 1 carries a P1 and a REVIEW_FAIL. The real offline policy —
    // the real schema and the real severity floor — refuses it.
    assert.equal(r.cycle1Decision.eligible, false);
    assert.ok(r.cycle1Decision.reasons.includes('OFFLINE_POLICY_FAILED'));

    // Cycle 2, at the new head, with every evidence class regenerated.
    assert.equal(r.cycle2Decision.eligible, true, 'production gate refused: ' + codes(r.cycle2Decision));
    assert.deepEqual(r.cycle2Decision.reasons, []);
    assert.equal(r.cycle2Decision.live.decision, 'ELIGIBLE');
    assert.equal(r.cycle2Decision.trustedHeadSha, r.sha2);

    // The merge really happened, at the SHA the gate trusted.
    assert.ok(r.mergeResult, 'no merge occurred');
    assert.equal(r.mergeResult.mergedSha, r.sha2);
  });
});

test('PRODUCTION: at least two review cycles actually ran, at two different heads', () => {
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const reviews = r.events.filter((e) => e.type === 'review_submitted');
    assert.ok(reviews.length >= 2, 'got ' + reviews.length + ' review cycles');
    assert.notEqual(r.sha1, r.sha2);
    assert.equal(reviews[0].sha, r.sha1);
    assert.equal(reviews[reviews.length - 1].sha, r.sha2);
  });
});

test('PRODUCTION: exact-SHA invalidation — cycle-1 evidence is refused at the new head', () => {
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    assert.equal(r.staleDecision.eligible, false);
    assert.ok(r.staleDecision.reasons.includes('EVIDENCE_SHA_STALE'));
    // Not only the package header: the review provenance in the
    // append-only ledger is bound to the old commit too, and the real
    // composition says so separately.
    assert.ok(r.staleDecision.reasons.includes('REVIEW_PROVENANCE_SHA_MISMATCH'));
    // And CI, because the check run for the new head had not been
    // reported when the stale package was re-decided.
    assert.ok(r.staleDecision.reasons.includes('CI_UNVERIFIED'));
  });
});

test('PRODUCTION: staleness alone refuses an otherwise-clean package', () => {
  // The isolating control. The cycle-1 package also carries a P1 and a
  // REVIEW_FAIL, so its refusal above cannot prove STALENESS caused it.
  // This builds a package that is clean on its own terms but belongs to
  // the superseded commit.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const cleanButStale = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: r.sha1,
      cycle: 1,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [],
      securityFindings: [],
      a11yFindings: [],
    });
    const decision = ctx.decide(cleanButStale, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_STALE'));
    assert.ok(
      !decision.reasons.includes('OFFLINE_POLICY_FAILED'),
      'the package was refused by the offline policy, so staleness is not shown to be the cause'
    );
  });
});

test('PRODUCTION: P2 findings in all three streams do not block the merge', () => {
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    // The merging package carries an open P2 in code, security and
    // accessibility simultaneously.
    assert.equal(r.cycle2Evidence.review.findings[0].severity, 'P2');
    assert.equal(r.cycle2Evidence.security.findings[0].severity, 'P2');
    assert.equal(r.cycle2Evidence.accessibility.findings[0].jev_severity, 'P2');
    assert.equal(r.cycle2Decision.eligible, true);
    assert.ok(r.mergeResult);
  });
});

test('PRODUCTION: a P1 in the same position does block — the control for the test above', () => {
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const withP1 = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: r.sha2,
      cycle: 2,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [{ severity: 'P1', status: 'OPEN' }],
      securityFindings: [],
      a11yFindings: [],
    });
    const decision = ctx.decide(withP1, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('OFFLINE_POLICY_FAILED'));
  });
});

// --------------------------------------------------------------------
// Scenario 2 — the autonomous lifecycle, through production.
// --------------------------------------------------------------------

test('PRODUCTION: draft -> ready -> merged -> dependents unblocked, with no human step', () => {
  withProductionGate((ctx) => {
    const r = runAutonomousLifecycle(Object.assign(ctx, { draft: true }));

    // Being a draft was the ONLY obstacle, and the production gate says
    // so structurally rather than leaving it to be inferred.
    const before = r.steps.find((s) => s.step === 'eligibility_decision' && s.phase === 'before_ready');
    assert.deepEqual(before.decision.reasons, ['PR_IS_DRAFT']);
    assert.equal(before.decision.live.blockedOnlyByDraft, true);
    assert.equal(before.decision.live.draftState, 'DRAFT');

    assert.equal(r.mergeConfirmed, true);
    assert.deepEqual(r.unblocked, ['TASK-002', 'TASK-003', 'TASK-004']);

    const stages = r.steps.map((s) => s.step);
    assert.ok(stages.includes('marked_ready'), 'missing lifecycle stage: marked_ready');
    assert.ok(
      r.events.every((e) => e.actor !== 'human'),
      'a human actor appears in a run that is required to be autonomous'
    );
  });
});

test('PRODUCTION: a changed head invalidates the approval even though the forge kept it', () => {
  withProductionGate((ctx) => {
    const r = runAutonomousLifecycle(Object.assign(ctx, { changeHeadAfterApproval: true }));
    assert.equal(r.mergeConfirmed, false);
    assert.equal(r.decision.eligible, false);
    assert.ok(r.decision.reasons.includes('REVIEW_PROVENANCE_SHA_MISMATCH'));
    assert.ok(!r.steps.some((s) => s.step === 'merged'));
    assert.deepEqual(r.unblocked, []);
  });
});

test('PRODUCTION: a re-reviewed new head is NOT refused — the control for the test above', () => {
  // Without this, "changed head refuses" could be satisfied by a gate
  // that refuses every second decision for any reason at all.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    assert.equal(r.cycle2Decision.eligible, true);
  });
});

// --------------------------------------------------------------------
// The branch-protection condition C-20(b) records, exercised not hidden.
// --------------------------------------------------------------------

test('PRODUCTION: GitHub reporting BLOCKED denies with the branch-protection reason', () => {
  // Run 002's main requires an approving review that nothing in the
  // control plane currently produces (audit row C-20(b)), so a real PR
  // would report mergeStateStatus BLOCKED. The gate must name that as a
  // repository-governance condition rather than as an evidence defect,
  // or an operator reads it as a bug in the evidence chain.
  withProductionGate(
    (ctx) => {
      const r = runMultiCycleScenario(ctx);
      assert.equal(r.cycle2Decision.eligible, false);
      assert.ok(r.cycle2Decision.reasons.includes('PR_BLOCKED_BY_BRANCH_PROTECTION'));
      assert.equal(r.mergeResult, null, 'a BLOCKED pull request was merged');
    },
    { mergeStateStatus: 'BLOCKED' }
  );
});

test('PRODUCTION: an unreported mergeStateStatus is a denial, not an assumption', () => {
  withProductionGate(
    (ctx) => {
      const r = runMultiCycleScenario(ctx);
      assert.equal(r.cycle2Decision.eligible, false);
      assert.ok(r.cycle2Decision.reasons.includes('PR_MERGE_STATE_NOT_CLEAN'));
    },
    { mergeStateStatus: ' ' }
  );
});

// --------------------------------------------------------------------
// The live half does work the offline half cannot.
// --------------------------------------------------------------------

test('PRODUCTION: CI is decided by the adapter, not by the package field', () => {
  // The package says ci.status PASS. The forge reports the check run as
  // a failure. The submitted claim must lose.
  withProductionGate((ctx) => {
    const { repo, gh } = ctx;
    repo.createBranch(TASK_BRANCH);
    const sha = repo.commit('TASK-001: ci disagreement', { 'src/a.txt': 'x\n' });
    const prNumber = gh.createPullRequest({
      title: 'TASK-001',
      headRef: TASK_BRANCH,
      headSha: sha,
      baseRef: 'main',
      draft: false,
    });
    gh.reportCheckRun(prNumber, { name: 'fixture-ci', sha: sha, status: 'FAIL' });
    gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha, verdict: 'REVIEW_PASS' });

    const pkg = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: sha,
      cycle: 1,
      ciStatus: 'PASS', // the claim
      reviewVerdict: 'REVIEW_PASS',
      codeFindings: [],
      securityFindings: [],
      a11yFindings: [],
    });
    const decision = ctx.decide(pkg, prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('CI_UNVERIFIED'));
  });
});

test('PRODUCTION: a verdict the ledger does not carry is refused', () => {
  // The package claims REVIEW_PASS; the reviewer actually returned
  // REVIEW_FAIL. The reviewer-identity adapter compares the two.
  withProductionGate((ctx) => {
    const { repo, gh } = ctx;
    repo.createBranch(TASK_BRANCH);
    const sha = repo.commit('TASK-001: verdict disagreement', { 'src/a.txt': 'x\n' });
    const prNumber = gh.createPullRequest({
      title: 'TASK-001',
      headRef: TASK_BRANCH,
      headSha: sha,
      baseRef: 'main',
      draft: false,
    });
    gh.reportCheckRun(prNumber, { name: 'fixture-ci', sha: sha, status: 'PASS' });
    gh.submitReview(prNumber, { producer: 'Codex Reviewer', sha: sha, verdict: 'REVIEW_FAIL' });

    const pkg = buildEvidencePackage({
      taskId: 'TASK-001',
      sha: sha,
      cycle: 1,
      ciStatus: 'PASS',
      reviewVerdict: 'REVIEW_PASS', // the claim the ledger contradicts
      codeFindings: [],
      securityFindings: [],
      a11yFindings: [],
    });
    const decision = ctx.decide(pkg, prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('REVIEWER_UNVERIFIED'));
  });
});

test('PRODUCTION: an irrelevant security surface needs no sha, a relevant one does', () => {
  // The schema requires result/artifact_reference/sha on a security
  // check ONLY under `if relevant === true`. Requiring one on every
  // surface denied every realistic package — a real task marks most of
  // the twelve surfaces irrelevant — so the gate could never open.
  // Fail-closed, and therefore safe, but unusable.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const irrelevant = r.cycle2Evidence.security.checks.filter((c) => c.relevant === false);
    const relevant = r.cycle2Evidence.security.checks.filter((c) => c.relevant === true);
    assert.ok(irrelevant.length > 0, 'the fixture package marks no surface irrelevant');
    assert.ok(relevant.length > 0, 'the fixture package marks no surface relevant');
    assert.ok(
      irrelevant.every((c) => c.sha === undefined),
      'an irrelevant surface carries a sha, so this test proves nothing'
    );
    assert.ok(relevant.every((c) => typeof c.sha === 'string'));
    assert.equal(r.cycle2Decision.eligible, true);
  });
});

test('PRODUCTION: an accessibility check with NO sha is refused', () => {
  // The other half of the relevance rule, and the half a mutation caught
  // as untested. accessibilityCheck requires sha on all ten
  // unconditionally — there is no `relevant` escape — so an ABSENT one
  // must deny. Without this case, making every sha optional passes the
  // whole file.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const tampered = JSON.parse(JSON.stringify(r.cycle2Evidence));
    delete tampered.accessibility.checks[0].sha;
    const decision = ctx.decide(tampered, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_UNBOUND'));
    assert.equal(
      decision.live.adapters.evidenceShaBinding.determination,
      'COULD_NOT_VERIFY',
      'an absent sha is missing evidence, not disproved evidence'
    );
  });
});

test('PRODUCTION: a RELEVANT security surface with no sha is refused', () => {
  // Relevance is what makes the sha required, so the relevant case must
  // still deny when it is absent — otherwise the relevance rule would
  // have exempted everything rather than only the irrelevant surfaces.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const tampered = JSON.parse(JSON.stringify(r.cycle2Evidence));
    const victim = tampered.security.checks.find((c) => c.relevant === true);
    assert.ok(victim, 'no relevant security surface to test');
    delete victim.sha;
    const decision = ctx.decide(tampered, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_UNBOUND'));
  });
});

test('PRODUCTION: a required provenance sha that is absent is refused', () => {
  // provenance.sha is required on all four blocks. Same mutation, third
  // surface — these three together mean "required" cannot be weakened
  // anywhere without a test going red.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const tampered = JSON.parse(JSON.stringify(r.cycle2Evidence));
    delete tampered.ci.provenance.sha;
    const decision = ctx.decide(tampered, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_UNBOUND'));
  });
});

test('PRODUCTION: an irrelevant surface carrying a FOREIGN sha is still refused', () => {
  // Un-required is not un-checked. A sha that is present must name the
  // trusted head whether or not the schema demanded it.
  withProductionGate((ctx) => {
    const r = runMultiCycleScenario(ctx);
    const tampered = JSON.parse(JSON.stringify(r.cycle2Evidence));
    const victim = tampered.security.checks.find((c) => c.relevant === false);
    victim.sha = r.sha1; // a real commit, but the superseded one
    const decision = ctx.decide(tampered, r.prNumber, { kind: 'branch', ref: TASK_BRANCH });
    assert.equal(decision.eligible, false);
    assert.ok(decision.reasons.includes('EVIDENCE_SHA_UNBOUND'));
  });
});
