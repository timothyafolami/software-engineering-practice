// Topic 1 -- drive payments-api. Each iteration is one logical payment with a
// unique request_id; payments-api is the client of `ledger` and records its own
// belief per ATTEMPT. The diff against t1_charges is sql/topic1_reconcile.sql.
import http from 'k6/http';

const BASE  = __ENV.BASE  || 'http://payments-api:8000';
const TOXIC = __ENV.TOXIC || 'none';
const VUS   = parseInt(__ENV.VUS   || '10', 10);
const ITERS = parseInt(__ENV.ITERS || '100', 10);

export const options = {
  scenarios: { pay: { executor: 'shared-iterations', vus: VUS,
                      iterations: ITERS, maxDuration: '5m' } },
  // No thresholds: under `timeout` and `crash_after_commit` most requests are
  // SUPPOSED to fail. A threshold here would just make k6 exit non-zero on a
  // successful experiment.
};

// A run that cannot reach its target is a BROKEN RUN, not a result. k6 exits 0
// on connection failures, so without this the whole matrix "passes" and every
// deliverable query returns zero rows.
export function setup() {
  if (!__ENV.TOXIC) {
    throw new Error('*** BROKEN RUN: TOXIC is unset. Every row would be ' +
                    'labelled "none" and all six faults would land in one ' +
                    'bucket with colliding request ids. Pass -e TOXIC=... ***');
  }
  const r = http.get(`${BASE}/health`, { timeout: '10s' });
  if (r.status !== 200) {
    throw new Error(`*** BROKEN RUN: ${BASE}/health -> status ${r.status} ` +
                    `(${r.error || 'no error'}). Nothing below would be a ` +
                    `measurement. Check BASE and the service name. ***`);
  }
  return { base: BASE };
}

export default function () {
  const rid = `${TOXIC}-${__VU}-${__ITER}`;
  http.post(`${BASE}/pay`, JSON.stringify({ request_id: rid, toxic: TOXIC }),
            { headers: { 'Content-Type': 'application/json' }, timeout: '30s' });
}
