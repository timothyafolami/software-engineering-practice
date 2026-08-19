# Layer 2 · Topic 7 — See it on the wire: tcpdump and ss against your own service

### The takeaway (read this first)

**The one idea:** every claim in Topics 1 through 6 is directly observable on
your own machine in about twenty minutes, and once you have watched a
handshake, a keep-alive reuse and an RST with your own eyes, you stop
reasoning about networks by analogy.

**Why it matters in practice:** the roadmap is blunt about this — *run tcpdump
against your own service once, it changes how you think.* It is the difference
between believing your client pools connections and knowing that it does.

**You'll know it landed when:** during an incident, "is it reusing
connections?" is a question you *answer in sixty seconds* rather than one you
argue about for an hour.

## The concept

Two tools, two different jobs.

**`ss`** (iproute2, inside the Linux container — macOS has no `ss`; `lsof -i`
and `netstat -an` **[host]** only if you must) shows sockets *as state*:

```
ss -tan                                                  # every TCP socket and its state
ss -tan state established '( dport = :8000 )' | wc -l    # is the pool actually pooling?
ss -ti dst upstream                                      # rtt, cwnd, retrans, congestion algo, bytes
ss -tln                                                  # listeners and accept backlog
ss -tan state time-wait | wc -l                          # churn — i.e. pooling is not working
```

`ss -ti` is the underrated one. It gives you `rtt`, `cwnd`, `retrans`,
`bytes_acked` and the congestion algorithm *per socket* — which is Topic 6's
entire mechanism, live, with no packet capture at all.

**`tcpdump`** shows sockets *as events*. Five things to recognise on sight:

1. `[S]` → `[S.]` → `[.]` — the three-way handshake. Count them per second
   under load. If your pool works, that number is close to zero.
2. A TLS ClientHello, with its SNI in the clear. Note whether it spans more
   than one segment now that hybrid post-quantum key exchange is a default
   (Topic 1). This is where you check that claim instead of believing it.
3. A request and response on an *existing* connection with no handshake in
   front of it: keep-alive, visibly working.
4. `[F.]` versus `[R]` — a graceful close versus a reset. Topic 4's 502 is a
   FIN, then a request on the same four-tuple, then an RST.
5. Retransmissions and duplicate ACKs — which is what loss looks like before
   it turns into latency.

**The macOS-specific part, which matters because it is where Layer 1 broke.**
You cannot usefully tcpdump container traffic from your Mac. Docker Desktop
runs containers inside a Linux VM, there is no `veth` interface on your host to
attach to, and the `nsenter` recipes in Linux blog posts do not work because
the PID they want lives in the VM. The portable pattern is the `sniff` sidecar
described in [`../lab/README.md`](../lab/README.md): `network_mode:
"service:api"`, `cap_add: [NET_ADMIN, NET_RAW]`, and a shared volume for the
`.pcap`. It sees exactly `api`'s interfaces, on every platform, with no
host-specific setup.

## How each language actually gets there

**This topic's subject is the tooling, not the runtime — `tcpdump` and `ss`
behave identically no matter what wrote the packets, so there is no
per-language mechanism to explain here.** The six languages earn their place a
different way: re-run Topic 1's six clients, one at a time, under a single
capture, and count SYNs. Everything Topic 1 claimed about `MaxIdleConnsPerHost`,
undici's global agent, hyper's shared pool, libcurl handle reuse and Java's
invisible per-client pool becomes one number per language, measured the same
way, with nothing to argue about. A runtime that claims to pool and emits one
SYN per request does not pool.

That table is the most convincing artefact this layer produces, and it costs
one capture per language.

## The experiment

Re-run three earlier topics and *watch* them.

**Pooling (Topic 1).** Capture `tcp[tcpflags] & tcp-syn != 0` for 60 s under
load, once with the COLD variant and once with WARM, and count SYNs. One SYN
per request versus almost none is the most convincing measurement in the
layer. Then repeat for each of the six Topic 1 clients.

**The 502 race (Topic 4).** Filter to FIN and RST, find one instance, and write
down the timestamps and the four-tuple. One instance is enough; the point is
that you saw it.

**DNS (Topic 5).** `tcpdump -n port 53` while resolving with and without a
trailing dot under `ndots:5`, and count packets. Compare against the
derivation in Topic 5 rather than against a remembered number.

**Then, once, do it against the service that is actually slow in production** —
in staging, at whatever load you can generate there. You are looking for one
thing: the SYN rate. If it is high, you have found your latency problem and
Topic 1 is the fix.

## How to run

```
cd 02-network/lab
docker compose exec sniff tcpdump -i any -nn -c 200 'tcp[tcpflags] & tcp-syn != 0 and not tcp[tcpflags] & tcp-ack != 0'
docker compose exec sniff sh -c "tcpdump -i any -nn -w /caps/pool.pcap 'port 8000' & sleep 60; kill %1"
docker compose exec api ss -ti state established
docker compose exec api ss -tan state time-wait | wc -l
open lab/caps/pool.pcap        # [host] — read it back in Wireshark
```

The first filter matches SYN-without-ACK, i.e. connection *initiations* only,
which is the number you want. A plain `tcp-syn != 0` filter also matches every
SYN/ACK and doubles your count. The `-c 200` cap stops it running forever; drop
it when you are writing to a file instead of to the terminal.

Those commands are also in [`sniff/`](sniff/) as two scripts, so you are not
retyping filters during an incident. Copy them into the containers and run
them there — both are Linux-only by nature:

```
cd 02-network/lab
docker compose cp ../07-see-it-on-the-wire/sniff/capture.sh sniff:/capture.sh
docker compose cp ../07-see-it-on-the-wire/sniff/sockets.sh api:/sockets.sh
docker compose exec sniff sh /capture.sh syns 60      # SYNs/s, printed
docker compose exec sniff sh /capture.sh pcap 60      # to /caps/pool.pcap
docker compose exec sniff sh /capture.sh fins 60      # topic 4's FIN/RST hunt
docker compose exec sniff sh /capture.sh dns          # topic 5's ndots count
docker compose exec api   sh /sockets.sh              # ss: estab, TIME-WAIT, rtt/cwnd/retrans
```

### The six-language table, without root and without a capture

The per-language half of this topic needs no privileges at all, because a
server that counts `accept()` calls sees exactly what a SYN counter sees, from
the other end:

```
python3 python/pools_as_advertised.py
```

That starts a connection-counting server, runs all six clients against it one
at a time — [`python/syn_client.py`](python/), [`nodejs/syn_client.js`](nodejs/),
[`golang/syn_client.go`](golang/), [`rust/syn_client/`](rust/),
[`cpp/syn_client.cpp`](cpp/), [`java/SynClient.java`](java/) — and prints
connections against requests for each. Every client uses its runtime's
*default* pooled client and reuses it, which is precisely the claim Topic 1
made about each of them.

A toolchain that is missing prints `BLOCKED` with the command to fix it rather
than being skipped: an absent row and a zero row mean different things. Run the
individual clients on their own against any URL — including your own service —
with `LAB_URL=http://host:port/path LAB_REQUESTS=100`, and the C++ one with
`LAB_CONTRAST=1` to see a fresh libcurl handle per request next to a reused
one, which is the same one-line bug as `httpx.Client()` inside a handler.

The accept-count and the SYN-count are not quite the same number, and the
difference is informative: retransmitted SYNs and connections that never
completed the handshake appear in the capture and never reach `accept()`. If
the two disagree by more than a little, that gap is a finding.

## Predict, then record

- SYNs/s, COLD at 200 rps: ______   · SYNs/s, WARM at 200 rps: ______
- TIME-WAIT sockets after a 60 s COLD run: ______
- Which language will emit the most SYNs for the same workload? ______

| Capture | SYNs/s | estab conns | TIME-WAIT | notable |
|---|---|---|---|---|
| COLD, 200 rps | | | | |
| WARM, 200 rps | | | | |
| Production-ish service | | | | |

| Language client | SYNs over 60 s | estab conns at end | pools as advertised? |
|---|---|---|---|
| Python (httpx) | | | |
| Node (undici) | | | |
| Go (net/http) | | | |
| Rust (reqwest) | | | |
| C++ (libcurl) | | | |
| Java (HttpClient) | | | |

Output shape:

```
<your count> packets captured over <your duration> s  →  <your number> SYNs/s
```

**What would mean the experiment is broken, not the prediction wrong:**

- No packets at all → wrong namespace (it must be the `sniff` sidecar), wrong
  interface (use `-i any`), or you filtered on a port the traffic does not use.
- SYN counts identical between COLD and WARM → COLD is accidentally reusing a
  client, or both paths go through a proxy that pools on their behalf. nginx
  will do exactly this and mask the difference entirely — capture between
  `api` and `upstream`, not between `load` and `lb`.
- Zero TLS traffic → the lab talks cleartext internally by design. Point one
  capture at a real HTTPS host if you want to see a ClientHello and count its
  segments.
- Everything looks perfect in production → you may be fine, or you captured a
  quiet window. Note the request rate during the capture. A measurement with
  no load figure attached is not a measurement.
- SYN count higher than request count → you are capturing both directions of a
  proxied path, or retransmitted SYNs are being counted. Check for `[S]` with
  the same sequence number repeating.

## Answer before moving on

1. You see 400 SYNs/s and 300 requests/s to one upstream. What are the possible
   explanations, and how would you distinguish them without changing any code?
2. A socket sits in `TIME-WAIT` for roughly 60 seconds. Why does that state
   exist at all, and why is *the client* accumulating them a much worse sign
   than the server accumulating them?
3. From `ss -ti` alone, how would you tell "the network is slow" from "the
   upstream is slow"? Name the fields and say which direction each moves.
4. You have one capture and sixty seconds of a colleague's attention. Which
   single number from this topic would you show them to settle an argument
   about whether a service pools connections, and what would they be entitled
   to object to?

## Next up

**Layer 3 — Data and databases.** The roadmap calls it the highest-return
section on the page, and Topic 2 already put you on its doorstep: pool
exhaustion is where the network layer hands off to the database layer, and the
reason your pool was full is going to turn out to be a query plan.
