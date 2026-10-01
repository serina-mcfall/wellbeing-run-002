// Canonical accessibility requirement registry (C-02 integration).
//
// WHAT THIS IS. protocol/SEVERITY-POLICY.md rule 1 says an accessibility
// FAILURE must cite "a stated requirement in product/ACCESSIBILITY.md's
// definition of done or cognitive accessibility list". severity-floor.js
// (and its 1:1 port control/severity.py) accepts that citation only if it
// matches an identifier in a registry the CALLER supplies — without one,
// every FAILURE fails closed as INVALID. This module is that registry.
//
// DERIVATION. Every entry is derived mechanically from product/ACCESSIBILITY.md
// (imported, byte-for-byte frozen, NOT edited by this work):
//
//   line 2 "Definition of done: ..."      -> group 'definition-of-done' (12 entries)
//   line 3 "Cognitive accessibility: ..." -> group 'cognitive'          (5 entries)
//
// Each listed phrase, comma-separated in the source, becomes exactly one
// entry. `source_phrase` is the phrase verbatim from that document — it is
// the provenance link, and requirement-registry.test.js re-parses the frozen
// document and asserts the two sets are equal, so a drift in either
// direction (a phrase added to the source, or an entry invented here) turns
// the suite red rather than silently widening or narrowing the gate.
//
// IDENTIFIERS ARE SEMANTIC, NOT POSITIONAL. `ACC-DOD-VISIBLE_FOCUS`, not
// `ACC-DOD-03`. A positional identifier would silently re-point at a
// different requirement if the source list were ever reordered, which would
// make already-filed evidence cite the wrong thing. The id is stable
// because it is tied to meaning.
//
// SCOPE LIMIT — this registry makes a citation RECOGNISABLE, not TRUE.
// Matching an id proves only that the reviewer named a real requirement
// from the frozen product spec. It is no evidence that the requirement is
// actually unmet at the reviewed SHA; that remains the independent
// Accessibility Reviewer's judgement (Protocol v2 "Accessibility gate").
// Nothing here weakens the P1 floor: the floor is applied by
// severity-floor.js after a citation is recognised, and an unrecognised
// citation still fails closed as INVALID.

const path = require('path');

const SOURCE_FILE = path.join(__dirname, '..', '..', 'product', 'ACCESSIBILITY.md');

const REQUIREMENTS = Object.freeze(
  [
    // product/ACCESSIBILITY.md line 2 — "Definition of done:"
    { id: 'ACC-DOD-SEMANTIC_HTML', group: 'definition-of-done', source_phrase: 'semantic HTML' },
    { id: 'ACC-DOD-KEYBOARD_OPERATION', group: 'definition-of-done', source_phrase: 'keyboard operation' },
    { id: 'ACC-DOD-VISIBLE_FOCUS', group: 'definition-of-done', source_phrase: 'visible focus' },
    { id: 'ACC-DOD-MEANINGFUL_LABELS', group: 'definition-of-done', source_phrase: 'meaningful labels' },
    {
      id: 'ACC-DOD-SCREEN_READER_FORMS_ERRORS',
      group: 'definition-of-done',
      source_phrase: 'screen-reader understandable forms/errors',
    },
    {
      id: 'ACC-DOD-COLOUR_INDEPENDENT_MEANING',
      group: 'definition-of-done',
      source_phrase: 'colour-independent meaning',
    },
    { id: 'ACC-DOD-SUFFICIENT_CONTRAST', group: 'definition-of-done', source_phrase: 'sufficient contrast' },
    { id: 'ACC-DOD-REDUCED_MOTION', group: 'definition-of-done', source_phrase: 'reduced motion' },
    { id: 'ACC-DOD-RESPONSIVE_LAYOUT', group: 'definition-of-done', source_phrase: 'responsive layout' },
    { id: 'ACC-DOD-TOUCH_TARGETS', group: 'definition-of-done', source_phrase: 'sensible touch targets' },
    { id: 'ACC-DOD-HEADING_STRUCTURE', group: 'definition-of-done', source_phrase: 'heading structure' },
    { id: 'ACC-DOD-CHART_SUMMARIES', group: 'definition-of-done', source_phrase: 'accessible chart summaries' },

    // product/ACCESSIBILITY.md line 3 — "Cognitive accessibility:"
    { id: 'ACC-COG-PREDICTABLE_NAVIGATION', group: 'cognitive', source_phrase: 'predictable navigation' },
    { id: 'ACC-COG-CONCISE_INSTRUCTIONS', group: 'cognitive', source_phrase: 'concise instructions' },
    { id: 'ACC-COG-LOW_INFORMATION_DENSITY', group: 'cognitive', source_phrase: 'low information density' },
    { id: 'ACC-COG-CLEAR_BACK_EXIT_ROUTES', group: 'cognitive', source_phrase: 'clear back/exit routes' },
    {
      id: 'ACC-COG-NO_UNNECESSARY_URGENCY_PUNISHMENT',
      group: 'cognitive',
      source_phrase: 'no unnecessary urgency/punishment',
    },
  ].map(Object.freeze)
);

const REQUIREMENT_IDS = Object.freeze(REQUIREMENTS.map((r) => r.id));

module.exports = {
  REQUIREMENTS: REQUIREMENTS,
  REQUIREMENT_IDS: REQUIREMENT_IDS,
  SOURCE_FILE: SOURCE_FILE,
};
