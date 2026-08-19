/*
 * Layer 5 lab - shared helpers for every k6 script in this directory.
 *
 * WHAT THIS IS
 *   Three things the nine scripts all need, in one place so they cannot
 *   drift apart:
 *
 *     configure()   push a variant's settings to the services through
 *                   POST /admin/config, in setup(). Every experiment here is
 *                   "change exactly one thing and rerun", and doing that from
 *                   the script rather than from the compose file is what makes
 *                   the two runs otherwise identical.
 *
 *     record()      turn the response headers the app sets into k6 metrics, so
 *                   in-flight count, pool wait and zombie flags land in the
 *                   same CSV as the latencies and ../tools/ can plot them
 *                   together.
 *
 *     pollCounters() sample GET /admin/counters once a second and emit the
 *                   DIFFERENCE between samples as a rate. Amplification,
 *                   goodput and cache hit rate are all rates of counters that
 *                   only the server can see, and diffing a monotonic counter
 *                   here beats trusting a rate computed by the thing under
 *                   test.
 *
 * ON THE LOAD MODEL
 *   Every scenario in this directory uses constant-arrival-rate or
 *   ramping-arrival-rate. A closed-loop generator stops sending when the
 *   server slows down, which erases the entire effect this layer exists to
 *   demonstrate. The one deliberate exception is 06_closed_loop.js, which
 *   exists to show you what that erasure looks like.
 */
import http from 'k6/http';
import { Counter, Gauge, Trend } from 'k6/metrics';

export const BASE_URL = __ENV.BASE_URL || 'http://app:8000';
export const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://gateway:8000';
export const SERVICE_B_URL = __ENV.SERVICE_B_URL || 'http://service-b:8000';
export const SERVICE_C_URL = __ENV.SERVICE_C_URL || 'http://service-c:8000';
export const TOXIPROXY_URL = __ENV.TOXIPROXY_URL || 'http://toxiproxy:8474';

/* Server-side numbers that arrive as response headers. */
export const inflight = new Trend('inflight');
export const poolWaitMs = new Trend('pool_wait_ms');
export const poolInUse = new Trend('pool_in_use');
export const serviceMs = new Trend('service_ms');
export const queueWaitMs = new Trend('queue_wait_ms');
export const remainingMs = new Trend('remaining_ms');
export const zombies = new Counter('zombie_completions');
export const shedCount = new Counter('shed_responses');

/* Server-side rates, derived by diffing /admin/counters. */
export const leafReceivedRps = new Trend('leaf_received_rps');
export const throughputRps = new Trend('throughput_rps');
export const goodputRps = new Trend('goodput_rps');
export const retryRps = new Trend('retry_rps');
export const retryRatio = new Trend('retry_ratio');
export const amplification = new Trend('amplification');
export const hitRatePct = new Trend('hit_rate_pct');
export const pgConns = new Gauge('pg_conns');
export const inflightGauge = new Gauge('inflight_gauge');
export const shedLimit = new Gauge('shed_limit');
export const zombieRps = new Trend('zombie_rps');

/**
 * Apply a config patch to one service and fail loudly if it does not take.
 * A sweep that silently ran with the previous variant's settings is worse
 * than a sweep that did not run.
 */
export function configure(url, patch) {
  const res = http.post(`${url}/admin/config`, JSON.stringify(patch), {
    headers: { 'Content-Type': 'application/json' },
    timeout: '10s',
    tags: { admin: 'true' },
  });
  if (res.status !== 200) {
    throw new Error(`configure(${url}) -> ${res.status}: ${String(res.body).slice(0, 300)}`);
  }
  return res.json();
}

/** Zero the counters (and optionally topic 7's tables) before a run. */
export function reset(url, tables) {
  return http.post(`${url}/admin/reset`, JSON.stringify({ tables: !!tables }), {
    headers: { 'Content-Type': 'application/json' },
    timeout: '30s',
    tags: { admin: 'true' },
  });
}

/** Read the current config, so a run can compute capacity from what is real. */
export function readConfig(url) {
  const res = http.get(`${url}/admin/config`, { timeout: '10s', tags: { admin: 'true' } });
  if (res.status !== 200) {
    throw new Error(`readConfig(${url}) -> ${res.status}`);
  }
  return res.json();
}

/** lambda_max = (pool_size + max_overflow) / service_time. Little's Law, rearranged. */
export function capacityRps(cfg) {
  const slots = Number(cfg.POOL_SIZE) + Number(cfg.MAX_OVERFLOW);
  const seconds = Number(cfg.SERVICE_MS) / 1000.0;
  return slots / seconds;
}

function num(headers, name) {
  const raw = headers[name];
  if (raw === undefined || raw === null || raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/**
 * Turn one response into metrics. Call this for every measured request.
 * `extra` becomes tags on every sample, which is how the plotters group.
 */
export function record(res, extra) {
  const tags = extra || {};
  const h = res.headers || {};
  const v = (name) => num(h, name);
  const put = (metric, value) => { if (value !== null) metric.add(value, tags); };
  put(inflight, v('X-Inflight'));
  put(poolWaitMs, v('X-Pool-Wait-Ms'));
  put(poolInUse, v('X-Pool-In-Use'));
  put(serviceMs, v('X-Service-Ms'));
  put(queueWaitMs, v('X-Queue-Wait-Ms'));
  put(remainingMs, v('X-Remaining-Ms'));
  if (h['X-Zombie'] === '1') zombies.add(1, tags);
  if (h['X-Shed'] === '1' || res.status === 503) shedCount.add(1, tags);
  return res;
}

/**
 * One poller iteration: diff this sample of /admin/counters against the last.
 *
 * `state` is a per-VU object you own; pass the same one every iteration.
 * Rates are emitted only from the second sample onward, because the first
 * diff would be against a zero that never happened.
 */
export function pollCounters(url, state, tags, offeredRps) {
  const res = http.get(`${url}/admin/counters`, { timeout: '5s', tags: { admin: 'true' } });
  if (res.status !== 200) return state;
  const now = res.json();
  const t = Number(now.now_ms) / 1000.0;
  const previous = state.previous;
  const out = { previous: now, t };
  if (previous) {
    const dt = t - Number(previous.now_ms) / 1000.0;
    if (dt > 0.05) {
      const rate = (name) => (Number(now[name] || 0) - Number(previous[name] || 0)) / dt;
      const received = rate('received');
      const completed = rate('completed');
      const retried = rate('retries');
      leafReceivedRps.add(received, tags);
      throughputRps.add(received, tags);
      goodputRps.add(completed, tags);
      retryRps.add(retried, tags);
      retryRatio.add(received > 0 ? retried / received : 0, tags);
      zombieRps.add(rate('zombies'), tags);
      if (offeredRps && offeredRps > 0) {
        amplification.add(received / offeredRps, tags);
      }
    }
  }
  hitRatePct.add(Number(now.cache_hit_rate_pct || 0), tags);
  pgConns.add(Number(now.pool_in_use || 0), tags);
  inflightGauge.add(Number(now.inflight || 0), tags);
  shedLimit.add(Number(now.shedder_limit || 0), tags);
  return out;
}

/** Add or remove a toxiproxy toxic. The fault window belongs in the script. */
export function addToxic(proxy, body) {
  return http.post(`${TOXIPROXY_URL}/proxies/${proxy}/toxics`, JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
    timeout: '10s',
    tags: { admin: 'true' },
  });
}

export function removeToxic(proxy, name) {
  return http.del(`${TOXIPROXY_URL}/proxies/${proxy}/toxics/${name}`, null, {
    timeout: '10s',
    tags: { admin: 'true' },
  });
}

/**
 * Standard handleSummary: k6's own text summary on stdout, plus the raw
 * summary JSON in /out next to the CSV. The CSV is what ../tools/ plots;
 * the JSON is what you read when a run looks wrong and you want to know
 * whether k6 itself fell behind (`dropped_iterations`).
 */
export function summaryTo(name, data, textSummary) {
  const out = {};
  out.stdout = textSummary
    ? textSummary(data, { indent: ' ', enableColors: false })
    : JSON.stringify(data.metrics, null, 2);
  out[`/out/${name}_summary.json`] = JSON.stringify(data, null, 2);
  return out;
}

/**
 * Warn in the run's own output if k6 could not keep up.
 * An arrival-rate executor that cannot allocate VUs has quietly become a
 * closed-loop generator, and every number in the run is then a measurement
 * of k6 rather than of the service.
 */
export function dropWarning(data) {
  const dropped = data.metrics.dropped_iterations
    ? data.metrics.dropped_iterations.values.count
    : 0;
  if (dropped > 0) {
    return `\nWARNING: k6 dropped ${dropped} iterations - it could not sustain the target\n` +
           `arrival rate, so this run is partly closed-loop. Raise preAllocatedVUs and rerun\n` +
           `before believing any percentile in it.\n`;
  }
  return '';
}
