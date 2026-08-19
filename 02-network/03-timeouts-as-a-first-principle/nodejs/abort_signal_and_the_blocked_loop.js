// Layer 2 · Topic 3 - Node: a timeout is a timer on the event loop, and a
// timer on the event loop cannot fire while the loop is blocked.
//
// undici's headersTimeout and bodyTimeout are on the order of five minutes,
// so the per-call answer in Node is AbortSignal.timeout(ms) handed to fetch().
// That works, and it has one failure mode no other runtime in this topic
// shares: the timeout you configured is a LOWER BOUND, not a bound, because
// the timer that would fire it is queued behind whatever else is running on
// the single thread. That is Layer 1's blocking-the-loop failure wearing a
// timeout costume, and phase D below reproduces it.
//
// Four questions, all measured against a real server in this process:
//
//   A. Does the budget shrink as you go deeper?   (three sequential hops)
//   B. When the timeout fires, what happens to the request already in flight
//      at the server?
//   C. Is the connection reusable afterwards?
//   D. What does a blocked event loop do to a 100 ms timeout?
//
// What to look for in the output:
//   - phase A: hop 3 is never started, because starting it would produce an
//     answer that arrives after the caller has given up
//   - phase B: the server's FINISHED count rises for the request the client
//     abandoned -- abort is a message to your own code, not to the server
//   - phase C: the connection count rises across the aborted request
//   - phase D: a 100 ms timeout that fires hundreds of milliseconds late.
//     No network was involved in that delay
//
// Run: node abort_signal_and_the_blocked_loop.js
'use strict';

const http = require('node:http');

const SLOW_MS = 400;          // how long the server holds /slow
const OUTER_BUDGET_MS = 900;  // what we promised our caller
const RESERVE_MS = 100;       // held back for writing our own response
const PER_HOP_CAP_MS = 500;   // the flat library-default value

const counters = { accepted: 0, started: 0, finished: 0 };

function startServer() {
  const server = http.createServer((req, res) => {
    if (req.url.startsWith('/slow')) {
      counters.started += 1;
      // No 'aborted'/'close' handling on purpose. A handler that ignores
      // client disconnects keeps working on requests nobody is waiting for,
      // which is how a retry storm turns into sustained load. Most handlers
      // in the wild look exactly like this one.
      setTimeout(() => {
        counters.finished += 1;
        res.end('slow ok');
      }, SLOW_MS);
      return;
    }
    res.end('fast ok');
  });
  server.on('connection', () => { counters.accepted += 1; });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, base: `http://127.0.0.1:${port}` });
    });
  });
}

// The pattern itself: an absolute instant, a reserve never spent upstream,
// and a per-call cap. Ten lines, and it is the entire topic.
class Deadline {
  constructor(totalMs, reserveMs) {
    this.expiresAt = Date.now() + totalMs;
    this.reserveMs = reserveMs;
  }
  remaining() { return this.expiresAt - Date.now(); }
  forCall(capMs) { return Math.min(this.remaining() - this.reserveMs, capMs); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function phaseA(base) {
  console.log('A. A budget, spent down three sequential hops');
  console.log(`    promised to our caller     ${String(OUTER_BUDGET_MS).padStart(5)} ms`);
  console.log(`    reserved for our own work  ${String(RESERVE_MS).padStart(5)} ms`);
  console.log(`    each hop's flat default    ${String(PER_HOP_CAP_MS).padStart(5)} ms  <- what a flat config would use\n`);

  const dl = new Deadline(OUTER_BUDGET_MS, RESERVE_MS);
  const t0 = Date.now();

  for (let hop = 1; hop <= 3; hop += 1) {
    const slice = dl.forCall(PER_HOP_CAP_MS);
    if (slice <= 0) {
      console.log(`    hop ${hop}  slice ${String(slice).padStart(6)} ms  -> NOT STARTED: its answer would arrive`);
      console.log('                              after our caller has stopped waiting. Failing here');
      console.log('                              is correct, and it is the line people skip.');
      break;
    }
    let outcome = 'ok';
    try {
      await fetch(`${base}/slow`, { signal: AbortSignal.timeout(slice) });
    } catch (err) {
      outcome = `${err.name} (${err.message})`;
    }
    console.log(`    hop ${hop}  slice ${String(slice).padStart(6)} ms  -> ${outcome}  (${Date.now() - t0} ms elapsed, ${dl.remaining()} ms left)`);
  }

  console.log(`\n    total spent ${Date.now() - t0} ms against a ${OUTER_BUDGET_MS} ms promise, ${dl.remaining()} ms left to answer`);
  console.log(`    A flat ${PER_HOP_CAP_MS} ms per hop would have spent ${PER_HOP_CAP_MS * 3} ms on three hops.`);
}

async function phaseB(base) {
  console.log('\nB. What a fired timeout does to the request already in flight');
  await sleep(SLOW_MS + 100);              // let phase A's abandoned hop land first
  const before = counters.finished;

  const t0 = Date.now();
  let err = null;
  try {
    await fetch(`${base}/slow`, { signal: AbortSignal.timeout(100) });
  } catch (e) {
    err = e;
  }
  console.log(`    client gave up after   ${Date.now() - t0} ms`);
  console.log(`    error name             ${err && err.name}   <- TimeoutError, distinguishable from a user abort`);

  await sleep(SLOW_MS + 200);
  console.log(`    server FINISHED this request anyway: ${before} -> ${counters.finished}`);
  console.log('    Aborting is a message to your own code. The socket was destroyed, but');
  console.log('    the handler on the far side had already been entered and ran to');
  console.log('    completion holding whatever it was holding.');
}

async function phaseC(base) {
  console.log('\nC. Is the connection reusable after the timeout fires?');
  // Sequential requests, and several of them, because fetch() goes through
  // undici's per-origin Pool: a single follow-up can land on a different idle
  // socket and tell you nothing. What we are counting is whether the origin
  // ever has to hand out a NEW connection afterwards.
  for (let i = 0; i < 3; i += 1) await fetch(`${base}/fast`);
  const warm = counters.accepted;
  console.log(`    connections accepted once the pool has settled       ${warm}`);

  try {
    await fetch(`${base}/slow`, { signal: AbortSignal.timeout(100) });
  } catch { /* expected */ }
  const afterTimeout = counters.accepted;

  for (let i = 0; i < 3; i += 1) await fetch(`${base}/fast`);
  const after = counters.accepted;

  console.log(`    ...after one timed-out request                      ${afterTimeout}`);
  console.log(`    ...after three more successful requests             ${after}`);

  const reused = after === warm;
  if (reused) {
    console.log('    No new connection was needed. Do NOT read that as "the aborted socket');
    console.log('    survived": undici destroys the socket it abandoned, and the pool');
    console.log('    simply had another idle one to hand out. The cost is still a socket,');
    console.log('    it was just paid earlier and by somebody else. Point this same phase');
    console.log('    at a pool of one and the handshake reappears.');
  } else {
    console.log(`    ${after - warm} new connection(s) were opened. undici destroyed the socket whose`);
    console.log('    response it abandoned mid-flight -- it can no longer find where the');
    console.log('    next response begins on that byte stream -- so the timeout cost a');
    console.log('    handshake on top of the wait. An aggressive timeout against a slow');
    console.log('    dependency therefore produces MORE load, not less.');
  }
  return reused;
}

async function phaseD(base) {
  console.log('\nD. The same 100 ms timeout, with the event loop blocked');
  const t0 = Date.now();
  const p = fetch(`${base}/slow`, { signal: AbortSignal.timeout(100) })
    .then(() => 'ok')
    .catch((e) => e.name);

  // 600 ms of synchronous CPU. Nothing exotic: a big JSON.parse, a hand-rolled
  // hash, a sync crypto call. The timer that should fire at t=100 ms cannot be
  // reached until this returns.
  const blockUntil = Date.now() + 600;
  let spin = 0;
  while (Date.now() < blockUntil) spin += 1;

  const outcome = await p;
  const elapsed = Date.now() - t0;
  console.log(`    configured timeout     100 ms`);
  console.log(`    actually fired after   ${elapsed} ms   (${outcome}, after ${spin} spins of blocking CPU)`);
  console.log('    No network was involved in that difference. Your timeout is a LOWER');
  console.log('    bound in Node: the timer queues behind whatever is running on the one');
  console.log('    thread. Every deadline you compute is only as good as your slowest');
  console.log('    synchronous block, which is Layer 1 Topic 3 arriving here in disguise.');
}

async function main() {
  const { server, base } = await startServer();

  console.log('='.repeat(78));
  console.log('Node: AbortSignal timeouts, and the one thing that makes them not fire');
  console.log('='.repeat(78));
  console.log(`  server holds /slow for ${SLOW_MS} ms`);
  console.log('  undici headersTimeout/bodyTimeout are on the order of MINUTES; check');
  console.log('  yours with: node -p "new (require(\'undici\').Agent)().constructor.name"');
  console.log('  and the undici docs for your release, then stop relying on them.\n');

  await phaseA(base);
  await phaseB(base);
  const reused = await phaseC(base);
  await phaseD(base);

  console.log('\n  For this topic\'s table:');
  console.log('    what a fired timeout does to the in-flight request:');
  console.log('      destroys the client socket; the server handler runs to completion.');
  console.log('    connection reused after?');
  console.log(reused
    ? '      the aborted socket is destroyed, but the pool had a spare, so no new'
    : '      no; a new connection had to be opened.');
  console.log(reused
    ? '      handshake was visible in THIS run. Re-run against a pool of one.'
    : '      The timeout cost a handshake as well as the wait.');
  console.log('    ...and the timer itself is only as punctual as your event loop (phase D).');

  server.close();
  process.exit(0);
}

main();
