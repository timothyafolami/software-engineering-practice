> **Worked example, not your artifact.** Yours goes in
> `artifacts/04-postmortem/latency-incident.md`, from `templates/postmortem.md`.
>
> Every number is a placeholder and **every `unknown` is real** — they are the
> shape this document takes when the instrumentation cannot answer the question,
> which is the normal case and the point of the exercise. Your `unknown` count is
> the primary output of pass 1. Each one becomes an action.
>
> No sentence here names a person, and there are no counterfactuals. Both are
> checkable: `python3 python/postmortem_check.py worked-example.md`.

# Postmortem: checkout has been intermittently slow for weeks

Status: Draft · Author: `<you>` · Date: `<date>` · Reviewers: `<name who was
involved>`, `<name who was not>`

## Summary

Users experienced `POST /checkout` taking longer than `<threshold>` on roughly
`<measured share>` of attempts, intermittently, over a window we cannot bound
earlier than `<earliest evidence date>`. The degradation is worst at the daily
traffic peak and recovers on its own overnight, which is why it presented as
"the site feels slow sometimes" rather than as an outage. It is not yet ended:
this postmortem is being written during the incident, not after it, because the
absence of a start time is itself the finding. The mechanism is a synchronous
outbound call inside an async handler, which holds the event-loop thread of a
uvicorn worker for the duration of the call and delays every other request that
worker is serving. Nothing changed to end it, because nothing has ended it.

## Impact

- `<measured share>` of `POST /checkout` requests over `<threshold>` during
  `<window>`. Source: `<dashboard query>`.
- `<n>` support tickets describing checkout as slow or hanging between `<date>`
  and `<date>`. Source: `<ticket search query>`.
- Failed checkouts (client gave up or timed out): **unknown — see Detection
  Gaps.** We do not distinguish a client disconnect from a completed slow
  request in the access log.
- Revenue impact: **unknown, and not estimated here.** We do not have the
  conversion data to derive it, and an invented figure in this document would be
  repeated in rooms where it cannot be checked.
- SLO burn: **unknown — no SLO is defined for this endpoint.** That absence is
  itself a contributing factor, not an oversight in this document.

## Timeline (UTC, one row per observable event)

| Time | What happened | How we knew (or didn't) |
|---|---|---|
| `<date/time>` | Deploy `<sha>` added the synchronous pricing call to the checkout handler | Deploy log; **nobody noticed at the time — no latency alert existed for this endpoint** |
| unknown | Latency actually began degrading | **unknown — request-duration metric retention is `<n>` days and the deploy is older than that. See Detection Gaps** |
| `<date>` | First support ticket describing checkout as "hanging" | Ticket search `<query>`; the ticket was routed to `<queue>` and closed as "could not reproduce" |
| `<date>` | Second and third tickets, same week | Same query. No mechanism connects three similar tickets into one signal |
| `<date/time>` | `<n>` slow requests in one hour, the largest cluster in the retained window | `<dashboard query>` — retained data only, so this is the largest cluster *we can see*, not the largest that happened |
| `<date/time>` | Pricing service deployed `<their change>` | Their deploy log; correlation only, and we have not established causation |
| `<date/time>` | Investigation opened; first hypothesis was a slow database query | `pg_stat_statements`, read at `<time>` |
| `<date/time>` | Database hypothesis weakened: the slowest statement by mean time is `<query>` at `<n>` ms, which does not account for `<threshold>` | Same source, plus the arithmetic on the page |
| `<date/time>` | Read the handler; found `requests.post` inside `async def` | Code read, `<file>:<line>`. **No tool told us this; it was found by looking** |
| unknown | Whether the pricing call is slower now than it was at the deploy | **unknown — we have no historical latency for outbound calls at all** |
| — | Mitigation | Not yet applied. See Actions |

Two rows carry a limitation rather than a fact, on purpose. `pg_stat_statements`
accumulates since the last reset, so it answers "what is slow in general", not
"what was slow during the incident" — writing that into the third column rather
than quietly using the number as if it were incident-scoped is the difference
between a record and a story.

## Contributing factors

1. **A synchronous HTTP client inside an `async def` handler.** `requests.post`
   holds the event-loop thread of its uvicorn worker for the full duration of
   the call, so one slow downstream delays every concurrent request on that
   worker, including requests that never touch pricing. This is a property of
   the asyncio concurrency model, not of our traffic volume — the same call in a
   Go service would park a goroutine and free the thread
   (see [`01-machine/03-concurrency-models/`](../../01-machine/03-concurrency-models/README.md)).

2. **A default nobody chose: no client-side timeout.** `requests` applies no
   timeout unless one is passed. Nothing in the codebase or in review makes the
   absence of a timeout visible — the call site with a timeout and the call site
   without one are one keyword apart in a diff, and only one of them is
   unbounded.

3. **No isolation between a slow dependency and unrelated requests.** The
   service has one shared resource (the worker's event loop) and no bulkhead, so
   the blast radius of any slow outbound call is "everything that worker is
   doing" rather than "the feature that made the call".

4. **The signal that would have shown this does not exist.** There is no
   outbound-call latency metric and no event-loop-lag metric. Every graph the
   investigation looked at was of the database, because those are the graphs
   that exist, and the shape of the dashboard determined the shape of the first
   hypothesis.

5. **Three similar support tickets did not aggregate into one signal.** Each was
   handled individually and closed as not reproducible, which is the correct
   handling of a single ticket and the wrong handling of a pattern. No mechanism
   raises "three tickets, same endpoint, same symptom" to anyone.

6. **Degradation that recovers overnight reads as normal variance.** The daily
   pattern made every partial observation consistent with "busy day", which is
   the most available explanation and was reasonable each time it was reached.

## Detection gaps — why this was hard to see

| Clock | Value | How you know |
|---|---|---|
| Time to detect | **unknown, lower bound `<n>` days** | Bounded below by first-ticket date minus deploy date; the true start is unrecoverable at `<n>`-day metric retention |
| Time to diagnose | `<hours from investigation opened to reading the handler>` | Investigation notes, `<source>` |
| Time to mitigate | not yet | No mitigation applied at time of writing |

The shape of those three numbers is the whole finding: **detection is measured in
weeks and cannot even be measured precisely, diagnosis in hours, mitigation not
yet started.** A long detect with a short diagnose is a monitoring problem
wearing an incident costume.

**What we were looking at that told us the wrong thing.** The database
dashboards, because they are the dashboards that exist. `pg_stat_statements`
showed a plausible slowest query and the investigation spent `<duration>` on it.
That was a *reasonable* first hypothesis: the endpoint writes to Postgres, the
symptom was latency, and the database is the component with the best
instrumentation, so it is the component whose behaviour can be examined at all.
The signal whose absence made it reasonable is outbound-call latency — with that
graph, the hypothesis would have been discarded in minutes rather than hours,
because the outbound call and the total request duration would have moved
together and the database time would visibly not have accounted for the gap.

**What would collapse the diagnosis time next time.** One panel showing, per
endpoint, total request duration split by where it was spent: database, outbound
HTTP, and everything else. Any incident whose cause is "one component of the
request got slow" becomes a lookup rather than an investigation. That is a class
of incident, not this incident.

## What went right

- **The endpoint degraded rather than failing.** No data was lost, no order was
  written twice, and the retry behaviour on the client did not amplify load into
  a metastable failure. That is worth naming because it is a property that could
  be removed accidentally by a future change — for example by adding a client
  retry without a budget.
- **The database was instrumented well enough to be *ruled out* quickly.**
  Eliminating a hypothesis in `<duration>` is a working control, and the reason
  it worked is that someone had already enabled `pg_stat_statements`.
- **The handler is small enough to read.** Diagnosis, once it reached the code,
  took minutes. Deep modules with narrow interfaces paid off here
  (see [`08-craft/01-deep-and-shallow-modules/`](../../08-craft/01-deep-and-shallow-modules/README.md)).

## Actions

| # | Action | Changes what | Owner | Due |
|---|---|---|---|---|
| 1 | Default the service's HTTP client to the async client at module level, so the blocking one has to be imported explicitly | An interface — the wrong choice becomes an unusual import that shows up in review | `<name>` | `<date>` |
| 2 | Add an explicit timeout to every outbound call, and a lint rule that fails CI on a call with no timeout argument | A default and a gate | `<name>` | `<date>` |
| 3 | Add an outbound-call latency metric and an event-loop-lag metric per worker, with retention of at least `<n>` days | A signal, and the retention that makes it usable for the *next* slow-onset incident | `<name>` | `<date>` |
| 4 | Add the request-duration-by-component panel described in Detection Gaps | A signal that collapses a whole class of diagnosis | `<name>` | `<date>` |
| 5 | Alert on `<n>` support tickets naming the same endpoint within `<window>` | A signal, from data we already have and do not aggregate | `<name>` | `<date>` |
| 6 | Define an SLO for `POST /checkout` so that "slow" has a threshold that is not a matter of opinion | A limit | `<name>` | `<date>` |
| 7 | Runbook: "checkout is slow" — the three graphs to open, in order, and what each rules out | A doc, and the only doc permitted here, because it shortens diagnosis | `<name>` | `<date>` |

Actions 3, 4 and 5 are derived directly from `unknown` cells above. That is the
honest version of "instrument the service": a list generated by questions this
document could not answer, rather than a dashboard built from whatever was easy
to graph.

## What we still don't know

- **When this began.** Metric retention is shorter than the age of the deploy,
  and no artefact older than `<n>` days records per-request duration. This is
  unrecoverable — not pending investigation, gone.
- **Whether the pricing service got slower, or our traffic grew into a
  pre-existing slowness.** Both are consistent with everything observed. The
  distinguishing evidence would have been outbound-call latency history, which
  does not exist. Action 3 makes this answerable the next time and does not make
  it answerable now.
- **How many users abandoned checkout.** The access log records the request that
  completed; a client that disconnected is indistinguishable from one that waited.
- **Whether other endpoints on the same workers were affected.** Almost certainly
  yes, by mechanism — factor 1 — but there is no per-endpoint latency history to
  confirm it, so it stays here rather than in Impact.

---

## What to notice about this example

- **The `unknown` count is not a defect in the document.** Count them, then notice that three of
  the seven actions are derived from them. An `unknown` with a source note is a
  finding; an `unknown` with no explanation is an unfinished sentence.
- **The timeline starts at the deploy**, which is `<n>` days before anyone
  noticed anything, and its third column says who did not notice and why.
- **Factor 2 is a default nobody chose** — rubric item 4. Those are the factors
  that generate actions changing a whole class of behaviour rather than one line.
- **The first hypothesis is defended, not mocked.** It was reasonable given the
  graphs that existed, and the finding is the missing signal that made it
  reasonable — rubric item 5. A postmortem where the first hypothesis looks
  stupid has been written with hindsight, and the lesson has been removed.
- **Every action changes a system.** Cover action 7 with your hand: the document
  still has teeth. That is the test for whether a doc-shaped action is carrying
  weight it cannot carry.
- **Impact declines to estimate revenue.** Writing `unknown` there costs you a
  sentence and buys you the ability to be believed about the numbers you do have.
