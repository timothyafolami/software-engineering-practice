# Layer 6 · Topic 6 — SLIs, SLOs and error budgets: reliability as a number

### The takeaway (read this first)

**The one idea:** an SLO turns "is it reliable enough?" from an argument into
arithmetic, and the mechanism doing the converting is the **error budget** — a
fixed, spendable quantity of failure per window that makes "ship faster" and
"stabilise" the same conversation instead of two opposed ones.

**Why it matters in practice:** without a budget, every reliability discussion is a
status contest, won by whoever is most senior or most recently burned. With one,
"we have six minutes of budget left this month" is a fact, and burn-rate alerting
turns that fact into a page that fires in proportion to how fast you are spending.

**You'll know it landed when:** you can read off a graph how long the current
degradation can continue before you must stop shipping — and you would defend *not*
paging for a 30-second blip to someone angry about it.

## The concept

An **SLI** is a ratio: good events ÷ valid events. Both halves are counts, and that
is the whole design. Counts are additive, so an SLI computed per pod, per region and
per five-minute window can be summed into an SLI for the fleet over a month without
anything going wrong.

The rookie move is defining the latency SLI as a percentile. You cannot average
percentiles (Topic 2), so a percentile SLO cannot be composed — there is no correct
way to combine "p99 was 280ms in eu-west" with "p99 was 310ms in us-east". Define it
instead as the *fraction of requests under a threshold*, which is a count over a
count and composes perfectly:

```
sum(rate(http_server_request_duration_seconds_bucket{le="0.3",...}[5m]))
/
sum(rate(http_server_request_duration_seconds_count{...}[5m]))
```

Note what that expression is doing: the `le="0.3"` bucket counter *is* "number of
requests under 300ms", already summed for you by the histogram. The composability
you need was in the data model the whole time; percentiles are what threw it away.

The **SLO** is a target on that ratio over a window. The **error budget** is what is
left over, `(1 − target) × window`, and it is worth deriving once by hand because
the number is the entire persuasive force of the idea:

```
28 days          = 28 × 24 × 60      = 40,320 minutes
99.9% target     → budget = 0.001 × 40,320 = 40.32 minutes
```

Forty minutes. Not "three nines", which sounds generous — forty minutes, which
sounds like one bad deploy, because it is one bad deploy.

**Burn rate** is spend speed relative to even consumption: burn rate 1 exhausts the
budget exactly at the end of the window, and rate *n* exhausts it in `window / n`.
So rate 14.4 burns the whole 28-day budget in `40,320 / 14.4 = 2,800` minutes, about
1.9 days. And in one hour at rate 14.4 you have spent
`60 × 14.4 / 40,320 = 2.1%` of the month. Every number in the standard table below
comes out of those two lines of arithmetic — none of it is a magic constant, and
being able to re-derive it is how you adapt it to a window that isn't 28 days:

| Long window | Short window | Burn rate | Budget consumed | Action |
|---|---|---|---|---|
| 1h | 5m | 14.4 | 2% | page |
| 6h | 30m | 6 | 5% | page |
| 3d | 6h | 1 | 10% | ticket |

(Check the middle row yourself: `6 × 360 / 40,320 = 5.4%`. The bottom row:
`1 × 4,320 / 40,320 = 10.7%`.)

The **short window is the part people drop**, and it is the part that stops an alert
staying lit for hours after the incident ended. The rule requires both windows to be
breaching: the long one for detection, the short one so that recovery clears the
alert promptly instead of waiting for the long window to roll off.

This is also where "alert on symptoms" (Topic 7) gets its arithmetic. A burn-rate
alert *is* a symptom alert — it fires on the promise being broken, at a speed
proportional to how badly.

## How each language actually gets there

**Python only, and the reason is structural.** Everything in this topic is
arithmetic that the monitoring system performs on counters: recording rules,
`rate()`, a ratio, a multiplier. There is no runtime behaviour anywhere in it — the
same PromQL runs identically whether the counters came from a Go service, a JVM or a
Python one. Python is here purely as the simulator language: it lets you replay
incident shapes against the burn-rate rules in seconds instead of waiting hours for
a 6-hour window to fill, and it is the stack you would write the rules for anyway.

The one runtime-shaped question in this topic is which languages' SDKs give you a
histogram bucket at exactly your SLO threshold. That is answered in Topic 2 (bucket
boundaries) and Topic 5 (semconv metric names), and it bites here: if `le="0.3"` is
not an actual boundary in your histogram, the query above silently returns nothing.

## The experiment

**Part 1 — the burn-rate simulator, standalone.** `python/burn_rate.py` implements
the budget arithmetic and the three multi-window rules directly, then replays three
incident shapes through them at simulated time, printing which alerts fire, when,
and how much budget each shape consumes:

- **A:** total outage, 3 minutes.
- **B:** 8% error rate, 4 hours.
- **C:** latency doubles (p99 300ms → 700ms), 45 minutes, zero errors.

It also replays the naive rule — `p99 > 500ms for 5m` — against all three, so the
page counts sit side by side. Because everything is derived from the counts, you can
check any line of output with the arithmetic above. Shape A, for example, spends
`3 / 40.32 = 7.4%` of a monthly budget in three minutes, and whether that should
page anyone is a design decision you now have the number to argue about.

**Part 2 — the same thing against the real stack.**

1. Define two SLIs for `api` as recording rules: availability (non-5xx ÷ all) and
   latency (fraction under 300ms).
2. Set 99.9% over 28 days. Compute the budget in minutes by hand *first*, then write
   the PromQL that displays remaining budget and check the two agree.
3. Implement the three burn-rate alerts as Prometheus rules, short window included.
4. Inject the three incident shapes with the fault endpoint.
5. Run the naive alert alongside, and compare page counts and detection times.

The point of running both parts is that the simulator tells you what *should*
happen, so when the real stack does something else you know to suspect the
evaluation interval rather than the theory.

## How to run

Part 1, standalone, no Docker:

```
python3 python/burn_rate.py
```

Part 2, from `lab/` — see [`../lab/README.md`](../lab/README.md):

```
docker compose exec api curl -XPOST localhost:8000/_fault \
  -H 'content-type: application/json' -d '{"mode":"outage","seconds":180}'

docker compose exec api curl -XPOST localhost:8000/_fault \
  -H 'content-type: application/json' -d '{"mode":"error_rate","ratio":0.08,"seconds":14400}'

curl -s localhost:9090/api/v1/rules | jq '.data.groups[].rules[] | {name, state}'
```

The `content-type` header is not tidiness: `curl -d` sends
`application/x-www-form-urlencoded`, and the endpoint takes a JSON body, so without
it every fault injection returns 422 and nothing is injected.

That `/api/v1/rules` call returns an empty group list until you give this Prometheus
somewhere to read rules from. The one in `grafana/otel-lgtm` ships with no
`rule_files:` and no `scrape_configs:`, so writing the rules is only half of step 3;
mounting them is the other half. `../lab/docker-compose.yml` has the two mounts
commented on the `lgtm` service.

Shape B runs for four hours by design — a 6-hour-window rule cannot be tested in ten
minutes, and shortening the window to make the test convenient changes the thing
being tested. Start it and go do Topic 7's reading.

## Predict, then record

Per shape, predict which burn-rate alerts fire, how long after onset, and how much
budget each consumes. Do the budget column with arithmetic before you run anything —
it is the one column you can know in advance, and being wrong about it means you
have the model wrong, not the rules.

| Shape | Alerts fired | Time to first page | Budget consumed | Naive alert fired? |
|---|---|---|---|---|
| A: 3-min total outage | | | | |
| B: 8% errors, 4h | | | | |
| C: latency 2x, 45m, no errors | | | | |

| Budget bookkeeping | Value |
|---|---|
| Budget for 99.9% / 28 days (minutes) | |
| Budget remaining after all three shapes | |
| Simulator's answer vs the stack's answer | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If nothing fires for shape A, that may be *correct*. Three minutes of total outage
  is about 7% of a 28-day budget at 99.9%, and whether that deserves a page is a
  design decision, not a bug. Check the arithmetic before touching the rule.
- If nothing fires for B, check that your evaluation interval and `for:` duration
  are short enough relative to the window. A 1-hour window cannot detect anything in
  its first few evaluations, and that is by design.
- If the latency SLI never degrades in C, confirm `le="0.3"` is an actual boundary
  in your histogram. If it is not, PromQL returns nothing and shows you a flat line
  rather than an error.
- If remaining budget goes *up* during an incident, your recording rule is using a
  ratio over a rolling window where you meant a cumulative count over the SLO
  window. Both are legitimate; only one is a budget.
- If the simulator and the stack disagree on budget consumed for the same shape, the
  fault injector's actual error ratio is worth checking before the rules are.

## Answer before moving on

1. You have a 99.9% SLO, 90% of the budget burned, 10 days left in the window, and
   someone wants to ship a risky migration. What do you say, and what makes the
   answer non-negotiable rather than an opinion?
2. Why is a threshold-ratio latency SLI composable when a percentile SLI is not?
   Answer in terms of what is being summed.
3. Give a case where 100% of the error budget going *unspent* is a problem, and say
   what you would change in response.
4. Re-derive the burn-rate table for a 7-day window at 99.5%. Which of the three
   rows still makes sense, and which one now pages for something trivial?

## Next up

[Topic 7 — alert on symptoms, and write the postmortem that changes the system](../07-symptoms-and-postmortems/README.md):
you have rules that fire proportionally. Next is proving that the ones you already
had were mostly firing for nobody.
