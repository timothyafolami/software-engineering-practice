// Layer 10 - Topic 2: does hanging up actually free the KV blocks? (Node.js)
//
// What this demonstrates
//     The same experiment as python/cancel_propagation.py, in the runtime
//     where the answer depends on an AbortController you remembered to
//     wire up. A stub model server streams 40 tokens at 100ms each while
//     watching for its caller to leave. A gateway sits in front with two
//     handlers, and a client hangs up after 500ms against each:
//
//       /naive       `await fetch(upstream)` then `await r.arrayBuffer()`.
//                    No signal, so nothing connects the downstream socket
//                    closing to the upstream request. Generation runs to
//                    completion for a response that is discarded.
//       /cancelling  an AbortController aborted from `req.on('close')`,
//                    passed to fetch as `signal`. The abort tears down the
//                    upstream socket, and the engine sees EOF.
//
// What to look for
//     - /naive: upstream decodes all 40 tokens, ~3.5s of it after the
//       client stopped listening -- KV blocks held the whole time.
//     - /cancelling: upstream sees EOF within a token or two of the hang-up.
//     - The Node-specific caveat, which this file cannot show you but which
//       decides your real cancellation latency: `req.on('close')` is an
//       event-loop callback. Any synchronous CPU-bound stretch on the main
//       thread delays it along with everything else, so cancellation
//       latency here is bounded below by your worst blocking call. That is
//       Layer 1's lesson, arriving with a bill attached.
//
// No dependencies. Runs with no arguments, binds 127.0.0.1 only:
//     node nodejs/cancel_propagation.js

'use strict';

const http = require('node:http');
const net = require('node:net');

const TOKENS = 40;
const TOKEN_INTERVAL_MS = 100;        // 4.0s of "decode" in total
const CLIENT_HANGS_UP_AFTER_MS = 500;

const ledger = [];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// The stub model server. Streams tokens; notices when its caller goes away.
// ---------------------------------------------------------------------------
const upstream = http.createServer(async (req, res) => {
  const started = process.hrtime.bigint();
  let sent = 0;
  let peerGone = false;

  // Fires when the connection closes, whether or not the response finished.
  res.on('close', () => {
    if (!res.writableFinished) peerGone = true;
  });

  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  for (let i = 0; i < TOKENS; i += 1) {
    if (peerGone) break;
    if (!res.write(`data: token ${i}\n\n`)) {
      // Backpressure, not disconnection -- but a write that throws EPIPE
      // lands in the 'error' handler below and means the same thing.
    }
    sent = i + 1;
    await sleep(TOKEN_INTERVAL_MS);
  }
  if (!peerGone) res.end();

  ledger.push({
    aborted: peerGone,
    tokens: sent,
    seconds: Number(process.hrtime.bigint() - started) / 1e9,
  });
});
upstream.on('clientError', () => {});

// ---------------------------------------------------------------------------
// The gateway: the same upstream call written two ways.
// ---------------------------------------------------------------------------
const gateway = http.createServer(async (req, res) => {
  const url = `http://127.0.0.1:${upstream.address().port}/completions`;

  if (req.url === '/naive') {
    // Buffer the whole response, then reply. Nothing in this shape can
    // observe the client, and no error is raised when it leaves: writing
    // to a closed response is a silent no-op in Node.
    try {
      const r = await fetch(url, { method: 'POST' });
      const body = await r.arrayBuffer();
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end(Buffer.from(body));
    } catch {
      res.destroy();
    }
    return;
  }

  // The fix, and it is four lines: one controller, one listener, one
  // signal, one guarded pipe.
  const ac = new AbortController();
  req.on('close', () => ac.abort());
  try {
    const r = await fetch(url, { method: 'POST', signal: ac.signal });
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    for await (const chunk of r.body) {
      if (ac.signal.aborted) break;
      res.write(chunk);
    }
    res.end();
  } catch (err) {
    if (err.name !== 'AbortError') res.destroy();
  }
});

// ---------------------------------------------------------------------------
// The client that hangs up. A raw socket on purpose: a timeout in an HTTP
// client library raises in your code without necessarily closing the TCP
// connection, so the server sees nothing and the experiment measures the
// wrong thing. Closing the socket is the only signal a server ever gets.
// ---------------------------------------------------------------------------
function hangUpOn(path) {
  return new Promise((resolve) => {
    const sock = net.connect(gateway.address().port, '127.0.0.1', () => {
      sock.write(
        `POST ${path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n`,
      );
      setTimeout(() => {
        sock.destroy();
        resolve();
      }, CLIENT_HANGS_UP_AFTER_MS);
    });
    sock.on('error', () => resolve());
  });
}

async function main() {
  await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
  await new Promise((r) => gateway.listen(0, '127.0.0.1', r));

  console.log('Node.js - cancellation on client disconnect');
  console.log(
    `  upstream streams ${TOKENS} tokens x ${TOKEN_INTERVAL_MS}ms = ` +
      `${((TOKENS * TOKEN_INTERVAL_MS) / 1000).toFixed(1)}s of decode`,
  );
  console.log(`  client hangs up after ${CLIENT_HANGS_UP_AFTER_MS / 1000}s\n`);
  console.log(
    '  handler        upstream saw     tokens decoded  upstream ran   wasted',
  );
  console.log(`  ${'-'.repeat(70)}`);

  for (const path of ['/naive', '/cancelling']) {
    ledger.length = 0;
    await hangUpOn(path);
    const deadline = Date.now() + TOKENS * TOKEN_INTERVAL_MS + 1000;
    while (ledger.length === 0 && Date.now() < deadline) await sleep(50);
    const e = ledger[0] || { aborted: false, tokens: -1, seconds: NaN };
    const wasted = Math.max(0, e.seconds - CLIENT_HANGS_UP_AFTER_MS / 1000);
    console.log(
      `  ${path.padEnd(14)} ${(e.aborted ? 'cancelled' : 'nothing').padEnd(16)} ` +
        `${String(e.tokens).padStart(14)} ${e.seconds.toFixed(2).padStart(12)}s ` +
        `${wasted.toFixed(2).padStart(7)}s`,
    );
  }

  console.log();
  console.log("  'wasted' is decode time spent on a response nobody read. On a");
  console.log('  loaded server those KV blocks stayed allocated the whole time,');
  console.log('  so the scheduler could not admit somebody who was still waiting.');

  upstream.close();
  gateway.close();
}

main();
