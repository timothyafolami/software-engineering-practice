// Topic 4 -- write, wait GAP_MS, read the same entity in the same session.
// The session header is the whole point: without a stable session identity the
// sticky and lsn fixes have nothing to key on and both would look like `none`.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE   = __ENV.BASE   || 'http://api:8000';
const GAP_MS = parseInt(__ENV.GAP_MS || '0', 10);
const VUS    = parseInt(__ENV.VUS    || '10', 10);
const ITERS  = parseInt(__ENV.ITERS  || '300', 10);

export const options = {
  scenarios: {
    rw: { executor: 'shared-iterations', vus: VUS, iterations: ITERS,
          maxDuration: '5m' },
  },
  thresholds: {},
};

// A run that cannot reach its target is a BROKEN RUN, not a result. k6 exits 0
// on connection failures, so without this the whole matrix "passes" and every
// deliverable query returns zero rows.
export function setup() {
  const r = http.get(`${BASE}/health`, { timeout: '10s' });
  if (r.status !== 200) {
    throw new Error(`*** BROKEN RUN: ${BASE}/health -> status ${r.status} ` +
                    `(${r.error || 'no error'}). Nothing below would be a ` +
                    `measurement. Check BASE and the service name. ***`);
  }
  return { base: BASE };
}

export default function () {
  const sess = `vu${__VU}`;
  const ent  = `e-${__VU}-${__ITER}`;
  const val  = `v-${__VU}-${__ITER}-${Date.now()}`;
  const H    = { headers: { 'X-Session': sess } };

  const w = http.post(`${BASE}/write?entity=${ent}&value=${val}`, null, H);
  check(w, { 'write 200': (r) => r.status === 200 });

  if (GAP_MS > 0) sleep(GAP_MS / 1000);

  const r = http.get(
    `${BASE}/read?entity=${ent}&expect=${val}&gap_ms=${GAP_MS}`, H);
  check(r, { 'read 200': (x) => x.status === 200 });
}
