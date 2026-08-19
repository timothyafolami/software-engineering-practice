> **Worked example, not your artifact.** Your commits go in your own repos; the
> PR description below is written from `templates/pr-description.md`. Numbers are
> placeholders — a benchmark number in a PR body is read as measured, so either
> fill it from a run you did or delete the line.
>
> Per-ecosystem versions of the commit message — and what each ecosystem's
> convention does to it — are in [`python/`](python/commit-conventions.md),
> [`nodejs/`](nodejs/commit-conventions.md), [`golang/`](golang/commit-conventions.md),
> [`rust/`](rust/commit-conventions.md), [`cpp/`](cpp/commit-conventions.md) and
> [`java/`](java/commit-conventions.md).

# A PR description for the change from Topic 1

This is the design doc's decision, shipped: the outbound pricing call moves to an
async client under an explicit timeout budget.

---

## Why

`POST /checkout` calls pricing with a synchronous client from inside an
`async def` handler, so the call holds the worker's event-loop thread for its
full duration and delays every other request that worker is serving — including
requests that never touch pricing.

Trigger: the latency incident writeup, contributing factors 1 and 2
(`<link to the postmortem>`). Observed: `<measured share>` of checkouts over
`<threshold>` at peak, from `<dashboard query>`. Decision and alternatives:
`<link to the RFC>`, accepted `<date>`, with dissent recorded from
`<reviewer>` — they held that the interim pool change alone would be sufficient,
and the condition that would prove them right is written into the RFC.

## What changes

The pricing call becomes `await`ed against a module-level `httpx.AsyncClient`
created in the FastAPI lifespan handler, with an explicit `<budget>` timeout
passed at the call site rather than inherited from a default. On timeout the
handler returns `<the degraded response>` with `<status>`. Behind `<flag>`,
default off; the synchronous path stays in the binary until the flag is removed.
The diff has the rest.

## What I tried first and rejected

Dispatching the existing synchronous client with `run_in_executor`. Smaller diff,
keeps a library whose failure modes we already know, and it does free the event
loop. Rejected because it moves the queue rather than removing it: the default
executor is sized `min(32, cpu_count + 4)`, our containers see `<n>` CPUs, and at
`<computed n>` concurrent slow pricing calls it saturates — with no metric on
executor depth, that saturation is invisible in every dashboard we have. It
flips if we cannot upgrade `<pinned dependency>`; that condition is in the RFC's
alternatives section.

## Risk and rollback

If `<budget>` is too tight it converts slow successes into degraded responses.
Noticed by: the degraded-response rate panel added in this PR, alerting at
`<rate>` — that signal did not exist before this change and shipping it is part
of the change rather than a follow-up.

Rollback is one flag flip, under two minutes, no deploy and no data migration to
undo. The rollout is `<n>%` → `<n>%` → full, each step gated on that rate.

## How this was verified

- `<the test you added>`, covering the timeout path — previously untested,
  because the timeout did not exist.
- A load run at `<rate>` req/s against a pricing stub delayed to `<delay>`:
  `<what you observed for p99 on an endpoint that does not call pricing>`, versus
  `<what you observed before the change>`. Both runs on `<machine>`, `<n>`
  workers, same generator and load shape.
- **Not verified:** behaviour when pricing is fully down rather than slow. Every
  checkout pays the full budget before degrading; a circuit breaker is the fix
  and is explicitly out of scope (RFC non-goal 3).

---

## The commit that goes with it

```
checkout: call pricing via async client under a <budget> timeout

Why now: <postmortem link>, factors 1 and 2. The sync client holds the
event-loop thread for the whole outbound call, so one slow dependency delays
every concurrent request on that worker. Measured: <measured share> of
checkouts over <threshold> at peak, from <dashboard query>.

Rejected first: run_in_executor with the existing sync client. Frees the loop
but moves the queue into a thread pool sized min(32, cpu_count + 4), where we
have no depth metric - a saturation we could not see. Flips if <pinned dep>
blocks the async client; see <RFC link>, Alternatives.

Constraint: the degraded response on timeout is a product decision, not an
engineering one - <owner> chose <the degraded response> in <RFC link>, open
question 1. Changing it is not a refactor.

Verified: <load run, machine, what you observed>. Not verified: pricing fully
down; every checkout pays the full budget first. Circuit breaker is out of
scope by RFC non-goal 3.

Refs: <issue>
```

## What to notice

- **"Why" links three durable artifacts** — postmortem, RFC, dashboard query —
  and states the measurement inline anyway. Links rot; one sentence of conclusion
  in the repository survives the wiki migration.
- **The rejected approach is specific enough to be wrong.** A reader can check
  the executor sizing and tell you the arithmetic is off. That is the property
  you want.
- **The dissent survives into the PR.** Someone reading this in a year learns
  that a competent colleague disagreed and what would have made them right.
- **"Not verified" is a section.** It is the sentence that stops a reviewer
  assuming the down-case was tested, and it costs one line.
- **The commit body is not the PR body.** The PR body can point at threads and
  links; the commit has to survive without them, because a clone in five years is
  all there is. If your team squash-merges, the PR body *becomes* the commit
  message — which means it has to carry the constraint, and a body that restates
  the diff loses it permanently.
