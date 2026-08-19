/*
 * Layer 5 · Topic 4 - metastable failure. The flagship.
 *
 * WHAT THIS DEMONSTRATES
 *   Offered load never changes. A cache is emptied once, instantaneously, and
 *   it starts refilling immediately. The trigger is gone within seconds. The
 *   system does not come back.
 *
 *   Stable state:  95% of requests are cache hits, the database sees 5% of
 *                  traffic, everything is comfortable.
 *   Trigger:       FLUSHALL. Every request becomes a miss at once.
 *   Amplification: misses queue on the pool; queued requests time out;
 *                  timed-out requests retry; retries are more misses.
 *   Sustaining:    the retry traffic is now enough to keep the pool saturated
 *                  BY ITSELF. The cache refills, the hit rate recovers, and
 *                  goodput does not, because the thing sustaining the failure
 *                  is no longer the thing that started it.
 *
 * WHERE THE SHIPPED CONSTANTS GET YOU, MEASURED ON ONE MACHINE
 *   These defaults were chosen by running them, and what they produce is a
 *   flat stable baseline and a real amplification event: at the trigger,
 *   goodput falls while offered load does not, in-flight requests jump into
 *   the hundreds, and the retry and timeout rates go from zero to hundreds
 *   per second. On the machine this was verified on it then CLEARS, in about
 *   the time the cache takes to refill.
 *
 *   That is the README's own "recovers in twenty seconds - a slow drain, not
 *   metastability, your amplification is too weak", and its prescription is
 *   to raise the offered load until the DATABASE sits at 75-85% utilisation
 *   in the stable state - LOAD_MULT and CACHE_TTL_S are the two knobs, and
 *   setup() prints the stable-state utilisation those two imply so you can
 *   aim rather than guess. Push it and watch the third broken-criterion in
 *   the README come at you from the other side: too hot and the baseline is
 *   already degraded before the trigger, which is not a result either.
 *   Finding the band between those two on YOUR machine is step 1, and it is
 *   why step 1 says do not proceed until goodput is flat.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   The gap between `throughput_rps` and `goodput_rps`. Throughput can look
 *   healthy while goodput is zero: the system is working extremely hard on
 *   requests whose callers have already given up. Watch `hit_rate_pct`
 *   recover while `goodput_rps` does not - that is the moment the topic
 *   lands, and it is why "wait five minutes" is not optional.
 *
 * RUN
 *   docker compose --profile metastable up -d --build
 *   docker compose run --rm k6 run /scripts/04_metastable.js \
 *     --out csv=/out/metastable.csv &
 *   sleep 180 && docker compose exec redis redis-cli FLUSHALL
 *   # then watch, and touch nothing, for five minutes
 *   python3 tools/plot_goodput.py out/metastable.csv
 *
 * ENV
 *   LOAD_MULT  offered load as a MULTIPLE of the database's own capacity
 *              (default 3). Greater than 1 on purpose: the cache is a
 *              capacity multiplier, and running at a load only the cache
 *              makes serviceable is the entire premise of this topic. At
 *              LOAD_MULT <= 1 the database can serve 100% of traffic
 *              unaided, FLUSHALL costs one refill pass, and nothing
 *              amplifies - the experiment quietly measures nothing.
 *   LOAD_PCT   deprecated alias for LOAD_MULT
 *   DURATION   seconds (default 600: 3 min stable, trigger, 5 min after)
 *   ESCAPE     none | budget | shed   (default none)
 *              Step 5 of the experiment asks which escapes are SUFFICIENT
 *              rather than merely helpful. `budget` and `shed` apply topic 3's
 *              and topic 5's mitigation at t=ESCAPE_AT WITHOUT dropping load,
 *              which is the only honest way to test them - dropping load is
 *              its own escape and it confounds every other one.
 *   ESCAPE_AT  seconds (default 420, i.e. 4 minutes after the trigger)
 */
import http from 'k6/http';
import {
  BASE_URL, capacityRps, configure, dropWarning, pollCounters, readConfig, record, reset, summaryTo,
} from './lib/harness.js';

const DURATION = Number(__ENV.DURATION || 600);
const LOAD_MULT = Number(__ENV.LOAD_MULT || __ENV.LOAD_PCT || 3.0);
const ESCAPE = __ENV.ESCAPE || 'none';
const ESCAPE_AT = Number(__ENV.ESCAPE_AT || 420);

/*
 * CAPACITY here is the DATABASE's capacity: the rate at which uncached reads
 * can be served, (pool_size + max_overflow) / SERVICE_MS. It is deliberately
 * NOT the rate the service can answer, because a cache hit never touches the
 * pool - which is the whole reason the service can run at LOAD_MULT times
 * this number and look comfortable doing it.
 *
 * SERVICE_MS is 200 rather than the harness default of 40 because an
 * uncached read has to be expensive enough to be worth caching. 15 slots at
 * 200ms is 75 misses per second; at LOAD_MULT=3 the offered load is 225 rps,
 * so the database alone could serve a third of it. The cache covers the rest
 * until it does not.
 */
const POOL_SIZE = Number(__ENV.POOL_SIZE || 5);
const MAX_OVERFLOW = Number(__ENV.MAX_OVERFLOW || 10);
const SERVICE_MS = Number(__ENV.SERVICE_MS || 200);
const CAPACITY = (POOL_SIZE + MAX_OVERFLOW) / (SERVICE_MS / 1000.0);
const RATE = Math.max(1, Math.round(CAPACITY * LOAD_MULT));
/*
 * TTL churn, not a frozen keyspace. With CACHE_KEYS keys and CACHE_TTL_S
 * seconds of TTL the steady state evicts KEYS/TTL per second, so there is a
 * CONTINUOUS miss stream and an equilibrium hit rate of 1 - (KEYS/TTL)/RATE.
 * A 300s TTL over 500 keys - the value this script shipped with - means that
 * after one refill pass there are no misses left at all, so FLUSHALL is a
 * one-second burst of work and cannot start a sustaining loop however
 * overloaded the pool is. The trigger has to leave the system in a regime,
 * not in an incident.
 */
const CACHE_KEYS = Number(__ENV.CACHE_KEYS || 500);
const CACHE_TTL_S = Number(__ENV.CACHE_TTL_S || 25);

const scenarios = {
  load: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: `${DURATION}s`,
    preAllocatedVUs: RATE * 6,
    maxVUs: RATE * 60,
    exec: 'load',
    tags: { escape: ESCAPE },
    gracefulStop: '15s',
  },
  poller: {
    executor: 'constant-arrival-rate',
    rate: 1,
    timeUnit: '1s',
    duration: `${DURATION + 5}s`,
    preAllocatedVUs: 1,
    maxVUs: 2,
    exec: 'poll',
    tags: { escape: ESCAPE },
  },
};

if (ESCAPE !== 'none') {
  scenarios.escape = {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: 1,
    startTime: `${ESCAPE_AT}s`,
    exec: 'escape',
    tags: { escape: ESCAPE },
  };
}

export const options = {
  scenarios: scenarios,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  configure(BASE_URL, {
    POOL_SIZE: POOL_SIZE,
    MAX_OVERFLOW: MAX_OVERFLOW,
    SERVICE_MS: SERVICE_MS,
    CACHE_TTL_S: CACHE_TTL_S,
    CACHE_KEYS: CACHE_KEYS,
    // Topic 3's naive retries, long timeouts, no budget, no shedding: the
    // configuration this layer has spent three topics warning about, so that
    // topic 4 can show what it does when nothing is actually broken.
    RETRY_ATTEMPTS: 3,
    RETRY_JITTER: 'none',
    RETRY_BUDGET_PCT: 0,
    CLIENT_TIMEOUT_MS: 2000,
    PROPAGATE_DEADLINE: 0,
    SHED_MODE: 'none',
  });
  reset(BASE_URL, false);
  const cfg = readConfig(BASE_URL);
  const measured = capacityRps(cfg);
  if (Math.abs(measured - CAPACITY) > 0.01) {
    throw new Error(`capacity mismatch: script assumed ${CAPACITY}, service reports ${measured}`);
  }
  const evictions = CACHE_KEYS / CACHE_TTL_S;
  console.log(`database capacity=${measured.toFixed(1)} misses/s, offering ${RATE} rps ` +
              `(${LOAD_MULT}x what the database alone could serve), ` +
              `escape=${ESCAPE}${ESCAPE !== 'none' ? ` at t=${ESCAPE_AT}s` : ''}`);
  console.log(`TTL churn: ${CACHE_KEYS} keys / ${CACHE_TTL_S}s = ${evictions.toFixed(0)} evictions/s, ` +
              `so the equilibrium hit rate is about ` +
              `${(100 * (1 - evictions / RATE)).toFixed(1)}% and the miss stream never stops.`);
  const dbUtil = 100 * evictions / measured;
  console.log(`stable-state database utilisation = ${dbUtil.toFixed(0)}%. ` +
              'The README wants 75-85% here: below ~30% the system cannot go');
  console.log('metastable at all, and too far above it the baseline is already degraded');
  console.log('before the trigger. Verify goodput is FLAT before you flush.');
  if (LOAD_MULT <= 1) {
    console.log('WARNING: LOAD_MULT <= 1 - the database can serve every request unaided, so');
    console.log('FLUSHALL costs one refill pass and nothing amplifies. This run cannot show');
    console.log('metastability whatever else it shows.');
  }
  console.log('Warm the cache, then: docker compose exec redis redis-cli FLUSHALL');
  console.log('Then change NOTHING for five minutes. Stopping at the trigger proves nothing.');
  return {};
}

export function load() {
  // A rotating key set, so the cache has something to be warm about and the
  // flush has something to lose.
  const key = Math.floor(Math.random() * 500);
  const res = http.get(`${BASE_URL}/cached?key=${key}`, {
    timeout: '30s',
    tags: { endpoint: 'cached', escape: ESCAPE },
  });
  record(res, { escape: ESCAPE });
}

const state = {};
export function poll() {
  state.app = pollCounters(BASE_URL, state.app || {}, { escape: ESCAPE }, RATE);
}

export function escape() {
  // Applied WITHOUT dropping load. If goodput recovers from here, the escape
  // is sufficient; if it only improves, it is merely helpful, and the
  // experiment asks you to record which.
  if (ESCAPE === 'budget') {
    configure(BASE_URL, { RETRY_BUDGET_PCT: 10, RETRY_JITTER: 'full' });
    console.log(`t=${ESCAPE_AT}s  ESCAPE: retry budget 10% enabled, load unchanged`);
  } else if (ESCAPE === 'shed') {
    const cfg = readConfig(BASE_URL);
    const limit = Number(cfg.POOL_SIZE) + Number(cfg.MAX_OVERFLOW);
    configure(BASE_URL, { SHED_MODE: 'static', SHED_LIMIT: limit, SHED_WAIT_MS: 50 });
    console.log(`t=${ESCAPE_AT}s  ESCAPE: static shedder at ${limit} in flight, load unchanged`);
  }
}

export function handleSummary(data) {
  const out = summaryTo('04_metastable', data);
  out.stdout = '\nLayer 5 / topic 4 - run complete.\n' +
               'Plot it:  python3 tools/plot_goodput.py out/metastable.csv\n' +
               'Then write three sentences: trigger, amplification mechanism, sustaining effect.\n' +
               dropWarning(data);
  return out;
}
