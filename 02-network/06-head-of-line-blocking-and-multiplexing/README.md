# Layer 2 · Topic 6 — Head-of-line blocking, multiplexing, and what loss does

### The takeaway (read this first)

**The one idea:** head-of-line blocking exists independently at three layers —
HTTP/1.1's one-request-per-connection, TCP's in-order byte stream underneath
HTTP/2, and QUIC's per-stream independence under HTTP/3 — and each protocol
version fixed the layer above it while leaving the layer below completely
intact.

**Why it matters in practice:** it explains why HTTP/2 is a large win on a
clean link and a measurable *loss* on a lossy one, why multiplexing quietly
replaces your connection pool with a stream limit you did not know you had,
and why HTTP/3 in 2026 is a browser-and-CDN concern rather than something to
put between your containers.

**You'll know it landed when:** you can predict which of HTTP/1.1-with-a-pool
and HTTP/2-on-one-connection wins at 0%, 1% and 5% packet loss, and explain
the crossover by naming which layer is doing the blocking.

## The concept

**HTTP/1.1** blocks at the *request* layer: one request in flight per
connection, responses returned in order. The workaround is the connection
pool — which is why Topics 1 and 2 exist. Pipelining was specified, is
implemented almost nowhere, and is dead.

**HTTP/2** (RFC 9113) multiplexes many streams over one TCP connection,
removing request-layer blocking. But TCP delivers bytes strictly in order, so
a single lost segment stalls the receiver's delivery of *every* multiplexed
stream until that segment is retransmitted — one round trip, minimum, during
which a hundred independent responses are all waiting on one packet that has
nothing to do with ninety-nine of them. On a clean datacenter link this never
matters. On a lossy mobile path, HTTP/2 can be worse than several HTTP/1.1
connections, for the simple reason that independent connections lose
independently.

**HTTP/3 over QUIC** (RFC 9000, RFC 9114) moves the transport into userspace
over UDP and makes streams genuinely independent, so loss affects only the
stream whose packet was lost. It also merges the transport and cryptographic
handshakes, and carries a connection ID rather than identifying a connection
by its four-tuple — so a connection survives the client changing IP address,
which is why HTTP/3 is a mobile story first and a throughput story second.

**Congestion control sits underneath all three.** Slow start opens the window
from RFC 6928's initial ten segments, doubling each round trip until a
congestion signal arrives. CUBIC — still the Linux default — treats loss as
that signal. BBR models the bottleneck bandwidth and round-trip propagation
time instead, which behaves better on shallow-buffered or lossy paths and
adds much less queueing delay on deeply buffered ones ("bufferbloat"); the
current specification work is the IETF
[BBR draft](https://datatracker.ietf.org/doc/draft-ietf-ccwg-bbr/). The
practical consequence, and the one you will measure below: on a path with real
loss, a loss-based controller repeatedly halves its window, so *throughput*
collapses long before the link is actually full.

This topic is where the layer's standard reference goes out of date, and it is
worth knowing exactly where. [High Performance Browser Networking](https://hpbn.co/)
(Grigorik, free) is still the best free explanation of TCP, TLS and HTTP/2
anywhere — and it **predates QUIC entirely, its TLS chapter is TLS 1.2-era,
and its congestion-control material predates BBR**, which is precisely the
three subjects of this page. For the parts it cannot cover, read Daniel
Stenberg's [HTTP/3 explained](https://http3-explained.haxx.se/) and Robin
Marx's HTTP/3 series on Smashing Magazine, which is the best available
treatment of *when HTTP/3 does not help*; then go to the primary sources
above.

**The 2026 state of HTTP/3, stated plainly, because this advice has changed.**
It is no longer new: nginx, Caddy and every major CDN support it, and a large
share of CDN traffic negotiates it. Do not quote a percentage from this page —
if you need the number, take it from Cloudflare Radar or your own CDN's
analytics on the day you need it. What matters here is where the win
concentrates: **lossy, high-RTT, client-facing** paths. For a FastAPI service
calling another service over a 0.3 ms datacenter link with negligible loss,
HTTP/3 buys approximately nothing and costs CPU, because the crypto is in
userspace and there is no kernel TLS offload. Enable it at the edge; do not
spend a quarter putting QUIC between your containers. gRPC over HTTP/2 remains
the normal internal choice.

**The new pool limit nobody configures.** Under HTTP/2,
`SETTINGS_MAX_CONCURRENT_STREAMS` — announced by the server, commonly in the
low hundreds — replaces connection count as your concurrency ceiling.
Switching a client to HTTP/2 to "remove the pool limit" just renames the
limit, and the new one is invisible to `ss`, invisible to your connection-count
dashboard, and set by somebody else's config file.

## How each language actually gets there

**The mechanism here lives in the protocol, not the runtime, so this topic
uses three languages rather than six: Python, Go and Node cover the three
distinct client behaviours worth seeing, and a Rust, C++ or Java HTTP/2 client
would restate the same RFC 9113 stream accounting with a different type
signature.**

**Python.** `httpx.AsyncClient(http2=True)` requires the `h2` package and then
negotiates HTTP/2 via ALPN. The consequence people miss is that
`max_connections` stops meaning what it meant: the pool now holds roughly one
connection per origin, and your real concurrency limit is the server's
advertised stream maximum. Every dashboard you built in Topic 2 is now
measuring the wrong number.

**Go.** `net/http` enables HTTP/2 automatically over TLS. Its HTTP/2 transport
keeps a single connection per host and, when the peer's stream limit is
reached, **queues** new requests on that connection rather than dialling a
second one — behaviour you can change with `Transport.StrictMaxConcurrentStreams`
and, in current versions, by allowing multiple connections per host. This is
the cleanest demonstration of the topic's punchline: the queue moved from the
pool into the connection, and nothing in `ss` will show it to you.

**Node.js.** Two separate implementations again. The `http2` module is a
manually managed client session — you create it, you keep it, you close it —
and undici will negotiate HTTP/2 when told to (`allowH2`). Node's version of
the trap is that a single `ClientHttp2Session` is a single TCP connection with
a single congestion window, so one slow or lossy response affects every stream
on it, and the ergonomics encourage you to keep exactly one session per origin
for the lifetime of the process.

## The experiment

`lab/topic6/` — Topic 1's `/fanout` workload, run two ways:

- **h1**: HTTP/1.1 with a client pool of 10 connections.
- **h2**: HTTP/2 over a single connection (`httpx.AsyncClient(http2=True)`,
  with the upstream served by hypercorn, or nginx with `http2 on`).

Toxiproxy applies 0%, 1% and 5% packet loss with 40 ms of latency in all
cases. Record p50 and p99, and from `ss -ti` inside `api` record `retrans`,
`cwnd`, `rtt` and the congestion algorithm in use.

Responses need to be large enough and concurrency high enough that something
is actually multiplexing — aim for responses on the order of 100 KB and a
fan-out of about 20, otherwise both protocols will look identical and you will
have measured nothing.

**Second run: change the congestion controller.** Set
`net.ipv4.tcp_congestion_control=bbr` and repeat the 5% case. This needs the
BBR module present in the kernel — which, on this machine, is Docker Desktop's
linuxkit VM, not macOS — and needs the container to be allowed to set a
namespaced sysctl (a compose `sysctls:` entry, or a privileged container).
Check `sysctl net.ipv4.tcp_available_congestion_control` first and **skip this
half gracefully** if `bbr` is absent. "Not available on this host" is a valid
recorded result; a number obtained after the sysctl silently failed is not.

## How to run

```
cd 02-network/lab
# PROTO is read at startup by `api` (which httpx client it builds) and by
# `upstream` (uvicorn/HTTP-1.1 versus hypercorn/h2c). Putting it in front of
# `docker compose run --rm load` sets it on the *k6* container, where nothing
# reads it, and both runs then speak HTTP/1.1 -- two identical rows and a
# wrong conclusion. It has to go in front of the `up`, which recreates both
# containers.
FANOUT=20 UPSTREAM_BODY_BYTES=102400 PROTO=h1 docker compose up -d api upstream
docker compose exec api sh -c 'python -c "import httpx,os;print(os.environ[\"PROTO\"])"'
docker compose run --rm load run /scripts/topic6.js

FANOUT=20 UPSTREAM_BODY_BYTES=102400 PROTO=h2 docker compose up -d api upstream
docker compose logs --tail 2 upstream        # must say hypercorn, h2c
docker compose run --rm load run /scripts/topic6.js
docker compose exec api ss -ti dst upstream
```

Add the loss between runs, and confirm it took effect before trusting the
comparison:

```
curl -s localhost:8474/proxies/upstream/toxics
docker compose exec api sysctl net.ipv4.tcp_available_congestion_control
```

Toxiproxy has no packet-loss toxic — it is a TCP-level proxy, not a link
emulator — so the loss half uses `tc netem` in the `sniff` sidecar, which
shares `api`'s network namespace and carries `NET_ADMIN` for exactly this:

```
docker compose exec sniff sh -c "tc qdisc add dev eth0 root netem loss 5% delay 40ms"
docker compose exec sniff sh -c "tc qdisc show dev eth0"     # confirm before believing anything
docker compose exec sniff sh -c "tc qdisc del dev eth0 root"
```

The single-file programs, from this topic's directory. Each starts both servers
in its own process and runs the identical fan-out over HTTP/1.1 and HTTP/2, so
the only variable is the protocol:

```
pip install -r python/requirements.txt && python3 python/h1_pool_vs_h2_streams.py
cd golang && go run h2_queues_where_h1_pools.go
node nodejs/http2_session_is_one_connection.js
```

The Python one needs `httpx[http2]`; without the `h2` package httpx silently
stays on HTTP/1.1, which is the most common way to run this comparison and
measure nothing — it refuses to start rather than let that happen. Go's needs
no dependency: it generates a self-signed certificate in-process, because
HTTP/2 in Go requires ALPN over TLS and a cleartext server would quietly hand
you HTTP/1.1.

Those three isolate the **concurrency-ceiling** half of this topic, and they
do not attempt the head-of-line-blocking half at all — loopback has no packet
loss, so that half only exists in the lab, above. What they do show is that
the three clients disagree completely about what to do when the peer's stream
limit is reached: httpx raises `LocalProtocolError` locally before a frame is
sent, Node opens the streams and the server refuses them (`REFUSED_STREAM`,
surfaced as `ERR_HTTP2_STREAM_ERROR`), and current Go dials an additional
connection rather than queueing on the existing one — which is not what the
folklore, or the paragraph about Go above, says it does. Record the toolchain
version next to any number, because that behaviour has changed across
releases.

## Predict, then record

- Which wins at 0% loss, h1-with-a-pool or h2-on-one-connection? By how much? ______
- Which wins at 5% loss? ______  Where is the crossover? ______
- Does switching to `bbr` change the 5% result more or less than switching
  protocol version does? ______
- Under h2, what is the server's advertised `SETTINGS_MAX_CONCURRENT_STREAMS`,
  and at what fan-out would you hit it? ______

| Proto | loss | p50 | p99 | retrans | cwnd |
|---|---|---|---|---|---|
| h1 pool=10 | 0% | | | | |
| h2 single | 0% | | | | |
| h1 pool=10 | 1% | | | | |
| h2 single | 1% | | | | |
| h1 pool=10 | 5% | | | | |
| h2 single | 5% | | | | |

| Client | conns to upstream | concurrency ceiling | who sets the ceiling |
|---|---|---|---|
| Python httpx h1 | | | |
| Python httpx h2 | | | |
| Go net/http h2 | | | |
| Node undici h2 | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- h2 shows *no* degradation at 5% loss → you are not on one connection.
  `ss -tan` inside `api` should show exactly one established connection per
  upstream during the h2 run. Two or more means the client fell back to
  HTTP/1.1 — very common, since h2 needs ALPN over TLS or explicit prior
  knowledge over cleartext.
- The loss toxic appears to do nothing → confirm it is attached to the right
  proxy and the right direction (`upstream` versus `downstream`).
- Both protocols identical everywhere → responses too small and concurrency
  too low, so nothing multiplexes and nothing queues. Push response size and
  fan-out up as described above and re-run.
- `ss -ti` still reports `cubic` after you set `bbr` → the sysctl did not apply
  in the container's network namespace, or the module is not loaded. Record
  "not available on this host" rather than a wrong number.
- p99 improves under loss → your load generator is dropping requests it cannot
  send. Check that the k6 scenario reports dropped iterations, and read the
  coordinated-omission note in [`../lab/README.md`](../lab/README.md).

## Answer before moving on

1. Multiplexing 100 streams over one TCP connection removes request-level
   head-of-line blocking. Say precisely what one lost segment does to those
   100 streams, and why 10 HTTP/1.1 connections behave differently.
2. Your CDN reports that 35% of client requests are HTTP/3 and p99 barely
   moved. Give two honest explanations that do not involve HTTP/3 being
   useless.
3. You "removed the pool limit" by switching to HTTP/2. What is the new
   ceiling, who sets it, and how would you observe that you are hitting it?
4. BBR and CUBIC disagree about what loss means. Describe a path where that
   disagreement changes your throughput by more than switching from HTTP/1.1
   to HTTP/2 does, and say which measurement would tell them apart.

## Next up

[Topic 7 — See it on the wire](../07-see-it-on-the-wire/README.md): every
claim in Topics 1 through 6 is directly observable, and it takes about twenty
minutes to stop taking any of them on faith.
