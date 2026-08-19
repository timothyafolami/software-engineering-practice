# Layer 7 · Topic 1 — Authentication vs. authorization, and IDOR

### The takeaway (read this first)

**The one idea:** authentication answers *who are you*; authorization
answers *are you allowed to touch this specific object*. They are different
questions, checked at different times, and **the overwhelming majority of
real breaches are the second check missing on one endpoint**, not the first
check being broken. OWASP's Top 10 has put Broken Access Control at **A01
in both the 2021 and the 2025 editions** — it was A5 in 2017, so this is
its second edition at the top, and it is not close.

**Why it matters in practice:** IDOR (Insecure Direct Object Reference) is
the cheapest serious bug to ship and the hardest to spot in review, because
the vulnerable code *looks complete*. `GET /api/invoices/8123` with a valid
session returns invoice 8123. It works in every demo. It works in every
test you wrote, because your test user owns invoice 8123. The bug is that
it *also* works when a different logged-in user asks for it — the handler
authenticated the caller and then trusted the ID in the URL to be theirs.

**You'll know it landed when:** your instinct on seeing any endpoint with an
ID in it — path param, query string, body, a hidden form field, a filename
— is to immediately ask "what happens when I put someone else's ID here,"
and you know that the answer must be enforced *at the query* and not by an
`if` in the handler.

## The concept

Draw the two checks as a pipeline every request goes through:

```
request → [authN: is the session/token valid? who is this?] → user_id
        → [authZ: may THIS user act on THIS object?]         → allow / 403
        → handler runs
```

Authentication you tend to get right, because it fails *loudly and for
everyone*: if the session check is broken, nobody can log in and you find
out in ten minutes. Authorization fails *quietly and selectively*: the
owner never notices anything wrong, because for the owner the missing check
would have passed anyway. The only person who notices is the attacker
enumerating IDs, and they are not going to file a bug.

The mechanism behind almost every IDOR is the same shape: the object
identifier is **attacker-controlled input**, and the code fetches by it
without constraining the fetch to the caller's scope. `db.get(Invoice,
invoice_id)` is a *lookup by primary key* — it has no idea who is asking.
The fix is not to add a check after the lookup (that is the buggy version
one nervous `if` away). The fix is to make the *query itself* incapable of
returning rows the caller does not own.

There are three places to enforce that, and which you pick is the actual
lesson:

- **Handler layer:** `row = get(Invoice, id); if row.owner_id !=
  current_user.id: raise 403`. Correct if you never forget it. You will
  forget it, on endpoint number forty, at 6pm, and nothing will tell you.
- **Query layer:** `SELECT ... WHERE id = :id AND owner_id =
  :current_user`. The wrong-owner row is *never fetched*; a mismatched ID
  returns zero rows and a clean 404. Better, but still relies on every
  author remembering the `AND`.
- **Data layer (Postgres Row-Level Security):** a policy on the table —
  `USING (owner_id = current_setting('app.current_user')::int)` — that the
  database applies to *every* query against that table, whether or not the
  application remembered. Now forgetting the filter fails *closed*: you get
  zero rows, not a leak. This is the version that survives a growing team.

Derive the ranking rather than memorising it: each layer down is a place
where the check is applied by something that **cannot be omitted by the
next person writing a handler**. That is the only property that matters,
and it is why "we added a code review checklist" is not the same class of
fix.

### The pooling footgun, which is a real production incident pattern

RLS needs to know who the caller is, and the only channel Postgres gives
you is a session variable. There are two ways to set it and one of them is
a cross-tenant leak:

- `SET LOCAL app.current_user = '7'` — **transaction-scoped**. Dies at
  `COMMIT`/`ROLLBACK`, so it cannot outlive the request.
- `SET app.current_user = '7'` — **session-scoped**. Survives the
  connection being returned to the pool, and the next request that checks
  out that connection inherits it.

Under a pooler in **transaction mode** (PgBouncer's `pool_mode =
transaction`, which is what you run in production because it is the mode
that actually multiplexes), a connection is handed to a different request
between every transaction. A plain `SET` therefore bleeds one tenant's
identity into the next request that lands on that backend. The leak rate is
not 100% and not 0% — it is a function of pool size, concurrency and how
often the pool hands you a connection last used by someone else, which is
exactly why this bug survives testing: at one concurrent user it never
fires.

## How each language actually gets there

Four languages, not six. **The bug is a logic omission, not a
memory-safety or type-system property** — so Rust and C++ have nothing to
add here beyond a third and fourth way to write the same missing `AND`.
What *does* differ between runtimes is the thing worth reading: which
framework makes authorization *feel* already handled.

**Python (FastAPI + SQLAlchemy/psycopg3, your stack).** The idiomatic trap
is a dependency that returns `current_user` and a handler that then trusts
the path param. `Depends(get_current_user)` is authentication, it is
declarative, it shows up in the OpenAPI schema, and it makes the endpoint
*look* protected — which is exactly what lulls you. FastAPI has no
equivalent declarative hook for "and this object belongs to them," so the
authz check is ordinary handler code that nothing reminds you to write.
The query-layer fix is a scoped-query helper every route calls; the
data-layer fix is RLS plus `SET LOCAL` inside the request's transaction.

**Node (Express/Fastify + Prisma or `pg`).** Same shape, one extra hazard:
middleware ordering. `app.use(requireAuth)` mounted on a router gives a
strong feeling of coverage, and a route registered on a *different* router,
or before the `use` call, silently has no check at all. Prisma's
`findUnique({ where: { id } })` is the exact analogue of `db.get` — a
primary-key lookup with no notion of a caller.

**Go (`database/sql` + `chi`/`net/http`).** Nothing about static typing
helps: `invoiceID` is an `int64` whether or not it is *yours*. Go's
contribution here is ergonomic in the other direction — because there is no
ORM doing implicit fetches, the `SELECT` is written out in front of you, so
the missing `AND owner_id = $2` is at least visible in the diff rather than
hidden behind an ORM method name. Worth noticing: that is the entire
difference, and it is a readability property, not a safety one.

**Java (Spring Boot + Spring Security).** The interesting case, because
Java is the one runtime here with a *declarative* authorization model:
`@PreAuthorize("@invoiceGuard.owns(#id, authentication)")` runs before the
method body. That is a genuine improvement — the check is visible at the
signature and a missing annotation is greppable — and it is also the
sharpest illustration of the limit: `@PreAuthorize` is opt-in per method,
so an unannotated new endpoint is wide open, and Spring Data's
`findById(id)` underneath is still a primary-key lookup. A declarative
check you can forget to declare has the same failure mode as an `if` you
can forget to write.

The RLS fix is database-side and therefore **language-agnostic**, which is
part of why it is the strong answer: it does not depend on every service in
front of that database being written carefully, including the ones written
next year in a language not on this list.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack — `api`, `postgres`,
`pgbouncer` in transaction mode — seeded with three tenants and 1,500
invoices, ids 1..1500, 500 per tenant, **interleaved** so enumeration hits
all three.

Three handler variants behind `HANDLER_MODE`, all serving
`GET /invoices/{id}` and `GET /invoices`:

- `vulnerable` — `db.get(Invoice, id)`, returns whatever it finds.
- `fixed_query` — `WHERE id = :id AND owner_id = :caller`.
- `fixed_rls` — plain `db.get`, but the RLS policy plus a per-request
  `SET LOCAL app.current_user` makes the owner filter impossible to omit.

**Run 1 — enumeration.** Log in as alice, then fire 1,000 requests at a
constant arrival rate of 100 rps with IDs drawn uniformly from 1..1500.
Count responses that are `200` with an `owner_id` that is not alice's.
Report **wrong-owner rows per 1,000 requests**. Repeat for all three modes.

**Run 2 — the pooling leak.** Set `RLS_SET_MODE=session` (plain `SET`),
point `api` at `LAB_POOLED_DSN` so every transaction goes through PgBouncer
in transaction mode, and run alice and bob concurrently at 100 rps combined,
each requesting only IDs they own. Any `200` returning a row owned by the
*other* tenant is a leak caused purely by connection reuse. Report
**cross-tenant rows per 1,000 requests**, and re-run at pool sizes 2, 5 and
20 to see what moves the rate.

### How you'd know the fix is fake

`fixed_rls` returning zero leaks proves nothing if `api` connected as
`lab_owner`. **A table owner bypasses RLS silently by default** — no error,
no warning, every policy inert. Before you believe any RLS result, run
`SELECT current_user, row_security` inside a request transaction and
confirm you are `app_user`. This is the canonical fake fix for this topic:
the control is present, enabled, and doing nothing, and the only way to
detect it is to look rather than to test.

## How to run

The full compose lab (PgBouncer in transaction mode, k6 at 100 rps) is the
way to reproduce the *pooling* leak under real concurrency. These
self-contained programs reproduce the *mechanism* — the three enforcement
layers and their leak counts — with one command each and no Docker. The
in-memory versions run everywhere; the Postgres version is the real
data-layer lesson (owner-bypass fake fix, `SET` vs `SET LOCAL`) and needs a
reachable Postgres, which the lab machine has on `localhost:5432`.

```
# Core enumeration: vulnerable vs fixed_query vs fixed_rls, all four runtimes
python3 python/idor_enumeration.py
node   nodejs/idor_enumeration.js
cd golang && go run idor_enumeration.go && cd ..
cd java   && javac IdorEnumeration.java -d /tmp/t1java && \
             java -cp /tmp/t1java IdorEnumeration && cd ..

# The real thing, on Postgres: owner bypass + the SET/SET LOCAL pooling footgun
python3 python/idor_rls_postgres.py     # bootstraps an ephemeral sec_lab db
```

Each core program prints the leak table — wrong-owner rows per 1,000
requests, per handler — and the Postgres program prints demos A/B/C. Paste
the trailer numbers into `PREDICTIONS.md`.

> The compose form (`./attack.sh idor vulnerable`, `--pool-size 2|20`) and
> the k6-driven pooled-`SET` sweep still belong in `lab/` once Docker is up;
> these programs are the runnable stand-in and produce the same leak counts.

## Predict, then record

Before running, write down:

1. Against `vulnerable`, how many of 1,000 enumeration requests return a row
   alice does not own? (You know the seed: 1,500 rows, 500 hers, IDs drawn
   uniformly. Derive a number, don't guess a word.)
2. Against `fixed_query` and `fixed_rls`, what HTTP status does a
   wrong-owner ID return — 403 or 404 — and **why does the difference
   matter to an attacker doing enumeration?**
3. In the pooled `SET` run, what is the cross-tenant rate per 1,000, and
   does raising the pool size from 2 to 20 make it larger or smaller? Name
   the mechanism before you name the direction.

| Handler | wrong-owner rows per 1,000 requests | status on wrong-owner ID | p99 ms on `/invoices/{id}` |
|---|---|---|---|
| `vulnerable` |  |  |  |
| `fixed_query` |  |  |  |
| `fixed_rls` (`SET LOCAL`) |  |  |  |

| Pooled run (`SET`, PgBouncer transaction mode) | pool size | cross-tenant rows per 1,000 requests |
|---|---|---|
| alice + bob, 100 rps |  2 |  |
| alice + bob, 100 rps |  5 |  |
| alice + bob, 100 rps | 20 |  |

**What would mean the experiment is broken, not the prediction:** if
`vulnerable` leaks *zero* rows, you almost certainly re-seeded with every
invoice owned by one tenant (a stale volume does this — `down -v` first), or
your session cookie is not scoping anything; check that bob and carol own
distinct rows and that you are sending alice's credentials. If `fixed_rls`
leaks rows even with `SET LOCAL`, check `current_user` — as the owner role,
RLS is silently off and you are testing nothing. If the pooled `SET` run
shows *zero* leaks, your PgBouncer is probably in session pooling mode (the
default in several images), not transaction mode: the leak needs connection
reuse *between* transactions to appear at all. If it shows a leak on
*every* request, you are almost certainly not resetting the variable
anywhere and have built a constant, not a race — check that `fixed_rls`
with `SET LOCAL` on the same pooled DSN reports zero.

## Answer before moving on

1. Why is enforcing ownership *at the query* strictly better than a correct
   `if` in the handler, even though both return the right answer when
   written correctly? Name the failure mode each one has under a growing
   codebase, and say which of the two a new hire is more likely to trip.
2. UUIDs instead of sequential integer IDs are often sold as an IDOR fix.
   Precisely why are they **not** an authorization control, and what,
   specifically, do they actually reduce? (Hint: what changes for the
   attacker, and what doesn't?)
3. Your RLS policy is correct and enforced. Name two distinct ways a bug
   *elsewhere* in the request path can still produce a cross-tenant leak
   without touching the policy at all.
4. The pooled `SET` leak is intermittent and load-dependent. Describe the
   test you would have to write to catch it in CI — and then say why most
   teams' integration suites structurally cannot catch it.

## Next up

[Topic 2 — SQL injection as a string-building failure](../02-sql-injection/README.md):
the same "attacker-controlled input reaches a place it was not supposed to
reach" shape, one layer down, where the thing being fooled is a parser.
