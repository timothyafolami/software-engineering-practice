// Topic 3 - timeouts. Constant 150 rps for three minutes; you inject the
// toxic at t=60s and remove it at t=120s from another shell:
//
//   curl -X POST localhost:8474/proxies/upstream/toxics \
//     -d '{"type":"latency","attributes":{"latency":20000,"jitter":2000}}'
//   curl -X DELETE localhost:8474/proxies/upstream/toxics/latency
//
// The measurement that matters is NOT p99 during the fault. It is the time
// from fault REMOVAL to full recovery. A system that does not recover promptly
// once the trigger is gone is metastable, and you are watching it happen.
// That is why this runs a full minute past the fault window.
import { arrivalRate, get } from './_shared.js';

export const options = {
  scenarios: { order: arrivalRate(__ENV.RATE || 150, __ENV.DURATION || '180s') },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  // The caller states its own deadline. TIMEOUT_PROFILE=budget|full honours
  // it; none|flat ignore it, which is the comparison.
  get('/order', { headers: { 'x-request-deadline': '3.0' }, timeout: '60s' });
}
