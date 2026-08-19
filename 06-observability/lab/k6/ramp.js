// Layer 6 lab - ramp.js: 10 -> 120 VU over 10 minutes. Topic 5.
//
// The ramp exists to make you record three VU counts and notice they are not
// the same number:
//
//   1. where p99 turns up
//   2. where pool utilization first hits 100%
//   3. where checkout waits begin
//
// The order they arrive in is the finding. Utilization pins early and then
// stops carrying information; saturation keeps climbing with no upper bound.
// Topic 5's standalone programs run this same ramp against a pool of 5 in four
// languages if you want the shape before you want the stack.
import http from 'k6/http';
import { check, sleep } from 'k6';

const API = __ENV.API_URL || 'http://api:8000';
const CUSTOMERS = 500;

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 10,
      stages: [
        { duration: '2m', target: 30 },
        { duration: '2m', target: 60 },
        { duration: '2m', target: 90 },
        { duration: '2m', target: 120 },
        { duration: '2m', target: 120 },
      ],
      gracefulRampDown: '30s',
    },
  },
};

export default function () {
  const customer = `cust-${String(1 + (__VU * 3 + __ITER) % CUSTOMERS).padStart(5, '0')}`;
  const res = http.get(`${API}/orders?customer_id=${customer}`, {
    tags: { endpoint: 'list_orders' },
  });
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(0.2);
}
