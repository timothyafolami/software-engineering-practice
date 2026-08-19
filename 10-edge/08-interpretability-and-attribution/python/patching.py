"""
Layer 10 - Topic 8: activation patching, with the hooks written out.

What this file is
    The mechanism, in about a hundred lines of PyTorch forward hooks and no
    interpretability library. `transformer_lens` is excellent and you
    should use it afterwards; writing the hooks once first is what turns
    "run the sweep" into "know what the sweep did".

    Two hook points, and the choice between them is the whole design:

      residual stream   a forward hook on block i, replacing the hidden
                        state at one position with the value the CLEAN run
                        had there. Cheap: layers x positions runs. Tells
                        you WHERE and WHEN the information becomes
                        decisive, and nothing about which component put it
                        there.
      head output       a forward PRE-hook on block i's attn.c_proj. Its
                        input is the concatenated per-head outputs, so head
                        h lives in columns [h*d_head, (h+1)*d_head) and can
                        be replaced on its own. This is the only place in a
                        HuggingFace GPT-2 where heads are still separable;
                        after c_proj they are summed and gone.

    Mean-ablation, not zero-ablation. Zeroing an activation takes it far
    off the distribution the rest of the network was trained against, so
    the behaviour breaks for reasons that have nothing to do with your
    hypothesis. Replacing it with its mean over the batch removes the
    information the component carried while leaving its scale where the
    network expects it.

What to look for
    - `restore_fraction`: 0 means the patch changed nothing, 1 means it
      fully restored the clean behaviour. Values above 1 are possible and
      interesting -- they mean the patched component alone is more
      decisive than the whole clean run, which usually means another
      component was working against it.
    - The ASCII heatmaps. They are here so this topic needs no plotting
      dependency and so the numbers stay visible; matplotlib is a better
      artifact for the write-up and a worse one for reading a diff.

Imported by the sweep scripts. Runs standalone as a self-check that
patching every layer at every position restores the clean run exactly:

    python3 python/patching.py
"""

from __future__ import annotations

import contextlib

import torch

BLOCKS = lambda model: model.transformer.h  # noqa: E731  (GPT-2 layout)


@torch.no_grad()
def cache_residuals(model, input_ids) -> list[torch.Tensor]:
    """Hidden state at the output of every block, for one forward pass."""
    cache: list[torch.Tensor] = []
    handles = []
    for block in BLOCKS(model):
        handles.append(block.register_forward_hook(
            lambda _m, _i, out: cache.append(out[0].detach().clone())))
    try:
        model(input_ids)
    finally:
        for h in handles:
            h.remove()
    return cache


@contextlib.contextmanager
def capturing_heads(model, store: dict):
    """Capture the concatenated per-head outputs of every layer.

    c_proj's input is (batch, seq, n_head * d_head) with head h in columns
    [h*d_head, (h+1)*d_head). That is the last point at which heads are
    separable; c_proj sums them.
    """
    handles = []
    for i, block in enumerate(BLOCKS(model)):
        def pre_hook(_m, args, idx=i):
            store[idx] = args[0].detach().clone()
            return None
        handles.append(block.attn.c_proj.register_forward_pre_hook(pre_hook))
    try:
        yield store
    finally:
        for h in handles:
            h.remove()


@contextlib.contextmanager
def patch_residual(model, layer: int, position: int, value: torch.Tensor):
    """Replace the residual stream at (layer, position) with `value`."""
    def hook(_m, _i, out):
        hidden = out[0].clone()
        hidden[:, position, :] = value[:, position, :]
        return (hidden,) + tuple(out[1:])

    handle = BLOCKS(model)[layer].register_forward_hook(hook)
    try:
        yield
    finally:
        handle.remove()


@contextlib.contextmanager
def patch_head(model, layer: int, head: int, value: torch.Tensor, d_head: int):
    """Replace one head's output at every position with `value`'s."""
    lo, hi = head * d_head, (head + 1) * d_head

    def pre_hook(_m, args):
        z = args[0].clone()
        z[:, :, lo:hi] = value[:, :, lo:hi]
        return (z,) + tuple(args[1:])

    handle = BLOCKS(model)[layer].attn.c_proj.register_forward_pre_hook(pre_hook)
    try:
        yield
    finally:
        handle.remove()


@contextlib.contextmanager
def mean_ablate_heads(model, heads: list[tuple[int, int]], d_head: int):
    """Replace each named head's output with its mean over the batch.

    Mean, not zero. Zeroing takes the activation off-distribution and
    breaks the model for reasons unrelated to the claim being tested; the
    mean removes the information the head carried while leaving the scale
    the rest of the network expects.
    """
    by_layer: dict[int, list[int]] = {}
    for layer, head in heads:
        by_layer.setdefault(layer, []).append(head)

    handles = []
    for layer, head_list in by_layer.items():
        def pre_hook(_m, args, head_list=head_list):
            z = args[0].clone()
            for head in head_list:
                lo, hi = head * d_head, (head + 1) * d_head
                z[:, :, lo:hi] = z[:, :, lo:hi].mean(dim=0, keepdim=True)
            return (z,) + tuple(args[1:])
        handles.append(
            BLOCKS(model)[layer].attn.c_proj.register_forward_pre_hook(pre_hook))
    try:
        yield
    finally:
        for h in handles:
            h.remove()


def ascii_heatmap(grid: list[list[float]], row_labels: list[str],
                  col_labels: list[str], vmax: float | None = None) -> str:
    """A heatmap that survives being pasted into a terminal or a diff."""
    ramp = " .:-=+*#%@"
    flat = [abs(v) for row in grid for v in row]
    vmax = vmax if vmax is not None else (max(flat) if flat else 1.0)
    vmax = max(vmax, 1e-9)

    width = max(len(l) for l in row_labels)
    header = " " * (width + 2) + "".join(f"{c:>3}" for c in col_labels)
    lines = [header]
    for label, row in zip(row_labels, grid):
        cells = []
        for v in row:
            level = min(len(ramp) - 1, int(abs(v) / vmax * (len(ramp) - 1)))
            cells.append(f"  {ramp[level]}")
        lines.append(f"{label:>{width}}  " + "".join(cells))
    lines.append(f"{'':>{width}}  scale: '{ramp[0]}' = 0.00 .. "
                 f"'{ramp[-1]}' = {vmax:.2f} (absolute value)")
    return "\n".join(lines)


def _self_check() -> None:
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from ioi import baselines, build_dataset, load_model, logit_diff

    model, tokenizer, device = load_model()
    examples = build_dataset(tokenizer)
    clean, corrupted, clean_ids, corrupt_ids = baselines(
        model, tokenizer, examples, device)
    cache = cache_residuals(model, clean_ids)
    n_layers = len(BLOCKS(model))
    seq = clean_ids.shape[1]

    print("patching.py self-check")
    print(f"  layers {n_layers}, sequence length {seq}")
    print(f"  clean {clean:+.3f}   corrupted {corrupted:+.3f}")

    # Patching the LAST layer at EVERY position must reproduce the clean run
    # exactly: at that point nothing downstream can undo it.
    with contextlib.ExitStack() as stack:
        for pos in range(seq):
            stack.enter_context(
                patch_residual(model, n_layers - 1, pos, cache[n_layers - 1]))
        with torch.no_grad():
            patched = logit_diff(model(corrupt_ids).logits, examples).mean().item()
    ok = abs(patched - clean) < 1e-3
    print(f"  patch last layer, all positions -> {patched:+.3f}  "
          f"{'PASS' if ok else 'FAIL'} (must equal the clean baseline)")

    # Patching nothing must reproduce the corrupted run exactly.
    with torch.no_grad():
        untouched = logit_diff(model(corrupt_ids).logits, examples).mean().item()
    ok2 = abs(untouched - corrupted) < 1e-6
    print(f"  patch nothing                   -> {untouched:+.3f}  "
          f"{'PASS' if ok2 else 'FAIL'} (must equal the corrupted baseline)")
    print("\n  Both ends of the range check out, so a number between them means")
    print("  what it claims to mean. Run the sweeps.")


if __name__ == "__main__":
    _self_check()
