// Topic 2 - pool exhaustion. Ramp the ARRIVAL RATE (not the VU count) from 50
// to 600 rps over five minutes and watch for the knee.
//
// The knee is where p99 goes vertical while every dependency still reports
// itself healthy. Little's Law predicts where it should be:
//     ceiling = (pool_size + max_overflow) / W
// Compute that first from the POOL_PROFILE you are running and the injected
// database latency, then see whether the knee lands there.
//
// Pair with:
//   docker compose exec db psql -U app -c "select state, count(*) from pg_stat_activity group by 1;"
//   curl -s localhost:8000/stats   # pool_waits, pool_wait_seconds, inflight_max, shed
import { get } from './_shared.js';

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: 4000,
      stages: [
        { target: 150, duration: '60s' },
        { target: 300, duration: '60s' },
        { target: 450, duration: '60s' },
        { target: 600, duration: '60s' },
        { target: 600, duration: '60s' },
      ],
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  get('/checkout');
}
