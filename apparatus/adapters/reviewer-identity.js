// Reviewer-identity adapter (C-04 live-gate adapter, one of four).
//
// apparatus/pr-evidence/validate.js checks review.provenance.producer against
// ALLOWED_REVIEW_PRODUCERS = new Set(['Codex Reviewer']). That is a string
// the submitting agent typed about itself. Any worker — a Builder, a Fixer,
// a shell script — can type it. Protocol v2 §"PR contract" is explicit:
// "Agents cannot satisfy independent boxes by self-attestation", and
// §"Anti-cheating" prohibits "Builder/Fixer self-attestation".
//
// This adapter asks the question the name cannot answer: did the deterministic
// Supervisor actually dispatch an independent Codex Reviewer worker for this
// task and PR, and is the verdict in the package the verdict that worker
// actually returned?
//
// Its trusted sources are the two durable Supervisor records, never the
// evidence package:
//   - the append-only ledger (.runtime/ledger.jsonl) — REVIEW_RESULT carries
//     agent_id, role, provider and outcome, written by the Supervisor, not by
//     the reviewer;
//   - the PR record in durable state (.runtime/state.json, doc.prs[n]) —
//     reviewed_head, the SHA the reviewer worker was actually dispatched
//     against (control/supervisor.py::dispatch_reviewer).
//
// LIMITATION — the ledger does not bind a review to a SHA. REVIEW_DISPATCHED
// and REVIEW_RESULT record task, PR, role, provider, agent_id and verdict, but
// no head SHA. The security worker's name is SHA-bound by construction
// (control/routing.py::security_worker_name); the code reviewer's is not — it
// is "<task>-review-<cycle>". So the only SHA binding available is
// prs[n].reviewed_head in MUTABLE state, which the next review cycle
// overwrites. This adapter therefore verifies the CURRENT review cycle only.
// It cannot prove, from durable evidence alone, that an older cycle's
// REVIEW_PASS was not re-presented for a newer commit; it can only prove that
// the Supervisor's current reviewed_head is the SHA being claimed. Closing
// that gap needs a SHA in the review ledger events, or a SHA-bound reviewer
// worker name, neither of which exists today.
//
// EVERY outcome is one of three, never a permissive default:
//   { ok: true, ... }                               identity verified
//   { ok: false, determination: 'VERIFIED_FALSE' }   identity verifiably wrong
//   { ok: false, determination: 'COULD_NOT_VERIFY' } the fact is unestablished
// COULD_NOT_VERIFY is not weak evidence of independence. Treating it as
// healthy reinstates exactly the self-attestation this adapter removes.

const fs = require('fs');
const path = require('path');

const SHA_RE = /^[0-9a-f]{40}$/;

// Protocol v2 §"Agent organisation" names exactly one independent code-review
// role. Kept identical to validate.js's ALLOWED_REVIEW_PRODUCERS on purpose:
// the name check is still necessary, it is simply no longer sufficient.
const CODEX_REVIEWER_PRODUCER = 'Codex Reviewer';

// Must equal config/experiment.json roles.reviewer.provider. A test asserts
// that equality so the two cannot drift apart silently.
const INDEPENDENT_REVIEW_PROVIDER = 'codex';

const REVIEW_ROLE = 'reviewer';
const REVIEW_RESULT_EVENT = 'REVIEW_RESULT';

// Protocol v2 §"Evidence provenance": "Builder/Fixer assertions are not
// independent evidence." A worker that ever acted in either role for this
// task cannot also be its independent reviewer.
const SELF_ATTESTING_ROLES = new Set(['builder', 'fixer']);

const VERIFIED_FALSE = 'VERIFIED_FALSE';
const COULD_NOT_VERIFY = 'COULD_NOT_VERIFY';

function fail(reason, determination, detail) {
  return { ok: false, reason: reason, determination: determination, detail: detail };
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

// control/supervisor.py::dispatch_reviewer builds the reviewer worker name as
// f"{task['id'].lower()}-review-{cycle}". Anything else was not dispatched as
// a reviewer by the Supervisor, whatever its ledger role says.
function reviewerWorkerPattern(taskId) {
  const escaped = taskId.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('^' + escaped + '-review-[0-9]+$');
}

function resolveReviewerIdentity(claim, options) {
  options = options || {};
  const ledgerEvents = options.ledgerEvents;
  const prRecord = options.prRecord;

  if (!Array.isArray(ledgerEvents)) {
    return fail('INVALID_OPTIONS', COULD_NOT_VERIFY, 'options.ledgerEvents must be an array of ledger events.');
  }
  if (!claim || typeof claim !== 'object') {
    return fail('INVALID_CLAIM', COULD_NOT_VERIFY, 'claim must be an object.');
  }
  if (!isNonEmptyString(claim.taskId)) {
    return fail('INVALID_CLAIM', COULD_NOT_VERIFY, 'claim.taskId must be a non-empty string.');
  }
  if (!Number.isInteger(claim.prNumber)) {
    return fail('INVALID_CLAIM', COULD_NOT_VERIFY, 'claim.prNumber must be an integer.');
  }
  if (!isNonEmptyString(claim.verdict)) {
    return fail('INVALID_CLAIM', COULD_NOT_VERIFY, 'claim.verdict must be a non-empty string.');
  }
  if (typeof claim.headSha !== 'string' || !SHA_RE.test(claim.headSha)) {
    return fail('MALFORMED_SHA', COULD_NOT_VERIFY, 'claim.headSha must be a full 40-character lowercase-hex git SHA.');
  }
  if (claim.producer !== CODEX_REVIEWER_PRODUCER) {
    return fail(
      'UNKNOWN_REVIEW_PRODUCER',
      VERIFIED_FALSE,
      'review.provenance.producer is "' + claim.producer + '", not the one independent code-review role Protocol v2 names.'
    );
  }

  // The ledger is append-only and chronological, so the last matching
  // REVIEW_RESULT is the current cycle's verdict.
  let event = null;
  for (const candidate of ledgerEvents) {
    if (!candidate || typeof candidate !== 'object') continue;
    if (candidate.event_type !== REVIEW_RESULT_EVENT) continue;
    if (candidate.task_id !== claim.taskId) continue;
    if (candidate.pr_id !== claim.prNumber) continue;
    if (candidate.role !== REVIEW_ROLE) continue;
    event = candidate;
  }
  if (!event) {
    return fail(
      'NO_REVIEW_EVIDENCE',
      COULD_NOT_VERIFY,
      'the ledger records no REVIEW_RESULT for ' + claim.taskId + ' PR #' + claim.prNumber + '.'
    );
  }

  const workerId = event.agent_id;
  if (!isNonEmptyString(workerId) || !reviewerWorkerPattern(claim.taskId).test(workerId)) {
    return fail(
      'FORGED_REVIEWER_IDENTITY',
      VERIFIED_FALSE,
      'agent_id "' + workerId + '" is not a reviewer worker the Supervisor dispatched for ' + claim.taskId + '.'
    );
  }
  if (event.provider !== INDEPENDENT_REVIEW_PROVIDER) {
    return fail(
      'NOT_INDEPENDENT_PROVIDER',
      VERIFIED_FALSE,
      'the review was produced by provider "' + event.provider + '", not the independent reviewer provider "' + INDEPENDENT_REVIEW_PROVIDER + '".'
    );
  }

  for (const candidate of ledgerEvents) {
    if (!candidate || typeof candidate !== 'object') continue;
    if (candidate.agent_id !== workerId) continue;
    if (SELF_ATTESTING_ROLES.has(candidate.role)) {
      return fail(
        'SELF_REVIEW',
        VERIFIED_FALSE,
        'worker "' + workerId + '" also acted as "' + candidate.role + '", so this review is self-attestation.'
      );
    }
  }

  if (event.outcome !== claim.verdict) {
    return fail(
      'REVIEW_VERDICT_MISMATCH',
      VERIFIED_FALSE,
      'the package claims "' + claim.verdict + '" but the reviewer returned "' + event.outcome + '".'
    );
  }

  if (!prRecord || typeof prRecord !== 'object') {
    return fail(
      'REVIEW_SHA_UNVERIFIABLE',
      COULD_NOT_VERIFY,
      'no durable PR record was available, so the reviewed SHA cannot be established.'
    );
  }
  const reviewedHead = prRecord.reviewed_head;
  if (typeof reviewedHead !== 'string' || !SHA_RE.test(reviewedHead)) {
    return fail(
      'REVIEW_SHA_UNVERIFIABLE',
      COULD_NOT_VERIFY,
      'the PR record carries no usable reviewed_head, so the reviewed SHA cannot be established.'
    );
  }
  if (reviewedHead !== claim.headSha) {
    return fail(
      'REVIEW_SHA_MISMATCH',
      VERIFIED_FALSE,
      'the reviewer was dispatched against ' + reviewedHead + ', not the claimed head ' + claim.headSha + '.'
    );
  }

  return {
    ok: true,
    workerId: workerId,
    provider: event.provider,
    verdict: event.outcome,
    reviewedHead: reviewedHead,
    resolvedFrom: 'ledger:REVIEW_RESULT+state:prs.' + claim.prNumber + '.reviewed_head',
  };
}

// loadReviewerEvidence(runtimeDir, prNumber) is the low-level, directory-
// agnostic loader — runtimeDir is required and NOT defaulted, so a caller must
// say explicitly which runtime it means. It exists to be unit-tested against
// isolated temp fixtures, exactly as git-head.js's resolveTrustedHeadSha is.
//
// A ledger line that will not parse makes the whole file untrustworthy for an
// absence proof: a missing REVIEW_RESULT might be sitting in the line that
// failed. control/ledger.py draws the same distinction with Inspection.complete.
function loadReviewerEvidence(runtimeDir, prNumber) {
  if (!isNonEmptyString(runtimeDir)) {
    return fail('INVALID_OPTIONS', COULD_NOT_VERIFY, 'runtimeDir is required.');
  }

  let ledgerEvents;
  try {
    const raw = fs.readFileSync(path.join(runtimeDir, 'ledger.jsonl'), 'utf8');
    ledgerEvents = [];
    for (const line of raw.split('\n')) {
      if (line.trim().length === 0) continue;
      ledgerEvents.push(JSON.parse(line));
    }
  } catch (err) {
    return fail(
      'LEDGER_UNREADABLE',
      COULD_NOT_VERIFY,
      'could not read every line of the ledger, so no absence in it is proof of anything.'
    );
  }

  // A missing or unreadable state file leaves prRecord null, which
  // resolveReviewerIdentity reports as REVIEW_SHA_UNVERIFIABLE. It is never
  // substituted for, and never treated as "no mismatch, therefore fine".
  let prRecord = null;
  if (Number.isInteger(prNumber)) {
    try {
      const doc = JSON.parse(fs.readFileSync(path.join(runtimeDir, 'state.json'), 'utf8'));
      const prs = doc && doc.prs;
      if (prs && typeof prs === 'object') {
        prRecord = prs[String(prNumber)] || null;
      }
    } catch (err) {
      prRecord = null;
    }
  }

  return { ok: true, ledgerEvents: ledgerEvents, prRecord: prRecord };
}

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');
const RUNTIME_DIR = path.join(RUN_002_REPO_ROOT, '.runtime');

// The real entrypoint. It derives the runtime directory from this module's own
// file location, never from a caller-supplied value, so no caller can point it
// at a different experiment's ledger.
function resolveRun002ReviewerIdentity(claim) {
  const prNumber = claim && typeof claim === 'object' ? claim.prNumber : undefined;
  const loaded = loadReviewerEvidence(RUNTIME_DIR, prNumber);
  if (!loaded.ok) {
    return loaded;
  }
  return resolveReviewerIdentity(claim, {
    ledgerEvents: loaded.ledgerEvents,
    prRecord: loaded.prRecord,
  });
}

module.exports = {
  resolveReviewerIdentity: resolveReviewerIdentity,
  loadReviewerEvidence: loadReviewerEvidence,
  resolveRun002ReviewerIdentity: resolveRun002ReviewerIdentity,
  CODEX_REVIEWER_PRODUCER: CODEX_REVIEWER_PRODUCER,
  INDEPENDENT_REVIEW_PROVIDER: INDEPENDENT_REVIEW_PROVIDER,
  VERIFIED_FALSE: VERIFIED_FALSE,
  COULD_NOT_VERIFY: COULD_NOT_VERIFY,
};
