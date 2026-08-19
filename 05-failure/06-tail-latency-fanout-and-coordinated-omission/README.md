# Layer 5 · Topic 6 — Tail latency, fan-out, and coordinated omission

### The takeaway (read this first)

**The one idea:** p50 is a statement about the requests that had no problem,
and in a fan-out of *n* the chance that at least one leg lands in the p99 is
`1 − 0.99^n` — about 63% at n=100 — so one dependency's rare tail is the
parent's common case.

**Why it matters in practice:** this is why "all our services are at 20ms
p50" and "users say it's slow" are simultaneously true, and why a load test
reporting a healthy p99 tells you nothing at all if it ran closed-loop.

**You'll know it landed when:** you never quote a mean latency again, you
notice immediately when someone averages percentiles across instances
(arithmetically meaningless), and you check a load generator's *model* before
you believe any number it prints.

## The concept

**Percentiles compound across fan-out.** With `n` independent parallel calls
each having tail probability `p`, the chance that the slowest leg is in the
tail is `1 − (1−p)^n`. For p = 1%:

| n | `1 − 0.99^n` |
|---|---|
| 1 | 1.0% |
| 5 | 4.9% |
| 10 | 9.6% |
| 20 | 18.2% |
| 50 | 39.5% |
| 100 | 63.4% |

That table is arithmetic, not measurement — recompute it for your own `p`
before you argue with anyone about it. Dean and Barroso's *The Tail at Scale*
(CACM 2013) is still the reference, and its remedies still hold: **hedged
requests** (send a second copy once you have waited past p95, take the first
answer, cancel the loser), tied requests, micro-partitioning, and selective
replication.

Hedging has a trap that drops you straight back into topic 3: an
unconditional hedge doubles your load. Hedge only past a high percentile, and
put the hedges under a budget — "at most 5% of requests may hedge" — or you
have built a retry storm with better branding.

**Percentiles do not average.** The p99 of ten instances is not the mean of
their ten p99s. If your metrics pipeline computes per-instance percentiles and
then averages them, your p99 is fiction — and it is fiction in the optimistic
direction, because averaging pulls the one bad instance toward the nine good
ones. Export **histograms**, merge the histograms, *then* compute the
quantile. This is not a nitpick; it is the difference between a dashboard that
can detect a single sick pod and one that cannot.

**Coordinated omission invalidates most load tests.** A closed-loop generator
— N virtual users, each waiting for a response before sending the next —
*stops sending load when the server slows down*. So the exact moments you most
need measured are the ones it declines to sample, and the p99 it reports
describes a system that was never stressed. Real traffic is **open**: users
arrive at a rate that has nothing to do with how your server is feeling.
Brooker's 2025 ICPE keynote makes this the central critique of how our field
benchmarks: happy case, closed model, then surprise on bad days.

In k6 this is one configuration line — `constant-arrival-rate` and
`ramping-arrival-rate`, never `ramping-vus`. Watch for the secondary tell as
well: if k6 warns that it cannot allocate enough VUs to sustain the target
rate, your generator has fallen behind and is *itself* now coordinating
omission, arrival-rate executor or not.

## How each language actually gets there

**Four languages here, not six: Python, Node.js, Go and Rust.** The mechanism
in this topic is statistical and architectural rather than runtime-specific —
what actually differs per language is only how you cancel the losing leg of a
hedge, and these four span the full range of answers (explicit cancellation,
signal propagation, automatic context cancellation, and cancel-by-drop). A
C++ and a Java version would restate one of those four with more ceremony.

**Python** waits for the slowest leg by construction: `asyncio.gather` is
exactly "all of them", so its e2e latency *is* the max of the legs, which is
what makes the fan-out arithmetic above so easy to demonstrate here. The
hedging primitive is `asyncio.wait(..., return_when=FIRST_COMPLETED)` followed
by explicit `.cancel()` on the losers — and forgetting that cancellation is
*the* common bug, because an uncancelled hedge still holds its pool
connection for the full duration, which means your hedge has doubled load in
exactly the way you were trying to avoid. In 3.11+, `TaskGroup` gives you
cancellation of siblings for free when one raises, which is the ergonomic
version of the same discipline.

**Node.js** hedges with `Promise.race` plus an `AbortController`, and the
cancellation story is honest about its limits: aborting a `fetch` stops
*your* side, and the server may keep going (topic 2 again, one layer down).
Node's second contribution to this topic is that its fan-out is genuinely
concurrent while its *response assembly* is not — the JSON serialisation of
all K responses happens on the one thread, so at large K the gateway itself
becomes the tail, and you will see it as a rise in event loop lag rather than
in any backend's numbers.

**Go** has the cleanest version: `errgroup.Group` for the fan-out, a `select`
over the result channel and `time.After` for the hedge trigger, and
`context.CancelFunc` for the loser — where cancellation is genuinely
automatic and genuinely propagates over the wire to a Go server on the other
end. This is the same advantage as topic 2 and it compounds here: hedging is
only cheap if the cancelled copy actually stops, and Go is the only runtime in
this lab where that is the default rather than the careful path.

**Rust** cancels by dropping. `tokio::select!` on two futures drops the loser
the instant the winner completes, and dropping a future stops polling it — so
the hedge cleanup that Python asks you to remember and Node asks you to wire
up is a consequence of the type system's ownership rules. The cost is the
mirror image: a future that owns a connection releases it on drop *whether or
not you thought about it*, so "cancellation safety" becomes something you must
reason about explicitly for anything with partial state (a half-written
buffer, a half-consumed stream).

## The experiment

A gateway fans out to K identical backends and waits for all of them. Each
backend's latency is drawn from a distribution where p99 is 20× p50 —
log-normal, and separately bimodal with a 1% slow mode, because those two
behave differently and the difference is the point.

1. Measure end-to-end p50 and p99 for **K = 1, 2, 5, 10, 20, 50**.
2. Compare the measured tail probability against the predicted
   `1 − (1−p)^K` from the table above.
3. Add hedging at the *measured* backend p95: take the first response, cancel
   the other, cap hedges at 5% via a token bucket. Re-measure at each K, and
   measure the **load increase at the backends** — hedging is not free and
   the point is to quantify what it cost.
4. **The coordinated-omission demo:** run K=10 twice at the same nominal
   load, once with `ramping-arrival-rate` and once with `ramping-vus`, and
   report both p99s side by side. This is the single most useful chart in
   this layer for arguing with people.

Output shape:

```
K=<n>  e2e_p50=<ms>  e2e_p99=<ms>  predicted_tail=<pct>  measured_tail=<pct>
hedge=on  backend_load=<rps>  hedge_rate=<pct of requests>
```

## How to run

> **Expect roughly three minutes per arm — that is correct, not a hang.**
> Measured on an M1 (macOS 27, 2026-08-19): Python 198s, Go 195s, Node 191s.
> Rust is the same shape and was not separately timed. All four run the same
> sweep, and the duration is the experiment, not overhead: separating a p99
> from a p50 needs enough samples that a short run would show noise instead of
> the effect. Do not shorten the sweep to make it finish faster — a version
> that returns in twenty seconds is measuring your scheduler, not tail latency.
> Running all four back to back takes about thirteen minutes.

**The harness is built and was executed here.** `lab/docker-compose.yml`,
`lab/app/`, `lab/scripts/*.js` and
`lab/tools/*.py` exist (specified in
[`../lab/README.md`](../lab/README.md)) and the commands below were run
against them. You do **not** need to install `k6`: it runs from the
`grafana/k6` image, which is what `docker compose run --rm k6` starts. What
you do need is Docker running (`docker info`) and host ports 8000-8003 free —
if something else on your machine holds 8000, `up` fails with `port is
already allocated`. From `05-failure/lab/`:

```
cd ../lab
docker compose --profile fanout up -d --build --scale backend=10
for K in 1 2 5 10 20 50; do
  docker compose run --rm k6 run /scripts/06_fanout.js -e K=$K -e HEDGE=off \
    --out csv=/out/06_fanout_k${K}_hedgeoff.csv
done
docker compose run --rm k6 run /scripts/06_fanout.js -e K=10 -e HEDGE=on \
  --out csv=/out/06_fanout_k10_hedgeon.csv
docker compose run --rm k6 run /scripts/06_closed_loop.js -e K=10 \
  --out csv=/out/06_closed_loop_k10.csv
python3 tools/plot_tail.py out/
```

Watch the two guards the fan-out script prints at the end. If it says the
gateway completed materially less than was offered, that run measured the
gateway's own queue rather than the tail arithmetic — lower `-e RATE=` until
offered and completed agree, then sweep `K` again. Backend calls are
`RATE x K`, so a rate that is comfortable at K=1 is fifty times the outbound
work at K=50, and the default is not right for every machine. The hedge
figures are printed against two denominators for the same reason:
`HEDGE_BUDGET_PCT` caps hedges as a fraction of **backend calls**, and the
per-request figure is about K times larger without anything being wrong.

`--scale backend=10` is what gives the gateway ten distinct backends to
resolve; with fewer replicas than K the addresses repeat, which changes the
per-backend load but not the tail arithmetic. Run the whole sweep a second
time with `-e DIST=bimodal` — the continuous tail and the 1%-slow-mode tail
respond to hedging differently, and that difference is the point.

The plotter runs today against the synthetic fixtures that ship with the
harness — a model, not a measurement:

```
cd ../lab && python3 tools/make_fixtures.py
python3 tools/plot_tail.py out/fixtures/
```

**All four standalone versions are written and were run here.** Each holds the
gateway, the K backends and both load models in one process, which reproduces
every effect above except real network variance — and needs no Docker, no k6
and no network. Each takes no arguments and runs for roughly three minutes:

```
python3 python/fanout.py
node nodejs/fanout.js
cd golang && go run fanout.go
cd rust/fanout && cargo run --release
```

All four print the same three phases against the same constants, so the tables
line up: a calibration block measuring one backend directly, phase A's
`predicted` vs `measured` tail sweep over K, phase B's hedging at the
*measured* p95 under the 5% bucket, and phase C's open-model and closed-loop
histograms of the same server. Phase B's `svc_ms/req` column is the one to read
first — it is the backend service time actually consumed per request, and it is
what separates the row that cancels the losing copy from the row that does not.
The per-language difference lives in that pair of rows: Go cancels a
`context.Context`, Rust drops the future, Node fires an `AbortController` the
backend is free to ignore, and Python calls `.cancel()` or forgets to.

## Predict, then record

Before running: given a backend p99 of 200ms and a p50 of 10ms, will e2e p99
at K=10 be close to 200ms or well above it — and what is the reasoning that
gets you there? How much extra backend load does 5%-budgeted hedging cost?
How large is the gap between the open-model and closed-model p99 at the same
nominal rate?

The `predicted tail prob` column below is derived from `1 − 0.99^K`, not
measured — it is your prediction's arithmetic, filled in so you cannot fudge
it after the fact.

| K | e2e p50 | e2e p99 | predicted tail prob | measured tail prob |
|---|---|---|---|---|
| 1 | | | 1.0% | |
| 5 | | | 4.9% | |
| 10 | | | 9.6% | |
| 20 | | | 18.2% | |
| 50 | | | 39.5% | |

| Variant (K=10) | p50 | p99 | backend load | notes |
|---|---|---|---|---|
| no hedge, open model | | | | |
| hedge @p95, 5% budget | | | | |
| no hedge, **closed model (VUs)** | | | | coordinated omission |

**What would mean the experiment is broken, not the prediction wrong:**

- **Measured tail probability well *below* prediction.** The formula assumes
  independence. If your backends share a bottleneck — one host, one CPU
  allocation, one database — their slow moments correlate, and correlated
  tails produce *fewer* distinct bad requests than independent ones. Real,
  worth understanding, and it cuts the other way in production, where
  correlated slowness means everything is slow at once. Isolate the backends
  with separate CPU limits and rerun.
- **e2e p99 does not grow with K at all.** Your fan-out is running
  sequentially, or the gateway is itself the bottleneck. Check that the
  gateway's own concurrency limit is not binding before you conclude anything
  about the backends.
- **Hedging makes p99 worse.** Either the hedge delay is too short — you are
  hedging typical requests, so set it from the *measured* p95 — or the budget
  is not actually enforced and you have doubled load.
- **Open and closed models give the same p99.** You never saturated
  anything. Push past 100% of capacity: below the knee the two models agree,
  which is precisely why closed-loop tests pass and then production does not.

## Answer before moving on

1. Why is averaging p99s across instances arithmetically invalid, and what
   exactly must your metrics pipeline export instead? Name the data
   structure.
2. A dependency improves p99 from 200ms to 100ms but worsens p50 from 10ms
   to 30ms. You fan out to 20 of them. Better or worse for users? Show the
   reasoning, not just the answer.
3. Hedging is a retry sent before the first attempt has failed, so everything
   in topic 3 applies to it. Write the hedging policy that satisfies all four
   of topic 3's conditions.
4. Your load test reports p99 = 45ms. What three questions do you ask before
   believing it?

## Sources

- Dean & Barroso, *The Tail at Scale*, CACM 2013 — the reference for
  fan-out tails and hedging
- Marc Brooker, *Good Performance for Bad Days* (ICPE 2025 keynote) —
  https://brooker.co.za/blog/2025/05/20/icpe/
- Gil Tene's original coordinated-omission argument, and the HdrHistogram
  documentation, for why histograms rather than percentiles must be exported

## Next up

[Topic 7 — Idempotency, and degradation decided in advance](../07-idempotency-and-degradation-decided-in-advance/README.md):
the precondition that makes every technique in topics 2-6 legal.
