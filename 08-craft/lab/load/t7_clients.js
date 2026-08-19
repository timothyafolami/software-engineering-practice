// Topic 7, ladder F: three clients, one fault, three collapse behaviours.
//
// WHAT THIS DEMONSTRATES: the same injected latency reaches the Python service
// in-process, a Go consumer and a Node consumer. Where each one queues, what it
// reports, and which of the three tells you it is in trouble BEFORE it fails are
// three different answers -- and the differences are partly runtime and partly
// somebody's configuration choice. Telling those two apart is question 4.
//
//   docker compose --profile load run --rm k6 run -e CLIENT=go     /load/t7_clients.js
//   docker compose --profile load run --rm k6 run -e CLIENT=node   /load/t7_clients.js
//   docker compose --profile load run --rm k6 run -e CLIENT=python /load/t7_clients.js
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const CLIENT = (__ENV.CLIENT || 'python').toLowerCase();
const TARGETS = {
  python: (__ENV.API || 'http://api:8000') + '/customers/1/orders?limit=50',
  go: (__ENV.CONSUMER_GO || 'http://consumer-go:8080') + '/orders?customer=1',
  node: (__ENV.CONSUMER_NODE || 'http://consumer-node:8081') + '/orders?customer=1',
};
const URL = TARGETS[CLIENT];
if (!URL) throw new Error(`CLIENT must be one of ${Object.keys(TARGETS).join('|')}`);

const RATE = Number(__ENV.RATE || 100);
const clientQueue = new Trend('client_reported_queue_ms');

export const options = {
  scenarios: {
    clients: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: __ENV.DURATION || '120s',
      preAllocatedVUs: Math.max(200, RATE * 2),
      maxVUs: Math.max(1000, RATE * 10),
    },
  },
  // p(99) is not one of k6's default trend stats (avg/min/med/max/p(90)/p(95)),
  // so without this line both p99 columns below print `undefined` on a run that
  // otherwise looks completely successful.
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(90)', 'p(99)', 'max'],
};

export default function () {
  const res = http.get(URL, { tags: { client: CLIENT }, timeout: '30s' });
  // Each consumer reports its own internal wait in a header, because the whole
  // question is where the queue is -- and from outside, a slow response looks
  // identical whether the wait happened in the client's connection pool, the
  // client's HTTP agent, the API's database pool, or Postgres itself.
  const q = res.headers['X-Client-Queue-Ms'];
  if (q !== undefined) clientQueue.add(Number(q));
  check(res, { 'not 5xx': (r) => r.status < 500 });
}

export function handleSummary(data) {
  const m = data.metrics;
  const g = (n, s) => (m[n] && m[n].values ? m[n].values[s] : undefined);
  return {
    stdout: [
      '',
      `CLIENT: ${CLIENT}  ->  ${URL}`,
      `  achieved rps                 ${g('http_reqs', 'rate')}`,
      `  p99 ms                       ${g('http_req_duration', 'p(99)')}`,
      `  error rate                   ${g('http_req_failed', 'rate')}`,
      `  client-reported queue p99 ms ${g('client_reported_queue_ms', 'p(99)')}`,
      '',
      '  If the client-reported queue is near zero while p99 is large, the wait',
      '  is downstream of this client -- which is the useful half of the answer.',
      '',
    ].join('\n'),
  };
}
