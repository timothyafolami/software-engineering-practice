# Layer 2 · Topic 5 — DNS, TTLs, and the pod that kept talking to a dead IP

### The takeaway (read this first)

**The one idea:** DNS is where your application's *cached decisions* outlive
reality — and the cache that usually kills you is not the DNS cache at all,
it is the **connection pool**, holding an open socket to an IP address that
DNS stopped advertising ten minutes ago.

**Why it matters in practice:** every failover, blue/green deploy, RDS
promotion and pod reschedule depends on clients noticing that an address
changed. A client with long-lived pooled connections and no maximum
connection lifetime will not notice, and the outage looks exactly like an
application bug: some instances fine, some failing, no code deployed, nothing
in the logs but connection errors to an address nobody can find in the
console.

**You'll know it landed when:** "half the pods are erroring after the
failover" makes you ask about connection lifetime and TTL before you open any
application code.

## The concept

The resolution path inside a container:

```
getaddrinfo()
  → /etc/nsswitch.conf        (which sources, in what order — glibc only)
  → /etc/resolv.conf          (nameservers, search domains, ndots, options)
  → cluster DNS (CoreDNS)     (or the Docker embedded resolver, in this lab)
  → upstream resolver
  → an answer, carrying a TTL that libc does not cache
```

That last clause is the one people get wrong. `getaddrinfo` does not cache.
Neither does Python, Go, or Node by default. The TTL on the record is
honoured by resolvers *between* you and the authority, not by your process.

Three landmines, all real, all in the stack you actually run:

**1. `ndots:5`.** Kubernetes writes `options ndots:5` into every pod's
`resolv.conf`, along with a search list. Any name with fewer than five dots —
that is, essentially every external hostname — has the search domains appended
*first*. Derive the packet count rather than memorising one:

```
search default.svc.cluster.local svc.cluster.local cluster.local
name:   api.stripe.com          (2 dots, fewer than 5)

attempt 1  api.stripe.com.default.svc.cluster.local   NXDOMAIN
attempt 2  api.stripe.com.svc.cluster.local           NXDOMAIN
attempt 3  api.stripe.com.cluster.local               NXDOMAIN
attempt 4  api.stripe.com                             answer

4 attempts × 2 record types (A and AAAA) = 8 queries for one hostname
```

Add a fourth search domain inherited from the node and it is ten. Per name,
per connection, on the cold path. The fixes, in increasing order of blast
radius: a trailing dot (`api.stripe.com.`), a per-pod `dnsConfig` with
`ndots:1`, or a node-local DNS cache.

**2. Alpine and musl.** musl issues its A and AAAA queries in parallel over
one socket, and has historically mismatched responses under load, producing an
NXDOMAIN for a name that resolves perfectly a second later. It also implements
a smaller set of `resolv.conf` options than glibc does — glibc's
`options single-request` forces the two queries to be sequential, and you
should verify whether your musl version honours anything equivalent before
relying on it. The durable fix is not basing production Python images on
Alpine.

**3. asyncio's resolver is a thread pool.** `loop.getaddrinfo()` is
`socket.getaddrinfo` dispatched to the default `ThreadPoolExecutor`, whose
default size is `min(32, cpu_count + 4)` — and which is shared with everything
else you send to `run_in_executor`. Saturate it with blocking work and DNS
lookups start "timing out" with no DNS problem anywhere in the system:
[cpython#112169](https://github.com/python/cpython/issues/112169). This is
Layer 1 Topic 3's blocking-in-async failure wearing a network costume, and it
is nasty precisely because every DNS metric you own will look perfect.

**The caches, ranked by how often they cause the incident:**

1. **The connection pool.** Worst by a distance. An established socket does
   not care what DNS now says, and nothing will make it care except closing
   it.
2. **`aiohttp`'s built-in resolver cache** — `use_dns_cache=True`,
   `ttl_dns_cache=10` by default: a real, on-by-default cache in your stack
   that uses its own fixed TTL rather than the record's.
3. **CoreDNS and node-local caches**, which do honour the record TTL.
4. **Your process**, which caches nothing — in Python, Go and Node.

The single fix that addresses cause 1, which is the one that matters, is a
**maximum connection lifetime**: retire every connection after N seconds
regardless of health. It costs you a handshake every N seconds per connection
and it converts an unbounded outage into a bounded one.

## How each language actually gets there

The resolver *is* the runtime here — six languages, six genuinely different
resolvers with different failure modes, so all six earn their place.

**Python.** `socket.getaddrinfo` is a direct call into libc, blocking, no
cache. Under asyncio it is dispatched to the shared default executor, which is
landmine 3 above. `aiohttp` adds its own 10-second cache in front, ignoring
the record's TTL; `httpx` and `requests` add nothing. So within one language
you have three different DNS behaviours depending on which client you imported.

**Node.js.** `dns.lookup()` — which is what every HTTP client uses by default —
calls `getaddrinfo` on **libuv's thread pool**, which defaults to four
threads and is shared with file IO and some crypto. Four concurrent slow
lookups therefore stall unrelated file reads, and the symptom is a service
that goes sluggish everywhere at once during a DNS blip. `dns.resolve()` is
the other API entirely: it speaks DNS over the network with c-ares, does not
touch the thread pool, and does not consult `/etc/hosts` — which is why it
sometimes returns a different answer than `dns.lookup()` for the same name.
Raise `UV_THREADPOOL_SIZE` if you must, but know that you are widening a queue,
not removing it.

**Go.** Go ships **two** resolvers: a pure-Go one that reads `/etc/resolv.conf`
and speaks DNS itself, and a cgo one that calls the system `getaddrinfo`. Which
one you get is decided by runtime heuristics, and you can force it with
`GODEBUG=netdns=go` or `netdns=cgo` (add `+2` for the resolver's own debug
output). This matters in containers more than anywhere else: the pure-Go
resolver does not implement everything `nsswitch.conf` can express, so a
lookup that works on your glibc host can behave differently in an Alpine
image, from the same binary. Go caches nothing.

**Rust.** `std`'s `ToSocketAddrs` calls blocking `getaddrinfo`. tokio and
reqwest push that onto `spawn_blocking`'s **dedicated blocking pool** rather
than onto the reactor's worker threads — which is the same fix Python needs
and does not apply by default, and it is why a Rust service degrades more
gracefully than an asyncio one during a DNS stall. Swap in `hickory-dns` and
you get an async resolver that actually honours record TTLs, at the cost of no
longer consulting `/etc/hosts` and `nsswitch` unless you configure it to.

**C++.** `getaddrinfo` blocks the calling thread. That is the whole story:
POSIX offers no asynchronous name resolution worth using, so every runtime
above is wrapping this same blocking call in a thread pool of some shape.
Writing it once — `getaddrinfo` on the calling thread, then `getaddrinfo` on a
worker with a queue — makes the design space visible, and makes clear that
c-ares exists because the standard library had no answer.

**Java.** The JVM is the one runtime here that caches by default, controlled
by `networkaddress.cache.ttl` and `networkaddress.cache.negative.ttl` in
`$JAVA_HOME/conf/security/java.security`. Older JDK behaviour cached
successful lookups indefinitely when a security manager was installed, which
is exactly why "the JVM caches DNS forever" is folklore in Java shops and not
in Python ones — and why AWS's SDK documentation has spent a decade telling
people to set that property. Read the value out of the file on the JDK you are
running before repeating either the folklore or this paragraph.

## The experiment

`lab/topic5/` — `upstream` is reachable through a compose network alias. Mid
run, start a second upstream container, move the alias to it, and stop the
first. DNS now points at a new IP address; the only question is when each
client notices.

Variants, all under steady load with warm pools:

| Variant | What it isolates |
|---|---|
| `requests` with a default `Session` | urllib3's pool, no expiry |
| `httpx` with default `keepalive_expiry=5` | a short idle expiry as an accidental fix |
| `httpx` with `keepalive_expiry=None` | connections that never expire |
| `aiohttp` with its default DNS cache | a resolver cache with its own TTL |
| any client with a **maximum connection lifetime** of 60 s | the actual fix |

Measure seconds of errors after the swap, and whether the client recovers
without a restart.

**Second half — the `ndots` packet count.** Add `options ndots:5` and a
Kubernetes-shaped search list to `api`, then measure resolution latency with
`dig` and packet count with `tcpdump -n port 53` for a cold external lookup,
with and without a trailing dot. Compare against the derivation above; if your
count does not match the arithmetic, work out which assumption was wrong
before you record the number.

The single-file programs, one per runtime, each resolving the same name under
load and reporting what it saw:

| Directory | What it shows |
|---|---|
| [`python/`](python/) | `pool_outlives_dns.py` — four connection-lifetime policies through a name change, and which ones ever recover; `executor_starvation.py` — the resolver queued behind your own blocking work on the shared default executor |
| [`nodejs/`](nodejs/) | `dns.lookup` and `fs.readFile` stalled by the same `pbkdf2`, then `lookup()` versus `resolve()` on one name |
| [`golang/`](golang/) | the same binary run three times under `netdns` default / `go` / `cgo`, with the resolver's own `+2` debug output |
| [`rust/blocking_pool_resolver/`](rust/) | the identical blocking `getaddrinfo` on the reactor's worker versus on `spawn_blocking`'s pool, measured with a ticker |
| [`cpp/`](cpp/) | wait time versus service time for the same lookups on a pool of four and a pool of one |
| [`java/`](java/) | the two cache-TTL properties read off your JDK, then positive and negative caching timed |

Two of those deserve a warning before you run them, because both are ways to
measure nothing and believe you measured something. The Python and Rust
programs each run two variants in one process, and the *first* variant warms
your OS resolver's cache for the second — so compare the columns each program
tells you to compare, not the raw times. And the C++ program generates fresh
random names on every phase for exactly that reason; if you edit it to reuse a
name list, every phase after the first becomes a cache hit and the pool-size
effect vanishes.

## How to run

```
cd 02-network/lab
# UPSTREAM_URL is the one lab-wide default this topic overrides, and it is not
# optional. By default `api` reaches `upstream` through Toxiproxy, so the name
# that moves is resolved by *toxiproxy*, whose per-connection dial re-resolves
# it immediately -- api's pool holds sockets to `toxi`, which does not move,
# and the swap produces a couple of stray errors instead of an outage. Point
# api straight at the name whose address is about to change.
UPSTREAM_URL=http://upstream:9000 docker compose up -d api upstream toxi
docker compose exec api sh -c "ss -tan state established | grep :9000 | wc -l"   # warm pool, to the OLD ip
docker compose run --rm load run /scripts/topic5.js &
docker compose --profile failover up -d upstream_b
# `docker network disconnect` takes a CONTAINER, and `upstream` is a network
# ALIAS, not a container -- the command fails with "No such container:
# upstream". The container is named <project>-<service>-<replica>; with the
# project name `lab` that is lab-upstream-1. Confirm with:
#   docker network inspect lab_default --format '{{range .Containers}}{{.Name}} {{end}}'
docker network disconnect lab_default lab-upstream-1
docker compose exec api sh -c "cat /etc/resolv.conf; dig +short upstream"
docker compose exec sniff tcpdump -n -i any port 53 -c 40
```

The network name `lab_default` and the second service `upstream_b` are fixed
by [`../lab/README.md`](../lab/README.md); if `docker network disconnect`
reports no such network, run `docker network ls` and fix the compose project
name rather than editing the command here. `upstream_b` sits behind a compose
profile so it is not started by default — `docker compose --profile failover up
-d upstream_b` if the plain form does not bring it up.

The single-file programs, from this topic's directory:

```
python3 python/pool_outlives_dns.py && python3 python/executor_starvation.py
node nodejs/lookup_vs_resolve_threadpool.js
UV_THREADPOOL_SIZE=16 node nodejs/lookup_vs_resolve_threadpool.js
cd golang && go run two_resolvers_one_binary.go
cd rust/blocking_pool_resolver && cargo run --release
c++ -O2 -std=c++17 -pthread -o /tmp/dnsblocks cpp/getaddrinfo_blocks.cpp && /tmp/dnsblocks
cd java && javac DnsCacheTtl.java -d /tmp/javabuild && java -cp /tmp/javabuild DnsCacheTtl
java -Dsun.net.inetaddr.ttl=0 -cp /tmp/javabuild DnsCacheTtl
```

All seven are standard library only (the Rust one needs tokio). Several resolve
real names, so they need working DNS; each one degrades to a recordable "did
not resolve" line rather than to a crash if you are offline.

Two of them read something off your own machine that is worth seeing before
you touch the compose stack. `two_resolvers_one_binary.go` prints the first
lines of `/etc/resolv.conf`, where **macOS states in its own words that the
file is not consulted for hostname resolution** — the pure-Go resolver reads it
anyway, which is the whole class of bug that topic paragraph is about. And
`DnsCacheTtl.java` prints `networkaddress.cache.ttl` and
`networkaddress.cache.negative.ttl` from the JDK you are running, rather than
repeating a number from a blog post. Record what yours says.

## Predict, then record

- Seconds of errors after the swap, `requests` default: ______
- The same, with a 60 s maximum connection lifetime: ______
- DNS packets to resolve `api.stripe.com` under `ndots:5`: ______
  (derive it first from your actual search list) ; with a trailing dot: ______

| Client config | error window (s) | requests failed | recovered without restart? |
|---|---|---|---|
| requests, default Session | | | |
| httpx, keepalive_expiry=5 | | | |
| httpx, keepalive_expiry=None | | | |
| aiohttp, default DNS cache | | | |
| max connection lifetime 60 s | | | |

| Runtime | caches lookups? | where the lookup runs | what stalls when DNS is slow |
|---|---|---|---|
| Python (asyncio) | | | |
| Node | | | |
| Go | | | |
| Rust (tokio) | | | |
| C++ | | | |
| Java | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- Every variant recovers in about a second → the pool was never warm. The swap
  must happen while connections are established and in steady use; confirm
  with `ss -tan` that `api` has established connections to the *old* address
  first.
- Nothing recovers, ever, in any variant → you removed the name, not just the
  address. `dig +short upstream` from inside `api` must return the new IP.
- Identical DNS packet counts with and without the trailing dot → your
  `resolv.conf` does not have the `ndots` option you think it has. `cat` it
  before believing the result.
- Errors start *before* you move the alias → you have reproduced something
  else, probably Topic 4. Good find; record it separately rather than folding
  it into this table.
- The Node thread-pool experiment shows no effect → `UV_THREADPOOL_SIZE` is
  set in your image, or your concurrency is below the pool size. Check the
  environment before concluding libuv does not do this.

## Answer before moving on

1. Your RDS writer fails over. DNS updates within 30 seconds and the TTL is 5
   seconds. Some pods recover immediately and some stay broken for twenty
   minutes. Explain the difference with no reference to DNS caching at all.
2. Lowering TTL is the standard pre-migration advice. Give two concrete
   reasons a low TTL does not guarantee fast client failover.
3. Why is "the DNS lookup timed out" an unreliable signal in an asyncio
   service, and what would you graph to separate a real DNS problem from the
   impostor?
4. Go picks between two resolvers by heuristic at runtime. Describe a bug that
   would appear only after a base-image change, with no code change, and say
   how you would confirm the resolver was the cause in under five minutes.

## Next up

[Topic 6 — Head-of-line blocking, multiplexing, and what loss does](../06-head-of-line-blocking-and-multiplexing/README.md):
you have been assuming one request per connection. Multiplexing removes that
assumption and replaces it with a different limit.
