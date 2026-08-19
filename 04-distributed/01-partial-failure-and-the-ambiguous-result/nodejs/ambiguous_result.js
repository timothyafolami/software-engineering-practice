// Layer 4 · Topic 1 — the third outcome, in Node.js.
//
// WHAT THIS DEMONSTRATES
//   Node is the sharpest version of this topic for one reason: `fetch` has no
//   default timeout. Not a long one -- none. A dependency that accepts your
//   connection and then says nothing holds your handler until the OS gives up on
//   the socket, which on macOS and Linux is minutes. Phase 0 measures that
//   directly against a server that is deliberately silent.
//
//   Phases 1 and 2 then run the same five faults through the same client with
//   the same timeout, differing only in retry policy: phase 1 retries on any
//   error, phase 2 retries only errors that prove the request never landed.
//
// WHAT TO LOOK FOR
//   1. Phase 0: the request is still outstanding when the watchdog fires. Nothing
//      in the fetch call said "wait forever" -- that is just the default.
//   2. The `AbortSignal` note under phase 0. AbortSignal.timeout is the right fix
//      for the hang, but it is a deadline on the whole operation, so it reports
//      one TimeoutError whether it fired during the TCP handshake or halfway
//      through reading a response body. Hard errors like ECONNREFUSED still come
//      through on `cause`; timeouts -- the common case -- do not say which phase
//      they hit. That is why phases 1 and 2 use node:http, which does.
//   3. Phase 1's duplicate charges vs phase 2's, and the ambiguity that survives
//      both.
//
// Zero dependencies: node:net for the fault server, node:http for the client.

'use strict';

const net = require('node:net');
const http = require('node:http');

const CLIENT_TIMEOUT_MS = 300;   // the deadline the caller is willing to wait
const SLOW_RESPONSE_MS = 1000;   // deliberately longer than CLIENT_TIMEOUT_MS
const NO_TIMEOUT_WATCHDOG_MS = 3000;
const REQUESTS_PER_MODE = 4;
const MAX_ATTEMPTS = 3;

const MODES = [
  ['ok', 'no fault'],
  ['slow', 'commits, then replies after the client has given up'],
  ['hang', 'commits, then never replies at all (blackhole)'],
  ['reset', 'commits, then RSTs the connection'],
  ['crash_after_commit', 'commits, then dies before writing a byte of response'],
  ['refused', 'nothing is listening; the request provably never landed'],
];

// --- server-side truth ------------------------------------------------------

const ledger = [];              // every accepted charge, duplicates included

function startLedgerServer() {
  const held = [];              // `hang` sockets, destroyed at shutdown
  const server = net.createServer((socket) => {
    socket.once('data', (buf) => {
      const line = buf.toString('latin1').split('\r\n')[0];
      const path = line.split(' ')[1] || '';
      const [, , mode, chargeId] = path.split('/');

      const reply = (status = '200 OK') => {
        const body = JSON.stringify({ charge_id: chargeId });
        socket.end(
          `HTTP/1.1 ${status}\r\nContent-Type: application/json\r\n` +
          `Content-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`
        );
      };

      switch (mode) {
        case 'ok':
          ledger.push(chargeId);
          reply();
          break;
        case 'slow':
          ledger.push(chargeId);
          setTimeout(reply, SLOW_RESPONSE_MS);
          break;
        case 'hang':
          ledger.push(chargeId);
          held.push(socket);            // accepted, committed, never answered
          break;
        case 'reset':
          ledger.push(chargeId);
          // destroy() on a socket with unread data sends RST rather than FIN.
          socket.resetAndDestroy();
          break;
        case 'crash_after_commit':
          // The case no timeout tuning can fix: durable work, dead reporter.
          ledger.push(chargeId);
          socket.destroy();
          break;
        default:
          reply('400 Bad Request');
      }
    });
    socket.on('error', () => { /* RSTs we caused ourselves */ });
  });
  server.held = held;
  return server;
}

// --- classification ---------------------------------------------------------
//
// The line is not "connection errors are safe, timeouts are not". It is "did we
// get far enough to have put the request bytes on the wire".
function classify(err) {
  const code = err && err.code;
  if (code === 'ECONNREFUSED' || code === 'EHOSTUNREACH' || code === 'ENOTFOUND') {
    return ['SAFE', code];            // never left this machine
  }
  if (code === 'ETIMEDOUT' && err.phase === 'connect') {
    return ['SAFE', 'connect ETIMEDOUT'];
  }
  if (err && err.phase === 'response') {
    return ['AMBIGUOUS', 'response timeout'];
  }
  if (code === 'ECONNRESET') {
    return ['AMBIGUOUS', 'ECONNRESET'];
  }
  if (err && err.message === 'socket hang up') {
    return ['AMBIGUOUS', 'socket hang up'];   // server closed before responding
  }
  // Anything unrecognised is ambiguous. Defaulting the other way is how
  // duplicate charges get shipped.
  return ['AMBIGUOUS', code || (err && err.message) || 'unknown'];
}

// One request with an explicit response deadline. node:http rather than fetch
// because it separates the connect phase from the response phase, and that
// separation *is* the safe/unsafe decision.
function request(port, path) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: '127.0.0.1', port, path, agent: false },
      (res) => {
        res.resume();
        res.on('end', () => resolve(['SUCCESS', String(res.statusCode)]));
      }
    );
    let connected = false;
    req.on('socket', (socket) => {
      socket.on('connect', () => { connected = true; });
    });
    req.setTimeout(CLIENT_TIMEOUT_MS, () => {
      const err = new Error('timeout');
      err.code = 'ETIMEDOUT';
      err.phase = connected ? 'response' : 'connect';
      req.destroy(err);
    });
    req.on('error', (err) => {
      if (err.code === 'ETIMEDOUT' && err.phase === undefined) {
        err.phase = connected ? 'response' : 'connect';
      }
      resolve(classify(err));
    });
    req.end();
  });
}

// --- phase 0: the Node-specific headline ------------------------------------

async function phaseZero(port) {
  console.log('');
  console.log('  phase 0 — fetch() with no timeout, against a server that never replies');
  const started = Date.now();
  const url = `http://127.0.0.1:${port}/charge/hang/phase0-0`;

  const controller = new AbortController();
  const inFlight = fetch(url, { signal: controller.signal })
    .then(() => 'resolved')
    .catch((e) => `rejected: ${e.name}`);

  const watchdog = new Promise((r) =>
    setTimeout(() => r('STILL PENDING'), NO_TIMEOUT_WATCHDOG_MS));

  const outcome = await Promise.race([inFlight, watchdog]);
  const waited = Date.now() - started;
  console.log(`  after ${waited}ms the fetch is: ${outcome}`);
  console.log('  Nothing in that call asked to wait forever. That is the default,');
  console.log('  and every one of these pins a request slot and a socket.');
  controller.abort();
  await inFlight;

  // Why the obvious fix is not enough on its own.
  const connErr = await fetch(`http://127.0.0.1:${CLOSED_PORT}/x`,
    { signal: AbortSignal.timeout(CLIENT_TIMEOUT_MS) }).catch((e) => e);
  const readErr = await fetch(`http://127.0.0.1:${port}/charge/hang/phase0-1`,
    { signal: AbortSignal.timeout(CLIENT_TIMEOUT_MS) }).catch((e) => e);
  console.log('');
  console.log('  AbortSignal.timeout fixes the hang. What it does not give you:');
  console.log(`    connect to a closed port : ${connErr.name}` +
    `${connErr.cause ? ' (cause ' + connErr.cause.code + ')' : ''}`);
  console.log(`    silent server, mid-read  : ${readErr.name}`);
  console.log('  A hard refusal still identifies itself through `cause`. A timeout');
  console.log('  does not say which phase it fired in -- the same TimeoutError means');
  console.log('  "the handshake never finished" (safe) and "I sent the request and');
  console.log('  gave up waiting" (not safe). Phases 1 and 2 use node:http, which');
  console.log('  keeps the connect phase and the response phase apart.');
}

// --- phases 1 and 2 ---------------------------------------------------------

async function runPhase(tag, name, note, port, retryAmbiguous) {
  const before = ledger.length;
  const perMode = [];

  console.log('');
  console.log(`  ${name}`);
  console.log(`  ${note}`);
  console.log(`  ${'fault'.padEnd(20)}${'client verdict'.padEnd(34)}` +
    `${'attempts'.padStart(9)}${'ledger rows'.padStart(12)}`);

  for (const [mode] of MODES) {
    const modeBefore = ledger.length;
    const verdicts = new Map();
    let attempts = 0;
    const targetPort = mode === 'refused' ? CLOSED_PORT : port;

    for (let i = 0; i < REQUESTS_PER_MODE; i++) {
      const chargeId = `${tag}-${mode}-${i}`;
      let verdict, label;
      for (let a = 0; a < MAX_ATTEMPTS; a++) {
        attempts++;
        [verdict, label] = await request(targetPort, `/charge/${mode}/${chargeId}`);
        if (verdict === 'SUCCESS') break;
        if (verdict === 'SAFE') continue;          // provably safe: try again
        if (retryAmbiguous) continue;              // the bug, made explicit
        break;                                     // correct: stop, escalate
      }
      const key = `${verdict}(${label})`;
      verdicts.set(key, (verdicts.get(key) || 0) + 1);
    }

    const rows = ledger.length - modeBefore;
    const summary = [...verdicts].map(([v, n]) => `${n}x ${v}`).join(', ');
    console.log(`  ${mode.padEnd(20)}${summary.padEnd(34)}` +
      `${String(attempts).padStart(9)}${String(rows).padStart(12)}`);
    perMode.push(verdicts);
  }

  const written = ledger.slice(before);
  const counts = new Map();
  for (const id of written) counts.set(id, (counts.get(id) || 0) + 1);
  const duplicates = [...counts.values()].reduce((s, n) => s + (n > 1 ? n - 1 : 0), 0);
  const unresolved = perMode.reduce((s, v) => s +
    [...v].filter(([k]) => k.startsWith('AMBIGUOUS')).reduce((a, [, n]) => a + n, 0), 0);

  console.log(`  ledger rows written this phase : ${written.length}`);
  console.log(`  DUPLICATE CHARGES              : ${duplicates}` +
    '   <- created by this client\'s retries');
  console.log(`  unresolved ambiguous outcomes  : ${unresolved}` +
    '   <- caller cannot tell whether these happened');
  return { duplicates, unresolved };
}

// --- main -------------------------------------------------------------------

let CLOSED_PORT = 0;

function findClosedPort() {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function main() {
  const server = startLedgerServer();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  CLOSED_PORT = await findClosedPort();

  console.log('='.repeat(78));
  console.log('Layer 4 · Topic 1 — partial failure and the ambiguous result (Node.js)');
  console.log('='.repeat(78));
  console.log(`  ledger        : 127.0.0.1:${port}  (in-process, holds server-side truth)`);
  console.log(`  closed port   : 127.0.0.1:${CLOSED_PORT}  (for the connect-refused case)`);
  console.log(`  client timeout: ${CLIENT_TIMEOUT_MS}ms   slow response: ${SLOW_RESPONSE_MS}ms   ` +
    `max attempts: ${MAX_ATTEMPTS}`);

  await phaseZero(port);
  const naive = await runPhase(
    'p1',
    'phase 1 — retry on any error',
    'the `catch (e) { retry() }` most Node codebases have',
    port, true);
  const fixed = await runPhase(
    'p2',
    'phase 2 — retry only provably-safe errors',
    'ECONNREFUSED and connect-phase timeouts are retried; everything else escalates',
    port, false);

  console.log('');
  console.log('-'.repeat(78));
  console.log(`  duplicate charges    phase 1: ${String(naive.duplicates).padEnd(6)}` +
    `phase 2: ${fixed.duplicates}`);
  console.log(`  unresolved ambiguity phase 1: ${String(naive.unresolved).padEnd(6)}` +
    `phase 2: ${fixed.unresolved}`);
  console.log('');
  console.log('  The fix removes the duplicates the client was causing. It does not');
  console.log('  remove the ambiguity -- nothing at this layer can. Topic 2 is what');
  console.log('  makes retrying an ambiguous outcome safe.');

  for (const s of server.held) s.destroy();
  server.close();
}

main();
