# Layer 6 · Topic 7 — Alert on symptoms, and write the postmortem that changes the system

### The takeaway (read this first)

**The one idea:** an alert should fire when a user is having a bad time, not when a
machine is having an unusual time — because the set of causes is unbounded and always
will be, while the set of symptoms is small, stable, and exactly what you promised.

**Why it matters in practice:** cause-based alerts are how you get 200 rules, an
on-call rotation that ignores them, and a real incident that fires none of them.
Symptom-based alerting is the only reason a page means anything.

**You'll know it landed when:** you can look at any alert rule and say which
user-visible promise it defends — and you delete the ones that cannot answer.

## The concept

**Causes are unbounded.** CPU, memory, replica lag, pool depth, queue depth, GC
pause, disk, expiring certs, error-log rate, thread count, file descriptors, DNS.
Any of them can be true while users are perfectly happy, and users can be miserable
with every one of them green. You cannot enumerate the set, so you cannot cover it,
so a strategy built on covering it fails in a specific way: it produces many rules,
each individually reasonable, that collectively train people to ignore pages.

**Symptoms are few.** Users get errors. Users wait too long. Users' data is stale or
wrong. Users cannot get in. That list is short because it is a restatement of what
you promised, and what you promised is finite.

So: **symptoms page, causes inform.** Cause metrics belong on the dashboard you open
*after* the page, because they are how you find the layer that is lying. Topic 6's
burn-rate alerts are already symptom alerts with arithmetic attached — this topic is
about what to do with everything else.

A cause earns a page only when it satisfies three properties at once: it predicts
imminent user-visible harm, with enough lead time that acting on it changes the
outcome, and with a false-positive rate low enough that people keep trusting it.
"Disk full in 4 hours" qualifies on all three. "CPU above 80%" fails the first and
the third. Certificate expiry in 14 days qualifies. Replica lag qualifies only if
reads actually route to that replica — which is scenario Y below, and the reason it
is in the list.

**The postmortem closes the loop, and "blameless" is not a manners rule.** It is an
information-gathering technique. People describe what they actually did — including
the part where they skipped the checklist — only when doing so is safe, and you
cannot fix a system you have an inaccurate account of. Blame does not make people
more careful; it makes accounts less accurate, which makes the next incident more
likely.

The output that matters is the **detection gap**: wall-clock time between first
user-visible harm and first human knowing. That single number is what this entire
layer exists to reduce, and after six topics you can now *measure* it from telemetry
instead of estimating it from memory:

```
T0  first user-visible harm      earliest trace exceeding the SLO threshold
T1  first metric deviation       earliest point the SLI left its band
T2  first alert                  the rule's activeAt
T3  first human action           deploy log, chat, runbook step

detection gap = T2 − T0        response gap = T3 − T2
```

`T1` sitting between `T0` and `T2` tells you where the fix is. If `T1 ≈ T0`, your
telemetry saw it immediately and your *rules* are slow. If `T1` is late, no rule
change helps and the instrumentation is the problem.

## How each language actually gets there

**Python only, and the reason is that there is no language in this topic.** Alert
rules are PromQL evaluated by Prometheus; the postmortem timeline is four timestamps
pulled from a trace store, a recording rule, the rules API and a deploy log. None of
it executes in your service, and re-implementing an alert-rule evaluator in six
languages would teach nothing about any of them. Python appears only as the
scenario driver and as the script that reconstructs the timeline from the APIs.

The one place a runtime does show through is worth naming, because it decides
whether `T0` is findable at all: the trace that establishes `T0` has to have been
sampled. Head sampling is blind to latency by construction (Topic 2), so a
head-sampled service can be missing the very trace that proves when harm started.
Tail sampling, or a 100% sampler on this service, is a prerequisite for Part 2 —
which is itself a finding about how sampling policy constrains postmortems.

## The experiment

**Part 1 — page-worthiness.** Write eight rules: four cause-based (CPU > 80%, pool
> 90% utilized, replica lag > 10s, error-log rate > 10/min) and four symptom-based
(Topic 6's three burn-rate alerts plus one availability rule). Then run four
scenarios:

- **W:** a batch job pegs CPU for 20 minutes; request latency stays flat.
- **X:** pool exhaustion driven by the `pricing` tail — CPU low, memory low, DB
  healthy, everything green, p99 at 8s.
- **Y:** a replica falls 60s behind, but no reads route to it.
- **Z:** a bad deploy — 3% of requests return 500 for 25 minutes.

Record per scenario which rules fired and whether users were actually harmed, then
compute a false-page rate per family. Two of these scenarios have no user impact at
all; the interesting result is how many rules fire anyway.

**Part 2 — the postmortem that changes the system.** Take scenario X and write a
real one, with the timeline reconstructed **entirely from telemetry**, not from
memory: `T0` from the earliest trace exceeding the SLO threshold, `T1` from the
earliest point the SLI recording rule left its band, `T2` from the alert's
`activeAt`, `T3` from whatever recorded your first action.

Then write contributing factors — plural, and none of them a person — what
specifically made detection slow, and **exactly one action item that changes the
system**. The acceptance criterion is the whole exercise: re-run scenario X
unchanged and demonstrate a smaller `T2 − T0`. If you cannot state the action item
as a re-runnable test, it is a wish, and wishes are what postmortem action-item
backlogs are made of.

## How to run

Both parts have a standalone version that needs no stack. They are the same
exercise with the scenarios simulated instead of injected, so you can see the
shape of the answer before you spend an afternoon producing it:

```
python3 python/page_worthiness.py
python3 python/detection_gap.py
```

`page_worthiness.py` runs the eight rules against all four scenarios and
computes the false-page rate per family from derived user harm — no rule's
verdict is asserted anywhere. `detection_gap.py` replays scenario X second by
second, reconstructs T0–T3 from four independent sources, applies exactly one
action item, and re-runs the identical fault to show the new `T2 − T0`. Both
mark every number that is a modelled input rather than a measurement — the two
timestamps involving humans, and nothing else.

Then the real thing, from `lab/` — see [`../lab/README.md`](../lab/README.md):

```
docker compose exec api curl -XPOST localhost:8000/_fault \
  -H 'content-type: application/json' -d '{"mode":"pricing_tail"}'

curl -s localhost:9090/api/v1/alerts | jq '.data.alerts[] | {labels, activeAt}'
curl -s localhost:9090/api/v1/rules  | jq '.data.groups[].rules[] | {name, state}'

# T0: the earliest trace over the SLO threshold, from Tempo
# T1: the SLI recording rule, queried over the incident window
# then apply the fix and re-run the identical fault, recomputing T2 − T0
```

Scenarios W, Y and Z are driven the same way through `/_fault` and by the load
scripts; scenario W wants a CPU-bound container alongside the ramp so that CPU moves
without latency following it.

## Predict, then record

Predict which scenario produces the most pages, which produces the most *useless*
pages, and which produces none at all while harming users.

| Scenario | Cause rules fired | Symptom rules fired | Users harmed? | Verdict |
|---|---|---|---|---|
| W: CPU pegged, latency flat | | | | |
| X: pool exhaustion | | | | |
| Y: replica lag, no reads | | | | |
| Z: 3% 500s, 25 min | | | | |

| Rule family | Pages fired | Pages with real user harm | False-page rate |
|---|---|---|---|
| Cause-based (4 rules) | | | |
| Symptom-based (4 rules) | | | |

| Postmortem timeline (X) | Timestamp | Source |
|---|---|---|
| T0 first harmed request | | trace |
| T1 first metric deviation | | SLI recording rule |
| T2 first alert | | `activeAt` |
| T3 first human action | | deploy log / chat |
| Detection gap (T2 − T0) | | |
| Response gap (T3 − T2) | | |
| Detection gap after the fix | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- If the detection gap comes out zero or negative, you sourced `T0` from the alert
  rather than from the trace. The entire point is that the two come from independent
  signals; if one is derived from the other the number is arithmetic, not evidence.
- If you cannot find a `T0` trace at all, check the sampler. A head-sampled service
  may simply not have kept the first slow request, which is a real finding about
  your sampling policy and not a broken experiment.
- If no rule fires in scenario X, that is very likely *correct* for the cause-based
  family — that is the scenario's whole purpose — and a bug in your burn-rate rules.
  Check them against Topic 6 before concluding symptom alerting does not work.
- If cause rules fire in every scenario including W and Y, your thresholds are tuned
  to your laptop's idle load rather than to the service. Re-baseline against a quiet
  run first.
- If the re-run after the fix shows a *larger* detection gap, check that the two
  runs used the same fault parameters. A `pricing_tail` at a different ratio is a
  different incident.

## Answer before moving on

1. Name a cause-based alert that genuinely deserves to page, and state the lead-time
   and false-positive properties that earn it the right.
2. Your detection gap is 11 minutes and the incident lasted 14. What is the
   highest-leverage change — better alerting, better dashboards, or a different SLO?
   Justify it with those two numbers.
3. Scenario Y fires a cause rule and harms nobody. Describe the smallest change to
   the *architecture*, not to the rule, that would make that same rule legitimately
   page-worthy.
4. Take your real production latency problem. Write the postmortem you would write
   *if it were an incident today*: what is the symptom, what would `T0` have been,
   and which of the four scenarios does it most resemble?

## Next up

Layer 7 — security at mechanism level. But this layer has an obligation the others
do not. Point the lab's collector at your actual service for one afternoon, run
[Topic 2](../02-real-p99/README.md)'s procedure against real traffic, and find out
which of the five shapes your p99 actually is. That is the whole return on this
layer, and it is available to you this week.
