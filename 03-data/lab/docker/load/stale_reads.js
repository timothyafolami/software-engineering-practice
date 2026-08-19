// Topic 8, experiment 2/3 -- read-your-own-writes across a replica, under load.
//
//   docker compose -f lab/docker/compose.yml --profile load run --rm \
//     -e READ_MODE=lsn k6 run /scripts/stale_reads.js
//
// READ_MODE = replica | primary | lsn. Each VU POSTs an order, then immediately
// GETs it back through the chosen routing strategy, and the `stale_reads`
// counter is the number of times the row it had just written was not there.
//
// Run all three modes and record two numbers each: stale reads, and the share
// of reads that still reached the replica (`served_by_replica`). A fix that
// eliminates stale reads by sending everything to the primary has undone the
// reason you added a replica -- and only the second counter shows that.
//
// RUN EVERY MODE TWICE, at THINK_MS=0 and at a THINK_MS comfortably above the
// standby's apply delay. At 0 the read chases the write down the wire and the
// replica has not replayed it yet, so `lsn` routes EVERY read to the primary
// and scores identically to `primary`: 0 stale, 0% on the replica. That is a
// real measurement and it is not the argument for LSN tokens -- it is the case
// where the token buys nothing. The argument appears at a think time longer
// than the lag, which is what a user who does something else before refreshing
// actually does: `lsn` then keeps its reads on the replica AND stays correct,
// which is the one combination `primary` and `replica` cannot offer.
//
//   -e THINK_MS=0     -e READ_MODE=replica|primary|lsn
//   -e THINK_MS=3000  -e READ_MODE=replica|primary|lsn     (APPLY_DELAY=2s)

import http from 'k6/http';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://api:8000';
const MODE = __ENV.READ_MODE || 'replica';
const RATE = Number(__ENV.ARRIVAL_RATE || 20);
// Gap between the write and the read. 0 is "refresh instantly"; anything above
// the standby's apply delay is "come back to the page later".
const THINK_MS = Number(__ENV.THINK_MS || 0);
const DURATION = __ENV.DURATION || '30s';

const stale = new Counter('stale_reads');
const total = new Counter('reads_total');
const onReplica = new Counter('served_by_replica');
const latency = new Trend('write_then_read_ms', true);

export const options = {
  scenarios: {
    rywr: {
      executor: 'constant-arrival-rate',
      rate: RATE, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 50, maxVUs: 300,
    },
  },
};

export default function () {
  const t0 = Date.now();
  const write = http.post(`${BASE}/orders`, null, { timeout: '30s' });
  if (write.status !== 200) return;
  const { id, lsn } = write.json();

  if (THINK_MS > 0) sleep(THINK_MS / 1000);

  const url = MODE === 'lsn'
    ? `${BASE}/orders/${id}?read=lsn&lsn=${encodeURIComponent(lsn)}`
    : `${BASE}/orders/${id}?read=${MODE}`;
  const read = http.get(url, { timeout: '30s' });
  latency.add(Date.now() - t0);
  if (read.status !== 200) return;

  const body = read.json();
  total.add(1);
  if (body.served_by === 'replica') onReplica.add(1);
  if (!body.found) stale.add(1);
}
