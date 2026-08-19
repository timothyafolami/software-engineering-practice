# Layer 10 · Topic 8 — Interpretability: activation patching, transcoders, attribution graphs

### The takeaway (read this first)

**The one idea:** interpretability has a causal standard. "Head 9.6 does
X" is not a claim about correlations in activations — it is a prediction
that *intervening* on that head changes the output in a stated direction.
Activation patching is the cheapest form of that experiment, and it runs
on your laptop.

**Why it matters in practice:** the roadmap's read — that this field still
rewards careful experimentalists more than credentialed ones — is
accurate. The barrier is experimental discipline: minimal pairs, a metric
fixed before you look, and an ablation that could falsify you. Exactly the
discipline this whole lab has been drilling, applied to a subject where
almost nobody applies it.

**You'll know it landed when:** you can take a behaviour, construct a
minimal clean/corrupted pair, choose a metric, produce a layer × position
patching heatmap, form a hypothesis, then confirm or falsify it *on a new
prompt set your hypothesis makes a prediction about* — and write it up so
that someone else can check you.

## The concept

### The residual stream as a channel

Each transformer block reads from and writes into a shared residual
stream; attention moves information *between* positions, MLPs process it
*in place*. The stream is additive, and that linearity is the only reason
attribution is tractable at all — a component's contribution is a term in
a sum, so removing or replacing it is a well-defined operation rather than
a metaphor.

### Activation patching, precisely

Run a *clean* prompt and a minimally different *corrupted* one. Splice one
component's clean activation into the corrupted run, and measure how much
of the clean logit difference is recovered:

```
recovery = ( metric(patched) − metric(corrupted) )
           / ( metric(clean)  − metric(corrupted) )
```

with `metric` fixed in advance — usually the logit difference between the
correct and the counterfactual token, *not* the probability, because
logit differences are additive in the residual stream and probabilities
are not.

Sweep components in order of cost: residual stream by layer × position
first (cheap, coarse), then attention heads by layer × head, then MLPs.
**Attribution patching** approximates the whole sweep with a first-order
gradient term, so it scales to many components at once at some fidelity
cost — use it to find candidates, then confirm the candidates with real
patching.

Two directions of the same experiment, worth keeping straight:

- **Denoising** (clean → corrupted, above) asks *which components are
  sufficient* to restore the behaviour.
- **Noising** (corrupted activation into the clean run) asks *which are
  necessary*. They disagree more often than people expect, and the
  disagreement is informative rather than a bug.

### SAEs and where they stand in 2026

This is the part most likely to be stale in your head. Sparse
autoencoders were the main bet on finding interpretable features in
superposition. They still matter, but the critique landed:
reconstruction-optimised dictionaries optimise for *compressing
activations*, not for *modelling a component's function*, with measured
problems including feature absorption and weak sensitivity.

Two directions took over for circuit work: **transcoders**, which
approximate an MLP's input→output function so that features compose
across layers (see Dunefsky, Chlenski and Nanda's work on transcoders for
feature circuits — search for it by title rather than trusting an
identifier printed here), and **cross-layer transcoders**, which current
attribution-graph work is built on. Matryoshka SAEs address the
multi-scale problem. Treat any 2023-24 "SAEs are the answer" summary as
out of date.

### Attribution graphs

Anthropic open-sourced `circuit-tracer` in 2025, with Neuronpedia hosting
interactive graphs; supported models include Gemma-2-2B, Llama-3.2-1B and
Qwen3-4B, and thousands of graphs exist publicly.

State the caveat in anything you publish, because the pictures are more
persuasive than the evidence: a graph for even a short factual prompt
routinely holds hundreds to thousands of cross-layer features, transcoder
interference is a real and unsolved problem, and reading graphs
automatically (auto-interp agents) is a frontier rather than a solved
step. A graph is a hypothesis generator. The causal test is still the
patch.

## How each language actually gets there

**Python only, and this time the reason is not ecosystem gravity but
access:** the experiment *is* reaching into a model's forward pass and
replacing a tensor mid-flight, and the hook APIs that permit that exist in
PyTorch. There is no version of this experiment in another language that
is not a binding to the same PyTorch internals.

Use plain `register_forward_hook` on a HuggingFace model for the first
implementation — the patching harness is about forty lines and teaches
more than `transformer_lens` does, because writing it forces you to be
explicit about *where* in the block you are intervening (pre-attention,
post-attention, post-MLP), which is the distinction the whole method
rests on. Move to `transformer_lens` afterwards for the sweeps, and to
`circuit-tracer` for attribution graphs.

## The experiment

Pick one narrow behaviour with a genuinely minimal pair. Indirect object
identification is canonical; "capital of X" and subject-verb agreement
also work. On GPT-2 small or Llama-3.2-1B, both of which run on the M1:

1. **Fix the metric first, in writing:** logit difference between the
   correct and counterfactual token. Record clean and corrupted baselines
   before touching anything, and write the expected direction down.
2. **Patch the residual stream** over layer × position and produce the
   heatmap. This is the cheap sweep and it tells you *where* and *when*
   the information becomes decisive.
3. **Narrow to components:** attention heads by layer × head, then MLPs
   at the layers the residual sweep implicated.
4. **State a hypothesis** that names specific components and says what
   they do, in a sentence that could be wrong.
5. **Try to falsify it.** Mean-ablate the named heads and check the
   behaviour degrades as predicted — mean-ablation rather than
   zero-ablation, because zeroing takes the activation off-distribution
   and breaks things for reasons unrelated to your claim. Then, the step
   most write-ups skip: construct a *new* prompt set your hypothesis
   makes a prediction about, and test there. A hypothesis that only
   explains the prompts it was derived from has explained nothing.
6. **Cross-check with attribution graphs.** Run `circuit-tracer` on the
   same prompt — Gemma-2-2B is comfortable on a rented GPU and slow
   locally — and see whether it agrees. The disagreements are the
   interesting part and belong in the write-up.
7. **Write it up publicly.** About 1500 words: setup, metric, heatmap,
   hypothesis, falsification attempt, and what you could not resolve. The
   roadmap asks for one design doc and one technical post a month; this is
   a post, and the honest unresolved section is what makes people trust
   the rest of it.

## How to run

```
pip install torch transformers
```

Everything below is GPT-2 small on the host with MPS, and each step takes
about a minute. Run them in this order — each one checks something the next
one assumes.

```
# 1. the metric, fixed in writing before anything is patched.
#    Prints the clean and corrupted baselines. If they do not bracket zero,
#    stop: the model does not do this task and every heatmap below is noise.
python3 python/ioi.py

# 2. the hook machinery, checked at both ends of its range before it is
#    trusted: patching everything must reproduce the clean run exactly, and
#    patching nothing must reproduce the corrupted run exactly.
python3 python/patching.py

# 3. the cheap sweep: WHERE and WHEN the answer becomes determined
python3 python/residual_sweep.py

# 4. narrow to components: WHICH HEAD put it there
python3 python/head_sweep.py

# 5. the falsification step, which is the actual experiment
python3 python/ablate_and_falsify.py --heads <from step 4>
```

`python/patching.py` writes the hooks out rather than importing
`transformer_lens`. Use the library afterwards — it is better than this and
you should — but writing the hooks once is what turns "run the sweep" into
"know what the sweep did". The two hook points are the residual stream
(a forward hook on a block) and per-head outputs (a forward **pre**-hook on
`attn.c_proj`, whose input is the last place in a HuggingFace GPT-2 where
heads are still separable; `c_proj` sums them).

`python/ablate_and_falsify.py` is the file that matters, and it runs three
checks in the order that lets each of them fail: mean-ablate the named heads,
mean-ablate the same *number* of random heads as a control, and then repeat
both on a **holdout** set with different names and different templates that
was never used to derive the hypothesis. Mean-ablation rather than zero:
zeroing takes the activation off-distribution and breaks the model for
reasons unrelated to your claim.

The heatmaps are ASCII on purpose — no plotting dependency, and the numbers
stay next to the picture. Render them properly for the write-up; read them
in the terminal while you work.

Attribution graphs, for the cross-check in step 6:

```
pip install circuit-tracer
```

Follow that project's own quickstart for the model you are tracing —
Llama-3.2-1B is comfortable locally, Gemma-2-2B wants a rented GPU. No
wrapper script is shipped here because the interesting output is whether an
independently-built method names the same components your sweep did, and a
wrapper of mine would only add a place for the two to be made to agree.

All of this runs on the host with MPS. Docker Desktop on the Mac has no
Metal passthrough — see [`../lab/README.md`](../lab/README.md) — so a
containerised run would be CPU-only and slow enough to change what
experiments you are willing to do, which is the worst kind of slow.

## Predict, then record

Write these down before the first sweep, and be specific enough to be
wrong.

- Clean logit diff will be ___; corrupted ___.
- The critical layers will be ___ (early / middle / late), and the effect
  will localise to about ___ heads.
- Ablating the named heads will reduce the logit diff by ___%.
- On the held-out prompt set, the effect size will be ___% of what it was
  on the original set.
- The attribution graph will / will not agree, because ___.

| Component | patched logit diff | % of clean recovered | ablation effect |
|---|---|---|---|
| | | | |

| Prompt set | n | mean logit diff | effect after ablation |
|---|---|---|---|
| original | | | |
| held-out | | | |

**What would mean the experiment is broken rather than your prediction
wrong:**

- **Patching nearly anywhere recovers ~100%.** Your pair is not minimal —
  it differs in too many places, so any patch drags the whole computation
  across. Rebuild it to differ in one token where possible.
- **Nothing recovers anything anywhere.** Check that the corrupted
  baseline actually differs from clean (if the model gets both right,
  there is no signal to recover), and that your hook fires where you
  think it does — print tensor shapes and assert the patched tensor
  changed.
- **A single head explains 100%.** Real circuits are usually distributed;
  a perfect single-component result more often means a degenerate metric
  — dominated by one token's embedding, say — than a clean mechanism.
  Test on the held-out set before believing it.
- **Zero-ablation destroys everything, everywhere.** Expected, and not a
  finding: zeroing an activation takes it off-distribution. Use mean
  ablation over the corrupted distribution and re-run.
- **The attribution graph agrees perfectly with your patching result.**
  Check they are not both reading the same cached artifact, and check the
  graph was built for the same tokenisation of the same prompt. Perfect
  agreement between two methods with different failure modes deserves the
  same suspicion as a perfect benchmark number.

## Answer before moving on

1. Denoising and noising ask different questions (sufficiency versus
   necessity). Construct a component that would score high on one and low
   on the other, and say what that pattern would mean mechanically.
2. Why is logit difference the right metric and probability the wrong
   one? Give the answer in terms of what the residual stream is doing.
3. Attribution patching approximates patching with a first-order term.
   Name the regime where that approximation fails badly, and how you
   would detect that it had.
4. You have a clean result on 20 prompts and it holds on a held-out set
   of 20 more. Someone asks whether you have found a *circuit* or a
   *dataset artifact*. What experiment settles it?

## Next up

Nothing — this is the last topic of the last layer. See the layer
[`README.md`](../README.md) for the cadence that replaces "next topic"
from here: one paper read properly each week, one reproduced each
quarter, one design doc and one technical post published each month.
