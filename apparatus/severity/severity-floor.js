// Implements protocol/SEVERITY-POLICY.md (C-02 proposal).
//
// Scope: this module applies the deterministic P1 accessibility floor and
// the evidence-shape rule (SEVERITY-POLICY.md rules 1-3) to a single
// finding. It does NOT decide merge eligibility for a whole PR (that is
// the PR evidence validator, C-04) and it does NOT own the canonical set
// of accessibility requirement identifiers — callers must supply
// knownRequirementIds, the identifier registry the future PR evidence
// validator maintains. Without that registry, a citation cannot be
// confirmed and the finding fails closed as INVALID: a nonempty
// arbitrary string alone is insufficient for the final gate.
//
// mergeBlocked reflects Protocol v2 "Severity — single source of truth":
// P0/P1 always block merge; P2/P3 do not independently block merge;
// INVALID evidence always blocks merge (a missing-evidence state, not a
// severity choice — SEVERITY-POLICY.md rule 3).

const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
const BLOCKING_SEVERITIES = new Set(['P0', 'P1']);

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function invalid(reason) {
  return {
    valid: false,
    severity: null,
    mergeBlocked: true,
    floorApplied: false,
    reason: reason,
  };
}

function applySeverityPolicy(finding, knownRequirementIds) {
  if (finding === null || typeof finding !== 'object' || Array.isArray(finding)) {
    return invalid('INVALID: malformed finding (not a plain object).');
  }

  const jev_severity = finding.jev_severity;
  const classification = finding.classification;
  const unmet_requirement = finding.unmet_requirement;
  const non_failure_rationale = finding.non_failure_rationale;

  if (typeof jev_severity !== 'string' || SEVERITIES.indexOf(jev_severity) === -1) {
    return invalid('INVALID: unknown or missing jev_severity (' + JSON.stringify(jev_severity) + ').');
  }

  if (classification !== 'FAILURE' && classification !== 'NON_FAILURE') {
    return invalid(
      'INVALID: evidence does not clearly cite an unmet requirement ' +
      '(case 1) or affirmatively explain why no requirement is unmet ' +
      '(case 2). Escalate to an independent reviewer per Protocol v2 ' +
      '"Accessibility gate".'
    );
  }

  const hasCitation = isNonEmptyString(unmet_requirement);
  const hasRationale = isNonEmptyString(non_failure_rationale);

  if (classification === 'FAILURE' && hasRationale) {
    return invalid('INVALID: FAILURE classification carries a non_failure_rationale; contradictory evidence.');
  }
  if (classification === 'NON_FAILURE' && hasCitation) {
    return invalid('INVALID: NON_FAILURE classification carries an unmet_requirement; contradictory evidence.');
  }

  if (classification === 'FAILURE') {
    if (!hasCitation) {
      return invalid('INVALID: FAILURE classification without an unmet_requirement citation.');
    }

    const registry = knownRequirementIds ? new Set(knownRequirementIds) : null;
    if (!registry || !registry.has(unmet_requirement)) {
      return invalid(
        'INVALID: unmet_requirement "' + unmet_requirement + '" does not match an ' +
        'identifier in the accessibility requirement registry (owned by the ' +
        'PR evidence validator, C-04); a nonempty arbitrary string alone is ' +
        'insufficient for the final gate.'
      );
    }

    const floorRank = SEVERITIES.indexOf('P1');
    const rawRank = SEVERITIES.indexOf(jev_severity);
    const finalRank = Math.min(rawRank, floorRank);
    const severity = SEVERITIES[finalRank];

    return {
      valid: true,
      severity: severity,
      mergeBlocked: BLOCKING_SEVERITIES.has(severity),
      floorApplied: finalRank !== rawRank,
      reason: null,
    };
  }

  if (!hasRationale) {
    return invalid('INVALID: NON_FAILURE classification without an affirmative rationale.');
  }

  return {
    valid: true,
    severity: jev_severity,
    mergeBlocked: BLOCKING_SEVERITIES.has(jev_severity),
    floorApplied: false,
    reason: null,
  };
}

module.exports = {
  applySeverityPolicy: applySeverityPolicy,
  SEVERITIES: SEVERITIES,
  BLOCKING_SEVERITIES: BLOCKING_SEVERITIES,
};
