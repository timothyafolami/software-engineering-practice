"""
Layer 10 - Topic 7: the training loop, and where naive softmax breaks.

What this demonstrates
    A complete training run against the transformer in model.py -- own
    RoPE, own attention, own AdamW, own loop -- with the softmax
    implementation and the compute dtype as command-line switches, so the
    numerical failure from topic 4 can be produced and then fixed without
    touching any other variable.

        --softmax naive --attn-dtype float16 --logit-scale 6   loss -> nan
        --softmax lse   --attn-dtype float16 --logit-scale 6   trains fine

    Everything else is identical: same seed, same data, same batches, same
    learning rate, same initial weights.

What to look for
    - The step at which the naive run produces its first nan, and the
      `max|score|` column next to it. exp() overflows float16 above about
      11.09, and the run dies once the scores cross it. This is not a
      mysterious instability; it is one number passing another number.
    - Parameters and the optimizer stay in float32 in every run here, which
      is what mixed precision does in practice. Only the attention scores
      and the softmax move, so the softmax really is the only variable. A
      loop with float16 optimizer state fails for an entirely different
      reason -- AdamW's eps of 1e-8 is below float16's smallest normal --
      and would have made this comparison meaningless.
    - tokens/s and the MFU line at the end. Feed them to mfu.py, and read
      that output against topic 1's bandwidth measurement before
      concluding you are compute-bound.
    - The loss floor. Byte-level on a small corpus will not go far, and
      that is fine: the deliverable here is a working loop you wrote, not
      a model. Point --data at a real corpus when you want a model.

Data
    Defaults to a deterministic synthetic corpus so the file runs offline
    with no download, which makes the naive-vs-lse comparison reproducible
    on any machine. Point --data at a text file (TinyStories, for example)
    for a real run.

Requires MLX. Runs with no arguments in a minute or two:
    python3 python/train.py
    python3 python/train.py --softmax naive --attn-dtype float16 --logit-scale 6
    python3 python/train.py --softmax lse   --attn-dtype float16 --logit-scale 6
    python3 python/train.py --d-model 512 --layers 8 --steps 2000 --data corpus.txt
"""

from __future__ import annotations

import argparse
import math
import pathlib
import random
import sys
import time

import mlx.core as mx
import mlx.nn as nn

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from model import (AdamW, Config, Transformer, cross_entropy_loss,  # noqa: E402
                   flops_per_token, param_count)

SEED = 20260818

_NOUNS = "cat dog bird fox mouse rabbit turtle owl fish bear".split()
_VERBS = "found lost carried watched chased buried opened shared hid dropped".split()
_PLACES = "garden river hill forest kitchen meadow harbour attic bridge field".split()
_ADJ = "small quiet golden broken warm strange bright empty heavy soft".split()


def synthetic_corpus(n_sentences: int = 40_000) -> str:
    """A tiny grammar, seeded. Not interesting text -- it exists so the loss
    curve has real structure to find and the run needs no network."""
    rng = random.Random(SEED)
    out = []
    for _ in range(n_sentences):
        out.append(
            f"The {rng.choice(_ADJ)} {rng.choice(_NOUNS)} {rng.choice(_VERBS)} "
            f"a {rng.choice(_ADJ)} thing near the {rng.choice(_PLACES)}. "
        )
    return "".join(out)


def load_data(path: pathlib.Path | None) -> mx.array:
    text = path.read_text() if path else synthetic_corpus()
    # Byte-level: vocabulary 256, no tokenizer to get wrong, and no
    # tokenizer-version drift between this run and the next one.
    return mx.array(list(text.encode("utf-8", errors="replace")), dtype=mx.uint32)


def batches(data: mx.array, batch: int, seq: int, steps: int):
    rng = random.Random(SEED)
    n = data.size - seq - 1
    for _ in range(steps):
        starts = [rng.randrange(n) for _ in range(batch)]
        x = mx.stack([data[s:s + seq] for s in starts])
        y = mx.stack([data[s + 1:s + seq + 1] for s in starts])
        yield x, y


def cosine_lr(step: int, total: int, peak: float, warmup: int) -> float:
    if step < warmup:
        return peak * (step + 1) / warmup
    t = (step - warmup) / max(1, total - warmup)
    return 0.1 * peak + 0.9 * peak * 0.5 * (1 + math.cos(math.pi * t))


def max_attention_score(model: Transformer, x: mx.array) -> float:
    """The number that decides whether naive softmax survives.

    Recomputed here rather than instrumented into the model, so the model
    stays readable and the diagnostic stays optional.
    """
    from model import apply_rope, rope_frequencies
    cfg = model.cfg
    h = model.embed(x)
    cos, sin = rope_frequencies(cfg.head_dim, x.shape[1], cfg.rope_theta, h.dtype)
    peak = 0.0
    for block in model.blocks:
        z = block.norm1(h)
        b, t, _ = z.shape
        q = block.attn.wq(z).reshape(b, t, cfg.n_heads, cfg.head_dim).transpose(0, 2, 1, 3)
        k = block.attn.wk(z).reshape(b, t, cfg.n_heads, cfg.head_dim).transpose(0, 2, 1, 3)
        q, k = apply_rope(q, cos, sin), apply_rope(k, cos, sin)
        scores = (q @ k.transpose(0, 1, 3, 2)) / math.sqrt(cfg.head_dim)
        scores = scores * cfg.logit_scale
        mx.eval(scores)
        peak = max(peak, float(mx.max(mx.abs(scores))))
        h = block(h, cos, sin, mx.triu(mx.full((t, t), -1e9, dtype=h.dtype), k=1))
    return peak


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--softmax", choices=("lse", "naive"), default="lse")
    ap.add_argument("--attn-dtype", choices=("float32", "float16", "bfloat16"),
                    default="float32",
                    help="dtype the attention scores and softmax are computed "
                         "in; parameters and the optimizer stay float32, as "
                         "mixed precision does in practice")
    ap.add_argument("--logit-scale", type=float, default=1.0,
                    help="multiplies pre-softmax scores; stands in for the "
                         "score growth that happens deep in a long run")
    ap.add_argument("--layers", type=int, default=6)
    ap.add_argument("--heads", type=int, default=8)
    ap.add_argument("--d-model", type=int, default=384)
    ap.add_argument("--seq", type=int, default=256)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--data", type=pathlib.Path, default=None)
    args = ap.parse_args()

    mx.random.seed(SEED)
    cfg = Config(n_layers=args.layers, n_heads=args.heads, d_model=args.d_model,
                 max_seq=args.seq, softmax=args.softmax,
                 attn_dtype=args.attn_dtype, logit_scale=args.logit_scale)
    model = Transformer(cfg)
    mx.eval(model.parameters())

    n_params = param_count(model)
    data = load_data(args.data)
    opt = AdamW(lr=args.lr, weight_decay=0.1)
    loss_and_grad = nn.value_and_grad(model, cross_entropy_loss)

    print("Training a decoder-only transformer, written out")
    print(f"  parameters   : {n_params:,}")
    print(f"  layers/heads : {cfg.n_layers} / {cfg.n_heads}   d_model {cfg.d_model}"
          f"   d_ff {cfg.d_ff}")
    print(f"  seq / batch  : {args.seq} / {args.batch}   "
          f"({args.seq * args.batch:,} tokens per step)")
    print(f"  softmax      : {args.softmax}     attention compute dtype: "
          f"{args.attn_dtype}   logit scale: {args.logit_scale}")
    if args.attn_dtype == "float16":
        print(f"  float16 exp() ceiling: scores above 11.09 overflow "
              f"(exp(11.09) = 65500, float16 max = 65504)")
    print(f"  corpus       : "
          f"{args.data if args.data else 'synthetic (seeded, offline)'} "
          f"-- {data.size:,} bytes")
    print(f"  6ND FLOPs/token: {flops_per_token(n_params):,}")
    print()
    print(f"  {'step':>6} {'loss':>10} {'lr':>10} {'max|score|':>12} "
          f"{'tok/s':>10} {'note':>22}")
    print("  " + "-" * 78)

    first_nan = None
    start = time.perf_counter()
    tokens = 0
    last_report = start

    for step, (x, y) in enumerate(batches(data, args.batch, args.seq, args.steps)):
        lr = cosine_lr(step, args.steps, args.lr, warmup=max(5, args.steps // 20))
        loss, grads = loss_and_grad(model, x, y)
        model.update(opt.update(model.parameters(), grads, lr))
        mx.eval(model.parameters(), loss)
        tokens += x.size

        loss_v = float(loss)
        if first_nan is None and not math.isfinite(loss_v):
            first_nan = step

        if step % max(1, args.steps // 15) == 0 or step == args.steps - 1:
            now = time.perf_counter()
            peak = max_attention_score(model, x[:1])
            note = ""
            if not math.isfinite(loss_v):
                note = "nan -- run is dead"
            elif args.attn_dtype == "float16" and peak > 11.09:
                note = "scores past exp() range"
            print(f"  {step:>6} {loss_v:>10.4f} {lr:>10.2e} {peak:>12.2f} "
                  f"{tokens / (now - start):>10.0f} {note:>22}")
            last_report = now

    elapsed = time.perf_counter() - start
    achieved_flops = flops_per_token(n_params) * tokens / elapsed

    print()
    print(f"  wall time        : {elapsed:.1f}s for {tokens:,} tokens "
          f"({tokens / elapsed:.0f} tok/s)")
    print(f"  achieved FLOP/s  : {achieved_flops / 1e9:.1f} GFLOP/s "
          f"(6ND estimate)")
    if first_nan is not None:
        print(f"\n  The loss became nan at step {first_nan}.")
        print("  exp() overflows float16 above about 11.09, because exp(11.09) is")
        print("  65500 and the format stops at 65504. Watch the max|score| column")
        print("  cross it. Re-run the identical command with --softmax lse: same")
        print("  seed, same data, same weights, one subtraction different.")
        print()
        print("  The max|score| column stops meaning anything after the run dies:")
        print("  the weights it is computed from are no longer finite. Read the lse")
        print("  run's column instead -- it keeps training at scores well past the")
        print("  ceiling that killed this one, which is the whole demonstration.")
    else:
        print("\n  Next: feed the wall time and token count to mfu.py, and read the")
        print("  result against topic 1's measured bandwidth before concluding")
        print("  anything about being compute-bound:")
        print(f"    python3 python/mfu.py --params {n_params} --tokens {tokens} "
              f"--seconds {elapsed:.1f}")


if __name__ == "__main__":
    main()
