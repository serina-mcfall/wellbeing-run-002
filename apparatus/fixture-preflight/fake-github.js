// INJECTED FAKE GitHub for the C-04a fixture preflight.
//
// There is no network here, no gh CLI, no token, and no real pull
// request. This is an in-memory model of the few GitHub behaviours the
// C-04a lifecycle depends on, so the scenario can be driven end to end
// without touching a live repository.
//
// It models exactly four GitHub rules and no more:
//   1. a draft pull request cannot be merged (GitHub refuses this);
//   2. a merge call that names a head SHA other than the pull request's
//      current head is refused (GitHub's expected_head_sha);
//   3. an already-merged or closed pull request cannot be merged again;
//   4. pushing a new head does NOT retroactively rewrite the SHA an
//      earlier review was submitted against.
//
// Rule 4 matters most. The fake deliberately does NOT dismiss stale
// approvals, because deciding that a review bound to an older SHA no
// longer authorizes a merge is the MERGE-ELIGIBILITY layer's job, not
// the forge's. If the fake silently dismissed them, the changed-head
// test would pass without the eligibility layer doing anything, and
// would prove nothing.
//
// Every state change appends to an event transcript with an explicit
// actor. The lifecycle test asserts that no event in a fully autonomous
// run carries actor 'human'.

class FakeGitHubError extends Error {
  constructor(code, message) {
    super(code + ': ' + message);
    this.code = code;
  }
}

function createFakeGitHub() {
  const pulls = new Map();
  const events = [];
  let nextNumber = 1;

  function record(type, actor, detail) {
    events.push(Object.assign({ type: type, actor: actor, seq: events.length }, detail));
  }

  function mustGet(number) {
    const pr = pulls.get(number);
    if (!pr) throw new FakeGitHubError('NO_SUCH_PR', 'pull request #' + number + ' does not exist.');
    return pr;
  }

  return {
    events: events,

    createPullRequest(opts) {
      const number = nextNumber++;
      const pr = {
        number: number,
        title: opts.title,
        headRef: opts.headRef,
        headSha: opts.headSha,
        baseRef: opts.baseRef,
        draft: opts.draft === true,
        state: 'OPEN',
        reviews: [],
        checkRuns: [],
      };
      pulls.set(number, pr);
      record('pr_created', opts.actor || 'automation', {
        number: number,
        draft: pr.draft,
        headSha: pr.headSha,
      });
      return number;
    },

    getPullRequest(number) {
      const pr = mustGet(number);
      return {
        number: pr.number,
        headRef: pr.headRef,
        headSha: pr.headSha,
        baseRef: pr.baseRef,
        draft: pr.draft,
        state: pr.state,
        reviews: pr.reviews.map((r) => Object.assign({}, r)),
        checkRuns: pr.checkRuns.map((c) => Object.assign({}, c)),
      };
    },

    // Models a real push: the head moves. Existing reviews keep the SHA
    // they were submitted against — see rule 4 above.
    pushHead(number, sha, actor) {
      const pr = mustGet(number);
      pr.headSha = sha;
      record('head_pushed', actor || 'automation', { number: number, headSha: sha });
    },

    submitReview(number, review) {
      const pr = mustGet(number);
      const entry = {
        producer: review.producer,
        sha: review.sha,
        verdict: review.verdict,
        cycle: pr.reviews.length + 1,
      };
      pr.reviews.push(entry);
      record('review_submitted', review.actor || 'automation', {
        number: number,
        producer: entry.producer,
        sha: entry.sha,
        verdict: entry.verdict,
        cycle: entry.cycle,
      });
      return entry;
    },

    reportCheckRun(number, run) {
      const pr = mustGet(number);
      const entry = { name: run.name, sha: run.sha, status: run.status };
      pr.checkRuns.push(entry);
      record('check_reported', run.actor || 'automation', {
        number: number,
        name: entry.name,
        sha: entry.sha,
        status: entry.status,
      });
      return entry;
    },

    markReadyForReview(number, actor) {
      const pr = mustGet(number);
      if (!pr.draft) {
        throw new FakeGitHubError('ALREADY_READY', 'pull request #' + number + ' is not a draft.');
      }
      pr.draft = false;
      record('pr_marked_ready', actor || 'automation', { number: number });
    },

    merge(number, opts) {
      const pr = mustGet(number);
      opts = opts || {};
      if (pr.state !== 'OPEN') {
        throw new FakeGitHubError('NOT_OPEN', 'pull request #' + number + ' is ' + pr.state + '.');
      }
      if (pr.draft) {
        throw new FakeGitHubError('DRAFT_NOT_MERGEABLE', 'pull request #' + number + ' is still a draft.');
      }
      if (opts.expectedHeadSha !== pr.headSha) {
        throw new FakeGitHubError(
          'HEAD_MOVED',
          'expected head ' + opts.expectedHeadSha + ' but pull request head is ' + pr.headSha + '.'
        );
      }
      pr.state = 'MERGED';
      pr.mergedSha = pr.headSha;
      record('pr_merged', opts.actor || 'automation', { number: number, mergedSha: pr.mergedSha });
      return { merged: true, mergedSha: pr.mergedSha };
    },
  };
}

module.exports = { createFakeGitHub, FakeGitHubError };
