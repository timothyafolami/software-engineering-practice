# Layer 2 · The shared lab

One compose stack, built once, reused by all seven topics. Every topic README
assumes this is running and refers back here rather than restating it.

Everything that inspects a network runs **inside a Linux container**. `ss`,
`tcpdump`, `/proc` and `resolv.conf` do not exist, or do not mean the same
thing, on macOS 27 / arm64 — which is the machine this lab is written for.
Commands that must run on the Mac itself are marked **[host]**.

## Services

| Service | What it is | Why it's here |
|---|---|---|
| `api` | FastAPI + uvicorn | The service under test |
| `db` | Postgres | Real pool exhaustion, not simulated |
| `upstream` | Tiny FastAPI service, tunable latency/error rate | The dependency that gets slow |
| `upstream_b` | Second instance of `upstream`, not started by default | Topic 5 moves the network alias from `upstream` to this one mid-run |
| `toxi` | [Toxiproxy](https://github.com/Shopify/toxiproxy) | Injects latency, jitter, loss, resets via its REST API |
| `lb` | nginx (stand-in for ALB/Envoy) | Keep-alive races, 502s, connection reuse |
| `load` | [k6](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/constant-arrival-rate/) | Constant *arrival rate*, not constant VUs |
| `sniff` | alpine + tcpdump/iproute2 | Shares `api`'s network namespace so tcpdump sees what the service sees |

These names are load-bearing. Every `docker compose exec`, every k6 target URL
and every Toxiproxy proxy name in the topic READMEs uses them literally.

## Ports, paths and names that must not drift

| Thing | Value | Used by |
|---|---|---|
| `api` listen port | `8000` | topics 1, 4, 7 (`ss`, tcpdump filters, nginx upstream) |
| Toxiproxy control API | `8474` | topics 1, 3 (`curl localhost:8474/proxies/...`) |
| Toxiproxy proxy name for the dependency | `upstream` | topic 3's toxic POST |
| Toxiproxy proxy name for the database | `db`, listening on `8476` | topic 2 — `DATABASE_URL` goes through it so the 100 ms that makes `W` a known constant can be injected. `docker compose exec db psql -U app` still reaches Postgres directly |
| Compose network name | `lab_default` | topic 5: `docker network disconnect lab_default lab-upstream-1` — the second argument is a *container*, and `upstream` is only a network alias |
| k6 script mount | host `lab/scripts/` → container `/scripts` | `docker compose run --rm load run /scripts/topicN.js` |
| pcap volume | host `lab/caps/` → container `/caps` | topic 4 (`/caps/topic4.pcap`), topic 7 (`/caps/pool.pcap`) |
| Postgres role / database | `app` | `docker compose exec db psql -U app` |

The compose file must mount `lab/caps/` at `/caps` in **both** `sniff` and
`api`, so a capture written by the sniffer is readable from the host at
`lab/caps/` and openable in Wireshark **[host]**.

## The `sniff` sidecar, and why it is shaped this way

```yaml
sniff:
  image: alpine:3
  network_mode: "service:api"
  cap_add: [NET_ADMIN, NET_RAW]
  volumes: ["./caps:/caps"]
  command: ["sleep", "infinity"]
```

`network_mode: "service:api"` puts the sidecar in `api`'s network namespace,
so `tcpdump -i any` inside `sniff` sees exactly the packets `api` sees. This
is not a stylistic choice. On macOS, Docker Desktop runs containers inside a
Linux VM: there is no `veth` pair on your host to attach to, `tcpdump` on the
Mac sees nothing of container traffic, and every `nsenter --net=/proc/<pid>/ns/net`
recipe in the Linux blogs fails because that PID lives in the VM, not on your
machine. The sidecar is the one pattern that works identically on macOS and
in Linux CI.

`NET_ADMIN` is there for the topics that shape or inspect the interface;
`NET_RAW` is what actually permits packet capture.

## Environment variables

Each is read by `api` at startup and selects a code path. Nothing else
changes between runs of the same topic.

| Variable | Values | Topic | Selects |
|---|---|---|---|
| `POOL_PROFILE` | `default`, `wide`, `fast_timeout`, `shed` | 2 | Which pool sizing / overload policy `api` runs with |
| `KEEPALIVE_PROFILE` | `mismatched`, `ordered`, `ordered_bounded` | 4 | Backend vs LB idle-timeout ordering, and whether `keepalive_requests` is bounded |
| `PROTO` | `h1`, `h2` | 6 | Which HTTP version the client under test negotiates |

Topic 1's three variants (`COLD`, `WARM`, `WARM_TUNED`) are selected the same
way, by the variant name itself, so the run lines read
`VARIANT=COLD docker compose up -d api`.

## Load generation: open model, always

**Use k6's `constant-arrival-rate` executor, never a fixed VU count.** A
closed-loop generator — N virtual users each waiting for a response before
sending the next request — *cannot* reproduce most of the failures in this
layer. When your service slows, a closed-loop generator slows with it, which
is feedback that real users and upstream callers do not provide. Open-model
load ("500 requests arrive per second whether or not you are ready") is what
makes queues grow, pools exhaust, and retries pile up.

This is the same defect as **coordinated omission**, stated once in the root
[`README.md`](../../README.md): a load generator that stops issuing requests
while the system is stalled never records the latency of the requests it
failed to send, so the worst part of the incident is missing from your
histogram entirely. Every table in this layer is invalid if the generator was
closed-loop, which is why "no queueing at any rate" appears in the
broken-experiment checklist of three separate topics.

`vegeta attack -rate=500/s` is the one-line alternative; it is open-model by
default and needs no scenario file.

## Version pins

Set in this directory so the topics cannot drift apart. The lab-wide table in
[`SEQUENCE.md`](../../SEQUENCE.md) is the source of truth; these are the ones
this layer touches.

| | Pin | Note |
|---|---|---|
| Python | 3.14 | `api` and `upstream` images |
| Node | 24 LTS | topic 1, 3, 4, 5, 6 clients |
| Go | 1.26.x | topic 1, 2, 3, 5 clients |
| Postgres | 18.6 | `db` |
| k6 | 2.2.0 | `load`. Earlier drafts in this repo said v1.x; that is stale. The registry has no `grafana/k6:2` tag — the short form is `v2`, so the compose file pins the patch release |

Rust, C++ and Java experiments run on whatever toolchain your machine has —
they are single files with no service dependency — but record the version you
used next to any number you record.

## Health check before you trust any run

```
cd 02-network/lab
docker compose ps
docker compose exec api sh -c "ss -tln"                    # api is listening on 8000
curl -s localhost:8474/proxies                             # toxiproxy knows about 'upstream' AND 'db'
docker compose exec sniff sh -c "tcpdump -D | head"        # the sniffer can see interfaces
docker compose exec db psql -U app -c "select version();"  # db is up, role is right
```

If `curl -s localhost:8474/proxies` returns `{}`, no toxic you inject later
will do anything, and several topics will produce a null result that looks
like a disproved prediction. Check this first, every time.

## Teardown

```
# The --profile flags are not optional: a plain `down -v` leaves `upstream_b`
# running, and a warm second upstream silently carried into the next topic is
# a fault you will spend an hour not finding.
docker compose --profile failover --profile tools down -v --remove-orphans
rm -f caps/*.pcap               # captures are large; they are gitignored, not tracked
```
