// The fixture product's build step.
//
// It has to do something real, because `install_build` distinguishes
// PRODUCT_INSTALL_FAILED from PRODUCT_BUILD_FAILED by which command
// exited non-zero, and a build that cannot fail proves neither branch is
// reachable. This one fails exactly when the page it is supposed to serve
// is missing — which is also the only way this fixture can be broken.
const fs = require('fs');
const path = require('path');

const page = path.join(__dirname, 'index.html');
if (!fs.existsSync(page)) {
  process.stderr.write('build failed: index.html is missing\n');
  process.exit(1);
}
process.stdout.write('build ok\n');
