// Layer 2 · Topic 4 - Node: the ordering constraint is internal as well as
// external, and the number you configure is not the number you get.
//
// Every other runtime in this topic has one idle timer to get wrong relative
// to the load balancer's. Node has two IN THE SAME PROCESS --
// `keepAliveTimeout` (how long an idle socket is kept) and `headersTimeout`
// (how long the server waits for a request's headers) -- so a deployment that
// sets one and leaves the other alone can recreate this race with no proxy
// anywhere near it.
//
// The "load balancer" here is a raw net.Socket rather than an http.Agent, on
// purpose: a proxy's pool holds a socket and writes a request onto it, and
// that is exactly what a raw socket lets us do at a moment of our choosing.
// An Agent would helpfully notice the FIN and open a new connection for us,
// which is the bug being hidden rather than the bug being observed.
//
// Four phases:
//   A. The defaults your Node actually ships. Printed, not quoted.
//   B. The external race: server idle timeout shorter than the pool's idle
//      gap. FastAPI-behind-an-ALB in miniature.
//   C. The same code with the ordering corrected.
//   D. When does the FIN actually arrive, measured against the configured
//      value? On this build the answer is not "at keepAliveTimeout", and the
//      gap is the reason this page tells you to measure rather than trust.
//
// What to look for in the output:
//   - phase B: EPIPE / ECONNRESET writing a request onto a socket the peer
//     already closed, and NOTHING on the server side. The server did what it
//     was told. So did the pool. The bug is in the gap between them.
//   - phase C: zero failures from one number changing.
//   - phase D: configured versus observed close time. Record the observed one.
//
// Run: node keepalive_vs_headers_timeout.js
'use strict';

const http = require('node:http');
const net = require('node:net');

// The pool's idle gap is 2000 ms rather than a round 1000 ms for a measured
// reason, not an aesthetic one: phase D below shows this build sending its FIN
// about a second LATER than the configured keepAliveTimeout, so a 1000 ms gap
// against a 300 ms timeout never actually finds a closed socket and the race
// silently does not reproduce. That near-miss is worth more than the fix -- an
// experiment tuned from a documented number instead of a measured one is how
// you get a clean run and a wrong conclusion.
const POOL_IDLE_MS = 2000;
const REQUESTS = 4;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeServer({ keepAliveTimeout, headersTimeout } = {}) {
  const server = http.createServer((req, res) => res.end('ok'));
  if (keepAliveTimeout !== undefined) server.keepAliveTimeout = keepAliveTimeout;
  if (headersTimeout !== undefined) server.headersTimeout = headersTimeout;
  server.stats = { accepted: 0, requests: 0 };
  server.on('connection', () => { server.stats.accepted += 1; });
  server.on('request', () => { server.stats.requests += 1; });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// A one-connection pool: connect, write, read, keep. Exactly what nginx's
// `upstream { keepalive 32; }` does to your backend, minus the bookkeeping.
class PooledConnection {
  constructor(port) {
    this.port = port;
    this.sock = null;
    this.handshakes = 0;
  }

  async connect() {
    // allowHalfOpen matters more than it looks. By default a Node socket
    // destroys itself the instant a FIN arrives, so you can never write into
    // a half-closed connection -- which is a real defence, and also means a
    // Node client cannot reproduce what a proxy holding this socket in a pool
    // experiences. allowHalfOpen: true keeps the socket writable after the
    // peer's FIN, which is precisely the state nginx's pooled connection is in
    // when it picks the connection and writes a request onto it.
    this.sock = net.connect({ port: this.port, host: '127.0.0.1', allowHalfOpen: true });
    this.handshakes += 1;
    this.sock.on('error', () => {});           // errors are read from the write path
    await new Promise((r) => this.sock.once('connect', r));
  }

  async request(path) {
    if (!this.sock || this.sock.destroyed) await this.connect();
    const peerAlreadyClosed = this.sock.readyState === 'writeOnly';
    return new Promise((resolve) => {
      const sock = this.sock;
      const done = (ok, detail) => {
        sock.removeAllListeners('data');
        sock.removeAllListeners('error');
        sock.removeAllListeners('close');
        sock.on('error', () => {});
        if (!ok) { sock.destroy(); this.sock = null; }
        clearTimeout(timer);
        resolve({ ok, detail });
      };
      const timer = setTimeout(() => done(false, 'no response in 2000 ms'), 2000);
      sock.once('data', (d) => done(true, d.toString().split('\r\n')[0]));
      sock.once('error', (e) => done(false, e.code || e.message));
      sock.once('close', () => done(false, 'closed with no response'));
      if (peerAlreadyClosed) {
        // The pool is about to do the thing that produces the 502: write a
        // request onto a connection the peer has already finished with.
        sock.write(`GET ${path} HTTP/1.1\r\nHost: lab\r\n\r\n`, (err) => {
          if (err) done(false, `${err.code || err.message} (wrote into a half-closed socket)`);
        });
        setTimeout(() => done(false, 'no response: peer had sent FIN before we wrote'), 250);
        return;
      }
      sock.write(`GET ${path} HTTP/1.1\r\nHost: lab\r\n\r\n`);
    });
  }

  destroy() { if (this.sock) this.sock.destroy(); }
}

async function runConfig(name, serverOpts, note) {
  const server = await makeServer(serverOpts);
  const pool = new PooledConnection(server.address().port);

  let failures = 0;
  const log = [];
  for (let i = 0; i < REQUESTS; i += 1) {
    const r = await pool.request('/work');
    if (!r.ok) failures += 1;
    log.push(`      request ${i}: ${r.ok ? 'ok ' : '502'}  ${r.detail}`);
    await sleep(POOL_IDLE_MS);       // the idle gap the bug needs
  }
  pool.destroy();

  console.log(`  ${name}`);
  console.log(`    server.keepAliveTimeout  ${server.keepAliveTimeout} ms`);
  console.log(`    server.headersTimeout    ${server.headersTimeout} ms`);
  console.log(`    pool idle gap            ${POOL_IDLE_MS} ms`);
  console.log(`    ordering                 ${note}`);
  log.forEach((l) => console.log(l));
  console.log(`    failures ${failures}/${REQUESTS}   `
            + `connections accepted ${server.stats.accepted}   `
            + `requests served ${server.stats.requests}   `
            + `handshakes ${pool.handshakes}`);
  console.log();

  server.close();
  return failures;
}

// When does the FIN actually arrive? Measured from the moment the response
// was received, on a raw socket, so nothing is inferred.
async function measureFin(label, serverOpts) {
  const server = await makeServer(serverOpts);
  const sock = net.connect(server.address().port, '127.0.0.1');
  sock.on('error', () => {});
  await new Promise((r) => sock.once('connect', r));

  sock.write('GET /work HTTP/1.1\r\nHost: lab\r\n\r\n');
  const advertised = await new Promise((r) => sock.once('data', (d) => r(d.toString())));
  const t0 = Date.now();
  const kaHeader = (advertised.match(/keep-alive: ?([^\r\n]+)/i) || [])[1] || '(none)';

  const finAfter = await new Promise((resolve) => {
    const t = setTimeout(() => resolve(null), 8000);
    sock.once('end', () => { clearTimeout(t); resolve(Date.now() - t0); });
  });
  sock.destroy();
  server.close();

  console.log(`  ${label}`);
  console.log(`    configured keepAliveTimeout   ${server.keepAliveTimeout} ms`);
  console.log(`    configured headersTimeout     ${server.headersTimeout} ms`);
  console.log(`    advertised to the peer        Keep-Alive: ${kaHeader}`);
  console.log(finAfter === null
    ? '    observed FIN                  none within 8000 ms'
    : `    observed FIN                  ${finAfter} ms after the response`);
  console.log();
  return finAfter;
}

async function main() {
  console.log('='.repeat(78));
  console.log('Node: two idle timers in one process, and a third at the load balancer');
  console.log('='.repeat(78));
  console.log();

  const probe = await makeServer();
  console.log('  A. Defaults on THIS Node (read, not quoted):');
  console.log(`       node ${process.version}`);
  console.log(`       server.keepAliveTimeout  ${probe.keepAliveTimeout} ms`);
  console.log(`       server.headersTimeout    ${probe.headersTimeout} ms`);
  console.log(`       server.requestTimeout    ${probe.requestTimeout} ms`);
  console.log('       one-liner for a shell:');
  console.log('         node -e "const s=require(\'http\').createServer();'
            + ' console.log(s.keepAliveTimeout, s.headersTimeout, s.requestTimeout)"');
  console.log('       The load balancer\'s number is not readable from here and must not');
  console.log('       be guessed. You need both numbers or you have neither.');
  console.log();
  probe.close();

  console.log('  B/C. The external race, and the same code with the ordering fixed:');
  console.log();
  const mismatched = await runConfig(
    'mismatched -- server closes idle after 300 ms, pool waits 2000 ms',
    { keepAliveTimeout: 300, headersTimeout: 60000 },
    'server closes first  <-- the bug');
  const ordered = await runConfig(
    'ordered -- server closes idle after 4000 ms, pool waits 2000 ms',
    { keepAliveTimeout: 4000, headersTimeout: 60000 },
    'pool closes first    <-- correct');

  console.log('  D. Configured versus observed: when does the FIN actually arrive?');
  console.log();
  const a = await measureFin('keepAliveTimeout 600 ms',  { keepAliveTimeout: 600,  headersTimeout: 60000 });
  const b = await measureFin('keepAliveTimeout 1500 ms', { keepAliveTimeout: 1500, headersTimeout: 60000 });

  console.log('  Summary');
  console.log(`    mismatched  ${mismatched} failures out of ${REQUESTS}`);
  console.log(`    ordered     ${ordered} failures out of ${REQUESTS}`);
  if (a !== null && b !== null) {
    console.log(`    observed close ran ${a - 600} ms and ${b - 1500} ms past the configured value`);
    console.log('    on this build. Do not record the configured number as the measured');
    console.log('    one. The Keep-Alive header the server advertises is in WHOLE SECONDS,');
    console.log('    so a peer that honours it is working from a rounded value too --');
    console.log('    which is one concrete reason the safety margin between the two sides');
    console.log('    should be seconds, not milliseconds.');
  }
  console.log();
  if (mismatched === 0) {
    console.log('    B produced no failures this run. Before recording that, check the');
    console.log('    observed FIN time in D against the pool idle gap: if the socket was');
    console.log('    still open when the pool reused it, the race never had a chance to');
    console.log('    happen and you have measured nothing.');
    console.log();
  }
  console.log('    The mechanism, when it lands: the server closed an idle');
  console.log('    connection exactly as configured; the pool wrote a request into it');
  console.log('    because it had no way to know, and got EPIPE or ECONNRESET back.');
  console.log('    nginx turns that into a 502 and cannot');
  console.log('    tell your client whether the request was processed -- which is also why');
  console.log('    it must not blindly retry a POST.');
  console.log();
  console.log('    Node\'s own hazard is that there are TWO server-side timers here and');
  console.log('    the smaller one wins. Tuning the obviously-named one and leaving the');
  console.log('    other at its default is how this race gets recreated inside a single');
  console.log('    process with no proxy to blame.');
  process.exit(0);
}

main();
