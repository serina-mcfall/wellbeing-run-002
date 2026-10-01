// The fixture product's server.
//
// PORT is REQUIRED and never defaulted. The port belongs to the attempt's
// claim (G2), so a server that silently picked its own would either bind a
// port nobody claimed or leave the claimed one free while readiness polled
// it forever. Exiting is the honest failure.
//
// It binds 127.0.0.1 only: this fixture must never be reachable from off
// the host.
const http = require('http');
const fs = require('fs');
const path = require('path');

const port = Number.parseInt(process.env.PORT, 10);
if (!Number.isInteger(port) || port <= 0) {
  process.stderr.write('PORT must be set to a positive integer\n');
  process.exit(2);
}

const page = fs.readFileSync(path.join(__dirname, 'index.html'));

// Every path serves the page. The scan navigates to "/" and the control
// plane's readiness probe accepts any 2xx-4xx, so routing would add
// nothing but a way to get a 404 at the wrong moment.
http
  .createServer((req, res) => {
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    res.end(page);
  })
  .listen(port, '127.0.0.1');
