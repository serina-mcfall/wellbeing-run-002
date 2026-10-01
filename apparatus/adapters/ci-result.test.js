// NO NETWORK. Every test injects a stub fetchCheckRuns; the real `gh api`
// path is never executed here. The check-run payloads below are synthetic
// GitHub-shaped fixtures, not live proof of any CI run.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { resolveCiResult, resolveRun002CiResult } = require('./ci-result.js');

const SHA = 'a'.repeat(40);
const OTHER_SHA = 'b'.repeat(40);
const REPO = 'example/fixture-repo';
const REQUIRED = ['ci'];

function run(name, overrides) {
  return Object.assign({ name: name, status: 'completed', conclusion: 'success', head_sha: SHA }, overrides);
}

function stub(checkRuns) {
  return function () {
    return { total_count: checkRuns.length, check_runs: checkRuns };
  };
}

function resolve(checkRuns, options) {
  return resolveCiResult(
    SHA,
    Object.assign({ repo: REPO, requiredChecks: REQUIRED, fetchCheckRuns: stub(checkRuns) }, options)
  );
}

test('positive: every required check completed successfully for this exact SHA', () => {
  const result = resolve([run('ci')]);
  assert.equal(result.ok, true);
  assert.equal(result.sha, SHA);
  assert.deepEqual(result.requiredChecks, ['ci']);
});

test('a check run belonging to a different commit is VERIFIED_FALSE, not ignored', () => {
  const result = resolve([run('ci'), run('lint', { head_sha: OTHER_SHA })]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'CI_SHA_MISMATCH');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('a successful run for the PREVIOUS commit cannot pass the current one', () => {
  const stale = resolveCiResult(OTHER_SHA, {
    repo: REPO,
    requiredChecks: REQUIRED,
    fetchCheckRuns: stub([run('ci')]), // head_sha is SHA, not OTHER_SHA
  });
  assert.equal(stale.ok, false);
  assert.equal(stale.reason, 'CI_SHA_MISMATCH');
});

test('no check runs at all is COULD_NOT_VERIFY, never a pass', () => {
  const result = resolve([]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'NO_CHECK_RUNS');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

test('a required check that never ran is VERIFIED_FALSE even when other checks passed', () => {
  const result = resolve([run('some-other-check')]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_MISSING');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

test('a required check still running is COULD_NOT_VERIFY, not a failure and not a pass', () => {
  const result = resolve([run('ci', { status: 'in_progress', conclusion: null })]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_INCOMPLETE');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

test('a required check that concluded failure is VERIFIED_FALSE', () => {
  const result = resolve([run('ci', { conclusion: 'failure' })]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_NOT_SUCCESS');
  assert.equal(result.determination, 'VERIFIED_FALSE');
});

// The forgery this adapter exists to catch: a check run that did not actually
// verify the commit, dressed up as a completed check. "skipped", "neutral" and
// "stale" all mean no verification happened.
test('skipped, neutral, cancelled and stale conclusions are never a pass', () => {
  for (const conclusion of ['skipped', 'neutral', 'cancelled', 'stale', 'timed_out', 'action_required']) {
    const result = resolve([run('ci', { conclusion: conclusion })]);
    assert.equal(result.ok, false, conclusion + ' must not pass');
    assert.equal(result.reason, 'REQUIRED_CHECK_NOT_SUCCESS', conclusion);
  }
});

test('a null conclusion on a completed run is not a pass', () => {
  const result = resolve([run('ci', { conclusion: null })]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_NOT_SUCCESS');
});

test('a re-run leaving one failed entry under the required name still fails', () => {
  const result = resolve([run('ci', { conclusion: 'failure' }), run('ci')]);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_NOT_SUCCESS');
});

test('a fetch that throws is COULD_NOT_VERIFY, never a silent pass', () => {
  const result = resolveCiResult(SHA, {
    repo: REPO,
    requiredChecks: REQUIRED,
    fetchCheckRuns: function () {
      throw new Error('gh: network unreachable');
    },
  });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'CI_FETCH_FAILED');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

test('a response without a check_runs array is malformed, not empty', () => {
  for (const payload of [null, 'ok', {}, { check_runs: 'ci passed' }, []]) {
    const result = resolveCiResult(SHA, {
      repo: REPO,
      requiredChecks: REQUIRED,
      fetchCheckRuns: function () {
        return payload;
      },
    });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'MALFORMED_CI_RESPONSE');
    assert.equal(result.determination, 'COULD_NOT_VERIFY');
  }
});

test('a check run without a usable name, status or full head_sha is malformed', () => {
  const malformed = [
    [{ status: 'completed', conclusion: 'success', head_sha: SHA }],
    [{ name: 'ci', conclusion: 'success', head_sha: SHA }],
    [{ name: 'ci', status: 'completed', conclusion: 'success' }],
    [{ name: 'ci', status: 'completed', conclusion: 'success', head_sha: SHA.slice(0, 7) }],
    [null],
  ];
  for (const checkRuns of malformed) {
    const result = resolve(checkRuns);
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'MALFORMED_CI_RESPONSE');
  }
});

test('an abbreviated, uppercase or absent head SHA is refused before any fetch', () => {
  let fetched = false;
  const spy = function () {
    fetched = true;
    return { check_runs: [run('ci')] };
  };
  for (const bad of [SHA.slice(0, 7), SHA.toUpperCase(), '', undefined, 123]) {
    const result = resolveCiResult(bad, { repo: REPO, requiredChecks: REQUIRED, fetchCheckRuns: spy });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'MALFORMED_SHA');
  }
  assert.equal(fetched, false, 'a malformed SHA must never reach the check-run source');
});

test('there is no default fetcher, so no call can reach GitHub by accident', () => {
  const result = resolveCiResult(SHA, { repo: REPO, requiredChecks: REQUIRED });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'INVALID_OPTIONS');
  assert.equal(result.determination, 'COULD_NOT_VERIFY');
});

test('an absent or empty required-check list fails closed rather than passing vacuously', () => {
  for (const requiredChecks of [undefined, [], ['ci', ''], 'ci']) {
    const result = resolveCiResult(SHA, { repo: REPO, requiredChecks: requiredChecks, fetchCheckRuns: stub([run('ci')]) });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'INVALID_OPTIONS');
  }
});

test('a missing repo fails closed', () => {
  const result = resolveCiResult(SHA, { requiredChecks: REQUIRED, fetchCheckRuns: stub([run('ci')]) });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'INVALID_OPTIONS');
});

// Boundary stated as a test so it cannot be forgotten: non-required checks are
// reported, not judged. Widening the gate to them is a governed change to
// config/experiment.json, not a quiet change here.
test('a failing non-required check is reported but does not itself block', () => {
  const result = resolve([run('ci'), run('optional-extra', { conclusion: 'failure' })]);
  assert.equal(result.ok, true);
  const extra = result.observedChecks.find((c) => c.name === 'optional-extra');
  assert.equal(extra.conclusion, 'failure');
});

test('the Run 002 entrypoint uses the real governed repo and required_checks', () => {
  const experiment = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', '..', 'config', 'experiment.json'), 'utf8')
  );
  let seenRepo = null;
  const result = resolveRun002CiResult(SHA, {
    fetchCheckRuns: function (repo) {
      seenRepo = repo;
      return { check_runs: experiment.github.required_checks.map((name) => run(name)) };
    },
  });
  assert.equal(result.ok, true);
  assert.equal(seenRepo, experiment.github.repo);
  assert.deepEqual(result.requiredChecks, experiment.github.required_checks);
});

test('the Run 002 entrypoint still fails closed when a governed check is absent', () => {
  const result = resolveRun002CiResult(SHA, { fetchCheckRuns: stub([run('not-the-governed-check')]) });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'REQUIRED_CHECK_MISSING');
});
