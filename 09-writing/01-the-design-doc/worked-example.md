# Worked example — a design doc for the checkout latency work

> **This is an example, not your artifact.** Your doc goes in
> `artifacts/01-design-doc/<slug>.md`, written from `templates/design-doc.md`.
>
> **Every number here is a placeholder** — `<your number>`, `<window>`,
> `<source>`. That is not laziness, it is rule 1 in
> [`../lab/README.md`](../lab/README.md): a number you did not measure does not
> belong in a document someone will make a decision from. Fill each placeholder
> from your own system, or delete the sentence that needs it. A sentence you
> cannot source is a sentence you cannot defend in review.
>
> Read it for two things: which sentences could be contradicted, and how much
> of the document is *not* the proposal.

---

# Move the outbound pricing call off the event loop, under a timeout budget
Author: `<you>` · Date: `<date>` · Status: Draft
Reviewers: `<name who owns the pricing service>`, `<name who carries the pager>`
Decision needed by: `<date, ~10 working days out>`

## Context

`POST /checkout` calls the pricing service with `requests.post` from inside an
`async def` handler. `requests` is a synchronous client, so that call occupies
the single event-loop thread of its uvicorn worker for its full duration —
every other request being served by that worker waits, including requests that
never touch pricing. This is a property of the asyncio concurrency model, not of
our code volume: see [`01-machine/03-concurrency-models/`](../../01-machine/03-concurrency-models/README.md).

Measured, with sources, so each can be contradicted:

- p99 of the outbound pricing call: `<your number>` over `<window>`, from
  `<dashboard query or log range>`.
- Share of `/checkout` requests exceeding `<threshold>` end to end:
  `<your number>`, same source.
- Arrival rate at the observed peak: `<your number>` req/s, from `<source>`.
- uvicorn runs `<n>` workers per container, `<n>` containers, from
  `<compose file / deployment manifest>`.
- SQLAlchemy pool: `pool_size=<n>`, `max_overflow=<n>`, against Postgres with
  `max_connections=<n>` shared with the worker fleet, from `<source>`.
- The pricing call has **no client-side timeout configured today**. Default for
  `requests` is no timeout at all, so the ceiling on a hung call is whatever the
  network or the upstream imposes — verified by reading `<file:line>`.

What we do not know, and are not going to pretend to: when this started
degrading. Request-duration metric retention is `<n>` days; the complaints
predate it. That gap is
[Topic 4](../04-the-postmortem/README.md)'s problem, and it is why this doc
proposes a change rather than a diagnosis.

## Goals

1. No single outbound pricing call occupies an event-loop thread for longer than
   the budget in *Proposed design*. Measured by: p99 event-loop lag per worker
   after the change, from `<source we will add>`.
2. p99 on `POST /checkout` under `<threshold>` at the arrival rate in Context.
   Measured by: the same dashboard query as Context, so before and after are
   comparable.
3. A pricing call that hangs degrades **checkout only**, not unrelated endpoints
   on the same worker. Measured by: p99 on `<an endpoint that does not call
   pricing>` during a fault-injection run where pricing is delayed.

Goal 3 is the one worth arguing about: it is the only one that would still be
worth shipping if the latency numbers turned out fine.

## Non-goals

- **Fixing the N+1 on the order-list endpoint.** It is real and it is not on the
  checkout path.
- **Making the pricing service itself faster.** We do not own it; this change
  makes us tolerant of it being slow, which is a different claim.
- **Adding a circuit breaker.** Deliberately deferred to a follow-up so that the
  timeout budget can be evaluated on its own. See Alternatives.
- **Changing the connection pool.** See Alternatives — it is the *next* change,
  not this one.
- **Caching prices.** Correctness question we have not answered (how stale may a
  quote be?), and it belongs to whoever owns pricing.

## Proposed design

Two changes, shipped together because either alone is incomplete.

**1. Async client.** Replace `requests.post` with `httpx.AsyncClient`, one
client instance per process, created in the FastAPI lifespan handler and reused.
The call site becomes `await`ed. Nothing else in the handler changes.

**2. An explicit budget.** The handler gets a total budget of `<total>` for
outbound work, and the pricing call gets `<pricing budget>` of it, passed as an
explicit timeout on the call rather than inherited from a default. On timeout we
return `<the degraded response: e.g. cached-tier price / retry-later error>`
with `<status code>`.

Request path, before:

```
client -> uvicorn worker (event loop)
            handler: requests.post(pricing)   <- thread held, whole duration
            handler: SELECT ... (async, pooled)
            response
```

After:

```
client -> uvicorn worker (event loop)
            handler: await httpx.post(pricing, timeout=<pricing budget>)
                       ^ awaits; loop serves other requests meanwhile
            handler: SELECT ... (async, pooled)
            response, or degraded response on timeout
```

**Where it degrades.** If pricing is slow but under budget, checkout is slow and
everything else is fine — that is the intended new behaviour. If pricing is
slower than budget, checkout returns the degraded response at a bounded time.
If pricing is *down*, every checkout pays the full budget before degrading; that
is the case a circuit breaker would fix and this doc does not.

**What we will not know until we ship:** whether the pricing service's own
latency distribution has a long enough tail that `<pricing budget>` converts an
acceptable number of slow successes into failures. We can bound this from the
current distribution but not settle it, because the current distribution is
measured under a load shape that this change alters.

## Alternatives considered

Written out in full, three ways, in
[Topic 2](../02-rejected-alternatives/README.md). Summary, each with the
condition that flips it:

- **Raise `pool_size` and leave the handler synchronous.** One line, ships today.
  Loses because the pool is not the binding constraint at `<observed
  concurrency>`; the queueing is on the outbound call. **Flips once the pricing
  call is bounded** — after this change, the pool is plausibly next, and that is
  the argument for doing it second rather than never.
- **Run the pricing call in a thread pool** (`run_in_executor`) and change
  nothing else. Genuinely smaller diff and keeps the sync client. Loses because
  it converts an event-loop stall into thread-pool exhaustion at
  `<pool size>` concurrent pricing calls, which is a harder failure to see.
  **Flips if the async client turns out to be blocked on a dependency we cannot
  upgrade.**
- **Do nothing.** The endpoint is slow, not failing. Loses because queueing is
  superlinear in arrival rate: at `<measured growth>` this degrades on its own
  rather than staying flat. **Flips if traffic growth stops**, or if `<the other
  work>` removes the arrival-rate pressure first.

## Risks

| Risk | How we would notice it in production |
|---|---|
| `<pricing budget>` is too tight and converts slow successes into degraded responses | Rate of degraded checkout responses, alerted at `<rate>`; this metric does not exist yet and shipping it is part of the change |
| `httpx.AsyncClient` connection-pool defaults differ from `requests` and we inherit a new limit we did not choose | Connection-wait time on the client, plus a deliberate load run before rollout; we will write the chosen limits down rather than accept defaults |
| The degraded response is wrong for the business (a price we should not have quoted) | This is an open question, not a risk — see below. Filing it here would be us deciding it alone |
| Freeing the event loop raises concurrency into Postgres and moves the queue to the pool | Postgres `numbackends` and connection-wait time; this is the flip condition on alternative 1 firing, which is the expected outcome, not a surprise |

## Open questions

1. **What is the correct degraded behaviour when pricing exceeds budget** — a
   stale price, a retry-later error, or a hard failure? Decider: `<name, product
   or pricing owner>`. This is not an engineering call and we should not make it
   by default in a diff.
2. **Is `<pricing budget>` inside the pricing team's own SLO?** Decider:
   `<name>`. If their p99 is above our budget, our budget is a decision to shed
   their slow tail, and they should hear that from us rather than from a graph.

## Rollout and rollback

- Behind `<flag name>`, default off, evaluated per request.
- Enable for `<n>%` of traffic for `<duration>`, then `<n>%`, then full — each
  step gated on the degraded-response rate staying under `<rate>`.
- **Rollback is one flag flip. Under two minutes, no deploy, no data migration
  to undo.** The async client and the sync client can coexist in the binary for
  the duration of the rollout; the flag chooses the call site.
- The flag is removed `<n>` weeks after full rollout. That removal is a
  scheduled follow-up, not an intention.

---

## What to notice about this example

- **Context is long and boring and lists its sources.** That is the section that
  stops a reviewer objecting for a reason you already ruled out.
- **Every goal names how it is measured**, and one of them (goal 3) is worth
  shipping even if the headline metric disappoints — which is a claim someone
  can disagree with.
- **The risks table has a second column** that is only answerable if the signal
  exists. One row admits the signal does not exist yet; that admission became
  part of the change.
- **One row was moved out of Risks into Open questions** on purpose. Filing a
  decision you have not made as a risk you have accepted is how you end up owning
  it alone — Topic 1's rubric item 2 and its "answer before moving on" question 2.
- **The rollback is in units of time**, which is the sentence that most often
  turns out to be false while you are writing it.
