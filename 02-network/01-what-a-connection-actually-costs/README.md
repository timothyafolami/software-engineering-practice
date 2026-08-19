# Layer 2 · Topic 1 — What a connection actually costs

### The takeaway (read this first)

**The one idea:** before one byte of your request is written, a new HTTPS
connection has already spent a DNS round trip, a TCP round trip and a TLS
round trip, and then it begins transmitting with a congestion window of about
ten packets — so a cold connection is slow twice over, once in latency and
once in throughput, and neither cost appears anywhere in your code.

**Why it matters in practice:** this is the entire reason connection pooling
exists. Derive the size of the effect for your own network rather than
trusting a multiplier: if your warm request costs one round trip and your cold
request costs a DNS round trip plus a TCP round trip plus a TLS round trip
before the request round trip, then at round-trip time *R* you are comparing
roughly *R* against roughly *4R* before any server work at all. On a 30 ms
path that is a difference you will see in p50; on a 0.3 ms path it hides until
you count syscalls. If you have a live latency problem and your handlers
construct `httpx.Client()` or `requests.Session()` inside the function body,
stop reading and go check that first.

**You'll know it landed when:** you can decompose a `curl` timing breakdown
into named protocol events, say which of them a warm pool skips, and predict
how much p99 disappears if you fix it — before fixing it.

## The concept

Every new HTTPS connection climbs the same staircase, and each step costs at
least one round trip:

1. **Name resolution.** `getaddrinfo()` → possibly several UDP queries.
   Topic 5 explains why "several" and not "one".
2. **TCP handshake.** SYN → SYN/ACK → ACK. A full round trip before you are
   allowed to send application data. TCP Fast Open exists, is specified in
   RFC 7413, and is largely undeployable through middleboxes; ignore it.
3. **TLS handshake.** TLS 1.3 (RFC 8446) is one round trip on a fresh
   connection and zero on resumption, down from TLS 1.2's two. Your client
   verifies a *chain*: leaf → intermediate → root, with the root in your trust
   store and the intermediates usually sent by the server. A server that omits
   the intermediate "works in Chrome" — which caches intermediates it has seen
   and does AIA fetching — and fails in Python, because `certifi` does
   neither. That asymmetry is the most common "works on my machine" TLS bug
   there is.
4. **Slow start.** RFC 6928 raised TCP's initial congestion window to ten
   segments, so a fresh connection may put roughly 10 × MSS on the wire before
   it must wait for an ACK. At a 1460-byte MSS that is about 14 KB. Work it
   out for a 200 KB response: 14 KB, then ~28 KB, then ~56 KB as the window
   doubles — four or five round trips of *transfer*, on top of the three you
   already spent connecting. A warm connection has already paid for that
   window growth and keeps it.

Steps 1–3 are what a pool removes. Step 4 is what a pool *preserves*, and it
is the half people forget: reuse is not only about skipping handshakes, it is
about inheriting a connection that has already learned how fast the path is.

**The 2026 wrinkle.** Hybrid post-quantum key exchange (`X25519MLKEM768`) is
now negotiated by default between current browsers and most large CDNs, and is
on by default in recent OpenSSL and BoringSSL builds. ML-KEM-768's
encapsulation key is 1184 bytes (FIPS 203, table of parameter sizes), and once
that is inside a ClientHello alongside everything else, the ClientHello no
longer fits in a single segment on a 1500-byte-MTU path — for the first time
in TLS's history. The CPU cost of the key exchange is small; the *packet* cost
is the interesting one, and it has broken real middleboxes that assumed a
ClientHello arrives in one packet. Topic 7 is where you count those segments
yourself rather than believing this paragraph.

## How each language actually gets there

The runtime is the subject here — a connection pool is a data structure owned
by a client library, and six libraries made six different decisions about
lifetime, so all six languages earn their place.

**Python.** `httpx.Client` (which wraps `httpcore`'s pool), `requests.Session`
(which wraps `urllib3`'s `PoolManager`, itself a cache of per-host
`HTTPConnectionPool` objects) and `aiohttp.ClientSession` (which owns a
`TCPConnector`) all pool — *if you keep the object alive*. The dominant real
bug is lifetime, not configuration: a client constructed inside a request
handler is a pool of one, used once, then garbage collected along with its
socket. The fix is a module-level client, or one built in FastAPI's `lifespan`
and stored on `app.state`, closed on shutdown. The sharp edge specific to
async Python: an `httpx.AsyncClient` or `aiohttp.ClientSession` created before
the event loop it will run on is a genuine "passes in tests, breaks in
production", because the connector binds to a loop.

**Node.js.** Two stacks in one runtime, and they pool differently. `http.request`
goes through `http.globalAgent`, which has had `keepAlive: true` by default
since Node 19. `fetch()` goes through undici, which keeps a per-origin `Pool`
of clients under a global dispatcher. Because pooling is global and on by
default, Node is the runtime least likely to have the cold-client bug — and
therefore the one where the trap moved somewhere else entirely: undici's
`headersTimeout` and `bodyTimeout` are measured in minutes, not seconds
(Topic 3). Check the values your Node version actually ships rather than
trusting this line: `node -p "require('undici').Agent"` and the undici docs
for your release.

**Go.** `http.DefaultTransport` pools sensibly with one exception that costs
people real money: `MaxIdleConnsPerHost` falls back to
`DefaultMaxIdleConnsPerHost`, which is **2**. Fan fifty concurrent requests
out to a single host and Go opens fifty connections, keeps two idle
afterwards, and closes forty-eight — which then sit in `TIME_WAIT` on the
client. The second Go-specific footgun is not a setting at all: a response
body that is not read to EOF *and* closed is never returned to the pool, so a
handler that checks `resp.StatusCode` and returns early silently converts a
pooled client into a cold one.

**Rust.** `reqwest::Client` is a handle around a hyper client and its
connection pool; cloning it is cheap and shares the pool, which is the idiom.
`Client::new()` per request is the same bug as Python's, with an extra cost
Python does not have: each construction builds a fresh TLS configuration and
loads the root certificate store. Nothing in the type system stops you —
this is the one place in the lab where Rust's compile-time enforcement offers
no protection at all, because "wrong lifetime for a cache" is not a memory
safety property. What Rust does give you is that the pool's ownership is
explicit: if the `Client` is dropped, you can see it being dropped.

**C++.** No client library, no pool, nothing between you and the kernel. You
call `getaddrinfo`, `socket`, `connect`, then drive OpenSSL's `SSL_connect`
yourself, and the cost of a connection stops being an abstraction and becomes
a list of syscalls you can count with `strace` (Linux, in the container) or
`dtruss` **[host]**. With libcurl, the easy handle *is* the pool: reuse one
`CURL*` across requests and curl reuses the connection; allocate a new handle
per request and you get a fresh TCP and TLS handshake every time — the exact
`httpx.Client()`-in-the-handler bug, in C, with the mechanism visible. TLS
session resumption is a separate cache you must wire up on purpose
(`SSL_SESSION` reuse or a `CURLSH` share handle); nothing does it for you.

**Java.** `java.net.http.HttpClient` (Java 11+) pools internally per client
instance, and the pool is deliberately not exposed through the public API —
you tune it with system properties (`jdk.httpclient.keepalive.timeout`,
`jdk.httpclient.connectionPoolSize`) rather than with constructor arguments.
That makes Java the best illustration of a specific hazard: a pool you cannot
see is a pool you will accidentally destroy, because building a new
`HttpClient` per request looks harmless and is not — each instance also owns a
selector thread and an executor. The legacy path (`HttpURLConnection`) is
worth knowing exists, because it is what older services in your estate use and
it is controlled by yet another set of properties (`http.keepAlive`,
`http.maxConnections`).

## The experiment

**The service-level run.** `lab/topic1/` — `api` exposes `/fanout`, which
calls `upstream` five times per request, in three variants selected by
environment variable:

- `COLD` — a new `httpx.Client()` constructed inside the handler, per call.
- `WARM` — a module-level client created in FastAPI's `lifespan`.
- `WARM_TUNED` — the same, plus explicit `httpx.Limits` and HTTP/2 enabled.

Toxiproxy adds a fixed 30 ms of latency in front of `upstream`, so the round
trip is a known constant instead of container-local near-zero. Drive at
200 rps for 60 s and compare p50, p95 and p99.

**The phase breakdown**, from inside the container, so the resolver and the
trust store are the container's:

```
curl -sk -o /dev/null -w 'dns=%{time_namelookup} tcp=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}\n' https://example.com/
```

Run it twice against the same host and note which numbers do *not* drop the
second time. They will not, because `curl` is a fresh process each time and
shares nothing with the previous one. That is exactly what `COLD` does on
every single request.

**The six-language run.** Each language directory holds one program that makes
N requests two ways — a fresh client per request, and one reused client — and
reports wall time plus, where the runtime will tell you, the number of
connections opened. The point is not to rank the languages; it is that the
same one-line mistake has a different shape in each:

| Directory | What it shows |
|---|---|
| [`python/`](python/) | `cold_vs_warm_client.py`, plus `handshake_phases.py` for the per-phase timing |
| [`nodejs/`](nodejs/) | `cold_vs_warm_client.js` — and how hard undici makes it to *be* cold |
| [`golang/`](golang/) | `max_idle_conns_per_host.go` — the default of 2, and the `TIME_WAIT` pile |
| [`rust/connection_reuse/`](rust/connection_reuse/) | clone-the-client vs rebuild-the-client, including TLS config cost |
| [`cpp/`](cpp/) | `connection_syscall_cost.cpp` — the connection as a syscall sequence |
| [`java/`](java/) | `ConnectionReuse.java` — a pool you cannot configure, only lose |

## How to run

```
cd 02-network/lab
docker compose up -d db upstream toxi api
# The 30 ms above is not decoration. Without it the round trip to `upstream` is
# container-local -- tens of microseconds -- and a handshake you skipped is
# lost in the noise, so COLD and WARM come out equal and the topic looks
# disproved. Inject it before the first run, and re-check it after every
# `docker compose up`, because a recreated toxi starts with no toxics.
curl -s -X POST localhost:8474/proxies/upstream/toxics \
  -d '{"type":"latency","attributes":{"latency":30,"jitter":0}}'
curl -s localhost:8474/proxies/upstream/toxics        # confirm before believing anything
docker compose run --rm load run /scripts/topic1.js
docker compose exec api sh -c "ss -tan state established | wc -l"
```

Read the established count **while k6 is still running**. The pool drains
within seconds of the last request, so the same command run after the load
finishes reports 1 on every variant and tells you nothing.

Repeat with `VARIANT=COLD`, `VARIANT=WARM`, `VARIANT=WARM_TUNED` in front of
the `docker compose up -d api` line. See [`../lab/README.md`](../lab/README.md)
for the service list and the health check to run before trusting any of this.

The single-file programs, from this topic's directory:

```
python3 python/cold_vs_warm_client.py && python3 python/handshake_phases.py
node nodejs/cold_vs_warm_client.js
cd golang && go run max_idle_conns_per_host.go
cd rust/connection_reuse && cargo run --release
c++ -O2 -std=c++17 -o /tmp/conncost cpp/connection_syscall_cost.cpp && /tmp/conncost
cd java && javac ConnectionReuse.java -d /tmp/javabuild && java -cp /tmp/javabuild ConnectionReuse
```

## Predict, then record

Write these down before running anything, in
[`PREDICTIONS.md`](../../PREDICTIONS.md):

- `COLD` vs `WARM` p99, as a ratio: ______
- Established connections from `api` under `WARM` at 200 rps: ______
- Does HTTP/2 (`WARM_TUNED`) change p99 measurably? ______
- Which language will show the *smallest* cold-vs-warm gap, and why? ______

| Variant | p50 | p95 | p99 | estab conns | new conns/s |
|---|---|---|---|---|---|
| COLD | | | | | |
| WARM | | | | | |
| WARM_TUNED | | | | | |

| Language | cold (per-request client) | warm (reused client) | conns opened, cold | conns opened, warm |
|---|---|---|---|---|
| Python (httpx) | | | | |
| Node (undici) | | | | |
| Go (net/http) | | | | |
| Rust (reqwest) | | | | |
| C++ (libcurl / raw) | | | | |
| Java (HttpClient) | | | | |

Output shape, so you know what you are copying in:

```
cold   <your number> ms total, <your number> connections
warm   <your number> ms total, <your number> connections
```

**What would mean the experiment is broken, not the prediction wrong:**

- `COLD` and `WARM` land within 10% of each other → the injected latency is
  not applied. Check `curl -s localhost:8474/proxies` and confirm `api` points
  at the proxy rather than at `upstream` directly.
- Established count is 0 or 1 under `WARM` at 200 rps → you read it from the
  wrong namespace. `ss` must run inside `api`.
- p99 swings run to run → the test is too short. Under 30 s you are measuring
  container start and Python imports, not steady state.
- `COLD` comes out *faster* → check that the cold variant is not accidentally
  sharing a module-level client, and that the warm variant is not serialising
  requests through a pool of one.
- A language shows zero difference between cold and warm → for Node, that may
  be correct and is the finding. For any other, suspect that the "cold" path
  is being pooled underneath you — by a proxy, or by libcurl's share handle.

## Answer before moving on

1. TLS 1.3 removed a round trip from the handshake. Why did that not make the
   first request to a new host roughly a third faster in practice? Name the
   costs that did not go away, and say which of them a pool removes anyway.
2. Your upstream is in the same datacenter, round-trip time around 0.4 ms, and
   a colleague argues pooling is pointless at that latency. Give the strongest
   argument they are wrong that has nothing to do with latency.
3. A hybrid post-quantum ClientHello no longer fits in one packet. Describe a
   concrete failure that would appear in your application logs as a random,
   intermittent connection timeout — and say which layer you would have to
   capture at to tell it apart from a slow server.
4. Go closes forty-eight of fifty connections after a fan-out because
   `MaxIdleConnsPerHost` is 2. Those sockets enter `TIME_WAIT` on the *client*.
   Why is that worse than the server accumulating them, and what breaks first?

## Next up

[Topic 2 — Connection pooling and pool exhaustion](../02-connection-pooling-and-pool-exhaustion/README.md):
now that you know what a connection costs, the pool that saves you that cost
becomes a queue with a hard capacity, and queues have a knee.
