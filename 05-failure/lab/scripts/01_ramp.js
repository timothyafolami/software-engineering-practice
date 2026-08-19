/*
 * Layer 5 · Topic 1 - the latency knee, on the real stack.
 *
 * WHAT THIS DEMONSTRATES
 *   Queueing delay is proportional to 1/(1-rho), not to load. This sweeps
 *   offered rate from 20% to 110% of the service's computed capacity in
 *   separate scenarios, one per rho, each tagged with its own rho so the
 *   steps never blur into each other.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   1. Achieved rate plateaus while offered rate keeps climbing. The plateau
 *      should equal (pool_size + max_overflow) / service_time - Little's Law
 *      rearranged, checked against a real pool rather than asserted.
 *   2. p99 leaves the predicted S/(1-rho) line behind once rho >= 1, because
 *      that formula assumes a stable system and past capacity there is not one.
 *   3. `pool_wait_ms` is ~0 at rho=0.2 and dominates by rho=0.9. If it is not
 *      ~0 at rho=0.2, capacity was measured wrong and you were never at 20%.
 *   4. `inflight` should equal offered_rate x latency. If it does not, one of
 *      your three numbers is lying, and that disagreement is the finding.
 *
 * RUN
 *   docker compose up -d --build
 *   docker compose run --rm k6 run /scripts/01_ramp.js --out csv=/out/ramp.csv
 *   python3 tools/plot_knee.py out/ramp.csv
 *
 *   Then change ONE thing and rerun:
 *   docker compose run --rm k6 run /scripts/01_ramp.js -e POOL_SIZE=10 \
 *     --out csv=/out/ramp_pool10.csv
 *
 * ENV
 *   POOL_SIZE   applied to the service before the sweep starts (default 5)
 *   STEP        seconds per rho step (default 30; the topic says at least 30)
 *   ENDPOINT    which handler to load (default /work)
 */
import http from 'k6/http';
import { BASE_URL, capacityRps, configure, readConfig, record, reset, dropWarning, summaryTo } from './lib/harness.js';

const STEP = Number(__ENV.STEP || 30);
const ENDPOINT = __ENV.ENDPOINT || '/work';
const RHOS = [0.2, 0.5, 0.8, 0.9, 0.95, 1.1];

/*
 * Capacity is computed from the service's own configuration in setup(), but
 * k6 needs the scenario rates before setup() runs. So the scenarios are built
 * from the same arithmetic against the values this script is about to APPLY -
 * and setup() then asserts that the service agrees. A sweep whose rho column
 * is computed against a capacity the server does not have is the single most
 * common way to get a knee-free chart out of a service that has a knee.
 */
const POOL_SIZE = Number(__ENV.POOL_SIZE || 5);
const MAX_OVERFLOW = Number(__ENV.MAX_OVERFLOW || 10);
const SERVICE_MS = Number(__ENV.SERVICE_MS || 40);
const CAPACITY = (POOL_SIZE + MAX_OVERFLOW) / (SERVICE_MS / 1000.0);

const scenarios = {};
RHOS.forEach((rho, i) => {
  const rate = Math.max(1, Math.round(rho * CAPACITY));
  scenarios[`rho_${String(rho).replace('.', '')}`] = {
    executor: 'constant-arrival-rate',
    rate: rate,
    timeUnit: '1s',
    duration: `${STEP}s`,
    startTime: `${i * (STEP + 5)}s`,       // 5s of drain between steps
    // Enough VUs that the generator never has to wait for one. If k6 warns
    // that it cannot allocate enough, it has become the bottleneck and is
    // coordinating omission itself - raise this and rerun.
    preAllocatedVUs: Math.max(50, rate * 3),
    maxVUs: Math.max(200, rate * 10),
    tags: { rho: String(rho), offered_rps: String(rate), pool: String(POOL_SIZE) },
    exec: 'work',
    gracefulStop: '5s',
  };
});

export const options = {
  scenarios: scenarios,
  // Percentiles, not averages, and the tail of the tail: this whole layer is
  // about what happens past p99.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
  discardResponseBodies: false,
  thresholds: {},
};

export function setup() {
  configure(BASE_URL, {
    POOL_SIZE: POOL_SIZE,
    MAX_OVERFLOW: MAX_OVERFLOW,
    SERVICE_MS: SERVICE_MS,
    SHED_MODE: 'none',
    RETRY_ATTEMPTS: 1,
    PROPAGATE_DEADLINE: 0,
    BULKHEAD: 0,
  });
  reset(BASE_URL, false);
  const cfg = readConfig(BASE_URL);
  const measured = capacityRps(cfg);
  if (Math.abs(measured - CAPACITY) > 0.01) {
    throw new Error(`capacity mismatch: script assumed ${CAPACITY} rps, service reports ${measured}`);
  }
  console.log(`pool=${cfg.POOL_SIZE}+${cfg.MAX_OVERFLOW}  S=${cfg.SERVICE_MS}ms  ` +
              `lambda_max = ${cfg.POOL_SIZE}+${cfg.MAX_OVERFLOW} / ${cfg.SERVICE_MS}ms = ${measured.toFixed(1)} rps`);
  return { capacity: measured };
}

export function work() {
  const res = http.get(`${BASE_URL}${ENDPOINT}`, {
    timeout: '60s',    // long on purpose: a client timeout here would hide the queue
    tags: { endpoint: 'work' },
  });
  record(res, {});
}

export function handleSummary(data) {
  const out = summaryTo('01_ramp', data);
  out.stdout = `\nLayer 5 / topic 1 - ramp complete. capacity assumed ${CAPACITY.toFixed(1)} rps\n` +
               `Plot it:  python3 tools/plot_knee.py out/ramp.csv\n` +
               dropWarning(data);
  return out;
}
