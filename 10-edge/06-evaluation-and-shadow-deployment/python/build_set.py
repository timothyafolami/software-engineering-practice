"""
Layer 10 - Topic 6: build a 200-item eval set with named slices.

What this demonstrates
    An eval set is a sampling design, not a folder of examples. Three
    decisions make it one, and this tool makes all three explicit:

      1. NAMED SLICES, fixed before sampling. The failure lives in a slice
         that is 2% of the set, so a set that cannot be cut by slice
         cannot find it. Four slices from real traffic plus one
         ADVERSARIAL slice mined from actual failures.
      2. A minimum n PER SLICE. A slice of 8 items supports no verdict at
         any effect size worth acting on -- see the `min detectable`
         column in compare.py -- so a slice below the floor is reported as
         a gap to fill, not quietly included.
      3. A recorded PROVENANCE for every item: where it came from and when
         it was sampled, so the set can be rebuilt and so contamination
         can be traced.

    Run topic 5's MinHash contamination check on the result BEFORE
    trusting any score computed from it. An eval set overlapping training
    data measures memorisation.

What to look for
    - The per-slice counts against the floor. It is normal for the
      adversarial slice to be short; that is a note to go mine more
      failures, not a reason to lower the floor.
    - Each item carrying its slice and its source. An item whose
      provenance is "someone pasted it in Slack" is not usable evidence
      six months later.

Standard library only. With --from-traces it samples a JSON Lines trace
file ({"prompt": ..., "response": ..., "ts": ..., "flagged": bool}); with
no arguments it emits a template set so the format is unambiguous:

    python3 python/build_set.py
    python3 python/build_set.py --from-traces traces.jsonl --n 200
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import time

SEED = 20260818
SLICES = ("short", "long", "code", "nonenglish", "adversarial")
MIN_PER_SLICE = 30


def classify(trace: dict) -> str:
    """Assign a trace to exactly one slice. Deliberately simple and
    deliberately visible: a slice definition you cannot read in ten
    seconds is a slice nobody will maintain."""
    if trace.get("flagged"):
        return "adversarial"
    prompt = trace.get("prompt", "")
    if any(ord(c) > 0x2E80 for c in prompt) or "¿" in prompt or "ß" in prompt:
        return "nonenglish"
    if "```" in prompt or "def " in prompt or "SELECT " in prompt.upper():
        return "code"
    return "long" if len(prompt) > 800 else "short"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-traces", type=pathlib.Path)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("eval_set.jsonl"))
    args = ap.parse_args()

    if not args.from_traces:
        print("No --from-traces given. Writing a template so the format is")
        print("unambiguous, and so the slice definitions are reviewable before")
        print("anyone spends a day labelling.\n")
        template = [
            {"item_id": f"{s}-000", "slice": s, "prompt": "<real traffic goes here>",
             "source": "template", "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                               time.gmtime())}
            for s in SLICES
        ]
        with args.out.open("w") as fh:
            for row in template:
                fh.write(json.dumps(row) + "\n")
        print(f"  wrote {args.out} with one template row per slice")
        print(f"  slices     : {', '.join(SLICES)}")
        print(f"  floor      : {MIN_PER_SLICE} items per slice for a verdict")
        print(f"  target n   : {args.n}")
        print("\n  Then, in order:")
        print("    1. sample real traffic into it (--from-traces)")
        print("    2. python3 ../05-pipelines-versioning-and-drift/python/"
              "minhash_contamination.py")
        print("    3. label it twice and check kappa with python/agreement.py")
        print("    4. only then score anything")
        return

    with args.from_traces.open() as fh:
        traces = [json.loads(l) for l in fh if l.strip()]

    buckets: dict[str, list[dict]] = {s: [] for s in SLICES}
    for t in traces:
        buckets[classify(t)].append(t)

    rng = random.Random(SEED)
    per_slice = max(MIN_PER_SLICE, args.n // len(SLICES))
    sampled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    rows = []
    print(f"  {'slice':<14} {'available':>10} {'sampled':>9} {'floor':>7} {'status':>12}")
    for s in SLICES:
        pool = buckets[s]
        take = min(per_slice, len(pool))
        chosen = rng.sample(pool, take) if take else []
        for i, t in enumerate(chosen):
            rows.append({
                "item_id": f"{s}-{i:03d}",
                "slice": s,
                "prompt": t.get("prompt", ""),
                "source": str(args.from_traces),
                "sampled_at": sampled_at,
            })
        status = "ok" if take >= MIN_PER_SLICE else "SHORT - mine more"
        print(f"  {s:<14} {len(pool):>10} {take:>9} {MIN_PER_SLICE:>7} {status:>12}")

    with args.out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"\n  wrote {len(rows)} items to {args.out}")
    print("  Next: the contamination check, then two independent labellings,")
    print("  then kappa. Scoring before those three is measuring something")
    print("  other than what you think.")


if __name__ == "__main__":
    main()
