// Topic 2 -- fire K unique keys, each DUPES times, SIMULTANEOUSLY from
// different VUs. Firing duplicates sequentially from one VU tests nothing:
// the first one has already committed by the time the second is sent.
//
// The simultaneity is arranged by having every VU walk the SAME key list in
// the same order at the same time, so VU1..VUn all hit key i together.
import http from 'k6/http';

const BASE  = __ENV.BASE  || 'http://payments-api:8000';
const KEYS  = parseInt(__ENV.KEYS  || '200', 10);
const DUPES = parseInt(__ENV.DUPES || '5', 10);   // = VUs; one per duplicate
const RUN   = __ENV.RUN   || 'compose';

export const options = {
  scenarios: { dup: { executor: 'per-vu-iterations', vus: DUPES,
                      iterations: KEYS, maxDuration: '10m' } },
};

export function setup() {
  const r = http.get(`${BASE}/health`, { timeout: '10s' });
  if (r.status !== 200) {
    throw new Error(`*** BROKEN RUN: ${BASE}/health -> ${r.status} ` +
                    `(${r.error || 'no error'}) ***`);
  }
  return {};
}

export default function () {
  const key = `${RUN}-key-${__ITER}`;      // same key across all VUs at this iter
  http.post(`${BASE}/payments`,
            JSON.stringify({ tenant_id: 't1', key: key, amount_cents: 100 }),
            { headers: { 'Content-Type': 'application/json' }, timeout: '30s' });
}
