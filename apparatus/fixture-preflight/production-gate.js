// THE SEAM, CLOSED. Drives the PRODUCTION merge-eligibility composition
// from the C-04a fixture world.
//
// ===================== WHY THIS FILE EXISTS =====================
// merge-eligibility.js in this directory is a STUB. Handover section 35.8
// named the consequence as "the single largest gap": the decision the
// fixture harness proves correct was made by a stand-in, not by shipping
// code, so what was proven was the SCENARIO, not the SYSTEM.
//
// apparatus/pr-evidence/live-gate.js (C-04, handover section 34) is the
// real composition layer. This module adapts the fixture world to it, so
// the same two scenarios can be re-decided by production code.
//
// ===================== WHAT IS REAL HERE =====================
// All four C-04 adapters are the REAL ones, and so is the composition:
//
//   evaluateLiveMergeEligibility  apparatus/pr-evidence/live-gate.js
//   resolveTrustedHeadSha         apparatus/adapters/git-head.js
//   resolveCiResult               apparatus/adapters/ci-result.js
//   resolveTaskRecord             apparatus/adapters/task-record.js
//   resolveReviewerIdentity       apparatus/adapters/reviewer-identity.js
//
// and through live-gate, checkOfflinePolicy -> Ajv over the real schema
// -> the real severity floor -> the real requirement registry.
//
// ===================== WHAT IS INJECTED =====================
// Only the EXTERNAL SERVICES, which is exactly the shape live-gate was
// built for: all four adapters arrive as injected functions and nothing
// is defaulted to a real entrypoint, so this cannot reach the network,
// `gh`, or the live .runtime/ even by accident.
//
//   git            NOT injected - a real git binary on a real throwaway repo
//   GitHub         injected - the in-memory fake forge (fake-github.js)
//   the ledger     injected - synthesised from the fake forge's review
//                  transcript, in the exact shape section 34.2 agreed
//   durable state  injected - a PR record carrying reviewed_head
//   the task graph NOT injected - this repository's real config/tasks.json
//
// No live product action occurs: no real PR, no merge on a real forge, no
// paid call, no worker, no notification.
//
// ===================== THE LEDGER IS SYNTHESISED, AND WHY THAT IS HONEST
// In production the Supervisor writes REVIEW_DISPATCHED and REVIEW_RESULT.
// In this fixture the scenario driver IS the Supervisor, so it is the
// thing that must produce them. They are built from the forge's recorded
// reviews rather than from the evidence package, so the package can still
// be caught lying: a package claiming a verdict or a SHA the ledger does
// not carry is refused by the real adapter, and a test holds that.

'use strict';

const path = require('path');
const { evaluateLiveMergeEligibility } = require('../pr-evidence/live-gate.js');
const { resolveTrustedHeadSha } = require('../adapters/git-head.js');
const { resolveCiResult } = require('../adapters/ci-result.js');
const { resolveTaskRecord } = require('../adapters/task-record.js');
const { resolveReviewerIdentity } = require('../adapters/reviewer-identity.js');

// The fixture forge's check name, and the producer/provider Protocol v2
// names for the one independent code-review role.
const FIXTURE_CHECK = 'fixture-ci';
const FIXTURE_REPO = 'fixture/run-002-preflight';
const CODEX_PRODUCER = 'Codex Reviewer';
const CODEX_PROVIDER = 'codex';

// The task graph is THIS repository's real committed config/tasks.json,
// not the throwaway fixture repo's — the fixture repo has no task graph,
// and inventing one would make the dependency step prove nothing.
// resolveTaskRecord requires repoRoot explicitly and defaults nothing, so
// this must be stated.
const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');

// Translates the fake forge's PASS/FAIL into GitHub's two-field shape.
// GitHub reports a check run as (status, conclusion), and ci-result.js
// requires status 'completed' AND conclusion 'success' - an allow-list of
// exactly one conclusion. Mapping FAIL to 'failure' rather than to a
// missing run keeps the distinction the adapter cares about.
function asCheckRun(entry) {
  return {
    name: entry.name,
    head_sha: entry.sha,
    status: 'completed',
    conclusion: entry.status === 'PASS' ? 'success' : 'failure',
  };
}

// The ledger the Supervisor would have written for the reviews this forge
// actually recorded. Section 34.2's contract: BOTH events carry the full
// 40-hex head SHA at metadata_redacted.head_sha, and the REVIEW_RESULT
// names the reviewer worker that produced it.
//
// The worker name shape is reviewer-identity.js's:
// "<task-id lowercased>-review-<cycle>". A builder/fixer event is
// deliberately NOT emitted under that same name, because the adapter
// refuses a worker that also acted as builder or fixer as SELF_REVIEW -
// and that check only means something if the fixture could have tripped it.
function synthesiseLedger(taskId, prNumber, reviews) {
  const events = [];
  for (const review of reviews) {
    const worker = taskId.toLowerCase() + '-review-' + review.cycle;
    const common = {
      task_id: taskId,
      pr_id: prNumber,
      role: 'reviewer',
      agent_id: worker,
      provider: CODEX_PROVIDER,
      metadata_redacted: { head_sha: review.sha },
    };
    events.push(Object.assign({ event_type: 'REVIEW_DISPATCHED', outcome: 'DISPATCHED' }, common));
    events.push(Object.assign({ event_type: 'REVIEW_RESULT', outcome: review.verdict }, common));
  }
  return events;
}

// The durable PR record the reviewer adapter reads reviewed_head from.
// The LAST review wins, exactly as control/supervisor.py overwrites
// prs[n].reviewed_head on each dispatch - which is why section 34.2 calls
// this the weaker of the two SHA sources and checks the ledger as well.
function prRecordFor(reviews) {
  const last = reviews[reviews.length - 1];
  return { reviewed_head: last ? last.sha : null };
}

// Builds a decide(evidence, prNumber) compatible with the stub's
// signature, so scenario.js can be handed either one.
//
// mergeStateStatus is a FIXTURE INPUT with no default guess hidden in it.
// GitHub computes it from branch protection, which this fake forge does
// not model. Defaulting it to 'CLEAN' would quietly assert that branch
// protection is satisfied - which for Run 002 today it is NOT (audit row
// C-20(b): main requires an approving review nothing in the control plane
// produces). Callers state it, and a test drives 'BLOCKED' precisely so
// that real condition is exercised rather than papered over.
function makeProductionDecider(options) {
  const repo = options.repo;
  const gh = options.gh;
  const taskId = options.taskId || 'TASK-001';
  const repoRoot = options.repoRoot || repo.root;
  const taskGraphRoot = options.taskGraphRoot || RUN_002_REPO_ROOT;
  const requiredChecks = options.requiredChecks || [FIXTURE_CHECK];
  const mergeStateStatus = options.mergeStateStatus || 'CLEAN';
  const mergeable = options.mergeable || 'MERGEABLE';

  return function decide(evidence, prNumber, identity) {
    const pr = gh.getPullRequest(prNumber);

    // Scoped to the commit under test, exactly as the real endpoint is
    // (`repos/<repo>/commits/<sha>/check-runs`). Without the filter the
    // adapter would see an earlier cycle's run and correctly refuse with
    // CI_SHA_MISMATCH - a fixture artefact, not a real finding.
    function fetchCheckRuns(_repo, headSha) {
      return {
        check_runs: pr.checkRuns.filter((c) => c.sha === headSha).map(asCheckRun),
      };
    }

    const ledgerEvents = synthesiseLedger(taskId, prNumber, pr.reviews);
    const prRecord = prRecordFor(pr.reviews);

    const result = evaluateLiveMergeEligibility(
      {
        identity: identity,
        prNumber: prNumber,
        pkg: evidence,
        prView: {
          state: pr.state,
          isDraft: pr.draft,
          headRefOid: pr.headSha,
          mergeable: mergeable,
          mergeStateStatus: mergeStateStatus,
        },
        ledgerEvents: ledgerEvents,
      },
      {
        resolveHeadSha: (id) => resolveTrustedHeadSha(id, { repoRoot: repoRoot }),
        resolveCi: (headSha) =>
          resolveCiResult(headSha, {
            repo: FIXTURE_REPO,
            requiredChecks: requiredChecks,
            fetchCheckRuns: fetchCheckRuns,
          }),
        resolveTask: (id) => resolveTaskRecord(id, { repoRoot: taskGraphRoot }),
        resolveReviewer: (claim) =>
          resolveReviewerIdentity(claim, {
            ledgerEvents: ledgerEvents,
            prRecord: prRecord,
          }),
      }
    );

    // The stub's shape, so a caller can be handed either decider, plus
    // the full production result for assertions the stub could not make.
    // `reasons` is the production CODE list, untranslated: mapping
    // production codes back onto the stub's vocabulary would hide what
    // the real gate actually says, which is the whole point of the swap.
    return {
      eligible: result.decision === 'ELIGIBLE',
      reasons: result.reasons.map((r) => r.code),
      trustedHeadSha: result.trustedHeadSha,
      live: result,
    };
  };
}

module.exports = {
  makeProductionDecider,
  synthesiseLedger,
  prRecordFor,
  asCheckRun,
  FIXTURE_CHECK,
  FIXTURE_REPO,
  CODEX_PRODUCER,
  CODEX_PROVIDER,
};
