/**
 * Layer 5 - Topic 6: fan-out tails, hedging, and coordinated omission (Node).
 *
 * One process holds a gateway, up to 50 backends and BOTH load models, so the
 * only thing missing versus the containerised version is real network
 * variance. Everything else -- the arithmetic of percentiles under fan-out,
 * the cost of hedging, and the lie a closed-loop generator tells -- is here.
 * Same constants, same phases and same columns as ../python/fanout.py and
 * ../golang/fanout.go, so the tables line up.
 *
 * THE TWO NODE-SPECIFIC THINGS, BOTH MEASURED RATHER THAN ASSERTED
 *
 * 1. `Promise.race` + `AbortController` is the hedging primitive, and its
 *    cancellation story is honest about its limits. Aborting stops *your*
 *    side. Whether the server stops is a question about the server, not about
 *    your AbortController -- `fetch` aborting does not reach into the remote
 *    process. Phase B therefore runs three rows, not two: no hedge, hedge
 *    where the abort reaches the backend and frees its worker, and
 *    `hedge @p95, abort NOT honoured` -- the wire-realistic case, where your
 *    side stops waiting and the backend keeps working anyway. Read them in
 *    the `svc_ms/req` column, which is the backend service time actually
 *    consumed per request. That column is the price of a hedge, and the third
 *    row is what a hedge costs when cancellation is a polite request.
 *
 * 2. Node's fan-out is genuinely concurrent while its *response assembly* is
 *    not. The K backend responses are real objects and the gateway really
 *    does `JSON.stringify` them into one payload, on the one thread, after
 *    the last leg lands. At large K that serialisation is the gateway's own
 *    tail, and it shows up as event loop lag rather than in any backend's
 *    numbers -- so phase A carries a `loop_lag_p99` column from
 *    `monitorEventLoopDelay`, sampled per cell. When it climbs with K while
 *    the backends are unchanged, the gateway has become the slow dependency.
 *
 * WHAT THIS DEMONSTRATES
 *
 *   Phase A  A gateway fans out to K identical backends and waits for all of
 *            them, K in {1,2,5,10,20,50}, against two service-time
 *            distributions that share a p50 of 10ms and a p99 of 200ms:
 *            log-normal, and bimodal with a 1% slow mode. Backends are
 *            deliberately unsaturated here, so the only thing acting on the
 *            latency columns is the arithmetic.
 *   Phase B  Hedging at the MEASURED backend p95, under a 5% token bucket,
 *            with the three abort outcomes above.
 *   Phase C  The same server, the same nominal rate, measured twice: once by
 *            an open-model generator (arrivals on a fixed schedule) and once
 *            by a closed-loop one (a fixed number of virtual users, each
 *            waiting for a response before sending again).
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *
 *   1. Phase A's `measured` column against `predicted`, which is 1 - 0.99^K
 *      and is arithmetic, not measurement. If the two disagree badly, read
 *      the README's "what would mean the experiment is broken" list before
 *      believing either.
 *   2. Phase A's `loop_lag_p99` against K, per the note above.
 *   3. Phase B's `svc_ms/req` and `+load` next to `e2e_p99`. Hedging is not
 *      free and the point of those columns is to quantify what it cost.
 *   4. Phase C's two p99s and the two histograms underneath them. The closed
 *      loop also prints an omission-corrected p99, measured from when each
 *      request was DUE rather than when the generator got round to sending
 *      it. The gap between raw and corrected is the size of the lie.
 *
 * A NOTE ON THE TIMER FLOOR: `setTimeout` resolves to roughly a millisecond
 * and the p50 here is 10ms. Read the calibration block first -- it prints
 * what the backend distribution actually measured as, not what it was
 * configured as, and every later table is relative to those numbers.
 *
 * RUN
 *     node fanout.js
 *
 * No dependencies. Takes roughly three minutes.
 */
'use strict';

const { monitorEventLoopDelay } = require('node:perf_hooks');

const MS = 1000.0;

// ------------------------------------------------------------------ config

const BACKEND_P50_MS = 10.0;
const TAIL_RATIO = 20.0;            // p99 / p50, per the README's specification
const Z99 = 2.3263478740408408;
const Z95 = 1.6448536269514722;
const LOGNORMAL_SIGMA = Math.log(TAIL_RATIO) / Z99;
const TAIL_THRESHOLD_MS = BACKEND_P50_MS * TAIL_RATIO;   // 200.0ms, by construction

const K_VALUES = [1, 2, 5, 10, 20, 50];
const SAMPLES_PER_CELL = 1500;
const MAX_RATE = 400.0;             // requests/s ceiling for a cell
const MAX_BACKEND_CALLS_PER_S = 10000.0;
const STAT_WORKERS = 512;           // phase A: backends must NOT queue

const HEDGE_K = [10, 50];
const HEDGE_BUDGET_RATIO = 0.05;    // "at most 5% of backend calls may hedge"
const HEDGE_BUCKET_CAPACITY = 20.0;

const CO_K = 10;
const CO_WORKERS = 4;               // phase C: backends that CAN saturate
const CO_RHO = 0.90;
const CO_SECONDS = 25.0;

const CALIB_SAMPLES = 20000;
const CALIB_BATCH = 500;
const SEED = 20260819;

/** Nearest-rank percentile. No interpolation, and no averaging of percentiles. */
function pct(sorted, q) {
  if (sorted.length === 0) return NaN;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
  return sorted[idx];
}

/** mulberry32: a seeded PRNG, because Math.random cannot be seeded. */
function makeRng(seed) {
  let a = seed >>> 0;
  const unit = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return {
    unit,
    // Box-Muller. No cached second value: the pairing would make the sample
    // stream depend on call interleaving, which is nondeterministic here.
    gauss() {
      let u = 0;
      while (u === 0) u = unit();
      return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * unit());
    },
    expo(rate) {
      let u = 0;
      while (u === 0) u = unit();
      return -Math.log(u) / rate;
    },
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * A backend response with a body, because a fan-out whose legs return `{ok:1}`
 * cannot show the thing this file claims about response assembly. Twelve rows
 * of a few fields each is a modest search-shard answer; the gateway below
 * really serialises all K of them on the one thread there is.
 */
const ITEMS_PER_RESPONSE = 12;
function makeBody(heldMs) {
  const items = new Array(ITEMS_PER_RESPONSE);
  for (let i = 0; i < ITEMS_PER_RESPONSE; i += 1) {
    items[i] = { id: i, score: (i + 1) * 0.017, title: `item-${i}`, tags: ['a', 'b'] };
  }
  return { ok: true, ms: heldMs, items };
}

// ----------------------------------------------------------- distributions

/** p50 = exp(mu); p99 = exp(mu + z99*sigma), sigma chosen so p99/p50 = 20. */
class LogNormal {
  constructor(p50ms, sigma) {
    this.name = 'lognormal';
    this.mu = Math.log(p50ms / MS);
    this.sigma = sigma;
  }
  sample(rng) { return Math.exp(this.mu + this.sigma * rng.gauss()); }
  p95ms() { return Math.exp(this.mu + Z95 * this.sigma) * MS; }
}

/**
 * 99% fast and tight, 1% slow -- and the slow mode's FLOOR is the p99.
 *
 * Putting the slow mode's minimum exactly at 20x the p50 is what makes
 * P(leg > 200ms) equal 1% on the nose, so the same tail threshold works for
 * both distributions and `predicted` stays honest. A slow mode centred on
 * 200ms would put only half of 1% above the threshold, and the
 * predicted/measured comparison would be comparing two different things.
 */
class Bimodal {
  constructor(p50ms, slowFloorMs, slowExtraMs = 50.0, pSlow = 0.01) {
    this.name = 'bimodal';
    this.fastMu = Math.log(p50ms / MS);
    this.fastSigma = 0.15;          // tight: the fast mode never reaches the floor
    this.slowFloor = slowFloorMs / MS;
    this.slowExtra = slowExtraMs / MS;
    this.pSlow = pSlow;
  }
  sample(rng) {
    if (rng.unit() < this.pSlow) return this.slowFloor + rng.expo(1.0 / this.slowExtra);
    return Math.exp(this.fastMu + this.fastSigma * rng.gauss());
  }
  p95ms() { return Math.exp(this.fastMu + Z95 * this.fastSigma) * MS; }
}

// --------------------------------------------------------------- the server

/**
 * One backend: a fixed number of workers, a queue, and a service time.
 *
 * `workers` is what makes phase C possible. Set it high and the backend is a
 * pure delay generator, which is what phase A wants; set it to 4 and the thing
 * has a capacity, a queue in front of it, and therefore an opinion about how
 * fast you are allowed to send.
 *
 * `honourAbort` is the Node-specific knob. When false the backend ignores the
 * signal exactly as a remote HTTP server ignores your AbortController: your
 * `fetch` promise rejects, the socket goes away, and the handler on the other
 * side runs to completion holding whatever it was holding.
 */
class Backend {
  constructor(workers, honourAbort = true) {
    this.slots = workers;
    this.waiters = [];
    this.honourAbort = honourAbort;
    this.started = 0;
    this.completed = 0;
    this.cancelled = 0;
    this.busyMs = 0;
    this.inWork = 0;   // calls still occupying a worker, abandoned ones included
  }

  _acquire() {
    if (this.slots > 0) { this.slots -= 1; return Promise.resolve(); }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  _release() {
    const next = this.waiters.shift();
    if (next) next(); else this.slots += 1;
  }

  /**
   * One call. Resolves with a response object, or rejects with an AbortError
   * if `signal` fires -- and note that the reject and the backend stopping
   * are two different events, which is the whole point of `honourAbort`.
   */
  async call(dist, rng, signal) {
    this.started += 1;
    await this._acquire();                     // queueing, if there is any
    const heldMs = dist.sample(rng) * MS;
    const t0 = process.hrtime.bigint();
    this.inWork += 1;
    const finish = (cancelled) => {
      this.busyMs += Number(process.hrtime.bigint() - t0) / 1e6;
      this.inWork -= 1;
      this._release();
      if (cancelled) this.cancelled += 1; else this.completed += 1;
    };

    return new Promise((resolve, reject) => {
      // Two separate flags, and the separation IS the Node lesson: `settled`
      // is about the promise the caller is holding, `workDone` is about the
      // backend. An abort that is not honoured settles the first and leaves
      // the second running, which is precisely what an aborted `fetch` does
      // to a remote HTTP handler.
      let settled = false;
      let workDone = false;
      let onAbort;

      const timer = setTimeout(() => {
        if (workDone) return;
        workDone = true;
        finish(false);
        if (settled) return;                  // nobody is waiting any more
        settled = true;
        if (signal && onAbort) signal.removeEventListener('abort', onAbort);
        resolve(makeBody(heldMs));
      }, heldMs);

      onAbort = () => {
        if (settled) return;
        settled = true;
        if (this.honourAbort && !workDone) {
          // The abort reached the far end: the worker is freed HERE.
          workDone = true;
          clearTimeout(timer);
          finish(true);
        }
        // Otherwise the timer above is deliberately left alone: it will run to
        // completion, hold its worker for the full service time, and be
        // counted as busy by a caller who is no longer listening.
        reject(new Error('AbortError'));
      };
      if (signal) {
        if (signal.aborted) { onAbort(); return; }
        signal.addEventListener('abort', onAbort, { once: true });
      }
    });
  }
}

/**
 * gRPC/Envoy-shaped retry throttle: every primary call earns `ratio` of a
 * token, every hedge spends a whole one. Steady state is therefore "hedges are
 * at most `ratio` of primary calls", with `capacity` worth of burst. This is
 * the difference between a hedge and a retry storm with better branding.
 */
class TokenBucket {
  constructor(ratio, capacity) {
    this.ratio = ratio; this.capacity = capacity; this.tokens = capacity;
  }
  onPrimary() { this.tokens = Math.min(this.capacity, this.tokens + this.ratio); }
  take() {
    if (this.tokens >= 1.0) { this.tokens -= 1.0; return true; }
    return false;
  }
}

/** Fans out to K backends and waits for every one of them. */
class Gateway {
  constructor(backends, dist, rng, hedgeDelayMs = null) {
    this.backends = backends;
    this.dist = dist;
    this.rng = rng;
    this.hedgeDelayMs = hedgeDelayMs;
    this.bucket = new TokenBucket(HEDGE_BUDGET_RATIO, HEDGE_BUCKET_CAPACITY);
    this.legs = 0;
    this.legsHedged = 0;
    this.budgetDenied = 0;
    this.assemblyMs = 0;
    this.orphans = new Set();
  }

  async _leg(backend) {
    this.legs += 1;
    if (this.hedgeDelayMs === null) return { hedged: false, body: await backend.call(this.dist, this.rng) };

    this.bucket.onPrimary();
    const ac1 = new AbortController();
    const first = backend.call(this.dist, this.rng, ac1.signal);
    first.catch(() => {});                     // rejection is handled below
    const marker = Symbol('timeout');
    const raced = await Promise.race([first, sleep(this.hedgeDelayMs).then(() => marker)]);
    if (raced !== marker) return { hedged: false, body: raced };

    // Past the measured p95 and still nothing. Hedge -- if the budget says so.
    if (!this.bucket.take()) {
      this.budgetDenied += 1;
      return { hedged: false, body: await first };
    }

    this.legsHedged += 1;
    const ac2 = new AbortController();
    const second = backend.call(this.dist, this.rng, ac2.signal);
    second.catch(() => {});
    const tagged = [first.then((b) => ({ b, who: 1 })), second.then((b) => ({ b, who: 2 }))];
    const winner = await Promise.race(tagged);
    // Abort the loser. Whether that stops the backend is the backend's
    // decision, not this line's -- see Backend.honourAbort.
    const loser = winner.who === 1 ? ac2 : ac1;
    const loserPromise = winner.who === 1 ? second : first;
    this.orphans.add(loserPromise);
    loserPromise.catch(() => {}).finally(() => this.orphans.delete(loserPromise));
    loser.abort();
    return { hedged: true, body: winner.b };
  }

  /**
   * The fan-out: K legs, wait for all of them, so end-to-end latency IS the
   * max of the legs -- plus, on this runtime, the cost of turning K response
   * objects into one payload on the only thread there is.
   */
  async handle(k) {
    const results = await Promise.all(
      Array.from({ length: k }, (_, i) => this._leg(this.backends[i])));
    const a0 = process.hrtime.bigint();
    JSON.stringify({ k, legs: results.map((r) => r.body) });
    this.assemblyMs += Number(process.hrtime.bigint() - a0) / 1e6;
    return results.some((r) => r.hedged);
  }
}

// ------------------------------------------------------------- the harness

class Cell {
  constructor() {
    this.latMs = [];
    this.lateMs = [];
    this.correctedMs = [];
    this.arrivalWall = 0;
    this.hedgedRequests = 0;
    this.backendStarted = 0;
    this.backendBusyMs = 0;
    this.loopLagP99 = 0;
    this.gw = null;
  }

  summary() {
    const lat = [...this.latMs].sort((a, b) => a - b);
    const late = [...this.lateMs].sort((a, b) => a - b);
    const over = lat.filter((v) => v > TAIL_THRESHOLD_MS).length;
    const den = Math.max(1, lat.length);
    return {
      n: lat.length,
      p50: pct(lat, 0.5),
      p99: pct(lat, 0.99),
      max: lat.length ? lat[lat.length - 1] : NaN,
      tail: (100.0 * over) / den,
      lateP99: pct(late, 0.99),
      backendRps: this.backendStarted / Math.max(1e-9, this.arrivalWall),
      svcMsPerReq: this.backendBusyMs / den,
      hedgeRate: (100.0 * this.hedgedRequests) / den,
      assemblyMsPerReq: this.gw ? this.gw.assemblyMs / den : 0,
    };
  }
}

/**
 * Open model: arrivals happen on a precomputed schedule, full stop.
 *
 * The schedule is absolute and computed before the run, so the generator's own
 * overhead cannot leak into it -- a generator that sleeps for expovariate(rate)
 * BETWEEN dispatches slows down exactly when the server does, and has quietly
 * become the closed-loop generator this topic is about. Latency is measured
 * from each request's DUE time, not from when the dispatch loop got round to
 * it, for the same reason.
 */
async function runOpenCell(k, dist, workers, rate, n, opts = {}) {
  const { hedgeDelayMs = null, honourAbort = true, seed = SEED } = opts;
  const rngArr = makeRng(seed);
  const rngSvc = makeRng(seed + 1);
  const backends = Array.from({ length: k }, () => new Backend(workers, honourAbort));
  const gw = new Gateway(backends, dist, rngSvc, hedgeDelayMs);
  const cell = new Cell();
  cell.gw = gw;

  const schedule = [];
  let acc = 0.0;
  for (let i = 0; i < n; i += 1) { acc += rngArr.expo(rate); schedule.push(acc * MS); }

  const lag = monitorEventLoopDelay({ resolution: 1 });
  lag.enable();
  const t0 = Number(process.hrtime.bigint()) / 1e6;
  const inflight = [];
  for (const offset of schedule) {
    const due = t0 + offset;
    const now = () => Number(process.hrtime.bigint()) / 1e6;
    const wait = due - now();
    if (wait > 0) await sleep(wait);   // recomputing `now` here can go negative
    cell.lateMs.push(now() - due);
    inflight.push((async () => {
      const hedged = await gw.handle(k);
      cell.latMs.push(Number(process.hrtime.bigint()) / 1e6 - due);
      if (hedged) cell.hedgedRequests += 1;
    })());
  }
  cell.arrivalWall = (Number(process.hrtime.bigint()) / 1e6 - t0) / MS;

  // Everything in flight at the end is counted. Dropping it would be its own
  // flavour of omission, and the requests still running are the slow ones.
  await Promise.all(inflight);
  // Un-honoured aborts are still occupying backend workers with nobody
  // awaiting them. Drain before summarising, or svc_ms/req would report a
  // half-finished bill -- which is exactly the mistake the column exists to
  // catch someone else making.
  for (let i = 0; i < 400 && backends.some((b) => b.inWork > 0); i += 1) await sleep(10);
  lag.disable();
  cell.loopLagP99 = lag.percentile(99) / 1e6;
  cell.backendStarted = backends.reduce((s, b) => s + b.started, 0);
  cell.backendBusyMs = backends.reduce((s, b) => s + b.busyMs, 0);
  return cell;
}

/**
 * Closed model: `vus` virtual users, each waiting before sending again. This is
 * `ramping-vus`, the executor the rest of this layer forbids. It is permitted
 * here and only here, because seeing it lie is the point.
 *
 * Two numbers are recorded per request. The raw one is what a closed-loop
 * generator reports: finish minus send. The corrected one is finish minus the
 * time the request was DUE under the nominal schedule -- because a VU stuck
 * waiting on a slow response is not sending the requests it owed, and those
 * unsent requests are exactly the ones that would have been slow.
 */
async function runClosedCell(k, dist, workers, vus, nominalRate, seconds, seed = SEED) {
  const rngSvc = makeRng(seed + 1);
  const backends = Array.from({ length: k }, () => new Backend(workers, true));
  const gw = new Gateway(backends, dist, rngSvc, null);
  const cell = new Cell();
  cell.gw = gw;
  const perVuIntervalMs = (vus / nominalRate) * MS;

  const t0 = Number(process.hrtime.bigint()) / 1e6;
  const deadline = t0 + seconds * MS;

  await Promise.all(Array.from({ length: vus }, (_, v) => (async () => {
    for (let j = 0; ; j += 1) {
      const start = Number(process.hrtime.bigint()) / 1e6;
      if (start >= deadline) return;
      const due = t0 + (v / nominalRate) * MS + j * perVuIntervalMs;
      await gw.handle(k);
      const fin = Number(process.hrtime.bigint()) / 1e6;
      cell.latMs.push(fin - start);
      cell.correctedMs.push(fin - Math.min(start, due));
    }
  })()));

  cell.arrivalWall = (Number(process.hrtime.bigint()) / 1e6 - t0) / MS;
  cell.backendStarted = backends.reduce((s, b) => s + b.started, 0);
  cell.backendBusyMs = backends.reduce((s, b) => s + b.busyMs, 0);
  return cell;
}

/** Measure ONE backend directly. Everything downstream is relative to this. */
async function calibrate(dist, workers, n, seed) {
  const rng = makeRng(seed);
  const b = new Backend(workers, true);
  const lat = [];
  for (let start = 0; start < n; start += CALIB_BATCH) {
    const batch = Math.min(CALIB_BATCH, n - start);
    await Promise.all(Array.from({ length: batch }, () => (async () => {
      const t0 = process.hrtime.bigint();
      await b.call(dist, rng);
      lat.push(Number(process.hrtime.bigint() - t0) / 1e6);
    })()));
  }
  lat.sort((a, b2) => a - b2);
  const over = lat.filter((v) => v > TAIL_THRESHOLD_MS).length;
  return {
    p50: pct(lat, 0.5), p95: pct(lat, 0.95), p99: pct(lat, 0.99),
    mean: lat.reduce((s, v) => s + v, 0) / lat.length,
    overPct: (100.0 * over) / lat.length,
  };
}

// ----------------------------------------------------------------- output

const HIST_EDGES_MS = [0, 5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120];

function histogram(label, vals) {
  if (!vals.length) return;
  const counts = new Array(HIST_EDGES_MS.length).fill(0);
  for (const v of vals) {
    let placed = false;
    for (let i = 0; i < HIST_EDGES_MS.length - 1; i += 1) {
      if (v >= HIST_EDGES_MS[i] && v < HIST_EDGES_MS[i + 1]) { counts[i] += 1; placed = true; break; }
    }
    if (!placed) counts[counts.length - 1] += 1;
  }
  const peak = Math.max(...counts) || 1;
  console.log(`  ${label}   (n=${vals.length})`);
  for (let i = 0; i < HIST_EDGES_MS.length; i += 1) {
    const lo = String(HIST_EDGES_MS[i]).padStart(6);
    const label2 = i + 1 < HIST_EDGES_MS.length
      ? `${lo} - ${String(HIST_EDGES_MS[i + 1]).padStart(6)} ms`
      : `${lo} +${''.padStart(8)}ms`;
    const bar = '#'.repeat(Math.round((40.0 * counts[i]) / peak));
    console.log(`    ${label2} |${bar.padEnd(40)}| ${String(counts[i]).padStart(6)}`);
  }
}

function rule(title) {
  console.log();
  console.log('='.repeat(78));
  console.log(title);
  console.log('='.repeat(78));
}

const f = (v, w, d = 1) => v.toFixed(d).padStart(w);
const s = (v, w) => String(v).padStart(w);
const sl = (v, w) => String(v).padEnd(w);
const cellRate = (k) => Math.min(MAX_RATE, MAX_BACKEND_CALLS_PER_S / k);

// ------------------------------------------------------------------- main

async function main() {
  const lognormal = new LogNormal(BACKEND_P50_MS, LOGNORMAL_SIGMA);
  const bimodal = new Bimodal(BACKEND_P50_MS, TAIL_THRESHOLD_MS);
  const dists = [lognormal, bimodal];

  rule('Layer 5 - Topic 6: fan-out, hedging and coordinated omission (Node.js)');
  console.log(`  backend p50 configured   ${BACKEND_P50_MS.toFixed(1)} ms`);
  console.log(`  backend p99 configured   ${TAIL_THRESHOLD_MS.toFixed(1)} ms   `
    + `(p99/p50 = ${TAIL_RATIO.toFixed(0)}x, log-normal sigma = ${LOGNORMAL_SIGMA.toFixed(4)})`);
  console.log(`  tail threshold t         ${TAIL_THRESHOLD_MS.toFixed(1)} ms   `
    + 'chosen so P(one leg > t) = 1% for BOTH distributions, by construction');
  console.log('  predicted below          1 - 0.99^K, arithmetic rather than measurement');

  // ------------------------------------------------------------ calibration
  rule('CALIBRATION: one backend, unsaturated, measured directly');
  console.log(`  ${sl('distribution', 12)}${s('p50', 9)}${s('p95', 9)}${s('p99', 9)}`
    + `${s('mean', 9)}${s('P(leg > t)', 13)}`);
  const calib = {};
  for (const d of dists) {
    const c = await calibrate(d, STAT_WORKERS, CALIB_SAMPLES, SEED + 7);
    calib[d.name] = c;
    console.log(`  ${sl(d.name, 12)}${f(c.p50, 7)}ms${f(c.p95, 7)}ms${f(c.p99, 7)}ms`
      + `${f(c.mean, 7)}ms${f(c.overPct, 12, 2)}%`);
  }
  console.log();
  console.log('  P(leg > t) is the measured check on the configured 1%. The hedge delay');
  console.log('  in phase B is the MEASURED p95 above, not the analytic one.');

  // ---------------------------------------------------------------- phase A
  rule('PHASE A: fan-out to K backends, wait for all, no hedging');
  console.log(`  backends have ${STAT_WORKERS} workers each -- they do not queue, so the only`);
  console.log('  mechanism acting on the latency columns is the arithmetic of maxima.');
  console.log('  asm_ms/req is the gateway\'s own JSON.stringify of the K responses, and');
  console.log('  loop_lag_p99 is what that costs everything else sharing the thread. Both');
  console.log('  are measured on this machine at this K -- read whether they rise with K,');
  console.log('  do not assume it.');
  console.log();
  console.log(`  ${sl('dist', 10)}${s('K', 3)}${s('rate', 7)}${s('n', 7)}${s('e2e_p50', 10)}`
    + `${s('e2e_p99', 10)}${s('e2e_max', 10)}${s('predicted', 11)}${s('measured', 10)}`
    + `${s('asm_ms/req', 12)}${s('loop_lag_p99', 14)}`);
  const baseline = {};
  for (const d of dists) {
    for (const k of K_VALUES) {
      const rate = cellRate(k);
      const cell = await runOpenCell(k, d, STAT_WORKERS, rate, SAMPLES_PER_CELL);
      const sm = cell.summary();
      baseline[`${d.name}/${k}`] = sm;
      const predicted = 100.0 * (1.0 - 0.99 ** k);
      console.log(`  ${sl(d.name, 10)}${s(k, 3)}${f(rate, 7, 0)}${s(sm.n, 7)}`
        + `${f(sm.p50, 8)}ms${f(sm.p99, 8)}ms${f(sm.max, 8)}ms${f(predicted, 10)}%`
        + `${f(sm.tail, 9)}%${f(sm.assemblyMsPerReq, 12, 3)}${f(cell.loopLagP99, 12, 2)}ms`);
    }
    console.log();
  }

  // ---------------------------------------------------------------- phase B
  rule('PHASE B: hedging at the measured backend p95, under a 5% token bucket');
  console.log('  Three rows per configuration, identical except for what the backend does');
  console.log('  when your AbortController fires: nothing (no hedge), stop and free the');
  console.log('  worker, or ignore you and run to completion -- which is what a remote');
  console.log('  server does, because your abort never reached it.');
  console.log();
  console.log('  svc_ms/req is the backend service time actually consumed per request. It is');
  console.log('  the column that separates the last two rows: they issue the same calls, and');
  console.log('  only one of them stops paying for the copy it threw away.');
  console.log();
  console.log(`  ${sl('dist', 10)}${s('K', 3)} ${sl('mode', 31)}${s('e2e_p50', 9)}`
    + `${s('e2e_p99', 9)}${s('be_rps', 11)}${s('+load', 7)}${s('svc_ms/req', 11)}`
    + `${s('hedge%', 8)}${s('denied', 7)}`);
  for (const d of dists) {
    const hedgeDelayMs = calib[d.name].p95;
    for (const k of HEDGE_K) {
      const rate = cellRate(k);
      const base = baseline[`${d.name}/${k}`];
      console.log(`  ${sl(d.name, 10)}${s(k, 3)} ${sl('no hedge', 31)}${f(base.p50, 7)}ms`
        + `${f(base.p99, 7)}ms${f(base.backendRps, 10, 0)}/s${s('-', 7)}`
        + `${f(base.svcMsPerReq, 11)}${s('-', 8)}${s('-', 7)}`);
      for (const honour of [true, false]) {
        const label = honour ? 'hedge @p95, abort honoured' : 'hedge @p95, abort NOT honoured';
        const cell = await runOpenCell(k, d, STAT_WORKERS, rate, SAMPLES_PER_CELL,
          { hedgeDelayMs, honourAbort: honour });
        const sm = cell.summary();
        const loadPct = 100.0 * (sm.backendRps / base.backendRps - 1.0);
        console.log(`  ${sl('', 10)}${s('', 3)} ${sl(label, 31)}${f(sm.p50, 7)}ms`
          + `${f(sm.p99, 7)}ms${f(sm.backendRps, 10, 0)}/s${f(loadPct, 6)}%`
          + `${f(sm.svcMsPerReq, 11)}${f(sm.hedgeRate, 7)}%${s(cell.gw.budgetDenied, 7)}`);
      }
      console.log(`  ${sl('', 10)}${s('', 3)}  hedge delay = measured p95 = ${hedgeDelayMs.toFixed(1)} ms`);
      console.log();
    }
  }

  // ---------------------------------------------------------------- phase C
  rule('PHASE C: the same server measured twice -- open model vs closed loop');
  const meanServiceS = calib.lognormal.mean / MS;
  const capacity = CO_WORKERS / meanServiceS;
  const rate = CO_RHO * capacity;
  console.log(`  K = ${CO_K}, log-normal, and this time each backend has only ${CO_WORKERS} workers.`);
  console.log(`  measured mean service time  ${(meanServiceS * MS).toFixed(1)} ms`);
  console.log(`  => capacity per backend     ${capacity.toFixed(1)} rps (${CO_WORKERS} workers / mean service)`);
  console.log(`  => nominal offered rate     ${rate.toFixed(1)} rps  (rho = ${CO_RHO.toFixed(2)})`);
  console.log();
  console.log('  rho is deliberately below 1. Above capacity the open model\'s queue grows');
  console.log('  without bound and its p99 becomes a statement about how long you ran,');
  console.log('  not about the server. Below capacity both numbers mean something.');

  // A short unsaturated pass to size the VU pool by Little's Law.
  const warm = await runOpenCell(CO_K, lognormal, STAT_WORKERS, rate, 600);
  const ws = warm.summary();
  const baseMeanE2eS = (warm.latMs.reduce((a, b) => a + b, 0) / warm.latMs.length) / MS;
  const vus = Math.max(1, Math.round(rate * baseMeanE2eS));
  console.log();
  console.log(`  unsaturated e2e mean at K=${CO_K}: ${(baseMeanE2eS * MS).toFixed(1)} ms `
    + `(p99 ${ws.p99.toFixed(1)} ms)`);
  console.log(`  => closed loop gets ${vus} VUs, from Little's Law: ${rate.toFixed(1)} rps `
    + `x ${(baseMeanE2eS * MS).toFixed(1)} ms.`);
  console.log('     At the healthy latency those VUs issue the nominal rate exactly. That');
  console.log('     is the whole trick: the generator is calibrated on a good day.');

  const openCell = await runOpenCell(CO_K, lognormal, CO_WORKERS, rate, Math.round(rate * CO_SECONDS));
  const os = openCell.summary();
  const closedCell = await runClosedCell(CO_K, lognormal, CO_WORKERS, vus, rate, CO_SECONDS);
  const cs = closedCell.summary();
  const corrected = [...closedCell.correctedMs].sort((a, b) => a - b);

  console.log();
  console.log(`  ${sl('model', 34)}${s('n', 7)}${s('achieved', 11)}${s('p50', 10)}`
    + `${s('p99', 10)}${s('max', 11)}`);
  console.log(`  ${sl('open  (arrival schedule)', 34)}${s(os.n, 7)}`
    + `${f(os.n / openCell.arrivalWall, 9, 0)}/s${f(os.p50, 8)}ms${f(os.p99, 8)}ms${f(os.max, 9)}ms`);
  console.log(`  ${sl(`closed (${vus} VUs), as reported`, 34)}${s(cs.n, 7)}`
    + `${f(cs.n / closedCell.arrivalWall, 9, 0)}/s${f(cs.p50, 8)}ms${f(cs.p99, 8)}ms${f(cs.max, 9)}ms`);
  console.log(`  ${sl('closed, omission-corrected', 34)}${s(corrected.length, 7)}${s('', 11)}`
    + `${f(pct(corrected, 0.5), 8)}ms${f(pct(corrected, 0.99), 8)}ms${f(corrected[corrected.length - 1], 9)}ms`);
  console.log();
  console.log(`  open-model generator lateness p99: ${os.lateP99.toFixed(2)} ms`);
  console.log('  (if that number is large the generator itself fell behind and is now');
  console.log('   coordinating omission too, arrival schedule or not -- k6\'s warning');
  console.log('   about not being able to allocate enough VUs is the same tell.)');
  console.log();
  histogram('open model  ', openCell.latMs);
  console.log();
  histogram('closed loop ', closedCell.latMs);
  console.log();
  console.log('  Same server. Same nominal rate. Read the two histograms\' right-hand');
  console.log('  ends against each other, then read the closed loop\'s raw p99 against');
  console.log('  its corrected p99.');
  console.log();
}

main().catch((e) => { console.error(e); process.exit(1); });
