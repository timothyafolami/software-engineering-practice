// Topic 7 ladder F: the Node client, serving on :8081 and reporting its own wait.
//
// WHAT THIS DEMONSTRATES: the event loop hides queueing entirely. The process
// looks idle while a thousand callbacks wait, because "waiting" is not a thread
// you can count. `monitorEventLoopDelay()` is the only way to see it, and it is
// reported in the same header the Go consumer uses so the two are comparable.
import http from 'node:http';
import { monitorEventLoopDelay } from 'node:perf_hooks';
import { makeClient } from './client.js';

const API = process.env.API || 'http://api:8000';
const client = makeClient(API, { validate: process.env.VALIDATE === '1' });

// resolution=20ms: fine enough to see a stall, coarse enough not to be the load.
const loopDelay = monitorEventLoopDelay({ resolution: 20 });
loopDelay.enable();

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/healthz') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end('{"ok":true}');
  }
  if (url.pathname !== '/orders') {
    res.writeHead(404);
    return res.end();
  }

  const customer = Number(url.searchParams.get('customer') || 1);
  const controller = new AbortController();
  const budget = setTimeout(() => controller.abort(), 5000);
  try {
    const { data, queueMs } = await client.fetchOrders(customer, { signal: controller.signal });
    res.writeHead(200, {
      'content-type': 'application/json',
      'x-client-queue-ms': String(queueMs),
      // The number that has no equivalent in the Go client, because Go's
      // scheduler does not have this failure mode. p99 event-loop delay in ms.
      'x-event-loop-p99-ms': String(Math.round(loopDelay.percentile(99) / 1e6)),
    });
    res.end(JSON.stringify(data));
  } catch (err) {
    res.writeHead(err.status && err.status < 500 ? err.status : 502, {
      'content-type': 'application/json',
      'x-client-queue-ms': String(err.queueMs ?? 0),
      'x-event-loop-p99-ms': String(Math.round(loopDelay.percentile(99) / 1e6)),
    });
    res.end(JSON.stringify({ error: 'upstream', message: String(err.message) }));
  } finally {
    clearTimeout(budget);
  }
});

server.listen(8081, () => {
  console.log(`consumer-node listening on :8081, target ${API}`);
});
