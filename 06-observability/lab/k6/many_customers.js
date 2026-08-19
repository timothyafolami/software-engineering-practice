// Layer 6 lab - many_customers.js: 10,000 distinct customer IDs. Topic 4.
//
// One label, ten thousand values. Run this with CARDINALITY_DEMO=customer_id
// on `api` and the request counter's series count becomes
//
//     routes x methods x statuses x 10,000
//
// which you should compute on a napkin BEFORE running it, because predicting
// the number is the exercise and watching the graph is only the confirmation.
//
// Run 1 leaves the SDK's default per-stream limit (2000 attribute sets) in
// place and produces the SILENT failure: totals stay correct, every breakdown
// undercounts, nothing logs. Run 2 sets OTEL_METRIC_CARDINALITY_LIMIT=0 and
// produces the loud one -- watch prometheus_tsdb_head_series while it happens,
// not afterwards, because after an OOM restart the count is zero again and
// tells you nothing.
import http from 'k6/http';
import { check } from 'k6';

const API = __ENV.API_URL || 'http://api:8000';
const CUSTOMERS = Number(__ENV.CUSTOMERS || 10000);

export const options = {
  scenarios: {
    many_customers: {
      executor: 'constant-arrival-rate',
      rate: 200,
      timeUnit: '1s',
      duration: '10m',
      preAllocatedVUs: 200,
      maxVUs: 1000,
    },
  },
};

export default function () {
  // Every iteration a different customer, so distinct values accumulate fast.
  // Count them yourself before doubting the SDK: if the generator sent fewer
  // distinct values than the limit, there is nothing to overflow.
  const n = 1 + Math.floor(Math.random() * CUSTOMERS);
  const customer = `cust-${String(n).padStart(5, '0')}`;
  const res = http.get(`${API}/orders?customer_id=${customer}`, {
    tags: { endpoint: 'list_orders' },
  });
  check(res, { 'status 200': (r) => r.status === 200 });
}
