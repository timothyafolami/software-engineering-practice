"""
Layer 10 - Topic 8, step 3: narrow to components -- attention heads.

What this demonstrates
    The residual sweep says WHEN the answer becomes determined. This says
    WHICH HEAD put it there. For every (layer, head), the corrupted run is
    re-run with that single head's output replaced by the clean run's, and
    the metric is re-measured.

    The hook point is the input to each block's attn.c_proj, because that
    is the last place in a HuggingFace GPT-2 where heads are still
    separable -- c_proj sums them. Patching after c_proj can only ever
    patch a whole layer.

What to look for
    - A small number of heads carrying most of the effect. If the map is
      uniformly warm, either the metric is not selective or the behaviour
      is genuinely distributed, and those are different findings that
      deserve different write-ups.
    - Heads with NEGATIVE restore fractions. Those are pushing the other
      way, and they are not noise: on IOI specifically there are heads
      that suppress the correct answer, and a hypothesis that mentions
      only the positive ones is incomplete.
    - The ranked list at the bottom is the input to ablate_and_falsify.py.
      Naming heads from this list is a HYPOTHESIS, not a result -- the
      result is whether ablating them does what you predicted.

Requires torch and transformers. Runs with no arguments in about a minute
on the M1:
    python3 python/head_sweep.py
"""

from __future__ import annotations

import pathlib
import sys
import time

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ioi import baselines, build_dataset, load_model, logit_diff, normalised
from patching import BLOCKS, ascii_heatmap, capturing_heads, patch_head


@torch.no_grad()
def main() -> None:
    model, tokenizer, device = load_model()
    examples = build_dataset(tokenizer)
    clean, corrupted, clean_ids, corrupt_ids = baselines(
        model, tokenizer, examples, device)

    n_layers = len(BLOCKS(model))
    n_heads = model.config.n_head
    d_head = model.config.n_embd // n_heads

    clean_heads: dict = {}
    with capturing_heads(model, clean_heads):
        model(clean_ids)

    print("Attention-head activation patching, layer x head")
    print(f"  model     : gpt2 on {device}, {n_layers} layers x {n_heads} heads, "
          f"d_head {d_head}")
    print(f"  clean     : {clean:+.3f}     corrupted: {corrupted:+.3f}     "
          f"span: {clean - corrupted:.3f}")
    print(f"  hook point: input to attn.c_proj -- the last place heads are "
          f"separable\n")

    start = time.perf_counter()
    grid = []
    for layer in range(n_layers):
        row = []
        for head in range(n_heads):
            with patch_head(model, layer, head, clean_heads[layer], d_head):
                patched = logit_diff(model(corrupt_ids).logits, examples).mean().item()
            row.append(normalised(patched, clean, corrupted))
        grid.append(row)
    elapsed = time.perf_counter() - start

    print(ascii_heatmap(grid, [f"L{i}" for i in range(n_layers)],
                        [str(h) for h in range(n_heads)]))
    print("  (magnitude only -- signs are in the ranked lists below)")

    flat = [(v, l, h) for l, row in enumerate(grid) for h, v in enumerate(row)]
    positive = sorted(flat, reverse=True)[:8]
    negative = sorted(flat)[:5]

    print(f"\n  heads that RESTORE the clean behaviour:")
    print(f"    {'head':>8} {'restore':>9}")
    for v, l, h in positive:
        print(f"    {f'{l}.{h}':>8} {v:>9.3f}")

    print(f"\n  heads that push the OTHER way:")
    print(f"    {'head':>8} {'restore':>9}")
    for v, l, h in negative:
        print(f"    {f'{l}.{h}':>8} {v:>9.3f}")

    top = ",".join(f"{l}.{h}" for _, l, h in positive[:3])
    print(f"\n  {elapsed:.1f}s for {n_layers * n_heads} patched forward passes")
    print()
    print("  This list is a HYPOTHESIS, not a result. Write it as a sentence")
    print("  that could be wrong -- 'heads " + top + " carry the indirect")
    print("  object's identity to the final position' -- and then try to")
    print("  falsify it:")
    print(f"\n    python3 python/ablate_and_falsify.py --heads {top}")


if __name__ == "__main__":
    main()
