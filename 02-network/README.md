# Layer 2 · The network

> **You own this layer when:** someone says *"the service just hangs
> sometimes"* and your first three questions are about **timeouts, pool size,
> and DNS** — and you are usually right.

Layer 1 was one process on one machine. This layer starts the moment a second
machine appears, which is the moment "it works on my machine" is born and the
moment *slow* becomes more dangerous than *down*.

| # | Topic | Folder |
|---|---|---|
| 1 | What a connection actually costs | [`01-what-a-connection-actually-costs/`](01-what-a-connection-actually-costs/README.md) |
| 2 | Connection pooling and pool exhaustion | [`02-connection-pooling-and-pool-exhaustion/`](02-connection-pooling-and-pool-exhaustion/README.md) |
| 3 | Timeouts as a first principle | [`03-timeouts-as-a-first-principle/`](03-timeouts-as-a-first-principle/README.md) |
| 4 | Keep-alive across a load balancer | [`04-keep-alive-across-a-load-balancer/`](04-keep-alive-across-a-load-balancer/README.md) |
| 5 | DNS, TTLs, and the pod that kept talking to a dead IP | [`05-dns-ttls-and-the-dead-ip/`](05-dns-ttls-and-the-dead-ip/README.md) |
| 6 | Head-of-line blocking, multiplexing, and what loss does | [`06-head-of-line-blocking-and-multiplexing/`](06-head-of-line-blocking-and-multiplexing/README.md) |
| 7 | See it on the wire: tcpdump and ss against your own service | [`07-see-it-on-the-wire/`](07-see-it-on-the-wire/README.md) |

## The shared lab

Every topic runs against one compose stack: [`lab/`](lab/README.md). It holds
the service list (`api`, `db`, `upstream`, `upstream_b`, `toxi`, `lb`, `load`,
`sniff`), the environment variables each topic switches on, the ports and
volume paths the run commands depend on, and the version pins. Read it once
before Topic 1 and refer back rather than re-reading it per topic.

Two rules from that file are worth restating here because they invalidate
results silently when broken. **Load is open-model** — k6's
`constant-arrival-rate` executor, never a fixed VU count, because a closed-loop
generator slows down when your service does and therefore cannot reproduce
queueing at all. And **everything that inspects a network runs inside a Linux
container**: `ss`, `tcpdump`, `/proc` and `resolv.conf` do not exist, or do not
mean the same thing, on the macOS 27 / arm64 machine this lab targets.
Host-only commands are marked **[host]**.

## The language set

**Six: Python, Node.js, Go, Rust, C++, Java** — the lab-wide set, for the
reasons in the root [`README.md`](../README.md). Not every topic uses all six;
each topic states its reason in one line where it uses fewer.

Where the runtime *is* the mechanism, all six appear, and the contrast is the
lesson rather than repetition: Topics 1, 2, 3 and 5 are about connection
pools, timeouts and resolvers, which are runtime data structures, and six
runtimes made six incompatible decisions about each of them. Topic 4's
mechanism is two idle timers disagreeing across a proxy, so it uses the four
runtimes whose *server* defaults differ. Topic 6's mechanism is in RFC 9113
rather than in any runtime, so it uses three clients. Topic 7's subject is
`tcpdump` and `ss`, which do not care what wrote the packets — so it uses the
six languages as a measurement instead, counting SYNs per runtime under one
capture.

## No fabricated results

Every topic ends with **Predict, then record**: a prediction you write *before*
running, a blank table you fill in *after*, and a list of outcomes that would
mean **the experiment is broken rather than your prediction wrong**. The tables
ship empty and stay empty until you run something. Beyond that, every number in
the prose here is either derived on the page or carries a source — an uncited
statistic is the same defect as a fabricated table, only harder to spot.
Predictions go in [`PREDICTIONS.md`](../PREDICTIONS.md).

## Resources

| | Why |
|---|---|
| [High Performance Browser Networking](https://hpbn.co/) (Grigorik, free) | Still the best free treatment of TCP, TLS and HTTP/2 — but dated in three specific places, listed in Topic 6 |
| RFCs [9110](https://www.rfc-editor.org/rfc/rfc9110), [9113](https://www.rfc-editor.org/rfc/rfc9113), [9114](https://www.rfc-editor.org/rfc/rfc9114), [9000](https://www.rfc-editor.org/rfc/rfc9000), [8446](https://www.rfc-editor.org/rfc/rfc8446) | HTTP semantics, HTTP/2, HTTP/3, QUIC, TLS 1.3 — the primary sources for Topics 1 and 6 |
| [Metastable Failures in Distributed Systems](https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf) (Bronson et al., HotOS '21) | Short, and the paper behind Topic 3 |
| AWS Builders' Library, *Timeouts, retries and backoff with jitter* | The retry budget in implementable form |
| The source of `httpx._config.Timeout`, `urllib3.connectionpool`, `aiohttp.connector` | Per the roadmap's read-one-library-a-month rule. They are short and you use them daily |

## Next up

**Layer 3 — Data and databases.** Topic 2 already put you on its doorstep:
pool exhaustion is where the network layer hands off to the database layer,
and the reason your pool was full is going to turn out to be a query plan.
