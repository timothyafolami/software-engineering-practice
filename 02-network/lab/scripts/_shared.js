// Shared k6 helpers for Layer 2.
//
// One rule, restated here because breaking it silently invalidates every table
// in the layer: load is OPEN MODEL. `constant-arrival-rate` issues requests on
// a clock, whether or not the service is keeping up. A fixed VU count is
// closed-loop -- it slows down when your service does, which is feedback real
// callers do not provide, and it therefore cannot reproduce queueing, pool
// exhaustion or metastability at all.
//
// Watch `dropped_iterations` in the summary. If it is non-zero, k6 could not
// start iterations at the requested rate and your histogram is missing exactly
// the requests that would have been slowest: coordinated omission, in your own
// results, from your own generator.
import http from 'k6/http';
import { check } from 'k6';

export function arrivalRate(rate, duration, extra = {}) {
  const r = Number(rate);
  return {
    executor: 'constant-arrival-rate',
    rate: r,
    timeUnit: '1s',
    duration: duration,
    // Enough pre-allocated VUs that k6 itself is never the bottleneck. If
    // dropped_iterations is non-zero, raise these, not the rate.
    preAllocatedVUs: Math.max(50, r),
    maxVUs: Math.max(200, r * 8),
    ...extra,
  };
}

export function target() {
  return __ENV.TARGET || 'http://api:8000';
}

export function get(path, params) {
  const res = http.get(`${target()}${path}`, params);
  check(res, { 'status is 2xx': (r) => r.status >= 200 && r.status < 300 });
  return res;
}
