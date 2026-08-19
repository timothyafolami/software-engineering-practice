// Topic 4 - keep-alive across a load balancer.
//
// LOW AND BURSTY on purpose. This bug needs IDLE time: the connection must sit
// unused past the backend's idle timeout (5 s under `mismatched`) and before
// the LB's (60 s). Sustained load hides it completely, which is itself one of
// the lessons -- this defect gets MORE likely as your traffic gets quieter,
// and it is therefore close to unreproducible in a busy staging load test.
//
// Traffic goes to `lb` (port 8080), not to `api`. Going straight to api would
// remove the second idle timer and with it the entire topic.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { arrivalRate } from './_shared.js';

// This script does NOT use _shared.js's get(), because _shared.js targets
// `api` and this topic must go through `lb`. Driving api directly removes the
// second idle timer, and with it every 502 this topic exists to produce -- a
// clean run that means nothing.
const TARGET = __ENV.TARGET || 'http://lb:8080';

export const options = {
  scenarios: { bursty: arrivalRate(__ENV.RATE || 10, __ENV.DURATION || '10m') },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  console.log(`topic4 target = ${TARGET}  (must be the lb, not api:8000)`);
  if (!TARGET.includes('lb')) {
    console.warn('TARGET does not point at `lb`. Every 502 in this topic is ' +
                 'produced by nginx reusing a pooled connection the backend ' +
                 'already closed; without nginx in the path there is nothing ' +
                 'to observe.');
  }
}

export default function () {
  const res = http.get(`${TARGET}/fanout`);
  check(res, { 'status is 2xx': (r) => r.status >= 200 && r.status < 300 });
  if (res.status === 502) {
    // Printed rather than only counted: you want the timestamp so you can find
    // the same instant in /caps/topic4.pcap.
    console.log(`502 at ${new Date().toISOString()}`);
  }
  // The idle gap. Each VU goes quiet for longer than the backend's 5 s
  // keep-alive, which is what lets the backend's timer fire on a connection
  // nginx still believes it owns.
  sleep(6 + Math.random() * 4);
}
