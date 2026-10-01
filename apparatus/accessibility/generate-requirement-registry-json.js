// Emits requirement-registry.json from the canonical registry module.
//
// WHY THIS EXISTS. requirement-registry.js is the canonical registry and
// stays so. The Python control plane cannot require() it, and C-05.3b's
// accessibility adjudication needs those identifiers from control/ before
// any accessibility FAILURE can be rated anything but INVALID
// (C05-3a-SESSION-HANDOVER.md section 36.5, D2). Rather than a second
// parser of product/ACCESSIBILITY.md — which would be a competing source
// of truth — this serialises the one registry into a form both languages
// can read.
//
// The chain of custody, every link machine-checked:
//
//   product/ACCESSIBILITY.md   (imported, frozen, never edited)
//     -> requirement-registry.js      pinned by requirement-registry.test.js
//       -> requirement-registry.json  pinned by requirement-registry-json.test.js
//         -> control/accessibility_registry.py   reads the JSON, derives nothing
//
// The JSON is a DERIVED ARTEFACT, not a source. It is committed so that
// neither CI nor the Python suite has to run a generator, and the drift
// test fails if a committed copy goes stale. Do not hand-edit it.
//
// Regenerate with:
//   node apparatus/accessibility/generate-requirement-registry-json.js

'use strict';

const fs = require('fs');
const path = require('path');
const { REQUIREMENTS } = require('./requirement-registry.js');

const OUTPUT_FILE = path.join(__dirname, 'requirement-registry.json');

// Deterministic by construction: source order preserved, fixed key order,
// two-space indent, trailing newline. No timestamp and no host detail — a
// generated file that changes when nothing changed would make the drift
// test worthless and every regeneration a spurious diff.
function serialise() {
  return (
    JSON.stringify(
      {
        _comment:
          'GENERATED from requirement-registry.js by ' +
          'generate-requirement-registry-json.js. Do not hand-edit. ' +
          'Canonical source: product/ACCESSIBILITY.md.',
        requirements: REQUIREMENTS.map((r) => ({
          id: r.id,
          group: r.group,
          source_phrase: r.source_phrase,
        })),
      },
      null,
      2
    ) + '\n'
  );
}

module.exports = { serialise: serialise, OUTPUT_FILE: OUTPUT_FILE };

if (require.main === module) {
  fs.writeFileSync(OUTPUT_FILE, serialise(), 'utf8');
  process.stdout.write(
    'wrote ' + OUTPUT_FILE + ' (' + REQUIREMENTS.length + ' requirements)\n'
  );
}
