// Layer 2 · Topic 6 - Node: one session is one TCP connection, one congestion
// window, and one shared fate.
//
// Node has two HTTP/2 stories. The `http2` module is a manually managed client
// SESSION -- you create it, you keep it, you close it -- and the ergonomics
// push you towards exactly one session per origin for the life of the process.
// undici will negotiate h2 when told to (`allowH2`). Either way the shape is
// the same and it is the shape this topic is about: many streams, one socket.
//
// This program runs the identical fan-out three ways against servers in this
// process:
//
//   h1                  keep-alive HTTP/1.1, an agent with maxSockets = POOL
//   h2, one session     every stream on one connection
//   h2, four sessions   the same streams spread over four connections
//
// The server holds each request for DELAY, so wall time measures how many
// requests were genuinely in flight:  effective = requests x DELAY / wall.
// It advertises SETTINGS_MAX_CONCURRENT_STREAMS = STREAM_LIMIT, which becomes
// the ceiling for the h2 runs -- per session, which is why the third run gets
// four times the concurrency out of the same protocol and the same server.
//
// What to look for in the output:
//   - connections accepted: POOL, then 1, then 4
//   - effective concurrency: the pool size, then the stream limit, then four
//     times the stream limit. The ceiling never went away; it changed owner
//     and then changed multiplier
//   - the failure count in the h2 rows. Node's client opens streams past the
//     advertised limit and the server REFUSES them (REFUSED_STREAM), which
//     surfaces as ERR_HTTP2_STREAM_ERROR. Nothing queued. Compare with httpx,
//     which fails locally before a frame is sent, and with current Go, which
//     dials another connection: three clients, one SETTINGS frame, three
//     completely different things happening to your requests
//
// Not measured here: head-of-line blocking. That needs real packet loss on the
// path and loopback has none -- do that half in the lab with `tc netem`, and
// record "not measured here" rather than inferring it.
//
// Run: node http2_session_is_one_connection.js
'use strict';

const http = require('node:http');
const http2 = require('node:http2');

const REQUESTS = 40;
const DELAY = 250;
const POOL = 8;
const STREAM_LIMIT = 5;
const BODY = 'x'.repeat(1024);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function counters() {
  return { connections: 0, inFlight: 0, maxInFlight: 0, served: 0 };
}

function enter(c) {
  c.inFlight += 1;
  c.served += 1;
  c.maxInFlight = Math.max(c.maxInFlight, c.inFlight);
}

function leave(c) { c.inFlight -= 1; }

function h1Server(c) {
  const server = http.createServer(async (req, res) => {
    enter(c);
    await sleep(DELAY);
    leave(c);
    res.end(BODY);
  });
  server.on('connection', () => { c.connections += 1; });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r(server)));
}

function h2Server(c) {
  // settings.maxConcurrentStreams is the line this whole topic is about: one
  // number, sent by the server, that becomes the client's concurrency limit.
  const server = http2.createServer({ settings: { maxConcurrentStreams: STREAM_LIMIT } });
  server.on('connection', () => { c.connections += 1; });
  server.on('stream', async (stream) => {
    enter(c);
    await sleep(DELAY);
    leave(c);
    stream.respond({ ':status': 200, 'content-length': Buffer.byteLength(BODY) });
    stream.end(BODY);
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r(server)));
}

function h1Request(port, agent) {
  return new Promise((resolve) => {
    const req = http.request({ host: '127.0.0.1', port, path: '/work', agent }, (res) => {
      res.resume();
      res.on('end', () => resolve({ ok: true }));
    });
    req.on('error', (e) => resolve({ ok: false, detail: e.code || e.message }));
    req.end();
  });
}

function h2Request(session) {
  return new Promise((resolve) => {
    const stream = session.request({ ':path': '/work' });
    stream.on('response', () => { });
    stream.resume();
    stream.on('end', () => resolve({ ok: true }));
    stream.on('error', (e) => resolve({ ok: false, detail: e.code || e.message }));
  });
}

function report(label, elapsed, results, c, ceiling) {
  const ok = results.filter((r) => r.ok).length;
  const failed = results.length - ok;
  console.log(`    ${label}`);
  console.log(`      wall time               ${(elapsed / 1000).toFixed(2)} s`);
  console.log(`      succeeded / failed      ${ok} / ${failed}`
            + (failed ? `   first: ${results.find((r) => !r.ok).detail}` : ''));
  console.log(`      connections accepted    ${c.connections}`);
  console.log(`      max concurrent at server${String(c.maxInFlight).padStart(4)}`);
  console.log(`      effective concurrency   ${(ok * DELAY / elapsed).toFixed(1)}`
            + `   (= ${ok} x ${DELAY}ms / ${(elapsed / 1000).toFixed(2)}s)`);
  console.log(`      ceiling                 ${ceiling}`);
  console.log();
}

async function main() {
  console.log('='.repeat(78));
  console.log('Node: an HTTP/2 session is ONE connection, and the limit belongs to the peer');
  console.log('='.repeat(78));
  console.log(`  node ${process.version}`);
  console.log(`  ${REQUESTS} concurrent requests, server holds each for ${DELAY} ms`);
  console.log(`  h1 agent maxSockets ${POOL}   h2 advertised maxConcurrentStreams ${STREAM_LIMIT}`);
  console.log();

  const c1 = counters();
  const s1 = await h1Server(c1);
  const c2 = counters();
  const s2 = await h2Server(c2);
  const port1 = s1.address().port;
  const port2 = s2.address().port;

  console.log('  Three runs, identical workload:');
  console.log();

  // --- h1 ---------------------------------------------------------------
  const agent = new http.Agent({ keepAlive: true, maxSockets: POOL });
  let t0 = Date.now();
  let results = await Promise.all(Array.from({ length: REQUESTS }, () => h1Request(port1, agent)));
  report(`h1, agent maxSockets=${POOL}`, Date.now() - t0, results, c1,
    `${POOL}  (you, in the Agent)`);
  agent.destroy();

  // --- h2, one session --------------------------------------------------
  c2.connections = 0; c2.maxInFlight = 0;
  const session = http2.connect(`http://127.0.0.1:${port2}`);
  await new Promise((r) => session.once('connect', r));
  t0 = Date.now();
  results = await Promise.all(Array.from({ length: REQUESTS }, () => h2Request(session)));
  report('h2, ONE session', Date.now() - t0, results, c2,
    `${STREAM_LIMIT}  (the server, in its SETTINGS frame)`);
  session.close();

  // --- h2, four sessions ------------------------------------------------
  c2.connections = 0; c2.maxInFlight = 0;
  const sessions = await Promise.all(Array.from({ length: 4 }, () => {
    const s = http2.connect(`http://127.0.0.1:${port2}`);
    return new Promise((r) => s.once('connect', () => r(s)));
  }));
  t0 = Date.now();
  results = await Promise.all(Array.from({ length: REQUESTS },
    (_, i) => h2Request(sessions[i % sessions.length])));
  report('h2, FOUR sessions', Date.now() - t0, results, c2,
    `${STREAM_LIMIT} per session x 4 = ${STREAM_LIMIT * 4}`);
  sessions.forEach((s) => s.close());

  s1.close();
  s2.close();

  console.log('  Read the three effective-concurrency numbers together.');
  console.log();
  console.log('    Under h1 you set the ceiling, in your own code, and every connection');
  console.log('    is visible to `ss`. Under h2 the ceiling is a number the server chose,');
  console.log('    per session, invisible to every socket tool you own -- and the only');
  console.log('    lever you have left is HOW MANY SESSIONS YOU OPEN, which is a');
  console.log('    connection pool again, wearing a different word.');
  console.log();
  console.log('    That is the honest summary of "HTTP/2 removes the pool limit": it');
  console.log('    replaces a limit you set and can see with a limit somebody else sets');
  console.log('    and nobody can see, and hands you back the same knob under a new name.');
  console.log();
  console.log('  Now the failures, which are the most useful thing in this output.');
  console.log();
  console.log('    Node\'s client did not queue. It opened every stream immediately, and');
  console.log('    the server refused the ones past its limit -- REFUSED_STREAM on the');
  console.log('    wire, ERR_HTTP2_STREAM_ERROR in your callback. Compare the other two');
  console.log('    files in this topic: httpx fails LOCALLY before a frame is sent');
  console.log('    (LocalProtocolError), and current Go dials ANOTHER CONNECTION. One');
  console.log('    SETTINGS frame, three clients, three completely different things');
  console.log('    happening to your requests. "We use HTTP/2" tells you nothing about');
  console.log('    how a service behaves at its ceiling.');
  console.log();
  console.log('    REFUSED_STREAM specifically is the one error in this topic that is');
  console.log('    SAFE to retry: RFC 9113 defines it to mean the request was not');
  console.log('    processed, which is the guarantee Topic 4\'s 502 could never give you.');
  console.log('    A client that surfaces it as a generic stream error -- as this one');
  console.log('    does -- has turned a retryable backpressure signal into a user-visible');
  console.log('    error. Handle it, or set your own concurrency gate below the peer\'s');
  console.log('    limit and never generate it.');
  console.log();
  console.log('  And the half this program deliberately does not measure: one lost segment');
  console.log('  stalls EVERY stream on a session, because they share one TCP byte stream');
  console.log('  and one congestion window. Loopback has no loss, so that row belongs to');
  console.log('  the lab with `tc netem loss 5%`, not to this file.');
  process.exit(0);
}

main();
