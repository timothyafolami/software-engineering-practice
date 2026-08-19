// Topic 6 -- steady load on POST /payments while the broker is stopped and
// restarted underneath it. Constant arrival rate, not shared-iterations: the
// question is what happens to requests arriving DURING the outage.
import http from 'k6/http';

const BASE = __ENV.BASE || 'http://payments-api:8000';
const RATE = parseInt(__ENV.RATE || '20', 10);
const DUR  = __ENV.DUR || '60s';

export const options = {
  scenarios: { load: { executor: 'constant-arrival-rate', rate: RATE,
                       timeUnit: '1s', duration: DUR,
                       preAllocatedVUs: 20, maxVUs: 50 } },
};

export function setup() {
  const r = http.get(`${BASE}/health`, { timeout: '10s' });
  if (r.status !== 200) throw new Error(`*** BROKEN RUN: ${BASE}/health -> ${r.status} ***`);
  return {};
}

export default function () {
  http.post(`${BASE}/payments`, null, { timeout: '30s' });
}
