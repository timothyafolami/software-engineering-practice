# Layer 10 · Topic 4 — Quantization, numerical stability, and the determinism you didn't have

### The takeaway (read this first)

**The one idea:** low precision is a *bandwidth* optimisation first
(topic 1 says why), and precision interacts with numerical stability in
ways that surface as incidents rather than as wrong answers on paper. The
sharpest current example: temperature-0 inference is nondeterministic in
production not because GPUs are randomly nondeterministic, but because
reduction kernels give bit-different results at different *batch shapes*
— and batch shape depends on who else is on the server at that instant.

**Why it matters in practice:** you will be asked "why did the same
prompt give a different answer," and the honest answer is architectural,
not a shrug about floating point. You will be asked "is 4-bit safe," and
the honest answer is "the aggregate says yes, and the aggregate is the
wrong summary" — degradation concentrates in slices: long context, code,
arithmetic, non-English. Which is why topic 6 exists.

**You'll know it landed when:** you can explain why softmax needs a max
subtraction without looking it up, why the same request yields different
tokens under load at temperature 0, and exactly what you would give up to
stop it.

## The concept

### Why quantization works at all

Weights within a small group have a narrow dynamic range, so a per-group
scale recovers most of the information. Activations do not: they have
outlier channels that ruin any shared scale. That asymmetry is the
field's entire history in one sentence — weight-only 4-bit (GPTQ, then
AWQ with activation-aware scaling) worked first because it only had to
solve the easy half.

**Where 2026 actually is**, since this is where instincts go stale
fastest. Treat every line as "check before spending money on it":

- **FP8 is the production default** on Hopper/Blackwell — native hardware
  support, and vendors report well under a point of aggregate benchmark
  loss. Their numbers, not measured here.
- **NVFP4** (E2M1 with two-level scaling, Blackwell tensor cores) is real
  and is the direction of travel, but calibration tooling is still
  maturing; not the safe default as of mid-2026.
- **AWQ 4-bit weight-only** still dominates on A100/L40S-era hardware.
- **On your Mac:** MLX group quantization (`--q-bits 4` / `8`) or GGUF
  `Q4_K_M`.
- **Outdated:** treating bitsandbytes int8/nf4 as a *serving* path — it is
  a fitting-in-memory path, not a throughput one — and the blanket claim
  that quantization costs meaningful quality.

### Numerical stability: three things that bite

**1. Softmax without max-subtraction overflows.** `exp(800)` is `inf` in
fp32 and fp16 strains far earlier, so the standard form subtracts the max
first:

```
softmax(x)_i = exp(x_i − max(x)) / Σ_j exp(x_j − max(x))
```

This is **exact**, not an approximation: multiplying numerator and
denominator by `exp(−max(x))` changes nothing mathematically and moves
the largest exponent to `exp(0) = 1`. FlashAttention's online-softmax
rescaling is the same identity applied incrementally as tiles stream
through, which is why it is numerically *safer* than the naive version it
replaced, not merely faster.

**2. Accumulate in higher precision than you store.** fp16 attention with
fp16 accumulation loses badly at long sequence length, because you are
summing thousands of terms into a format with ~3 decimal digits of
precision. Storage precision and accumulation precision are separate
decisions and conflating them is a classic bug.

**3. Catastrophic cancellation.** `Var(x) = E[x²] − E[x]²` is correct
algebra and a terrible algorithm: for a variable with a large mean the two
terms are nearly equal, so subtracting them in float32 can return a
*negative* variance. Welford's online algorithm does not. This is not
academic — it shows up in normalisation layers, in drift monitors (topic
5), and in metric aggregation everywhere.

### The determinism finding

Thinking Machines Lab's *Defeating Nondeterminism in LLM Inference*
(September 2025) is the primary source and worth reading in full. Their
published result: sampling 1000 completions from Qwen3-235B at
temperature 0 gave **80 unique completions**, with all 1000 identical for
the first 102 tokens before diverging. Their fix — a `batch-invariant-ops`
library with fixed reduction order regardless of shape — gave 1000/1000
bit-identical outputs, at a throughput cost they report as 26s → 42s on
their benchmark, roughly 60%.

The cause is not the usual concurrency-plus-floating-point hand-wave.
Kernels are not **batch-invariant**: a reduction split differently across
a different batch shape sums the same numbers in a different order, and
floating-point addition is not associative, so the result differs in the
last bits. Those bits occasionally cross an argmax boundary between two
close logits, and from there the sequences diverge completely. Your batch
shape depends on other people's traffic.

The lesson generalises far past LLMs, and this is the sentence to keep:
**your result depended on how the work was partitioned, and the
partitioning depended on load.** Go find that bug in something you
already own.

## How each language actually gets there

Six, and unusually the *non-Python* ones carry the important half. The
model work is Python-only, but the mechanism — reduction order changing a
result — is a property of floating-point arithmetic that every one of
these languages exposes differently, and four of them let you reproduce
the batch-invariance finding on a CPU with no GPU at all.

**Python.** All of the model work: MLX or PyTorch for the softmax and
variance experiments, an OpenAI-compatible client for the determinism
runs. NumPy's `sum` uses pairwise summation, so it is *more* accurate than
a naive loop and its accuracy depends on array length — a small,
self-contained instance of the same phenomenon.

**C++.** The one where you can see the compiler change your arithmetic.
`-ffast-math` licenses reassociation and turns `a*b + c` into an FMA with
a different rounding profile; the same source then produces different
results at `-O2` and `-O3` on different targets. Build the softmax
experiment twice, with and without, and diff the outputs. This is also
where `__fp16` vs `float` accumulation is easiest to control explicitly.

**Rust.** The deliberate contrast to C++: no reassociation, ever. `f32`
arithmetic is IEEE-strict, FMA only happens if you call `f32::mul_add`
explicitly, and there is no stable `-ffast-math`. Same program, same bits,
every build — which makes Rust the reference implementation to check the
other five against.

**Go.** The best CPU-only reproduction of the batch-invariance result.
Sum ten million `float32` values by splitting the slice across W
goroutines and combining partials. Vary W from 1 to 64. The sum changes
with W, deterministically per W and differently across W — no GPU, no
kernels, the same bug. The Go spec is strict about float semantics, so
what you are seeing is purely partition order, which is exactly the
finding.

**Java.** Same experiment via `DoubleStream.parallel().sum()`, whose
result depends on how the spliterator split the work. Worth knowing the
history: `strictfp` used to be a keyword you needed for reproducible
results, and JEP 306 (Java 17) made all floating-point strict by default —
so Java went from "your result depends on the host FPU" to "your result
depends only on your partitioning," which is precisely the journey this
topic is about.

**Node.js.** The control case. JavaScript has one number type, `float64`,
and V8 does not reassociate, so the naive versions of these experiments
mostly *don't* break — which is the real lesson: "just use float64" is a
genuine mitigation that costs 2x bandwidth, and topic 1 told you exactly
what that costs at serving time. `Math.fround` lets you simulate float32
to show the failure returning.

## The experiment

1. **Stability, about 30 lines.** Naive softmax versus the LSE-stable
   form on logits whose max is 50, 200 and 800, in float32 and float16;
   find each crossover point. Then naive two-pass variance versus
   Welford's algorithm on a stream of `1e8 + N(0,1)` values, and watch the
   naive version return a negative number. Run both in Python, and the
   softmax half in C++ with and without `-ffast-math`.
2. **Partition-order determinism, no GPU required.** The Go and Java
   parallel-sum experiment above: same data, W workers, W from 1 to 64,
   printing the sum's exact bits (`%b` in Go, `Double.toHexString` in
   Java). Record how many distinct sums appear.
3. **Determinism, on your laptop, with a real server.** The same prompt at
   temperature 0, 200 times, (a) strictly serially and (b) with 32-64
   concurrent clients against the same server. Count distinct completions
   and the token index of first divergence. If your server exposes a
   deterministic or batch-invariant mode, measure both the uniqueness
   count *and* the throughput cost — the tradeoff is the finding, not the
   fix.
4. **Quantization, measured properly.** Convert to 8-bit and 4-bit;
   record bytes on disk, decode tok/s (topic 1's harness), and score a
   200-item eval **stratified by slice** — short context, long context,
   code, arithmetic, non-English — reporting per-slice deltas and never
   one aggregate. Topic 6 supplies the machinery to score it honestly;
   do not skip ahead and trust an aggregate.

## How to run

**Everything except run 3 works with no GPU, no server and no Docker.**
Numerical stability and partition-order determinism are properties of
floating-point arithmetic, and floating-point arithmetic is on your laptop
already.

Stability — the softmax crossover, and catastrophic cancellation:

```
pip install numpy
python3 python/softmax_crossover.py     # crossovers found by bisection, not quoted
python3 python/welford_vs_naive.py      # naive variance goes negative; Welford does not
```

The compiler changing your arithmetic — build the same file twice and diff:

```
c++ -O2 -std=c++20 -o /tmp/sm cpp/softmax.cpp && /tmp/sm
c++ -O2 -ffast-math -std=c++20 -o /tmp/sm_fast cpp/softmax.cpp && /tmp/sm_fast
diff <(/tmp/sm) <(/tmp/sm_fast)         # if this is empty, check the loop wasn't elided
```

Then the reference to check both of those builds against — Rust, where
`-ffast-math` does not exist, reassociation never happens, and an FMA
appears only where you wrote `mul_add`:

```
cargo run --release --manifest-path rust/strict_fp/Cargo.toml
cargo run           --manifest-path rust/strict_fp/Cargo.toml   # same numbers, on purpose
```

Partition-order determinism — the batch-invariance finding, reproduced on
a CPU in three languages. Same input, W workers, W from 1 to 64, exact bits
printed:

```
cd golang && go run parallel_sum.go && cd ..
cd golang && go run parallel_sum.go -workers 1,3,7,64 && cd ..
cd java && javac ParallelSum.java -d /tmp/javabuild && \
  java -cp /tmp/javabuild ParallelSum && cd ..
node nodejs/float64_control.js          # the control: one number type, float64
```

`nodejs/float64_control.js` is the wider-type arm and belongs in the run —
but read its output rather than assuming it is a null result, because it is
not one. JavaScript has only `float64` and V8 does not reassociate, so no
compiler licence is in play; partition order alone still produces distinct
sums, and the naive softmax still reaches `NaN`. What the wider type buys is
*spread*, not immunity: the same sweep under `Math.fround` (simulated
`float32`) has a relative spread orders of magnitude larger, and the spread
is what decides whether two close logits ever swap places. "Use a wider type"
is a real mitigation with a real price, and topic 1 already told you what
that price is at serving time.

Run 3 needs a server, on the **host** — no Metal in Docker Desktop:

```
python3 -m mlx_lm.server --model ./q4 --port 8081
python3 python/sample_determinism.py                    # both modes, 50 samples
python3 python/sample_determinism.py --n 200 --clients 32
```

It refuses to record anything if no server answers, and prints the command
to start one. A blocked experiment is not a null result.

Run 4 (quantization scored by slice) uses topic 1's decode harness for the
bytes-and-speed half and topic 6's eval machinery for the scoring half:
`python3 ../01-prefill-decode-and-bandwidth/python/predict_decode.py ./q4 ./q8`
for on-disk bytes and predicted decode, then topic 6 for the per-slice
deltas. Do not skip ahead and trust an aggregate score.

## Predict, then record

- Naive softmax will overflow at max logit ≈ ___ (fp32) and ___ (fp16).
- Naive two-pass variance goes negative at a mean of ≈ ___.
- The Go parallel sum will produce ___ distinct results across 7 worker
  counts.
- Serial temp-0 sampling gives ___ distinct outputs; concurrent gives
  ___, first diverging around token ___.
- 4-bit loses ___ points aggregate; the worst-hit slice will be ___.

| Run | distinct / 200 | first divergent token | tok/s |
|---|---|---|---|
| serial (batch 1) | | | |
| 32 concurrent | | | |
| deterministic mode (if available) | | | |

| Slice | fp16 | q8 | q4 | n |
|---|---|---|---|---|
| | | | | |

| Workers W | sum (hex bits) | distinct so far |
|---|---|---|
| | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **1 distinct completion under concurrency.** Confirm batching actually
  happened — check the server's running-batch metric. Thirty-two clients
  against a fast small model may still execute batch 1 on most steps, in
  which case the hypothesis was never tested. Raise concurrency or slow
  the model down.
- **Distinct completions even serially at batch 1.** Temperature 0 is not
  greedy in your client. Check `top_p`, `top_k` and per-request seeds;
  some servers treat temperature 0 as "a very small temperature" rather
  than argmax.
- **The parallel sum is identical for every W.** Your values are too
  well-conditioned (all similar magnitude, or exactly representable).
  Mix magnitudes — `1e8` alongside `1e-3` — so that reordering actually
  changes the rounding.
- **A 20-point slice delta at n=8.** That is a sample size, not a
  finding. Take it to topic 6 before believing it.
- **`-ffast-math` and strict builds agree exactly.** The optimiser may
  have deleted a loop whose result you never printed — the same defect
  Layer 1's race experiment hit. Print a checksum of every intermediate.

## Answer before moving on

1. Explain why max-subtraction in softmax is exact rather than an
   approximation, in one sentence, without using the word "stability."
2. Batch-invariant kernels cost roughly 60% throughput in the published
   benchmark. Name two production situations where you would pay that,
   and two where paying it would be an error — and say what you would do
   instead in the second case.
3. Your Go parallel sum is deterministic for a fixed W but differs across
   W. A colleague says "just fix W." Give two reasons that is not a fix
   in a real service.
4. 4-bit quantization loses 0.4 points aggregate and 6 points on the code
   slice. Write the two-sentence recommendation you would put in the PR
   description, and state the decision rule you would have written
   *before* seeing those numbers.

## Next up

[Topic 5 — Data pipelines, versioning, and drift](../05-pipelines-versioning-and-drift/README.md).
You have just seen a result depend on something nobody versioned. Topic 5
is the systematic version of that problem: the model artifact is the least
interesting version in the system.
