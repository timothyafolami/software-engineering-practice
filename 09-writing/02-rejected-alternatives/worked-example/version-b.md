> **Worked example, version B — every rejection names the condition that flips it.**
> This is an example, not your artifact; yours goes in
> `artifacts/02-alternatives/`. Numbers are placeholders on purpose — see
> [`../../lab/README.md`](../../lab/README.md), rule 1.
> Read A and B side by side: `diff -u version-a.md version-b.md`.

# Alternatives considered

**Alternative: raise `pool_size` from `<current>` to `<larger>` and leave the
handler synchronous.** This is one line and ships today. Rejected because the
pool is not the binding constraint: at `<observed concurrency>` concurrent
requests we are queueing on the outbound pricing call (measured: p99 of that
call is `<your number>` for `<your share>` of requests, from `<source>`), and a
larger pool converts a bounded wait into `<larger>` concurrent Postgres sessions
on a server configured for `max_connections=<n>`, shared with the worker fleet.
**This flips once the pricing call is bounded** — with the outbound call capped
at `<budget>`, the pool becomes the next constraint and raising it is the correct
next change. Revisit after the timeout work lands; the signal that it is time is
connection-wait time on the pool exceeding `<threshold>`, from `<source>`.

**Alternative: keep the synchronous client and dispatch the call with
`run_in_executor`.** Smaller diff than replacing the client, and it keeps a
library we already understand the failure modes of. Rejected because it converts
an event-loop stall into thread-pool exhaustion: the default executor is sized
`min(32, cpu_count + 4)` and our containers see `<n>` CPUs, so `<computed n>`
concurrent slow pricing calls exhaust it, and the queue that forms behind it is
invisible in every dashboard we currently have — a harder failure to see than the
one we have now, which is the actual objection. **This flips if `httpx` turns
out to be blocked on a dependency we cannot upgrade** (we pin `<lib>` at
`<version>` for `<reason>`), in which case the executor path is the only way to
free the loop and we accept the queue plus a gauge on executor depth.

**Alternative: rewrite the checkout handler in Go.** Not a category judgement:
it would genuinely fix the mechanism, because the netpoller parks the goroutine
rather than the OS thread, so a slow pricing call costs one goroutine instead of
every concurrent request on the worker (see
[`01-machine/03-concurrency-models/`](../../../01-machine/03-concurrency-models/README.md)).
Rejected because it buys one endpoint's latency at the cost of a second
deployment toolchain, a second set of on-call runbooks, and a second place where
the `<shared model/schema>` has to be kept in step — and because the same
mechanism is available in Python for the size of the diff in *Proposed design*.
**This flips if more than `<n>` endpoints hit the same pattern**, at which point
the argument stops being about this endpoint and becomes about the service.

**Alternative: do nothing.** The endpoint is slow but not failing; the complaint
rate is `<measured>` per week from `<source>` and the degraded-checkout rate is
`<measured>`. Rejected because the queueing is superlinear in arrival rate — at
`<measured growth>` per month this degrades on its own rather than staying flat,
and the point at which it stops being a complaint and starts being a timeout is
`<derive it or write unknown>`. **This flips if traffic growth stops**, or if
`<the other work>` lands first and removes the arrival-rate pressure by itself.
It also flips, harder, if the honest answer to "what happens if we ship nothing
for six months" turns out to be "it degrades slowly and nobody notices" — which
is a claim `<source>` can settle and we should settle before spending the weeks.
