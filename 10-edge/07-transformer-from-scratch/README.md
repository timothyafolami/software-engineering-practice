# Layer 10 · Topic 7 — A transformer from scratch, and the economics of training it

### The takeaway (read this first)

**The one idea:** you do not understand attention until you have written
the backward pass and had the loss break for a numerical reason, and you
do not understand distributed training until you have priced the
communication. Each parallelism strategy is a different answer to one
question — *what do we ship over the wire, and how often.* Data parallel
ships gradients once per step; tensor parallel ships activations many
times per step, which is why it stays inside NVLink; pipeline parallel
ships almost nothing and pays a bubble instead.

**Why it matters in practice:** it turns "we need more GPUs" from a budget
request into an arithmetic problem, and it makes you legible to the people
who do this full time.

**You'll know it landed when:** you can compute FLOPs, memory and
communication volume on paper and predict measured tokens/sec within a
factor of two *before* launching the run.

## The concept

### The three formulas that price a training run

**Training FLOPs ≈ 6ND** for N parameters and D tokens. The 6 is
derivable, not memorised: a forward pass does one multiply and one add per
parameter per token (2), and the backward pass computes gradients with
respect to both inputs and weights, costing about twice the forward (4).
2 + 4 = 6. Inference is 2ND for the same reason with no backward pass —
and confusing the two is the most common way an MFU number comes out
impossible.

**MFU** = achieved FLOP/s ÷ peak FLOP/s. On tuned large runs 40-50% is
generally considered good; single-digit MFU on a laptop is normal and
informative rather than a failure. Use the *dense* peak for your dtype,
per topic 1's warning about sparsity asterisks.

**Training memory** = parameters + gradients + optimizer state. Adam with
mixed precision costs about **16 bytes per parameter**, and again it is
derivable:

```
bf16 parameters            2
bf16 gradients             2
fp32 master weights        4
fp32 Adam first moment     4
fp32 Adam second moment    4
                          --
                          16 bytes / parameter,  before activations
```

That single line explains the shape of the entire field. A 1B-parameter
model needs ~16 GB for state alone and therefore will not train on a
16 GB laptop; a 25M-parameter model needs ~400 MB and trains comfortably.
Everything from activation checkpointing to ZeRO/FSDP sharding exists to
attack one term of that sum, and knowing which term tells you which
technique is relevant to your problem.

### GPU basics, only as deep as this needs

Occupancy is resident warps per streaming multiprocessor, bounded by
registers and shared memory per block. Its *purpose* is latency hiding:
more resident warps means more work to switch to while one waits on
memory. Which loops straight back to topic 1 — **if you are
bandwidth-bound, more occupancy does not help**, because there is no idle
bandwidth for the extra warps to use. Knowing that saves a week spent
tuning the wrong knob.

### Communication cost, per strategy

- **Data parallel / FSDP2.** An all-reduce of gradient bytes once per
  step (or all-gather + reduce-scatter when sharded). Volume is
  `O(params)` and independent of batch size, so it amortises with larger
  batches — which is exactly why DP tolerates slower interconnects and
  why it is the strategy that crosses nodes.
- **Tensor parallel.** Two all-reduces per transformer block in the
  forward pass and again in the backward, on *activations* sized
  `batch × seq × hidden`. That is many times per step and it grows with
  batch, so it needs NVLink-class bandwidth and stays inside one node.
- **Pipeline parallel.** Point-to-point activation sends at stage
  boundaries only — cheap bytes — but it pays a bubble:

```
bubble fraction ≈ (stages − 1) / (microbatches + stages − 1)

4 stages, 4 microbatches  → 3/7  ≈ 43% idle
4 stages, 32 microbatches → 3/35 ≈  9% idle
```

Interleaved schedules shrink the bubble; nothing removes it.

The standard 2D/3D layout falls straight out of those three facts: TP
within a node, FSDP across nodes, PP when the model fits neither.

**2026 tooling.** `torchtitan` is the PyTorch-native reference that
composes FSDP2 + TP + PP as orthogonal layers, and it is the right thing
to read for this material. DeepSpeed is still maintained, but the centre
of gravity moved to torch-native.

### On the from-scratch build

The roadmap points at Karpathy's zero-to-hero, still the right pedagogy.
The current reference to read *afterwards* is `karpathy/nanochat`, which
covers tokenizer → pretrain → SFT → eval → inference → chat UI in a few
thousand readable lines, and documents a speedrun cost and duration on
8×H100 in its own README — read that figure there rather than from this
page, since it moves with the code. Read it **after** you write yours;
reading first turns a reproduction into a transcription.

### Renting, sanely

You need hours, not weeks. Budget on the order of $50 across topics 7 and
8, and checkpoint from the first script you write, because the
interruptible tier is interruptible.

Two tiers exist: interruptible (RunPod Community, Vast.ai) and
"still there in an hour" (Lambda, RunPod Secure), with hyperscalers
roughly double the latter. **Prices move monthly and are not reproduced
here** — a price in a README is stale within a quarter, and this lab's
rule is that a number is either derived on the page or carries a source.
Take the current per-hour rate from the provider's own pricing page at
the moment you rent, record it in the table below next to the run it paid
for, and compute $/1M tokens yourself. That derived figure, not the
hourly rate, is the number worth keeping.

## How each language actually gets there

**Python, and only Python** — MLX on the Mac, PyTorch on rented GPUs.
This is the one topic where the lab's six-language habit would actively
mislead: the ecosystem is one language deep, and every other binding is a
wrapper over the same C++/CUDA/Metal kernels, so a Go or Java version
would demonstrate FFI rather than transformers.

The honest cross-language note is a negative one, and it is worth stating
so the omission does not read as laziness: if you find yourself wanting
to write the training loop in a faster language because Python feels
slow, topic 1's measurement is the answer. At training scale you are
inside kernels for essentially all of the wall clock, and the interpreter
overhead you would be optimising away is not where the time is. Measure
before you rewrite — which is the same lesson as Layer 1's, arriving from
the other direction.

## The experiment

1. **Write it.** A decoder-only transformer in MLX with no framework
   attention: your own RoPE, multi-head attention using the LSE-stable
   softmax from [topic 4](../04-quantization-and-determinism/README.md),
   your own AdamW, your own training loop. Roughly 10-25M parameters, a
   TinyStories-scale corpus, trained to a target loss. Implement the
   *naive* softmax first, on purpose, and watch where it breaks.
2. **Measure MFU.** Expected FLOPs via 6ND, measured wall time, achieved
   FLOP/s ÷ dense peak. Then make exactly *one* change you expect to help
   — fuse QKV into one projection, or double the batch — and re-measure.
   If MFU barely moves, check tokens/sec against topic 1's bandwidth
   bound: you are probably bandwidth-bound, and that is the lesson rather
   than a disappointment.
3. **Rent one GPU for an hour.** The same model on an A100 or a 4090.
   Predict the speedup twice — once from the *bandwidth* ratio, once from
   the *FLOPs* ratio — and see which is closer. That single comparison
   tells you which regime your training loop is in, and it is the same
   experiment as topic 1 with the arrow pointing at training.
4. **Two GPUs.** DDP or FSDP2. Predict scaling efficiency from the
   communication formula above *before* running, then measure. Predict
   again for a batch size 4x larger and check that the amortisation
   argument holds.

## How to run

```
pip install mlx

# the model, checked before it is trusted: shapes, causality, RoPE's
# relative-position property, and the two softmaxes side by side
python3 python/model.py

# a real training run, offline, on a seeded synthetic corpus
python3 python/train.py

# topic 4's finding, arriving inside your own model. Same seed, same data,
# same initial weights; one word different.
python3 python/train.py --softmax naive --attn-dtype float16 --logit-scale 6
python3 python/train.py --softmax lse   --attn-dtype float16 --logit-scale 6

# a longer run against a real corpus
python3 python/train.py --layers 8 --d-model 512 --seq 512 --batch 16 \
    --steps 2000 --data tinystories.txt

# then the arithmetic, from the numbers train.py printed
python3 python/mfu.py --params <N> --tokens <T> --seconds <wall> \
    --peak-tflops <DENSE peak for your part and dtype> \
    --bandwidth-gbs <from topic 1's stream.py> --tokens-per-step <batch x seq>
```

Three notes on those commands, each of which is the difference between a
measurement and a number.

`--attn-dtype`, not `--dtype`: parameters and the optimizer stay float32 in
every run here, because that is what mixed precision does in practice, and
because a loop with float16 optimizer state fails for a completely
different reason — AdamW's `eps` of 1e-8 is below float16's smallest normal
— which would have made the softmax comparison meaningless. Only the
attention scores and the softmax move between the two runs.

`--logit-scale` is a knob, labelled as a knob. Attention scores grow over a
long run and with larger `d_model`; scaling them lets the overflow happen
in a two-minute run instead of a two-day one. It forces the condition; it
does not manufacture the result. The `lse` run at the same scale trains
happily with scores well past the ceiling that kills the naive one, and
that contrast is the finding.

`--peak-tflops` wants the **dense** figure for the dtype you actually ran.
NVIDIA's headline BF16 numbers carry an asterisk meaning 2:4 structural
sparsity, and dense is half. `python3 python/mfu.py` with no arguments
prints reference figures and their ridge points to check a datasheet
against.

Then the rented box (Linux, CUDA), for experiments 3 and 4. **No PyTorch
port is shipped here on purpose** — `python/model.py` is two hundred lines
with no framework attention in it, and translating it is the exercise that
proves you understood it. Ship a port that somebody else wrote and you have
a working training loop and no new knowledge.

What transfers unchanged is `python/mfu.py`, which is arithmetic and does
not care which framework produced the wall time:

```
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv -l 1
python3 python/mfu.py --params <N> --tokens <T> --seconds <wall> \
    --peak-tflops <dense peak for the rented part> \
    --dollars-per-hour <the rate the provider charged you at that moment>
```

Predict the single-GPU speedup twice before you measure it — once from the
*bandwidth* ratio against your Mac, once from the *FLOPs* ratio — and see
which is closer. That one comparison tells you which regime your training
loop is in, and `mfu.py --bandwidth-gbs ... --peak-tflops ...` prints the
same verdict from the other direction.

Nothing in this topic runs in Docker on the Mac: Docker Desktop has no
Metal passthrough, so a containerised training loop here would run on the
CPU. The rented box is a Linux host and the natural place for containers;
put your environment in one there so the run is reproducible after you
destroy the instance.

## Predict, then record

- My model has ___ parameters; at ___ tokens the run is ___ FLOPs.
- On my part at ___ TFLOP/s dense peak that is ___ minutes at 100% MFU,
  so at my predicted ___% MFU it is ___.
- Optimizer state will be ___ GB by the 16-bytes/param arithmetic;
  measured peak memory will be ___.
- Rented speedup will be ___x by bandwidth ratio, ___x by FLOPs ratio —
  I predict ___.
- 2-GPU scaling efficiency will be ___%, and at 4x batch ___%.

| Setup | tokens/s | MFU % | peak mem | $/hr (from provider, dated) | $ per 1M tokens |
|---|---|---|---|---|---|
| M1, batch B | | | | 0 | |
| M1, batch 2B | | | | 0 | |
| rented, 1 GPU | | | | | |
| rented, 2 GPU DDP | | | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **MFU above 100%.** Your FLOP count is wrong — most likely 2ND
  (inference) instead of 6ND, or you dropped the attention term, which
  matters at long sequence length. It can also mean you used a peak
  figure that included sparsity.
- **2-GPU speedup of exactly 2.0x.** Verify gradients are actually being
  synchronised. Two independent copies scale perfectly and learn nothing
  extra. Loss curves and gradient norms should match across ranks step
  for step; if they diverge, you have two models, not one.
- **NaN loss in the first hundred steps.** Not necessarily a maths bug —
  check learning-rate warmup, and check whether you skipped the softmax
  max-subtraction. Both are the intended lesson, and one of them is
  deliberately built into step 1.
- **Rented GPU slower than your laptop.** Almost always data loading, not
  compute: the corpus is coming from network storage once per batch. Time
  a forward/backward on synthetic tensors held in GPU memory to separate
  the two.
- **Loss decreasing beautifully with a tiny dataset.** Check you are not
  training on the eval split, and check tokens-seen against dataset size
  — at 25M parameters it is easy to memorise a small corpus and call it
  learning.

## Answer before moving on

1. Derive 6ND from scratch for a model whose parameters are all in dense
   matmuls, then say what the derivation ignores and at what sequence
   length that omission stops being ignorable.
2. Tensor parallel ships activations sized `batch × seq × hidden` several
   times per step; data parallel ships `O(params)` once. Find the batch
   size at which TP's per-step volume exceeds DP's for a model you pick,
   and explain why that crossover is not the reason TP stays in a node.
3. You have 4 pipeline stages and a 43% bubble. Give three ways to shrink
   it, ranked by what each costs you elsewhere.
4. Your MFU is 8% on the laptop and 38% on a rented A100 for the same
   code. Give the two most likely explanations and the one measurement
   that distinguishes them.

## Next up

[Topic 8 — Interpretability: activation patching, transcoders, attribution
graphs](../08-interpretability-and-attribution/README.md). You have now
built the thing. The last topic is the discipline of making a *causal*
claim about what is going on inside it.
