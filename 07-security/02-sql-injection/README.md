# Layer 7 · Topic 2 — SQL injection as a string-building failure

### The takeaway (read this first)

**The one idea:** SQL injection is not "attacker put a quote in a field."
It is that **you built one string that mixes code (SQL) and data (user
input), and the parser cannot tell which is which.** Parameterization fixes
it not by escaping harder but by *never putting the data in the code string
at all* — the query and the values travel to Postgres in separate protocol
messages, so there is no string for the attacker's input to break out of.

**Why it matters in practice:** Injection sits at **A05** in the OWASP Top
10:2025, down from A03 in 2021 — not because it stopped mattering but
because parameterized-query libraries became the default, which is itself
the lesson: the mechanism-level fix worked so well it demoted the whole
category. It is still catastrophic where it survives: string-built SQL in a
reporting endpoint, a dynamic `ORDER BY`, a search filter someone assembled
with an f-string under deadline.

**You'll know it landed when:** you can look at `f"SELECT * FROM users WHERE
email = '{email}'"` and *see the parse* — see exactly where `email = "'
OR '1'='1"` re-lexes the string into `... WHERE email = '' OR '1'='1'` — and
you can explain why `cur.execute("... WHERE email = %s", [email])` is
categorically different rather than just "the safe way."

## The concept

A SQL statement is text that Postgres **lexes into tokens, then parses into
a tree**. When you build the statement with an f-string, the user's input
becomes part of that text *before the lexer ever sees it*, so a `'` in the
input is a real string-literal-ending quote to the parser — the data has
been promoted to code. Escaping (doubling the quotes) tries to prevent that
promotion character by character, and it is a losing game: encodings, `E''`
strings, edge cases, and the one code path that forgot.

Parameterization changes the **protocol**, not the escaping. Postgres has
two ways to run a statement:

- **Simple Query** (`Q` message): one string, parsed and executed. This is
  the one an f-string ends up in, and it also permits multiple statements
  separated by `;`, which is where `; DROP TABLE` stops being a joke.
- **Extended Query**: `Parse` carries `SELECT ... WHERE email = $1` and is
  turned into a parse tree *with a typed placeholder in it*. `Bind` carries
  the value, as a length-prefixed byte string in its own message field.
  `Execute` runs the already-parsed plan.

Read that sequence twice, because it contains the entire answer: at the
moment Postgres decides what this statement *means*, the attacker's bytes
are not in the buffer being parsed. They arrive afterward, into a slot whose
type is already fixed. There is no string to break out of because at parse
time there was no string. That is why parameterization is a mechanism: it
removes the mixing, it does not police it.

The corollary you must internalize: **parameters bind values, not
identifiers.** A placeholder is a slot in a parse tree, and a table name, a
column name or a sort direction is not a slot — it is *structure*, decided
before the tree exists. So `ORDER BY {col} {direction}` is injectable no
matter how carefully you parameterized the `WHERE`. The fix there is an
allowlist: map the user's `sort=name` to a known-good column through a dict,
because the input is choosing code, and the only safe way to let input
choose code is to constrain it to a fixed menu you wrote.

## How each language actually gets there

All six, and this is one of the topics where they genuinely differ —
because the variable is **where the binding happens**, and every client
library answers that differently. Two of these six do not even use the
extended protocol by default.

**Python (psycopg3).** `cur.execute("... WHERE email = %s", [email])` uses
server-side binding by default in psycopg3 — the value never touches the SQL
string on the client either. This is a real change from psycopg2, which
did client-side interpolation with `mogrify` and produced a single
fully-formed string; safe if `adapt()` was correct, but a different trust
model. For identifiers, `psycopg.sql.Identifier` composes structure safely —
but it composes from *your* strings, so the allowlist still has to gate what
the user may select. With SQLAlchemy the leak is `text()` with an f-string
inside it.

**Node (`pg`).** Always extended protocol for parameterized calls: `$1`
placeholders, values sent in `Bind`. The seam worth knowing is the
template-literal libraries — `` sql`SELECT ... WHERE email = ${email}` ``
in `postgres.js` *looks* like interpolation and is in fact a tagged template
that parameterizes. The same-looking expression passed to `pool.query()`
directly is real interpolation and is injectable. Two nearly identical
character sequences, opposite safety properties: know which one your file
is using.

**Go (`database/sql` + `pgx`).** `db.Query("... WHERE email = $1", email)`
prepares and binds. Go's specific trap is the placeholder dialect —
`database/sql` documents `?` and Postgres wants `$1`; people who have
switched drivers reach for the wrong one, get an error, and "fix" it by
building the string. Go also makes the vulnerable version marginally harder
to write by accident, because `fmt.Sprintf` into a query is a visible extra
call rather than an f-string's two characters.

**Rust (`sqlx`).** The most different answer in the set: the `query!` macro
sends your SQL to a live database **at compile time**, gets back the
parameter and result types, and fails the build if they do not match. That
is not merely parameterization, it is the query being type-checked before
the binary exists — and it means the injectable version (`format!` into
`sqlx::query`) is a deliberate departure from the ergonomic path rather
than the default one. Rust earns its place here for exactly this: it is the
only language in the lab where the *safe* path is also the *convenient*
one because the compiler is involved.

**C++ (libpq).** The one talking to the wire with nothing in between:
`PQexec` sends a Simple Query — one string, multi-statement allowed —
while `PQexecParams(conn, "... WHERE email = $1", 1, NULL, values, ...)`
sends Parse/Bind/Execute. You can read the two functions' signatures and
see the entire security property of this topic in the difference between
them: one takes a string, the other takes a string *and an array of
values*. Every other language in this list is a wrapper over this
distinction. C++ is also the one with no guardrail at all: nothing warns
you, and `std::string query = "... '" + email + "'"` compiles cleanly.

**Java (JDBC).** `PreparedStatement` with `?` placeholders, and a piece of
history worth carrying: the *driver* decides whether binding is server-side.
The PostgreSQL JDBC driver binds server-side (after a few executions it
even switches to a named prepared statement), while MySQL Connector/J
historically defaulted to **client-side** rewriting — the placeholders were
substituted in the driver and a single assembled string went to the server.
Same API, same `PreparedStatement` class, different protocol underneath.
The lesson generalises: "I used a prepared statement" is a claim about your
code, not about the wire, and only the wire decides.

## The experiment

Uses the shared [`lab/`](../lab/README.md) stack. Two endpoints on `api`,
each with a `SQL_MODE` switch:

- `GET /search?email=` — `vulnerable` builds the string with an f-string;
  `parameterized` uses `%s` binding.
- `GET /list?sort=` — the identifier case. `parameterized` still
  concatenates the column into `ORDER BY`; `parameterized_allowlist` maps
  `sort` through a dict of four known columns and rejects anything else.

Four payload families, fired at both modes:

1. **Boolean tautology** — `' OR '1'='1` — the lookup/auth bypass. Measure
   **rows returned**.
2. **`UNION SELECT`** — pull `api_keys.key_hash` out through the `/search`
   result shape. Measure **rows returned from the other table**.
3. **Boolean-blind** — `' AND (SELECT substr(key,N,1) FROM api_keys WHERE
   user_id=1) = 'X` — the endpoint returns only "found" or "not found", and
   you recover the 32-character key one character at a time. Measure
   **requests needed to extract 32 characters** and **wall-clock seconds**.
   This is the one that matters most for calibration: it is the number that
   tells you whether "it only returns a boolean" is a mitigation.
4. **Identifier injection** — `sort=(SELECT ...)`, and `sort=name; --`.

Also run each payload against the `parameterized` mode and record what
Postgres *does* with the literal, because "returns zero rows" and "raises an
error" are different answers with different diagnostic meanings.

### How you'd know the fix is fake

Two shapes, both common. **First**, an endpoint that parameterizes the
`WHERE` and concatenates the `ORDER BY` looks fixed to a scanner and to a
code reviewer skimming for `%s` — the injectable half is twelve lines lower.
**Second**, a WAF or an input filter that strips `'` and the word `UNION`
makes every payload in this file fail while the endpoint remains injectable
by any payload you did not think of. If your evidence that SQLi is fixed is
"my payloads stopped working" rather than "the value is not in the parsed
string," you have tested your payload list, not your code.

## How to run

Each program is self-contained: it bootstraps its own ephemeral database (or
in-memory SQLite for Node), seeds three users and a secret 32-char key, then
runs all four payload families in `vulnerable` and `parameterized` modes and
prints the measured leak — including the blind-extraction request count.

```
# Real Postgres on localhost:5432 (the lab machine has one):
python3 python/sqli.py
cd golang && GOFLAGS=-mod=mod GOPROXY=off go run sqli.go && cd ..
LIBPQ=/opt/homebrew/opt/libpq; g++ -O2 -std=c++17 -I$LIBPQ/include -L$LIBPQ/lib \
  -o /tmp/cpp_sqli cpp/sqli.cpp -lpq && /tmp/cpp_sqli

# No database service needed — built-in SQLite:
node nodejs/sqli.js
```

Two languages need a one-time online fetch on this machine (neither the sqlx
crates nor a JDBC driver are cached here), then run against the same Postgres:

```
cd rust && cargo fetch && cargo run && cd ..           # sqlx not in cargo cache
cd java && javac Sqli.java && \
  curl -L -o /tmp/pg.jar \
    https://jdbc.postgresql.org/download/postgresql-42.7.4.jar && \
  java -cp .:/tmp/pg.jar Sqli && cd ..                  # pg JDBC driver not cached
```

`java/Sqli.java` compiles with the JDK alone (`java.sql.*` is standard); only
running needs the driver jar. The single-shot `curl -sG localhost:8007/...`
form belongs to the compose `api` once Docker is up.

## Predict, then record

Before running:

1. Against `parameterized`, what does the server *do* with the literal
   string `' OR '1'='1` — error, zero rows, or all rows? Answer in terms of
   which protocol message that byte string ends up in.
2. How many HTTP requests does the blind extraction of a 32-character
   hex-ish key take, and how did you derive that number? (Consider both the
   one-character-per-request approach and a binary search per character.
   The ratio between those two answers is the point.)
3. Does parameterizing the `WHERE` in the `/list` endpoint close the `sort`
   injection? Why or why not — at the level of the parse tree.

| Payload | mode `vulnerable`: status / rows returned | mode `parameterized`: status / rows returned |
|---|---|---|
| `' OR '1'='1` |  |  |
| `UNION SELECT` cross-table |  |  |
| `sort=` identifier injection |  | (and with `parameterized_allowlist`) |

| Blind extraction | value |
|---|---|
| requests to recover 32 characters, one char per request |  |
| requests to recover 32 characters, binary search per char |  |
| wall-clock seconds at the lab's default rate |  |
| bytes of response that differ between "true" and "false" |  |

**What would mean the experiment is broken, not the prediction:** if the
`vulnerable` endpoint does *not* dump all users on `' OR '1'='1`, check that
you actually built the string with an f-string and did not accidentally pass
it as a parameter — and that Postgres is not rejecting the whole statement
on a type error before the injection triggers (try the payload against a
`text` column, not an `int`). If `parameterized` returns an *error* rather
than zero rows, your driver may be interpreting a `%` in the literal as a
placeholder: pass params as a sequence, and remember psycopg wants `%s`, not
an f-string. If the blind payload succeeds instantly and identically every
time, you may be reading the non-blind path by accident — the tell of a real
blind channel is that each request answers exactly one yes/no, so the
request count should be roughly linear (or logarithmic) in the secret
length, never constant.

## Answer before moving on

1. Parameterization is described as separating code from data. State the
   general principle this is a special case of — the same principle that
   explains why XSS (Topic 3) and command injection are the *same class of
   bug* as SQLi.
2. An ORM parameterizes everything by default. Give the exact code shape in
   SQLAlchemy that reintroduces injection anyway, and say why the ORM did
   not save you.
3. Why can you not parameterize a table or column name, at the protocol
   level? Answer with the Parse/Bind sequence, not with "the library does
   not support it."
4. The JDBC note says a `PreparedStatement` may be bound client-side by the
   driver. If binding happens in the driver rather than the server, is the
   code still safe from injection? Say what property it now depends on, and
   what would have to go wrong for it to fail.

## Next up

[Topic 3 — XSS by output context, and where you must never put a token](../03-xss-and-output-context/README.md):
the identical bug class, with the browser as the parser you are fooling —
and one twist SQL does not have, which is that the correct escaping depends
on *where in the document* the value lands.
