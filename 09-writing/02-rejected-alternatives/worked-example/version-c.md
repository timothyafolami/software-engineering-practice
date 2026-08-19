> **Worked example, version C — the strongest advocate, in good faith, no hedging.**
> This is an example, not your artifact; yours goes in
> `artifacts/02-alternatives/`. Numbers are placeholders — see
> [`../../lab/README.md`](../../lab/README.md), rule 1.
>
> Pick the alternative you rejected **most confidently**, not the one you
> rejected most casually. In version B that is raising `pool_size`: the rejection
> is two clauses long and reads as settled. If version C is comfortable to write,
> you picked the wrong alternative.

# In defence of: raise `pool_size`, leave the handler synchronous

Raise the pool and ship it this afternoon. The change is one number in one
config file, it is behind no flag because it needs none, and it is reversible in
the time it takes to redeploy. Against it stands a design doc that will take
`<n>` weeks to write, review, and implement, during which every checkout is
exactly as slow as it is today. The pool number is not a competing theory of the
bug; it is the mitigation available now, and the correct order of operations
during ongoing user pain is to stop the pain and then fix the mechanism. Every
hour spent arguing about which of the two is the *real* fix is an hour the
degraded behaviour stays in production, and that trade has been made in favour of
the elegant answer many more times than it has worked out.

The technical objection to it is weaker than it looks. It is true that the pool
is not the binding constraint at `<observed concurrency>`; it does not follow
that raising it buys nothing, because the binding constraint moves under load and
the measurement establishing that the pool is not binding was taken at a single
arrival rate on a single day. The Postgres `max_connections` argument is a real
limit, but `<larger>` is `<derive the fraction>` of it and the worker fleet's
actual usage is `<measured>`, not its configured ceiling — so the headroom claim
in version B is a claim about configuration, not about behaviour. Meanwhile the
proposed fix has a risk the pool change does not: it changes what the endpoint
*returns* when pricing is slow, which is a product decision currently filed as an
open question with no decider. Shipping a config number does not require anyone
to decide what a degraded price is.

## Decide again

Write the verdict here, after the two paragraphs above, not before them.

**Verdict (example):** the design doc's decision stands, and its *sequencing*
changes. Raise `pool_size` to `<larger>` now as a mitigation, with the
connection-wait and Postgres `numbackends` panels watched for `<duration>`, and
keep the timeout-budget work as the fix. Version B's flip condition for this
alternative said it becomes correct once the pricing call is bounded; version C
found the weaker assumption underneath — that the alternative and the fix are
mutually exclusive, which nothing in the analysis actually supports. That is the
only thing this exercise is for, and it produced a change on this run.

**What to write in the record:** amend version B's first alternative from
"rejected" to "accepted as an interim mitigation, revisit after the timeout work
lands", and add a row to the [Topic 3](../../03-the-rfc-loop/README.md) ledger,
because a decision that changed after the doc was drafted is exactly what the
ledger exists to preserve.

**If your version C changes nothing**, say so explicitly rather than leaving the
section unfinished, and check the two failure modes in the topic README: did you
pick the confidently-rejected alternative, and did you write the two paragraphs
without a single "of course, however"?
