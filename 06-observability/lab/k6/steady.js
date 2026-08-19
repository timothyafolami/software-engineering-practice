// Layer 6 lab - steady.js: closed-loop, 60 VU, 5 minutes. Topics 1 and 2.
//
// This is the load test almost everybody writes: N virtual users, each sending
// its next request only after the last one returns. It is also the load test
// that structurally cannot observe the failure it was written to catch, and
// Topic 2 is about why.
//
// The tell is in the summary, and it is the reason `iteration_duration` is
// given a threshold below: when the server stalls, a closed-loop generator
// STOPS ASKING. The stall then shows up as fewer requests rather than as slow
// ones, so `http_req_duration` stays flat while `iteration_duration` climbs.
// If you see that pair, the number you are about to report is not a latency.
//
// Run arrival.js against the same service and compare. That difference is the
// whole of Topic 2, Part 3, steps 1 and 2.
import http from 'k6/http';
import { check, sleep } from 'k6';

const API = __ENV.API_URL || 'http://api:8000';
const CUSTOMERS = 500;

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-vus',   // closed loop: this is the point
      vus: 60,
      duration: '5m',
    },
  },
  thresholds: {
    // Deliberately generous. These are here to make you read the two numbers
    // side by side, not to pass or fail a build.
    http_req_duration: ['p(99)<10000'],
    iteration_duration: ['p(99)<10000'],
  },
};

export default function () {
  const customer = `cust-${String(1 + (__VU * 7 + __ITER) % CUSTOMERS).padStart(5, '0')}`;
  const res = http.get(`${API}/orders?customer_id=${customer}`, {
    tags: { endpoint: 'list_orders' },
  });
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(0.1);   // think time: 60 VUs at ~10 req/s each, if nothing is slow
}
