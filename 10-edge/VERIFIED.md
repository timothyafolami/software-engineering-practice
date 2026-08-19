# Layer 10 — verification record

**What this file is.** An independent second pass over `10-edge/`. Every
program below was compiled and executed on this machine, by someone who
did not write it, using the command printed in its topic README. Where
the command was wrong the README was fixed; where the program was wrong
the program was fixed and re-run.

**What this file is not.** It records that the code **executes and prints
what it claims to print**. It does not record that anything was learned.
The `Predict, then record` tables in every topic README are still blank
and are meant to stay that way until you fill them in — they are the
exercise, not an omission.

Date of the first pass: **2026-08-19**. A second pass on the same day —
after the Docker daemon came up — ran everything that had been blocked
behind it; see [Unblock pass](#unblock-pass--docker-daemon-up-2026-08-19-same-day-later)
at the end. Eight further defects were found there, all of them in code
that had never executed.

## The machine

| | |
|---|---|
| OS | macOS 27.0 (build 26A5406e), Darwin 27.0.0 |
| CPU | Apple M1, arm64, 8 hardware threads, 16.0 GiB unified memory |
| Python | 3.13.5 — numpy 1.26.4, mlx, mlx-lm 0.29.1, torch 2.9.1, transformers 4.46.3, fastapi 0.128.0, uvicorn 0.40.0, httpx 0.28.1, pytest 9.1.1, tiktoken |
| Node.js | v24.14.0 |
| Go | go1.24.5 darwin/arm64 |
| Rust | rustc 1.97.1 (8bab26f4f 2026-07-14), cargo 1.97.1 |
| C++ | Apple clang 21.0.0 (clang-2100.1.1.101) — really clang; `-pthread` works |
| Java | OpenJDK 21.0.2 LTS — virtual threads available |
| Docker | CLI 28.1.1, **daemon UP as of the second pass** — Docker Desktop 4.78.0, engine 29.5.3, linux/arm64, 4 CPUs / 4.8 GiB in the VM; Compose v5.1.4 |
| k6 | **not installed on the host, and does not need to be** — `grafana/k6:latest` resolved to **k6 v2.2.0** (go1.26.5, linux/arm64), which satisfies the v2.x pin in `lab/README.md` |
| circuit-tracer | not installed |

Nothing was installed and no `brew` was run. The first pass did not start
the daemon either; by the second pass (below) it was already running.

## Defects found and fixed (first pass)

Three, all in topic 3, plus one README overclaim in topic 4. The second
pass found eight more, all in `lab/`; they are listed in their own section
below rather than merged in here, because which pass found a defect is
itself information — everything below this line was found by *running the
host programs*, and everything in the unblock section was found by
*starting the stack*, which no amount of static checking had caught.

**1. Throughput was counted over the wrong interval — all six languages.**
`done/s` divided the *post-drain* completion count by the *arrival-window*
duration. Requests that finished during the drain were credited to a window
they did not finish in, so at λ=440 against a c/W wall of 400 req/s the
table read **451 (Python), 443 (Node), 453 (Go), 431 (Rust), 451 (C++),
433 (Java)** — a system delivering 13% more throughput than the wall the
section exists to demonstrate. Fixed in all six by snapshotting completions
at the instant the arrival window closes. Post-fix every implementation
saturates at or below its own wall.

**2. The Kingman arm ran too short to confirm its own prediction.**
`python/pool_queueing.py` Part 2 ran each service-time distribution for 10
seconds. The mean wait of a `c_s = 1` queue at ρ=0.85 is a heavy-tailed
average; 10 seconds does not contain enough deep excursions to estimate it,
so the measured exponential/fixed ratio came out **1.17x against a predicted
2.71x** — and the file then printed a paragraph declaring the prediction
confirmed. Raised to 30 seconds per arm (measured 2.03x vs predicted 2.33x
on the verification run) and the closing verdict is now computed from the
numbers obtained, with an explicit BROKEN-RUN branch if the direction ever
reverses. Cost: the program now takes ~90s, and the topic README's "about
20 seconds" was corrected.

**3. `java/PoolQueueing.java`'s "what to look for" contradicted its own
output.** The header told the reader to expect `wait before service` near
zero in the platform-thread row. The program deliberately times the wait
from *submission* so it can see the executor queue, so that column reads
~45ms in both rows — and the program's own footer explains why. A reader
following the header would conclude the experiment was broken. Header
rewritten to describe what is actually measured.

Also fixed: `cpp/pool_queueing.cpp` now prints why `done/s` tops out a few
percent under the header's λ_max (`sleep_for(50ms)` overshoots to ~56ms, so
the real wall is `c / measured W`).

**4. Topic 4 README called `nodejs/float64_control.js` "the null-result
arm".** It is not one. The program prints 5-of-7 distinct sums at float64
and a `NaN` naive softmax at float64, and says so in its own output. The
README paragraph now describes what the program prints: the wider type buys
*spread*, not immunity.

## No fabricated numbers

All eight `Predict, then record` tables in the topic READMEs are still
blank after both passes, and every number the second pass produced was
written here, not into them.

Every figure in the topic READMEs is either derived on the page (ridge
points from vendor-published TFLOP/s and GB/s, the KV-bytes arithmetic,
`c/W`) or explicitly labelled as somebody else's. No prose claims a
measurement taken on this machine. All eight `Predict, then record` tables
are blank, and all eight fill-in-the-blank lists still have their blanks.

## Coverage against each README's own language list

| Topic | README says | Shipped | Verdict |
|---|---|---|---|
| 01 | three: Python, C++, Rust | python, cpp, rust | complete |
| 02 | six | python, nodejs, golang, rust, cpp, java | complete |
| 03 | six | python, nodejs, golang, rust, cpp, java | complete |
| 04 | six | python, nodejs, golang, rust, cpp, java | complete |
| 05 | three: Python, Go, Node | python, golang, nodejs | complete |
| 06 | two: Python, Go | python, golang | complete |
| 07 | Python only | python | complete |
| 08 | Python only | python | complete |

No topic folder is empty and none ships fewer languages than its README
covers. Topic 3's Kingman arm and topic 5's drift/contamination arms are
Python-only by a stated argument in the file headers and the READMEs, not
silently.

## Every program

`RAN` = executed as written. `FIXED-THEN-RAN` = defect found, corrected,
re-executed clean. `BLOCKED` = could not run here; reason and the exact
one-line unblock command are given.

### Topic 1 — prefill, decode, bandwidth

| Program | Command | Status |
|---|---|---|
| `python/stream.py` | `python3 python/stream.py` | RAN — 3.4s; best sustained 50.6 GB/s, allocation tax 72% |
| `cpp/stream.cpp` | `c++ -O3 -std=c++20 -o /tmp/stream cpp/stream.cpp && /tmp/stream` | RAN — 4.3s; 47.8 GB/s at 1 thread |
| `rust/stream/` | `cargo run --release --manifest-path rust/stream/Cargo.toml` | RAN — 4.2s; 51.8 GB/s at 8 threads |
| `python/predict_decode.py` | `python3 python/predict_decode.py` | RAN — worked example, no model |
| `python/predict_decode.py --bandwidth` | `python3 python/predict_decode.py --bandwidth 51.8` | RAN |
| model conversion + generate | `python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 --mlx-path ./q4` | BLOCKED — mlx-lm is installed but the run needs a ~16 GB Hugging Face download and the measurement is the reader's exercise. Unblock: `python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 --mlx-path ./q4` |

The three implementations agree within 8% at one thread, which is the
result the topic asks for.

### Topic 2 — continuous batching, paged KV, prefix caching

| Program | Command | Status |
|---|---|---|
| `python/test_prefix_stability.py` | `python3 python/test_prefix_stability.py` | RAN — tail PASS (124 cacheable blocks), head FAIL (1). The guard fails on purpose in the head arm |
| `python/prompt_layout.py` | imported by the above and mounted into `gateway` | RAN (as a module; it is not a standalone entry point) |
| `python/cancel_propagation.py` | `python3 python/cancel_propagation.py` | RAN — 5.7s; naive 40 tokens / 3.57s wasted, cancelling 6 tokens / 0.11s |
| `nodejs/cancel_propagation.js` | `node nodejs/cancel_propagation.js` | RAN — 4.8s; 40 / 3.57s vs 6 / 0.11s |
| `golang/cancel_propagation.go` | `cd golang && go run cancel_propagation.go` | RAN — 6.7s; 40 / 3.56s vs 4 / 0.00s |
| `rust/cancel_propagation/` | `cargo run --release --manifest-path rust/cancel_propagation/Cargo.toml` | RAN — 15s incl. build; 40 / 3.60s vs 4 / 0.00s |
| `cpp/cancel_propagation.cpp` | `c++ -O2 -std=c++20 -pthread -o /tmp/cancel_cpp cpp/cancel_propagation.cpp && /tmp/cancel_cpp` | RAN — 5.5s; 40 / 3.73s vs 5 / 0.04s. Uses `poll(2)`, not `epoll` — portable to Darwin |
| stack up | `cd 10-edge/lab && docker compose up -d prom grafana gateway` | FIXED-THEN-RAN — `gateway` builds and serves; `/healthz` reports `block_size 16` out of the *mounted* `prompt_layout`, so the bind mount works. Prometheus scrapes `gateway` (target up) |
| `/debug/prompt`, both layouts | `curl -s "localhost:8000/debug/prompt?chars=256"` | RAN — `PROMPT_VOLATILE=tail` → `identical_head: true`; `=head` → `false`, at 1,899 approx tokens either way. The head/tail switch works through the container |
| cancellation through the stack | 10 × `curl --max-time 1.5 … /generate -d '{"max_tokens":2000}'` | RAN — `gateway_requests_total{outcome="client_disconnect"} 10`, `gateway_upstream_cancelled_total 10`, and the upstream's own cancelled counter also 10 with in-flight back to 0. Propagation is real, not just counted |
| load runs (`arrival_rate.js`) against a **stub** upstream | `python3 tools/fake_upstream.py --port 8085` then `docker compose --profile load run --rm k6 run /scripts/arrival_rate.js -e RATE=16 -e DURATION=30s` | FIXED-THEN-RAN — see the defect list below; the script was measuring the ASGI header flush and calling it TTFT. Numbers are the stub's, **not a model's** |
| load runs against a **real engine** | same, with `python3 -m mlx_lm.server --model ./q4 --port 8081` | BLOCKED — no converted model on this machine (see topic 1). Unblock: convert a model, then `MODEL_URL=http://host.docker.internal:8081/v1 docker compose up -d gateway prom grafana`. Note port 8081 is occupied on this host by an unrelated container, so pick a free port and edit `prometheus/prometheus.yml` too |
| `java/CancelPropagation.java` | `cd java && javac CancelPropagation.java -d /tmp/javabuild && java -cp /tmp/javabuild CancelPropagation` | RAN — 6.2s; 40 / 3.74s vs 5 / 0.02s |

All six reproduce the finding in the same direction and the same order of
magnitude. No `sys/epoll.h`, no `/proc`, no cgroup paths anywhere in this
layer.

### Topic 3 — Little's Law, Kingman, tail compounding

| Program | Command | Status |
|---|---|---|
| `python/pool_queueing.py` | `python3 python/pool_queueing.py` | FIXED-THEN-RAN — throughput window + Kingman run length. ~90s. Post-fix: done/s tops at 389 against a 400 wall; Kingman measured 2.03x vs predicted 2.33x |
| `nodejs/pool_queueing.js` | `node nodejs/pool_queueing.js` | FIXED-THEN-RAN — throughput window. 29s. Post-fix done/s tops at 388 |
| `golang/pool_queueing.go` | `cd golang && go run pool_queueing.go` | FIXED-THEN-RAN — throughput window. 20s. Post-fix done/s tops at 391; `MaxIdleConns=2` opens 226 connections vs 0 |
| `rust/pool_queueing/` | `cargo run --release --manifest-path rust/pool_queueing/Cargo.toml` | FIXED-THEN-RAN — throughput window. 28s. Post-fix done/s tops at 376; `timeout(spawn(work))` wastes 42.6 permit-seconds vs 0.13 |
| `cpp/pool_queueing.cpp` | `c++ -O2 -std=c++20 -pthread -o /tmp/pool_cpp cpp/pool_queueing.cpp && /tmp/pool_cpp` | FIXED-THEN-RAN — throughput window + measured-W note. 24s |
| `java/PoolQueueing.java` | `cd java && javac PoolQueueing.java -d /tmp/javabuild && java -cp /tmp/javabuild PoolQueueing` | FIXED-THEN-RAN — throughput window + header claim. 22s. 20 threads vs 1407, same throughput |
| `python/fanout_hedging.py` | `python3 python/fanout_hedging.py` | RAN — 7.3s. Independent arm tracks the arithmetic (ratio 0.99–1.01); correlated arm exceeds the prediction by up to 148% |
| Postgres / k6 service experiment | `DB_PORT=55433 docker compose up -d db api` then `pool_ramp.js` / `fanout.js` | FIXED-THEN-RAN — four defects in the stack itself (asyncpg wheel, Postgres 18 volume path, published port collision, k6 summaries) before it would start. Post-fix the pool-exhaustion graph is exactly the claimed one; numbers in the unblock section below |
| `/healthz` arithmetic against reality | `curl -s localhost:8001/healthz` + `pg_stat_activity` | RAN — `effective_c 15`, `max_lambda_req_per_s 300`. Under saturation Postgres shows **exactly 15** `app` backends, so `c = pool_size + max_overflow` is confirmed from the database side, not just asserted |
| the fixed arm (`sized` / `budgeted`) | `POOL_PROFILE=budgeted docker compose up -d api` | RAN — knee moves from 300 to 400 req/s and the failure becomes explicit (`api_shed_total`, 503 + `Retry-After`). The deadline is **not** a bound under deep overload; recorded honestly below |

### Topic 4 — quantization, numerical stability, determinism

| Program | Command | Status |
|---|---|---|
| `python/softmax_crossover.py` | `python3 python/softmax_crossover.py` | RAN — crossovers by bisection: naive survives to 11.1 (fp16), 88.7 (fp32), 709.8 (fp64) |
| `python/welford_vs_naive.py` | `python3 python/welford_vs_naive.py` | RAN — naive float32 variance goes negative at offset 1e6; Welford holds |
| `cpp/softmax.cpp` (two builds + diff) | `c++ -O2 -std=c++20 -o /tmp/sm cpp/softmax.cpp && c++ -O2 -ffast-math -std=c++20 -o /tmp/sm_fast cpp/softmax.cpp && diff <(/tmp/sm) <(/tmp/sm_fast)` | RAN — diff is non-empty and load-bearing: under `-ffast-math` the naive sum prints `1` where the IEEE build prints `nan`, while `max p` stays 0. The overflow is still there; the NaN that would have told you is gone |
| `rust/strict_fp/` release and debug | `cargo run --release --manifest-path rust/strict_fp/Cargo.toml` and `cargo run --manifest-path rust/strict_fp/Cargo.toml` | RAN — outputs are byte-identical between profiles, which is the claim |
| `golang/parallel_sum.go` | `cd golang && go run parallel_sum.go` | RAN — 7 distinct float32 sums from 7 partitionings, each reproducible |
| `golang/parallel_sum.go -workers` | `cd golang && go run parallel_sum.go -workers 1,3,7,64` | RAN — 4 distinct from 4 |
| `java/ParallelSum.java` | `cd java && javac ParallelSum.java -d /tmp/javabuild && java -cp /tmp/javabuild ParallelSum` | RAN — 7 of 7 distinct at double and at float |
| `nodejs/float64_control.js` | `node nodejs/float64_control.js` | RAN — 5 of 7 distinct at float64, 6 of 7 under `Math.fround`. README corrected: this is not a null result |
| `python/sample_determinism.py` | `python3 python/sample_determinism.py` | BLOCKED — correctly refuses and prints its own unblock line rather than recording a null result. Unblock: `python3 -m mlx_lm.server --model ./q4 --port 8081` |

### Topic 5 — pipelines, versioning, drift

| Program | Command | Status |
|---|---|---|
| `python/seed_events.py` | `python3 python/seed_events.py` | RAN — 300,330 events, 5,000 users, deterministic |
| `python/features.py` | `python3 python/features.py` | RAN — all three boundary self-checks PASS |
| `golang/features.go` native and conform | `cd golang && go run features.go && go run features.go -mode conform -out ../data/features_go_conform.csv` | RAN — both modes, 3,764 users each |
| `nodejs/features.js` native and conform | `node nodejs/features.js && node nodejs/features.js --mode conform` | RAN — both modes |
| `python/three_way_diff.py` | `python3 python/three_way_diff.py` | RAN — native 21.41% / 29.99% / 46.60% pairwise disagreement, each attributed to a named decision; conform 0.00% on all three pairs |
| `python/test_feature_contract.py` (native) | `python3 python/test_feature_contract.py` | RAN — FAILS as designed, 94 of 500 vectors, named rows |
| `python/test_feature_contract.py` (conform) | `python3 python/test_feature_contract.py --log conform` | RAN — PASSES |
| same, under pytest | `pytest python/test_feature_contract.py` | RAN — 2 passed |
| `python/offline_online_skew.py` | `python3 python/offline_online_skew.py` | RAN — AUC 0.9032 offline → 0.8969 online |
| `python/drift_psi_kl.py` | `python3 python/drift_psi_kl.py` | RAN — PSI 0.381 costs +0.0004 AUC; the −0.0896 AUC failure has PSI 0.009. Bin-count sweep included |
| `python/minhash_contamination.py` | `python3 python/minhash_contamination.py` | RAN — 26 of 200 flagged, 0 false positives, estimator error checked against exact Jaccard |
| Postgres path | `DB_PORT=55433 docker compose up -d db api && docker compose exec db psql -U app -c "select count(*) from items;"` | FIXED-THEN-RAN — same stack fixes as topic 3. `init.sql` runs on first boot and seeds **200,000** rows; `show max_connections` is 100, i.e. 6.7x the pool's `c` of 15, which is why the pool and not the server is the wall |

### Topic 6 — evaluation design and shadow deployment

| Program | Command | Status |
|---|---|---|
| `python/agreement.py` | `python3 python/agreement.py` | RAN — kappa against base rate, judge/human ceiling ratio, Krippendorff alpha with missing labels |
| `python/judge_position_bias.py` | `python3 python/judge_position_bias.py` | RAN — recovered bias tracks the injected lean; consistency falls as it rises |
| `python/compare.py` | `python3 python/compare.py` | RAN — aggregate "no signal" while the adversarial slice is −51.4 points; `nonenglish` correctly marked underpowered rather than given a verdict |
| `python/build_set.py` | `python3 python/build_set.py` | RAN — writes `eval_set.jsonl` template (now git-ignored) |
| `golang/shadow_gateway.go` | `cd golang && go run shadow_gateway.go` | RAN — 11s. Primary p99 4.354s when the shadow shares the in-flight budget, 79ms when it has its own transport |
| contamination check on the eval set | `python3 ../05-pipelines-versioning-and-drift/python/minhash_contamination.py` | RAN (see topic 5) |
| shadow mirroring through the stack, **stub** candidates | `SHADOW_TARGET=http://host.docker.internal:8086/v1 docker compose up -d gateway prom grafana` | RAN — against a live-but-5x-slower candidate: `gateway_shadow_total{outcome="ok"} 12`, primary 12/12 ok, primary TTFT mean 12.4 ms. Against a **dead** port: `gateway_shadow_total{outcome="error"} 12`, primary still 12/12 ok, TTFT mean 13.6 ms. The candidate cannot reach the primary response, which is the property |
| real shadow run with two model servers | two host model servers + `SHADOW_TARGET=…` | BLOCKED — no converted model (see topic 1); the stub above exercises the mirroring path but produces no model output to compare. Unblock: convert two models, serve them on two free ports, then `SHADOW_TARGET=http://host.docker.internal:<candidate>/v1 docker compose up -d gateway prom grafana` |

### Topic 7 — a transformer from scratch

| Program | Command | Status |
|---|---|---|
| `python/model.py` | `python3 python/model.py` | RAN — 0.3s. Causality PASS, RoPE relative-position PASS (q·k equal at offsets 2,5 / 5,8 / 11,14), fp16 naive softmax → `nan` while lse survives |
| `python/train.py` | `python3 python/train.py` | RAN — **165s**. 10.8M params, loss 5.64 → 0.23 over 300 steps, 7,457 tok/s |
| `python/train.py --softmax naive --attn-dtype float16 --logit-scale 6` | as written | RAN — **143s**. Loss becomes `nan` at step 1 and the run stays dead |
| `python/train.py --softmax lse --attn-dtype float16 --logit-scale 6` | as written | RAN — **149s**. Trains to 0.23 at `max\|score\|` up to 38, well past float16's 11.09 exp() ceiling. This pair is the finding and it reproduces |
| `python/mfu.py` | `python3 python/mfu.py` | RAN — reference table, refuses to guess a peak or a price |
| `python/mfu.py` with the run's numbers | `python3 python/mfu.py --params 10818432 --tokens 1228800 --seconds 149.4 --peak-tflops 2.6 --bandwidth-gbs 51.8 --tokens-per-step 4096` | RAN — 25.0% MFU, COMPUTE-bound |
| longer run on a real corpus | `python3 python/train.py --layers 8 --d-model 512 --seq 512 --batch 16 --steps 2000 --data tinystories.txt` | BLOCKED — no corpus on this machine. Unblock: supply `tinystories.txt` and re-run the printed command |
| rented-GPU steps | `nvidia-smi ...` | BLOCKED — no NVIDIA GPU. Unblock: rent one; `python/mfu.py` transfers unchanged |

The three training runs take 2½ minutes each. That is the work, not a
hang — every one prints a progress row every 20 steps and finishes.

### Topic 8 — interpretability and attribution

Run in the README's order; each step checks something the next assumes.
GPT-2 small on MPS, weights already in the local Hugging Face cache.

| Program | Command | Status |
|---|---|---|
| `python/ioi.py` | `python3 python/ioi.py` | RAN — 7.7s. Clean +2.879, corrupted −2.701, span 5.580. Baselines bracket zero, so the task is real for this model |
| `python/patching.py` | `python3 python/patching.py` | RAN — 5.3s. Both ends PASS: patch-everything reproduces clean exactly, patch-nothing reproduces corrupted exactly |
| `python/residual_sweep.py` | `python3 python/residual_sweep.py` | RAN — 16s, 144 forward passes, ASCII heatmap |
| `python/head_sweep.py` | `python3 python/head_sweep.py` | RAN — 16s, 144 passes. Names 8.10 / 10.0 / 9.7 as restorers and 10.7 / 11.10 as pushing the other way |
| `python/ablate_and_falsify.py` | `python3 python/ablate_and_falsify.py --heads 8.10,10.0,9.7` | RAN — 5.8s. Named heads 25.1% damage vs 1.1% for five random-head controls; 26.5% vs 4.0% on a holdout with new names *and* new templates |
| attribution-graph cross-check | `pip install circuit-tracer` | BLOCKED — not installed. Unblock: `pip install circuit-tracer` |

### The shared lab

**First pass:** the Docker daemon was not running, so nothing in `lab/` was
executed. It was checked statically instead — and every one of these static
checks passed while the stack still could not start, which is the honest
argument for the second pass:

| Check | Result |
|---|---|
| `docker compose config` | parses clean |
| Service names | `gateway`, `api`, `db`, `k6`, `prom`, `grafana` — exactly what `lab/README.md` specifies |
| Ports | gateway 8000, api 8001→8000, db 5432, prom 9090, grafana 3000 — as specified |
| Env vars | `MODEL_URL`, `PROMPT_VOLATILE`, `SHADOW_TARGET`, `POOL_PROFILE`, `RATE`, `N` — all present and defaulted |
| `host.docker.internal` | `extra_hosts: host-gateway` on `gateway`, `k6`, `prom` |
| prompt-layout mount | topic 2's `python/` bind-mounted read-only into `gateway`, so the service and the regression test cannot drift |
| k6 executor | all three scripts use `constant-arrival-rate`, never `constant-vus`, and all three declare `dropped_iterations: ['count == 0']` as a threshold |
| `node --check` on the k6 scripts | all three parse |
| `python -m py_compile` on `gateway/app.py`, `api/app.py` | both compile |
| Version pins | Postgres 18.6 and Python 3.14 match `lab/README.md`. `k6` uses `grafana/k6:latest` rather than the pinned v2.x — pin it if you want the run reproducible |

**Second pass:** the daemon was up, the stack was actually built and run.
Every row in that table stayed true and the stack still would not start:
`docker compose config` parses a file whose Postgres volume is at a path
Postgres 18 refuses, `py_compile` compiles an `app.py` whose dependency has
no wheel for the pinned interpreter, and `node --check` parses a k6 script
that measures the wrong quantity. Static checking cannot reach any of that.
See the section below.

Note that `k6` itself is not installed on this host and does not need to
be — the lab runs it as a container. `grafana/k6:latest` pulled **v2.2.0**,
which satisfies `lab/README.md`'s v2.x pin; the tag is still floating, so
pin it if you want a run reproducible a year from now.

## Unblock pass — Docker daemon up (2026-08-19, same day, later)

The daemon was started between the two passes. Everything above that said
"BLOCKED — Docker daemon down" was re-run. This section is what happened,
including the parts that did not work.

### The environment the second pass actually ran in

Docker Desktop 4.78.0, engine 29.5.3, `linux/arm64`, **4 CPUs and 4.8 GiB**
inside the VM, with nine unrelated containers already resident (~900 MiB).
Each topic was brought up under its own project name (`-p layer10-topic3`,
`-p layer10-topic2`), used, and torn down with `down -v` before the next.
Two collisions with this particular host are worth naming because they are
not repo defects and will not reproduce elsewhere: **port 5432 is taken by
a host Postgres**, and **port 8081 — the lab's documented model-server port
— is taken by an unrelated container**, which is why the Prometheus
`model_server` target reports `403 Forbidden` rather than a connection
refusal.

### Eight defects, all of them first contact with a runtime

**1. `lab/api/requirements.txt`: `asyncpg==0.30.0` cannot be installed on
the pinned Python.** `python:3.14-slim` has no `gcc` and asyncpg 0.30.0
publishes no cp314 wheel, so `docker compose up -d db api` died in the
build with `error: [Errno 2] No such file or directory: 'gcc'`. Bumped to
`asyncpg==0.31.0`, which ships
`asyncpg-0.31.0-cp314-cp314-manylinux…aarch64.whl`. The whole `api` service
— and therefore all of topics 3 and 5's service half — was unbuildable as
written.

**2. `lab/docker-compose.yml`: the Postgres volume was mounted at the
pre-18 path.** `pgdata:/var/lib/postgresql/data` against `postgres:18.6`
makes the entrypoint refuse to start: *"in 18+, these Docker images are
configured to store database data in a format which is compatible with
pg_ctlcluster"*. `db` exited 1 and `api` reported `dependency failed to
start`. Moved to `pgdata:/var/lib/postgresql`, with the reason in a
comment.

**3. `lab/docker-compose.yml`: the published Postgres port was hardcoded.**
`"5432:5432"` aborts the entire `up` on any machine that already runs
Postgres. Now `"${DB_PORT:-5432}:5432"` — the default is unchanged, `api`
still reaches `db:5432` on the compose network, and this pass ran with
`DB_PORT=55433`.

**4. All three k6 scripts threw their own summaries away.** Each
`handleSummary` returned `'summary.json': …`, written into a container
created by `run --rm` and deleted with it. `fanout.js`'s own instructions
say to run `-e N=1` first *and keep that summary*, which was not possible.
Added a writable `./out:/out` bind mount to the `k6` service and pointed
each script at `/out/<script>-<param>.json`.

**5. `pool_ramp.js` and `arrival_rate.js` printed none of the numbers their
topic READMEs ask you to record.** `handleSummary` *replaces* k6's default
summary, so a run reported `dropped_iterations` and nothing else — no
latency at all. Both now print the percentiles that make up a row of the
topic's table.

**6. `arrival_rate.js` was measuring the wrong thing and calling it TTFT.**
This is the serious one. The script asserted that `http_req_waiting` "for a
streaming completion is TTFT as the client experiences it". It is not: an
ASGI `StreamingResponse` flushes the response **header** before it pulls
the first item out of the body generator, so `http_req_waiting` is the time
to the header. Measured, same stack, same runs:

| λ (stub, capacity ≈ 12.5 req/s) | k6 `http_req_waiting` p50 | `gateway_ttft_seconds` p50 | full response p50 |
|---|---|---|---|
| 4 | 2.8 ms | 25 ms | 741 ms |
| 12 | 2.8 ms | 1,263 ms | 1,994 ms |
| 16 | 3.0 ms | 6,533 ms | 7,259 ms |

The client-side number moves by 0.2 ms across a 4x change in arrival rate
while the true TTFT moves by a factor of 260. A reader following the
script's own "what to look for" would have read the flat column, concluded
"flat p99 while RATE rises is a broken experiment", and gone hunting for a
closed-loop generator that was never there. Renamed the trend to
`first_byte` with the reason on the line above it, and added
`setup()`/`teardown()` that diff the gateway's `gateway_ttft_seconds`
buckets across the run and print the real TTFT next to it.

**7. `fanout.js` asked for a comparison it could not compute.** The
independence prediction for the p99 of a max-of-N is the single-call
quantile at `0.99^(1/N)` — 99.90% at N=10, 99.95% at N=20 — and
`summaryTrendStats` emitted neither. Added `p(99.9)` and `p(99.95)`, and
the script now prints the exact quantile level its own prediction needs.

**8. `pool_ramp.js`'s prescribed λ=400 row is void as configured.**
`maxVUs: RATE * 10` cannot cover an open-loop generator pointed at an
*unbounded* queue: above the wall the backlog grows at `(λ − c/W)` per
second for the whole run, so the VU ceiling has to scale with duration.
The run reported `dropped_iterations = 2903` and the script's own threshold
correctly voided it. Shortening to 20 s still dropped 943. Added a
`MAX_VUS` override and a message that names the three real fixes (more
VUs, shorter run, or a bounded queue via `POOL_PROFILE=budgeted`).

### Topic 3 — what the service experiment actually shows

Default profile: `pool_size 5`, `max_overflow 10`, so `c = 15`, and with a
measured `W ≈ 53.7 ms` the wall is `c/W ≈ 279 req/s`. Measured peak
completion rate under saturation: **276 req/s** (fixed) and **271 req/s**
(exponential). The arithmetic predicts the observed ceiling to within 2%.

Three timers, service side, 45 s per row, `dropped_iterations = 0` except
where noted:

| λ | acquire mean | acquire p99 | query mean | query p99 | request mean |
|---|---|---|---|---|---|
| 50 | 0.3 ms | 1.6 ms | 53.4 ms | 99.5 ms | 54.7 ms |
| 200 | 3.5 ms | 154.6 ms | 52.2 ms | 99.6 ms | 56.4 ms |
| 250 | 215.9 ms | 1,679 ms | 53.7 ms | 99.9 ms | 271.7 ms |
| 400 (row void, dropped 2,903) | 6,436 ms | ≥10 s | 54.1 ms | 249.9 ms | 6,492 ms |

That is the claim the topic exists for, and it holds: **query time is flat
across an 8x change in arrival rate while total goes vertical, and the
entire difference is acquire wait.** At λ=400 the mean request is 6,492 ms
of which 6,436 ms is waiting for a pool slot.

Confirmed from the other side, during the λ=400 run:

```
select state, wait_event_type, count(*) from pg_stat_activity where usename='app' …
active | Timeout | 15
```

Exactly fifteen backends, sampled six times over the run. `c` is
`pool_size + max_overflow`, not `pool_size`, and Postgres says so.

**The fix arm.** `POOL_PROFILE=budgeted` → `pool_size 20`, `max_overflow
0`, `c = 20`, deadline 500 ms, sheds. At λ=300, comfortably inside its
400 req/s wall: `dropped_iterations 0`, request p99 **401 ms** against the
default profile's 1,900 ms at a *lower* λ, and 12 sheds out of 13,501
(0.09%). The knee moved and the failure became explicit.

**But the deadline is not a bound, and the honest record says so.** At
λ=400 — only 8% past its own wall — the budgeted profile shed **15,786 of
17,267** requests (91.4%) and *still* reported a handler p50 of 791 ms and
a p99 of 4,406 ms against a 500 ms deadline. `asyncio.timeout` fires only
when the event loop gets round to it, and unwinding a cancelled request
still costs a round trip to Postgres to kill the in-flight query. A caveat
saying so was added to `lab/api/app.py`'s docstring. Read
`api_request_seconds` and `api_shed_total` together or the 503s look like
a latency improvement.

**Fan-out tail compounding**, λ=10 fan-outs/s, exponential 50 ms service,
`dropped_iterations = 0` on all three rows:

| N | fan-out p99 | independence prediction (single-call `q(0.99^(1/N))`) | measured vs predicted |
|---|---|---|---|
| 1 | 273.6 ms | 270.9 ms (p99) | +1% — sanity check |
| 10 | 533.0 ms | 436.9 ms (p99.9) | **+22%** |
| 20 | 6,268 ms | 4,270 ms (p99.95) | **+47%** |

Measured worse than the independence floor at both widths, in the direction
the script says to expect. Note the N=20 row puts 200 req/s through the
`api` service, close enough to the knee that the single-call distribution
has itself blown out (p90 985 ms) — the correlation being measured there is
partly shared queueing, which is exactly the mechanism, and partly the row
being taken past the service's comfortable range.

**One negative result, recorded because it is one.** Running `DIST=fixed`
against `DIST=exp` at λ=250 to see Kingman's `(c_a²+c_s²)/2` on the service
side does **not** work in a 45 s run. Mean acquire wait over repeated runs:
fixed 215.9 / 459.8 / 318.6 ms, exponential 26.6 / 241.9 ms. The run-to-run
spread inside each arm is larger than the gap between the arms, so a single
run can be read as confirming Kingman, contradicting it, or showing
nothing. This is the same failure the first pass fixed inside
`python/pool_queueing.py` (defect 2 above), reappearing in the service
version: near ρ≈0.9 the mean wait is a heavy-tailed average and 45 s does
not contain enough of it. The topic README is right to keep the Kingman arm
in the Python program, where the run length is under its own control.
Nothing was added to the README on the strength of these five runs.

### Topics 2 and 6 — what could and could not be run without a model

There is still no converted model on this machine, so the *engine* half of
topic 2 — prefix-cache hit rate, preemptions, the KV-block accounting — is
untouched and remains blocked. What was blocked *only* on the daemon is now
verified:

- `gateway` builds, serves, and imports `prompt_layout` **out of the bind
  mount** from topic 2's `python/` (`/healthz` returns `block_size 16`),
  so the service and `test_prefix_stability.py` genuinely cannot drift.
- `host.docker.internal` resolves from inside the `gateway` container.
  This is the check `lab/README.md` says to run before trusting anything,
  and it passes.
- Prometheus scrapes `gateway` (target up) and the series are queryable at
  `localhost:9090/api/v1/query`. Grafana 11.1.0 comes up with the
  Prometheus datasource provisioned at `http://prom:9090`; the dashboards
  directory is deliberately empty and documented as such.
- `PROMPT_VOLATILE=head` vs `tail` changes `/debug/prompt`'s
  `identical_head` from `false` to `true` at 1,899 approximate tokens.
- Cancellation propagates all the way: 10 abandoned requests →
  `client_disconnect 10` → `gateway_upstream_cancelled_total 10` →
  10 cancellations counted upstream, with upstream in-flight back to zero.
- Shadow mirroring cannot touch the primary. With a live-but-slower
  candidate: 12/12 shadow ok, primary 12/12 ok, primary TTFT mean 12.4 ms.
  With the candidate's port dead: 12/12 shadow *error*, primary still 12/12
  ok, TTFT mean 13.6 ms.

**About the stub.** `lab/tools/fake_upstream.py` was added to run the
plumbing without a 16 GB download. **It is not a model** — no weights, no
text, and every latency in the λ table above is one that file computed from
its own constants (8 decode slots, 200 tok/s, prefill charged per uncached
16-token block). Its numbers are labelled "stub upstream" wherever they
appear and must never be mixed into a table of engine measurements. It
earns its place by making the k6 → `gateway` → `host.docker.internal` path
executable, which is how defect 6 was found. For the record, with the
stub's block cache cleared between arms, `PROMPT_VOLATILE=tail` gave 17,970
cached blocks against 180 misses and a 25 ms TTFT, `=head` gave 0 cached
against 18,000 misses and 386 ms — a 15x TTFT difference that demonstrates
the *shape* of the prefix-cache argument on a machine that charges for it
by construction, and demonstrates nothing at all about any real engine.

### Still blocked, and what each would need

| Item | Why | What it needs |
|---|---|---|
| Topic 1 model conversion, and everything downstream of it | Was never a Docker block. Needs a ~16 GB Hugging Face download, and the measurement is the reader's exercise | `python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 --mlx-path ./q4` — 243 GB free here, so it is bandwidth and the reader's intent, not disk |
| Topic 4 `python/sample_determinism.py` | Needs a real sampler behind an OpenAI-compatible endpoint. The stub has no logits, so it cannot stand in | the converted model, then `python3 -m mlx_lm.server --model ./q4 --port 8081` |
| Topic 2 engine-side load runs | Prefix-cache hit rate, preemption counts and KV-block accounting exist only in a real engine's `/metrics` | the converted model on a free port, plus the matching edit to `prometheus/prometheus.yml` (its target port is hardcoded and `MODEL_URL` does not reach it) |
| Topic 6 real shadow comparison | Needs two model servers producing comparable output | two converted models on two free ports |
| Topic 7 longer run on a real corpus | No corpus on this machine | supply `tinystories.txt` and re-run the printed command |
| Topic 7 rented-GPU steps | No NVIDIA GPU | rent one; `python/mfu.py` transfers unchanged |
| Topic 8 attribution-graph cross-check | Not installed, and this pass installed nothing | `pip install circuit-tracer` |

Everything else that carried an unblock command in the first pass has now
been run.

## Portability

No `<sys/epoll.h>`, no `/proc`, no cgroup paths anywhere in this layer's
code. Topic 2's C++ gateway uses `poll(2)`, which exists on Darwin.
Everything that cannot run natively here is labelled and blocked with a
command, not silently broken.
