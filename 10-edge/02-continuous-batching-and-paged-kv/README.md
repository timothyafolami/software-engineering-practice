# Layer 10 · Topic 2 — Continuous batching, paged KV, and prefix caching under real load

### The takeaway (read this first)

**The one idea:** on a loaded inference server the *scheduler* sets your
p99, not the model. Static batching makes every request in a batch wait
for the longest one; continuous batching admits and retires requests every
decode step. Paged KV cache — fixed 16-token blocks allocated on demand —
is what makes that possible without reserving worst-case memory per
request, and prefix caching is what makes a shared 2000-token system
prompt free instead of re-paid on every single call.

**Why it matters in practice:** the most common LLM latency incident is
not a slow model, it is a deep queue that no dashboard shows. GPU
utilisation reads 95%, throughput looks healthy, and TTFT p99 is 40
seconds because three hundred requests are waiting to be admitted. The
second most common is a one-line change that puts a timestamp at the
*top* of the system prompt, taking prefix-cache hit rate from ~100% to
~0% and tripling prefill work overnight with no deploy that looks
suspicious.

**You'll know it landed when:** you can name the four metrics that catch
each of those in under a minute, and explain why an open-loop load
generator finds them while a closed-loop one hides them.

## The concept

vLLM's V1 engine is the reference implementation worth reading as source
— which the roadmap's "read one library a month" rule would have you do
anyway. Check the version you are actually running before trusting any
description of it, including this one: the
[releases page](https://github.com/vllm-project/vllm/releases) is the
source of truth, and blog posts about vLLM are routinely a year stale on
version numbers.

**The two queues.** The engine keeps `waiting` and `running`. Each
scheduling step prioritises in-flight decodes, then admits new prefills,
and can mix both in a single batch. A request in `waiting` is consuming
nothing but your customer's patience, and it appears in no
utilisation metric — that is the whole reason queue depth has to be
scraped explicitly.

**Paged KV.** The KV cache is a pool of fixed-size blocks (16 tokens by
default). A request takes `ceil(new_tokens / block_size)` blocks from a
free list and returns them on completion. Nothing is reserved for the
worst case, nothing fragments, and the memory question becomes a simple
counting question: blocks free vs blocks needed. Topic 1's KV arithmetic
tells you how many bytes a block is.

**Prefix caching.** Each *complete* block of prompt tokens is hashed and
kept as hash → block; a later request whose first blocks hash identically
reuses them with a refcount bump instead of recomputing. Two consequences
follow directly from that design and both are load-bearing:

- It is a **prefix** match, so one differing byte near the start
  invalidates everything after it. A request id or timestamp at position
  0 makes the entire prompt uncacheable.
- It matches on **block-aligned** chunks, so a shared prefix shorter than
  one block is invisible to it, and a shared prefix of 100 tokens caches
  only the first 96 (six blocks of 16) at default settings.

**Chunked prefill** (on by default in V1, with a token budget in the
thousands) splits a long prompt across scheduling steps so it cannot
monopolise one. It is the explicit TTFT-versus-ITL knob: bigger chunks
finish prefills sooner and stall decodes longer.

**Preemption.** When blocks run out, V1 evicts a running request and
*recomputes* it later (V0 swapped its KV to CPU instead). A non-zero
preemption count is an alert, not a curiosity: it means you
oversubscribed KV memory, and the work is being done twice.

**The 2026 landscape, briefly.** vLLM is the default choice — broadest
hardware support, one `pip install`. SGLang wins when the workload is a
prefix *tree* (agents, multi-turn chat, eval harnesses) because
RadixAttention plus cache-aware routing fits that shape structurally.
TensorRT-LLM wins if you are NVIDIA-only and will pay operational cost
for latency. Disaggregating prefill and decode onto separate replicas has
moved from paper to production pattern, and pays when prompts are long
and QPS is non-trivial — it is the same idea as this topic's core
observation, taken to its conclusion: the two phases have different
bottlenecks, so stop making them share a machine.

## How each language actually gets there

Six languages, and they earn it — but not on the server side. The
scheduler is inside vLLM and is Python-fronted C++/CUDA; rewriting it in
another language teaches nothing. What differs per runtime is the
**gateway**, and specifically one question with six different answers:
*when a client hangs up, does the in-flight upstream request actually get
cancelled, or does it keep holding KV blocks for a response nobody will
read?* Under load that is a meaningful fraction of your cache.

**Python (FastAPI).** Cancellation is cooperative and explicit: you poll
`await request.is_disconnected()`, or you rely on the ASGI server
cancelling the handler task on `http.disconnect` — which then raises
`CancelledError` at the next `await`. If your handler is inside a long
`httpx` streaming read, that arrives promptly; if it is inside a blocking
call, it does not arrive at all until the call returns.

**Node.js.** `req.on('close')` fires on the event loop, and the modern
form is to hang an `AbortController` off it and pass `signal` into
`fetch`. Same caveat as everywhere in Node: a CPU-bound stretch on the
main thread delays the close event with everything else, so cancellation
latency is bounded below by your worst synchronous block.

**Go.** The one that gets this right by construction. `r.Context()` is
already cancelled when the client disconnects, and every well-written
client takes a `context.Context`, so cancellation propagates through the
whole call chain without you writing anything. This is why gateways in
front of expensive backends are so often Go, and it is a better reason
than "Go is fast."

**Rust (axum/tokio).** Cancellation is *dropping the future*. When the
connection closes, the task is dropped, and everything it owned unwinds —
the strongest guarantee here, and the one that requires the least
discipline. The corresponding hazard is that a drop at an arbitrary
await point can leave external state half-updated, which is the
async-Rust version of "cancellation is not free."

**C++.** No cancellation mechanism exists at all. A thread blocked in
`read()` on the upstream socket learns nothing about the downstream one;
you must either poll the client socket for EOF yourself or shut down the
upstream fd from another thread. This is the version that shows what the
other five runtimes are doing *for* you — same argument as Layer 1's
C++/Rust pairing.

**Java.** Servlet async completion and `AsyncListener.onError`, or with
virtual threads (Java 21+) the far simpler shape: one virtual thread per
request in blocking style, interrupted when the connection closes. The
gateway is exactly the workload virtual threads were designed for — many
concurrent requests, each mostly waiting.

**Node also appears as k6's scripting language**, which is not a language
comparison, just a fact about the load generator.

## The experiment

The stack is [`../lab/README.md`](../lab/README.md): `gateway`, `k6`,
`prom` and `grafana` in compose, with the **model server on the host**
because Docker Desktop on macOS cannot reach Metal. Three runs.

1. **Find the knee.** k6 `constant-arrival-rate` — an *open* model with a
   fixed λ, never a fixed VU count — stepped upward across runs. At each
   rate record TTFT p50/p99, ITL p99, queue wait, `num_requests_waiting`,
   `num_requests_running`, preemption count and prefix-cache hit rate.
   Plot latency against λ and find where it goes vertical. Predict that
   point from topic 3's arithmetic *before* you run the ramp.
2. **Incident replay — prefix cache collapse.** Identical load, identical
   2000-token system prompt, one variable: a per-request unique string
   (`Current time: 2026-08-18T14:02:11Z`, or a request id) placed at the
   *start* of the prompt versus at the *end*. Measure cache hit rate,
   prefill tokens/s and TTFT p99 both ways. Then fix it the way a real PR
   would: volatile content strictly after the stable prefix, plus a test
   asserting the first N rendered prompt tokens are byte-identical across
   two different requests. That test is the deliverable — it is what stops
   the regression coming back in six months.
3. **Incident replay — the dashboard that lies.** Past the knee, screenshot
   a Grafana panel showing only throughput and GPU utilisation. Then add
   queue depth and TTFT p99 to the same panel and screenshot it again. The
   *pair* of screenshots is the artifact, and it is the most portable
   thing in this layer: it is the same lesson as Layer 6's RED/USE
   material, with an inference server as the subject.

Optionally, run step 1 twice more with the gateway written in Go and in
C++, killing the k6 VUs mid-run each time, and count how many upstream
requests the model server reports as aborted. That is the
cancellation-propagation experiment above, made numeric.

## How to run

**Runs 1 and 3 need the compose stack and a model server; run 2 and the
cancellation experiment do not.** Start with the ones that need nothing.

The prefix-cache incident, and the regression test that is the deliverable
— no Docker, no model, no GPU:

```
python3 python/test_prefix_stability.py
```

It renders two different requests both ways and asserts the first N
characters are byte-identical. `tail` passes, `head` fails, and the failing
arm is printed on purpose: a guard that cannot fail is not a guard.
`python/prompt_layout.py` is the single implementation of that rendering —
`../lab/docker-compose.yml` mounts it into the `gateway` container rather
than copying it, so the service and the test cannot drift apart.

The cancellation-propagation experiment, one program per runtime. Each
stands up a stub model server that streams 40 tokens at 100ms, puts a
gateway in front with a naive and a cancelling handler, hangs a client up
after 500ms, and prints what the upstream saw. All six bind 127.0.0.1 on
ephemeral ports and take no arguments:

```
pip install -r python/requirements.txt      # fastapi, uvicorn, httpx
python3 python/cancel_propagation.py
node nodejs/cancel_propagation.js
cd golang && go run cancel_propagation.go && cd ..
cargo run --release --manifest-path rust/cancel_propagation/Cargo.toml
c++ -O2 -std=c++20 -pthread -o /tmp/cancel_cpp cpp/cancel_propagation.cpp && /tmp/cancel_cpp
cd java && javac CancelPropagation.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild CancelPropagation && cd ..
```

Then the load runs, which need the stack:

```
cd ../lab
docker compose up -d prom grafana gateway

# on the HOST, not in a container — no Metal in Docker Desktop
python3 -m mlx_lm.server --model ./q4 --port 8081

docker compose --profile load run --rm k6 run /scripts/arrival_rate.js -e RATE=2
docker compose --profile load run --rm k6 run /scripts/arrival_rate.js -e RATE=4
docker compose --profile load run --rm k6 run /scripts/arrival_rate.js -e RATE=8

# the metrics that matter, scraped directly
curl -s localhost:8081/metrics | grep -E 'num_requests|preemption|prefix_cache'
curl -s localhost:8000/metrics | grep -E 'gateway_(ttft|inflight|upstream_cancelled)'
```

`PROMPT_VOLATILE=head` / `PROMPT_VOLATILE=tail` on the `gateway` service
selects the two variants of run 2 under real load. Everything the run
lines depend on — service names, ports, script paths, env vars — is
specified once in [`../lab/README.md`](../lab/README.md).

`gateway_upstream_cancelled_total` is the numeric version of the
cancellation experiment: if it stays at zero while
`gateway_requests_total{outcome="client_disconnect"}` climbs, abandoned
requests are still holding KV blocks on the server.

## Predict, then record

- The knee will be at λ ≈ ___ req/s.
- Moving the timestamp to the front will change hit rate from ___% to
  ___%, and TTFT p99 by ___x.
- At 1.2x the knee, queue depth will be ___ and TTFT p99 will be ___ s.
- Preemptions will first appear at λ ≈ ___.

| λ (req/s) | TTFT p50 | TTFT p99 | ITL p99 | queue depth | cache hit % | preemptions |
|---|---|---|---|---|---|---|
| | | | | | | |

| Prompt variant | cache hit % | prefill tok/s | TTFT p99 |
|---|---|---|---|
| volatile at head | | | |
| volatile at tail | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **TTFT p99 stays flat as λ rises.** Your generator is closed-loop and
  self-throttling — textbook coordinated omission. Check that the
  executor is `constant-arrival-rate` and not `constant-vus`, and check
  k6's `dropped_iterations`: non-zero means the *generator* saturated,
  not the server, and every latency number in that row is invalid.
- **Latency looks great and the error rate is 3%.** You are histogramming
  only successes. Shed load never appears in a latency histogram, so your
  p99 improves every time the system gets worse. Count 429s and timeouts
  as their own series.
- **0% cache hit in both variants.** Prefix caching may be off in your
  build, or your prompt is shorter than the block boundary that makes it
  visible. Sanity-check with two byte-identical requests back to back
  before concluding anything about placement.
- **100% cache hit in both variants.** The volatile string is not
  actually varying (a cached clock, a constant request id), or the
  gateway is templating it after the tokenizer sees the prompt. Print the
  first 64 tokens of two different requests and diff them.
- **Queue depth always zero while latency climbs.** You are scraping the
  wrong endpoint, or the queue is in the gateway rather than in the
  engine. Both are real places for a queue to be; know which one you are
  looking at.

## Answer before moving on

1. Continuous batching admits new requests every decode step. Explain
   precisely why that improves TTFT p99 without improving single-request
   latency at all — and what it costs the request already running.
2. Prefix caching matches block-aligned chunks of 16 tokens. Construct a
   prompt layout that is 99% shared between requests and still gets a 0%
   hit rate. Then construct one that is only 60% shared and gets a high
   hit rate.
3. Preemption in V1 recomputes rather than swapping to CPU. Under what
   ratio of PCIe bandwidth to compute would swapping win again — and what
   does the answer look like on a unified-memory Mac, where there is no
   PCIe hop at all?
4. Your dashboard shows 95% GPU utilisation and stable throughput while
   TTFT p99 is 40s. Write the three-line explanation you would give in an
   incident channel, and name the one metric you would add to the
   dashboard tonight.

## Next up

[Topic 3 — Little's Law, Kingman, and why independent p99s
compound](../03-littles-law-and-tail-compounding/README.md). You just
found a knee empirically by ramping load. Topic 3 is how to predict where
it will be with one line of arithmetic, and why LLM traffic hits it
earlier than CRUD traffic at identical utilisation.
