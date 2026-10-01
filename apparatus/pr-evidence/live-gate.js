// C-04 LIVE MERGE-GATE COMPOSITION LAYER.
//
// apparatus/pr-evidence/validate.js answers "is this submitted package
// internally well-formed and policy-clean?" — a question about CLAIMS. Nothing
// in this repository previously joined two adapters together, so no code
// anywhere computed a live merge decision. This module is that join: one entry
// point that takes a PR/task identity, calls all four C-04 adapters plus the
// accessibility severity floor against the real requirement registry (via
// checkOfflinePolicy), and returns one merge decision.
//
// THE ANCHOR IS THE TRUSTED HEAD SHA, NEVER THE PACKAGE.
// git-head.js resolves the current commit for the task identity from git
// itself. Everything downstream — CI, reviewer identity, review provenance,
// the live pull-request view — is checked against THAT value. pkg.head_sha is
// never an input to any of them; it is only ever compared to the trusted head
// and must equal it. A model's claimed SHA is a claim, and a claim can be
// written by the thing being judged.
//
// DEFAULT DENY. The decision starts DENIED and is only ever raised to ELIGIBLE
// by one explicit positive assertion at the very bottom, which re-reads every
// adapter's own ok flag. Deleting a denial is therefore not enough to make
// something merge; the final assertion still refuses. Every unknown — an
// unobservable head, an unreadable ledger, an adapter that COULD_NOT_VERIFY, a
// merge state nobody reported — is a denial, not a pass. COULD_NOT_VERIFY is
// not weak evidence of health; it is the absence of evidence.
//
// NOTHING HERE PERFORMS AN EFFECT. No merge, no GitHub write, no notification,
// no state mutation, no clock read. Every adapter arrives as an injected
// function, so this module cannot reach the network even by accident, and a
// caller must say explicitly where each fact comes from.
//
// WHAT THIS DOES NOT DO: it never marks a draft pull request ready. Draft
// status is not governed anywhere in protocol/RUN-002-PROTOCOL-v2.0.md — the
// word does not appear in it — so taking a pull request out of draft is an
// ungoverned action and inventing one here would be a silent amendment to a
// frozen specification. Instead a draft is reported as its own finite
// condition, and `blockedOnlyByDraft` says whether draft is the ONLY thing
// standing between this pull request and eligibility. See handover §34.

const { checkOfflinePolicy } = require('./validate.js');

const SHA_RE = /^[0-9a-f]{40}$/;

const ELIGIBLE = 'ELIGIBLE';
const DENIED = 'DENIED';

const VERIFIED_FALSE = 'VERIFIED_FALSE';
const COULD_NOT_VERIFY = 'COULD_NOT_VERIFY';

// The ledger events that carry independent review provenance, and the role
// that produces them (control/supervisor.py::dispatch_reviewer).
const REVIEW_DISPATCHED_EVENT = 'REVIEW_DISPATCHED';
const REVIEW_RESULT_EVENT = 'REVIEW_RESULT';
const REVIEW_ROLE = 'reviewer';

// The one merge state that clears. An ALLOW-list, not a deny-list of the three
// bad values control/routing.py::evaluate_merge happens to name: GitHub's
// mergeStateStatus vocabulary (BEHIND, BLOCKED, CLEAN, DIRTY, DRAFT, HAS_HOOKS,
// UNKNOWN, UNSTABLE) can gain a member, and an unrecognised one must deny.
const MERGEABLE_STATE = 'CLEAN';

const DRAFT = 'DRAFT';
const READY = 'READY';
const UNKNOWN = 'UNKNOWN';

function isSha(value) {
  return typeof value === 'string' && SHA_RE.test(value);
}

function isFunction(value) {
  return typeof value === 'function';
}

/**
 * The head SHA a review ledger event is bound to, under the contract this
 * module requires of control/supervisor.py (handover §34.2).
 *
 * `control/ledger.py`'s FIELDS tuple has no `head_sha`, so a Supervisor that
 * calls `self.log("REVIEW_DISPATCHED", ..., head_sha=head)` lands the value in
 * `metadata_redacted.head_sha`. Should `head_sha` later be promoted into
 * FIELDS it would appear at the top level instead. Both spellings are read, so
 * this module does not break when that promotion happens — but when BOTH are
 * present they must be identical. Two disagreeing provenances are worse than
 * one: nothing may choose between them.
 */
function eventHeadSha(event) {
  const top = event.head_sha;
  const meta = (event.metadata_redacted || {}).head_sha;
  if (top !== undefined && meta !== undefined && top !== meta) {
    return { ok: false, conflict: true };
  }
  const value = top !== undefined ? top : meta;
  if (!isSha(value)) {
    return { ok: false, conflict: false };
  }
  return { ok: true, sha: value };
}

function lastMatchingEvent(ledgerEvents, eventType, taskId, prNumber) {
  let found = null;
  for (const candidate of ledgerEvents) {
    if (!candidate || typeof candidate !== 'object') continue;
    if (candidate.event_type !== eventType) continue;
    if (candidate.task_id !== taskId) continue;
    if (candidate.pr_id !== prNumber) continue;
    if (candidate.role !== REVIEW_ROLE) continue;
    found = candidate;
  }
  return found;
}

/**
 * Whether the append-only ledger independently binds this review to this exact
 * commit.
 *
 * This is the check that makes the gate SHA-bound end to end, and it is
 * deliberately separate from reviewer-identity.js's own SHA check. That
 * adapter's only SHA source is `prs[n].reviewed_head` in .runtime/state.json —
 * MUTABLE state that the next review cycle overwrites and that anyone who can
 * write the file can edit (its own header says so). The ledger is append-only
 * and fsynced per event, so a REVIEW_DISPATCHED / REVIEW_RESULT pair carrying
 * head_sha is evidence an older cycle's REVIEW_PASS cannot be re-presented
 * against: the verdict for commit B must be carried by events that themselves
 * name commit B.
 *
 * `workerId` is the agent_id reviewer-identity.js already verified. Requiring
 * the SHA-bound REVIEW_RESULT to name that same worker is what stops a
 * correctly-SHA-bound event from one dispatch vouching for a verdict that came
 * out of another.
 */
function verifyReviewProvenance(ledgerEvents, taskId, prNumber, trustedHeadSha, workerId) {
  if (!Array.isArray(ledgerEvents)) {
    return {
      ok: false,
      code: 'REVIEW_PROVENANCE_UNREADABLE',
      determination: COULD_NOT_VERIFY,
      detail: 'no ledger events were supplied, so no review can be bound to a commit.',
    };
  }

  const dispatched = lastMatchingEvent(ledgerEvents, REVIEW_DISPATCHED_EVENT, taskId, prNumber);
  const result = lastMatchingEvent(ledgerEvents, REVIEW_RESULT_EVENT, taskId, prNumber);
  if (!dispatched || !result) {
    return {
      ok: false,
      code: 'REVIEW_PROVENANCE_MISSING',
      determination: COULD_NOT_VERIFY,
      detail:
        'the ledger carries no ' +
        (!dispatched ? REVIEW_DISPATCHED_EVENT : REVIEW_RESULT_EVENT) +
        ' for ' + taskId + ' PR #' + prNumber + '.',
    };
  }

  for (const [label, event] of [['REVIEW_DISPATCHED', dispatched], ['REVIEW_RESULT', result]]) {
    const bound = eventHeadSha(event);
    if (bound.conflict) {
      return {
        ok: false,
        code: 'REVIEW_PROVENANCE_CONFLICT',
        determination: VERIFIED_FALSE,
        detail: label + ' carries two disagreeing head_sha values.',
      };
    }
    if (!bound.ok) {
      return {
        ok: false,
        code: 'REVIEW_PROVENANCE_UNBOUND',
        determination: COULD_NOT_VERIFY,
        detail:
          label + ' carries no usable head_sha, so this review is bound to no commit. ' +
          'A SHA the package itself claims is not a substitute.',
      };
    }
    if (bound.sha !== trustedHeadSha) {
      return {
        ok: false,
        code: 'REVIEW_PROVENANCE_SHA_MISMATCH',
        determination: VERIFIED_FALSE,
        detail:
          label + ' is bound to ' + bound.sha + ', not the trusted head ' + trustedHeadSha + '.',
      };
    }
  }

  if (typeof workerId !== 'string' || result.agent_id !== workerId) {
    return {
      ok: false,
      code: 'REVIEW_PROVENANCE_WORKER_MISMATCH',
      determination: VERIFIED_FALSE,
      detail:
        'the SHA-bound REVIEW_RESULT was produced by "' + result.agent_id +
        '", not the reviewer worker whose identity was verified ("' + workerId + '").',
    };
  }

  return { ok: true, headSha: trustedHeadSha, workerId: workerId };
}

// Every place the PR-evidence schema can carry a commit SHA, as a path into
// the package. checkOfflinePolicy compares only `ci.sha` and `review.sha` to
// `head_sha`, so accessibility and security evidence produced against a
// DIFFERENT commit passes the offline policy untouched. That is a real hole:
// Protocol v2's rule is "new SHA => regenerate required automated evidence",
// and it does not exempt two of the four evidence classes.
//
// Binding belongs here rather than in the offline checker for a reason beyond
// ownership: the offline checker can only compare these values to `head_sha`,
// which the submitter also wrote, so agreement there proves internal
// consistency and nothing else. Here every one of them is compared to the
// TRUSTED head git itself resolved, which is the comparison that means
// something.
function shaBearingPaths(pkg) {
  const paths = [];
  const push = (path, value, required) => paths.push({ path, value, required });

  for (const block of ['ci', 'review', 'accessibility', 'security']) {
    const section = pkg[block];
    if (!section || typeof section !== 'object') continue;
    // `ci.sha`/`review.sha` are schema-required; `accessibility.sha` and
    // `security.sha` are optional, so an absent one is the schema's gap, not a
    // forgery - it is reported as a gap in handover §34.5 and not failed here.
    // `provenance.sha` is required on all four blocks, so its absence IS a
    // failure.
    push(block + '.sha', section.sha, block === 'ci' || block === 'review');
    const prov = section.provenance;
    if (prov && typeof prov === 'object') {
      push(block + '.provenance.sha', prov.sha, true);
    } else {
      push(block + '.provenance.sha', undefined, true);
    }
    if (Array.isArray(section.checks)) {
      section.checks.forEach((check, index) => {
        // A security surface marked `relevant: false` carries no result,
        // no artifact_reference and no sha — the schema's own conditional
        // says so: securityCheck requires only check_id and relevant, and
        // requires result/artifact_reference/sha ONLY under
        // `if relevant === true`. An accessibility check has no such
        // conditional; sha is required on all ten unconditionally.
        //
        // Requiring a sha on an irrelevant surface denied every realistic
        // package — a real task marks most of the twelve security surfaces
        // irrelevant — so this gate could never return ELIGIBLE. That is
        // fail-closed, and therefore safe, but a gate that can never open
        // is not a gate; it is an outage waiting for T+00.
        //
        // A sha that IS present is still checked against the trusted head
        // below, relevant or not: nothing is exempted, only un-required.
        const required =
          block !== 'security' || !check || check.relevant === true;
        push(block + '.checks[' + index + '].sha', check && check.sha, required);
      });
    }
  }
  return paths;
}

/**
 * Every evidence class in the package must name the one trusted head. An
 * absent SHA where the schema requires one is a denial, never an exemption.
 */
function verifyEvidenceShaBinding(pkg, trustedHeadSha) {
  const unbound = [];
  const mismatched = [];
  for (const entry of shaBearingPaths(pkg)) {
    if (entry.value === undefined || entry.value === null) {
      if (entry.required) unbound.push(entry.path);
      continue;
    }
    if (!isSha(entry.value) || entry.value !== trustedHeadSha) {
      mismatched.push(entry.path + '=' + entry.value);
    }
  }
  if (mismatched.length > 0) {
    return {
      ok: false,
      code: 'EVIDENCE_SHA_UNBOUND',
      determination: VERIFIED_FALSE,
      detail:
        'evidence produced against another commit: ' + mismatched.join(', ') +
        '; the trusted head is ' + trustedHeadSha + '.',
    };
  }
  if (unbound.length > 0) {
    return {
      ok: false,
      code: 'EVIDENCE_SHA_UNBOUND',
      determination: COULD_NOT_VERIFY,
      detail: 'evidence carrying no commit at all: ' + unbound.join(', ') + '.',
    };
  }
  return { ok: true };
}

function draftStateOf(prView) {
  if (!prView || typeof prView !== 'object') return UNKNOWN;
  if (prView.isDraft === true) return DRAFT;
  if (prView.isDraft === false) return READY;
  return UNKNOWN;
}

/**
 * The live merge decision for one pull request.
 *
 * request:
 *   identity     - a git-head.js identity, e.g. { kind: 'branch', ref: 'run-002/...' }
 *   prNumber     - integer. Supplied out of band because the PR-evidence
 *                  schema has no pr_number field (handover §28.3 item 3, §34.5).
 *   pkg          - the submitted PR-evidence package. CLAIMS ONLY.
 *   prView       - one `gh pr view` observation, already taken by the caller.
 *   ledgerEvents - the decoded append-only ledger, for review provenance.
 *
 * adapters (all REQUIRED, all injected — this module makes no call of its own):
 *   resolveHeadSha(identity) - apparatus/adapters/git-head.js
 *   resolveCi(headSha)       - apparatus/adapters/ci-result.js
 *   resolveTask(taskId)      - apparatus/adapters/task-record.js
 *   resolveReviewer(claim)   - apparatus/adapters/reviewer-identity.js
 */
function evaluateLiveMergeEligibility(request, adapters) {
  const reasons = [];
  const deny = (code, determination, detail) => {
    reasons.push({ code: code, determination: determination, detail: detail });
  };

  request = request || {};
  adapters = adapters || {};

  let headResult = null;
  let ciResult = null;
  let taskResult = null;
  let reviewerResult = null;
  let provenance = null;
  let offline = null;
  let shaBinding = null;
  let trustedHeadSha = null;

  const draftState = draftStateOf(request.prView);
  const pkg = request.pkg;
  const prNumber = request.prNumber;

  const missingAdapters = ['resolveHeadSha', 'resolveCi', 'resolveTask', 'resolveReviewer'].filter(
    (name) => !isFunction(adapters[name])
  );
  const pkgUsable = Boolean(pkg) && typeof pkg === 'object' && !Array.isArray(pkg);
  if (missingAdapters.length > 0) {
    deny(
      'COMPOSITION_INPUT_INVALID',
      COULD_NOT_VERIFY,
      'these adapters were not supplied: ' + missingAdapters.join(', ') + '.'
    );
  }
  if (!pkgUsable) {
    deny('COMPOSITION_INPUT_INVALID', COULD_NOT_VERIFY, 'request.pkg must be an object.');
  }
  if (!Number.isInteger(prNumber)) {
    deny('COMPOSITION_INPUT_INVALID', COULD_NOT_VERIFY, 'request.prNumber must be an integer.');
  }
  // Only a structurally unusable REQUEST stops the live half. A failing
  // OFFLINE policy deliberately does NOT: the two halves are independent
  // sources, and collapsing them would hide every live fact behind the first
  // schema complaint, leaving an operator unable to see whether anything else
  // is also wrong.
  const inputsUsable = missingAdapters.length === 0 && pkgUsable && Number.isInteger(prNumber);

  // The offline half: schema, explicit applicability, blocking findings, and
  // the accessibility severity floor against the real requirement registry.
  // One half of a two-source check, never a substitute for the live half.
  if (pkgUsable) {
    offline = checkOfflinePolicy(pkg);
    for (const error of offline.errors) {
      deny('OFFLINE_POLICY_FAILED', VERIFIED_FALSE, error);
    }
  }

  if (inputsUsable) {
    // ---------------------------------------------------- 1. the trusted head
    headResult = adapters.resolveHeadSha(request.identity);
    if (!headResult || headResult.ok !== true || !isSha(headResult.sha)) {
      deny(
        'HEAD_SHA_UNVERIFIED',
        (headResult && headResult.determination) || COULD_NOT_VERIFY,
        'the task identity could not be resolved to a trusted head commit: ' +
          ((headResult && headResult.reason) || 'no result') + '.'
      );
    } else {
      trustedHeadSha = headResult.sha;
    }
  }

  if (trustedHeadSha) {
    // ------------------------------- 2. the package's claim must match it
    // Stale evidence dies here. A verdict produced against an older head
    // carries that older head, and no amount of internal self-consistency
    // inside the package can make it current.
    if (!isSha(pkg.head_sha)) {
      deny(
        'EVIDENCE_SHA_MISSING',
        COULD_NOT_VERIFY,
        'the evidence package carries no usable head_sha to compare with the trusted head.'
      );
    } else if (pkg.head_sha !== trustedHeadSha) {
      deny(
        'EVIDENCE_SHA_STALE',
        VERIFIED_FALSE,
        'the evidence package is for ' + pkg.head_sha + ', but the task identity now resolves to ' +
          trustedHeadSha + '.'
      );
    }

    // ----------- 2b. EVERY evidence class, not only CI and the code review
    shaBinding = verifyEvidenceShaBinding(pkg, trustedHeadSha);
    if (!shaBinding.ok) {
      deny(shaBinding.code, shaBinding.determination, shaBinding.detail);
    }

    // ----------------------------- 3. the live pull request must match it too
    // git and GitHub are two different observations. If the pull request's head
    // has moved since the branch was resolved, no evidence gathered for either
    // value describes what would actually be merged.
    const observedHead = request.prView && request.prView.headRefOid;
    if (!isSha(observedHead)) {
      deny(
        'PR_HEAD_UNOBSERVED',
        COULD_NOT_VERIFY,
        'the pull-request observation carries no usable headRefOid.'
      );
    } else if (observedHead !== trustedHeadSha) {
      deny(
        'PR_HEAD_MOVED',
        VERIFIED_FALSE,
        'the pull request head is ' + observedHead + ', not the trusted head ' + trustedHeadSha + '.'
      );
    }

    // ------------------------------------------------------ 4. task identity
    taskResult = adapters.resolveTask(pkg.task_id);
    if (!taskResult || taskResult.ok !== true) {
      deny(
        'TASK_RECORD_UNVERIFIED',
        (taskResult && taskResult.determination) || COULD_NOT_VERIFY,
        'task_id "' + pkg.task_id + '" could not be resolved: ' +
          ((taskResult && taskResult.reason) || 'no result') + '.'
      );
    }

    // ----------------------------------------- 5. CI, for THIS exact commit
    ciResult = adapters.resolveCi(trustedHeadSha);
    if (!ciResult || ciResult.ok !== true) {
      deny(
        'CI_UNVERIFIED',
        (ciResult && ciResult.determination) || COULD_NOT_VERIFY,
        ((ciResult && ciResult.reason) || 'no result') + ' — ' +
          ((ciResult && ciResult.detail) || 'the CI adapter returned nothing.')
      );
    }

    // --------------------------------- 6. who actually produced the review
    const review = pkg.review || {};
    reviewerResult = adapters.resolveReviewer({
      taskId: pkg.task_id,
      prNumber: prNumber,
      headSha: trustedHeadSha,
      producer: review.provenance && review.provenance.producer,
      verdict: review.verdict,
    });
    if (!reviewerResult || reviewerResult.ok !== true) {
      deny(
        'REVIEWER_UNVERIFIED',
        (reviewerResult && reviewerResult.determination) || COULD_NOT_VERIFY,
        ((reviewerResult && reviewerResult.reason) || 'no result') + ' — ' +
          ((reviewerResult && reviewerResult.detail) || 'the reviewer adapter returned nothing.')
      );
    }

    // ------------- 7. append-only proof that THIS review is for THIS commit
    provenance = verifyReviewProvenance(
      request.ledgerEvents,
      pkg.task_id,
      prNumber,
      trustedHeadSha,
      reviewerResult && reviewerResult.ok === true ? reviewerResult.workerId : null
    );
    if (!provenance.ok) {
      deny(provenance.code, provenance.determination, provenance.detail);
    }
  }

  // ------------------------------------------------- 8. mergeability, live
  // Judged last and independently of the evidence chain, so that a pull
  // request held up only by its draft flag is visibly held up only by that.
  const prView = request.prView;
  if (!prView || typeof prView !== 'object') {
    deny('PR_UNOBSERVED', COULD_NOT_VERIFY, 'no pull-request observation was supplied.');
  } else {
    if (prView.state !== 'OPEN') {
      deny('PR_NOT_OPEN', VERIFIED_FALSE, 'the pull request state is "' + prView.state + '".');
    }
    if (draftState === DRAFT) {
      deny(
        'PR_IS_DRAFT',
        VERIFIED_FALSE,
        'the pull request is a draft. Marking it ready is not a governed action ' +
          '(protocol/RUN-002-PROTOCOL-v2.0.md never mentions draft pull requests), ' +
          'so this gate reports the condition and changes nothing.'
      );
    } else if (draftState === UNKNOWN) {
      deny('PR_DRAFT_STATE_UNKNOWN', COULD_NOT_VERIFY, 'the observation carries no isDraft boolean.');
    }
    const mergeable = String(prView.mergeable || '').toUpperCase();
    const mergeState = String(prView.mergeStateStatus || '').toUpperCase();
    if (mergeable === 'CONFLICTING') {
      deny('PR_CONFLICTING', VERIFIED_FALSE, 'the branch conflicts with the base branch.');
    }
    if (mergeState === 'BEHIND') {
      deny('PR_BEHIND_BASE', VERIFIED_FALSE, 'the branch is behind its base and needs reconciliation.');
    } else if (mergeState === 'BLOCKED') {
      // Named separately rather than lumped into "merge state not clean".
      // BLOCKED is what GitHub reports when branch protection is unsatisfied —
      // today, for Run 002, a required approving review that nothing in the
      // control plane submits. An operator reading a generic denial would go
      // looking for a defect in the evidence chain that is not there.
      deny(
        'PR_BLOCKED_BY_BRANCH_PROTECTION',
        VERIFIED_FALSE,
        'GitHub reports mergeStateStatus BLOCKED: a base-branch protection rule is unsatisfied ' +
          '(for example a required approving review). This is a repository-governance condition, ' +
          'not a defect in the evidence chain above.'
      );
    } else if (mergeState !== MERGEABLE_STATE) {
      deny(
        'PR_MERGE_STATE_NOT_CLEAN',
        mergeState ? VERIFIED_FALSE : COULD_NOT_VERIFY,
        'mergeStateStatus is "' + (mergeState || '(absent)') + '"; only "' + MERGEABLE_STATE +
          '" clears this gate.'
      );
    }
  }

  // ------------------------------------------------------- the decision
  //
  // DEFAULT DENY. Not "no reasons were pushed, therefore allow" — every
  // adapter's own ok flag is re-read here. Removing a deny() above does not
  // make anything merge, because this assertion still refuses.
  const verified =
    reasons.length === 0 &&
    isSha(trustedHeadSha) &&
    headResult !== null && headResult.ok === true &&
    ciResult !== null && ciResult.ok === true &&
    taskResult !== null && taskResult.ok === true &&
    reviewerResult !== null && reviewerResult.ok === true &&
    provenance !== null && provenance.ok === true &&
    shaBinding !== null && shaBinding.ok === true &&
    offline !== null && offline.policyValid === true &&
    draftState === READY;

  const draftOnly =
    reasons.length === 1 && reasons[0].code === 'PR_IS_DRAFT';

  return {
    decision: verified ? ELIGIBLE : DENIED,
    trustedHeadSha: trustedHeadSha,
    draftState: draftState,
    // True when draft is the ONLY thing holding this pull request back. A
    // draft must never be a silent stall: this is the distinguishable,
    // reportable "would merge if a human took it out of draft" condition.
    blockedOnlyByDraft: draftOnly,
    reasons: reasons,
    adapters: {
      headSha: headResult,
      ci: ciResult,
      task: taskResult,
      reviewer: reviewerResult,
      reviewProvenance: provenance,
      evidenceShaBinding: shaBinding,
      offlinePolicy: offline,
    },
  };
}

module.exports = {
  evaluateLiveMergeEligibility: evaluateLiveMergeEligibility,
  verifyReviewProvenance: verifyReviewProvenance,
  verifyEvidenceShaBinding: verifyEvidenceShaBinding,
  eventHeadSha: eventHeadSha,
  ELIGIBLE: ELIGIBLE,
  DENIED: DENIED,
  VERIFIED_FALSE: VERIFIED_FALSE,
  COULD_NOT_VERIFY: COULD_NOT_VERIFY,
  MERGEABLE_STATE: MERGEABLE_STATE,
};
