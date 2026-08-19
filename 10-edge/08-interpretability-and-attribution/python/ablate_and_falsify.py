"""
Layer 10 - Topic 8, steps 5 and 6: try to falsify the hypothesis.

What this demonstrates
    The head sweep produced a list of heads. A list is not a finding. This
    file runs the three checks that turn it into one, in the order that
    makes each of them able to fail:

      1. ABLATE the named heads on the CLEAN prompts and check the
         behaviour degrades. Mean-ablation, not zero-ablation: zeroing
         takes the activation off the distribution the rest of the network
         was trained against, so the model breaks for reasons that have
         nothing to do with the claim.
      2. ABLATE THE SAME NUMBER of randomly chosen heads, several times,
         as a control. If random heads do nearly as much damage, the
         hypothesis has identified "attention heads matter" and nothing
         more specific. This is the check that most write-ups skip and it
         is the cheapest one here.
      3. Test on a HOLDOUT prompt set -- different templates, different
         names, never used to derive the hypothesis. A hypothesis that
         only explains the prompts it came from has explained nothing.

What to look for
    - Named-head damage against random-head damage. The gap, not the
      absolute number, is the claim.
    - The holdout column. Similar damage on prompts the hypothesis never
      saw is the result worth writing up. Much smaller damage there means
      you fitted the derivation set, which is a real and publishable
      finding about your method rather than about the model.
    - Anything you could not resolve. The honest unresolved section is what
      makes people trust the rest of the write-up.

Requires torch and transformers. Runs with no arguments using the heads
head_sweep.py ranked highest on this machine; pass your own:

    python3 python/ablate_and_falsify.py
    python3 python/ablate_and_falsify.py --heads 9.9,10.0,9.6
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ioi import (build_dataset, load_model, logit_diff,  # noqa: E402
                 tokenize_batch)
from patching import BLOCKS, mean_ablate_heads  # noqa: E402

SEED = 20260818

# A holdout set: different templates AND different names, never used to
# derive any hypothesis. The point of a holdout is that it was not
# available when the claim was made.
HOLDOUT_NAMES = [("Nina", "Carl"), ("Rosa", "Frank"), ("Grace", "Henry"),
                 ("Clara", "Simon")]
HOLDOUT_TEMPLATES = [
    "Then {a} and {b} arrived at the party, and {b} gave a gift to",
    "Once {a} and {b} left the office, {b} sent the report to",
]


@torch.no_grad()
def score(model, tokenizer, examples, device, heads=None, d_head=64) -> float:
    ids = tokenize_batch(tokenizer, [e.clean for e in examples], device)
    if heads:
        with mean_ablate_heads(model, heads, d_head):
            logits = model(ids).logits
    else:
        logits = model(ids).logits
    return logit_diff(logits, examples).mean().item()


def parse_heads(text: str) -> list[tuple[int, int]]:
    out = []
    for token in text.split(","):
        layer, head = token.strip().split(".")
        out.append((int(layer), int(head)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heads", default="8.10,10.0,9.7",
                    help="comma-separated layer.head, from head_sweep.py")
    ap.add_argument("--controls", type=int, default=5,
                    help="how many random control sets to draw")
    args = ap.parse_args()

    model, tokenizer, device = load_model()
    n_layers = len(BLOCKS(model))
    n_heads = model.config.n_head
    d_head = model.config.n_embd // n_heads

    derivation = build_dataset(tokenizer, quiet=True)
    holdout = build_dataset(tokenizer, name_pairs=HOLDOUT_NAMES,
                            templates=HOLDOUT_TEMPLATES, quiet=True)

    named = parse_heads(args.heads)
    base_d = score(model, tokenizer, derivation, device)
    base_h = score(model, tokenizer, holdout, device)

    print("Falsifying the hypothesis")
    print(f"  model            : gpt2 on {device}")
    print(f"  hypothesis       : heads {args.heads} carry the indirect object's")
    print(f"                     identity to the final position")
    print(f"  ablation         : MEAN over the batch, not zero")
    print(f"  derivation set   : {len(derivation)} prompts (the sweep used these)")
    print(f"  holdout set      : {len(holdout)} prompts, new names AND new")
    print(f"                     templates, never used to derive anything")
    print()
    print(f"  {'condition':<34} {'derivation':>12} {'holdout':>10} "
          f"{'deriv drop':>12} {'holdout drop':>13}")
    print("  " + "-" * 86)
    print(f"  {'no ablation (baseline)':<34} {base_d:>12.3f} {base_h:>10.3f} "
          f"{'-':>12} {'-':>13}")

    abl_d = score(model, tokenizer, derivation, device, named, d_head)
    abl_h = score(model, tokenizer, holdout, device, named, d_head)
    drop_d = 100 * (base_d - abl_d) / abs(base_d)
    drop_h = 100 * (base_h - abl_h) / abs(base_h)
    print(f"  {'ablate named heads':<34} {abl_d:>12.3f} {abl_h:>10.3f} "
          f"{drop_d:>11.1f}% {drop_h:>12.1f}%")

    rng = random.Random(SEED)
    all_heads = [(l, h) for l in range(n_layers) for h in range(n_heads)]
    control_d, control_h = [], []
    for i in range(args.controls):
        pick = rng.sample([h for h in all_heads if h not in named], len(named))
        cd = score(model, tokenizer, derivation, device, pick, d_head)
        ch = score(model, tokenizer, holdout, device, pick, d_head)
        control_d.append(100 * (base_d - cd) / abs(base_d))
        control_h.append(100 * (base_h - ch) / abs(base_h))
        label = "random control " + ",".join(f"{l}.{h}" for l, h in pick)
        print(f"  {label[:34]:<34} {cd:>12.3f} {ch:>10.3f} "
              f"{control_d[-1]:>11.1f}% {control_h[-1]:>12.1f}%")

    mean_ctrl_d = sum(control_d) / len(control_d)
    mean_ctrl_h = sum(control_h) / len(control_h)
    print("  " + "-" * 86)
    print(f"  {'mean of random controls':<34} {'':>12} {'':>10} "
          f"{mean_ctrl_d:>11.1f}% {mean_ctrl_h:>12.1f}%")

    print()
    print(f"  named heads did {drop_d:.1f}% damage on the derivation set against "
          f"{mean_ctrl_d:.1f}% for")
    print(f"  the same number of random heads, and {drop_h:.1f}% against "
          f"{mean_ctrl_h:.1f}% on the holdout.")
    print()
    if drop_d > 2 * max(mean_ctrl_d, 1e-6) and drop_h > 2 * max(mean_ctrl_h, 1e-6):
        print("  The hypothesis survived both checks: the named heads do specific")
        print("  damage, and they do it on prompts they were never selected on.")
        print("  Surviving is not proving. Write down what this does NOT show --")
        print("  it does not show these heads are sufficient, it does not show")
        print("  they are the only route, and it does not show they do the same")
        print("  job on any other task.")
    else:
        print("  The hypothesis did NOT survive. That is a result, and it is the")
        print("  more common one. Either random heads did comparable damage (the")
        print("  claim is not specific), or the effect vanished on the holdout")
        print("  (the claim was fitted to the derivation prompts). Say which,")
        print("  and say it in the write-up rather than trying more head sets")
        print("  until one works -- that search is how a heatmap becomes a story.")

    print()
    print("  Cross-check next: run circuit-tracer's attribution graph on the same")
    print("  prompt and see whether it names the same components. The")
    print("  disagreements are the interesting part and belong in the write-up.")


if __name__ == "__main__":
    main()
