# Layer 9 · Topic 4 — The postmortem (flagship, and you have a live one)

### The takeaway (read this first)

**The one idea:** the timeline is not the analysis. The analysis is the answer to
*why was this hard to see*, and it lives in the gap between when the system
started misbehaving and when a human understood what was happening — a gap
usually much larger than the fix, and the only part of the incident under your
direct control next time.

**Why it matters in practice:** you have users experiencing your service as slow,
right now. That is not a bug ticket. It is an ongoing incident with no start
timestamp, which is the worst kind, because "when did this start" is a question
you probably cannot answer — and *that inability is the most important finding
you will produce this month.*

**You'll know it landed when:** your postmortem contains at least one action item
that makes a whole *class* of future incidents visible faster, and zero action
items whose subject is a person.

## The concept

### Three clocks, and organisations reliably optimise the wrong one

- **Time to detect** — misbehaviour starts → something or someone notices. For
  slow degradation this is frequently measured in weeks, and the detector is a
  customer.
- **Time to diagnose** — noticed → understood. Usually the largest of the three,
  always the least instrumented, and the one where writing pays off most, because
  the fix for a slow diagnosis is almost always a *document* — a runbook, a
  dashboard with the right question on it, an architecture note — rather than a
  code change.
- **Time to mitigate** — understood → users are okay again. The only one anybody
  measures, because it is the only one with an obvious start and end.

Write all three with real clock times. The shape of the three numbers tells you
where to spend: a long detect and a short mitigate is a monitoring problem
wearing an incident costume.

### Why "root cause" is the wrong frame, and what replaced it

Google's [postmortem culture chapter](https://sre.google/sre-book/postmortem-culture/)
is still the right first read for *culture* — blameless framing, explicit
triggers for when a postmortem is mandatory, the point that any stakeholder may
request one. But its [example postmortem](https://sre.google/sre-book/example-postmortem/)
is a decade old and shows it: it leads with root cause and lands in an action-item
table.

The resilience-engineering line of work argues for a different emphasis —
**multiple contributing factors that interact, what made the system hard to
understand under pressure, and a deliberate gap between the review meeting and
the action-item commitment**, so the meeting produces understanding rather than a
to-do list. The practical reading is John Allspaw, Richard Cook and David Woods'
*Debriefing Facilitation Guide* and the Learning From Incidents community that
grew around it. The version used below is deliberately mechanical: **separate
contributing factors from actions, and write the detection-gap section before you
write any action at all.**

### The best public teaching case in years

The Cloudflare outage of
[18 November 2025](https://blog.cloudflare.com/18-november-2025-outage/) is worth
reading in full. The mechanism is simple once you know it: a ClickHouse
permissions change caused a Bot Management feature file to include duplicate rows
and roughly double in size; the file exceeded a hard-coded preallocation limit;
the parsing code handled the resulting error the way `.unwrap()` handles an
`Err`, panicking the thread and returning 5xx.

What makes the writeup exceptional is not the mechanism but its honesty about
**why diagnosis took so long**: the bad file was regenerated every few minutes,
so the system alternated between healthy and broken, which looks far more like an
attack than an internal bug — and their independently hosted status page went
down at the same time by coincidence, convincing part of the team that someone
was targeting multiple systems at once. That is a *detection and diagnosis
narrative*, and it is the section almost every internal postmortem omits.

Then read the follow-up,
[*Code Orange: Fail Small*](https://blog.cloudflare.com/fail-small-resilience-plan/),
for the shape of the commitments: staged rollout for *configuration* the way they
already do for code, interfaces reviewed to degrade rather than cascade, circular
dependencies removed from break-glass access. Every one changes a system, not a
person. That is the standard.

Two contrasts worth having in your head. The
[AWS us-east-1 event of 19–20 October 2025](https://aws.amazon.com/message/101925)
— a latent race condition in DynamoDB's automated DNS management, cascading
across dozens of services for most of a day — is the case study in *cascading
dependency*, and a lesson in sourcing: go to the primary writeup, not the twenty
summaries that paraphrase each other. And GitHub's monthly availability reports
on the [GitHub blog](https://github.blog/news-insights/company-news/) are the
model for *cadence*: routine, unglamorous, published whether or not the month was
embarrassing. For volume,
[danluu/post-mortems](https://github.com/danluu/post-mortems) remains the best
curated collection.

### Two habits that separate an analysis from a report

**Kill counterfactuals.** "If only the engineer had checked the file size"
describes a world that did not exist. It feels like analysis and produces
nothing, because the next engineer will also not check the thing nobody told them
to check. Replace every "if only X had" with "X was not visible because ___" and
watch the sentence turn into an action item by itself.

**Contributing factors, plural, and separated from actions.** Write every factor
before you allow yourself to write a single remediation. Committing to actions
while you are still discovering factors truncates the discovery — you stop
looking at the first thing that has an obvious fix.

### What the machines took, and what they left

Incident tooling now assembles a postmortem draft from the alert stream, the
incident channel, custom fields, and the deploy log, section by section — see
[incident.io's documentation of AI-assisted postmortem
writing](https://docs.incident.io/post-incident/postmortem-ai) for one vendor's
description of the mechanism. Vendors describe the remaining human work as cause
analysis and remediation, and that framing is roughly right.

Read it as a warning about where value moved. The timeline table, the impact
summary, the participant list — machine work now. **Contributing-factor analysis,
the detection-gap story, and choosing actions that change the system are the parts
still worth a human**, and they are exactly the parts most postmortems were
already thin on. Write the timeline by hand *once*, so you know what the machine
is assembling and what it cannot see, then get out of that business.

## How each language actually gets there

**This topic uses one: Python.** The incident is in your FastAPI/Postgres/Docker
stack, and a postmortem's mechanism — reconstructing what was knowable, when — is
not a runtime property.

The runtime does show up in one place, and it produces the most valuable sentence
in the document when it appears: a contributing factor is frequently a *property
of the concurrency model* that nobody chose deliberately. "A synchronous outbound
call inside an `async def` occupies the single event-loop thread for its full
duration, so one slow downstream degrades every concurrent request on that
worker" is a contributing factor with a mechanism, and it is a Layer 1 fact. The
same code in Go would have parked the goroutine and freed the thread. Naming the
concurrency model as a factor is what turns "the pricing service was slow" into
"we had no isolation between a slow dependency and unrelated requests" — a factor
that generates a real action.

## The experiment

The artifact is a postmortem of your own live latency incident. The measurement
is not a benchmark: it is **the count of cells you have to mark `unknown`**.

### The artifact

Copy this into `templates/postmortem.md`:

```markdown
# Postmortem: <user-visible symptom, not the cause>
Status: Draft | Reviewed · Author · Date · Reviewers:

## Summary
Five sentences max. What users experienced, over what window, how big, and what
changed to end it. Written for someone who will read only this.

## Impact
In user units first: how many requests over what threshold, how many users, how
many failed actions. Then money or SLO burn if you have it. Every number gets its
source: dashboard query, log range, or "unknown — see Detection Gaps".

## Timeline (UTC, one row per observable event)
| Time | What happened | How we knew (or didn't) |

Start the timeline at the change that made it possible, not at the alert.

## Contributing factors
Numbered, plural. Include the ones that are about the system's shape: a default
that was never chosen, a dependency nobody knew was synchronous, a limit nobody
had read. No factor may name a person.

## Detection gaps — why this was hard to see
Time to detect / time to diagnose / time to mitigate, with the actual clock
times. Then: what were we looking at that told us the wrong thing? What did we
suspect first, and why was that reasonable? What signal would have collapsed the
diagnosis time?

## What went right
A real section, not a courtesy. Anything that limited the blast radius is a
control you must not accidentally remove later.

## Actions
| # | Action | Changes what | Owner | Due |

Every action must change the system: a default, a limit, a signal, an interface,
a rollout mechanism. "Be more careful", "add it to the review checklist" and
"document this" are person-changes wearing a system costume. At most one action
may be a doc, and only if the doc is a runbook that shortens diagnosis.

## What we still don't know
```

**Your `unknown` count is the primary output of the first pass.** Fill in as much
as you honestly can from what exists today — deploy history, `pg_stat_statements`,
whatever logging you have, support tickets, the dates users started complaining.
Then count the cells where you had to write `unknown`. Each one is a detection
gap and each detection gap becomes an action. This is the honest version of
"instrument your service": a list derived from real questions you could not
answer, rather than a dashboard built from whatever was easy to graph.

Where a gap blocks you, go get the evidence with the stack from Layer 6
([`06-observability/lab/`](../../06-observability/lab/README.md)) — its planted
defects are a catalogue of the usual suspects in a Python/Postgres/Docker latency
incident.

### Worked example

**A timeline row.** The third column is the one that makes it a record rather
than a story; a row that cannot fill it is telling you something.

> | `<time>` | Deploy `<sha>` added the synchronous pricing call to the checkout handler | Deploy log; **nobody noticed at the time — no latency alert existed** |
> | `<time>` | First support ticket describing checkout as "hanging" | Zendesk `<query>` |
> | unknown | Latency actually began degrading | **unknown — retention on request-duration metrics is `<n>` days; see Detection Gaps** |

**A contributing factor.** Counterfactual, and therefore inert:

> If only we had load-tested the checkout path before shipping the pricing
> integration, we would have caught this.

The same observation as a property of the system:

> The pricing call was added inside an existing `async def` handler. Nothing in
> review, CI, or runtime made the difference between a blocking and a
> non-blocking client visible — the two call sites look identical in a diff, and
> we have no check that flags synchronous IO inside async handlers. *(Action:
> add a lint or runtime warning for blocking calls in async context; that is a
> class of incident, not this incident.)*

**An action, before and after.** Person-shaped:

> Engineers should be careful to use async clients for outbound calls.

System-shaped:

> Default the service's HTTP client to the async client at the module level, so
> the blocking one has to be imported explicitly and shows up in review as an
> unusual import.

### Rubric

1. Can a reader who was not there state, from the summary alone, what users
   experienced?
2. Does every number cite where it came from?
3. Does the timeline start before the alert — at the change that made the
   incident possible?
4. Are there at least three contributing factors, and does at least one describe
   a *default nobody chose*?
5. Does the detection-gap section explain what you believed first and why that
   was reasonable at the time? (If your first hypothesis looks stupid in the
   writeup, you have written it with hindsight and removed the actual lesson.)
6. Zero counterfactuals. Search the document for "should have" and "if only".
7. Every action changes a system. Cover the person-shaped ones with your hand and
   check the document still has teeth.
8. No sentence names an individual as a cause.

## How to run

[`worked-example.md`](worked-example.md) is this postmortem written out for the
checkout latency incident — every number a placeholder, every `unknown` real, and
three of its seven actions derived from those `unknown` cells. Read it, then write
your own; the two tools below are in Python because the incident is.

```bash
cd 09-writing
cp templates/postmortem.md artifacts/04-postmortem/latency-incident.md

# evidence available today, before any new instrumentation
git log --since='<date>' --oneline -- <path to the service>   # what changed, when
docker compose logs --since=<duration> api | head -50          # what it said
# needs the pg_stat_statements extension already loaded: it must be in
# shared_preload_libraries (a server restart) AND created in the database.
# On a stock Postgres this errors with: relation "pg_stat_statements" does not exist
psql -c "select calls, mean_exec_time, max_exec_time, query
         from pg_stat_statements order by mean_exec_time desc limit 20;"
```

**The timeline skeleton, from the logs themselves.**
[`python/timeline_from_logs.py`](python/timeline_from_logs.py) groups log lines
into distinct message shapes, reports when each was first and last seen, and
emits markdown rows for the `## Timeline` table. It reproduces timestamps
verbatim, refuses to guess a start time, and leaves the third column for you —
because only you know how you knew. Run it with no input to see the parser work
on clearly-labelled synthetic lines:

```bash
cd 09-writing/04-the-postmortem
docker compose logs --no-color --since 72h api | python3 python/timeline_from_logs.py
python3 python/timeline_from_logs.py /path/to/app.log /path/to/postgres.log
python3 python/timeline_from_logs.py            # synthetic demo of the parser
```

Read its COVERAGE block before the table. If the change that made the incident
possible is older than the earliest line it saw, your first timeline row is
`unknown` with a retention note — which is a finding, not a hole.

**Rubric items 2, 6, 7 and 8 are mechanical — run them, do not eyeball them.**
[`python/postmortem_check.py`](python/postmortem_check.py) prints the `unknown`
count first (the primary output of pass 1), then counterfactuals, person-shaped
factors and actions, and numbers with no source:

```bash
cd 09-writing/04-the-postmortem
python3 python/postmortem_check.py worked-example.md    # calibrate on a clean one
python3 python/postmortem_check.py                      # then artifacts/04-postmortem/*.md
```

The items it cannot reach — whether the Summary works for someone who was not
there, whether the timeline starts early enough, and whether the first hypothesis
is defended rather than mocked — are in [`rubric.md`](rubric.md).

`pg_stat_statements` accumulates since the last reset, so treat it as "what is
slow in general", not "what was slow during the incident" — and write that
limitation into the timeline's third column rather than quietly ignoring it. The
distinction is itself a detection gap.

Then circulate to two named people, one of whom was involved, and log the row in
[`../log.md`](../log.md). If you intend to publish a sanitised version in
[Topic 7](../07-writing-publicly/README.md), run the sanitisation gate —
`python3 lab/tools/sanitise_gate.py` from the `09-writing` root — **before**
writing that version.

## Predict, then record

Fill the Predicted row before you open any data.

| | Your top-suspect cause | Time-to-detect you expect to find | How many timeline cells will be `unknown` |
|---|---|---|---|
| Predicted | | | |
| Actual | | | |

Second table, filled once the analysis is done:

| Clock | Value | How you know |
|---|---|---|
| Time to detect | | |
| Time to diagnose | | |
| Time to mitigate | | |

**What would mean the experiment is broken rather than your prediction wrong:**
if you finish with a single clean root cause and fewer than three contributing
factors, you probably stopped at the first thing that *could* have caused it
rather than everything that *did*. Real latency incidents in a Python service
behind Postgres in containers are almost always a stack: a slow query *and* a
pool that serialises on it *and* a client retry that multiplies it *and* a
timeout longer than the user's patience.

Conversely, if your timeline is complete and precise and you needed no `unknown`
at all, check whether you reconstructed it from memory. Memory is confident and
wrong, and a timeline whose third column is empty is a story, not a record. And
if you cannot establish a start time at all, that is not a failed exercise: write
`unknown`, put "we cannot determine when this began" in *What we still don't
know*, and notice you have just derived your highest-value action item from an
absence.

## Answer before moving on

1. Take your longest clock. If you could only shorten one of the three by half,
   which would you pick for *this* incident, and which would you pick for the
   next incident you have not had yet? If the answers differ, explain why — that
   difference is an argument about where to spend instrumentation budget.
2. Pick your strongest contributing factor and ask: what would the same factor
   have looked like in a Go service, where a blocking call parks a goroutine
   rather than a thread? Does the factor disappear, or move?
3. You wrote an action item for every `unknown`. Which of those actions would
   have *also* shortened diagnosis for an unrelated incident? The ones that would
   not are suspiciously specific to the incident you just had — the classic shape
   of fighting the last war.
4. Your first hypothesis was wrong (they usually are). Reconstruct why it was the
   *reasonable* hypothesis given what was on screen at the time, then name the
   signal whose absence made it reasonable. That signal, not the fix, is the
   finding.

## Next up

[Topic 5 — explaining a tradeoff to someone who does not care how it
works](../05-explaining-tradeoffs/README.md). You now have the analysis. Whether
the remediation gets two weeks of your time or gets deferred behind a feature is
a separate skill, in someone else's units.
