// Derivation guard for the accessibility requirement registry.
//
// The registry claims to be derived from product/ACCESSIBILITY.md — an
// imported, frozen document this work must never edit. A claim nobody
// checks is not a derivation, so these tests re-parse that document
// INDEPENDENTLY of the registry module's data and assert the two agree
// exactly. If the frozen source ever gains, loses or reworks a listed
// requirement, or if an entry is invented here that the source does not
// state, this suite goes red instead of the merge gate silently widening
// or narrowing.

// Strict mode is required for the freeze test below: in sloppy mode an
// assignment to a frozen property fails SILENTLY instead of throwing.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { REQUIREMENTS, REQUIREMENT_IDS, SOURCE_FILE } = require('./requirement-registry.js');

// Independent re-parse: pull the comma-separated phrases out of a
// "<Heading>: a, b, c." line of product/ACCESSIBILITY.md without consulting
// the registry at all.
function phrasesUnder(heading) {
  const text = fs.readFileSync(SOURCE_FILE, 'utf8');
  const line = text.split('\n').find((l) => l.startsWith(heading + ':'));
  assert.ok(line, 'product/ACCESSIBILITY.md has no "' + heading + ':" line — registry derivation is broken.');
  return line
    .slice(heading.length + 1)
    .replace(/\.\s*$/, '')
    .split(',')
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}

test('every registry entry quotes a phrase that is literally in product/ACCESSIBILITY.md', () => {
  const text = fs.readFileSync(SOURCE_FILE, 'utf8');
  for (const req of REQUIREMENTS) {
    assert.ok(
      text.includes(req.source_phrase),
      req.id + ' cites "' + req.source_phrase + '", which does not appear in product/ACCESSIBILITY.md.'
    );
  }
});

test('the definition-of-done group covers that source list exactly — no additions, no omissions', () => {
  const fromSource = phrasesUnder('Definition of done').slice().sort();
  const fromRegistry = REQUIREMENTS.filter((r) => r.group === 'definition-of-done')
    .map((r) => r.source_phrase)
    .sort();
  assert.deepEqual(fromRegistry, fromSource);
});

test('the cognitive group covers that source list exactly — no additions, no omissions', () => {
  const fromSource = phrasesUnder('Cognitive accessibility').slice().sort();
  const fromRegistry = REQUIREMENTS.filter((r) => r.group === 'cognitive')
    .map((r) => r.source_phrase)
    .sort();
  assert.deepEqual(fromRegistry, fromSource);
});

test('the registry covers both source lists and nothing else', () => {
  const fromSource = phrasesUnder('Definition of done').concat(phrasesUnder('Cognitive accessibility')).sort();
  const fromRegistry = REQUIREMENTS.map((r) => r.source_phrase).sort();
  assert.deepEqual(fromRegistry, fromSource);
  assert.equal(REQUIREMENTS.length, fromSource.length);
});

test('identifiers are unique, semantic (not positional), and stable in shape', () => {
  assert.equal(new Set(REQUIREMENT_IDS).size, REQUIREMENT_IDS.length, 'duplicate identifier in the registry');
  for (const id of REQUIREMENT_IDS) {
    assert.match(id, /^ACC-(DOD|COG)-[A-Z0-9_]+$/, id + ' does not match the registry identifier shape');
    assert.doesNotMatch(id, /-\d+$/, id + ' looks positional; positional ids silently re-point when the source reorders');
  }
});

test('the registry is frozen so a caller cannot widen the gate at runtime', () => {
  assert.throws(() => {
    REQUIREMENT_IDS.push('ACC-DOD-ANYTHING_I_LIKE');
  }, TypeError);
  assert.throws(() => {
    REQUIREMENTS[0].id = 'ACC-DOD-TAMPERED';
  }, TypeError);
  assert.ok(!REQUIREMENT_IDS.includes('ACC-DOD-ANYTHING_I_LIKE'));
  assert.equal(REQUIREMENTS[0].id, 'ACC-DOD-SEMANTIC_HTML');
});
