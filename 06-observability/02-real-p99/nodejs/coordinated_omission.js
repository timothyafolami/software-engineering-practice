/*
 * Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.
 *
 * Why Node: it is the cheapest place in this lab to write the *correct*
 * generator, and that fact is worth more than it looks. An open-loop generator
 * must hold every request that has been issued and not yet answered. In Python,
 * Rust and C++ that is an OS thread each. In Node it is a pending promise --
 * a few hundred bytes, no kernel object, no scheduler decision. So the runtime
 * that is worst at CPU-bound work is the one that makes the honest measurement
 * easiest, and the runtime you would reach for on instinct (threads) is the one
 * that quietly pushes you toward the dishonest one.
 *
 * The same property is the trap on the *service* side: this file models the
 * service as a single server with a queue, which is precisely what a Node
 * service is. A 500ms stall in a Node handler is not one slow request, it is
 * every request that arrived during those 500ms.
 *
 * What this demonstrates
 * ----------------------
 *   * Service: single server, FIFO queue, 3ms per request -> ~333 req/s.
 *   * Offered load: 200 req/s, a comfortable 60% of capacity.
 *   * At T+2.5s exactly one request takes 500ms. One request.
 *
 *   * CLOSED-LOOP: 4 virtual users, each send -> wait -> think 30ms -> repeat.
 *     This is `k6 run --vus 4`, and almost every load test ever written.
 *   * OPEN-LOOP: requests issued at a fixed 200/s whether or not earlier ones
 *     came back. This is k6's constant-arrival-rate executor, or vegeta -rate.
 *
 * What to look for in the output
 * ------------------------------
 * 1. "requests started IN the stall window": ~4 closed-loop, ~100 open-loop.
 *    That one line is the entire mechanism.
 * 2. The p99 rows. Same service, same fault, two answers.
 * 3. Closed-loop iteration duration vs request duration -- k6's tell.
 * 4. "OS threads used by the generator": zero. That is the Node result.
 *
 * Run:  node coordinated_omission.js
 */

'use strict';

const SERVICE_MS = 3;          // -> ~333 req/s capacity
const STALL_AFTER_MS = 2500;   // when the one slow request happens
const STALL_MS = 500;          // how long that one request takes
const RUN_MS = 5000;
const OPEN_RATE_PER_SEC = 200; // offered load, ~60% of capacity
const CLOSED_VUS = 4;
const CLOSED_THINK_MS = (CLOSED_VUS / OPEN_RATE_PER_SEC) * 1000;

const nowMs = () => Number(process.hrtime.bigint()) / 1e6;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A single server with a FIFO queue. The queue is where the latency a
 * closed-loop generator cannot see accumulates.
 */
class Service {
  constructor(epochMs) {
    this.epochMs = epochMs;
    this.queue = [];
    this.stalled = false;
    this.running = true;
    this.idle = null;
    this.loop = this.serve();
  }

  submit(request) {
    request.sentMs = nowMs();
    this.queue.push(request);
    if (this.idle) {
      const wake = this.idle;
      this.idle = null;
      wake();
    }
  }

  async serve() {
    while (this.running) {
      if (this.queue.length === 0) {
        await new Promise((resolve) => {
          this.idle = resolve;
          setTimeout(resolve, 5);
        });
        continue;
      }
      const request = this.queue.shift();
      const elapsed = nowMs() - this.epochMs;
      if (!this.stalled && elapsed >= STALL_AFTER_MS) {
        this.stalled = true;
        await sleep(STALL_MS);   // the one bad request
      } else {
        await sleep(SERVICE_MS);
      }
      request.doneMs = nowMs();
      request.resolve();
    }
  }

  async stop() {
    this.running = false;
    if (this.idle) this.idle();
    await this.loop;
  }
}

function makeRequest(seq, arrivalMs) {
  const request = { seq, arrivalMs, sentMs: 0, doneMs: 0 };
  request.settled = new Promise((resolve) => { request.resolve = resolve; });
  return request;
}

function percentile(values, q) {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const idx = Math.min(ordered.length - 1,
    Math.max(0, Math.round(q * (ordered.length - 1))));
  return ordered[idx];
}

const startedInStall = (requests, epochMs) =>
  requests.filter((r) => {
    const t = r.sentMs - epochMs;
    return t >= STALL_AFTER_MS && t < STALL_AFTER_MS + STALL_MS;
  }).length;

async function runClosedLoop() {
  const epochMs = nowMs();
  const service = new Service(epochMs);
  const requests = [];
  const iterationMs = [];
  let seq = 0;

  async function virtualUser() {
    while (nowMs() - epochMs < RUN_MS) {
      const iterStart = nowMs();
      const request = makeRequest(++seq, nowMs());
      service.submit(request);
      await request.settled;       // <- this user is now blocked
      requests.push(request);
      await sleep(CLOSED_THINK_MS);
      iterationMs.push(nowMs() - iterStart);
    }
  }

  await Promise.all(Array.from({ length: CLOSED_VUS }, virtualUser));
  await service.stop();

  return {
    requests: requests.length,
    latencyMs: requests.map((r) => r.doneMs - r.sentMs),
    iterationMs,
    startedInStall: startedInStall(requests, epochMs),
    peakInFlight: CLOSED_VUS,
    osThreads: 0,
  };
}

async function runOpenLoop() {
  const epochMs = nowMs();
  const service = new Service(epochMs);
  const requests = [];
  const pending = [];
  let inFlight = 0;
  let peakInFlight = 0;
  let seq = 0;

  const intervalMs = 1000 / OPEN_RATE_PER_SEC;
  while (seq * intervalMs < RUN_MS) {
    const targetMs = epochMs + seq * intervalMs;
    const waitMs = targetMs - nowMs();
    if (waitMs > 0) await sleep(waitMs);
    seq += 1;
    const request = makeRequest(seq, targetMs);
    // No thread, no worker, no pool. One pending promise per in-flight
    // request, which is why this generator is cheap to write correctly here.
    inFlight += 1;
    peakInFlight = Math.max(peakInFlight, inFlight);
    service.submit(request);
    pending.push(request.settled.then(() => {
      inFlight -= 1;
      requests.push(request);
    }));
  }

  await Promise.all(pending);
  await service.stop();

  return {
    requests: requests.length,
    // Latency from INTENDED arrival, not from when the generator got round to
    // sending. In a working open-loop generator these are the same, and that
    // sameness is the property being demonstrated.
    latencyMs: requests.map((r) => r.doneMs - r.arrivalMs),
    serviceMs: requests.map((r) => r.doneMs - r.sentMs),
    startedInStall: startedInStall(requests, epochMs),
    peakInFlight,
    osThreads: 0,
  };
}

async function main() {
  const bar = '='.repeat(74);
  console.log(bar);
  console.log('COORDINATED OMISSION   (Node.js, single-server FIFO service)');
  console.log(bar);
  console.log(`service capacity ~${Math.round(1000 / SERVICE_MS)} req/s (${SERVICE_MS}ms/request), `
    + `offered load ${OPEN_RATE_PER_SEC} req/s`);
  console.log(`one request at T+${STALL_AFTER_MS}ms takes ${STALL_MS}ms instead of ${SERVICE_MS}ms`);
  console.log(`run length ${RUN_MS}ms\n`);

  console.log(`running closed-loop (${CLOSED_VUS} virtual users, ${CLOSED_THINK_MS}ms think time)...`);
  const closed = await runClosedLoop();
  console.log(`running open-loop (${OPEN_RATE_PER_SEC} req/s arrival rate)...\n`);
  const open = await runOpenLoop();

  const row = (label, a, b) =>
    console.log(label.padEnd(38) + String(a).padStart(14) + String(b).padStart(14));

  row('', 'CLOSED-LOOP', 'OPEN-LOOP');
  row('requests completed', closed.requests, open.requests);
  row('requests started IN the stall window', closed.startedInStall, open.startedInStall);
  row('peak requests in flight', closed.peakInFlight, open.peakInFlight);
  row('OS threads used by the generator', closed.osThreads, open.osThreads);
  console.log();
  for (const [label, q] of [['p50', 0.5], ['p75', 0.75], ['p95', 0.95],
    ['p99', 0.99], ['p99.9', 0.999], ['max', 1.0]]) {
    console.log(('latency ' + label).padEnd(38)
      + (percentile(closed.latencyMs, q).toFixed(1) + 'ms').padStart(14)
      + (percentile(open.latencyMs, q).toFixed(1) + 'ms').padStart(14));
  }

  console.log('\nThe closed-loop column measures request duration: send -> response.');
  console.log('The open-loop column measures from the moment the request was DUE.');
  console.log(`Note the first row too: closed-loop completed ${closed.requests} requests to`);
  console.log(`open-loop's ${open.requests}. It did not go slower -- it asked for less,`);
  console.log('precisely while the service was worst.');

  console.log('\nThe tell, inside the closed-loop run alone:');
  console.log(`  request duration p99   : ${percentile(closed.latencyMs, 0.99).toFixed(1)}ms`);
  console.log(`  iteration duration p99 : ${percentile(closed.iterationMs, 0.99).toFixed(1)}ms`);
  console.log('  If iteration_duration climbs while http_req_duration does not, your');
  console.log('  generator stopped asking. That is k6\'s version of this same line.');

  const closed99 = percentile(closed.latencyMs, 0.99);
  const open99 = percentile(open.latencyMs, 0.99);
  console.log(`\nVERDICT: open-loop p99 is ${(open99 / Math.max(closed99, 0.001)).toFixed(1)}x the`
    + ' closed-loop p99 for the identical');
  console.log('service and the identical fault.');
  console.log(`The closed-loop generator sampled the stall ${closed.startedInStall} times out of`);
  console.log(`${closed.requests} requests `
    + `(${((100 * closed.startedInStall) / Math.max(1, closed.requests)).toFixed(2)}%), `
    + 'which is why it never reaches the 99th percentile.');
  console.log('\nNode footnote: both generators above used zero OS threads. The correct');
  console.log('generator was the cheap one to write. In the Python, Rust and C++');
  console.log('versions of this file the open-loop generator costs one thread per');
  console.log('in-flight request, which is the practical reason closed-loop tools won.');
}

main();
