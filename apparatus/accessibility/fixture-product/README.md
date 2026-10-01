# Accessibility fixture products — NOT the Run 002 product

Two disposable npm-shaped applications whose only purpose is to be built,
served and scanned by the **real** `control/accessibility_services.py`
pipeline, so that pipeline has actually run before a product PR depends on
it.

| Directory | Serves | Expected verdict |
|---|---|---|
| `clean/` | `apparatus/accessibility/fixtures/clean.html` | `ACCESSIBILITY_AUTO_PASS` |
| `violations/` | `apparatus/accessibility/fixtures/violations.html` | `ACCESSIBILITY_AUTO_FAIL` |

## Why they are shaped like this

`control/accessibility_evidence.py` requires `package.json` and
`package-lock.json` at the checkout root and runs the governed commands
`npm ci`, `npm run build`, `npm run start` with `PORT` in the environment.
Those commands are named by the apparatus, not by the PR, so a fixture that
wants to exercise the real services has to answer to them exactly. These
two do, with **zero dependencies** — `npm ci` therefore needs no network
and completes in well under a second.

## Why they are here and not at the repository root

`PRODUCT_ENTRYPOINT` and `PRODUCT_LOCKFILE` are resolved relative to the
attempt's isolated checkout **root**. A `package.json` at the repository
root would be found by a real attempt and scanned as if it were TASK-001's
Next.js app. There is deliberately no root `package.json` in this
repository — `apparatus/package.json` keeps the root free for the product —
and these fixtures must not be the thing that changes that.

The tests reach them by pointing `ProductServices` at
`<worktree>/apparatus/accessibility/fixture-product/<variant>`, so the
fixture is still read **out of a detached worktree at an exact commit**:
the provenance path under test is the real one.

## The pages are copies, deliberately

`index.html` in each directory is a byte copy of the corresponding file in
`../fixtures/`. `fixture-product-pages.test.js` asserts that, in both
directions, so the copies cannot drift from the fixtures `run.test.js`
proves the individual checks against. A symlink would not survive
`git worktree add` on every platform the apparatus may run on, and a build
step that copied the file would mean the served page was not the committed
page.

## They are not run by anything else

Nothing dispatches these. They are driven only by
`tests/test_c05_3c_local_fixture_scan.py`, which is skipped unless
`RUN_002_LOCAL_FIXTURE_SCAN=1` is set, because the scan launches a real
Chromium and takes tens of seconds.
