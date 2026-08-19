// Topic 6 - head-of-line blocking and multiplexing.
//
// Run once with PROTO=h1 and once with PROTO=h2 on the `api` and `upstream`
// containers, at 0%, 1% and 5% loss injected by Toxiproxy:
//
//   curl -X POST localhost:8474/proxies/upstream/toxics \
//     -d '{"type":"latency","attributes":{"latency":40,"jitter":0}}'
//
// Toxiproxy has no packet-loss toxic (it is a TCP-level proxy, not a link
// emulator); use `tc netem loss 5%` inside the `sniff` sidecar, which shares
// api's network namespace and has NET_ADMIN for exactly this:
//   docker compose exec sniff sh -c "tc qdisc add dev eth0 root netem loss 5% delay 40ms"
//   docker compose exec sniff sh -c "tc qdisc del dev eth0 root"
// Confirm it took effect before trusting the comparison. A silently failed
// qdisc gives you two identical rows and a wrong conclusion.
//
// Fan-out and body size must be pushed up for this topic, on the containers
// rather than here: with small responses and low concurrency nothing
// multiplexes and nothing queues, and both protocols look identical because
// you measured nothing.
//
//   FANOUT=20 UPSTREAM_BODY_BYTES=102400 PROTO=h2 docker compose up -d api upstream
import { arrivalRate, get } from './_shared.js';

export const options = {
  scenarios: { fanout: arrivalRate(__ENV.RATE || 50, __ENV.DURATION || '120s') },
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  get('/fanout');
}
