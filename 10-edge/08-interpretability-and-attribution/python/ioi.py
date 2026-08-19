"""
Layer 10 - Topic 8: the minimal pair and the metric, fixed before anything
is patched.

What this file is
    The indirect-object identification (IOI) task, its corruption, and the
    one number every other file in this topic reports. Fixing the metric in
    writing BEFORE running anything is the step that separates
    interpretability from looking at heatmaps until one is pretty.

    clean       "When Mary and John went to the store, John gave a drink to"
                -> the model should say " Mary" (the indirect object)
    corrupted   the same sentence with the two names swapped everywhere
                -> the model should now say " John"

    The swap is the corruption, and it is a good one because the two
    prompts have identical token LENGTH and identical structure. Only the
    identity of the names differs, so any position can be patched from one
    run into the other without shifting anything.

    metric      logit_diff = logit[" Mary"] - logit[" John"] at the final
                position, averaged over prompts.

                Positive on clean, negative on corrupted, by construction.
                A patch that restores the clean behaviour drives it back up,
                and the normalised score

                    (patched - corrupted) / (clean - corrupted)

                is 0 for "changed nothing" and 1 for "fully restored".

What to look for
    - The clean and corrupted baselines, and the gap between them. If the
      gap is small the model does not do this task reliably and every
      heatmap built on it is noise. Check the baseline before believing
      anything downstream.
    - Both answer tokens being single tokens. A multi-token name makes
      "the logit of the answer" ambiguous, and the ambiguity will not
      announce itself.

Requires torch and transformers. GPT-2 small runs on the M1 in seconds.
Imported by the sweep scripts; runs standalone to print the baselines:

    python3 python/ioi.py
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME = "gpt2"

# Name pairs, and the templates they go into. Both names must be a single
# token with a leading space, which is checked at build time rather than
# assumed -- see build_dataset.
NAME_PAIRS = [
    ("Mary", "John"), ("Alice", "Bob"), ("Sarah", "Tom"),
    ("Emma", "Paul"), ("Anna", "Mark"), ("Laura", "Steve"),
    ("Julia", "David"), ("Helen", "Peter"),
]

TEMPLATES = [
    "When {a} and {b} went to the store, {b} gave a drink to",
    "After {a} and {b} finished work, {b} handed the keys to",
    "While {a} and {b} waited outside, {b} passed the letter to",
]


@dataclass
class Example:
    clean: str
    corrupted: str
    io_token: int      # the correct answer on the CLEAN prompt
    s_token: int       # the correct answer on the CORRUPTED prompt


def load_model(device: str | None = None):
    if device is None:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(MODEL_NAME).to(device).eval()
    for p in model.parameters():
        p.requires_grad_(False)
    return model, tokenizer, device


def build_dataset(tokenizer, name_pairs=None, templates=None,
                  quiet: bool = False) -> list[Example]:
    """Every pair is checked, not assumed: both names single-token, both
    prompts the same token length."""
    candidates: list[tuple[int, Example]] = []
    name_pairs = name_pairs or NAME_PAIRS
    templates = templates or TEMPLATES
    for a, b in name_pairs:
        ta = tokenizer.encode(" " + a)
        tb = tokenizer.encode(" " + b)
        if len(ta) != 1 or len(tb) != 1:
            continue  # multi-token name: "the logit of the answer" is ambiguous
        for template in templates:
            clean = template.format(a=a, b=b)
            corrupted = template.format(a=b, b=a)
            n_clean = len(tokenizer.encode(clean))
            if n_clean != len(tokenizer.encode(corrupted)):
                continue  # different lengths: positions would not line up
            candidates.append((n_clean, Example(clean, corrupted, ta[0], tb[0])))

    if not candidates:
        return []
    # Every example must also be the same length as every OTHER example, or
    # "layer 7, position 4" means a different thing in different rows of the
    # heatmap. Keep the largest same-length group and say how many were
    # dropped rather than silently padding.
    lengths = [n for n, _ in candidates]
    modal = max(set(lengths), key=lengths.count)
    kept = [e for n, e in candidates if n == modal]
    dropped = len(candidates) - len(kept)
    if dropped and not quiet:
        print(f"  (dropped {dropped} of {len(candidates)} candidate prompts whose "
              f"token length was not {modal}; padding them would have made "
              f"position indices mean different things per row)")
    return kept


def logit_diff(logits: torch.Tensor, examples: list[Example]) -> torch.Tensor:
    """logit[IO] - logit[S] at the final position, per example.

    On the clean prompt the indirect object is the correct continuation, so
    this is positive. On the corrupted prompt the names have swapped, so the
    same difference is negative. That sign flip is what makes the metric a
    metric rather than a score.
    """
    last = logits[:, -1, :]
    io = torch.tensor([e.io_token for e in examples], device=logits.device)
    s = torch.tensor([e.s_token for e in examples], device=logits.device)
    return last.gather(1, io[:, None]).squeeze(1) - last.gather(1, s[:, None]).squeeze(1)


def tokenize_batch(tokenizer, texts: list[str], device: str) -> torch.Tensor:
    enc = tokenizer(texts, return_tensors="pt", padding=False)
    return enc["input_ids"].to(device)


@torch.no_grad()
def baselines(model, tokenizer, examples: list[Example], device: str):
    clean_ids = tokenize_batch(tokenizer, [e.clean for e in examples], device)
    corrupt_ids = tokenize_batch(tokenizer, [e.corrupted for e in examples], device)
    clean = logit_diff(model(clean_ids).logits, examples).mean().item()
    corrupted = logit_diff(model(corrupt_ids).logits, examples).mean().item()
    return clean, corrupted, clean_ids, corrupt_ids


def normalised(patched: float, clean: float, corrupted: float) -> float:
    """0 = the patch changed nothing, 1 = it fully restored clean behaviour."""
    span = clean - corrupted
    return (patched - corrupted) / span if abs(span) > 1e-9 else float("nan")


def main() -> None:
    model, tokenizer, device = load_model()
    examples = build_dataset(tokenizer)
    clean, corrupted, clean_ids, _ = baselines(model, tokenizer, examples, device)

    print("IOI minimal pairs -- the metric, fixed before anything is patched")
    print(f"  model            : {MODEL_NAME} on {device}")
    print(f"  examples         : {len(examples)} "
          f"({len(NAME_PAIRS)} name pairs x {len(TEMPLATES)} templates, "
          f"filtered to single-token names and matched lengths)")
    print(f"  sequence length  : {clean_ids.shape[1]} tokens")
    print()
    print(f"  example clean    : {examples[0].clean!r}")
    print(f"  example corrupted: {examples[0].corrupted!r}")
    print(f"  metric           : logit[IO] - logit[S] at the final position")
    print()
    print(f"  clean baseline     : {clean:+.3f}   (should be clearly positive)")
    print(f"  corrupted baseline : {corrupted:+.3f}   (should be clearly negative)")
    print(f"  span               : {clean - corrupted:.3f}")
    print()
    if clean <= 0 or corrupted >= 0:
        print("  BROKEN: the baselines do not bracket zero, so the model does not")
        print("  do this task reliably and every heatmap built on it is noise.")
        print("  Fix the prompts before running any sweep.")
    else:
        print("  The baselines bracket zero, so the task is real for this model")
        print("  and a patch has something to restore. Write both numbers down")
        print("  now -- every sweep in this topic is reported as a fraction of")
        print("  that span, and a span you did not record is a result you cannot")
        print("  reproduce.")


if __name__ == "__main__":
    main()
