// Drift guard for the generated requirement-registry.json.
//
// requirement-registry.test.js pins the JS registry to the frozen
// product/ACCESSIBILITY.md. This file pins the committed JSON to that JS
// registry, so the Python control plane reads exactly what the canonical
// module says and a stale committed artefact turns the suite red instead
// of quietly feeding control/ an out-of-date identifier set.
//
// Why a byte comparison and not just a deep-equal of the parsed data: the
// committed file is what Python reads, so the committed BYTES are the
// thing that has to be current. A deep-equal would pass on a file whose
// content was right but whose generator had changed shape, and the next
// regeneration would then produce a surprise diff.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');

const { REQUIREMENTS, REQUIREMENT_IDS } = require('./requirement-registry.js');
const { serialise, OUTPUT_FILE } = require('./generate-requirement-registry-json.js');

test('the committed JSON is byte-identical to what the generator produces now', () => {
  const committed = fs.readFileSync(OUTPUT_FILE, 'utf8');
  assert.equal(
    committed,
    serialise(),
    'requirement-registry.json is stale. Regenerate it:\n' +
      '  node apparatus/accessibility/generate-requirement-registry-json.js'
  );
});

test('the JSON carries every canonical requirement, in source order, and nothing else', () => {
  const parsed = JSON.parse(fs.readFileSync(OUTPUT_FILE, 'utf8'));
  assert.deepEqual(
    parsed.requirements,
    REQUIREMENTS.map((r) => ({
      id: r.id,
      group: r.group,
      source_phrase: r.source_phrase,
    }))
  );
  assert.deepEqual(
    parsed.requirements.map((r) => r.id),
    REQUIREMENT_IDS.slice()
  );
});

test('the JSON is parseable and has no duplicate identifier', () => {
  const parsed = JSON.parse(fs.readFileSync(OUTPUT_FILE, 'utf8'));
  const ids = parsed.requirements.map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length, 'duplicate identifier in the generated JSON');
  assert.equal(ids.length, 17, 'the frozen source states 17 requirements');
  for (const id of ids) {
    assert.match(id, /^ACC-(DOD|COG)-[A-Z0-9_]+$/, id + ' does not match the registry identifier shape');
  }
});
