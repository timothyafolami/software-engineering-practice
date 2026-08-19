// Topic 5 - DNS, TTLs, and the pod that kept talking to a dead IP.
//
// Steady load with WARM pools, so that when you move the network alias the
// client is holding established sockets to the OLD address. That is the whole
// experiment: the cache that kills you is not the DNS cache, it is the
// connection pool.
//
// Run this in the background, then, while it is running:
//   docker compose up -d upstream_b
//   docker network disconnect lab_default upstream
//
// Record the error window: seconds from the disconnect to the first success
// against the new address, and whether it recovers without a restart.
import { arrivalRate, get } from './_shared.js';

export const options = {
  scenarios: { steady: arrivalRate(__ENV.RATE || 50, __ENV.DURATION || '300s') },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const res = get('/fanout');
  if (res.status !== 200) {
    console.log(`${new Date().toISOString()} status=${res.status} error=${res.error}`);
  }
}
