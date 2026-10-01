// The fixture products serve BYTE COPIES of the fixture pages run.test.js
// proves the individual checks against. If those copies drift, the local
// fixture scan stops being evidence about the same pages, and would do so
// silently — a passing scan of a page nobody had checked.
//
// Checked in both directions, so neither "someone edited the served copy"
// nor "someone edited the fixture and forgot the copy" can pass.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const HERE = __dirname;
const PAIRS = [
  ['clean', path.join(HERE, 'fixtures', 'clean.html'),
    path.join(HERE, 'fixture-product', 'clean', 'index.html')],
  ['violations', path.join(HERE, 'fixtures', 'violations.html'),
    path.join(HERE, 'fixture-product', 'violations', 'index.html')],
];

for (const [name, fixture, served] of PAIRS) {
  test(`the ${name} fixture product serves a byte copy of the ${name} fixture`, () => {
    const a = fs.readFileSync(fixture);
    const b = fs.readFileSync(served);
    assert.ok(a.equals(b),
      `${served} has drifted from ${fixture}; re-copy it rather than editing one side`);
  });
}

test('each fixture product declares exactly the governed npm contract', () => {
  for (const [name] of PAIRS) {
    const dir = path.join(HERE, 'fixture-product', name);
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    // control/accessibility_evidence.py runs `npm ci`, `npm run build` and
    // `npm run start`. A fixture missing either script would fail the
    // attempt for a reason that has nothing to do with accessibility.
    assert.strictEqual(typeof pkg.scripts.build, 'string', `${name}: no build script`);
    assert.strictEqual(typeof pkg.scripts.start, 'string', `${name}: no start script`);
    // Zero dependencies is what makes `npm ci` work with no network.
    assert.strictEqual(pkg.dependencies, undefined, `${name}: must have no dependencies`);
    assert.strictEqual(pkg.devDependencies, undefined, `${name}: must have no devDependencies`);
    assert.ok(fs.existsSync(path.join(dir, 'package-lock.json')),
      `${name}: npm ci requires a committed lockfile`);
  }
});

test('no package.json sits at the repository root', () => {
  // PRODUCT_ENTRYPOINT is resolved relative to the attempt's checkout
  // ROOT. A root package.json would be picked up by a real attempt and
  // scanned as if it were the product. These fixtures must not become
  // that, and neither must anything else.
  const root = path.resolve(HERE, '..', '..');
  assert.ok(!fs.existsSync(path.join(root, 'package.json')),
    'a root package.json would be mistaken for the product entrypoint');
});
