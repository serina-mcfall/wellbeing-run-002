// Tests for THE GATE PROGRAM — the deployment half of C-20a(C) action 8.
//
// SIMULATED EXTERNAL SERVICES ONLY. Three edges exist and all three are
// simulated:
//
//   * GITHUB's check-run surface is an array this file writes, handed in
//     through `request.checkRuns`. No `gh`, no token, no network, and the
//     program contains no fetcher that could reach one.
//   * the PULL REQUEST observation is an object this file composes.
//   * the EXPORT is a directory this file builds by copying `apparatus/`
//     and `config/` into a temp directory.
//
// What is NOT simulated: `git` is the real binary against a real
// throwaway repository this file creates, the ledger is a real
// `.runtime/ledger.jsonl` on disk, the task graph is this repository's
// real `config/tasks.json`, and the composition, the validator, the
// schema, the severity floor and all four adapters are the real modules.
//
// THE MOST IMPORTANT TEST IN THIS FILE is `the export cannot see the
// workspace and still resolves a real head`. It runs the gate from a
// directory that is NOT `config/isolation.json`'s `workspace` and proves
// a real SHA comes back — the regression test for THE INVOKER TRAP, in
// all three of its instances.

const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');

const cli = require('./gate-cli.js');
const { evaluateLiveMergeEligibility } = require('./live-gate.js');
const { resolveTrustedHeadSha } = require('../adapters/git-head.js');
const {
  loadReviewerEvidence,
  resolveReviewerIdentity,
} = require('../adapters/reviewer-identity.js');
const { resolveRun002CiResult } = require('../adapters/ci-result.js');
const { resolveRun002TaskRecord } = require('../adapters/task-record.js');
const { buildEvidencePackage } = require('../fixture-preflight/evidence.js');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const BRANCH = 'run-002/gate-cli-fixture';
const TASK = 'TASK-001';
const PR = 11;
const WORKER = TASK.toLowerCase() + '-review-1';

// config/experiment.json's github.required_checks, which the gate reads
// from the EXPORT. Named here so a drift shows up as a failing fixture
// rather than as a mysterious REQUIRED_CHECK_MISSING.
const REQUIRED_CHECK = 'ci';

const COMMIT_ENV = {
  GIT_AUTHOR_NAME: 'Gate Fixture',
  GIT_AUTHOR_EMAIL: 'gate@example.invalid',
  GIT_COMMITTER_NAME: 'Gate Fixture',
  GIT_COMMITTER_EMAIL: 'gate@example.invalid',
  GIT_CONFIG_GLOBAL: '/dev/null',
  GIT_CONFIG_SYSTEM: '/dev/null',
};

function tempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/**
 * The program's source with its comments stripped.
 *
 * Several claims below are about what the CODE reaches, while the
 * docstring deliberately NAMES the things it must not reach in order to
 * explain why. Both comment forms are stripped — the line comments of the
 * header and the JSDoc blocks over each function — and gate-cli.js holds
 * no string literal containing either delimiter, so the strip is exact.
 */
function programCode() {
  return fs
    .readFileSync(path.join(__dirname, 'gate-cli.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

// ------------------------------------------------------------------
// A real throwaway checkout: the only place real git facts exist.
// ------------------------------------------------------------------

function makeLiveCheckout() {
  const root = tempDir('run002-gatecli-live-');
  const git = (args) =>
    execFileSync('git', ['-C', root].concat(args), {
      encoding: 'utf8',
      env: Object.assign({}, process.env, COMMIT_ENV),
    });

  git(['init', '-q', '-b', 'main']);
  fs.writeFileSync(path.join(root, 'README'), 'gate-cli fixture\n');
  git(['add', 'README']);
  git(['commit', '-q', '-m', 'fixture']);
  git(['branch', BRANCH]);
  const sha = git(['rev-parse', '--verify', BRANCH]).trim();

  // The append-only ledger the Supervisor would have written, in the
  // shape handover §34.2 agreed: both events carry the full head SHA at
  // metadata_redacted.head_sha, and REVIEW_RESULT names the worker.
  const runtime = path.join(root, '.runtime');
  fs.mkdirSync(runtime);
  const common = {
    task_id: TASK,
    pr_id: PR,
    role: 'reviewer',
    agent_id: WORKER,
    provider: 'codex',
    metadata_redacted: { head_sha: sha },
  };
  fs.writeFileSync(
    path.join(runtime, 'ledger.jsonl'),
    [
      JSON.stringify(Object.assign({ event_type: 'REVIEW_DISPATCHED', outcome: 'DISPATCHED' }, common)),
      JSON.stringify(Object.assign({ event_type: 'REVIEW_RESULT', outcome: 'REVIEW_PASS' }, common)),
    ].join('\n') + '\n'
  );
  fs.writeFileSync(
    path.join(runtime, 'state.json'),
    JSON.stringify({ prs: { [String(PR)]: { reviewed_head: sha } } })
  );

  return { root, sha };
}

// ------------------------------------------------------------------
// A simulated read-only export.
// ------------------------------------------------------------------
//
// The WHOLE of `apparatus/` is copied, not a hand-picked pair of
// adapters: the gate's require chain is gate-cli -> live-gate ->
// validate -> severity-floor + requirement-registry + ajv, and an export
// missing any link crashes before deciding anything. Copying two files
// would make this suite pass while the real export died.
//
// `node_modules/` is provisioned separately and ON PURPOSE — see
// `an export with no node_modules cannot load the gate at all`, which
// proves that provisioning is load-bearing rather than incidental.

function nodeModulesDir() {
  // Resolved rather than assumed, so this works wherever the install is.
  // `ajv/package.json` is not reachable through ajv's exports map, so the
  // probe is a real entry point and the directory is read back off it.
  const parts = require.resolve('ajv/dist/2020').split(path.sep);
  const at = parts.lastIndexOf('node_modules');
  assert.notEqual(at, -1, 'ajv did not resolve from a node_modules directory');
  return parts.slice(0, at + 1).join(path.sep);
}

// Everything the gate's require chain reaches: `apparatus/` for the code,
// `protocol/` for the schema validate.js loads at module scope,
// `config/` for the pinned task graph and required-check list, and
// `product/` for the accessibility requirement registry's source.
const EXPORT_TREES = ['apparatus', 'protocol', 'config', 'product'];

function makeExport({ withNodeModules = true } = {}) {
  const root = tempDir('run002-gatecli-export-');
  for (const tree of EXPORT_TREES) {
    fs.cpSync(path.join(REPO_ROOT, tree), path.join(root, tree), {
      recursive: true,
      filter: (src) => !src.split(path.sep).includes('node_modules'),
    });
  }
  if (withNodeModules) {
    fs.symlinkSync(nodeModulesDir(), path.join(root, 'apparatus', 'node_modules'), 'dir');
  }
  return root;
}

function runExportedGate(exportRoot, envelope) {
  const entry = path.join(exportRoot, 'apparatus', 'pr-evidence', 'gate-cli.js');
  const proc = require('node:child_process').spawnSync(
    process.execPath,
    [entry],
    { input: JSON.stringify(envelope), encoding: 'utf8', timeout: 60000 }
  );
  return proc;
}

// ------------------------------------------------------------------
// Request material.
// ------------------------------------------------------------------

function passingCheckRuns(sha) {
  return [{ name: REQUIRED_CHECK, head_sha: sha, status: 'completed', conclusion: 'success' }];
}

function prView(sha, over) {
  return Object.assign(
    {
      state: 'OPEN',
      isDraft: false,
      headRefOid: sha,
      mergeable: 'MERGEABLE',
      mergeStateStatus: 'CLEAN',
    },
    over
  );
}

function request(sha, over) {
  return Object.assign(
    {
      identity: { kind: 'branch', ref: BRANCH },
      prNumber: PR,
      pkg: buildEvidencePackage({ taskId: TASK, sha: sha, cycle: 1 }),
      prView: prView(sha),
      checkRuns: passingCheckRuns(sha),
    },
    over
  );
}

function envelopeFor(live, over) {
  return {
    [cli.LIVE_ROOT_KEY]: live.root,
    [cli.REQUEST_KEY]: request(live.sha, over),
  };
}

// One live checkout and one export for the whole file. Both are real
// directories; building them per test would spend minutes on nothing.
let LIVE = null;
let EXPORT = null;

test.before(() => {
  LIVE = makeLiveCheckout();
  EXPORT = makeExport();
});

test.after(() => {
  if (LIVE) fs.rmSync(LIVE.root, { recursive: true, force: true });
  if (EXPORT) fs.rmSync(EXPORT, { recursive: true, force: true });
});

// ==================================================================
// 1. THE ROUND TRIP
// ==================================================================

test('the invoker envelope goes in and a complete decision comes out', () => {
  const result = cli.main(JSON.stringify(envelopeFor(LIVE)));

  assert.equal(result.exitCode, 0, result.stdout);
  const decision = JSON.parse(result.stdout);
  assert.ok(cli.decisionIsComplete(decision), result.stdout);
  assert.equal(decision.trustedHeadSha, LIVE.sha);
  assert.equal(decision.decision, 'ELIGIBLE', JSON.stringify(decision.reasons));
  assert.deepEqual(decision.reasons, []);
});

test('every adapter really ran, against the real sources', () => {
  const decision = JSON.parse(cli.main(JSON.stringify(envelopeFor(LIVE))).stdout);

  // git, on the throwaway repo.
  assert.equal(decision.adapters.headSha.ok, true);
  assert.equal(decision.adapters.headSha.resolvedFrom, 'branch:' + BRANCH);
  // the pinned task graph, from the export side.
  assert.equal(decision.adapters.task.ok, true);
  assert.equal(decision.adapters.task.task.id, TASK);
  // the injected check-run observation.
  assert.equal(decision.adapters.ci.ok, true);
  assert.deepEqual(decision.adapters.ci.requiredChecks, [REQUIRED_CHECK]);
  // the real ledger on disk, in the LIVE checkout.
  assert.equal(decision.adapters.reviewer.ok, true);
  assert.equal(decision.adapters.reviewer.workerId, WORKER);
  assert.equal(decision.adapters.reviewProvenance.ok, true);
});

test('a denial is a decision: exit 0, and the reason survives', () => {
  // A red required check. The gate judged it and said no; that is not a
  // failure of the gate and must not look like one.
  const envelope = envelopeFor(LIVE, {
    checkRuns: [
      { name: REQUIRED_CHECK, head_sha: LIVE.sha, status: 'completed', conclusion: 'failure' },
    ],
  });
  const result = cli.main(JSON.stringify(envelope));

  assert.equal(result.exitCode, 0);
  const decision = JSON.parse(result.stdout);
  assert.equal(decision.decision, 'DENIED');
  assert.deepEqual(decision.reasons.map((r) => r.code), ['CI_UNVERIFIED']);
  assert.equal(decision.trustedHeadSha, LIVE.sha);
});

test('the decision is emitted byte-identically, never rebuilt', () => {
  // The same inputs, composed twice: once through the library directly,
  // once through the program. If the program picked fields, reordered
  // them, or re-derived anything, these strings would differ.
  const envelope = envelopeFor(LIVE);
  const req = envelope[cli.REQUEST_KEY];
  const loaded = loadReviewerEvidence(path.join(LIVE.root, '.runtime'), PR);
  const direct = evaluateLiveMergeEligibility(
    {
      identity: req.identity,
      prNumber: req.prNumber,
      pkg: req.pkg,
      prView: req.prView,
      ledgerEvents: loaded.ledgerEvents,
    },
    {
      resolveHeadSha: (id) => resolveTrustedHeadSha(id, { repoRoot: LIVE.root }),
      resolveCi: (sha) =>
        resolveRun002CiResult(sha, {
          fetchCheckRuns: () => ({ check_runs: req.checkRuns }),
        }),
      resolveTask: (id) => resolveRun002TaskRecord(id),
      resolveReviewer: (claim) =>
        resolveReviewerIdentity(claim, {
          ledgerEvents: loaded.ledgerEvents,
          prRecord: loaded.prRecord,
        }),
    }
  );

  assert.equal(cli.main(JSON.stringify(envelope)).stdout, JSON.stringify(direct));
});

test('the pending-independent-review flag is passed through, not re-derived', () => {
  // THE F5 MUTATION, from the far side. GitHub reports BLOCKED for a
  // protection rule AND for a red `ci`. live-gate.js computes the flag
  // from the reason list plus the CI adapter's own `ok`; a program that
  // read `mergeStateStatus === 'BLOCKED'` instead would publish an
  // independent-review pass for a pull request whose CI is red.
  const blocked = prView(LIVE.sha, { mergeStateStatus: 'BLOCKED' });

  const green = JSON.parse(cli.main(JSON.stringify(
    envelopeFor(LIVE, { prView: blocked })
  )).stdout);
  assert.equal(green.blockedOnlyByPendingIndependentReview, true);
  assert.deepEqual(green.reasons.map((r) => r.code), ['PR_BLOCKED_BY_BRANCH_PROTECTION']);

  const red = JSON.parse(cli.main(JSON.stringify(
    envelopeFor(LIVE, {
      prView: blocked,
      checkRuns: [
        { name: REQUIRED_CHECK, head_sha: LIVE.sha, status: 'completed', conclusion: 'failure' },
      ],
    })
  )).stdout);
  // Same mergeStateStatus on both runs, opposite flag: the raw state is
  // demonstrably not what the flag is computed from.
  assert.equal(blocked.mergeStateStatus, 'BLOCKED');
  assert.equal(red.blockedOnlyByPendingIndependentReview, false,
    'the flag tracked mergeStateStatus instead of the CI leg');
});

// ==================================================================
// 2. THE INVOKER TRAP — the regression tests
// ==================================================================

test('the export cannot see the workspace and still resolves a real head', () => {
  // THE SINGLE MOST IMPORTANT TEST IN THIS FILE.
  //
  // The program runs from a directory that is NOT
  // config/isolation.json's `workspace`, with a pinned isolation.json
  // that names a workspace it can never be. The convenience wrappers
  // would return WORKSPACE_MISMATCH and LEDGER_UNREADABLE from here, and
  // every pull request would be denied forever. A real SHA coming back
  // is the proof that the low-level forms were used with the live root.
  const proc = runExportedGate(EXPORT, envelopeFor(LIVE));

  assert.equal(proc.status, 0, proc.stderr);
  const decision = JSON.parse(proc.stdout);
  assert.equal(decision.trustedHeadSha, LIVE.sha,
    'the gate resolved no head from the export — THE INVOKER TRAP');
  assert.equal(decision.decision, 'ELIGIBLE', JSON.stringify(decision.reasons));
});

test('the export is really somewhere else, and really has no runtime dir', () => {
  // The precondition the test above rests on. Without this, a green
  // trap test could be green because the export was the workspace.
  const isolation = JSON.parse(
    fs.readFileSync(path.join(EXPORT, 'config', 'isolation.json'), 'utf8')
  );
  assert.notEqual(fs.realpathSync(EXPORT), isolation.workspace);
  assert.equal(fs.existsSync(path.join(EXPORT, '.runtime')), false);
});

test('trap 1: the head would be unresolvable from the export location', () => {
  // The trap reproduced rather than argued, at the exact position the
  // program occupies. `resolveTrustedHeadSha` pointed at the EXPORT -
  // which is what deriving a root from `__dirname` amounts to - cannot
  // resolve the branch, because the export is not that repository.
  const fromExport = resolveTrustedHeadSha(
    { kind: 'branch', ref: BRANCH }, { repoRoot: EXPORT }
  );
  assert.equal(fromExport.ok, false,
    'the trap did not reproduce — re-read this before trusting the fix');
  assert.equal(fromExport.reason, 'NO_SUCH_REF');

  // The same call with the LIVE root, which is what the program does.
  const fromLive = resolveTrustedHeadSha(
    { kind: 'branch', ref: BRANCH }, { repoRoot: LIVE.root }
  );
  assert.equal(fromLive.ok, true);
  assert.equal(fromLive.sha, LIVE.sha);
});

test('trap 3: the ledger read from the export would deny every pull request', () => {
  // The THIRD instance, and the one no wrapper is involved in.
  // `live-gate.js` takes `request.ledgerEvents` whole, so this program
  // decides where the ledger comes from. From the export there is none.
  const fromExport = loadReviewerEvidence(path.join(EXPORT, '.runtime'), PR);
  assert.equal(fromExport.ok, false,
    'the ledger trap did not reproduce — re-read this test');
  assert.equal(fromExport.reason, 'LEDGER_UNREADABLE');

  // And the program, run from that same export, nevertheless proves
  // review provenance — because it read the LIVE checkout's ledger.
  const decision = JSON.parse(runExportedGate(EXPORT, envelopeFor(LIVE)).stdout);
  assert.equal(decision.adapters.reviewer.ok, true,
    'the gate read no ledger — trap 3');
  assert.equal(decision.adapters.reviewer.workerId, WORKER);
  assert.equal(decision.adapters.reviewProvenance.ok, true);
  assert.equal(
    decision.reasons.filter((r) => r.code.startsWith('REVIEW')).length, 0
  );
});

test('a caller-supplied ledger is ignored; the gate reads its own', () => {
  // The ledger is a FACT. A request that carried its own events would be
  // a request that supplied its own provenance, which is the
  // self-attestation the reviewer adapter exists to remove.
  const forged = [{
    event_type: 'REVIEW_RESULT', task_id: TASK, pr_id: PR, role: 'reviewer',
    agent_id: 'not-a-dispatched-worker', provider: 'codex',
    outcome: 'REVIEW_PASS', metadata_redacted: { head_sha: LIVE.sha },
  }];
  const decision = JSON.parse(cli.main(JSON.stringify(
    envelopeFor(LIVE, { ledgerEvents: forged })
  )).stdout);

  // The real on-disk ledger names the real worker; the forged array did
  // not reach the adapter at all.
  assert.equal(decision.adapters.reviewer.workerId, WORKER);
  assert.equal(decision.decision, 'ELIGIBLE', JSON.stringify(decision.reasons));
});

test('neither Run002 convenience wrapper appears in the program', () => {
  // A cheap tripwire alongside the behavioural tests above. The two
  // wrappers are NAMED in the file's comments, so only code is read.
  const code = programCode();

  assert.equal(code.includes('resolveRun002TrustedHeadSha'), false);
  assert.equal(code.includes('resolveRun002ReviewerIdentity'), false);
  // The two that are CORRECT to use, still used: config from the export.
  assert.equal(code.includes('resolveRun002CiResult'), true);
  assert.equal(code.includes('resolveRun002TaskRecord'), true);
});

// ==================================================================
// 3. NO FETCHER, NO NETWORK
// ==================================================================

test('the CI fetcher is required and has no default', () => {
  const refusal = cli.runGate(envelopeFor(LIVE), {});
  assert.equal(refusal.ok, false);
  assert.equal(refusal.outcome, cli.NO_CI_SOURCE);

  // And it is checked before anything else, so no envelope can smuggle
  // a run past it.
  assert.equal(cli.runGate({}, {}).outcome, cli.NO_CI_SOURCE);
  assert.equal(cli.runGate(null, undefined).outcome, cli.NO_CI_SOURCE);
});

test('no check-run observation is a refusal, never a fetch', () => {
  const envelope = envelopeFor(LIVE);
  delete envelope[cli.REQUEST_KEY].checkRuns;
  const result = cli.main(JSON.stringify(envelope));

  assert.equal(result.exitCode, cli.REFUSED_EXIT);
  assert.equal(JSON.parse(result.stdout).outcome, cli.CI_OBSERVATION_MISSING);
});

test('the program constructs no gh-invoking fetcher at all', () => {
  // The strongest statement available about an edge: the source cannot
  // reach it, so no test can forget to stub it.
  const code = programCode();

  for (const forbidden of ['execFileSync', 'execSync', 'spawn', 'https', 'fetch(', "'gh'"]) {
    assert.equal(code.includes(forbidden), false,
      'gate-cli.js reaches for ' + forbidden);
  }
});

test('a fetcher that throws denies; it never passes', () => {
  const exploding = () => { throw new Error('simulated outage'); };
  const result = cli.runGate(envelopeFor(LIVE), { fetchCheckRuns: exploding });

  assert.equal(result.ok, true, 'a dead CI source is a denial, not a crash');
  assert.equal(result.decision.decision, 'DENIED');
  assert.equal(result.decision.adapters.ci.reason, 'CI_FETCH_FAILED');
  assert.equal(result.decision.adapters.ci.determination, 'COULD_NOT_VERIFY');
  assert.equal(result.decision.blockedOnlyByPendingIndependentReview, false);
});

// ==================================================================
// 4. FAIL CLOSED, FINITELY, DISTINCTLY
// ==================================================================

test('unparseable stdin is a refusal', () => {
  const result = cli.main('{not json');
  assert.equal(result.exitCode, cli.REFUSED_EXIT);
  assert.equal(JSON.parse(result.stdout).outcome, cli.ENVELOPE_UNPARSEABLE);
});

test('a malformed envelope is a refusal, distinct from unparseable', () => {
  for (const raw of ['null', '[]', '"text"', '{}', '{"request": 3}', '{"request": []}']) {
    const result = cli.main(raw);
    assert.equal(result.exitCode, cli.REFUSED_EXIT, raw);
    assert.equal(JSON.parse(result.stdout).outcome, cli.ENVELOPE_MALFORMED, raw);
  }
});

test('a missing or relative live root is its own refusal', () => {
  for (const live of [undefined, null, '', '   ', 42, 'relative/path', './x']) {
    const envelope = { [cli.REQUEST_KEY]: request(LIVE.sha) };
    if (live !== undefined) envelope[cli.LIVE_ROOT_KEY] = live;
    const result = cli.main(JSON.stringify(envelope));
    assert.equal(result.exitCode, cli.REFUSED_EXIT, String(live));
    assert.equal(JSON.parse(result.stdout).outcome, cli.LIVE_ROOT_INVALID, String(live));
  }
});

test('a live root that is not a repository denies; it does not refuse', () => {
  // An absolute path with no git in it is a FACT about the world, not a
  // malformed request, so the gate judges and denies.
  const empty = tempDir('run002-gatecli-empty-');
  try {
    const envelope = {
      [cli.LIVE_ROOT_KEY]: empty,
      [cli.REQUEST_KEY]: request(LIVE.sha),
    };
    const result = cli.main(JSON.stringify(envelope));
    assert.equal(result.exitCode, 0);
    const decision = JSON.parse(result.stdout);
    assert.equal(decision.decision, 'DENIED');
    assert.equal(decision.trustedHeadSha, null);
    assert.ok(decision.reasons.some((r) => r.code === 'HEAD_SHA_UNVERIFIED'));
  } finally {
    fs.rmSync(empty, { recursive: true, force: true });
  }
});

test('an adapter that raises is a refusal, never a partial decision', () => {
  // `evaluateLiveMergeEligibility` does not guard its adapter calls, so a
  // raising adapter escapes it mid-composition. Nothing complete exists
  // at that point, and emitting a DENIED would assert a judgement that
  // was never made.
  const envelope = envelopeFor(LIVE);
  const result = cli.runGate(envelope, {
    fetchCheckRuns: () => {
      // A Proxy whose property read throws escapes ci-result.js's own
      // try/catch, which only wraps the call itself.
      return new Proxy({}, { get() { throw new Error('simulated'); } });
    },
  });
  assert.equal(result.ok, false);
  assert.equal(result.outcome, cli.EVALUATION_FAILED);
  assert.equal(result.decision, undefined);
});

test('an incomplete decision is refused rather than emitted', () => {
  // Driven through the same predicate the program applies, over every
  // field `gate_invoker.decision_is_usable` reads.
  const complete = {
    decision: 'DENIED',
    trustedHeadSha: null,
    blockedOnlyByPendingIndependentReview: false,
    reasons: [],
  };
  assert.equal(cli.decisionIsComplete(complete), true);

  const broken = [
    Object.assign({}, complete, { decision: 'MAYBE' }),
    Object.assign({}, complete, { blockedOnlyByPendingIndependentReview: 'true' }),
    Object.assign({}, complete, { blockedOnlyByPendingIndependentReview: 1 }),
    Object.assign({}, complete, { reasons: 'none' }),
    Object.assign({}, complete, { trustedHeadSha: 'abc' }),
    Object.assign({}, complete, { trustedHeadSha: 'A'.repeat(40) }),
  ];
  for (const value of broken) {
    assert.equal(cli.decisionIsComplete(value), false, JSON.stringify(value));
  }
  const noKey = Object.assign({}, complete);
  delete noKey.trustedHeadSha;
  assert.equal(cli.decisionIsComplete(noKey), false);

  for (const value of [null, undefined, 3, 'x', []]) {
    assert.equal(cli.decisionIsComplete(value), false, String(value));
  }
});

test('a stdin read fault is reported, not thrown', () => {
  // The one link that touches the outside world. An unhandled 'error' on
  // stdin would crash the process with no outcome token at all.
  const stream = new Readable({ read() {} });
  return new Promise((resolve, reject) => {
    cli.readAll(stream, (err, text) => {
      try {
        assert.ok(err instanceof Error, 'the read fault was swallowed');
        assert.equal(err.message, 'simulated stdin fault');
        assert.equal(text, undefined, 'a faulted read handed back text');
        resolve();
      } catch (failure) {
        reject(failure);
      }
    });
    stream.destroy(new Error('simulated stdin fault'));
  });
});

test('readAll delivers what it was given, once', () => {
  const stream = Readable.from(['{"a"', ':1}']);
  let calls = 0;
  let text = null;
  cli.readAll(stream, (err, value) => { calls += 1; text = value; });
  return new Promise((resolve) => {
    stream.on('close', () => {
      assert.equal(calls, 1);
      assert.equal(text, '{"a":1}');
      resolve();
    });
  });
});

test('every outcome token is distinct, and every refusal is one of them', () => {
  assert.equal(new Set(cli.OUTCOMES).size, cli.OUTCOMES.length);

  const seen = [
    JSON.parse(cli.main('{nope').stdout).outcome,
    JSON.parse(cli.main('[]').stdout).outcome,
    JSON.parse(cli.main(JSON.stringify({ [cli.REQUEST_KEY]: request(LIVE.sha) })).stdout).outcome,
    JSON.parse(cli.main(JSON.stringify((() => {
      const e = envelopeFor(LIVE);
      delete e[cli.REQUEST_KEY].checkRuns;
      return e;
    })())).stdout).outcome,
    cli.runGate(envelopeFor(LIVE), {}).outcome,
  ];
  assert.deepEqual(seen, [
    cli.ENVELOPE_UNPARSEABLE,
    cli.ENVELOPE_MALFORMED,
    cli.LIVE_ROOT_INVALID,
    cli.CI_OBSERVATION_MISSING,
    cli.NO_CI_SOURCE,
  ]);
  assert.equal(new Set(seen).size, seen.length, 'two failures share one token');
  for (const outcome of seen) assert.ok(cli.OUTCOMES.includes(outcome), outcome);
});

test('a refusal body can never be mistaken for a decision', () => {
  // Belt and braces for a reader that ignored the exit code:
  // `gate_invoker.decision_is_usable` would call this MALFORMED, which
  // publishes nothing — never a verdict.
  const refusal = JSON.parse(cli.main('{nope').stdout);
  assert.equal(cli.decisionIsComplete(refusal), false);
  assert.equal(refusal.ok, false);
  assert.equal('decision' in refusal, false);
});

// ==================================================================
// 5. THE CONTRACT WITH THE PYTHON HALF
// ==================================================================

test('the envelope keys are the ones gate_invoker.py writes', () => {
  const python = fs.readFileSync(
    path.join(REPO_ROOT, 'control', 'gate_invoker.py'), 'utf8'
  );
  assert.ok(python.includes('LIVE_ROOT_KEY = "' + cli.LIVE_ROOT_KEY + '"'));
  assert.ok(python.includes('REQUEST_KEY = "' + cli.REQUEST_KEY + '"'));
  // And the entry path it spawns is this file.
  assert.ok(python.includes('GATE_ENTRY = "apparatus/pr-evidence/gate-cli.js"'));
});

test('a real subprocess run satisfies the invoker, stderr and all', () => {
  // `gate_invoker.run_node` captures stdout only. A chatty gate must
  // still parse, and an exit code must still be readable.
  const ok = runExportedGate(EXPORT, envelopeFor(LIVE));
  assert.equal(ok.status, 0);
  assert.doesNotThrow(() => JSON.parse(ok.stdout));

  const bad = require('node:child_process').spawnSync(
    process.execPath,
    [path.join(EXPORT, 'apparatus', 'pr-evidence', 'gate-cli.js')],
    { input: 'not an envelope', encoding: 'utf8', timeout: 60000 }
  );
  assert.notEqual(bad.status, 0, 'a refusal exited zero and would be read as a decision');
  assert.equal(JSON.parse(bad.stdout).outcome, cli.ENVELOPE_UNPARSEABLE);
});

test('an export with no node_modules cannot load the gate at all', () => {
  // THE PACKAGING GAP, reproduced. `node_modules/` is gitignored, so
  // `git worktree add --detach` creates none, and validate.js requires
  // ajv at MODULE LOAD with live-gate.js requiring validate.js at its
  // own. The gate crashes before deciding anything.
  //
  // This is why every other test in this file provisions node_modules
  // into its export: that provisioning is load-bearing, not incidental.
  // Packaging is the integration owner's to fix; if it is fixed by
  // shipping dependencies inside the export, this test should be
  // revisited rather than deleted.
  const bare = makeExport({ withNodeModules: false });
  try {
    const proc = runExportedGate(bare, envelopeFor(LIVE));
    assert.notEqual(proc.status, 0);
    assert.match(proc.stderr, /Cannot find module/);
  } finally {
    fs.rmSync(bare, { recursive: true, force: true });
  }
});

test('nothing in the program can publish', () => {
  // Comment lines are excluded because the docstring names both Python
  // modules in order to say it reaches neither; the claim under test is
  // about the CODE.
  for (const forbidden of ['publisher', 'publication_path', 'statuses/', 'createCommitStatus']) {
    assert.equal(programCode().includes(forbidden), false,
      'gate-cli.js references ' + forbidden);
  }
});
