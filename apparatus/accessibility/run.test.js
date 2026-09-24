const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');
const { runAccessibilityChecks, walkTabOrder } = require('./run');

const SHA_A = 'a'.repeat(40);
const SHA_B = 'b'.repeat(40);
const CLEAN_URL = pathToFileURL(path.join(__dirname, 'fixtures', 'clean.html')).href;
const VIOLATIONS_URL = pathToFileURL(path.join(__dirname, 'fixtures', 'violations.html')).href;
const OUT_DIR = path.join(__dirname, 'artifacts', 'test-run');

function resultFor(checks, checkId) {
  const found = checks.find((c) => c.check_id === checkId);
  assert.ok(found, `expected a ${checkId} check result`);
  return found;
}

test('every check result matches the schema shape (check_id, result, sha, artifact_reference)', async () => {
  const { checks } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  assert.equal(checks.length, 9);
  for (const c of checks) {
    assert.equal(typeof c.check_id, 'string');
    assert.ok(c.result === 'PASS' || c.result === 'FAIL');
    assert.equal(c.sha, SHA_A);
    assert.equal(typeof c.artifact_reference, 'string');
    assert.ok(c.artifact_reference.length > 0);
  }
});

test('the clean fixture passes every check', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  const failing = checks.filter((c) => c.result !== 'PASS');
  assert.deepEqual(
    failing.map((c) => c.check_id),
    [],
    'unexpected failures: ' + JSON.stringify(failing.map((c) => ({ id: c.check_id, detail: details[c.check_id] })), null, 2)
  );
});

test('RESPONSIVE_375PX: the violations fixture still loads cleanly at 375px (this fixture has no viewport-load defect)', async () => {
  const { checks } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'RESPONSIVE_375PX').result, 'PASS');
});

test('NO_OVERFLOW_CLIPPING_OVERLAP: a 2000px-wide box at 375px viewport fails, real scrollWidth measured', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'NO_OVERFLOW_CLIPPING_OVERLAP').result, 'FAIL');
  assert.ok(details.NO_OVERFLOW_CLIPPING_OVERLAP.scrollWidth > details.NO_OVERFLOW_CLIPPING_OVERLAP.clientWidth);
});

test('TOUCH_TARGETS: a 10x10px button fails the 24px minimum, real bounding rect measured', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'TOUCH_TARGETS').result, 'FAIL');
  assert.ok(details.TOUCH_TARGETS.offenders.some((o) => o.width < 24 || o.height < 24));
});

test('KEYBOARD_OPERATION: a div[role=button] with no keydown handler does not activate on Enter/Space', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'KEYBOARD_OPERATION').result, 'FAIL');
  assert.ok(details.KEYBOARD_OPERATION.offenders.some((o) => o.reason === 'NOT_ACTIVATABLE_VIA_KEYBOARD'));
});

test('FOCUS_ORDER_VISIBLE_NO_TRAPS: a real keydown-preventDefault trap is caught by real Tab presses, not by count', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  const result = resultFor(checks, 'FOCUS_ORDER_VISIBLE_NO_TRAPS');
  assert.equal(result.result, 'FAIL');
  assert.equal(details.FOCUS_ORDER_VISIBLE_NO_TRAPS.forward.trapped, true);
  assert.ok(details.FOCUS_ORDER_VISIBLE_NO_TRAPS.forward.visitedCount < details.FOCUS_ORDER_VISIBLE_NO_TRAPS.focusableCount);
});

test('FOCUS_ORDER_VISIBLE_NO_TRAPS: the clean fixture reaches every element forward and backward with a visible indicator', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  const result = resultFor(checks, 'FOCUS_ORDER_VISIBLE_NO_TRAPS');
  assert.equal(result.result, 'PASS');
  const d = details.FOCUS_ORDER_VISIBLE_NO_TRAPS;
  assert.equal(d.forward.trapped, false);
  assert.equal(d.backward.trapped, false);
  assert.equal(d.forward.visitedCount, d.focusableCount);
  assert.equal(d.backward.visitedCount, d.focusableCount - 1);
  assert.equal(d.noVisibleIndicatorCount, 0);
});

test('LABELS_AND_TEXT_ERRORS: an unlabeled input and an invalid-without-error-text input both fail', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'LABELS_AND_TEXT_ERRORS').result, 'FAIL');
  const reasons = details.LABELS_AND_TEXT_ERRORS.offenders.map((o) => o.reason);
  assert.ok(reasons.includes('NO_ACCESSIBLE_NAME'));
  assert.ok(reasons.includes('INVALID_WITHOUT_TEXT_ERROR'));
});

test('REDUCED_MOTION_NO_FLASHING_AUTOPLAY: three distinct real signals are each independently caught', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'REDUCED_MOTION_NO_FLASHING_AUTOPLAY').result, 'FAIL');
  const d = details.REDUCED_MOTION_NO_FLASHING_AUTOPLAY;
  assert.ok(d.ignoresReducedMotion.length > 0, 'the 3s spin animation should still be running under reduced motion');
  assert.ok(d.rapidCycling.length > 0, 'the 0.3s flash-badge animation should trip the rapid-cycling proxy');
  assert.ok(d.autoplaying.length > 0, 'the autoplay video should be detected');
  assert.match(d.limitation, /not a pixel-luminance measurement/);
});

test('REDUCED_MOTION_NO_FLASHING_AUTOPLAY: the clean fixture disables its animation under reduced motion and has no autoplay', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'REDUCED_MOTION_NO_FLASHING_AUTOPLAY').result, 'PASS');
  const d = details.REDUCED_MOTION_NO_FLASHING_AUTOPLAY;
  assert.equal(d.ignoresReducedMotion.length, 0);
  assert.equal(d.rapidCycling.length, 0);
  assert.equal(d.autoplaying.length, 0);
});

test('AXE_SCAN: an image with no alt attribute is a real axe-core violation', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: VIOLATIONS_URL, sha: SHA_B, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'AXE_SCAN').result, 'FAIL');
  assert.ok(details.AXE_SCAN.violations.some((v) => v.id === 'image-alt'));
});

test('AXE_SCAN: the clean fixture has no axe-core violations', async () => {
  const { checks } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  assert.equal(resultFor(checks, 'AXE_SCAN').result, 'PASS');
});

test('SCREENSHOTS_ARTIFACTS: real, non-empty PNG files are written to outDir', async () => {
  const { checks, details } = await runAccessibilityChecks({ url: CLEAN_URL, sha: SHA_A, outDir: OUT_DIR });
  const result = resultFor(checks, 'SCREENSHOTS_ARTIFACTS');
  assert.equal(result.result, 'PASS');
  const { viewportPath, fullPagePath } = details.SCREENSHOTS_ARTIFACTS;
  assert.ok(fs.statSync(viewportPath).size > 0);
  assert.ok(fs.statSync(fullPagePath).size > 0);
});

test('SCREENSHOTS_ARTIFACTS: an unwritable outDir fails closed, not silently ok', async (t) => {
  const blockedDir = path.join(OUT_DIR, 'blocked');
  fs.mkdirSync(blockedDir, { recursive: true });
  fs.chmodSync(blockedDir, 0o500); // no write permission
  t.after(() => fs.chmodSync(blockedDir, 0o700));
  const { checks } = await runAccessibilityChecks({
    url: CLEAN_URL,
    sha: SHA_A,
    outDir: path.join(blockedDir, 'nested'),
  });
  assert.equal(resultFor(checks, 'SCREENSHOTS_ARTIFACTS').result, 'FAIL');
});

test('rejects a non-40-hex sha rather than silently accepting it', async () => {
  await assert.rejects(() => runAccessibilityChecks({ url: CLEAN_URL, sha: 'not-a-sha', outDir: OUT_DIR }));
});

test('walkTabOrder: reaching every expected distinct target stops immediately — real wraparound is never pressed into, so it is never misread as a trap', async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    // A correct, intentional circular tab order (the same pattern a
    // modal dialog's focus trap legitimately uses): C's keydown sends
    // focus back to A. If the walker took one more step than it needed,
    // it would observe A a second time and could misread that as a
    // trap. window.__aFocusCount proves whether that extra step ever
    // actually happened.
    await page.setContent(`
      <button id="a">A</button>
      <button id="b">B</button>
      <button id="c">C</button>
      <script>
        window.__aFocusCount = 0;
        document.getElementById('a').addEventListener('focus', () => { window.__aFocusCount++; });
        document.getElementById('c').addEventListener('keydown', (e) => {
          if (e.key === 'Tab' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('a').focus();
          }
        });
      </script>
    `);
    const result = await walkTabOrder(page, 'Tab', 3, 3 + 3);
    assert.equal(result.trapped, false);
    assert.equal(result.visited.size, 3);
    const aFocusCount = await page.evaluate(() => window.__aFocusCount);
    assert.equal(aFocusCount, 1, 'the walker must stop the instant it reaches 3 distinct targets, before the wraparound press to A ever happens');
  } finally {
    await browser.close();
  }
});

test('walkTabOrder: A->B->A before the expected count is reached is still a genuine trap', async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    // B's keydown bounces back to A instead of advancing to C — a real
    // defect, not wraparound, because only 2 of the 3 expected targets
    // have been reached when the repeat occurs.
    await page.setContent(`
      <button id="a">A</button>
      <button id="b">B</button>
      <button id="c">C</button>
      <script>
        document.getElementById('b').addEventListener('keydown', (e) => {
          if (e.key === 'Tab' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('a').focus();
          }
        });
      </script>
    `);
    const result = await walkTabOrder(page, 'Tab', 3, 6);
    assert.equal(result.trapped, true);
    assert.ok(result.visited.size < 3);
  } finally {
    await browser.close();
  }
});
