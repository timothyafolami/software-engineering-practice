# Layer 10 · Topic 1 — Prefill, decode, and being memory-bandwidth-bound

### The takeaway (read this first)

**The one idea:** generating one token reads *every weight in the model*
out of memory and does about 2 floating-point operations per weight. That
ratio — roughly 1 FLOP per byte — sits far below what any accelerator
needs to keep its arithmetic units busy, so decoding is a memory transfer
with a little arithmetic attached. Tokens/sec at batch 1 ≈ **memory
bandwidth ÷ bytes of weights**, and little else.

**Why it matters in practice:** it tells you which optimisations are worth
trying *before* you try any. Quantizing 16-bit → 4-bit moves 4x fewer
bytes, so it buys close to 4x decode speed. A card with more FLOP/s and
the same bandwidth buys nothing at batch 1. Batching is the only lever
that changes the ratio itself, because one weight read then serves the
whole batch — and speculative decoding works for the same reason:
verifying k drafted tokens costs one weight read instead of k.

**You'll know it landed when:** given parameter count, quantization,
memory bandwidth and context length, you can predict tokens/sec on paper
to within about 30%, and say which of "more FLOPs / more bandwidth /
smaller weights / bigger batch / shorter context" would actually move it.

## The concept

Inference has two physically different phases and a tool that reports one
blended "tokens/sec" is averaging across both, which makes it useless
here.

**Prefill** processes the whole prompt at once. It is matrix × matrix:
each weight is read once and used S times, where S is the prompt length.
Arithmetic intensity is high, the phase is compute-bound, and its cost
grows with prompt length. Prefill sets **TTFT** (time to first token).

**Decode** emits one token at a time. It is matrix × vector: each weight
is read once and used once per sequence in the batch, so arithmetic
intensity ≈ batch size. The phase is bandwidth-bound. Decode sets **ITL**
(inter-token latency) and **TPOT** (time per output token).

### The roofline, and the ridge point

The roofline model says achievable throughput is

```
FLOP/s_achievable = min( peak FLOP/s , bandwidth × arithmetic_intensity )
```

The two terms cross at the **ridge point**, which is just peak FLOP/s
divided by peak bandwidth, in FLOPs per byte. Below it you are
bandwidth-bound; above it you are compute-bound. Everything about
inference performance follows from where your workload sits relative to
that one number, so compute it for your own hardware before anything
else:

```
ridge point = peak FLOP/s ÷ peak bandwidth          [ FLOP per byte ]

H100 SXM    989 TFLOP/s bf16 ÷ 3.35 TB/s   ≈ 295
M1 (base)   2.6 TFLOP/s FP32 ÷ 68.25 GB/s  ≈  38
M1 Pro      5.2 TFLOP/s FP32 ÷ 200 GB/s    ≈  26
M1 Max     10.4 TFLOP/s FP32 ÷ 400 GB/s    ≈  26
```

The inputs are vendor-published figures — NVIDIA's H100 datasheet for the
first row, Apple's published GPU FP32 throughput and memory bandwidth per
part for the rest. The division is the only thing this page is claiming.
Check which part you have with `system_profiler SPHardwareDataType`
before using any row.

**Two traps in that table, both worth naming.**

First, **do not mix parts.** An earlier draft of this layer wrote "~50 for
an M1 Pro (≈10 TFLOP/s over 200 GB/s)". The 200 GB/s is an M1 Pro figure;
the ~10 TFLOP/s is an M1 **Max** figure. Combining a Max numerator with a
Pro denominator produced a ridge point roughly 2x too high, which would
have made batch-1 decode look four times further from the roof than it
is. Within one product line the ratio is remarkably stable — Apple scaled
GPU cores and memory channels together, so Pro and Max land on the same
≈26 — and that stability is exactly why the mixed-part number stood out
as wrong.

Second, **use the FLOP/s figure for the dtype and the mode you actually
run.** NVIDIA's headline "1,979 TFLOPS BF16" for H100 SXM carries an
asterisk meaning *with 2:4 structural sparsity*; dense bf16 is half that,
which is the 989 used above. Marketing FLOP/s is the single most commonly
mis-transcribed number in this whole subject.

Batch-1 decode sits at arithmetic intensity ≈ 1. Against a ridge point of
26 to 295, that is not "near the roof" — it is on the floor, and the
floor is memory. So:

```
decode tok/s (batch 1)  ≈  achievable bandwidth ÷ bytes of weights
```

A clean round-number example, deliberately not the model you are about to
measure: 1 GB of weights on a machine sustaining 100 GB/s gives 100
tok/s, and if you halve the weights you double the rate. That is the
whole model. The interesting work is in *achievable* — no real machine
sustains its spec-sheet bandwidth, and finding your actual fraction is
step 1 of the experiment.

### The second byte stream: the KV cache

Weights are not the only thing decode reads. Every step also re-reads the
whole KV cache for the sequence:

```
KV bytes per token = 2 (K and V) × layers × kv_heads × head_dim × dtype_bytes
```

Llama-3-8B has 32 layers, 8 KV heads under grouped-query attention, head
dim 128, at fp16:

```
2 × 32 × 8 × 128 × 2 bytes = 131,072 B = 128 KiB per token
```

So an 8k-token context holds 8192 × 128 KiB = **1 GiB**, re-read on every
decode step alongside the weights. That is the mechanism behind "the model
got slower as the conversation went on" when nothing about the model
changed, and it is why grouped-query attention (fewer `kv_heads`) and
multi-head latent attention (a compressed KV representation) exist at all:
both attack this term and only this term.

Note what the formula does *not* contain: batch size. Per-token KV cost is
per sequence, so total KV bytes scale with batch × context while weight
bytes stay fixed. Push batch high enough at long context and the KV
stream, not the weights, becomes the thing you are waiting for — which is
where topic 2's block allocator starts to matter.

## How each language actually gets there

**Three languages here, not six, and the reason is the point:** the
mechanism lives in the memory controller, not in any runtime. The
languages appear to *demonstrate* that.

**Python** owns the model side outright: `mlx` / `mlx-lm` on Apple
Silicon, PyTorch + vLLM elsewhere, and every measurement tool
(`mlx_lm.generate --verbose`, `llama-bench`, vLLM's `/metrics`) has a
Python front door. NumPy's `b[:] = a` is a memcpy in C underneath, so
Python measures the machine rather than the interpreter here — which is
the one case in this lab where "it's Python" costs you nothing.

**C++** is the reference bandwidth measurement — a STREAM-style triad
loop with `-O3`, nothing between the loop and the load/store units. If
any language is going to reach the memory controller's ceiling, it is
this one, and it is the number the other two get compared against.

**Rust** at `--release` should land on the same figure as C++, and it is
worth running specifically to confirm that bounds checking and the
ownership model cost nothing in a loop the optimiser can prove is in
range. A gap here means you wrote the loop badly (indexing rather than
iterating over slices), not that Rust is slower at memcpy.

Go, Node and Java are omitted from the bandwidth harness on purpose:
they would land on the same GB/s, because a sequential streaming loop is
the one workload where runtime differences vanish entirely. That null
result *is* the lesson from Layer 1 restated — when you are bound by
physics, the language stops mattering — and it does not need three more
implementations to make it. Go and Node earn real places in topic 2 and
topic 3, where the runtime genuinely is the subject.

## The experiment

1. **Establish achievable memory bandwidth — not the spec sheet.** A
   STREAM-style benchmark over a buffer several times larger than the
   last-level cache (~2 GB is safe): `copy` (`b[:] = a`), `add`
   (`c[:] = a + b`), and `triad` (`c[:] = a + q*b`), each timed over
   several repetitions, reported in GB/s counting *all* bytes moved
   (a copy of N bytes moves 2N — read plus write). Do it in Python
   (NumPy), C++ and Rust. Take the best sustained figure as your ceiling.
2. **Predict, then measure decode.** Convert a ~7-8B model to 4-bit with
   `mlx_lm.convert`. Compute weight bytes from the on-disk size, divide
   your achieved bandwidth by it, and write that prediction down *before*
   generating. Then generate at least 512 tokens and read the reported
   decode tok/s.
3. **Separate the variables, one at a time.**
   - *Bytes:* the same model at 8-bit vs 4-bit. The speed ratio should
     track the on-disk *byte* ratio. There is no FLOP ratio to confound
     it — the arithmetic is identical, only the bytes moved change. This
     is the cleanest single confirmation of the whole model.
   - *Context:* the same model and the same quantization at 256, 4k and
     16k prompt tokens. Watch the KV term eat decode speed while TTFT
     grows with prefill. Record prefill and decode **separately**;
     anything reporting one blended number is averaging two different
     physical regimes.

## How to run

Everything here runs on the **host**, not in a container. Docker Desktop
on macOS has no Metal passthrough, so a containerised model server on
this machine runs on the CPU and measures nothing you want. See
[`../lab/README.md`](../lab/README.md) for the split.

```
pip install -r python/requirements.txt      # numpy
pip install mlx-lm                          # the model side, Apple Silicon
system_profiler SPHardwareDataType          # which M1: base, Pro, Max, Ultra

# 1. the ceiling: STREAM copy/scale/add/triad, three implementations
python3 python/stream.py
c++ -O3 -std=c++20 -o /tmp/stream cpp/stream.cpp && /tmp/stream
cargo run --release --manifest-path rust/stream/Cargo.toml

# 2. the prediction, written down before anything generates a token.
#    With no arguments it recomputes the KV arithmetic above so you can
#    check the README rather than trust it.
python3 python/predict_decode.py
python3 python/predict_decode.py --bandwidth <best GB/s from step 1>

# 3. the measurement
python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 --mlx-path ./q4
python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 8 --mlx-path ./q8
python3 python/predict_decode.py ./q4 ./q8 --bandwidth <best GB/s from step 1>

python3 -m mlx_lm.generate --model ./q4 --max-tokens 512 --verbose True --prompt "..."
python3 -m mlx_lm.generate --model ./q8 --max-tokens 512 --verbose True --prompt "..."
```

`python/stream.py` prints one extra row the C++ and Rust versions cannot:
NumPy's `c[:] = a + q*b` against `np.multiply(..., out=c)`. Same logical
work, but the naive form allocates two full-size temporaries per call. If
your Python numbers sit far below C++ and Rust, check which form you wrote
before concluding anything about the memory controller.

`c++ -O3` and `cargo --release` each report a one-thread and an
all-threads row, because which one is higher is a property of the machine,
not something to assume: on a part where one core already saturates the
memory controller, the extra threads only add contention.

While a long generation runs, watch power and clocks in a second shell —
`sudo powermetrics --samplers gpu_power,thermal -i 1000` — so a thermal
result does not get recorded as a bandwidth result.

## Predict, then record

Write these in [`../../PREDICTIONS.md`](../../PREDICTIONS.md) before you
run anything.

- Achievable bandwidth will be ___% of the spec-sheet figure for my part.
- My ridge point is ___ FLOP/byte, so batch-1 decode is ___x below it.
- 4-bit 8B → weights ≈ ___ GB → predicted decode ≈ ___ tok/s.
- 8-bit will be ___x slower than 4-bit, because on-disk sizes differ ___x.
- At 16k context, decode will be ___% of its speed at 256 tokens.

| Measurement | Predicted | Measured | Ratio |
|---|---|---|---|
| Achievable bandwidth, C++ (GB/s) | | | |
| Achievable bandwidth, Rust (GB/s) | | | |
| Achievable bandwidth, NumPy (GB/s) | | | |
| Decode tok/s, q4, short ctx | | | |
| Decode tok/s, q8, short ctx | | | |
| Prefill tok/s (prompt len ÷ TTFT) | | | |
| Decode tok/s, q4, 4k ctx | | | |
| Decode tok/s, q4, 16k ctx | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **Decode faster than the bandwidth bound.** Physically impossible at
  batch 1, so something is mislabelled: the tool reported prefill tok/s
  as decode, generation stopped early (count the tokens actually emitted,
  not the max you requested), or speculative decoding is silently on.
- **Under ~30% of the bound.** You are not bandwidth-bound at all.
  Suspect Python-side sampling overhead per token, a run short enough
  that model load time dominates (generate ≥256 tokens), or thermal
  throttling. Re-run from cold with `powermetrics` open.
- **8-bit and 4-bit identical.** The 8-bit path is probably dequantizing
  to fp16 before the matmul, so you moved fp16 bytes either way. The tell
  is on-disk size: if the files differ ~2x and the speed does not, the
  bytes are not reaching the compute unit in the format you think.
- **The three STREAM implementations disagree by more than ~10%.** That
  is a benchmark bug, not a language finding. Check the buffer is bigger
  than the last-level cache, that the compiler has not deleted a loop
  whose result is unused, and that you counted read+write bytes the same
  way in all three.
- **Context length changes nothing.** Either the server is truncating
  your long prompts, or prefix caching served the whole prompt from cache
  and you measured cache lookup. Check the reported prompt token count
  matches what you sent.

## Answer before moving on

1. Speculative decoding verifies k drafted tokens with one pass over the
   weights. Why does that not violate the bandwidth bound — what resource
   is it actually spending instead, and what property of the workload
   makes it lose money rather than make it?
2. You quantize 16-bit → 4-bit and measure 2.2x, not the ~4x the byte
   ratio predicts. Give three mechanisms that could account for the gap,
   and name one measurement that separates them.
3. Derive the batch size at which decode on your machine stops being
   bandwidth-bound, using the ridge point you computed and the fact that
   decode's arithmetic intensity ≈ batch size. Then say why real servers
   rarely reach it.
4. KV bytes per token do not depend on batch size, but total KV bytes do.
   For an 8k context on your machine's memory, how many concurrent
   conversations fit alongside the weights — and what does that number
   have to do with the scheduler in topic 2?

## Next up

[Topic 2 — Continuous batching, paged KV, and prefix caching under real
load](../02-continuous-batching-and-paged-kv/README.md). You now know what
one request costs. Topic 2 is what happens when three hundred of them
arrive at once and the scheduler, not the model, decides your p99.
