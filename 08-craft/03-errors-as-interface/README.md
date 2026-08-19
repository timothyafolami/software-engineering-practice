# Layer 8 · Topic 3 — Errors as part of the interface, and the silent-failure replay

### The takeaway (read this first)

**The one idea:** an error is a return value with a contract. The only
question that matters for each one is *what can the caller do about it* — and
if the answer is nothing, the code must fail loudly rather than return
something that looks like an answer.

**Why it matters in practice:** this is the highest-severity failure class in
production because it does not page anyone. A swallowed exception in a data
access path turns a broken database into a *fast, successful, empty*
response. Your dashboards go green. Your latency improves. Your users see an
empty page.

**You'll know it landed when:** you can look at any `except` block and say
which of the three categories it is — caller-actionable, retryable, or a bug —
and you notice immediately when a `try` block is wider than the one statement
inside it that can actually fail.

## The concept

Three categories, and nearly every error-handling bug is a miscategorisation:

1. **Caller-actionable.** The caller can do something specific and different:
   `OrderNotFound` → return 404; `InsufficientFunds` → show a message;
   `RateLimited(retry_after)` → back off. These belong in the *signature* —
   the type, the docstring, and the OpenAPI responses. An error the caller is
   supposed to handle but cannot see is not a contract, it is a rumour.
2. **Retryable / transient.** Connection reset, `40001` serialization
   failure, 503 from upstream. The caller can retry *with a budget* (Layer 5's
   material). The distinguishing property is precise: the same call, unchanged,
   might succeed later.
3. **Bugs.** `KeyError` on a dict you built, `TypeError`, a violated
   invariant. Nothing the caller can do. **Crash loudly.** A 500 with a stack
   trace in your logs is strictly better than a 200 with wrong data, because
   only one of them is discoverable.

The design principle underneath is Ousterhout's **define errors out of
existence** — the best exception is the one the API makes impossible.
`dict.pop(k, default)` instead of catching `KeyError`. An idempotent `DELETE`
that returns 204 whether or not the row existed. `UPDATE ... WHERE id = ? AND
status = 'pending'` returning a rowcount, instead of a read-check-write that
can race. Each of these removes a branch from *every* caller, forever. It is
the highest-leverage error-handling move available and almost nobody files it
under error handling.

Two properties are worth stating as rules because they catch most of the rest:

- **The `try` block should contain exactly the statements that can raise the
  thing you are catching.** A wide `try` catches errors from code you never
  considered — which is how a `KeyError` in your own response-building code
  ends up being reported to the user as "database unavailable".
- **Catching and logging is not handling.** If the function cannot restore the
  caller's ability to proceed, the only correct moves are re-raise, translate
  (with the cause preserved), or crash.

## How each language actually gets there

Six languages, and this is a topic where the runtime genuinely is the subject:
each of the six makes a *different* category the easy one, and you can predict
a codebase's characteristic error bug from its language before you read it.

**Python (your stack).** Exceptions are the mechanism, so all the discipline
has to live in the taxonomy, because nothing in the language distinguishes
category 1 from category 3 — both are just classes. Define a small hierarchy
in `core/errors.py`: one base `AppError`, then `NotFound`, `Conflict`,
`Invalid`, `Unavailable(retry_after)`. Map them to HTTP exactly once, in a
single FastAPI exception handler — never by raising `HTTPException` from a
repository, which couples your data layer to a transport it should not know
exists. Then the rule that catches the most bugs in practice: **`except
Exception` is banned outside the top-level handler.** Narrow it, or do not
catch it. Two Python specifics worth having at your fingertips: `raise
NewError(...) from e` sets `__cause__` explicitly (raising inside an `except`
block preserves the original as `__context__` anyway; `from None` deliberately
erases it, which is right only when the inner exception is an implementation
detail nobody will ever need). And `ExceptionGroup`/`except*` (PEP 654, Python
3.11+) is not a curiosity — `asyncio.TaskGroup` raises one when several
children fail, and a plain `except Exception` handler will silently handle
only the first. Any endpoint that fans out with `TaskGroup` needs `except*`.

**Node.** `new Error(msg, { cause: err })` is the `raise ... from` equivalent.
The Node-specific hazard is the unhandled rejection: a promise rejected with
no handler terminates the process on modern Node, which is *correct* category-3
behaviour and regularly gets "fixed" by installing a global
`unhandledRejection` handler that logs and continues — converting a loud
crash into exactly the silent failure this topic is about. The second hazard
is that an `async` function's rejection is invisible unless someone awaits it:
fire-and-forget work in a handler swallows its own errors by construction.

**Go.** The category distinction lives in the type system by convention:
sentinel errors (`errors.Is(err, ErrNotFound)`) for caller-actionable, custom
types with `errors.As` when the caller needs data out of the error, and
`panic` for bugs. Go's real advantage is not verbosity, it is that
`if err != nil` makes "what does the caller do about this" a question you
cannot skip syntactically. `%w` in `fmt.Errorf` is the cause chain. Steal the
habit, not the syntax. Go's own failure mode is the opposite of Python's: an
error wrapped and returned at every level with no added information produces a
message that names four layers and no cause.

**Rust.** The only language here where the *signature* states which errors
exist and the compiler refuses to let you ignore one. `Result<T, E>` is a
value; `?` propagates with a `From` conversion; `#[non_exhaustive]` on an
error enum lets you add variants without breaking callers' matches. Category 3
has its own mechanism: `panic!`, `unwrap`, `expect`, and the unwinding they
cause are for bugs, and the split between `Result` and `panic` is the exact
distinction this topic is teaching, enforced by a type. The failure mode is
real too: `Box<dyn Error>` or a single `anyhow::Error` everywhere collapses
the taxonomy back to Python's situation, which is fine at the application edge
and wrong in a library.

**C++.** The most fragmented story, and instructive because of it: exceptions,
error codes, `errno`, and since C++23 `std::expected<T, E>` all coexist in one
codebase. Exceptions can be disabled entirely by a compiler flag, which means
a library that throws is unusable in half the embedded world, which is why so
much C++ returns codes. `noexcept` is a genuine contract — violate it and the
program calls `std::terminate` rather than propagating, which is the loudest
category-3 behaviour in this lab. The lesson to take back to Python: when your
language offers four mechanisms, the taxonomy has to be written down, because
the language will not imply one.

**Java.** The only language here that put the category distinction into the
type system as *checked exceptions* and the only one whose ecosystem then
largely rejected it. That history is the lesson. Checked exceptions are
category 1 made mandatory; they failed in practice because the granularity
was wrong (`throws IOException` on everything) and because the escape hatch —
wrap in a `RuntimeException` — was easier than the correct handling. Modern
Java code is mostly unchecked, with the same discipline problem Python has,
plus one hazard of its own: `catch (Exception e)` also catches
`InterruptedException`, and swallowing that one breaks cancellation for
everything above you. Restore the flag or rethrow.

## The experiment

Two parts. The second one will change how you read code.

**1. Taxonomy audit under fuzz.** Run schemathesis (topic 6's tool, borrowed
early) against the API. Every response it produces gets classified against one
invariant: **no input the schema permits may produce a 5xx.** Every 5xx is a
miscategorised error — either a real bug to fix, or a caller-actionable
condition wearing the wrong clothes. Record the count and the distinct
operations before and after your fixes.

**2. The silent failure.** The orders repository ships with a deliberately
realistic anti-pattern, selected by `ERROR_MODE=swallow`:

```python
try:
    result = await session.execute(stmt)
    return list(result.scalars())
except Exception:            # "defensive"
    logger.warning("order lookup failed")
    return []
```

Bring the stack up and confirm `GET /customers/1/orders` returns real data.
Then cut the database at the proxy and hit it again. Record four things:
HTTP status, response body, response **latency**, and what your k6 error rate
says. Then run the same load against `ERROR_MODE=none` (no `except` at all)
and `ERROR_MODE=correct` (catch only `sqlalchemy.exc.OperationalError`,
re-raise as `Unavailable(retry_after=1)`, mapped to 503 by the single
exception handler).

The row that matters is the latency column, and it is the reason this topic
sits in the layer that is supposed to be about clean code: the swallowing
variant is not just wrong, it is **wrong and fast**, which is precisely the
signature that makes it invisible on a dashboard built around error rate and
p99.

## How to run

```
cd 08-craft/lab && docker compose up -d
docker compose exec api make seed
docker compose exec toxiproxy /toxiproxy-cli create -l 0.0.0.0:5433 -u postgres:5432 pg

docker compose --profile load run --rm k6 run /load/t3_errors.js      # healthy baseline

docker compose exec toxiproxy /toxiproxy-cli toxic add pg -t timeout -n cut -a timeout=1
docker compose --profile load run --rm k6 run /load/t3_errors.js      # database "gone"
docker compose exec toxiproxy /toxiproxy-cli toxic delete pg -n cut

# repeat the two runs above with each variant
ERROR_MODE=none    docker compose up -d --force-recreate api
ERROR_MODE=correct docker compose up -d --force-recreate api

docker compose --profile tools run --rm tools schemathesis run http://api:8000/openapi.json
```

`timeout=1` means one millisecond: the toxic drops the connection almost
immediately, which is the "database is gone" case. Making it *slow* instead is
topic 7, and the two produce opposite latency signatures.

The three variants are one `if` in `lab/api/app/repositories/orders.py`
(`orders_for_customer`), selected by `ERROR_MODE`; the taxonomy and the single
HTTP translation are in `lab/api/app/core/errors.py`. `ERROR_MODE` is validated
at startup, so a typo fails loudly instead of silently meaning `swallow` and
quietly invalidating a row of the table.

`/load/t3_errors.js` counts `empty_ok_rate` -- 200s with an empty body -- as an
explicit metric, because neither `http_req_failed` nor p99 can see this failure.
If your production dashboard has no equivalent counter, it cannot detect this
incident either, which is question 1.

### The six-language half, all of it native

The language section above names a different mechanism per runtime. Each one is
a self-contained program that reproduces the failure, prints evidence, applies
the fix and prints evidence the fix worked -- so the contrast is in one output
rather than across two files.

```
python3 python/exception_groups.py
node nodejs/unhandled_rejection.js
cd golang && go run errors_as_values.go
cd rust/taxonomy && cargo run
g++ -std=c++23 -O2 -o /tmp/t3_cpp cpp/four_mechanisms.cpp && /tmp/t3_cpp
cd java && javac ErrorTaxonomy.java -d /tmp/t3java && java -cp /tmp/t3java ErrorTaxonomy
```

| File | The mechanism it makes visible |
|---|---|
| `python/exception_groups.py` | `except Exception` around a `TaskGroup` catches the *group*, so every `isinstance` check fails and three categories collapse into one 500. Also `from e` / implicit context / `from None`, with `__suppress_context__` shown, because `from None` does not detach the original -- it only stops the traceback printer showing it |
| `nodejs/unhandled_rejection.js` | fire-and-forget `async` work swallows its own rejection by construction; the global `unhandledRejection` handler that "fixes" it converts a loud crash into a silence. `new Error(msg, { cause })` is Node's `raise ... from` |
| `golang/errors_as_values.go` | sentinel + `errors.Is` versus type + `errors.As`, and Go's own failure mode: four layers of `%w` that name the call stack and add no cause. The retry budget comes *out* of the error as a `time.Duration` |
| `rust/taxonomy/` | `Box<dyn Error>` collapses the taxonomy back to Python's situation; the typed enum keeps it, and `#[non_exhaustive]` prices the promise. `panic!` is the separate category-3 mechanism |
| `cpp/four_mechanisms.cpp` | exceptions, error codes, `errno` and C++23 `std::expected` side by side -- only the last puts the error in the signature. `noexcept` calls `std::terminate`; the file compiles with the `-Wexceptions` warning **deliberately**, because that warning is the demonstration |
| `java/ErrorTaxonomy.java` | checked exceptions plus the wrap-in-RuntimeException escape hatch that beat them, then the `catch (Exception e)` that swallows `InterruptedException` and breaks cancellation for everything above it -- measured on a virtual thread, with and without restoring the flag |

All six compile and run on this machine (Apple clang 21, Go 1.24, rustc 1.97,
JDK 21, Node 24, Python 3.13). The C++ file needs `-std=c++23` for `<expected>`
and falls back to running the other three mechanisms if your toolchain lacks it.

**Blocked on this machine, with the exact unblock command:**

| What | Why | Unblock |
|---|---|---|
| every `docker compose` line | the Docker daemon is not running | start Docker Desktop |
| `k6` on the host | k6 is not installed | run it through compose as shown, or `brew install k6` |

The taxonomy itself is importable and testable without any of that:
`cd 08-craft/lab/api && DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -m pytest tests/unit -q`.
## Predict, then record

Predict first, in writing: with the swallowing `except` in place and the
database unreachable, what status code does the endpoint return, and is it
*faster* or *slower* than the healthy baseline? Write both down. Then predict
which of your existing production dashboards would show anything at all.

| Variant | status | p99 latency | k6 error rate | body |
|---|---|---|---|---|
| healthy | | | | |
| DB cut, `ERROR_MODE=swallow` | | | | |
| DB cut, `ERROR_MODE=none` | | | | |
| DB cut, `ERROR_MODE=correct` (503) | | | | |

| schemathesis run | 5xx count | distinct failing operations |
|---|---|---|
| before fixes | | |
| after fixes | | |

**What would mean the experiment is broken rather than your prediction wrong:**

- **The swallowing variant is *slower* than baseline rather than faster.**
  Your toxic is `latency`, not `timeout`. A timeout toxic drops the
  connection, so the call fails fast; a latency toxic makes it slow. Both are
  interesting and they are different experiments — confusing them teaches
  exactly the wrong lesson about what silent failure looks like on a graph.
- **The swallowing variant returns 500 anyway.** Something above the
  repository is also raising — most likely response validation on an empty
  list, or a second query outside the `try`. Find it; it is a free lesson in
  how narrow the blast radius of one bad `except` actually is.
- **schemathesis reports zero 5xx on the first run against a real service.**
  Check that it generated requests at all (`-v`, or `--report junit` and read
  the report) and that your endpoints are not all behind auth it could not
  satisfy. A schema with no examples and a 401 on everything produces a clean
  run that means nothing.
- **The `correct` variant's 503s do not show up in k6's error rate.** Check
  what your k6 script counts as a failure — the default `http_req_failed`
  threshold treats any non-2xx as failed, but a custom `check()` that only
  looks for a JSON body will happily pass a 503 with a JSON body.

## Answer before moving on

1. The swallowing `except` returned `200 []` faster than the healthy path.
   Name the *monitoring* signal that would have caught it, and explain why
   neither error rate nor p99 latency did.
2. "Define errors out of existence." Take one endpoint in your production
   service and rewrite its contract so that a currently-possible error becomes
   impossible. What did you give up to get that?
3. When is `except Exception: log; raise` correct, and when is it pure noise?
4. Category 2 says "the same call might succeed later, unchanged." Give an
   error that looks transient, is retryable in one caller and a bug in
   another, and say what the API should do about it.

## Next up

[Topic 4 — What belongs at each test level, and the mock that lies](../04-test-levels-and-the-mock-that-lies/README.md):
you just watched a broken database produce a green dashboard. Now watch a
green test suite do the same thing.
