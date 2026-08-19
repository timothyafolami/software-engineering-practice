# Layer 10 · The shared lab

One compose stack, built once, reused by the topics that need a service.
Every topic README assumes this file and refers back to it rather than
restating it. Read it once before Topic 1.

## The constraint that shapes everything: no Metal in Docker

Docker Desktop on macOS runs containers inside a Linux VM with **no Metal
passthrough**. A container on this machine cannot use the GPU — still
true in 2026. So the split is fixed and non-negotiable:

- **In compose:** the gateway, the load generator, Postgres, Prometheus,
  Grafana. Everything that is a service rather than a model.
- **On the host:** the model server, every MLX process, every PyTorch/MPS
  process, and the from-scratch training loop in topic 7.

Containers reach the host model server at **`host.docker.internal`**.
Docker's own answer — Model Runner, which ships a `vllm-metal` build that
runs on the host and exposes an OpenAI-compatible endpoint to containers
— is the same workaround, blessed. Do not spend an evening debugging a
containerised model server that is 40x slow; it is running on the CPU and
that is the expected result.

## What runs where

| Topic | M1 laptop | Rented GPU |
|---|---|---|
| 1 Prefill/decode, bandwidth | yes — unified memory makes the arithmetic cleaner | no |
| 2 Serving under load | yes, small model, host-side server | only for realistic batch sizes |
| 3 Little's Law / queueing | yes — FastAPI + Postgres, no GPU involved | no |
| 4 Quantization + determinism | yes | for FP8/NVFP4 specifically |
| 5 Data pipelines / drift | yes | no |
| 6 Evaluation design | yes | no |
| 7 Transformer from scratch | yes at ~10-30M params | above ~100M, and all multi-GPU |
| 8 Interpretability | yes (GPT-2 small, Llama-3.2-1B) | for Gemma-2-2B attribution graphs |

## Services

These names are load-bearing. Every `docker compose` line, every k6 target
URL and every env var in the topic READMEs uses them literally.

| Service | What it is | Used by |
|---|---|---|
| `gateway` | FastAPI in front of the host model server | topics 2, 6 |
| `api` | FastAPI + SQLAlchemy async over `db` | topics 3, 5 |
| `db` | Postgres | topics 3, 5 |
| `k6` | Load generator, open model only | topics 2, 3 |
| `prom` | Prometheus, scrapes `gateway` and the host model server | topics 2, 6 |
| `grafana` | Dashboards, including the deliberately misleading one in topic 2 | topic 2 |

`gateway` and `api` are two different services on purpose: `gateway`
talks to the model server and owns cancellation-on-disconnect, `api`
talks to Postgres and owns the connection pool. Topics 3 and 5 never
need the model server; topics 2 and 6 never need Postgres.

## Ports, paths and names that must not drift

| Thing | Value | Used by |
|---|---|---|
| Host model server (primary) | `8081` | topics 1, 2, 4 — `python -m mlx_lm.server --port 8081` |
| Host model server (shadow candidate) | `8082` | topic 6, via `SHADOW_TARGET` |
| Host address from inside a container | `host.docker.internal` | `gateway` upstream URL |
| `gateway` listen port | `8000` | k6 target, Prometheus scrape |
| `api` listen port | `8000` | k6 target |
| Prometheus | `9090` | topic 2 |
| Grafana | `3000` | topic 2 |
| k6 script mount | host `lab/scripts/` → container `/scripts` | `docker compose run --rm k6 run /scripts/<name>.js` |
| k6 scripts | `/scripts/arrival_rate.js`, `/scripts/pool_ramp.js`, `/scripts/fanout.js` | topics 2, 3 |
| Postgres role / database | `app` | `docker compose exec db psql -U app` |

## Environment variables

Each is read at startup by the named service and selects one code path.
Nothing else changes between runs of the same topic.

| Variable | Service | Values | Topic | Selects |
|---|---|---|---|---|
| `RATE` | `k6` | requests/second | 2, 3 | Arrival rate for the open-model executor (`-e RATE=…`) |
| `PROMPT_VOLATILE` | `gateway` | `head`, `tail` | 2 | Whether the per-request unique string goes before or after the stable system prompt |
| `POOL_PROFILE` | `api` | `default`, `sized`, `budgeted` | 3 | Pool sizing and overload policy: copied config, derived from Little's Law, or derived plus deadline budget and 503 shedding |
| `N` | `k6` | integer | 3 | Fan-out width for `fanout.js` (`-e N=10`) |
| `SHADOW_TARGET` | `gateway` | URL | 6 | Candidate model server to mirror traffic to; unset disables shadowing |
| `MODEL_URL` | `gateway` | URL | 2, 6 | Primary model server, normally `http://host.docker.internal:8081/v1` |

## Load generation: open model, always

**Use k6's `constant-arrival-rate` executor, never a fixed VU count.** A
closed-loop generator — N virtual users each waiting for a response before
sending the next request — *cannot* reproduce the failures in topics 2 and
3. When the service slows, a closed-loop generator slows with it, which is
feedback that real users and upstream callers do not provide.

This is **coordinated omission**, stated once in the root
[`README.md`](../../README.md): a generator that stops issuing requests
while the system is stalled never records the latency of the requests it
failed to send, so the worst part of the incident is missing from the
histogram entirely. Every latency table in this layer is invalid if the
generator was closed-loop, which is why "latency flat as λ rises" is the
first line of the broken-experiment checklist in both topics.

Check `dropped_iterations` after every run. Non-zero means the *generator*
saturated, not the server.

`vegeta attack -rate=500/s` is the one-line alternative; it is open-model
by default and needs no scenario file.

## Version pins

The lab-wide table in [`SEQUENCE.md`](../../SEQUENCE.md) is the source of
truth. These are the ones this layer touches.

| | Pin | Note |
|---|---|---|
| Python | 3.14 | `gateway`, `api`, and the host-side MLX environment |
| Node | 24 LTS | topic 5's third feature implementation |
| Go | 1.26.x | topic 3's pool client, topic 5's ingest, topic 6's mirroring gateway |
| Postgres | 18.6 | `db` |
| k6 | v2.x | `k6`. Earlier drafts elsewhere in this repo said v1.x; that is stale |
| vLLM / MLX | whatever you install, recorded per run | both move monthly; check the releases page, not a blog post, and write the version next to any number you record |

Rust, C++ and Java appear as single-file programs with no service
dependency; record the toolchain version next to any number they produce.

## Rented GPUs

Topics 7 and 8 want an hour or two on a real GPU. Two tiers exist —
interruptible (RunPod Community, Vast.ai) and reserved (Lambda, RunPod
Secure), with hyperscalers roughly double the reserved tier. **Per-hour
prices are deliberately not written here**: they move monthly, and this
lab's rule is that every number is either derived on the page or carries a
source. Take the rate from the provider's pricing page at the moment you
rent, record it in the topic's table next to the run it paid for, and
derive $/1M tokens yourself.

Checkpoint from the first script you write. The cheap tier is
interruptible, and that is the whole reason it is cheap.

## Health check before you trust any run

```
cd 10-edge/lab
docker compose ps
curl -s localhost:8081/v1/models                            # host model server is up
docker compose exec gateway sh -c \
  "curl -s http://host.docker.internal:8081/v1/models"      # ...and reachable from a container
docker compose exec db psql -U app -c "select version();"   # db is up, role is right
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

If the second command fails while the first succeeds, the container
cannot see the host and every topic-2 and topic-6 run will produce
connection errors that look like server overload.

## Two things this stack needs that are not in the table above

**`DB_PORT`.** `db` publishes `${DB_PORT:-5432}:5432`. If you already have
a Postgres on the host — most people running this do — `docker compose up`
fails to bind and the whole `up` aborts. `DB_PORT=55433 docker compose up -d
db api` moves the *published* port only; `api` reaches the database at
`db:5432` on the compose network either way, so nothing else changes.

**`lab/out/`.** `docker compose run --rm k6` deletes the container when the
script exits, which takes the `summary.json` k6 wrote with it. The three k6
scripts write their summaries to `/out`, bind-mounted from `lab/out/`, so
the run you are told to keep (`fanout.js -e N=1`) survives the run that
produced it. The directory is git-ignored.

## Running the plumbing without a model

`tools/fake_upstream.py` is a stand-in for the host model server. **It is
not a model** — it has no weights and generates no text; it holds a fixed
number of decode slots, sleeps per token, and charges prefill only for
prompt blocks it has not seen. That is enough to make k6 → `gateway` →
`host.docker.internal` → upstream a runnable path, so a wiring regression
in the stack is caught without a 16 GB download:

```
python3 tools/fake_upstream.py --port 8085          # on the HOST
MODEL_URL=http://host.docker.internal:8085/v1 \
  docker compose up -d gateway prom grafana
```

Use a port other than 8081 so a real model server and this can never be
confused in a scrape. Every latency it produces is one the file computed
from its own constants — label those rows "stub upstream" and never mix
them into a table of engine measurements.

## Teardown

```
docker compose down -v          # -v also drops the Postgres volume
```

Kill the host-side model server separately; it is not managed by compose,
which is the one ergonomic cost of the Metal split.
