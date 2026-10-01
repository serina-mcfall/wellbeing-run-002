// Live CI-result adapter (C-04 live-gate adapter, one of four).
//
// apparatus/pr-evidence/validate.js's CI_NOT_PASS check reads pkg.ci.status
// — a field the submitting agent wrote about its own work. That is a claim,
// not evidence. This adapter answers the different question: did a CI run
// that GitHub itself reports actually complete successfully for EXACTLY this
// 40-character head SHA?
//
// It judges only the checks config/experiment.json's github.required_checks
// governs — the one place a human declares which checks constitute "CI" for
// Run 002. A required check that GitHub never ran for this commit is a
// failure, not an absence to shrug at: that is the hole a zero-check or
// wrong-commit CI report would otherwise walk straight through.
//
// EVERY outcome is one of three, never a permissive default:
//   { ok: true, ... }                             CI verifiably passed
//   { ok: false, determination: 'VERIFIED_FALSE' } CI verifiably did not pass
//   { ok: false, determination: 'COULD_NOT_VERIFY' } the fact is unestablished
// The last is NOT weaker evidence of passing. A caller that treats
// COULD_NOT_VERIFY as healthy has reintroduced the defect this adapter exists
// to remove.
//
// resolveCiResult(headSha, { repo, requiredChecks, fetchCheckRuns }) is the
// low-level function. fetchCheckRuns is REQUIRED and not defaulted, so no
// network call can happen unless a caller explicitly supplies one — unit
// tests inject a stub and never reach GitHub.
//
// resolveRun002CiResult(headSha, options) is the real entrypoint: it reads
// github.repo and github.required_checks from this module's own repository's
// config/experiment.json and, unless options.fetchCheckRuns overrides it,
// calls `gh api` for real. Tests must always override it.

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const SHA_RE = /^[0-9a-f]{40}$/;

// GitHub check-run status values: queued, waiting, requested, pending,
// in_progress, completed. Only a completed run has reached a conclusion.
const COMPLETED_STATUS = 'completed';

// An ALLOW-list of conclusions, not a deny-list of failures. GitHub's
// "neutral", "skipped" and "stale" conclusions all mean the check did not
// actually verify this commit, so none of them may stand in for a pass.
const PASSING_CONCLUSIONS = new Set(['success']);

const VERIFIED_FALSE = 'VERIFIED_FALSE';
const COULD_NOT_VERIFY = 'COULD_NOT_VERIFY';

function fail(reason, determination, detail) {
  return { ok: false, reason: reason, determination: determination, detail: detail };
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function lower(value) {
  return typeof value === 'string' ? value.toLowerCase() : '';
}

// Only the first page is requested. More than 100 check runs on one commit
// would leave a required check unseen, which this adapter reports as
// REQUIRED_CHECK_MISSING — a fail-closed outcome, never a false pass.
function ghCheckRuns(repo, headSha) {
  const output = execFileSync(
    'gh',
    ['api', 'repos/' + repo + '/commits/' + headSha + '/check-runs?per_page=100'],
    { encoding: 'utf8' }
  );
  return JSON.parse(output);
}

function resolveCiResult(headSha, options) {
  options = options || {};
  const repo = options.repo;
  const requiredChecks = options.requiredChecks;
  const fetchCheckRuns = options.fetchCheckRuns;

  if (!isNonEmptyString(repo)) {
    return fail('INVALID_OPTIONS', COULD_NOT_VERIFY, 'options.repo is required.');
  }
  if (!Array.isArray(requiredChecks) || requiredChecks.length === 0 || !requiredChecks.every(isNonEmptyString)) {
    return fail(
      'INVALID_OPTIONS',
      COULD_NOT_VERIFY,
      'options.requiredChecks must be a non-empty array of non-empty check names.'
    );
  }
  if (typeof fetchCheckRuns !== 'function') {
    return fail('INVALID_OPTIONS', COULD_NOT_VERIFY, 'options.fetchCheckRuns is required.');
  }
  if (typeof headSha !== 'string' || !SHA_RE.test(headSha)) {
    return fail(
      'MALFORMED_SHA',
      COULD_NOT_VERIFY,
      'headSha must be a full 40-character lowercase-hex git SHA.'
    );
  }

  let payload;
  try {
    payload = fetchCheckRuns(repo, headSha);
  } catch (err) {
    return fail('CI_FETCH_FAILED', COULD_NOT_VERIFY, 'the check-run source could not be read.');
  }

  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.check_runs)) {
    return fail(
      'MALFORMED_CI_RESPONSE',
      COULD_NOT_VERIFY,
      'the check-run source did not return an object with a check_runs array.'
    );
  }

  const runs = [];
  for (const raw of payload.check_runs) {
    if (!raw || typeof raw !== 'object' || !isNonEmptyString(raw.name) || !isNonEmptyString(raw.status)) {
      return fail(
        'MALFORMED_CI_RESPONSE',
        COULD_NOT_VERIFY,
        'a check run is missing a usable name or status.'
      );
    }
    if (typeof raw.head_sha !== 'string' || !SHA_RE.test(raw.head_sha)) {
      return fail(
        'MALFORMED_CI_RESPONSE',
        COULD_NOT_VERIFY,
        'check run "' + raw.name + '" does not carry a full 40-character head_sha.'
      );
    }
    if (raw.head_sha !== headSha) {
      return fail(
        'CI_SHA_MISMATCH',
        VERIFIED_FALSE,
        'check run "' + raw.name + '" belongs to ' + raw.head_sha + ', not the head under test ' + headSha + '.'
      );
    }
    runs.push({
      name: raw.name,
      status: lower(raw.status),
      conclusion: lower(raw.conclusion),
    });
  }

  if (runs.length === 0) {
    return fail(
      'NO_CHECK_RUNS',
      COULD_NOT_VERIFY,
      'GitHub reports no check runs at all for commit ' + headSha + '.'
    );
  }

  for (const required of requiredChecks) {
    const matching = runs.filter(function (run) {
      return run.name === required;
    });
    if (matching.length === 0) {
      return fail(
        'REQUIRED_CHECK_MISSING',
        VERIFIED_FALSE,
        'required check "' + required + '" did not run for commit ' + headSha + '.'
      );
    }
    for (const run of matching) {
      if (run.status !== COMPLETED_STATUS) {
        return fail(
          'REQUIRED_CHECK_INCOMPLETE',
          COULD_NOT_VERIFY,
          'required check "' + required + '" is "' + run.status + '", so it has reached no conclusion yet.'
        );
      }
      if (!PASSING_CONCLUSIONS.has(run.conclusion)) {
        return fail(
          'REQUIRED_CHECK_NOT_SUCCESS',
          VERIFIED_FALSE,
          'required check "' + required + '" concluded "' + (run.conclusion || 'null') + '".'
        );
      }
    }
  }

  // Checks outside required_checks are reported, never judged: the governed
  // list is the human-declared definition of "CI" for Run 002, and silently
  // widening it here would be a gate change nobody approved. A composition
  // layer that wants to block on them has observedChecks to do it with.
  return {
    ok: true,
    sha: headSha,
    requiredChecks: requiredChecks.slice(),
    observedChecks: runs.map(function (run) {
      return { name: run.name, status: run.status, conclusion: run.conclusion };
    }),
    resolvedFrom: 'check-runs:' + repo + '@' + headSha,
  };
}

const RUN_002_REPO_ROOT = path.resolve(__dirname, '..', '..');

function resolveRun002CiResult(headSha, options) {
  options = options || {};
  let experiment;
  try {
    experiment = JSON.parse(
      fs.readFileSync(path.join(RUN_002_REPO_ROOT, 'config', 'experiment.json'), 'utf8')
    );
  } catch (err) {
    return fail('NO_GITHUB_CONFIG', COULD_NOT_VERIFY, 'could not read config/experiment.json.');
  }
  const github = experiment && experiment.github;
  if (!github || typeof github !== 'object') {
    return fail('NO_GITHUB_CONFIG', COULD_NOT_VERIFY, 'config/experiment.json has no github block.');
  }
  return resolveCiResult(headSha, {
    repo: github.repo,
    requiredChecks: github.required_checks,
    fetchCheckRuns: options.fetchCheckRuns || ghCheckRuns,
  });
}

module.exports = {
  resolveCiResult: resolveCiResult,
  resolveRun002CiResult: resolveRun002CiResult,
  PASSING_CONCLUSIONS: PASSING_CONCLUSIONS,
  VERIFIED_FALSE: VERIFIED_FALSE,
  COULD_NOT_VERIFY: COULD_NOT_VERIFY,
};
