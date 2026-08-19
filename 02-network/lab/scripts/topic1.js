// Topic 1 - what a connection actually costs.
//
// Drive /fanout at a constant arrival rate and compare p50/p95/p99 across the
// three variants. Nothing in this script knows which variant is running; that
// is decided by VARIANT on the `api` container, which is the point -- the only
// thing changing between runs is where the client object was constructed.
//
//   VARIANT=COLD docker compose up -d api && docker compose run --rm load run /scripts/topic1.js
//
// Read alongside: docker compose exec api sh -c "ss -tan state established | wc -l"
import { arrivalRate, get } from './_shared.js';

export const options = {
  scenarios: { fanout: arrivalRate(__ENV.RATE || 200, __ENV.DURATION || '60s') },
  // Absolute thresholds are deliberately absent: this topic records numbers,
  // it does not assert them. A threshold here would be a fabricated
  // expectation about a machine nobody has measured yet.
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  get('/fanout');
}
