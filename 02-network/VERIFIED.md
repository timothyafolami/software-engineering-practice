# Layer 2 · Verification record

**What this file is.** An independent check that the code in this layer
*compiles and executes on this machine*, run by an agent that did not write it,
using the exact commands in each topic's `How to run`. It records that the
programs run and that each one measures what its own prose says it measures.

**What this file is not.** It is not a record of anything learned. Every
`Predict, then record` table in the seven topics is still blank, and it stays
blank — those are the reader's exercise, and a table filled in by someone else
is worth nothing. Numbers quoted below are real because they were captured from
these runs; they are evidence that a program produced output, not results.

Verified **2026-08-19**. The Docker daemon was down for the first pass, so
everything in `lab/` was recorded BLOCKED. It came up the same day and a second
pass ran all of it — see **[Unblock pass — Docker daemon up](#unblock-pass--docker-daemon-up--2026-08-19)**
at the end of this file, which supersedes the BLOCKED rows below and is where
the real `lab/` output lives.

## The machine

| | |
|---|---|
| OS | macOS 27.0, Darwin arm64 (Apple M1) |
| Python | 3.13.5 (`httpx` 0.28.1, `aiohttp` 3.11.10, `h2` 4.3.0 present) |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 (cargo, `--release`) |
| C++ | Apple clang 21.0.0 — `-std=c++17 -pthread` |
| Java | OpenJDK 21.0.2 LTS (virtual threads available) |
| Postgres | `pg_isready` → `/tmp:5432 - accepting connections` (host server up) |
| Docker | CLI 28.1.1 → **daemon up on 2026-08-19**: server 29.5.3, `linux/aarch64`, 4 CPUs, 5.1 GB. Compose v5.1.4 |
| k6 | **not installed on the host**, and not needed — every k6 line runs in the `load` container (`grafana/k6:2.2.0`) |

## Coverage against each topic README

Each topic's `How each language actually gets there` section states which
languages that topic uses. Checked folder by folder:

| Topic | README says | Files present | |
|---|---|---|---|
| 01 what-a-connection-actually-costs | six | python ×2, nodejs, golang, rust, cpp, java | complete |
| 02 connection-pooling-and-pool-exhaustion | six | python ×2, nodejs, golang, rust, cpp, java | complete |
| 03 timeouts-as-a-first-principle | six | python ×2, nodejs, golang, rust, cpp, java | complete |
| 04 keep-alive-across-a-load-balancer | four, *"Rust and C++ are omitted because…"* | python, nodejs, golang, java | complete as specified |
| 05 dns-ttls-and-the-dead-ip | six | python ×2, nodejs, golang, rust, cpp, java | complete |
| 06 head-of-line-blocking-and-multiplexing | three, *"the mechanism lives in the protocol"* | python, golang, nodejs | complete as specified |
| 07 see-it-on-the-wire | six clients + harness | six `syn_client`s, `pools_as_advertised.py`, `sniff/` ×2 | complete |

**No topic is incomplete.** Topics 4 and 6 ship fewer than six languages
because their own READMEs argue for fewer, in the sentence quoted above.

## Every program, run

Thirty-six single-file programs. All exit 0, none exceeds 40 s, none prints a
traceback, panic, or unhandled error. Commands are the ones in each topic's
`How to run`, run verbatim from that topic's directory.

| Program | Command | Status | Wall |
|---|---|---|---|
| 01 `python/cold_vs_warm_client.py` | `python3 python/cold_vs_warm_client.py` | RAN | 4 s |
| 01 `python/handshake_phases.py` | `python3 python/handshake_phases.py` | RAN | 2 s |
| 01 `nodejs/cold_vs_warm_client.js` | `node nodejs/cold_vs_warm_client.js` | RAN | <1 s |
| 01 `golang/max_idle_conns_per_host.go` | `go run max_idle_conns_per_host.go` | RAN | 1 s |
| 01 `rust/connection_reuse` | `cargo run --release` | RAN | 1 s |
| 01 `cpp/connection_syscall_cost.cpp` | `c++ -O2 -std=c++17 -o /tmp/conncost …` | RAN | 1 s |
| 01 `java/ConnectionReuse.java` | `javac … && java -cp /tmp/javabuild ConnectionReuse` | RAN | 1 s |
| 02 `python/pool_full_behaviour.py` | `python3 python/pool_full_behaviour.py` | RAN | 9 s |
| 02 `python/littles_law_and_shedding.py` | `python3 python/littles_law_and_shedding.py` | RAN | 34 s |
| 02 `nodejs/agent_queue_is_invisible.js` | `node nodejs/agent_queue_is_invisible.js` | RAN | 7 s |
| 02 `golang/fails_open_by_default.go` | `go run fails_open_by_default.go` | RAN | 3 s |
| 02 `rust/pool_permit` | `cargo run --release` | RAN | 22 s |
| 02 `cpp/hand_rolled_pool.cpp` | `c++ -O2 -std=c++17 -pthread …` | RAN | 4 s |
| 02 `java/PoolCeilingVsThreads.java` | `javac … && java … PoolCeilingVsThreads` | RAN | 28 s |
| 03 `python/deadline_budget.py` | `python3 python/deadline_budget.py` | **FIXED-THEN-RAN** | 9 s |
| 03 `python/retry_storm_and_budget.py` | `python3 python/retry_storm_and_budget.py` | RAN | 37 s |
| 03 `nodejs/abort_signal_and_the_blocked_loop.js` | `node nodejs/abort_signal_and_the_blocked_loop.js` | RAN | 3 s |
| 03 `golang/context_deadline_chain.go` | `go run context_deadline_chain.go` | RAN | 2 s |
| 03 `rust/cancellation_safety` | `cargo run --release` | RAN | 3 s |
| 03 `cpp/poll_deadline.cpp` | `c++ -O2 -std=c++17 -pthread …` | RAN | 3 s |
| 03 `java/TimeoutIsAnInterrupt.java` | `javac … && java … TimeoutIsAnInterrupt` | RAN | 4 s |
| 04 `python/idle_timeout_defaults.py` | `python3 python/idle_timeout_defaults.py` | RAN | 16 s |
| 04 `nodejs/keepalive_vs_headers_timeout.js` | `node nodejs/keepalive_vs_headers_timeout.js` | RAN | 20 s |
| 04 `golang/no_idle_timeout_by_default.go` | `go run no_idle_timeout_by_default.go` | RAN | 16 s |
| 04 `java/IdleTimersOnBothSides.java` | `javac … && java … IdleTimersOnBothSides` | RAN | 16 s |
| 05 `python/pool_outlives_dns.py` | `python3 python/pool_outlives_dns.py` | RAN | 8 s |
| 05 `python/executor_starvation.py` | `python3 python/executor_starvation.py` | RAN | 6 s |
| 05 `nodejs/lookup_vs_resolve_threadpool.js` | `node …` and `UV_THREADPOOL_SIZE=16 node …` | RAN | 1 s |
| 05 `golang/two_resolvers_one_binary.go` | `go run two_resolvers_one_binary.go` | RAN | <1 s |
| 05 `rust/blocking_pool_resolver` | `cargo run --release` | RAN | 2 s |
| 05 `cpp/getaddrinfo_blocks.cpp` | `c++ -O2 -std=c++17 -pthread …` | RAN | 1 s |
| 05 `java/DnsCacheTtl.java` | `java … DnsCacheTtl` and `java -Dsun.net.inetaddr.ttl=0 …` | RAN | <1 s |
| 06 `python/h1_pool_vs_h2_streams.py` | `python3 python/h1_pool_vs_h2_streams.py` | RAN | 5 s |
| 06 `golang/h2_queues_where_h1_pools.go` | `go run h2_queues_where_h1_pools.go` | **FIXED-THEN-RAN** | 5 s |
| 06 `nodejs/http2_session_is_one_connection.js` | `node nodejs/http2_session_is_one_connection.js` | RAN | 2 s |
| 07 `python/pools_as_advertised.py` (drives all six clients) | `python3 python/pools_as_advertised.py` | RAN | 4 s |

Two programs resolve real names over the network (`01/python/handshake_phases.py`
against `example.com`, `05/golang/two_resolvers_one_binary.go`). Both ran with
working DNS here; both are written to record a "did not resolve" line rather
than crash offline, and that path was not exercised.

## What was blocked, and the exact unblock

Every row here was blocked on one thing — the Docker daemon — and every row
has since been run. Kept for the record; the results are in the
**Unblock pass** section below.

| Thing | Why it was blocked | Status now |
|---|---|---|
| The whole `lab/` compose stack — every `docker compose` line in all seven topics' `How to run` | Docker daemon was down: `docker info` failed | **FIXED-THEN-RAN** — five build/config defects had to be fixed first |
| All six k6 scripts (`lab/scripts/topic1–6.js`) | k6 runs only inside the `load` container; no host k6 | **RAN** (`docker compose run --rm load run /scripts/topicN.js`); host k6 is still absent and still not needed |
| `07/sniff/capture.sh`, `07/sniff/sockets.sh` | `tcpdump -i any`, `ss` and `tc` are Linux-only | **RAN** — all four `capture.sh` modes and all of `sockets.sh` |
| Image pulls for `api`/`upstream` builds (`python:3.14-slim` + pinned wheels) | no daemon, so nothing was built | **FIXED-THEN-RAN** — one pinned wheel does not exist; see fix 4 |

## What was fixed

1. **`lab/docker-compose.yml` — `grafana/k6:2` is not a published tag.**
   Queried Docker Hub directly: `grafana/k6:2` returns HTTP 404. The registry
   has `v2` as the short form and `2.0.0` / `2.1.0` / `2.2.0` as releases.
   Pinned to `grafana/k6:2.2.0` and recorded the reason in the pin table in
   `lab/README.md`. The other three images were checked the same way and exist:
   `postgres:18.6-alpine` (200), `nginx:1.29-alpine` (200),
   `ghcr.io/shopify/toxiproxy:2.12.0` (200), `alpine:3` (200).

2. **`06/golang/h2_queues_where_h1_pools.go` — the banner contradicted the
   program's own measurement.** The file's header comment and its printed
   title both asserted that Go's HTTP/2 transport *"keeps a SINGLE connection
   per host and QUEUES"* the excess. Its own output on go1.24.5 reports
   `connections accepted 18` for the h2 run, and the body text further down
   already says so honestly. Rewrote the header comment and the banner to tell
   the reader to *measure* it and read the connection count, so the top of the
   file no longer disagrees with the bottom. Re-ran: `gofmt` clean, output
   consistent end to end.

3. **`03/python/deadline_budget.py` — 50 lines of Python traceback before the
   report.** When a client's deadline fires it closes the socket mid-response;
   `ThreadingHTTPServer` printed a full `BrokenPipeError` / `ConnectionResetError`
   traceback for each, burying the output. Added a `handle_error` override that
   absorbs exactly those two — they *are* the phenomenon this file demonstrates,
   seen from the server side — and lets anything else through. Re-ran: clean
   output, exit 0.

Nothing else needed fixing. In particular, no Darwin-portability defect was
found anywhere in this layer: no `<sys/epoll.h>`, no `/proc`, no cgroup paths in
any source file — the four C++ files say so in their header comments and the
grep agrees. This is the trap Layer 1 fell into, and Layer 2 did not.

## Claims checked against the code that makes them

The point of this pass, and the reason the two fixes above exist. Spot-checked
every program's stated claim against what it actually does:

- **`05/python/executor_starvation.py`** resolves concurrently, on purpose, and
  says why in a docstring: measuring one lookup at a time would let the executor
  drain between measurements and hide the queue. The experiment is correct for
  its claim.
- **`01/python/handshake_phases.py`** really does drive a handshake through an
  `ssl.MemoryBIO` and parse the captured record — the ClientHello size and the
  `supported_groups` list are read out of real bytes, not quoted.
- **`02/cpp/hand_rolled_pool.cpp`** runs the two timeout policies against the
  same fixed workload, and the "2 s never fires" claim holds in the captured
  table (`timeout_2s` 40 served / 0 timed out, `timeout_300ms` 12 / 28).
- **`04/nodejs/keepalive_vs_headers_timeout.js`** measures the FIN rather than
  quoting the setting, and its "configured is not observed" warning is derived
  from that measurement.
- **`03/nodejs`'s blocked-loop phase** blocks the loop with a `while` on
  `Date.now()` — that loop *is* the block, not a mis-measured benchmark.
- **`07/python/pools_as_advertised.py`** counts `accept()` on its own server,
  which is the claim it makes, and drove all six clients here with no `BLOCKED`
  row.

Two mismatches survive as **flags, not fixes**, because both are teaching prose
rather than a run command and the topic README already contradicts itself in the
reader's favour:

- `06/README.md`, *The experiment*: "Toxiproxy applies 0%, 1% and 5% packet
  loss". Toxiproxy has no packet-loss toxic — it is a TCP-level proxy. The
  *How to run* section three screens later says exactly that and routes the loss
  half through `tc netem` in the `sniff` sidecar. Read the run section; the
  experiment paragraph is wrong.
- `06/README.md`, *How each language actually gets there*, the Go paragraph:
  states the transport "**queues** new requests on that connection rather than
  dialling a second one". On go1.24.5 here it dials. The *How to run* section
  names this contradiction explicitly, and the Go program now measures it.

## No fabricated numbers

- All seven `Predict, then record` tables were re-read and are **blank**, as
  they should be.
- Grepped the seven topic READMEs for figures presented as observed. Every
  number with a unit is either a configuration value being set (`keepalive 32`,
  `RATE 200`), arithmetic derived on the page (the Little's Law worked example,
  the 3000 ms budget breakdown), or a hypothetical stated as one ("on a 30 ms
  path"). No measurement is quoted as fact in any topic README.
- Grepped all thirty-six programs for measured-looking constants inside print
  statements. Every hit was a hypothetical RTT or a config value; every reported
  measurement is computed at runtime.

## Real output, captured here

Included because it is evidence the programs execute, not because it is a
result. Your numbers will differ and that is the point.

`07/python/pools_as_advertised.py` — the six-language table Topic 1 claims:

```
  client                      conns   requests   verdict
  Python (httpx)                  1         30   pools: one connection for every request
  Node (undici/fetch)             2         30   partial: 15.0 requests per connection
  Go (net/http)                   1         30   pools: one connection for every request
  Rust (std::net)                 1         30   pools: one connection for every request
  C++ (libcurl)                   1         30   pools: one connection for every request
  Java (HttpClient)               1         30   pools: one connection for every request
```

`02/cpp/hand_rolled_pool.cpp` — one overload, five policies:

```
  policy           served  refused  timedout   created   max wait      wall
  unbounded            40        0         0        40        0 ms    107 ms
  block_forever        40        0         0         4      957 ms   1068 ms
  timeout_2s           40        0         0         4      961 ms   1072 ms
  timeout_300ms        12        0        28         4      215 ms    317 ms
  shed_when_full        4       36         0         4        0 ms    110 ms
```

`06/golang/h2_queues_where_h1_pools.go` on go1.24.5 — the number that forced
fix 2:

```
    h2, multiplexed
      connections accepted    18
      max streams on ONE conn  250   <- the ceiling that is not yours
```

## One note on the author's report

The report says the six k6 scripts are "all `constant-arrival-rate`".
`lab/scripts/topic2.js` uses `ramping-arrival-rate`, which is the right
executor for finding a knee and is equally open-model — so the code is correct
and the description of it was not. Recorded here so the next reader is not
surprised.

---

# Unblock pass — Docker daemon up · 2026-08-19

The daemon came up. Everything above that said BLOCKED has now been run by an
agent that did not write it, on the machine described in **The machine**, using
each topic's own `How to run`. This is the first time any of `lab/` has
executed, and it is the first time anyone could check whether these experiments
demonstrate what their prose claims. Several do not, and those are named here
rather than smoothed over.

Same rule as above: every number below was observed on this machine on this
date. The `Predict, then record` tables in all seven topics are still blank and
stay blank.

## The machine, restated

Docker server **29.5.3**, `linux/aarch64`, **4 CPUs / 5.1 GB** inside Docker
Desktop's linuxkit VM, Compose **v5.1.4**. Unrelated containers belonging to
the user (a `craft-lab` stack and an `interview-ai` stack, ~12 containers
including Celery workers) were running throughout and were **not** stopped;
they are part of why the arrival rates the topics document are unreachable
here. Compose project name left at the default `lab`, so the network is
`lab_default` exactly as Topic 5 requires.

## Defects found and fixed, in the order they stopped a run

Nothing in `lab/` had ever executed, so this is its first contact with a
runtime. Nine defects; five stopped the stack from starting, four made an
experiment measure the wrong thing.

1. **`lab/upstream/requirements.txt` — `hypercorn==0.18.1` does not exist.**
   `docker compose build` failed at `pip install` with
   `No matching distribution found for hypercorn==0.18.1`; the newest published
   release is `0.18.0`. Pinned to `0.18.0`. Both images then built.

2. **`lab/docker-compose.yml` — the Postgres volume mount path is wrong for
   Postgres 18.** `db` exited 1 before running any init script:
   *"there appears to be PostgreSQL data in: /var/lib/postgresql/data (unused
   mount/volume)"*. From 18 the official image puts `PGDATA` in a
   major-version subdirectory and refuses to start when it finds a mount at
   the old path. Changed to `pgdata:/var/lib/postgresql`. `db` now starts and
   `01-init.sql` runs — `select version()` reports PostgreSQL 18.6.

3. **`lab/docker-compose.yml` — two published host ports collide with an
   ordinary developer machine.** `db` published `5432:5432` (the Mac's own
   Postgres already holds it) and `lb` published `8080:8080`. Moved to
   `55432:5432` and `18080:8080`. Neither host port is referenced by any
   command in any topic — the topics reach both services through
   `docker compose exec` and over the compose network — so the container-side
   ports, which *are* load-bearing, are unchanged.

4. **`lab/docker-compose.yml` — the `load` container's `TARGET`, `RATE` and
   `DURATION` defaults silently overrode every script's own defaults.** This
   is the worst one, because it produced runs that looked fine. Compose set
   `TARGET: ${TARGET:-http://api:8000}`, `RATE: ${RATE:-200}`,
   `DURATION: ${DURATION:-60s}` on **every** run, so `__ENV.TARGET` and friends
   were always defined and the `__ENV.RATE || 150` fallbacks inside the scripts
   could never fire. Consequences, all observed:
   `topic4.js` — whose entire subject is the load balancer — drove `api:8000`
   directly, bypassing `lb`; `topic3.js` ran at 200 rps for 60 s instead of
   150 rps for 180 s, so the toxic its own instructions inject at t=60 s landed
   **after the load generator had stopped**; `topic5.js`'s 300 s window closed
   at 60 s; `topic6.js` likewise. Changed all three to `${VAR:-}`; an empty
   string is falsy in `__ENV`, so each script's own default now applies.
   Topic 3 was re-run from scratch after this fix and the earlier results were
   discarded.

5. **`lab/scripts/topic4.js` targeted `api`, not `lb`.** It imports the shared
   `get()`, which is hard-wired to `api:8000`, while its own header says
   *"Traffic goes to `lb` (port 8080), not to `api`"*. Rewritten to build its
   own URL from `__ENV.TARGET || 'http://lb:8080'` and to print the resolved
   target in `setup()`, which the runs below quote.

6. **`lab/docker-compose.yml` — `upstream` was never told `PROTO`.**
   `upstream/entrypoint.sh` switches uvicorn → hypercorn (h2c) on `PROTO`, but
   the variable was only on `api`. `PROTO=h2 docker compose up -d api upstream`
   therefore moved the client half of Topic 6 and left the server on HTTP/1.1.
   Added `PROTO` to both `upstream` services. Confirmed: with the fix,
   `docker compose logs upstream` says
   `Running on http://0.0.0.0:9000` from hypercorn under `PROTO=h2` and
   `Uvicorn running on http://0.0.0.0:9000` under `PROTO=h1`.

7. **Topic 2 had no way to inject the database latency its own experiment
   describes.** `02/README.md` says *"Toxiproxy adds 100 ms to the database
   path so `W` is a known constant"*, and Little's Law is the whole topic —
   but `toxiproxy.json` defined one proxy (`upstream`) and `DATABASE_URL`
   pointed straight at `db:5432`. With a container-local database `W` is under
   a millisecond, the pool ceiling is thousands of rps, and the single uvicorn
   worker's CPU saturates long before fifteen connections are busy. Added a
   `db` proxy on `0.0.0.0:8476` and routed `DATABASE_URL` through it.
   `docker compose exec db psql -U app` still reaches the database directly,
   which is what you want — the observer should not sit behind the fault.

8. **`lab/api/app.py` — `pool_waits` and `pool_wait_seconds` were structurally
   always zero.** `/checkout` timed the entry into
   `async with STATE["session"]()`. An `AsyncSession` is lazy: entering it
   allocates a Python object and does not touch the pool, so the timer measured
   nothing. Measured directly: a `/checkout` that took **1.85 s** wall clock
   through the 100 ms-latency proxy reported `pool_wait_ms: 0.06`. The two
   counters this topic exists to export would have read zero through an
   incident that was entirely pool waiting. Switched to
   `async with STATE["engine"].connect()`, whose `__aenter__` really does
   acquire. First call after restart now reports `pool_wait_ms: 1405.1`,
   second `0.21`.

9. **`lab/api/app.py` — `TIMEOUT_PROFILE=none` had a 10-second timeout.**
   The `none` branch called `call_upstream(..., timeout=None)`, and
   `call_upstream` treats `None` as *"pass no argument"*, so httpx applied the
   client default that `build_client()` sets to 10 s. Scenario 1 — the one that
   is supposed to hang forever and stay hung — instead failed in bounded 10 s
   chunks and recovered by itself. This is precisely the bullet in Topic 3's
   own broken-experiment list (*"you have a timeout you did not know about …
   find it"*), found in the lab's own code. Changed to
   `httpx.Timeout(None)`. The difference is in the table below and it is the
   whole point of the topic.

10. **`lab/api/app.py` — `POOL_PROFILE=shed` shed its own `/stats`.** The
    admission-control middleware ran before routing, so during the incident
    every attempt to read the counters that explain the incident returned
    `{"error":"shed"}`. Exempted `/stats` and `/healthz`. Verified mid-run:
    `{"inflight":40,"inflight_max":41,…,"shed":828}` read successfully while
    the same run was rejecting 90% of `/checkout`.

11. **Topic 5's two documented commands could not execute.**
    `docker network disconnect lab_default upstream` fails with
    `Error response from daemon: No such container: upstream` — the second
    argument is a container, and `upstream` is only a network alias; the
    container is `lab-upstream-1`. And `docker compose exec api sh -c "… dig
    +short upstream"` fails with `dig: not found` — the `api` image installs
    `iproute2 curl procps` only. Fixed the command in `05/README.md` and
    `lab/README.md`, and added `dnsutils` to `api/Dockerfile`. `dig +short
    upstream` inside `api` now answers `172.21.0.7`.

12. **Topic 5 could not observe its own failure through Toxiproxy.**
    `api` reaches `upstream` through `toxi` by design — correct for topics 1,
    3 and 6, wrong for 5. The name that moves is resolved by *toxiproxy*,
    which dials a fresh TCP connection per proxied connection and re-resolves
    immediately, so `api`'s pool holds sockets to `toxi`, which never moves.
    Made `UPSTREAM_URL` overridable and documented topic 5's run as
    `UPSTREAM_URL=http://upstream:9000 docker compose up -d api`. Also added a
    `KEEPALIVE_EXPIRY` knob to `build_client()` so the lab can run more than
    one row of Topic 5's own variant table. Neither change was enough to make
    the outage appear — see **Experiments that run but do not demonstrate
    their claim**.

Two run-command gaps were also fixed in the topic READMEs, because the run
block did not match the experiment the same file describes: Topic 1's 30 ms
Toxiproxy latency (described in *The experiment*, absent from *How to run* —
without it `COLD` and `WARM` are indistinguishable), and Topic 6's `PROTO`,
which has to precede the `up`, not the `run --rm load`.

## RAN, with real output

### Topic 1 — cold vs warm, 30 ms in front of `upstream`

Documented rate is 200 rps for 60 s. **On this machine that rate is not
usable**: k6 reported `dropped_iterations` of 6015 (COLD), 9741 (WARM) and
9354 (WARM_TUNED) against 60 s of load, which by `lab/README.md`'s own
coordinated-omission rule invalidates the histogram. Re-run at **40 rps for
30 s**, where `dropped_iterations` is 0:

| VARIANT | avg | med | p95 | p99 | max | clients built | estab in `api`, mid-run |
|---|---|---|---|---|---|---|---|
| COLD | 44.18 ms | 41.97 ms | 49.01 ms | **98.56 ms** | 221.52 ms | 1200 | 61 |
| WARM | 38.95 ms | 38.79 ms | 41.18 ms | **47.36 ms** | 103.83 ms | 1 | 65 |
| WARM_TUNED | 38.73 ms | 38.81 ms | 41.13 ms | **47.65 ms** | 77.08 ms | 1 | 66 |

1200 requests, 1200 clients under COLD against 1 under WARM. The established
count must be read *while k6 is still running* — the pool drains in seconds
and the same command afterwards reports 1 on all three variants. That note is
now in the topic's `How to run`.

### Topic 2 — pool exhaustion, `db` behind a 100 ms toxic

The documented ramp (50 → 600 rps over five minutes) run verbatim, four
profiles. `dropped_iterations` is large in all four because the ramp's top end
is far past this machine; the *knee* is the measurement and it arrives early,
so the runs are still informative. Little's Law with `W ≈ 200 ms` (two queries,
100 ms each) predicts a ceiling near 75 rps for `default` and 150 for `wide`.

| POOL_PROFILE | med | p95 | p99 | max | failed | pool_waits | inflight_max | `pg_stat_activity` idle |
|---|---|---|---|---|---|---|---|---|
| default | 30.11 s | 30.78 s | 31.32 s | 32.82 s | 69.96% | 10 232 | 4001 | 5 |
| wide | 30.17 s | 31.38 s | 32.30 s | 33.18 s | 41.03% | 20 195 | 4001 | 20 |
| fast_timeout | 8.65 s | 17.70 s | 55.87 s | 60 s | 88.48% | 5 826 | 1482 | 5 |
| **shed** | **417 µs** | **1.11 s** | **1.13 s** | **2.23 s** | 89.96% | 10 857 | **41** | 5 |

The topic's claim — *"the first three move the knee, only the fourth changes
the shape of the failure"* — holds exactly. `default` and `wide` fail as
30-second waits; `fast_timeout` moves the queue out of the pool and into the
event loop (p99 55.87 s while the pool timeout is 2 s); `shed` holds in-flight
at its threshold of 40 (observed max 41), answers in **milliseconds**, and
turns the failure into 98 517 explicit 503s. `pg_stat_activity` idle counts
match `pool_size` in every profile: 5, 20, 5, 5.

### Topic 3 — timeouts, and time to recovery

150 rps for 180 s, 20 s ± 2 s latency toxic injected at t=60 s and removed at
t=120 s, with an independent probe issuing one `/order` every two seconds for
240 s — past the end of the load, because the measurement is recovery.

| TIMEOUT_PROFILE | med | p95 | p99 | max | failed | recovery after removal at t=120 s |
|---|---|---|---|---|---|---|
| none | 3.90 ms | 60 s | 60 s | 60 s | 33.88% | **never** — probe still 0% ok, 40 s timeouts, at t=240 s |
| flat | 4.23 ms | 52.30 s | 60 s | 60 s | 46.74% | not by t=230 s; failures bounded at 5.01 s |
| budget | 2.08 s | 5.53 s | 57.79 s | 60 s | 63.87% | not by t=230 s; failures bounded at 1.01 s |
| **full** | 2.96 ms | 1.00 s | **1.34 s** | 2.43 s | 33.53% | **t≈120–130 s** — 80% ok in the first 10 s bucket after removal, 100% from t=130 s |

Under `none` the service stopped answering `/stats` entirely during the fault
window. Under `full` the counters all move: `retries` 399,
`retries_denied_by_budget` 1173, `breaker_open_rejections` 7899. This is the
topic's central claim — *"the critical measurement is time to recovery"* — and
it is now visible, but only after fix 4 (the load actually overlapping the
fault) and fix 9 (`none` actually having no timeout).

### Topic 4 — keep-alive across the load balancer

Three profiles, 10 rps for 180 s through `lb`, FIN/RST captured in the `sniff`
sidecar. k6 confirms its target in `setup()`:
`topic4 target = http://lb:8080  (must be the lb, not api:8000)`.

| KEEPALIVE_PROFILE | nginx 502s | nginx log | FIN/RST packets on :8000 | who closes |
|---|---|---|---|---|
| mismatched | **0** | 1763 × 200 | 12 | every `F` goes **Out** from `api` — backend's 5 s beats nginx's 60 s |
| ordered | 0 | 1764 × 200 | **0** | nobody, in a busy 180 s |
| ordered_bounded | 0 | 1763 × 200 | 68 | every `F` comes **In** from nginx — `keepalive_requests 50` rotating the pool |

The direction flip is real and is the mechanism. The 502 is not — see below.

### Topic 5 — the alias move

`upstream_b` started, `lab-upstream-1` disconnected from `lab_default` at
t≈49 s of a 300 s run at 50 rps. `/whoami` through the name confirms the swap:
`upstream_a` before, `upstream_b` (`172.21.0.7`) after. `dig +short upstream`
inside `api` returns `172.21.0.7`, `getent hosts upstream` agrees, and the
sniffer sees the container's DNS traffic on `lo` to `127.0.0.11`. `api`'s
`/etc/resolv.conf` here says `options ndots:0`, not the `ndots:5` the topic's
arithmetic assumes — worth reading before doing that arithmetic.

The error window: **3 of 15 001 requests** (max 10.02 s, httpx's own timeout),
recovery immediate. Same with `KEEPALIVE_EXPIRY=none`. That is not the outage
the topic is named after — see below.

### Topic 6 — multiplexing under loss

`FANOUT=20`, `UPSTREAM_BODY_BYTES=102400`. At the documented 50 rps every run
collapsed (`dropped_iterations` 4794–5196, p99 60 s). Re-run at **5 rps for
90 s**, where `dropped_iterations` is 0:

| run | med | p95 | p99 | max | failed | estab `api`→upstream path |
|---|---|---|---|---|---|---|
| h1, no netem | 47.26 ms | 57.74 ms | 96.64 ms | 132.80 ms | 0/451 | 6 |
| h2, no netem | 50.88 ms | 62.75 ms | 101.81 ms | 333.75 ms | 0/451 | 11 |
| h1, netem `loss 5% delay 40ms` | 391.05 ms | 682.37 ms | 971.97 ms | 2.09 s | 1/451 | 29 |
| h2, netem `loss 5% delay 40ms` | 377.82 ms | 661.82 ms | 935.67 ms | 1.34 s | 0/451 | 40 |

`tc` had to be installed in `sniff` before use; the qdisc was confirmed each
time (`qdisc netem … limit 1000 delay 40ms loss 5%`) rather than assumed.
`docker compose exec api sysctl net.ipv4.tcp_available_congestion_control`
returns **`reno cubic`** — **bbr is not available** in Docker Desktop's
linuxkit VM, which is the "record 'not available on this host'" case the topic
anticipates. The h1/h2 comparison itself did not work — see below.

### Topic 7 — on the wire

`sniff/capture.sh` and `sniff/sockets.sh` copied into the containers and run
there, all four modes:

| command | result |
|---|---|
| `capture.sh syns 30`, `VARIANT=WARM` | 4 packets / 30 s → **0.13 SYNs/s** |
| `capture.sh syns 30`, `VARIANT=COLD` | 2992 packets / 30 s → **99.73 SYNs/s** |
| `capture.sh pcap 20` | `/caps/pool.pcap`, 248.4 K, readable on the host at `lab/caps/pool.pcap` |
| `capture.sh fins 15` | 0 packets under continuous load — correct, and the reason Topic 4 needs idle gaps |
| `capture.sh dns` | prints the container's `resolv.conf` (`options ndots:0`) and captures its DNS packets |
| `sockets.sh` (in `api`) | 55 established, 50 to `:8000`, 11 TIME-WAIT, and per-socket `cubic … rtt … cwnd:10 … delivery_rate` |

The COLD number is the topic's whole thesis, measured: at 20 rps with
`FANOUT=5`, `99.73` SYNs/s is one connection per upstream call, and the WARM
run at the identical rate produced four SYNs in thirty seconds.
`07/python/pools_as_advertised.py` was re-run and still prints the six-language
table with no `BLOCKED` row.

## Experiments that run but do not demonstrate their claim

These are not blocked and not fixed. They execute, they produce clean output,
and the output does not support the sentence the topic writes about it. Saying
so is the point of this pass.

1. **Topic 4 produces no 502s under `mismatched`.** Three 180 s runs and then
   a targeted attempt — 440 requests at inter-arrival gaps of 4.9, 5.0, 5.05
   and 5.1 s, straddling the backend's 5 s idle timer from five concurrent
   probes — produced **440 × 200 and zero 502s**. The reason is nginx: it
   keeps a read event armed on every connection in its upstream keepalive
   pool, so the backend's `FIN` closes the cached connection instead of being
   discovered by the next write. The race window is the microseconds between
   choosing a cached connection and writing to it, and nginx wins it. What the
   capture *does* show is the direction of close flipping between profiles,
   which is the mechanism the 502 is only a symptom of. Recorded in the topic's
   broken-experiment list so the next reader checks the pcap before concluding
   the run was bad. To produce the 502 itself the lab would need a proxy that
   does not watch its idle upstream sockets.

2. **Topic 5's alias move produces a blip, not an outage.** 3 failed requests
   out of 15 001, bounded by the client's 10 s timeout, full recovery with no
   restart — and identically so with `KEEPALIVE_EXPIRY=none`, which was
   supposed to be the never-recovers row. The pool is not the binding
   constraint because httpx's pool can *grow*: `max_connections` defaults to
   100, so when the warm connections to the departed address hang, new
   requests open new connections, which re-resolve. Only the connections
   in flight at the instant of the swap fail. Making this topic's lab half
   demonstrate its claim needs `max_connections` pinned to the warm count so
   every request must reuse a dead socket. The per-runtime program
   `05/python/pool_outlives_dns.py`, which runs the four lifetime policies
   properly, is the part of this topic that does work, and it RAN above.

3. **Topic 6 shows no h1/h2 difference, with or without loss.** At 0% loss:
   96.64 ms (h1) vs 101.81 ms (h2) at p99. At 5% loss + 40 ms: 971.97 ms (h1)
   vs 935.67 ms (h2). The topic's own checklist names the diagnosis: *"h2
   shows no degradation at 5% loss → you are not on one connection. `ss -tan`
   inside `api` should show exactly one established connection per upstream
   during the h2 run."* It shows **11** at 0% loss and **40** under netem. So
   the h2 half is not running on a single multiplexed connection, and until it
   does the comparison measures nothing. The server half is confirmed correct
   after fix 6 (hypercorn h2c in the logs); the client half is not, and this is
   unresolved.

4. **Every documented arrival rate is above this machine's capacity.** Topic 1
   at 200 rps, topic 2's ramp to 600, topic 3 at 150 (survivable), topic 6 at
   50 with 20 × 100 KB fan-out: all produced four- and five-figure
   `dropped_iterations`, which `lab/README.md` itself says invalidates the
   histogram. Rates that work here are recorded next to each table above. This
   is a property of a 4-CPU / 5.1 GB VM shared with the user's other stacks,
   not a defect in the repo — but a reader on a laptop should expect to lower
   the rate and should say so next to any number they record.

## Still blocked

| Thing | Why | What it would need |
|---|---|---|
| `bbr` congestion control in Topic 6 | `sysctl net.ipv4.tcp_available_congestion_control` → `reno cubic`; Docker Desktop's linuxkit kernel does not ship the `tcp_bbr` module | A Linux host (or CI runner) with `tcp_bbr` loadable — `modprobe tcp_bbr`. Not fixable from macOS |
| `open lab/caps/pool.pcap` in Wireshark **[host]** | Wireshark is not installed on this Mac | `brew install --cask wireshark`. Not installed — the capture itself is verified present and 248 K at `lab/caps/pool.pcap` |
| k6 on the host | still absent | `brew install k6`. Not needed: every k6 line in the layer runs in the `load` container |
| Topic 4's 502, Topic 5's outage, Topic 6's h1/h2 gap | the experiments run and do not reproduce the symptom | see the four entries in the section above; each names what it would take |

## Teardown

`docker compose --profile failover --profile tools down -v --remove-orphans`
after each topic, and again at the end; `lab/caps/*.pcap` removed. `docker ps`
shows no `lab-*` container. The `--profile` flags matter: a plain
`docker compose down -v` leaves `upstream_b` running, which silently carries a
warm second upstream into the next topic's run.
