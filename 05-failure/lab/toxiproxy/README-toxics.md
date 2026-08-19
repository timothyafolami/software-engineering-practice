# Toxiproxy, and why the fault goes here rather than in the code

Toxiproxy sits between a service and its dependency, so you can make the
dependency **slow** without touching application code. That distinction is
the whole of Layer 5: a dependency that is down produces an error you
already handle, and a dependency that is slow produces a queue you did not
plan for.

Two proxies are defined in `toxiproxy.json` and start with the `chain`,
`metastable` and `payments` profiles:

| Proxy | Listener (in-container) | Upstream | Published on host |
|---|---|---|---|
| `postgres` | `0.0.0.0:5433` | `postgres:5432` | 55433 |
| `redis` | `0.0.0.0:6380` | `redis:6379` | 56380 |

`service-c` is pointed at `toxiproxy:5433` rather than at Postgres, so
every query the leaf runs is interceptable.

The k6 scripts drive the admin API themselves — `03_retry_storm.js` adds
the fault at t=60s and removes it at t=80s from inside the run, because a
fault window you type by hand is a fault window you cannot reproduce. The
lines below are the same operations by hand, for when you want to poke at
it live.

Add 800ms of latency to every query the leaf makes:

    curl -s -XPOST localhost:8474/proxies/postgres/toxics \
      -d '{"name":"slow","type":"latency","stream":"downstream",
           "attributes":{"latency":800,"jitter":100}}'

Cut the connection instead — the failure everyone tests for, and the easy one:

    curl -s -XPOST localhost:8474/proxies/postgres/toxics \
      -d '{"name":"cut","type":"timeout","attributes":{"timeout":1}}'

Remove a toxic, which is the half of the experiment that matters:

    curl -s -XDELETE localhost:8474/proxies/postgres/toxics/slow

List what is currently applied, before you conclude anything about a run:

    curl -s localhost:8474/proxies | python3 -m json.tool
