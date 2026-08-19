# Layer 4 · Topic 1 — Partial failure and the ambiguous result

### The takeaway (read this first)

**The one idea:** a local call has two outcomes; a remote call has three, and
the third — *I do not know whether it happened* — is not an error state you can
engineer away. It is the normal case, and everything else in this layer exists
to make it safe.

**Why it matters in practice:** a slow service means callers are timing out,
and a timeout is precisely the ambiguous outcome. If they retry (they do) and
your handler is not idempotent (it probably is not), a latency problem is
already producing duplicate work, which makes you slower, which produces more
timeouts. That loop is the shape of a metastable failure and it starts here.

**You'll know it landed when:** you look at an `except Exception` around a
network call and can immediately say which exceptions inside it are safe to
retry — and discover you cannot write correct error handling without knowing
whether the operation is idempotent, which is why Topic 2 exists.

## The concept

The outcome space for a remote call, written out:

1. the request was lost before it arrived;
2. the request arrived, the work was done, the response was lost;
3. the work is still in progress and you gave up waiting;
4. the server crashed *after* committing, before replying;
5. the server crashed *before* committing.

**Several of these are indistinguishable from the caller, and no amount of
caller-side instrumentation fixes that**, because the information you need is on
the far side of the thing that broke. Everything downstream in this layer is a
consequence of that one fact: idempotency (Topic 2) makes retrying an unknown
safe; the outbox (Topic 6) removes the unknown between database and broker;
consensus (Topic 5) removes the unknown about who is in charge; fencing (Topic
7) makes a stale actor harmless rather than absent.

**CAP, correctly.** It is not a menu you pick two from. Linearizability and
availability are in tension *only while a partition is happening*; with no
partition you have both. The real statement: when the network splits, you either
refuse requests (CP) or serve possibly-stale, possibly-divergent answers (AP),
and you have already decided which whether you know it or not. **That decision
is not on a whiteboard, it is in your `except` block.** Go and read yours.

The extension worth carrying is **PACELC**: if Partitioned, trade Availability
or Consistency; **Else**, trade Latency or Consistency. The "else" half is where
you live daily. And the sting: to a timeout, a *slow* dependency is
indistinguishable from a partitioned one — so your CAP choice fires on latency,
not on cable cuts, and therefore fires constantly.

## How each language actually gets there

**Languages: all six.** This is a runtime topic. What a client tells you about a
failed remote call is decided entirely by the HTTP stack in front of you, and
the six runtimes draw the safe/unsafe line in six different places — one of them
(Node) does not draw it at all by default, and one of them (Java) retries behind
your back. That contrast *is* the topic.

**Python — `httpx`, a taxonomy with one dangerous parent.** `ConnectError` and
`ConnectTimeout` prove the request never landed; `ReadTimeout`, `WriteTimeout`,
`RemoteProtocolError` and `ReadError` prove nothing at all. All six are
subclasses of `httpx.HTTPError`, and catching the parent is the bug. Separately,
`asyncio.CancelledError` on client disconnect cancels *your* coroutine, not the
work already handed downstream — that downstream commits anyway.

**Node.js — no default timeout at all.** A `fetch` against a silent server is
still outstanding minutes later and nothing in the call asked for that.
`AbortSignal.timeout(ms)` fixes the hang, but it is a deadline on the whole
operation, so it reports one `TimeoutError` whether it fired during the
handshake or mid-body — the distinction you need is exactly the one it erases.
Dropping to `node:http` keeps the connect phase and the response phase apart;
undici's `UND_ERR_CONNECT_TIMEOUT` vs `UND_ERR_HEADERS_TIMEOUT` vs
`UND_ERR_BODY_TIMEOUT` map onto the same split.

**Go — `net/http/httptrace`, the only client that will simply tell you.**
`ConnectDone` and `WroteRequest` fire at exactly the boundary that decides
whether a retry is safe, so the classification is a *fact* rather than an
inference. Go also gives the clearest demonstration that context cancellation is
a **local** event: `errors.Is(err, context.DeadlineExceeded)` on the client says
nothing about the server, and a handler that has already read the request body
finishes its transaction regardless of what the caller did.

**Rust — no HTTP client, and a permit type.** `connect` / `write` / `read` are
three separate calls, so "which phase failed" is just "which line failed", and a
write that moved zero bytes is distinguishable from a partial write — a
distinction no mainstream HTTP client exposes. Then a `RetryPermit` type with a
private constructor makes retrying an `Unknown` outcome *fail to compile*; the
verbatim `error[E0603]` is at the bottom of the source file. The single `unsafe`
block is `setsockopt(SO_LINGER)`, because `TcpStream::set_linger` is still
unstable — a fair picture of exactly where the guardrails end.

**C++ — the same hazards with nothing in the way.** `connect()` failed, `send()`
failed at zero bytes, `send()` failed after *n* bytes, `recv()` failed: four
distinct facts straight from the kernel, with no library deciding which of them
you are allowed to see. And nothing prevents the wrong retry policy — the naive
version compiles clean at `-Wall -Wextra`. That is the direct comparison with
Rust, and the reason both are here.

**Java — the safe case is a subclass of the unsafe one.** The program prints, at
runtime, that `HttpConnectTimeoutException extends HttpTimeoutException`. So
`catch (HttpTimeoutException e)` silently covers both, and only testing the
subclass first works. The sharper finding is in phase 0: `HttpClient` **retries
idempotent methods by itself** when a connection dies before a response — one
call, one exception, two charges on the server. The measured phases use `POST`
so they measure the retry policy in the file rather than the one in the JDK.

## The experiment

**Part A — six programs, no infrastructure.** Each runs a real ledger server
in-process, so there is a **server-side truth** to diff the client's belief
against, then injects six faults:

| fault | what the server does |
|---|---|
| `ok` | commits, replies |
| `slow` | commits, replies **after** the client's deadline |
| `hang` | commits, never replies at all |
| `reset` | commits, then RSTs the connection (`SO_LINGER 0`) |
| `crash_after_commit` | commits, then dies without writing a byte |
| `refused` | nothing is listening — the request provably never landed |

Four of those six commit and then fail to tell the caller. Exactly one is
provably safe to retry.

Every program runs the same load twice. **Phase 1** retries on any error — this
is `except httpx.HTTPError:`, `catch (e) { retry() }`, `if err != nil { retry }`,
`Err(_) => continue`, `if (rc != 0) continue;`, the line almost every codebase
has. **Phase 2** retries only failures that prove the request was never sent.
Both phases print `DUPLICATE CHARGES` (rows the *client's own retry* created)
and `unresolved ambiguous outcomes`. The first number collapses. The second does
not — and that residue is the whole reason Topic 2 exists.

The deadlines are in the source and deliberately tight: `CLIENT_TIMEOUT = 0.3`
seconds against a `SLOW_RESPONSE = 1.0` second reply, so `slow` always misses.

**Part B — the compose version** (`../docker/`, see [`../lab/`](../lab/README.md)).
`payments-api` calls `ledger` through Toxiproxy, driven by k6. `ledger` writes
every accepted charge to Postgres with the caller's request id — server-side
truth. k6 records what each request *appeared* to do — the client's belief. The
experiment is the diff, and the deliverable is a reconciliation query counting
**orphaned charges**: rows the server committed that the client recorded as
failures. One run per toxic — `timeout` (blackhole), `latency 5000` against a 2s
client timeout, `reset_peer`, `bandwidth 0` applied after headers — then a fifth
with `CRASH_AFTER_COMMIT=1`, which commits the charge and then `os._exit(1)`
before replying. That last one makes ambiguity 100% and no timeout tuning will
ever fix it.

## How to run

Part A needs nothing running — no Docker, no Postgres, no network. Each takes
about ten seconds and binds an ephemeral loopback port.

```
python3 python/ambiguous_result.py
node nodejs/ambiguous_result.js
cd golang && go run ambiguous_result.go
cd rust/ambiguous_result && cargo run --release
g++ -O2 -std=c++17 -pthread -Wall -Wextra -o /tmp/l4t1_cpp cpp/ambiguous_result.cpp && /tmp/l4t1_cpp
javac java/AmbiguousResult.java -d /tmp/javabuild && java -cp /tmp/javabuild AmbiguousResult
```

Part B is **blocked on this machine**: the Docker daemon is down and k6 is not
installed. Check with `python3 ../lab/local/check_env.py`. When it is up:

```
docker compose up -d postgres toxiproxy ledger payments-api
curl -s -XPOST localhost:8474/proxies/ledger/toxics \
  -d '{"type":"latency","attributes":{"latency":5000}}'
docker compose run --rm k6 run /scripts/topic1.js
psql -d sep_lab_04_dist -f sql/topic1_reconcile.sql
```

## Predict, then record

**Predict first, in writing.** For Part A: which language's phase-1 duplicate
count is highest, and why? Does any runtime produce duplicates in phase 2 — and
if one does, is that the runtime retrying or your code? For Part B: for each
toxic, what fraction of client-recorded failures actually completed
server-side? Which toxic gives the highest orphan rate? What does the client see
under `CRASH_AFTER_COMMIT`?

| language | phase 1 duplicates | phase 2 duplicates | unresolved ambiguity |
|---|---|---|---|
| Python | | | |
| Node.js | | | |
| Go | | | |
| Rust | | | |
| C++ | | | |
| Java | | | |

| Toxic | Client 2xx | Client errors (by type) | Ledger rows | Orphaned charges | Orphan rate |
|---|---|---|---|---|---|
| none (baseline) | | | | | |
| `timeout` | | | | | |
| `latency 5000` | | | | | |
| `reset_peer` | | | | | |
| `bandwidth 0` post-headers | | | | | |
| `CRASH_AFTER_COMMIT` | | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **Phase 1 and phase 2 give the same duplicate count.** The retry branch is not
  actually differing between phases — check the `SAFE`/`AMBIGUOUS` verdict is
  being *read*, not just logged.
- **`refused` writes ledger rows.** The closed-port probe raced something else
  onto that port. Re-run; if it persists, the port picker is broken.
- **Java's phase 0 shows one request for GET.** Then the JVM's `HttpClient` did
  not retry, and you should find out why before trusting the rest — most likely
  the method changed or the server replied with something parseable.
- **Any program reports zero unresolved ambiguity.** Not possible with `hang`
  and `crash_after_commit` in the mode list. Something is swallowing the outcome
  before it is counted.
- **Orphan rate 0 for `latency 5000`.** Your client timeout is probably longer
  than 5s, or `ledger` replies before it commits. Verify the timeout is 2s and
  that the commit precedes the response.
- **Orphan rate 0 for `CRASH_AFTER_COMMIT`.** This case cannot be zero. Either
  the crash fires before the commit, or you are reading client errors from the
  wrong run.
- **Baseline shows non-zero orphans.** That is a finding, not a harness bug.
  Something already times out under no fault at all. Chase it before continuing.

## Answer before moving on

1. Name a client-observable signal that distinguishes "my request was lost" from
   "the response was lost." Keep going until you are convinced there is not one.
2. Is your production service CP or AP today? Point at the specific lines where
   that is decided. (It is an exception handler, and nobody wrote it
   deliberately.)
3. Java retries GET for you. Name every *other* place in your stack — proxy,
   load balancer, service mesh, SDK, ORM — that might also be retrying, and say
   how you would find out for each one.
4. Why does adding a retry in front of a *slow* dependency lengthen the outage?
   Draw the loop, and mark the point where it becomes self-sustaining.
5. PACELC's "else": name one read path where you would trade consistency for
   latency, and say exactly what a user observes on the day that goes wrong.

## Next up

[Topic 2 — Idempotency keys, atomically](../02-idempotency-keys-atomically/README.md):
making the ambiguous outcome safe to retry, which is the only reason
idempotency keys exist.
