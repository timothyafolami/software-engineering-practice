/*
 * Layer 5 - Topic 1: the latency knee in Node.
 *
 * WHAT THIS DEMONSTRATES
 *   The same queueing arithmetic as the Python file, in the runtime with
 *   the smallest natural concurrency. Two limits stack in every Node
 *   service and people usually know only one of them:
 *     - a *count* you configured -- `pg`'s pool (default max 10), an
 *       undici Agent's `connections`, a semaphore you wrote. This is what
 *       binds for IO-bound handlers, and it behaves exactly like the
 *       SQLAlchemy pool in the Python file.
 *     - the one thread that runs all your JavaScript. For CPU-bound
 *       handlers the real L is 1 regardless of what the pool says, and
 *       capacity is 1/S. The last section shows a pool of 10 in front of
 *       a 5ms CPU handler promising 2000 rps and delivering ~200.
 *
 *   Node's specific contribution to this lab: it is the only runtime here
 *   with a first-class saturation signal that is not CPU percent.
 *   `perf_hooks.monitorEventLoopDelay()` measures how late the loop runs
 *   its own timers. Watch that column.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   1. `achieved` plateaus at pool / service time -- Little's Law
 *      rearranged, lambda_max = L / W.
 *   2. p99 leaves p50 behind between rho=0.8 and rho=0.95 and tracks the
 *      S/(1-rho) column while it does.
 *   3. `wait p50`, the time spent queued for a pool slot doing nothing,
 *      is ~0 at rho=0.2 and is most of the latency by rho=0.95. The
 *      handler never got slower. The waiting room got longer.
 *   4. Loop lag stays flat through the pooled sweeps: this process is not
 *      busy, it is blocked on a count. That is the signature of the
 *      production problem this layer opens with.
 *   5. Doubling the pool moves capacity and the knee proportionally.
 *   6. In the CPU-bound section the pool is never even full, throughput
 *      stops at 1/S, and loop lag is the only metric that noticed.
 *
 * RUN
 *     node latency_knee.js
 */
'use strict';

const { monitorEventLoopDelay } = require('node:perf_hooks');

const SERVICE_MS = 40;        // an awaited query: the handler holds a slot
const CPU_MS = 5;             // the CPU-bound handler in the last section
const POOL_SIZES = [5, 10];   // SQLAlchemy's default pool_size, then double
const RHOS = [0.2, 0.5, 0.8, 0.9, 0.95, 1.1];
const STEP_SECONDS = 8;
const GAUGE_INTERVAL_MS = 20;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const now = () => Number(process.hrtime.bigint()) / 1e6;

/*
 * A bounded resource with an unbounded FIFO waiting room: a connection
 * pool, in one class. `acquire` never rejects, which is the default
 * posture of most pools -- node-postgres queues you forever, SQLAlchemy
 * queues you for `pool_timeout` seconds and then throws. Topic 5 is about
 * what to do instead of queueing.
 */
class Pool {
  constructor(size) {
    this.size = size;
    this.available = size;
    this.waiters = [];
  }

  acquire() {
    if (this.available > 0) {
      this.available -= 1;
      return Promise.resolve();
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  release() {
    const next = this.waiters.shift();
    if (next) next();
    else this.available += 1;
  }
}

function percentile(sorted, p) {
  if (sorted.length === 0) return 0;
  const k = Math.min(sorted.length - 1, Math.round((p / 100) * (sorted.length - 1)));
  return sorted[k];
}

const mean = (v) => (v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0);

/* Exponential gaps: a Poisson process, the standard model for independent
 * users arriving. Evenly spaced arrivals would understate the queue,
 * because bursts are what fill it. */
const exponential = (rate) => -Math.log(1 - Math.random()) / rate;

/*
 * One measurement step at a fixed offered rate.
 *
 * OPEN MODEL, and specifically: every arrival's clock starts at the time
 * it was *scheduled* to arrive, not at the time this process got around
 * to dispatching it. That distinction is the whole of topic 6. A
 * generator that starts the clock when it dispatches will silently
 * forgive itself for being late -- and when the handler is CPU-bound, the
 * generator shares a thread with it and is guaranteed to be late. Real
 * users do not wait for your event loop before deciding to click.
 */
async function step(pool, rate, seconds, cpuBound) {
  const total = [];
  const wait = [];
  const gauge = [];
  const completions = [];
  let inflight = 0;
  let sent = 0;

  const histogram = monitorEventLoopDelay({ resolution: 5 });
  histogram.enable();
  const sampler = setInterval(() => gauge.push(inflight), GAUGE_INTERVAL_MS);

  const begin = now();
  const schedule = [];
  for (let t = exponential(rate) * 1000; t < seconds * 1000; t += exponential(rate) * 1000) {
    schedule.push(begin + t);
  }

  const one = async (scheduledAt) => {
    inflight += 1;
    await pool.acquire();
    const acquired = now();
    if (cpuBound) burnCpu(CPU_MS);
    else await sleep(SERVICE_MS);
    pool.release();
    inflight -= 1;
    // Node timers may fire up to a millisecond early, so clamp rather
    // than print a negative wait.
    wait.push(Math.max(0, acquired - scheduledAt));
    const done = now();
    completions.push(done);
    total.push(done - scheduledAt);
  };

  const pending = [];
  for (const scheduledAt of schedule) {
    const delay = scheduledAt - now();
    if (delay > 0) await sleep(delay);
    sent += 1;
    pending.push(one(scheduledAt));
  }
  const window = seconds;

  // Drain before reporting. Past rho=1 this is where the backlog built up
  // during the step finally comes out, which is why those rows carry
  // latencies larger than the step itself.
  await Promise.all(pending);
  clearInterval(sampler);
  histogram.disable();
  total.sort((a, b) => a - b);
  wait.sort((a, b) => a - b);

  return {
    target: rate,
    offered: sent / window,
    // Completions that landed INSIDE the step, not after the drain. Past
    // rho=1 the difference between these two numbers is the backlog.
    achieved: completions.filter((t) => t <= begin + seconds * 1000).length / window,
    p50: percentile(total, 50),
    p99: percentile(total, 99),
    waitP50: percentile(wait, 50),
    meanTotal: mean(total),
    gaugeL: mean(gauge),
    loopLagP99Ms: histogram.percentile(99) / 1e6,
  };
}

function burnCpu(ms) {
  const until = now() + ms;
  let acc = 0;
  while (now() < until) acc += Math.sqrt(acc + 1);
  return acc;
}

/* The knee is a shape, and a table of numbers hides shapes. */
function chart(rows) {
  const top = Math.max(...rows.map((r) => r[1])) || 1;
  console.log('\n  p99 (ms) against rho');
  for (const [rho, value] of rows) {
    const bar = '#'.repeat(Math.max(1, Math.round((56 * value) / top)));
    console.log(`  rho=${rho.toFixed(2).padEnd(6)}|${bar} ${value.toFixed(0)}`);
  }
  console.log(`  ${' '.repeat(10)}+${'-'.repeat(56)} ${top.toFixed(0)} ms full scale`);
}

async function sweep(poolSize) {
  const pool = new Pool(poolSize);

  // Measure S rather than assuming SERVICE_MS. setTimeout has a
  // resolution floor and the loop adds a little; a capacity computed from
  // a constant nobody measured is the most common way this experiment
  // goes wrong.
  const warm = await step(pool, 5, 2, false);
  const service = warm.meanTotal / 1000;
  const capacity = poolSize / service;

  console.log(`\n=== pool = ${poolSize} slots, measured service time S = ` +
    `${(service * 1000).toFixed(1)} ms ===`);
  console.log(`predicted capacity L/S = ${capacity.toFixed(1)} rps\n`);

  const header = '  rho   offered  achieved      p50      p99   wait p50   ' +
    'L (gauge)   lam*Wbar   S/(1-rho)   loop lag p99';
  console.log(header);
  console.log('-'.repeat(header.length));

  const rows = [];
  for (const rho of RHOS) {
    const r = await step(pool, capacity * rho, STEP_SECONDS, false);
    const little = r.achieved * (r.meanTotal / 1000);
    const predicted = rho < 1 ? (service / (1 - rho)) * 1000 : Infinity;
    rows.push([rho, r.p99]);
    console.log(
      `${rho.toFixed(2).padStart(5)} ${r.offered.toFixed(1).padStart(9)} ` +
      `${r.achieved.toFixed(1).padStart(9)} ${r.p50.toFixed(1).padStart(8)} ` +
      `${r.p99.toFixed(1).padStart(8)} ${r.waitP50.toFixed(1).padStart(10)} ` +
      `${r.gaugeL.toFixed(1).padStart(11)} ${little.toFixed(1).padStart(10)} ` +
      `${(Number.isFinite(predicted) ? predicted.toFixed(1) : 'inf').padStart(11)} ` +
      `${r.loopLagP99Ms.toFixed(1).padStart(14)}`);
  }
  console.log('\n  lam*Wbar uses the MEAN latency, not p50. Little\'s Law is a');
  console.log('  statement about means; L = lambda * p50 is not a law and does not');
  console.log('  hold once the distribution is skewed, which is precisely when you');
  console.log('  most want to use it.');
  chart(rows);
}

/*
 * The trap this runtime sets specifically, and the reason Node earns a
 * place in this topic.
 *
 * Everything above assumed the handler *awaits* its work, so the pool was
 * the only limit and capacity was pool/S. Make the work CPU-bound instead
 * -- a big JSON.parse, a template render, a hand-rolled hash, any
 * synchronous crypto -- and the pool stops mattering. A CPU-bound handler
 * owns the one thread that runs all your JavaScript, so the real L is 1
 * whatever the pool says, and capacity is 1/S.
 */
async function cpuBoundSweep() {
  const poolSize = 10;
  console.log(`\n\n=== same pool (${poolSize} slots), but the handler burns ` +
    `${CPU_MS} ms of CPU instead of awaiting ===`);
  const trueCapacity = 1000 / CPU_MS;
  console.log(`pool arithmetic promises  L/S = ${(poolSize * trueCapacity).toFixed(0)} rps`);
  console.log(`the one JS thread permits 1/S = ${trueCapacity.toFixed(0)} rps\n`);

  const header = '  offered  achieved      p50      p99   L (gauge)   loop lag p99';
  console.log(header);
  console.log('-'.repeat(header.length));

  for (const rho of [0.5, 0.9, 1.25]) {
    const r = await step(new Pool(poolSize), trueCapacity * rho, STEP_SECONDS, true);
    console.log(
      `${r.offered.toFixed(1).padStart(9)} ${r.achieved.toFixed(1).padStart(9)} ` +
      `${r.p50.toFixed(1).padStart(8)} ${r.p99.toFixed(1).padStart(8)} ` +
      `${r.gaugeL.toFixed(1).padStart(11)} ${r.loopLagP99Ms.toFixed(1).padStart(14)}`);
  }
  console.log('\n  Throughput stops near 1/S, not near pool/S, and the pool gauge stays');
  console.log('  low -- there is nothing to queue *for*, because the contended resource');
  console.log('  was never the pool. Loop lag is the column that noticed. Note also');
  console.log('  that `top` would show one core busy on a many-core machine, which most');
  console.log('  dashboards average into a comfortable-looking CPU percentage.');
}

async function main() {
  console.log('Latency knee in Node: one bounded resource, open-model arrivals.');
  console.log('CPU is not the limit anywhere in the pooled sweeps. That is the point.');
  for (const size of POOL_SIZES) await sweep(size);
  await cpuBoundSweep();
  console.log('\nThe two pooled sweeps ran identical code and an identical ramp; the');
  console.log('only difference is the pool size. Compare the two capacity lines and');
  console.log('the two rho=0.9 rows: that is the whole topic in two numbers.');
}

main();
