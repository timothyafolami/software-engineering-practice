// k6, ramping arrival rate -- for finding where the knee is, not where p99 is.
//
// WHAT THIS DEMONSTRATES
//   steady.js answers "how bad is it at rate R". This answers "at what R
//   does it fall over", which is the question you actually need before
//   choosing a quota. The ramp is on the ARRIVAL rate, so the offered load
//   keeps climbing even after the service stops keeping up -- the point at
//   which dropped_iterations starts climbing is the real capacity, and it
//   is usually well below the rate at which average CPU looks alarming.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   The stage at which p99 leaves p50 behind. Under a CFS quota that
//   departure is abrupt rather than gradual: below the knee almost nothing
//   is throttled, above it almost every period is.
//
// RUN
//   docker compose --profile load run --rm --no-deps k6 run /scripts/spike.js

import http from 'k6/http';

const TARGET = __ENV.TARGET || 'http://api:8000';
const ENDPOINT = __ENV.ENDPOINT || '/mixed';
const PEAK = Number(__ENV.PEAK || 160);

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 1000,
      stages: [
        { target: Math.round(PEAK * 0.125), duration: '20s' },
        { target: Math.round(PEAK * 0.25), duration: '20s' },
        { target: Math.round(PEAK * 0.5), duration: '20s' },
        { target: PEAK, duration: '20s' },
        { target: PEAK, duration: '20s' },
      ],
    },
  },
  thresholds: {
    http_req_duration: ['p(99)<300'],
  },
};

export default function () {
  http.get(`${TARGET}${ENDPOINT}`);
}
