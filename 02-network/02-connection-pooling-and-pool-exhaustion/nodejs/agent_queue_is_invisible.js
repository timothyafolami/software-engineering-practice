// Layer 2 · Topic 2 - Node: the queue is real, bounded, and nowhere in
// your metrics.
//
// Node is in this topic for one specific reason. When an http.Agent hits
// maxSockets, the excess requests do not fail and do not open sockets --
// they go into `agent.requests`, a plain object of arrays keyed by
// host:port, inside the agent, in your process. Nothing logs it. No
// counter exports it. The request has been "sent" as far as your code is
// concerned, and it has not been sent at all.
//
// That is the same shape as SQLAlchemy's QueuePool wait and Go's
// MaxConnsPerHost wait, with one difference that matters operationally:
// an http.Agent request sitting in that queue is NOT covered by the
// socket timeout, because there is no socket yet. The clock you think you
// set has not started.
//
// This program saturates a small agent, then reads the queue depth out of
// the agent while the requests are still queued, so you can see the thing
// your dashboards cannot.
//
// What to look for in the output:
//   - sockets in use vs requests queued, sampled mid-flight
//   - the timeout run: how many requests time out, and which clock did it
//
// Run: node agent_queue_is_invisible.js

'use strict';

const http = require('node:http');

const CONCURRENCY = 40;
const MAX_SOCKETS = 4;
const HOLD_MS = 300;

let connectionsOpened = 0;

const server = http.createServer((req, res) => {
  setTimeout(() => {
    const body = '{"ok":true}';
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': body.length });
    res.end(body);
  }, HOLD_MS);
});
server.on('connection', () => {
  connectionsOpened += 1;
});

function agentDepth(agent) {
  const sockets = Object.values(agent.sockets).reduce((n, list) => n + list.length, 0);
  const free = Object.values(agent.freeSockets).reduce((n, list) => n + list.length, 0);
  const queued = Object.values(agent.requests).reduce((n, list) => n + list.length, 0);
  return { sockets, free, queued };
}

function requestOnce(agent, options, timeoutMs) {
  return new Promise((resolve) => {
    const started = process.hrtime.bigint();
    const done = (outcome) => resolve({ outcome, ms: Number(process.hrtime.bigint() - started) / 1e6 });

    const req = http.get({ ...options, agent }, (res) => {
      res.resume();
      res.on('end', () => done('ok'));
    });
    req.on('error', (err) => done(err.code || err.message));

    if (timeoutMs) {
      // req.setTimeout is a SOCKET timeout: it starts when the socket is
      // assigned, which for a queued request is not now. That is the trap.
      req.setTimeout(timeoutMs, () => {
        req.destroy(new Error('socket-timeout'));
      });
    }
  });
}

async function run(label, agentOptions, timeoutMs, wallClockDeadlineMs) {
  const agent = new http.Agent(agentOptions);
  const options = { hostname: '127.0.0.1', port: server.address().port, path: '/slow' };
  const before = connectionsOpened;
  const started = Date.now();

  const samples = [];
  const sampler = setInterval(() => samples.push({ t: Date.now() - started, ...agentDepth(agent) }), 100);

  const inFlight = Array.from({ length: CONCURRENCY }, () => {
    const p = requestOnce(agent, options, timeoutMs);
    if (!wallClockDeadlineMs) return p;
    // A real deadline, started when the REQUEST was created rather than when
    // a socket happened to become available. This is the fix for the trap
    // above, and it is the only clock that matches what a caller experiences.
    return Promise.race([
      p,
      new Promise((resolve) =>
        setTimeout(() => resolve({ outcome: 'deadline-exceeded', ms: wallClockDeadlineMs }), wallClockDeadlineMs)
      ),
    ]);
  });

  const results = await Promise.all(inFlight);
  clearInterval(sampler);
  agent.destroy();

  const byOutcome = results.reduce((acc, r) => {
    acc[r.outcome] = (acc[r.outcome] || 0) + 1;
    return acc;
  }, {});
  const latencies = results.map((r) => r.ms).sort((a, b) => a - b);
  const at = (f) => latencies[Math.min(latencies.length - 1, Math.floor(latencies.length * f))];

  console.log(`  ${label}`);
  console.log(`    requests fired          ${CONCURRENCY}`);
  console.log(`    TCP connections opened  ${connectionsOpened - before}`);
  console.log(`    outcomes                ${JSON.stringify(byOutcome)}`);
  console.log(`    latency p50 ${at(0.5).toFixed(0)} ms   p95 ${at(0.95).toFixed(0)} ms   max ${at(1).toFixed(0)} ms`);
  console.log('    agent internals sampled every 100 ms (t ms: sockets/free/QUEUED):');
  const line = samples
    .slice(0, 12)
    .map((s) => `${s.t}:${s.sockets}/${s.free}/${s.queued}`)
    .join('  ');
  console.log(`      ${line}`);
}

async function main() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));

  console.log('='.repeat(78));
  console.log('Node: an http.Agent queue you cannot see from outside the agent');
  console.log('='.repeat(78));
  console.log(`  node ${process.version}   upstream holds each request ${HOLD_MS} ms`);
  console.log(`  ${CONCURRENCY} requests at once, agent maxSockets ${MAX_SOCKETS}\n`);

  await run(`1. SATURATED - maxSockets ${MAX_SOCKETS}, no timeout anywhere`,
    { keepAlive: true, maxSockets: MAX_SOCKETS }, 0, 0);

  console.log();
  await run(`2. SOCKET TIMEOUT 500 ms - the timeout most people set`,
    { keepAlive: true, maxSockets: MAX_SOCKETS }, 500, 0);

  console.log();
  await run(`3. REQUEST DEADLINE 500 ms - a clock that starts when the caller asked`,
    { keepAlive: true, maxSockets: MAX_SOCKETS }, 0, 500);

  console.log();
  console.log('  Compare runs 2 and 3.');
  console.log('    Run 2 sets req.setTimeout(500). That timer starts when a socket is');
  console.log('    ASSIGNED. A request that spends 2 s in agent.requests and then 300 ms');
  console.log('    on the wire never trips a 500 ms socket timeout, because it was only');
  console.log('    ever 300 ms on a socket. The caller waited 2.3 s.');
  console.log('    Run 3 races the whole operation against a deadline created when the');
  console.log('    request was, which is the only clock that matches the caller.');
  console.log('    In undici/fetch the equivalent is AbortSignal.timeout(ms) passed as');
  console.log('    `signal`, NOT headersTimeout/bodyTimeout -- same distinction.');
  console.log();
  console.log('  The other pool you probably have, for reference (node-postgres):');
  console.log('    pg.Pool defaults: max 10, idleTimeoutMillis 10000,');
  console.log('    connectionTimeoutMillis 0 -- and 0 means WAIT FOREVER for a');
  console.log('    connection. Same invisible queue, different library.');

  server.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
