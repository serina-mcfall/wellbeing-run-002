// Automated accessibility evidence pipeline (B1 — Accessibility
// Worker/Reviewer, Protocol v2 "Accessibility gate"). Real Chromium via
// Playwright, real axe-core scan — no simulated/stubbed DOM checks.
//
// Produces nine of the ten check_ids in protocol/PR-EVIDENCE-V2.schema.json's
// accessibilityCheck enum: RESPONSIVE_375PX, NO_OVERFLOW_CLIPPING_OVERLAP,
// TOUCH_TARGETS, KEYBOARD_OPERATION, FOCUS_ORDER_VISIBLE_NO_TRAPS,
// LABELS_AND_TEXT_ERRORS, REDUCED_MOTION_NO_FLASHING_AUTOPLAY, AXE_SCAN,
// SCREENSHOTS_ARTIFACTS. The tenth, COGNITIVE_SENSORY_REVIEW, is the
// qualitative human/LLM review in prompts/accessibility.md — this module
// does not attempt it.
//
// This module returns raw check results and diagnostic detail only. It
// does NOT allocate TASK###-R#-A11Y-### finding IDs — that belongs to
// whatever assembles a PR evidence package (not yet built; no product PR
// exists pre-T+00), and inventing one here would be exactly the kind of
// fabricated capability Protocol v2's anti-cheating rule forbids. A FAIL
// result from this module is not yet schema-valid PR evidence until a
// real finding ID is attached downstream.

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const AxeBuilder = require('@axe-core/playwright').default;

const VIEWPORT = { width: 375, height: 667 };
const MIN_TOUCH_TARGET_PX = 24; // WCAG 2.2 SC 2.5.8 Target Size (Minimum), Level AA
const RAPID_CYCLE_MAX_SECONDS = 1; // see checkReducedMotion's limitation note
const INTERACTIVE_SELECTOR =
  'a[href], button, input:not([type=hidden]), select, textarea, [role="button"], [role="link"], [tabindex]:not([tabindex="-1"])';

// C-09 identifies a process by (pid, start_ticks), never by pid alone -
// a reused pid cannot fake the start time. This is the same value
// control/proc.py::start_ticks reads: field 22 of /proc/<pid>/stat, taken
// after the LAST ')' because comm (field 2) may itself contain spaces and
// parentheses. Null when unreadable - never a guess.
function startTicks(pid) {
  if (!pid || pid <= 0) return null;
  try {
    const text = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    const fields = text.slice(text.lastIndexOf(')') + 1).trim().split(/\s+/);
    const ticks = parseInt(fields[19], 10);
    return Number.isNaN(ticks) ? null : ticks;
  } catch (err) {
    return null;
  }
}

// Written via a temp file and renamed, so a kill mid-write can never leave
// a torn sidecar that the control plane would misread as "no browser here".
//
// An unwritable outDir must not crash the run - checkScreenshots already
// reports that condition as a FAIL, and turning it into a throw would
// lose every other check result. It returns false instead, and the
// control plane records the sidecar's ABSENCE explicitly: a missing
// sidecar means "unknown", never "no browser was launched".
function writeSidecar(sidecarPath, record) {
  try {
    fs.mkdirSync(path.dirname(sidecarPath), { recursive: true });
    const tmp = sidecarPath + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(record, null, 2) + '\n');
    fs.renameSync(tmp, sidecarPath);
    return true;
  } catch (err) {
    return false;
  }
}

// The sidecar is written OPEN immediately after launch and only marked
// CLOSED after browser.close() has actually returned. If this process is
// killed at any point in between - a hard timeout, a crash - the file
// stays OPEN and still carries the identity needed to find the browser
// that was left behind.
// Playwright's Browser exposes no process handle (only BrowserServer does,
// and launching a server would bind a websocket port that C-09's listener
// reconciliation would then have to account for). chromium.launch() spawns
// the browser as a DIRECT CHILD of this process, so /proc/self/task/*/
// children names it in one read - no process table scan, no port.
//
// Ambiguity is never resolved by guessing: zero or more than one candidate
// returns null, and the sidecar then records an honestly unknown identity
// rather than a plausible wrong pid.
function browserPid() {
  const kids = [];
  for (const tid of fs.readdirSync('/proc/self/task')) {
    try {
      kids.push(...fs.readFileSync(`/proc/self/task/${tid}/children`, 'utf8')
        .trim().split(/\s+/).filter(Boolean).map(Number));
    } catch (err) {
      // a thread that exited between readdir and read names no children
    }
  }
  const browsers = kids.filter((pid) => {
    try {
      return /chrome|chromium|headless/i.test(
        fs.readFileSync(`/proc/${pid}/comm`, 'utf8'));
    } catch (err) {
      return false;
    }
  });
  return browsers.length === 1 ? browsers[0] : null;
}

async function withPage(url, sidecarPath, fn) {
  const browser = await chromium.launch();
  const pid = browserPid();
  const record = {
    pid,
    start_ticks: startTicks(pid),
    state: 'OPEN',
    opened_at: new Date().toISOString(),
    closed_at: null,
  };
  writeSidecar(sidecarPath, record);
  try {
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', (err) => pageErrors.push(String(err)));
    let response = null;
    try {
      response = await page.goto(url, { waitUntil: 'load' });
    } catch (err) {
      pageErrors.push(String(err));
    }
    return await fn(page, { response, pageErrors });
  } finally {
    await browser.close();
    writeSidecar(sidecarPath, {
      ...record, state: 'CLOSED', closed_at: new Date().toISOString(),
    });
  }
}

async function checkResponsive375(page, ctx) {
  const viewport = page.viewportSize();
  const clientWidth = await page.evaluate(() => document.documentElement.clientWidth);
  const responseOk = ctx.response === null || ctx.response.ok();
  const ok = responseOk && ctx.pageErrors.length === 0 && viewport.width === 375 && clientWidth === 375;
  return {
    result: ok ? 'PASS' : 'FAIL',
    detail: { viewport, clientWidth, responseOk, pageErrors: ctx.pageErrors },
  };
}

async function checkOverflow(page) {
  const { scrollWidth, clientWidth, offenders } = await page.evaluate(() => {
    const doc = document.documentElement;
    const offenders = [];
    document.querySelectorAll('body *').forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.right > doc.clientWidth + 1) {
        offenders.push({ tag: el.tagName, id: el.id || null, right: rect.right });
      }
    });
    return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, offenders };
  });
  const ok = scrollWidth <= clientWidth + 1 && offenders.length === 0;
  return { result: ok ? 'PASS' : 'FAIL', detail: { scrollWidth, clientWidth, offenders } };
}

async function checkTouchTargets(page) {
  const offenders = await page.evaluate((min) => {
    const results = [];
    document
      .querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role="button"]')
      .forEach((el) => {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;
        const rect = el.getBoundingClientRect();
        if (rect.width < min || rect.height < min) {
          results.push({
            tag: el.tagName,
            text: (el.textContent || '').trim().slice(0, 40),
            width: rect.width,
            height: rect.height,
          });
        }
      });
    return results;
  }, MIN_TOUCH_TARGET_PX);
  return { result: offenders.length === 0 ? 'PASS' : 'FAIL', detail: { offenders, minimum_px: MIN_TOUCH_TARGET_PX } };
}

async function checkKeyboardOperation(page) {
  const offenders = [];
  const handles = await page.$$(INTERACTIVE_SELECTOR);
  for (const handle of handles) {
    const info = await handle.evaluate((el) => ({
      tag: el.tagName,
      role: el.getAttribute('role'),
      text: (el.textContent || '').trim().slice(0, 40),
    }));
    await handle.focus();
    const isFocused = await handle.evaluate((el) => document.activeElement === el);
    if (!isFocused) {
      offenders.push({ ...info, reason: 'NOT_FOCUSABLE' });
      continue;
    }
    const isButtonLike = info.tag === 'BUTTON' || info.role === 'button';
    if (!isButtonLike) continue;

    await handle.evaluate((el) => {
      el.__clicked = false;
      el.addEventListener('click', () => { el.__clicked = true; }, { once: true });
    });
    // Real, trusted key events (not a synthetic dispatchEvent from JS —
    // untrusted events do not trigger a native <button>'s default
    // activation, so this must go through the real keyboard.
    await page.keyboard.press('Enter');
    const clickedOnEnter = await handle.evaluate((el) => el.__clicked === true);

    await handle.focus();
    await handle.evaluate((el) => {
      el.__clicked = false;
      el.addEventListener('click', () => { el.__clicked = true; }, { once: true });
    });
    await page.keyboard.press(' ');
    const clickedOnSpace = await handle.evaluate((el) => el.__clicked === true);

    if (!clickedOnEnter || !clickedOnSpace) {
      offenders.push({ ...info, reason: 'NOT_ACTIVATABLE_VIA_KEYBOARD', clickedOnEnter, clickedOnSpace });
    }
  }
  return { result: offenders.length === 0 ? 'PASS' : 'FAIL', detail: { offenders } };
}

// Walks the page's real tab order using actual keyboard input (never
// el.focus()) for up to `maxSteps` presses of `key` ("Tab" or
// "Shift+Tab"), sampling document.activeElement after each real press.
// Stops the instant `expectedCount` distinct real targets have been
// observed — success, returned immediately. This is what keeps normal
// tab-order wraparound (a dialog's last element correctly cycling back
// to its first) from ever being pressed into and misread as a trap: the
// walk simply never takes that extra step once every expected target is
// already accounted for. A repeated real target seen again BEFORE
// `expectedCount` distinct targets have been reached is still a genuine
// trap, since in that case wraparound cannot be the explanation.
//
// Chromium can land on document.body as a transitional no-op step (for
// example, the very first press right after a script-driven blur())
// before a later press reaches a real element. That is not "focus left
// the page" and must not be read as evidence of anything — it is
// skipped without counting toward visited and without stopping the
// loop. `maxSteps` carries a small fixed slack over `expectedCount`
// specifically to absorb it.
async function walkTabOrder(page, key, expectedCount, maxSteps) {
  const order = [];
  const visited = new Set();
  const focusSamples = [];
  let trapped = false;
  for (let i = 0; i < maxSteps; i++) {
    await page.keyboard.press(key);
    const sample = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      const style = getComputedStyle(el);
      return {
        id: el.tagName + '#' + (el.id || '') + ':' + Array.prototype.indexOf.call(document.querySelectorAll('*'), el),
        focusVisible: typeof el.matches === 'function' && el.matches(':focus-visible'),
        outlineNone: style.outlineStyle === 'none' || style.outlineWidth === '0px',
        boxShadowNone: !style.boxShadow || style.boxShadow === 'none',
      };
    });
    if (sample === null) continue; // no real element currently focused — not evidence of a trap or of reaching the end
    if (visited.has(sample.id)) {
      trapped = true;
      break;
    }
    visited.add(sample.id);
    order.push(sample.id);
    focusSamples.push(sample);
    if (visited.size >= expectedCount) break; // every expected target reached — stop now, do not press into slack
  }
  return { order, visited, focusSamples, trapped };
}

async function checkFocusOrderAndTraps(page) {
  await page.evaluate(() => { if (document.activeElement) document.activeElement.blur(); });
  const focusableCount = (await page.$$(INTERACTIVE_SELECTOR)).length;
  if (focusableCount === 0) {
    return { result: 'PASS', detail: { focusableCount: 0, note: 'no focusable elements to traverse' } };
  }

  // Small fixed slack absorbs a transitional body-focus step only; it
  // does not relax how many DISTINCT real elements must be reached
  // (walkTabOrder stops the instant expectedCount is hit), so a cycle
  // that revisits the same elements still cannot satisfy "reached all".
  const STEP_SLACK = 3;

  // Forward: starts from "nothing focused", so all N real Tab presses
  // must land on N distinct elements.
  const forward = await walkTabOrder(page, 'Tab', focusableCount, focusableCount + STEP_SLACK);
  // Backward: starts already sitting on the last forward element (not
  // re-seeded programmatically), so it only needs to reach the other
  // N-1 elements via real Shift+Tab presses.
  const backwardExpected = Math.max(focusableCount - 1, 0);
  const backward = await walkTabOrder(page, 'Shift+Tab', backwardExpected, backwardExpected + STEP_SLACK);

  const forwardReachedAll = forward.visited.size >= focusableCount;
  const backwardReachedAll = backward.visited.size >= Math.max(focusableCount - 1, 0);

  const allSamples = [...forward.focusSamples, ...backward.focusSamples];
  const noVisibleIndicator = allSamples.filter((s) => !s.focusVisible && s.outlineNone && s.boxShadowNone);

  const ok =
    !forward.trapped &&
    !backward.trapped &&
    forwardReachedAll &&
    backwardReachedAll &&
    noVisibleIndicator.length === 0;

  return {
    result: ok ? 'PASS' : 'FAIL',
    detail: {
      focusableCount,
      forward: { visitedCount: forward.visited.size, order: forward.order, trapped: forward.trapped },
      backward: { visitedCount: backward.visited.size, order: backward.order, trapped: backward.trapped },
      noVisibleIndicatorCount: noVisibleIndicator.length,
    },
  };
}

async function checkLabelsAndTextErrors(page) {
  const offenders = await page.evaluate(() => {
    const offenders = [];
    const hasNonEmptyReferencedText = (idList) =>
      !!idList &&
      idList
        .split(/\s+/)
        .filter(Boolean)
        .some((id) => {
          const target = document.getElementById(id);
          return target && target.textContent.trim().length > 0;
        });

    document.querySelectorAll('input:not([type=hidden]), select, textarea').forEach((el) => {
      const hasAriaLabel = !!(el.getAttribute('aria-label') || '').trim();
      const hasAriaLabelledby = hasNonEmptyReferencedText(el.getAttribute('aria-labelledby'));
      const hasWrappingOrForLabel = !!(el.labels && el.labels.length > 0);
      if (!hasAriaLabel && !hasAriaLabelledby && !hasWrappingOrForLabel) {
        offenders.push({ tag: el.tagName, name: el.getAttribute('name') || null, reason: 'NO_ACCESSIBLE_NAME' });
      }
      if (el.getAttribute('aria-invalid') === 'true') {
        const hasErrorText = hasNonEmptyReferencedText(el.getAttribute('aria-describedby'));
        if (!hasErrorText) {
          offenders.push({ tag: el.tagName, name: el.getAttribute('name') || null, reason: 'INVALID_WITHOUT_TEXT_ERROR' });
        }
      }
    });
    return offenders;
  });
  return { result: offenders.length === 0 ? 'PASS' : 'FAIL', detail: { offenders } };
}

// Three separately-measured signals, none inferred from another:
//   1. ignoresReducedMotion — a long-running animation/transition that
//      keeps running once prefers-reduced-motion:reduce is emulated.
//   2. rapidCycling — a CSS animation-timing PROXY for flashing/pulsing:
//      an effectively-infinite animation with a short duration. This is
//      NOT a pixel-luminance measurement of WCAG 2.3.1's exact "more
//      than three flashes per second" threshold — that needs frame-by-
//      frame rendered-pixel analysis, which this module does not do.
//      The schema's check result is PASS/FAIL only, so this sub-signal
//      cannot be reported as a separate NOT_AUTOMATED state; it is
//      instead honestly scoped and documented rather than silently
//      folded into a claimed-complete flash-detection PASS.
//   3. autoplaying — an actual autoplay video/audio element.
async function checkReducedMotion(page) {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.waitForTimeout(50);
  const { ignoresReducedMotion, rapidCycling, autoplaying } = await page.evaluate((maxSeconds) => {
    const ignoresReducedMotion = [];
    const rapidCycling = [];
    document.querySelectorAll('*').forEach((el) => {
      const style = getComputedStyle(el);
      const animationDuration = parseFloat(style.animationDuration) || 0;
      const transitionDuration = parseFloat(style.transitionDuration) || 0;
      const hasAnimation = style.animationName && style.animationName !== 'none';
      if (animationDuration > 0.5 || transitionDuration > 0.5) {
        ignoresReducedMotion.push({
          tag: el.tagName,
          id: el.id || null,
          animationDuration: style.animationDuration,
          transitionDuration: style.transitionDuration,
        });
      }
      const isEffectivelyInfinite =
        style.animationIterationCount === 'infinite' || parseFloat(style.animationIterationCount) > 20;
      if (hasAnimation && isEffectivelyInfinite && animationDuration > 0 && animationDuration <= maxSeconds) {
        rapidCycling.push({ tag: el.tagName, id: el.id || null, animationDuration: style.animationDuration });
      }
    });
    const autoplaying = Array.from(document.querySelectorAll('video[autoplay], audio[autoplay]')).map((el) => ({
      tag: el.tagName,
      id: el.id || null,
    }));
    return { ignoresReducedMotion, rapidCycling, autoplaying };
  }, RAPID_CYCLE_MAX_SECONDS);
  const ok = ignoresReducedMotion.length === 0 && rapidCycling.length === 0 && autoplaying.length === 0;
  return {
    result: ok ? 'PASS' : 'FAIL',
    detail: {
      ignoresReducedMotion,
      rapidCycling,
      autoplaying,
      limitation:
        'rapidCycling is a CSS animation-timing proxy (effectively-infinite iteration, duration <= ' +
        RAPID_CYCLE_MAX_SECONDS +
        's), not a pixel-luminance measurement of WCAG 2.3.1\'s exact flash-rate threshold.',
    },
  };
}

async function checkAxe(page) {
  const results = await new AxeBuilder({ page }).analyze();
  const violations = results.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length }));
  return { result: violations.length === 0 ? 'PASS' : 'FAIL', detail: { violations } };
}

async function checkScreenshots(page, sha, outDir) {
  try {
    fs.mkdirSync(outDir, { recursive: true });
    const viewportPath = path.join(outDir, sha + '-viewport-375.png');
    const fullPagePath = path.join(outDir, sha + '-fullpage.png');
    await page.screenshot({ path: viewportPath });
    await page.screenshot({ path: fullPagePath, fullPage: true });
    const okSizes = fs.statSync(viewportPath).size > 0 && fs.statSync(fullPagePath).size > 0;
    return {
      result: okSizes ? 'PASS' : 'FAIL',
      artifact_reference: viewportPath + ',' + fullPagePath,
      detail: { viewportPath, fullPagePath },
    };
  } catch (err) {
    return { result: 'FAIL', artifact_reference: outDir, detail: { error: String(err) } };
  }
}

const SHA_RE = /^[0-9a-f]{40}$/;

async function runAccessibilityChecks({ url, sha, outDir, outJson }) {
  if (typeof url !== 'string' || url.trim().length === 0) {
    throw new Error('url is required');
  }
  if (typeof sha !== 'string' || !SHA_RE.test(sha)) {
    throw new Error('sha must be a full 40-character lowercase-hex git SHA');
  }
  if (typeof outDir !== 'string' || outDir.trim().length === 0) {
    throw new Error('outDir is required');
  }

  return withPage(url, path.join(outDir, 'browser.json'), async (page, ctx) => {
    const results = {};
    results.RESPONSIVE_375PX = await checkResponsive375(page, ctx);
    results.NO_OVERFLOW_CLIPPING_OVERLAP = await checkOverflow(page);
    results.TOUCH_TARGETS = await checkTouchTargets(page);
    results.KEYBOARD_OPERATION = await checkKeyboardOperation(page);
    results.FOCUS_ORDER_VISIBLE_NO_TRAPS = await checkFocusOrderAndTraps(page);
    results.LABELS_AND_TEXT_ERRORS = await checkLabelsAndTextErrors(page);
    results.REDUCED_MOTION_NO_FLASHING_AUTOPLAY = await checkReducedMotion(page);
    results.AXE_SCAN = await checkAxe(page);
    results.SCREENSHOTS_ARTIFACTS = await checkScreenshots(page, sha, outDir);

    const genericArtifact = results.SCREENSHOTS_ARTIFACTS.artifact_reference;
    const checks = Object.entries(results).map(([check_id, r]) => ({
      check_id,
      result: r.result,
      sha,
      artifact_reference: r.artifact_reference || genericArtifact,
    }));
    const details = Object.fromEntries(Object.entries(results).map(([k, r]) => [k, r.detail]));
    const payload = { checks, details };
    // Complete result to a file, not to stdout: the control plane's
    // bounded runner keeps only a ledger-sized scrubbed tail of stdout,
    // and a truncated tail is not evidence.
    if (typeof outJson === 'string' && outJson.trim().length > 0) {
      fs.mkdirSync(path.dirname(outJson), { recursive: true });
      fs.writeFileSync(outJson, JSON.stringify(payload, null, 2) + '\n');
    }
    return payload;
  });
}

module.exports = {
  runAccessibilityChecks,
  walkTabOrder,
  startTicks,
  browserPid,
  MIN_TOUCH_TARGET_PX,
  RAPID_CYCLE_MAX_SECONDS,
  VIEWPORT,
  INTERACTIVE_SELECTOR,
};

if (require.main === module) {
  const argv = process.argv.slice(2);
  const flagAt = argv.indexOf('--out-json');
  const outJson = flagAt === -1 ? undefined : argv[flagAt + 1];
  const [url, sha, outDirArg] = flagAt === -1 ? argv : argv.slice(0, flagAt);
  const outDir = outDirArg || path.join(__dirname, 'artifacts');
  runAccessibilityChecks({ url, sha, outDir, outJson })
    .then((result) => {
      process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    })
    .catch((err) => {
      process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
      process.exitCode = 1;
    });
}
