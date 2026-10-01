// The ledger events and PR records below are synthetic fixtures shaped like
// the real ones control/supervisor.py writes. They are test data, not live
// proof that any review ever happened. Filesystem tests use isolated temp
// directories under os.tmpdir(), never the real .runtime.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
  resolveReviewerIdentity,
  loadReviewerEvidence,
  CODEX_REVIEWER_PRODUCER,
  INDEPENDENT_REVIEW_PROVIDER,
} = require('./reviewer-identity.js');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const TASK = 'TASK-002';
const PR = 7;
const SHA = 'c'.repeat(40);
const OTHER_SHA = 'd'.repeat(40);
const REVIEWER_WORKER = 'task-002-review-1';

function reviewResult(overrides) {
  return Object.assign(
    {
      event_type: 'REVIEW_RESULT',
      task_id: TASK,
      pr_id: PR,
      role: 'reviewer',
      provider: INDEPENDENT_REVIEW_PROVIDER,
      agent_id: REVIEWER_WORKER,
      outcome: 'REVIEW_PASS',
    },
    overrides
  );
}

function claim(overrides) {
  return Object.assign(
    { taskId: TASK, prNumber: PR, headSha: SHA, producer: CODEX_REVIEWER_PRODUCER, verdict: 'REVIEW_PASS' },
    overrides
  );
}

function prRecord(overrides) {
  return Object.assign({ reviewed_head: SHA }, overrides);
}

function resolve(events, record, claimOverrides) {
  return resolveReviewerIdentity(claim(claimOverrides), { ledgerEvents: events, prRecord: record });
}

test('positive: a Supervisor-dispatched Codex reviewer verdict for this exact head verifies', () => {
  const result = resolve([reviewResult()], prRecord());
  assert.equal(result.ok, true);
  assert.equal(result.workerId, REVIEWER_WORKER);
  assert.equal(result.provider, INDEPENDENT_REVIEW_PROVIDER);
  assert.equal(result.verdict, 'REVIEW_PASS');
  assert.equal(result.reviewedHead, SHA);
});

// The forgery this adapter exists to catch. The producer string is exactly the
// one validate.js accepts; the worker behind it is a Builder.
test('forged identity: the right producer name over a builder worker is VERIFIED_FALSE', () => {
  const result = resolve([reviewResult({ agent_id: 'task-002-build', provider: 'claude' })], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'FORGED_REVIEWER_IDENTITY');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('forged identity: a reviewer-shaped name for a DIFFERENT task is rejected', () => {
  const result = resolve([reviewResult({ agent_id: 'task-003-review-1' })], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'FORGED_REVIEWER_IDENTITY');
});

test('forged identity: a name that merely starts like a reviewer worker is rejected', () => {
  for (const agentId of ['task-002-review-1-extra', 'xtask-002-review-1', 'task-002-review-', 'task-002-review-x']) {
    const result = resolve([reviewResult({ agent_id: agentId })], prRecord());
    assert.equal(result.ok, false, agentId);
    assert.equal(result.reason, 'FORGED_REVIEWER_IDENTITY', agentId);
  }
});

test('a later review cycle is a legitimate reviewer worker', () => {
  const result = resolve([reviewResult({ agent_id: 'task-002-review-12' })], prRecord());
  assert.equal(result.ok, true);
  assert.equal(result.workerId, 'task-002-review-12');
});

test('a review produced by a non-independent provider is VERIFIED_FALSE', () => {
  const result = resolve([reviewResult({ provider: 'claude' })], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NOT_INDEPENDENT_PROVIDER');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('a worker that also acted as builder or fixer cannot be its own reviewer', () => {
  for (const role of ['builder', 'fixer']) {
    const events = [
      { event_type: 'FIX_DISPATCHED', task_id: TASK, pr_id: PR, role: role, agent_id: REVIEWER_WORKER },
      reviewResult(),
    ];
    const result = resolve(events, prRecord());
    assert.equal(result.ok, false, role);
    assert.equal(result.reason, 'SELF_REVIEW', role);
    assert.equal(result.determination, 'VERIFIED_FALSE', role);
  }
});

test('a verdict the reviewer never returned is VERIFIED_FALSE', () => {
  const result = resolve([reviewResult({ outcome: 'REVIEW_FAIL' })], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REVIEW_VERDICT_MISMATCH');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('the latest review cycle governs: an earlier PASS cannot cover a later FAIL', () => {
  const events = [reviewResult({ agent_id: 'task-002-review-1' }), reviewResult({ agent_id: 'task-002-review-2', outcome: 'REVIEW_FAIL' })];
  const result = resolve(events, prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REVIEW_VERDICT_MISMATCH');
});

test('a review of a different head SHA is stale, and stale is VERIFIED_FALSE', () => {
  const result = resolve([reviewResult()], prRecord({ reviewed_head: OTHER_SHA }));
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REVIEW_SHA_MISMATCH');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('no PR record, or one without a usable reviewed_head, is COULD_NOT_VERIFY', () => {
  for (const record of [null, undefined, {}, { reviewed_head: null }, { reviewed_head: SHA.slice(0, 12) }]) {
    const result = resolve([reviewResult()], record);
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'REVIEW_SHA_UNVERIFIABLE');
    assert.equal(result.determination, 'COULD_NOT_VERIFY');
  }
});

test('no REVIEW_RESULT in the ledger is COULD_NOT_VERIFY, never an implicit pass', () => {
  const result = resolve([], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_REVIEW_EVIDENCE');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

test("another task's or another PR's review never satisfies this one", () => {
  const foreign = [
    reviewResult({ task_id: 'TASK-003' }),
    reviewResult({ pr_id: 99 }),
    reviewResult({ event_type: 'REVIEW_DISPATCHED' }),
    reviewResult({ role: 'security' }),
  ];
  const result = resolve(foreign, prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_REVIEW_EVIDENCE');
});

test('a producer that is not the one named independent review role is VERIFIED_FALSE', () => {
  for (const producer of ['Builder', 'Fixer', 'codex reviewer', 'Codex Reviewer ', 'Security Reviewer', '', undefined]) {
    const result = resolve([reviewResult()], prRecord(), { producer: producer });
    assert.equal(result.ok, false, String(producer));
    assert.equal(result.reason, 'UNKNOWN_REVIEW_PRODUCER', String(producer));
    assert.equal(result.determination, 'VERIFIED_FALSE', String(producer));
  }
});

test('a malformed claim fails closed before any ledger evidence is weighed', () => {
  const bad = [
    [{ taskId: '' }, 'INVALID_CLAIM'],
    [{ prNumber: '7' }, 'INVALID_CLAIM'],
    [{ prNumber: 7.5 }, 'INVALID_CLAIM'],
    [{ verdict: '' }, 'INVALID_CLAIM'],
    [{ headSha: SHA.slice(0, 7) }, 'MALFORMED_SHA'],
    [{ headSha: SHA.toUpperCase() }, 'MALFORMED_SHA'],
  ];
  for (const [overrides, reason] of bad) {
    const result = resolve([reviewResult()], prRecord(), overrides);
    assert.equal(result.ok, false, JSON.stringify(overrides));
    assert.equal(result.reason, reason, JSON.stringify(overrides));
  }
  assert.equal(resolveReviewerIdentity(null, { ledgerEvents: [] }).reason, 'INVALID_CLAIM');
});

test('ledger junk entries are skipped without throwing, and absence still fails closed', () => {
  const result = resolve([null, 'REVIEW_RESULT', 42, {}, { event_type: 'REVIEW_RESULT' }], prRecord());
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_REVIEW_EVIDENCE');
});

test('ledger events that are not an array fail closed', () => {
  const result = resolveReviewerIdentity(claim(), { ledgerEvents: undefined, prRecord: prRecord() });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'INVALID_OPTIONS');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

// Drift guards: these constants are copies of frozen facts held elsewhere.
test('the independent reviewer provider matches config/experiment.json', () => {
  const experiment = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'config', 'experiment.json'), 'utf8'));
  assert.equal(INDEPENDENT_REVIEW_PROVIDER, experiment.roles.reviewer.provider);
});

test('the producer name matches the role Protocol v2 actually names', () => {
  const protocolText = fs.readFileSync(path.join(REPO_ROOT, 'protocol', 'RUN-002-PROTOCOL-v2.0.md'), 'utf8');
  assert.ok(
    protocolText.includes('**' + CODEX_REVIEWER_PRODUCER + ':** independent read-only engineering review'),
    'Protocol v2 no longer names this role in these words'
  );
});

// --- loadReviewerEvidence: the real file-reading path, isolated fixtures ---

let runtimeDir;
const cleanupDirs = [];

test.before(() => {
  runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), 'run002-reviewer-runtime-'));
  cleanupDirs.push(runtimeDir);
  fs.writeFileSync(path.join(runtimeDir, 'ledger.jsonl'), JSON.stringify(reviewResult()) + '\n\n');
  fs.writeFileSync(
    path.join(runtimeDir, 'state.json'),
    JSON.stringify({ prs: { [String(PR)]: prRecord() } })
  );
});

test.after(() => {
  for (const dir of cleanupDirs) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('loader: a real ledger and state file on disk feed a verifying result', () => {
  const loaded = loadReviewerEvidence(runtimeDir, PR);
  assert.equal(loaded.ok, true);
  const result = resolveReviewerIdentity(claim(), loaded);
  assert.equal(result.ok, true);
  assert.equal(result.workerId, REVIEWER_WORKER);
});

test('loader: an absent ledger is COULD_NOT_VERIFY, never an empty-and-therefore-fine ledger', () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'run002-reviewer-empty-'));
  cleanupDirs.push(empty);
  const loaded = loadReviewerEvidence(empty, PR);
  assert.equal(loaded.ok, false);
  assert.equal(loaded.reason, 'LEDGER_UNREADABLE');
  assert.equal(loaded.determination, 'COULD_NOT_VERIFY');
});

test('loader: one unparseable ledger line makes the whole file unusable as proof', () => {
  const broken = fs.mkdtempSync(path.join(os.tmpdir(), 'run002-reviewer-broken-'));
  cleanupDirs.push(broken);
  fs.writeFileSync(
    path.join(broken, 'ledger.jsonl'),
    JSON.stringify(reviewResult()) + '\n{ this line is truncated\n'
  );
  const loaded = loadReviewerEvidence(broken, PR);
  assert.equal(loaded.ok, false);
  assert.equal(loaded.reason, 'LEDGER_UNREADABLE');
});

test('loader: a missing state file leaves the reviewed SHA unverifiable, not assumed', () => {
  const noState = fs.mkdtempSync(path.join(os.tmpdir(), 'run002-reviewer-nostate-'));
  cleanupDirs.push(noState);
  fs.writeFileSync(path.join(noState, 'ledger.jsonl'), JSON.stringify(reviewResult()) + '\n');
  const loaded = loadReviewerEvidence(noState, PR);
  assert.equal(loaded.ok, true);
  assert.equal(loaded.prRecord, null);
  const result = resolveReviewerIdentity(claim(), loaded);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REVIEW_SHA_UNVERIFIABLE');
});

test('loader: a missing runtimeDir argument fails closed', () => {
  const loaded = loadReviewerEvidence(undefined, PR);
  assert.equal(loaded.ok, false);
  assert.equal(loaded.reason, 'INVALID_OPTIONS');
});
