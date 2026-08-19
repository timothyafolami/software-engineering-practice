# Layer 5 · Topic 3 — Retries that don't become the outage

### The takeaway (read this first)

**The one idea:** retries multiply load at exactly the moment capacity is
lowest, and the multiplication is *multiplicative across a chain* — three
hops each retrying up to three times is up to 27× at the leaf, from a client
that sent one request.

**Why it matters in practice:** "add a retry" is the most common reliability
change engineers make, it improves the error dashboard on ordinary days, and
it is the most common ingredient in self-sustaining outages. It is also the
canonical amplification mechanism for topic 4.

**You'll know it landed when:** you cannot write a retry loop without also
writing the jitter, the cap and the budget, and you can point at which single
layer of an existing system owns retries.

## The concept

Four things must hold for a retry to be safe.

**(1) The failure is genuinely transient.** Retrying 400, 401, 403, 404 or
422 is pure waste — the same request will fail the same way. Retry connect
errors, connect timeouts, 429 and 503 (honouring `Retry-After`), and read
timeouts *only* where the operation is idempotent (topic 7 is the
precondition for that clause).

**(2) Backoff is exponential *with jitter*.** Everyone gets the exponential
part and skips the jitter, which is the part that matters. Without jitter,
every client that failed at the same moment retries at the same moment,
producing synchronised waves against a service that is trying to recover.
Full jitter — `sleep = random(0, min(cap, base * 2**attempt))` — is the AWS
Builders' Library recommendation and beats "exponential plus a little noise",
because it spreads the retries of a synchronised cohort across the *whole*
interval rather than around a common centre.

**(3) A hard cap** on both attempts and total elapsed time, fitting inside
topic 2's budget. A retry policy that can outlive its caller's deadline is
generating zombie work on purpose.

**(4) A retry *budget*.** Almost nobody implements this, and it is the piece
that actually prevents the outage. A budget is a token bucket that permits
retries only while retries stay under some fraction of successes. Envoy's
retry-budget defaults are documented as `budget_percent` 20% of active
requests with a `min_retry_concurrency` floor of 3; gRPC calls the same idea
`retryThrottling`; Yandex reported settling on 10% in their incident
write-up. The *property* is what matters and it is qualitative: at low
failure rates a budgeted client behaves like a normal retrying client, and as
failures climb its retry traffic goes **to zero automatically**, instead of
to maximum.

**Two findings that update older advice.**

**Exponential backoff does not prevent amplification, it delays it.** Past a
certain outage duration, backoff has spread retries as far as its cap allows
and steady-state amplification returns — you converge on
`attempts / backoff_cap` extra requests per second per client, forever.
Backoff buys time; only a budget bounds load. (Isaev's *Good Retry, Bad
Retry* is the clearest narrative treatment of this.)

**Retry at exactly one layer.** The structural alternative to per-hop retries
is that only the hop *adjacent to the failure* retries, and on exhaustion it
marks the error **non-retryable** on the way up, so no ancestor multiplies it
again. It is easier to reason about, it composes cleanly with topic 2's
deadline propagation, and it turns the worst case from `3³` back into `3`.

## How each language actually gets there

**Five languages here, not six: no C++.** The mechanism in this topic lives
in a policy library and a token bucket, not in the runtime — a C++ version
would be the same thirty lines as the Rust one with none of the ecosystem
lesson attached. The five below differ in what their ecosystems hand you for
free, which is the actual variable under study.

**Python** gives you the backoff and never the budget. `tenacity` has
`wait_exponential_jitter` built in and is the reasonable default choice;
`urllib3.util.Retry` gained `backoff_jitter` in 2.x, which older code almost
certainly does not set, so a `requests` `HTTPAdapter` configured years ago is
retrying in synchronised waves right now; and `httpx` has **no** retry logic
beyond connection-level retries, which is honest — it makes you supply the
policy. Nothing in the Python ecosystem ships a retry budget. The token
bucket is about thirty lines you write yourself, shared across the process,
and it is the highest-value thirty lines in this topic.

**Node.js** puts retries in the interceptor layer: undici's `RetryAgent` gives
you attempts, backoff and status-code selection at the dispatcher, which is
the right layer (one policy, all callers). Same missing budget. Node's
specific trap is that `fetch` failures surface as a generic `TypeError` with
the real reason on `.cause`, so a retryable-error predicate written against
the top-level error type retries everything or nothing.

**Go** has no stdlib retry at all — you reach for `failsafe-go` or
`cenkalti/backoff/v5` — but gRPC-Go implements service-config retry policies
**with throttling**, which is the closest thing to a batteries-included
budget anywhere in this lab's six languages. Go's other advantage is topic 2's:
because `context` carries the deadline, a correctly written retry loop cannot
outlive the caller's budget without you explicitly detaching it.

**Rust** makes the state machine explicit. `tower::retry` takes a `Policy`
trait — you implement `retry()` and `clone_request()` — and the second method
is the interesting one, because it forces you to answer "can this request even
*be* replayed?" at compile time. A streaming body is not clonable, so the type
system asks the idempotency question topic 7 answers, before you can compile
the retry. No mainstream Rust crate ships a budget either; a shared
`AtomicI64` token bucket is the natural implementation.

**Java** has the most mature policy libraries — Resilience4j's `Retry` with
`IntervalFunction.ofExponentialRandomBackoff`, Spring Retry, and gRPC-Java's
`retryThrottling` from the same service-config spec as Go's. The Java-specific
hazard is layering: a Feign client with retries, inside a Resilience4j
decorator with retries, over an `HttpClient` whose connection pool retries
idempotent requests, is three multipliers most teams do not know they have.
Counting the layers in an existing Java service is a genuinely useful exercise
and the answer is rarely one.

## The experiment

The three-hop chain again, each hop retrying up to 3 times with exponential
backoff. Toxiproxy injects a hard fault at the leaf's database for 20
seconds, then removes it.

1. Put a counter at the leaf for **requests received**, not requests
   succeeded, and compare it against the client's offered rate. That ratio is
   your live amplification factor.
2. Offer 50 rps. Fault on at t=60s, off at t=80s, observe until t=300s.
3. Chart amplification over time. Note its peak — and, more importantly, its
   value at **t=200s**, two minutes after the fault is gone.
4. Then three variants: **B** adds full jitter; **C** adds a 10%
   token-bucket budget at every hop; **D** retries only at the hop adjacent
   to the database and propagates non-retryable errors upward.

Output shape:

```
t=<s>  offered=<rps>  leaf_received=<rps>  amplification=<ratio>  success=<pct>
```

## How to run

Uses the shared harness — see [`../lab/README.md`](../lab/README.md).

**Built, and executed on this machine.** The shared harness exists —
`lab/docker-compose.yml`, `lab/app/`, `lab/scripts/*.js`, `lab/tools/*.py`,
specified in [`../lab/README.md`](../lab/README.md) — and the commands below
were run against it. You do **not** need to install `k6`: it runs from the
`grafana/k6` image, which is what `docker compose run --rm k6` starts. What
you do need is Docker running (`docker info`) and host ports 8000-8003 free —
if something else on your machine holds 8000, `up` fails with `port is
already allocated`. From `05-failure/lab/`:

```
cd ../lab
docker compose --profile chain up -d --build
for V in naive jitter budget edge_only; do
  docker compose run --rm k6 run /scripts/03_retry_storm.js -e VARIANT=$V \
    --out csv=/out/03_retry_storm_$V.csv
done
python3 tools/plot_amplification.py out/
```

Each run is five minutes: the script adds the toxiproxy fault at t=60s and
removes it at t=80s from inside the run, so all four variants get the same
fault window. The plotter prints amplification at t=200s, which is the column
the experiment is actually about.

The plotter runs today against the synthetic fixtures that ship with the
harness — a model, not a measurement, there so a broken plotting script is
found by running it:

```
cd ../lab && python3 tools/make_fixtures.py
python3 tools/plot_amplification.py out/fixtures/
```

The standalone versions simulate the same chain in one process, so you can
watch amplification without containers:

```
python3 python/retry_storm.py
node nodejs/retry_storm.js
cd golang && go run retry_storm.go
cd rust/retry_storm && cargo run --release
cd java && javac RetryStorm.java -d /tmp/javabuild && java -cp /tmp/javabuild RetryStorm
```

## Predict, then record

Before running: what is peak amplification at the leaf in the naive variant?
The theoretical worst case for 3 hops × 3 attempts is 27× — will you see it,
and why or why not? Does jitter reduce *peak* amplification, *sustained*
amplification, both, or neither? At t=200s, 120 seconds after the fault is
gone, what is the offered load at the leaf in each variant?

| Variant | peak amp | amp at t=200s | success % after fault clears | time to recovery |
|---|---|---|---|---|
| naive (exp backoff, no jitter) | | | | |
| + full jitter | | | | |
| + 10% retry budget | | | | |
| retry at edge only | | | | |

**What would mean the experiment is broken, not the prediction wrong:**

- **Amplification ≈ 1.0.** Retries are not firing. Either the injected error
  class is not in your retryable set — toxiproxy's `timeout` and `reset_peer`
  toxics surface as *different* exceptions in every language here — or the
  deadline budget from topic 2 expires before the first retry can be
  attempted, which is a correct system, not a broken one. Check which.
- **Amplification above 27×.** There is a retry layer you forgot. Candidates:
  driver-level reconnect logic, k6's own connection-error behaviour, an
  ingress `proxy_next_upstream`, or connection-level retries in the HTTP
  client. Finding the extra layer *is* the exercise.
- **Everything recovers instantly in every variant.** Offered load is too far
  below capacity for amplification to matter. Rerun at 70-80% of topic 1's
  measured capacity — being in a vulnerable state is a precondition, and it
  is exactly the setup for topic 4.
- **The budget variant shows *zero* retries from the start.** Your bucket is
  refilling from the wrong signal. It should refill on successes, not on
  wall-clock alone; otherwise a service with no traffic never earns tokens.

## Answer before moving on

1. A 10% budget means that during a total outage your retry traffic falls to
   near zero — you have made recovery *slower* for the individual client.
   Argue why that is the right trade, then name a case where it is not.
2. Why is jitter's benefit invisible in a single-client test and dominant
   across a thousand production clients? Your answer should be about
   correlation, not about randomness.
3. You retry a POST that timed out; the server actually processed it. Name
   the three distinct mechanisms that make this safe, and say which layer
   each one belongs in.
4. Your service retries, and the dependency you call also retries. Neither is
   wrong in isolation. Write the two-sentence rule that makes the composition
   correct.

## Next up

[Topic 4 — Metastable failure](../04-metastable-failure/README.md): what
happens when the amplification you just built outlives the fault that started
it. This is the flagship, and it needs topics 1-3 reproducible first.
