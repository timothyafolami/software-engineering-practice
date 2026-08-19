"""
Layer 10 - Topic 4: the same prompt at temperature 0, 200 times.

What this demonstrates
    Thinking Machines Lab's *Defeating Nondeterminism in LLM Inference*
    (September 2025) sampled 1000 completions from Qwen3-235B at
    temperature 0 and got 80 unique completions, all 1000 identical for the
    first 102 tokens before diverging. This is that experiment at laptop
    scale, and the variable is the one that matters: whether the requests
    are serial or concurrent.

    Serial requests hit a server with a batch size of one every time.
    Concurrent requests hit a server whose batch shape depends on how many
    of your peers happened to be in flight -- and a reduction split
    differently across a different batch shape sums the same numbers in a
    different order. Floating-point addition is not associative, so the
    logits differ in the last bits, and occasionally those bits cross an
    argmax boundary between two close candidates. After that the sequences
    have nothing to do with each other.

What to look for
    - `distinct completions` in each mode. Serial should be 1. If the
      concurrent run is also 1, either your server batches nothing (try
      more clients) or it has a batch-invariant mode on.
    - `first divergence at token` -- the index where the completions stop
      agreeing. It is usually far into the output, which is why this bug
      survives casual testing: the first sentence is always identical.
    - If your server has a deterministic or batch-invariant mode, run both
      ways and record the THROUGHPUT cost as well as the uniqueness count.
      The tradeoff is the finding. The published cost on their benchmark
      was 26s to 42s, roughly 60%; yours will differ and yours is the one
      that matters.

Requires a model server on the host -- Docker Desktop on macOS has no
Metal passthrough, so a containerised server measures the CPU and nothing
you want. See ../lab/README.md.

    python3 -m mlx_lm.server --model ./q4 --port 8081

Standard library only. Runs with no arguments (both modes, 50 samples):
    python3 python/sample_determinism.py
    python3 python/sample_determinism.py --n 200 --clients 32
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = "http://127.0.0.1:8081/v1"
PROMPT = ("List the first twelve prime numbers, then explain in one "
          "paragraph why there are infinitely many of them.")


def complete(base: str, prompt: str, max_tokens: int, model: str) -> str:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{base}/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["text"]


def first_divergence(a: str, b: str) -> int:
    """Character index where two completions stop agreeing.

    Characters, not tokens, because this script has no tokenizer and an
    approximate token index would be a fabricated number. Divide by ~4 for
    a rough token estimate, and say that you did.
    """
    for i, (ca, cb) in enumerate(zip(a, b)):
        if ca != cb:
            return i
    return min(len(a), len(b))


def report(mode: str, texts: list[str], seconds: float) -> None:
    counts = collections.Counter(texts)
    most_common, most_common_n = counts.most_common(1)[0]
    divergences = [first_divergence(most_common, t) for t in texts
                   if t != most_common]
    print(f"\n  {mode}")
    print(f"    completions               : {len(texts)}")
    print(f"    distinct completions      : {len(counts)}")
    print(f"    most common seen          : {most_common_n} times")
    if divergences:
        print(f"    first divergence (chars)  : min {min(divergences)}, "
              f"median {int(statistics.median(divergences))}, "
              f"max {max(divergences)}")
        print(f"    ~tokens (chars / 4)       : ~{min(divergences) // 4} at the "
              f"earliest")
    else:
        print("    first divergence          : none -- every completion identical")
    print(f"    wall time                 : {seconds:.1f}s "
          f"({len(texts) / seconds:.1f} completions/s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--model", default="local")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--clients", type=int, default=32)
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--mode", choices=("serial", "concurrent", "both"), default="both")
    args = ap.parse_args()

    print("Temperature-0 determinism -- serial vs concurrent")
    print(f"  server     : {args.base}")
    print(f"  samples    : {args.n} per mode, max_tokens {args.max_tokens}")
    print(f"  concurrency: {args.clients} clients in the concurrent mode")

    try:
        complete(args.base, "ping", 1, args.model)
    except (urllib.error.URLError, OSError, KeyError) as exc:
        print(f"\n  BLOCKED: no model server answering at {args.base} ({exc}).")
        print("  Start one on the HOST -- not in a container, Docker Desktop on")
        print("  macOS has no Metal passthrough:")
        print("      python3 -m mlx_lm.server --model ./q4 --port 8081")
        print("  Then re-run. Nothing is recorded from a run that could not")
        print("  reach a server; a blocked experiment is not a null result.")
        return 2

    if args.mode in ("serial", "both"):
        start = time.perf_counter()
        texts = [complete(args.base, PROMPT, args.max_tokens, args.model)
                 for _ in range(args.n)]
        report("serial (batch size 1 every time)", texts,
               time.perf_counter() - start)

    if args.mode in ("concurrent", "both"):
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.clients) as pool:
            texts = list(pool.map(
                lambda _: complete(args.base, PROMPT, args.max_tokens, args.model),
                range(args.n)))
        report(f"concurrent ({args.clients} clients, batch shape varies)", texts,
               time.perf_counter() - start)

    print("\n  If the concurrent run has more distinct completions than the serial")
    print("  one, you have reproduced the finding on your own laptop: the answer")
    print("  depended on how the work was partitioned, and the partitioning")
    print("  depended on load. Nothing about your request changed.")
    print("\n  If your server offers a batch-invariant or deterministic mode, run")
    print("  the concurrent arm again with it on and record BOTH the uniqueness")
    print("  count and the completions/s. The tradeoff is the finding; the fix")
    print("  on its own is not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
