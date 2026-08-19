# Layer 10 · Your actual edge — ML systems + math

The roadmap's framing, kept because it is the point: *layers 1 to 9 make
you a strong engineer, and also make you comparable to other strong
engineers. This is the part that does not.* The mistake would be closing
the fundamentals gap by becoming a more generic backend engineer. Close
it, then point the whole stack at the intersection.

| # | Topic | Folder |
|---|---|---|
| 1 | Prefill, decode, and being memory-bandwidth-bound | [`01-prefill-decode-and-bandwidth/`](01-prefill-decode-and-bandwidth/README.md) |
| 2 | Continuous batching, paged KV, and prefix caching under load | [`02-continuous-batching-and-paged-kv/`](02-continuous-batching-and-paged-kv/README.md) |
| 3 | Little's Law, Kingman, and why independent p99s compound | [`03-littles-law-and-tail-compounding/`](03-littles-law-and-tail-compounding/README.md) |
| 4 | Quantization, numerical stability, and the determinism you didn't have | [`04-quantization-and-determinism/`](04-quantization-and-determinism/README.md) |
| 5 | Data pipelines, versioning, and drift | [`05-pipelines-versioning-and-drift/`](05-pipelines-versioning-and-drift/README.md) |
| 6 | Evaluation design, and shadow deployment for models | [`06-evaluation-and-shadow-deployment/`](06-evaluation-and-shadow-deployment/README.md) |
| 7 | A transformer from scratch, and the economics of training it | [`07-transformer-from-scratch/`](07-transformer-from-scratch/README.md) |
| 8 | Interpretability: activation patching, transcoders, attribution graphs | [`08-interpretability-and-attribution/`](08-interpretability-and-attribution/README.md) |

[`SEQUENCE.md`](../SEQUENCE.md) runs this layer in parallel from week 1
rather than after Layer 9: 1 and 4 stand alone, 3 and 2 ride along with
Layer 5's queueing work, 7 and 8 each need a contiguous block.

## The shared lab

Topics 2, 3, 5 and 6 run against one compose stack: [`lab/`](lab/README.md)
— service names (`gateway`, `api`, `db`, `k6`, `prom`, `grafana`), ports,
k6 script paths, env vars and version pins, in one place. Two rules from
that file invalidate results silently when broken. **Docker Desktop on
macOS has no Metal passthrough**, so a container on this M1 cannot use the
GPU: compose runs the services, while the model server, MLX and PyTorch
run on the **host**, reached at `host.docker.internal`. And **load is
open-model** — k6's `constant-arrival-rate`, never a fixed VU count,
because a closed-loop generator self-throttles and cannot reproduce
queueing at all.

## The "you own this when" test — invented here, not from the roadmap

Layer 10 is one of two layers in the roadmap with **no "you own this when"
block** — Layer 8 is the other, and [`08-craft/`](../08-craft/README.md)
flags its invented one the same way. This is the test, and it is this
lab's, not the roadmap's:

> You can look at a serving stack and say, before measuring, which
> resource limits it. You can price a capacity decision in one line of
> arithmetic instead of a load test. And you can tell the difference
> between an eval result and a measurement.

## The language set

**Six: Python, Node.js, Go, Rust, C++, Java** — the lab-wide set, for the
reasons in the root [`README.md`](../README.md); each topic states its
reason in one line where it uses fewer. All six appear where the runtime
*is* the mechanism: topic 3 (a pool is a data structure inside a runtime,
and six runtimes disagree about it, down to Java's virtual threads moving
the queue rather than removing it), topic 2 (on client disconnect, is the
in-flight request cancelled or does it keep holding KV blocks?), and topic
4 (four of the six reproduce the batch-invariance finding on a CPU, no GPU
needed). Fewer where the mechanism lives outside the language: three in
topics 1 and 5, two in topic 6, Python only in 7 and 8.

## No fabricated results

Every topic ends with **Predict, then record**: a prediction written
*before* running, a blank table filled in *after*, and the outcomes that
would mean **the experiment is broken rather than your prediction wrong**.
The tables ship empty. Every number in the prose is either derived on the
page or carries a source — an uncited statistic is the same defect as a
fabricated table, only harder to spot. Predictions go in
[`PREDICTIONS.md`](../PREDICTIONS.md).

## Sources worth reading directly (not the listicles)

- [Inside vLLM](https://www.aleksagordic.com/blog/vllm) — scheduler, paged KV, prefix cache · [vLLM releases](https://github.com/vllm-project/vllm/releases), where version claims get checked rather than in blog posts
- [Defeating Nondeterminism in LLM Inference](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/) — the batch-invariance finding behind topic 4
- Agrawal et al., *Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve* ([arXiv 2403.02310](https://arxiv.org/abs/2403.02310)) — chunked prefill · Dean & Barroso, *The Tail at Scale*, CACM 2013 — fan-out tails
- [Anthropic: open-sourcing circuit tracing](https://www.anthropic.com/research/open-source-circuit-tracing) · [Neuronpedia graphs](https://www.neuronpedia.org/graph/info) · Dunefsky, Chlenski & Nanda on **transcoders for feature circuits**, found by title — two bare arXiv identifiers were cut from this file, because a bare number for a recent paper is the classic shape of a confabulated citation
- [karpathy/nanochat](https://github.com/karpathy/nanochat) (topic 7, read *after* writing your own) · [pytorch/torchtitan](https://github.com/pytorch/torchtitan) · [Docker Model Runner + vLLM on Metal](https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/)

**Claims here I could not verify against a primary source** — check before
repeating: the 2026 production share of NVFP4 versus FP8 (vendor blogs
only), and whether SGLang shipped a deterministic mode.

## Next up

Nothing — this is layer 10. What replaces "next topic" is a cadence: **one
paper read properly each week, one reproduced each quarter, one design doc
and one technical post published each month.** Topics 6, 7 and 8 each
produce a publishable artifact by design; what makes you rare is not
reading this, it is the public record that you ran it.
