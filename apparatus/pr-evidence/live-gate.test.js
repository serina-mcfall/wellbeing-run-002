// C-04 live merge-gate composition tests.
//
// Every adapter is a stub. No git process is spawned, no `gh` call is made, no
// ledger file is read, and nothing merges. These fixtures prove the
// COMPOSITION's logic — that the trusted head anchors every other fact, that
// each unknown denies, and that no single deletion opens the gate. They are
// not live proof of a real commit, a real CI run or a real reviewer; that is
// still the per-PR, post-T+00 requirement recorded against C-04a.

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  evaluateLiveMergeEligibility,
  verifyReviewProvenance,
  verifyEvidenceShaBinding,
  eventHeadSha,
  ELIGIBLE,
  DENIED,
  COULD_NOT_VERIFY,
  VERIFIED_FALSE,
} = require('./live-gate.js');

const HEAD = 'a'.repeat(40);
const OLD_HEAD = 'b'.repeat(40);
const TASK = 'TASK-002';
const PR = 7;
const WORKER = 'task-002-review-1';
const IDENTITY = { kind: 'branch', ref: 'run-002/task-002' };

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

function pkgFor(sha) {
  return {
    task_id: TASK,
    head_sha: sha,
    ci: {
      sha: sha,
      status: 'PASS',
      source: 'github-actions',
      provenance: provenance(sha, { producer: 'GitHub Actions', kind: 'ci-run' }),
    },
    review: {
      sha: sha,
      verdict: 'REVIEW_PASS',
      findings: [],
      provenance: provenance(sha, { producer: 'Codex Reviewer', kind: 'code-review' }),
    },
    accessibility: {
      applicable: false,
      not_applicable_reason: 'No UI surface changed in this PR.',
      provenance: provenance(sha, { producer: 'Accessibility Reviewer', kind: 'accessibility-review' }),
    },
    security: {
      applicable: false,
      not_applicable_reason: 'No security-relevant surface changed in this PR.',
      provenance: provenance(sha, { producer: 'Security Reviewer', kind: 'security-review' }),
    },
  };
}

function prView(overrides) {
  return Object.assign(
    {
      number: PR,
      state: 'OPEN',
      isDraft: false,
      headRefOid: HEAD,
      mergeable: 'MERGEABLE',
      mergeStateStatus: 'CLEAN',
    },
    overrides || {}
  );
}

function ledgerFor(sha, overrides) {
  const dispatched = Object.assign(
    {
      event_type: 'REVIEW_DISPATCHED',
      task_id: TASK,
      pr_id: PR,
      role: 'reviewer',
      agent_id: WORKER,
      provider: 'codex',
      metadata_redacted: { head_sha: sha },
    },
    (overrides || {}).dispatched || {}
  );
  const result = Object.assign(
    {
      event_type: 'REVIEW_RESULT',
      task_id: TASK,
      pr_id: PR,
      role: 'reviewer',
      agent_id: WORKER,
      provider: 'codex',
      outcome: 'REVIEW_PASS',
      metadata_redacted: { head_sha: sha },
    },
    (overrides || {}).result || {}
  );
  return [dispatched, result];
}

// Each stub records the argument it was called with, so a test can assert that
// the composition passed the TRUSTED head down rather than the package's claim.
function adaptersFor(overrides) {
  const calls = { ci: [], reviewer: [], task: [], head: [] };
  const base = {
    calls: calls,
    resolveHeadSha(identity) {
      calls.head.push(identity);
      return { ok: true, sha: HEAD, resolvedFrom: 'branch:' + IDENTITY.ref };
    },
    resolveCi(sha) {
      calls.ci.push(sha);
      return { ok: true, sha: sha, requiredChecks: ['ci'], observedChecks: [] };
    },
    resolveTask(taskId) {
      calls.task.push(taskId);
      return { ok: true, task: { id: taskId, title: 'fixture', kind: 'product' } };
    },
    resolveReviewer(claim) {
      calls.reviewer.push(claim);
      return { ok: true, workerId: WORKER, provider: 'codex', verdict: 'REVIEW_PASS', reviewedHead: claim.headSha };
    },
  };
  return Object.assign(base, overrides || {});
}

function evaluate(requestOverrides, adapterOverrides) {
  const request = Object.assign(
    {
      identity: IDENTITY,
      prNumber: PR,
      pkg: pkgFor(HEAD),
      prView: prView(),
      ledgerEvents: ledgerFor(HEAD),
    },
    requestOverrides || {}
  );
  return evaluateLiveMergeEligibility(request, adaptersFor(adapterOverrides));
}

function codes(result) {
  return result.reasons.map((r) => r.code);
}

// ------------------------------------------------------------- the happy path

test('positive: every adapter verified and SHA-bound provenance yields ELIGIBLE', () => {
  const result = evaluate();
  assert.deepEqual(result.reasons, []);
  assert.equal(result.decision, ELIGIBLE);
  assert.equal(result.trustedHeadSha, HEAD);
  assert.equal(result.draftState, 'READY');
  assert.equal(result.blockedOnlyByDraft, false);
});

test('the trusted head, not the package claim, is what the adapters are asked about', () => {
  const adapters = adaptersFor();
  evaluateLiveMergeEligibility(
    { identity: IDENTITY, prNumber: PR, pkg: pkgFor(HEAD), prView: prView(), ledgerEvents: ledgerFor(HEAD) },
    adapters
  );
  assert.deepEqual(adapters.calls.ci, [HEAD]);
  assert.equal(adapters.calls.reviewer[0].headSha, HEAD);
  assert.deepEqual(adapters.calls.task, [TASK]);
});

// -------------------------------------------- REQUIRED REGRESSION: stale output

test('REGRESSION stale output: a verdict produced against an older head denies', () => {
  // The package, its CI block and its review block are all internally
  // consistent at OLD_HEAD — checkOfflinePolicy alone calls this valid. The
  // branch has since moved to HEAD, so the whole package is stale.
  const result = evaluate({ pkg: pkgFor(OLD_HEAD), ledgerEvents: ledgerFor(OLD_HEAD) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('EVIDENCE_SHA_STALE'));
  assert.equal(
    result.reasons.find((r) => r.code === 'EVIDENCE_SHA_STALE').determination,
    VERIFIED_FALSE
  );
});

test('REGRESSION stale output: an internally consistent old package is accepted by the OFFLINE checker', () => {
  // The control for the test above: proves the denial comes from the live
  // composition and not from the offline policy half, so the composition is
  // doing work nothing else in the repository does.
  const { checkOfflinePolicy } = require('./validate.js');
  assert.equal(checkOfflinePolicy(pkgFor(OLD_HEAD)).policyValid, true);
});

// --------------------------------------------- REQUIRED REGRESSION: changed head

test('REGRESSION changed head: the pull request head moving after approval denies', () => {
  // git still resolves the branch to HEAD; GitHub reports a different head.
  const result = evaluate({ prView: prView({ headRefOid: OLD_HEAD }) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_HEAD_MOVED'));
});

test('REGRESSION changed head: the branch moving under a previously-valid package denies', () => {
  const result = evaluate(
    { pkg: pkgFor(OLD_HEAD), prView: prView({ headRefOid: OLD_HEAD }), ledgerEvents: ledgerFor(OLD_HEAD) },
    { resolveHeadSha: () => ({ ok: true, sha: HEAD }) }
  );
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('EVIDENCE_SHA_STALE'));
  assert.ok(codes(result).includes('PR_HEAD_MOVED'));
});

// -------------------------------------- REQUIRED REGRESSION: missing provenance

test('REGRESSION missing provenance: no review ledger events at all denies', () => {
  const result = evaluate({ ledgerEvents: [] });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_MISSING'));
});

test('REGRESSION missing provenance: an unreadable ledger denies rather than defaulting to clean', () => {
  const result = evaluate({ ledgerEvents: undefined });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'REVIEW_PROVENANCE_UNREADABLE');
  assert.ok(reason);
  assert.equal(reason.determination, COULD_NOT_VERIFY);
});

test('REGRESSION missing provenance: REVIEW_DISPATCHED present but REVIEW_RESULT absent denies', () => {
  const result = evaluate({ ledgerEvents: [ledgerFor(HEAD)[0]] });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_MISSING'));
});

// ----------------------------------- REQUIRED REGRESSION: mismatched provenance

test('REGRESSION mismatched provenance: review events bound to an older commit deny', () => {
  const result = evaluate({ ledgerEvents: ledgerFor(OLD_HEAD) });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'REVIEW_PROVENANCE_SHA_MISMATCH');
  assert.ok(reason);
  assert.equal(reason.determination, VERIFIED_FALSE);
});

test('REGRESSION mismatched provenance: a dispatch for the new head with a result for the old denies', () => {
  const events = ledgerFor(HEAD);
  events[1].metadata_redacted = { head_sha: OLD_HEAD };
  const result = evaluate({ ledgerEvents: events });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_SHA_MISMATCH'));
});

test('REGRESSION mismatched provenance: a SHA-bound result from another worker denies', () => {
  const events = ledgerFor(HEAD);
  events[1].agent_id = 'task-002-builder';
  const result = evaluate({ ledgerEvents: events });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_WORKER_MISMATCH'));
});

// --------------------------- REQUIRED REGRESSION: the model's own claimed SHA

test('REGRESSION self-claimed SHA: a package whose only SHA evidence is its own claim denies', () => {
  // The package says head_sha = HEAD in every block, and the reviewer adapter
  // is satisfied (its SHA source is mutable state that agrees). The ledger —
  // the one append-only source — binds the review to NO commit. A SHA the
  // submitting model typed about itself is not independently verified
  // provenance, which is the entire point of C-04.
  const events = ledgerFor(HEAD);
  delete events[0].metadata_redacted.head_sha;
  delete events[1].metadata_redacted.head_sha;
  const result = evaluate({ ledgerEvents: events });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'REVIEW_PROVENANCE_UNBOUND');
  assert.ok(reason);
  assert.equal(reason.determination, COULD_NOT_VERIFY);
  assert.match(reason.detail, /not a substitute/);
});

test('REGRESSION self-claimed SHA: a head_sha copied into the package cannot stand in for the ledger', () => {
  // Same ledger defect, and the package is made maximally self-consistent.
  // Nothing a submitter can write to the package changes the outcome.
  const events = ledgerFor(HEAD);
  delete events[0].metadata_redacted.head_sha;
  delete events[1].metadata_redacted.head_sha;
  const pkg = pkgFor(HEAD);
  pkg.review.provenance.sha = HEAD;
  const result = evaluate({ pkg: pkg, ledgerEvents: events });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_UNBOUND'));
});

test('two disagreeing head_sha spellings on one event deny rather than one being chosen', () => {
  const events = ledgerFor(HEAD);
  events[1].head_sha = OLD_HEAD;
  const result = evaluate({ ledgerEvents: events });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_CONFLICT'));
});

test('the top-level head_sha spelling is honoured, so promoting the field into ledger FIELDS keeps working', () => {
  const events = ledgerFor(HEAD);
  for (const event of events) {
    event.head_sha = HEAD;
    delete event.metadata_redacted;
  }
  const result = evaluate({ ledgerEvents: events });
  assert.equal(result.decision, ELIGIBLE, JSON.stringify(result.reasons));
});

// ------------------------- EVERY evidence class binds to the one trusted head

test('accessibility evidence from a different commit denies, which the offline checker misses', () => {
  const pkg = pkgFor(HEAD);
  pkg.accessibility.provenance.sha = OLD_HEAD;
  // The control: checkOfflinePolicy binds only ci.sha and review.sha, so it is
  // perfectly happy with accessibility evidence for another commit.
  const { checkOfflinePolicy } = require('./validate.js');
  assert.equal(checkOfflinePolicy(pkg).policyValid, true);

  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'EVIDENCE_SHA_UNBOUND');
  assert.ok(reason);
  assert.match(reason.detail, /accessibility\.provenance\.sha/);
  assert.equal(reason.determination, VERIFIED_FALSE);
});

test('security evidence from a different commit denies', () => {
  const pkg = pkgFor(HEAD);
  pkg.security.provenance.sha = OLD_HEAD;
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.code === 'EVIDENCE_SHA_UNBOUND' &&
    r.detail.includes('security.provenance.sha')));
});

test('a per-check SHA from a different commit denies', () => {
  const pkg = pkgFor(HEAD);
  pkg.accessibility = {
    applicable: true,
    findings: [],
    checks: [
      { check_id: 'AXE_SCAN', result: 'PASS', artifact_reference: 'a://1', sha: HEAD },
      { check_id: 'TOUCH_TARGETS', result: 'PASS', artifact_reference: 'a://2', sha: OLD_HEAD },
    ],
    provenance: provenance(HEAD, { producer: 'Accessibility Reviewer', kind: 'accessibility-review' }),
  };
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.code === 'EVIDENCE_SHA_UNBOUND' &&
    r.detail.includes('accessibility.checks[1].sha')));
});

test('a missing required provenance SHA denies as COULD_NOT_VERIFY', () => {
  const pkg = pkgFor(HEAD);
  delete pkg.security.provenance.sha;
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'EVIDENCE_SHA_UNBOUND');
  assert.ok(reason);
  assert.equal(reason.determination, COULD_NOT_VERIFY);
});

test('the optional accessibility.sha is bound when present', () => {
  const pkg = pkgFor(HEAD);
  pkg.accessibility.sha = OLD_HEAD;
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.detail.includes('accessibility.sha=')));
});

test('verifyEvidenceShaBinding passes when every class names the trusted head', () => {
  assert.deepEqual(verifyEvidenceShaBinding(pkgFor(HEAD), HEAD), { ok: true });
});

// -------------------------------------------------------- default deny, per adapter

test('DEFAULT DENY: a COULD_NOT_VERIFY from CI is a denial, not weak evidence of a pass', () => {
  const result = evaluate({}, {
    resolveCi: () => ({ ok: false, reason: 'CI_FETCH_FAILED', determination: COULD_NOT_VERIFY, detail: 'unreadable' }),
  });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'CI_UNVERIFIED');
  assert.equal(reason.determination, COULD_NOT_VERIFY);
});

test('DEFAULT DENY: a VERIFIED_FALSE from CI denies', () => {
  const result = evaluate({}, {
    resolveCi: () => ({ ok: false, reason: 'REQUIRED_CHECK_NOT_SUCCESS', determination: VERIFIED_FALSE, detail: 'failure' }),
  });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('CI_UNVERIFIED'));
});

test('DEFAULT DENY: an unverified reviewer identity denies', () => {
  const result = evaluate({}, {
    resolveReviewer: () => ({ ok: false, reason: 'SELF_REVIEW', determination: VERIFIED_FALSE, detail: 'builder reviewed itself' }),
  });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('REVIEWER_UNVERIFIED'));
});

test('DEFAULT DENY: an unresolvable task record denies', () => {
  const result = evaluate({}, {
    resolveTask: () => ({ ok: false, reason: 'NO_SUCH_TASK', detail: 'not in config/tasks.json' }),
  });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('TASK_RECORD_UNVERIFIED'));
});

test('DEFAULT DENY: an unresolvable head SHA denies and leaves trustedHeadSha null', () => {
  const result = evaluate({}, {
    resolveHeadSha: () => ({ ok: false, reason: 'NO_SUCH_REF', detail: 'unknown ref' }),
  });
  assert.equal(result.decision, DENIED);
  assert.equal(result.trustedHeadSha, null);
  assert.ok(codes(result).includes('HEAD_SHA_UNVERIFIED'));
});

test('DEFAULT DENY: an adapter returning a truthy non-ok object does not pass', () => {
  // `{ sha: ... }` with no ok flag must not be read as success.
  const result = evaluate({}, { resolveHeadSha: () => ({ sha: HEAD }) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('HEAD_SHA_UNVERIFIED'));
});

test('DEFAULT DENY: a missing adapter function denies instead of throwing', () => {
  const result = evaluateLiveMergeEligibility(
    { identity: IDENTITY, prNumber: PR, pkg: pkgFor(HEAD), prView: prView(), ledgerEvents: ledgerFor(HEAD) },
    { resolveHeadSha: () => ({ ok: true, sha: HEAD }) }
  );
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('COMPOSITION_INPUT_INVALID'));
});

test('DEFAULT DENY: an empty request denies instead of throwing', () => {
  const result = evaluateLiveMergeEligibility({}, {});
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.length > 0);
});

test('DEFAULT DENY: a missing prNumber denies, because the schema carries none', () => {
  const result = evaluate({ prNumber: undefined });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('COMPOSITION_INPUT_INVALID'));
});

// --------------------------------------------------- the offline half still runs

test('the severity floor and requirement registry still govern: an unknown citation denies', () => {
  const pkg = pkgFor(HEAD);
  pkg.accessibility = {
    applicable: true,
    findings: [
      {
        id: 'TASK002-R1-ACC-001',
        classification: 'FAILURE',
        jev_severity: 'P3',
        status: 'OPEN',
        unmet_requirement: 'ACC-DOD-NOT-A-REAL-REQUIREMENT',
      },
    ],
    provenance: provenance(HEAD, { producer: 'Accessibility Reviewer', kind: 'accessibility-review' }),
  };
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.code === 'OFFLINE_POLICY_FAILED' &&
    r.detail.includes('INVALID_ACCESSIBILITY_EVIDENCE')));
});

test('a registry-recognised FAILURE finding is floored to P1 and denies as a blocking finding', () => {
  const pkg = pkgFor(HEAD);
  pkg.accessibility = {
    applicable: true,
    findings: [
      {
        id: 'TASK002-R1-ACC-002',
        classification: 'FAILURE',
        jev_severity: 'P3',
        status: 'OPEN',
        unmet_requirement: 'ACC-DOD-VISIBLE_FOCUS',
      },
    ],
    provenance: provenance(HEAD, { producer: 'Accessibility Reviewer', kind: 'accessibility-review' }),
  };
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.detail.includes('UNRESOLVED_BLOCKING_FINDING')));
  // It must block as a RECOGNISED P1, not as unrecognised evidence. Both deny,
  // so without this the registry could be bypassed entirely and every test
  // above would still pass - the citation would simply never be recognised and
  // the accessibility box could never go green.
  assert.ok(!result.reasons.some((r) => r.detail.includes('INVALID_ACCESSIBILITY_EVIDENCE')));
});

test('a self-attested review producer denies through the offline half', () => {
  const pkg = pkgFor(HEAD);
  pkg.review.provenance.producer = 'Builder';
  const result = evaluate({ pkg: pkg });
  assert.equal(result.decision, DENIED);
  assert.ok(result.reasons.some((r) => r.detail.includes('SELF_ATTESTED_OR_UNKNOWN_PRODUCER')));
});

// ------------------------------------------------------------ live merge state

test('a draft pull request is DENIED and reported as a draft, never silently stalled', () => {
  const result = evaluate({ prView: prView({ isDraft: true }) });
  assert.equal(result.decision, DENIED);
  assert.equal(result.draftState, 'DRAFT');
  assert.deepEqual(codes(result), ['PR_IS_DRAFT']);
  assert.equal(result.blockedOnlyByDraft, true);
});

test('blockedOnlyByDraft is false when something else is also wrong', () => {
  const result = evaluate({ prView: prView({ isDraft: true }), ledgerEvents: [] });
  assert.equal(result.blockedOnlyByDraft, false);
  assert.ok(codes(result).includes('PR_IS_DRAFT'));
  assert.ok(codes(result).includes('REVIEW_PROVENANCE_MISSING'));
});

test('an absent isDraft flag is UNKNOWN and denies, rather than being read as ready', () => {
  const view = prView();
  delete view.isDraft;
  const result = evaluate({ prView: view });
  assert.equal(result.draftState, 'UNKNOWN');
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_DRAFT_STATE_UNKNOWN'));
});

test('mergeStateStatus BLOCKED is named as branch protection, not a generic denial', () => {
  const result = evaluate({ prView: prView({ mergeStateStatus: 'BLOCKED' }) });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'PR_BLOCKED_BY_BRANCH_PROTECTION');
  assert.ok(reason);
  assert.match(reason.detail, /approving review/);
});

test('a closed pull request denies distinguishably from a draft one', () => {
  const result = evaluate({ prView: prView({ state: 'CLOSED' }) });
  assert.ok(codes(result).includes('PR_NOT_OPEN'));
  assert.ok(!codes(result).includes('PR_IS_DRAFT'));
});

test('a conflicting branch denies', () => {
  const result = evaluate({ prView: prView({ mergeable: 'CONFLICTING' }) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_CONFLICTING'));
});

test('a branch behind its base denies', () => {
  const result = evaluate({ prView: prView({ mergeStateStatus: 'BEHIND' }) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_BEHIND_BASE'));
});

test('an unrecognised merge state denies: the allow-list is CLEAN only', () => {
  const result = evaluate({ prView: prView({ mergeStateStatus: 'UNSTABLE' }) });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_MERGE_STATE_NOT_CLEAN'));
});

test('an absent merge state denies as COULD_NOT_VERIFY', () => {
  const view = prView();
  delete view.mergeStateStatus;
  const result = evaluate({ prView: view });
  assert.equal(result.decision, DENIED);
  const reason = result.reasons.find((r) => r.code === 'PR_MERGE_STATE_NOT_CLEAN');
  assert.equal(reason.determination, COULD_NOT_VERIFY);
});

test('no pull-request observation at all denies', () => {
  const result = evaluate({ prView: undefined });
  assert.equal(result.decision, DENIED);
  assert.ok(codes(result).includes('PR_UNOBSERVED'));
  assert.ok(codes(result).includes('PR_HEAD_UNOBSERVED'));
});

// --------------------------------------------------------- the unit under it

test('eventHeadSha reads metadata_redacted.head_sha', () => {
  assert.deepEqual(eventHeadSha({ metadata_redacted: { head_sha: HEAD } }), { ok: true, sha: HEAD });
});

test('eventHeadSha rejects an abbreviated SHA', () => {
  assert.equal(eventHeadSha({ metadata_redacted: { head_sha: HEAD.slice(0, 12) } }).ok, false);
});

test('verifyReviewProvenance refuses a non-array ledger', () => {
  const result = verifyReviewProvenance(null, TASK, PR, HEAD, WORKER);
  assert.equal(result.ok, false);
  assert.equal(result.code, 'REVIEW_PROVENANCE_UNREADABLE');
});

test('verifyReviewProvenance ignores events for another pull request', () => {
  const events = ledgerFor(HEAD);
  for (const event of events) event.pr_id = PR + 1;
  const result = verifyReviewProvenance(events, TASK, PR, HEAD, WORKER);
  assert.equal(result.ok, false);
  assert.equal(result.code, 'REVIEW_PROVENANCE_MISSING');
});

test('verifyReviewProvenance ignores events for another task', () => {
  const events = ledgerFor(HEAD);
  for (const event of events) event.task_id = 'TASK-009';
  const result = verifyReviewProvenance(events, TASK, PR, HEAD, WORKER);
  assert.equal(result.ok, false);
  assert.equal(result.code, 'REVIEW_PROVENANCE_MISSING');
});

test('the LATEST review cycle governs: an older passing cycle cannot cover a newer commit', () => {
  // Cycle 1 reviewed OLD_HEAD and passed. Cycle 2 was dispatched for HEAD. The
  // last REVIEW_RESULT is cycle 1's, bound to OLD_HEAD, so the gate refuses.
  const events = [
    { event_type: 'REVIEW_DISPATCHED', task_id: TASK, pr_id: PR, role: 'reviewer',
      agent_id: WORKER, metadata_redacted: { head_sha: HEAD } },
    { event_type: 'REVIEW_RESULT', task_id: TASK, pr_id: PR, role: 'reviewer',
      agent_id: 'task-002-review-1', outcome: 'REVIEW_PASS',
      metadata_redacted: { head_sha: OLD_HEAD } },
  ];
  const result = verifyReviewProvenance(events, TASK, PR, HEAD, 'task-002-review-1');
  assert.equal(result.ok, false);
  assert.equal(result.code, 'REVIEW_PROVENANCE_SHA_MISMATCH');
});

// ------------------------------------- F5: blockedOnlyByPendingIndependentReview
//
// THE DEADLOCK THIS BREAKS. Once `run-002/independent-review` is a REQUIRED
// context on main, GitHub reports mergeStateStatus BLOCKED until something
// publishes it — and the thing that publishes it is this gate. Left alone the
// gate denies for PR_BLOCKED_BY_BRANCH_PROTECTION, nothing ever publishes, and
// every product PR is blocked forever: strictly worse than today, which is why
// proposal §0.1 lists it as one of the three blockers that must be resolved
// before the App is provisioned.
//
// The flag is a REPORTED FIELD for the publisher. It is never an input to
// `verified`, and `decision` is computed before it.

test('F5: the flag is false on a pull request that is already eligible', () => {
  const result = evaluate();
  assert.equal(result.decision, ELIGIBLE);
  assert.equal(result.blockedOnlyByPendingIndependentReview, false);
});

test('F5: BLOCKED as the ONLY reason, with CI verified green, sets the flag', () => {
  const result = evaluate({ prView: prView({ mergeStateStatus: 'BLOCKED' }) });
  assert.deepEqual(codes(result), ['PR_BLOCKED_BY_BRANCH_PROTECTION']);
  assert.equal(result.blockedOnlyByPendingIndependentReview, true);
});

test('F5: the flag never makes the decision ELIGIBLE', () => {
  const result = evaluate({ prView: prView({ mergeStateStatus: 'BLOCKED' }) });
  assert.equal(result.decision, DENIED);
});

test('F5: BLOCKED with a FAILING CI does not set the flag', () => {
  // The case the entire safety argument is about: GitHub reports BLOCKED
  // for a failing required check too, so a flag derived from the raw state
  // would bless a pull request whose CI is red.
  const result = evaluate(
    { prView: prView({ mergeStateStatus: 'BLOCKED' }) },
    { resolveCi: () => ({ ok: false, determination: VERIFIED_FALSE,
      reason: 'CI_FAILED', detail: 'ci concluded failure' }) }
  );
  assert.equal(result.blockedOnlyByPendingIndependentReview, false);
  assert.ok(codes(result).includes('CI_UNVERIFIED'));
});

test('F5: BLOCKED alongside any second reason does not set the flag', () => {
  const result = evaluate({
    prView: prView({ mergeStateStatus: 'BLOCKED', isDraft: true }),
  });
  assert.ok(result.reasons.length > 1);
  assert.equal(result.blockedOnlyByPendingIndependentReview, false);
});

test('F5: a different merge-state denial does not set the flag', () => {
  for (const state of ['DIRTY', 'BEHIND', 'UNKNOWN', 'UNSTABLE']) {
    const result = evaluate({ prView: prView({ mergeStateStatus: state }) });
    assert.equal(result.blockedOnlyByPendingIndependentReview, false,
      'state ' + state + ' must not set the flag');
  }
});

test('F5: an unobserved pull request does not set the flag', () => {
  const result = evaluate({ prView: undefined });
  assert.equal(result.blockedOnlyByPendingIndependentReview, false);
});

test('F5 MUTATION: deriving the flag from mergeStateStatus is fail-open', () => {
  // Demonstrated, not asserted — the proposal required this before the
  // resolution is believed. Both formulations are computed over the SAME
  // observation, for a pull request whose CI is VERIFIED FAILING.
  const observation = prView({ mergeStateStatus: 'BLOCKED' });
  const result = evaluate(
    { prView: observation },
    { resolveCi: () => ({ ok: false, determination: VERIFIED_FALSE,
      reason: 'CI_FAILED', detail: 'ci concluded failure' }) }
  );

  // The REJECTED formulation, written out in full.
  const rejected = String(observation.mergeStateStatus).toUpperCase() === 'BLOCKED';
  assert.equal(rejected, true,
    'the rejected formulation would publish an independent-review PASS for a ' +
    'pull request whose CI concluded failure');

  // The IMPLEMENTED one refuses, because it reads the reason list and the
  // CI adapter rather than the raw state.
  assert.equal(result.blockedOnlyByPendingIndependentReview, false);
});
