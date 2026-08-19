// Layer 6 lab - arrival.js: open-loop, 300 RPS. Topics 2 and 3.
//
// The same requests as steady.js under a different generator design. This one
// issues at a fixed arrival rate regardless of what came back, so when the
// server stalls the requests pile up and every one of them is measured. That
// is the difference between a latency measurement and a measurement of how
// often your generator felt like asking.
//
// `preAllocatedVUs` and `maxVUs` are the honest cost of an open-loop test:
// somebody has to hold every in-flight request. k6 is written in Go for
// exactly this reason -- see Topic 2's coordinated_omission programs, which
// print the thread count each runtime needs to hold ~100 requests in flight.
//
// If k6 warns that it could not start enough VUs to hit the target rate, the
// run is invalid: you have just built a closed-loop test with extra steps.
// Raise maxVUs and run it again.
import http from 'k6/http';
import { check } from 'k6';

const API = __ENV.API_URL || 'http://api:8000';
const RATE = Number(__ENV.RATE || 300);
const CUSTOMERS = 500;

export const options = {
  scenarios: {
    arrival: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 300,
      maxVUs: 2000,          // the ceiling on requests this test can hold
    },
  },
  thresholds: {
    http_req_duration: ['p(50)<1000', 'p(99)<10000'],
    dropped_iterations: ['count<1'],   // any drop means the rate was not met
  },
};

export default function () {
  const customer = `cust-${String(1 + (__ITER % CUSTOMERS)).padStart(5, '0')}`;
  const res = http.get(`${API}/orders?customer_id=${customer}`, {
    tags: { endpoint: 'list_orders' },
  });
  check(res, { 'status 200': (r) => r.status === 200 });
}
