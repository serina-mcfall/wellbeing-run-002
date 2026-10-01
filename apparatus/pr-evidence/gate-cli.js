// THE GATE PROGRAM. The deployment half of C-20a(C) §9 action 8.
//
// WHAT THIS IS. `live-gate.js` is a library function; nothing in this
// repository ran it as a process. `control/gate_invoker.py` is the Python
// half of action 8 — it proves an export trustworthy, spawns
// `apparatus/pr-evidence/gate-cli.js` inside it, and feeds it an ENVELOPE
// on stdin. This file is the other side of that interface, and its
// contract is `gate_invoker.py`'s, not its own:
//
//     stdin   {"liveRepoRoot": "<absolute path>", "request": <request>}
//     stdout  ONE JSON object — live-gate.js's decision, verbatim
//     exit 0  a decision was reached (ELIGIBLE or DENIED, both are
//             decisions); any other code means no decision was reached
//
// stderr is free: `gate_invoker.run_node` captures stdout only, and
// `ConnectedPathCase::test_diagnostics_on_stderr_do_not_corrupt_the_decision`
// holds that a chatty gate still parses.
//
// ===================================================================
// THE RULE: CONFIG FROM THE EXPORT, FACTS FROM THE LIVE CHECKOUT.
// ===================================================================
// This is the whole design, and getting it wrong denies every pull
// request forever. §7 of the approval package calls it THE INVOKER TRAP.
//
// The export is `git worktree add --detach /opt/run-002/gate-<SHA>`,
// `chmod -R a-w`. Every `resolveRun002*` wrapper in `apparatus/adapters/`
// derives a root from its OWN `__dirname`, which inside the export is the
// export. For two of the four that is fatal:
//
//   * `git-head.js:163` computes RUN_002_REPO_ROOT from `__dirname` and
//     `resolveRun002TrustedHeadSha` refuses WORKSPACE_MISMATCH unless it
//     equals `config/isolation.json`'s `workspace`. From the export they
//     can never be equal -> HEAD_SHA_UNVERIFIED -> DENIED, always.
//   * `reviewer-identity.js:252` derives RUNTIME_DIR from `__dirname` the
//     same way, and `resolveRun002ReviewerIdentity` reads a ledger there.
//     `.runtime/` is gitignored, so a fresh worktree has none
//     -> LEDGER_UNREADABLE -> REVIEWER_UNVERIFIED -> DENIED, always.
//
// Neither can be patched in place under `chmod -R a-w`. So this program
// calls the LOW-LEVEL, repo-agnostic forms instead, driven by the
// `liveRepoRoot` the invoker hands it:
//
//     resolveTrustedHeadSha(identity, { repoRoot: liveRepoRoot })
//     loadReviewerEvidence(<liveRepoRoot>/<runtime_dir>, prNumber)
//       + resolveReviewerIdentity(claim, { ledgerEvents, prRecord })
//
// The two Run002 wrappers above appear nowhere in this file's code, and a
// test asserts that over the source text.
//
// AND THE OTHER TWO ARE DELIBERATELY LEFT ALONE. `task-record.js` and
// `ci-result.js` derive roots that feed only CONFIG — `config/tasks.json`
// and `config/experiment.json` — and the pinned export's copies are the
// TRUSTED ones. `resolveRun002TaskRecord` and `resolveRun002CiResult` are
// therefore the correct calls, and "fixing" them to read from the live
// checkout would let a pull request edit the task graph and the required-
// check list that judge it. The split is the point; do not collapse it.
//
// `config/isolation.json` is read from the export too, for ONE field:
// `runtime_dir`, the NAME of the ledger directory. Its `workspace` field
// is deliberately not read — that comparison IS the trap.
//
// THE LEDGER IS THE THIRD INSTANCE OF THE SAME TRAP, and the easiest to
// miss because no `resolveRun002*` wrapper is involved. `live-gate.js`
// consumes `request.ledgerEvents` WHOLE from its caller and never derives
// it, so this file is what decides where the ledger is read from. Read
// from `__dirname/../../<runtime_dir>` — the natural thing to write — it
// is empty in any export, `verifyReviewProvenance` reports
// REVIEW_PROVENANCE_UNREADABLE, and every pull request is denied forever
// under a third reason code. It is therefore loaded from
// `<liveRepoRoot>/<runtime_dir>`, and `request.ledgerEvents` is ignored
// if a caller supplies one: the gate reads its own provenance.
//
// BRANCH IDENTITIES ONLY. `git-head.js` also accepts
// `{ kind: 'worktree', path }`, and that form is NOT viable under the
// approved arrangement — it is accepted by the adapter, not supported
// here. `config/isolation.json` declares `worktree_dir: ".worktrees"`
// while `control/workers.py` actually creates `<parent>/<name>__worktrees`,
// and neither directory exists, so `loadWorktreeDir` fails
// NO_ISOLATION_CONFIG before any path check runs. Independently, approval
// package §6 gives a worker's worktree mode 0700 owned by `run002-wrk`
// while the gate runs as `run002-sup`, which could not traverse it even
// if the configuration agreed. Callers pass
// `{ kind: 'branch', ref: 'run-002/...' }`. Nothing here special-cases
// either form — a worktree identity simply fails closed, as it should.
//
// ===================================================================
// NO FETCHER REACHES THE NETWORK, AND THERE IS NO DEFAULT TO REACH.
// ===================================================================
// `ci-result.js` and `reviewer-identity.js` leave their external edges
// required and undefaulted so no test can reach GitHub by forgetting
// something. That property is preserved here, and strengthened: this file
// constructs NO `gh`-invoking fetcher at all.
//
// `resolveRun002CiResult` is always called WITH an explicit
// `fetchCheckRuns`, so its own `ghCheckRuns` default is unreachable from
// here. `runGate` requires `services.fetchCheckRuns` and refuses
// GATE_CLI_NO_CI_SOURCE without it. `main` builds one — a pure function
// over `request.checkRuns`, the check-run observation the CALLER already
// took, exactly as `request.prView` is the `gh pr view` observation the
// caller already took (live-gate.js's own contract). An absent
// observation is GATE_CLI_CI_OBSERVATION_MISSING, never a fetch: a caller
// who supplied no observation has made a malformed request, not observed
// a red build, and inventing the difference is how a gate lies.
//
// A deployer who wants the gate to fetch for itself supplies the function
// at the `runGate` seam, exactly as `publication_path.py` takes `poster`.
// Supplying one is deployment, and deployment is unapproved.
//
// ===================================================================
// IT DOES NOT JUDGE, AND IT DOES NOT RECOMPUTE.
// ===================================================================
// The decision is `JSON.stringify`'d exactly as live-gate.js returned it.
// No field is picked, renamed, defaulted or derived. In particular
// `blockedOnlyByPendingIndependentReview` is passed through untouched:
// live-gate.js computes it from the reason list PLUS the CI adapter's own
// `ok`, and its `F5 MUTATION` test shows the obvious alternative —
// reading `mergeStateStatus === 'BLOCKED'` — would publish an
// independent-review pass for a pull request whose CI concluded failure.
// Only the SHAPE is checked, at the boundary, against the same predicate
// `gate_invoker.decision_is_usable` applies; a DENIED decision and an
// ELIGIBLE one are equally complete.
//
// IT PUBLISHES NOTHING. It has no transport, makes no network call, and
// references neither `control/publisher.py` nor
// `experiment/github-app/publication_path.py`. It computes and prints.
//
// FAIL CLOSED, FINITELY, WITHOUT RAISING. Every way this can go wrong has
// its own token in OUTCOMES, is printed as a refusal object that is NOT
// decision-shaped, and exits non-zero. There is no permissive branch and
// no partial decision that could pass for a complete one.

'use strict';

const fs = require('fs');
const path = require('path');

const { evaluateLiveMergeEligibility } = require('./live-gate.js');
// LOW-LEVEL forms only. See THE RULE above.
const { resolveTrustedHeadSha } = require('../adapters/git-head.js');
const {
  loadReviewerEvidence,
  resolveReviewerIdentity,
} = require('../adapters/reviewer-identity.js');
// CONFIG-ONLY roots, correctly read from the pinned export.
const { resolveRun002CiResult } = require('../adapters/ci-result.js');
const { resolveRun002TaskRecord } = require('../adapters/task-record.js');

// This file's own location, two levels up: the EXPORT root. Used for
// config and for nothing else — never as a source of git facts.
const EXPORT_ROOT = path.resolve(__dirname, '..', '..');

// The envelope keys, which are `gate_invoker.LIVE_ROOT_KEY` and
// `gate_invoker.REQUEST_KEY`. Spelled here because a program in an
// immutable export cannot import from the Python half; a test asserts the
// two spellings agree.
const LIVE_ROOT_KEY = 'liveRepoRoot';
const REQUEST_KEY = 'request';

// Finite outcomes. Exactly one of these describes any run.
const OK = 'GATE_CLI_OK';
const STDIN_UNREADABLE = 'GATE_CLI_STDIN_UNREADABLE';
const ENVELOPE_UNPARSEABLE = 'GATE_CLI_ENVELOPE_UNPARSEABLE';
const ENVELOPE_MALFORMED = 'GATE_CLI_ENVELOPE_MALFORMED';
const LIVE_ROOT_INVALID = 'GATE_CLI_LIVE_ROOT_INVALID';
const PINNED_CONFIG_UNREADABLE = 'GATE_CLI_PINNED_CONFIG_UNREADABLE';
const CI_OBSERVATION_MISSING = 'GATE_CLI_CI_OBSERVATION_MISSING';
const NO_CI_SOURCE = 'GATE_CLI_NO_CI_SOURCE';
const EVALUATION_FAILED = 'GATE_CLI_EVALUATION_FAILED';
const DECISION_INCOMPLETE = 'GATE_CLI_DECISION_INCOMPLETE';
const DECISION_UNSERIALISABLE = 'GATE_CLI_DECISION_UNSERIALISABLE';

const OUTCOMES = [
  OK, STDIN_UNREADABLE, ENVELOPE_UNPARSEABLE, ENVELOPE_MALFORMED,
  LIVE_ROOT_INVALID, PINNED_CONFIG_UNREADABLE, CI_OBSERVATION_MISSING,
  NO_CI_SOURCE, EVALUATION_FAILED, DECISION_INCOMPLETE,
  DECISION_UNSERIALISABLE,
];

// One exit code for every refusal, on purpose. `gate_invoker` collapses
// every non-zero exit into GATE_RUN_FAILED, so a code carries no
// information the Python half can read; the token on stdout is where the
// distinction lives, for the operator and the log.
const REFUSED_EXIT = 1;

const SHA_RE = /^[0-9a-f]{40}$/;
const VERDICTS = new Set(['ELIGIBLE', 'DENIED']);

function refuse(outcome, detail) {
  // Deliberately NOT decision-shaped: it carries no `decision` key, so a
  // reader that ignored the exit code and parsed this anyway would get
  // GATE_DECISION_MALFORMED from `gate_invoker`, never a verdict.
  return { ok: false, outcome: outcome, detail: detail };
}

/**
 * Whether a decision carries every field the publisher reads.
 *
 * SHAPE ONLY, and a deliberate mirror of
 * `control/gate_invoker.py::decision_is_usable`. Nothing here re-decides
 * anything. If the two ever drift, this one refusing is still fail-closed
 * — a refusal exits non-zero and publishes nothing — but they are meant
 * to agree and a test asserts the field list.
 *
 * `trustedHeadSha` may be null: live-gate.js returns null when the task
 * identity resolved to no commit, and that is a decision it DID make.
 */
function decisionIsComplete(decision) {
  if (!decision || typeof decision !== 'object' || Array.isArray(decision)) return false;
  if (!VERDICTS.has(decision.decision)) return false;
  const flag = decision.blockedOnlyByPendingIndependentReview;
  // A real boolean. JSON can carry the string "true", and a downstream
  // `if (flag)` would read that as a pass.
  if (flag !== true && flag !== false) return false;
  if (!Array.isArray(decision.reasons)) return false;
  if (!Object.prototype.hasOwnProperty.call(decision, 'trustedHeadSha')) return false;
  const trusted = decision.trustedHeadSha;
  return trusted === null || (typeof trusted === 'string' && SHA_RE.test(trusted));
}

/**
 * The envelope's two parts, or null if it is not an envelope at all.
 *
 * Shared by `main` and `runGate` so one malformed envelope cannot be
 * described by two different tokens depending on which found it first.
 */
function envelopeParts(envelope) {
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) return null;
  const request = envelope[REQUEST_KEY];
  if (!request || typeof request !== 'object' || Array.isArray(request)) return null;
  return { request: request, liveRepoRoot: envelope[LIVE_ROOT_KEY] };
}

/**
 * The NAME of the runtime directory, from the PINNED config.
 *
 * The name is config and comes from the export; the root it is joined to
 * is a fact and comes from the envelope. `isolation.workspace` is
 * deliberately not read: comparing it to this module's own location is
 * THE INVOKER TRAP.
 */
function pinnedRuntimeDirName(exportRoot) {
  let isolation;
  try {
    isolation = JSON.parse(
      fs.readFileSync(path.join(exportRoot, 'config', 'isolation.json'), 'utf8')
    );
  } catch (err) {
    return null;
  }
  const name = isolation && isolation.runtime_dir;
  if (typeof name !== 'string' || name.trim().length === 0) return null;
  return name;
}

/**
 * Judge one pull request. NEVER RAISES.
 *
 * `services.fetchCheckRuns(repo, headSha)` is REQUIRED and undefaulted —
 * it is the one edge that could reach GitHub, so nothing here invents it.
 *
 * Returns { ok: true, outcome: OK, decision } or a refusal from `refuse`.
 */
function runGate(envelope, services) {
  services = services || {};
  if (typeof services.fetchCheckRuns !== 'function') {
    return refuse(NO_CI_SOURCE, 'services.fetchCheckRuns is required and has no default.');
  }

  const parts = envelopeParts(envelope);
  if (parts === null) {
    return refuse(
      ENVELOPE_MALFORMED,
      'stdin must be an object carrying "' + REQUEST_KEY + '" as an object.'
    );
  }

  const liveRepoRoot = parts.liveRepoRoot;
  if (typeof liveRepoRoot !== 'string' || liveRepoRoot.trim().length === 0
      || !path.isAbsolute(liveRepoRoot)) {
    return refuse(
      LIVE_ROOT_INVALID,
      '"' + LIVE_ROOT_KEY + '" must be a non-empty absolute path; git facts exist nowhere else.'
    );
  }

  const runtimeName = pinnedRuntimeDirName(EXPORT_ROOT);
  if (runtimeName === null) {
    return refuse(
      PINNED_CONFIG_UNREADABLE,
      "the export's config/isolation.json carries no usable runtime_dir."
    );
  }
  const runtimeDir = path.join(liveRepoRoot, runtimeName);

  const request = parts.request;
  let decision;
  try {
    // THE LEDGER IS A FACT, so it is loaded from the LIVE checkout and
    // never taken from the envelope. A `request.ledgerEvents` a caller
    // supplied is dropped on the floor by the explicit construction
    // below: the gate reads its own provenance or reads none.
    const loaded = loadReviewerEvidence(runtimeDir, request.prNumber);

    decision = evaluateLiveMergeEligibility(
      {
        identity: request.identity,
        prNumber: request.prNumber,
        pkg: request.pkg,
        prView: request.prView,
        // null when the ledger could not be read in full, which
        // live-gate reports as REVIEW_PROVENANCE_UNREADABLE. An
        // unreadable ledger is an absence of evidence, never a pass.
        ledgerEvents: loaded.ok === true ? loaded.ledgerEvents : null,
      },
      {
        // FACT, from the live checkout. The low-level form.
        resolveHeadSha: (identity) =>
          resolveTrustedHeadSha(identity, { repoRoot: liveRepoRoot }),
        // CONFIG, from the export — plus an explicitly injected fetcher,
        // so `ci-result.js`'s own `gh` default is unreachable from here.
        resolveCi: (headSha) =>
          resolveRun002CiResult(headSha, { fetchCheckRuns: services.fetchCheckRuns }),
        // CONFIG, from the export: the pinned task graph.
        resolveTask: (taskId) => resolveRun002TaskRecord(taskId),
        // FACT, from the live checkout. The low-level form. A failed load
        // is handed on as the adapter failure it already is, so
        // live-gate reports REVIEWER_UNVERIFIED with its determination
        // rather than this file inventing one.
        resolveReviewer: (claim) =>
          loaded.ok === true
            ? resolveReviewerIdentity(claim, {
              ledgerEvents: loaded.ledgerEvents,
              prRecord: loaded.prRecord,
            })
            : loaded,
      }
    );
  } catch (err) {
    // An adapter or the composition raised, so no decision exists.
    // Printing a DENIED here would assert a judgement that was never
    // made; `publication_path.py` draws the same distinction.
    return refuse(
      EVALUATION_FAILED,
      'the gate raised while judging and reached no decision.'
    );
  }

  // DEFENSIVE DEPTH, AND SAY SO. No input can drive this branch today:
  // live-gate.js always returns all four fields in their declared types,
  // so `decisionIsComplete` cannot be made to fail from outside. A
  // mutation that short-circuits this `if` therefore turns no test red,
  // and that was measured rather than assumed. It is kept because
  // DECISION_INCOMPLETE is a required finite outcome and because a future
  // change to live-gate.js must not become a decision the publisher acts
  // on; the predicate itself, and the fact that runGate consults it, are
  // both mutation-proven.
  if (!decisionIsComplete(decision)) {
    return refuse(
      DECISION_INCOMPLETE,
      'the gate returned no complete decision, so there is nothing to publish.'
    );
  }
  return { ok: true, outcome: OK, decision: decision };
}

/**
 * One run, from raw stdin to what should be written and exited with.
 *
 * Pure: touches no stream and no process state, so every path is
 * testable without spawning anything. NEVER RAISES.
 */
function main(raw) {
  try {
    let envelope;
    try {
      envelope = JSON.parse(raw);
    } catch (err) {
      return emit(refuse(ENVELOPE_UNPARSEABLE, 'stdin did not parse as JSON.'));
    }

    const parts = envelopeParts(envelope);
    if (parts === null) {
      return emit(refuse(
        ENVELOPE_MALFORMED,
        'stdin must be an object carrying "' + REQUEST_KEY + '" as an object.'
      ));
    }

    // The caller's own check-run observation, wrapped as the fetcher
    // `ci-result.js` requires. Pure; it reaches nothing.
    const observed = parts.request.checkRuns;
    if (!Array.isArray(observed)) {
      return emit(refuse(
        CI_OBSERVATION_MISSING,
        'request.checkRuns must be the caller\'s observed check-run array; '
        + 'this gate never fetches one for itself.'
      ));
    }
    const fetchCheckRuns = function fetchObservedCheckRuns() {
      return { check_runs: observed };
    };

    return emit(runGate(envelope, { fetchCheckRuns: fetchCheckRuns }));
  } catch (err) {
    return emit(refuse(EVALUATION_FAILED, 'the gate raised and reached no decision.'));
  }
}

/** Render one result as the bytes to print and the code to exit with. */
function emit(result) {
  if (result.ok === true) {
    let body;
    try {
      // VERBATIM. The object live-gate.js returned, nothing added or
      // removed. See IT DOES NOT JUDGE above.
      body = JSON.stringify(result.decision);
    } catch (err) {
      return emit(refuse(
        DECISION_UNSERIALISABLE,
        'the decision could not be serialised, so none was emitted.'
      ));
    }
    return { exitCode: 0, outcome: OK, stdout: body };
  }
  return {
    exitCode: REFUSED_EXIT,
    outcome: result.outcome,
    stdout: JSON.stringify(result),
  };
}

/**
 * Collect a readable stream as UTF-8 text.
 *
 * Separated out, and exported, for the same reason
 * `gate_invoker.run_node` is: it is the one link that touches the outside
 * world, and an untested one is the link that fails on launch day.
 * `done(null, text)` on success, `done(err)` on a read fault.
 */
function readAll(stream, done) {
  let raw = '';
  let finished = false;
  const once = (err, text) => {
    if (finished) return;
    finished = true;
    done(err, text);
  };
  stream.setEncoding('utf8');
  stream.on('data', (chunk) => { raw += chunk; });
  stream.on('error', (err) => once(err));
  stream.on('end', () => once(null, raw));
}

if (require.main === module) {
  readAll(process.stdin, (err, raw) => {
    const result = err
      ? emit(refuse(STDIN_UNREADABLE, 'the envelope could not be read from stdin.'))
      : main(raw);
    process.stdout.write(result.stdout);
    // `process.exitCode`, never `process.exit()`: exiting outright can
    // truncate a stdout write that has not drained, and a half-written
    // decision parses as nothing.
    process.exitCode = result.exitCode;
  });
}

module.exports = {
  runGate: runGate,
  main: main,
  readAll: readAll,
  decisionIsComplete: decisionIsComplete,
  envelopeParts: envelopeParts,
  LIVE_ROOT_KEY: LIVE_ROOT_KEY,
  REQUEST_KEY: REQUEST_KEY,
  OUTCOMES: OUTCOMES,
  OK: OK,
  STDIN_UNREADABLE: STDIN_UNREADABLE,
  ENVELOPE_UNPARSEABLE: ENVELOPE_UNPARSEABLE,
  ENVELOPE_MALFORMED: ENVELOPE_MALFORMED,
  LIVE_ROOT_INVALID: LIVE_ROOT_INVALID,
  PINNED_CONFIG_UNREADABLE: PINNED_CONFIG_UNREADABLE,
  CI_OBSERVATION_MISSING: CI_OBSERVATION_MISSING,
  NO_CI_SOURCE: NO_CI_SOURCE,
  EVALUATION_FAILED: EVALUATION_FAILED,
  DECISION_INCOMPLETE: DECISION_INCOMPLETE,
  DECISION_UNSERIALISABLE: DECISION_UNSERIALISABLE,
  REFUSED_EXIT: REFUSED_EXIT,
};
