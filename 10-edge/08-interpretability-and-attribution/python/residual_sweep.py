"""
Layer 10 - Topic 8, step 2: patch the residual stream over layer x position.

What this demonstrates
    The cheap sweep, and the one to run first. For every (layer, position),
    the corrupted run is re-run with that one hidden state replaced by the
    value the clean run had there, and the metric is re-measured. The
    result is a map of WHERE and WHEN the information that decides the
    answer becomes decisive.

    It deliberately says nothing about WHICH COMPONENT put the information
    there -- that is head_sweep.py, and running it first on all
    layers x heads costs more and tells you less.

What to look for
    - A bright region late in the sequence at the middle-to-late layers.
      That is the "the answer is now determined" boundary, and everything
      before it is the circuit assembling.
    - Bright cells at the NAME positions early on: the model reading the
      names, before it has decided anything with them.
    - Cells above 1.0. A patch can restore MORE than the clean run
      achieves, which means some other component was pushing the other way
      and you have just removed its input. Those cells are usually the
      most interesting thing on the map.

Reported as the normalised restore fraction: 0 = the patch changed
nothing, 1 = it fully restored the clean behaviour.

Requires torch and transformers. Runs with no arguments in about a minute
on the M1:
    python3 python/residual_sweep.py
"""

from __future__ import annotations

import pathlib
import sys
import time

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ioi import baselines, build_dataset, load_model, logit_diff, normalised
from patching import BLOCKS, ascii_heatmap, cache_residuals, patch_residual


@torch.no_grad()
def main() -> None:
    model, tokenizer, device = load_model()
    examples = build_dataset(tokenizer)
    clean, corrupted, clean_ids, corrupt_ids = baselines(
        model, tokenizer, examples, device)
    cache = cache_residuals(model, clean_ids)

    n_layers = len(BLOCKS(model))
    seq = clean_ids.shape[1]
    tokens = [tokenizer.decode([t]).strip() or "_"
              for t in clean_ids[0].tolist()]

    print("Residual-stream activation patching, layer x position")
    print(f"  model     : gpt2 on {device}, {n_layers} layers, {seq} positions")
    print(f"  examples  : {len(examples)}")
    print(f"  clean     : {clean:+.3f}     corrupted: {corrupted:+.3f}     "
          f"span: {clean - corrupted:.3f}")
    print(f"  runs      : {n_layers * seq} forward passes\n")

    start = time.perf_counter()
    grid = []
    for layer in range(n_layers):
        row = []
        for pos in range(seq):
            with patch_residual(model, layer, pos, cache[layer]):
                patched = logit_diff(model(corrupt_ids).logits, examples).mean().item()
            row.append(normalised(patched, clean, corrupted))
        grid.append(row)
    elapsed = time.perf_counter() - start

    print(ascii_heatmap(grid,
                        [f"L{i}" for i in range(n_layers)],
                        [t[:2] for t in tokens]))
    print(f"\n  columns are token positions: "
          + " ".join(f"{i}:{t}" for i, t in enumerate(tokens)))

    flat = sorted(((v, l, p) for l, row in enumerate(grid) for p, v in enumerate(row)),
                  reverse=True)
    print(f"\n  strongest cells (restore fraction, 1.0 = fully restored):")
    print(f"    {'layer':>6} {'pos':>4} {'token':>12} {'restore':>9}")
    for v, l, p in flat[:10]:
        print(f"    {l:>6} {p:>4} {tokens[p]:>12} {v:>9.3f}")

    print(f"\n  {elapsed:.1f}s for {n_layers * seq} patched forward passes")
    print()
    print("  Read this map before running head_sweep.py, and use it to choose")
    print("  which layers to sweep heads in. Sweeping all layers x heads first")
    print("  costs more and tells you less: this map is the one that says WHEN")
    print("  the answer becomes determined, which is the question the component")
    print("  sweep cannot answer.")


if __name__ == "__main__":
    main()
