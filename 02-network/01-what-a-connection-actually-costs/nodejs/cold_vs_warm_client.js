// Layer 2 · Topic 1 - What a connection costs in Node, and where Node's
// default moved.
//
// Node is here because its answer changed underneath everybody. Before
// Node 19, `http.globalAgent` had keepAlive:false, so every `http.request`
// opened a fresh TCP connection and the pooling advice was "always pass
// your own Agent". Since Node 19 the global agent keeps connections alive
// by default, and `fetch()` is undici, which pools per origin on its own
// dispatcher. So the classic Node bug mostly went away -- and the trap
// moved to undici's timeouts instead (Topic 3).
//
// Four variants, same 200 requests to the same local server, which counts
// TCP connections on its 'connection' event (one per accept(2)).
//
// What to look for in the output: connections opened per variant. Only
// variant 1 -- an explicit keepAlive:false agent, which is what pre-19 code
// and a lot of copy-pasted code still does -- should open one per request.
//
// Run: node cold_vs_warm_client.js

'use strict';

const http = require('node:http');

const REQUESTS = 200;
const CONCURRENCY = 10;

let connectionsAccepted = 0;

const server = http.createServer((req, res) => {
  const body = '{"ok":true}';
  res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': body.length });
  res.end(body);
});
server.on('connection', () => {
  connectionsAccepted += 1;
});

function requestOnce(agent, url) {
  return new Promise((resolve, reject) => {
    const req = http.get({ ...url, agent }, (res) => {
      res.resume();
      res.on('end', resolve);
    });
    req.on('error', reject);
  });
}

function fetchOnce(href) {
  // fetch() is undici. It ignores http.Agent entirely and uses its own
  // global dispatcher, which pools per origin. Mixing the two is a common
  // source of "I configured the agent and nothing changed".
  return fetch(href).then((res) => res.arrayBuffer());
}

async function drive(label, run) {
  const before = connectionsAccepted;
  const latencies = [];
  let issued = 0;

  async function worker() {
    while (issued < REQUESTS) {
      issued += 1;
      const started = process.hrtime.bigint();
      await run();
      latencies.push(Number(process.hrtime.bigint() - started) / 1e6);
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  const opened = connectionsAccepted - before;
  latencies.sort((a, b) => a - b);
  const at = (f) => latencies[Math.min(latencies.length - 1, Math.floor(latencies.length * f))];
  console.log(`  ${label}`);
  console.log(`    requests issued        ${latencies.length}`);
  console.log(`    TCP connections opened ${opened}`);
  console.log(`    requests per connection ${(latencies.length / Math.max(opened, 1)).toFixed(1)}`);
  console.log(
    `    latency p50 ${at(0.5).toFixed(3)} ms   p95 ${at(0.95).toFixed(3)} ms   p99 ${at(0.99).toFixed(3)} ms`
  );
}

async function main() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const url = { hostname: '127.0.0.1', port, path: '/thing' };
  const href = `http://127.0.0.1:${port}/thing`;

  console.log('='.repeat(78));
  console.log('Node: connection reuse, and where the default moved');
  console.log('='.repeat(78));
  console.log(`  node ${process.version}   server ${href}   ${REQUESTS} requests, ${CONCURRENCY} in flight`);
  console.log(`  http.globalAgent.keepAlive = ${http.globalAgent.keepAlive}   (false before Node 19, true since)\n`);

  const coldAgent = new http.Agent({ keepAlive: false });
  await drive('COLD - new http.Agent({ keepAlive: false })', () => requestOnce(coldAgent, url));
  coldAgent.destroy();

  console.log();
  const perRequestAgent = () => new http.Agent({ keepAlive: true, maxSockets: CONCURRENCY });
  await drive('COLD-BY-LIFETIME - a keepAlive agent constructed per request', async () => {
    const agent = perRequestAgent();
    try {
      await requestOnce(agent, url);
    } finally {
      agent.destroy();
    }
  });

  console.log();
  await drive('WARM - http.globalAgent (Node >= 19 default)', () => requestOnce(http.globalAgent, url));

  console.log();
  await drive('WARM - fetch() / undici global dispatcher', () => fetchOnce(href));

  console.log('\n  The middle variant is the point. keepAlive:true on an agent you');
  console.log('  throw away every request buys you nothing: a pool that does not');
  console.log('  outlive the request is a pool of one. Identical mistake to');
  console.log('  building httpx.AsyncClient() inside a FastAPI handler.');

  console.log('\n  globalAgent settings on this build:');
  console.log(`    keepAlive        ${http.globalAgent.keepAlive}`);
  console.log(`    keepAliveMsecs   ${http.globalAgent.keepAliveMsecs} ms  (TCP keepalive probe delay, NOT the idle close)`);
  console.log(`    maxSockets       ${http.globalAgent.maxSockets}`);
  console.log(`    maxFreeSockets   ${http.globalAgent.maxFreeSockets}`);
  console.log('    (undici, which backs fetch(), has its own pool with its own');
  console.log('     numbers -- setting these does not touch it.)');

  http.globalAgent.destroy();
  server.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
