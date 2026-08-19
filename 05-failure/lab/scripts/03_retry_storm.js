/*
 * Layer 5 · Topic 3 - retry amplification, and the three things that bound it.
 *
 * WHAT THIS DEMONSTRATES
 *   Three hops, each retrying up to 3 times, is up to 27 requests at the leaf
 *   for one at the edge - and the leaf is the thing that was already
 *   struggling. Toxiproxy breaks the leaf's database for 20 seconds at t=60s
 *   and repairs it at t=80s. The run continues to t=300s, because the whole
 *   question is what the system is doing two minutes after the fault is gone.
 *
 *   VARIANT=naive       3 attempts, no jitter, no budget
 *   VARIANT=jitter      full jitter on the backoff
 *   VARIANT=budget      full jitter plus a 10% token-bucket budget per hop
 *   VARIANT=edge_only   retries ONLY at the hop next to the database;
 *                       everything above propagates the failure upward
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   `amplification` = the leaf's received rate divided by the offered rate.
 *   Its peak during the fault is the obvious number. Its value at t=200s is
 *   the one that matters: a system still amplifying two minutes after its
 *   trigger is gone is in a metastable state, which is topic 4.
 *
 *   Note which of these is not like the others. Jitter changes WHEN the
 *   retries arrive; a cap changes how many per request. Only the budget
 *   changes the total, because it is refilled by successes - when everything
 *   is failing, nothing refills it and retries stop by themselves.
 *
 * RUN
 *   docker compose --profile chain up -d --build
 *   for V in naive jitter budget edge_only; do
 *     docker compose run --rm k6 run /scripts/03_retry_storm.js -e VARIANT=$V \
 *       --out csv=/out/03_retry_storm_$V.csv
 *   done
 *   python3 tools/plot_amplification.py out/
 *
 * ENV
 *   VARIANT     naive | jitter | budget | edge_only   (default naive)
 *   RATE        offered rps (default 50)
 *   DURATION    total seconds (default 300)
 *   FAULT_AT    seconds until the fault (default 60)
 *   FAULT_FOR   seconds the fault lasts (default 20)
 */
import http from 'k6/http';
import {
  GATEWAY_URL, SERVICE_B_URL, SERVICE_C_URL,
  addToxic, configure, dropWarning, pollCounters, record, removeToxic, reset, summaryTo,
} from './lib/harness.js';

const VARIANT = __ENV.VARIANT || 'naive';
const RATE = Number(__ENV.RATE || 50);
const DURATION = Number(__ENV.DURATION || 300);
const FAULT_AT = Number(__ENV.FAULT_AT || 60);
const FAULT_FOR = Number(__ENV.FAULT_FOR || 20);

const VARIANTS = {
  naive:     { attempts: 3, jitter: 'none', budget: 0,  edgeOnly: false },
  jitter:    { attempts: 3, jitter: 'full', budget: 0,  edgeOnly: false },
  budget:    { attempts: 3, jitter: 'full', budget: 10, edgeOnly: false },
  edge_only: { attempts: 3, jitter: 'full', budget: 0,  edgeOnly: true },
};

if (!VARIANTS[VARIANT]) {
  throw new Error(`unknown VARIANT=${VARIANT}; expected one of ${Object.keys(VARIANTS).join('|')}`);
}

export const options = {
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: `${DURATION}s`,
      preAllocatedVUs: RATE * 6,
      maxVUs: RATE * 60,
      exec: 'load',
      tags: { variant: VARIANT },
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
      tags: { variant: VARIANT },
    },
    // The fault window belongs in the script. A window you type by hand is a
    // window you cannot reproduce, and every comparison here depends on the
    // four runs having had the same one.
    faultOn: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      startTime: `${FAULT_AT}s`,
      exec: 'faultOn',
      tags: { variant: VARIANT },
    },
    faultOff: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      startTime: `${FAULT_AT + FAULT_FOR}s`,
      exec: 'faultOff',
      tags: { variant: VARIANT },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const v = VARIANTS[VARIANT];
  const common = {
    CLIENT_TIMEOUT_MS: 500,
    RETRY_BASE_MS: 50,
    RETRY_JITTER: v.jitter,
    RETRY_BUDGET_PCT: v.budget,
    PROPAGATE_DEADLINE: 0,
    SHED_MODE: 'none',
  };
  // edge_only: only the hop ADJACENT to the database retries. The hops above
  // it propagate the failure upward instead of each rediscovering it.
  configure(GATEWAY_URL, Object.assign({}, common,
    { SERVICE_MS: 5, RETRY_ATTEMPTS: v.edgeOnly ? 1 : v.attempts }));
  configure(SERVICE_B_URL, Object.assign({}, common,
    { SERVICE_MS: 5, RETRY_ATTEMPTS: v.edgeOnly ? 1 : v.attempts }));
  configure(SERVICE_C_URL, Object.assign({}, common,
    { SERVICE_MS: 40, RETRY_ATTEMPTS: v.attempts }));
  [GATEWAY_URL, SERVICE_B_URL, SERVICE_C_URL].forEach((u) => reset(u, false));
  // Leave no toxic behind from a previous run, or the "before" window is a lie.
  removeToxic('postgres', 'storm');
  console.log(`variant=${VARIANT} attempts=${v.attempts} jitter=${v.jitter} ` +
              `budget=${v.budget}% edge_only=${v.edgeOnly}  offered=${RATE} rps  ` +
              `fault ${FAULT_AT}s..${FAULT_AT + FAULT_FOR}s of ${DURATION}s`);
  return {};
}

export function load() {
  const res = http.get(`${GATEWAY_URL}/chain`, {
    timeout: '30s',
    tags: { endpoint: 'chain', variant: VARIANT },
  });
  record(res, { variant: VARIANT });
}

const state = {};
export function poll() {
  state.c = pollCounters(SERVICE_C_URL, state.c || {}, { hop: 'service_c', variant: VARIANT }, RATE);
  state.g = pollCounters(GATEWAY_URL, state.g || {}, { hop: 'gateway', variant: VARIANT }, RATE);
}

export function faultOn() {
  // A timeout toxic with timeout:0 holds the connection open and never
  // answers: the leaf's database becomes SLOW rather than absent, which is
  // the case this layer exists for. An outright refusal is the easy failure.
  const res = addToxic('postgres', {
    name: 'storm',
    type: 'timeout',
    stream: 'upstream',
    toxicity: 1.0,
    attributes: { timeout: 0 },
  });
  console.log(`t=${FAULT_AT}s  FAULT ON  (toxiproxy -> ${res.status})`);
}

export function faultOff() {
  const res = removeToxic('postgres', 'storm');
  console.log(`t=${FAULT_AT + FAULT_FOR}s  FAULT OFF (toxiproxy -> ${res.status}) ` +
              `- everything from here is recovery, and it is the part that matters`);
}

export function teardown() {
  removeToxic('postgres', 'storm');
  const c = http.get(`${SERVICE_C_URL}/admin/zombies`).json();
  const b = http.get(`${SERVICE_B_URL}/admin/zombies`).json();
  const g = http.get(`${GATEWAY_URL}/admin/zombies`).json();
  // Retries are counted at the hop that ISSUES them, and C is the leaf - it
  // has no downstream, so c.retries is structurally 0 in every variant and
  // every run. The retries that make the amplification are the gateway's and
  // B's, so those are the ones printed.
  const retries = Number(g.retries) + Number(b.retries);
  const amp = Number(c.received) / Math.max(1, Number(g.received));
  console.log(`\nvariant=${VARIANT}  offered(gateway)=${g.received}  leaf received=${c.received}` +
              `  amplification=${amp.toFixed(2)}x` +
              `\n            retries issued: gateway=${g.retries} + B=${b.retries} = ${retries}` +
              `   gateway success=` +
              `${(100 * Number(g.completed) / Math.max(1, Number(g.received))).toFixed(1)}%`);
}

export function handleSummary(data) {
  const out = summaryTo(`03_retry_storm_${VARIANT}`, data);
  out.stdout = `\nLayer 5 / topic 3 - variant ${VARIANT} complete.\n` +
               'Plot all four together:  python3 tools/plot_amplification.py out/\n' +
               'Read amplification at t=200s, not at its peak.\n' +
               dropWarning(data);
  return out;
}
