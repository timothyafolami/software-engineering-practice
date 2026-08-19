"""
Layer 1 - Memory & cache locality
Pointer-chasing benchmark: same logical traversal (visit N nodes exactly once
each lap), same amount of "work" (one addition per step), but two different
physical memory layouts:

  sequential -> node i's successor lives at i+1 in memory (cache-friendly)
  shuffled   -> node i's successor is a random other node (cache-hostile)

If locality mattered as much as the roadmap claims, shuffled should be
noticeably slower than sequential even though both do the exact same number
of additions. Run it and see what Python's interpreter overhead does to that
signal.
"""
import random
import time

N = 2_000_000        # big enough to blow past L2/L3 cache
LAPS = 5              # full traversals of all N nodes


def build(shuffled: bool):
    values = list(range(N))
    next_idx = [0] * N
    if not shuffled:
        for i in range(N):
            next_idx[i] = (i + 1) % N
    else:
        perm = list(range(N))
        random.shuffle(perm)
        for i in range(N):
            next_idx[perm[i]] = perm[(i + 1) % N]
    return values, next_idx


def traverse(values, next_idx, laps):
    total = 0
    idx = 0
    steps = N * laps
    for _ in range(steps):
        total += values[idx]
        idx = next_idx[idx]
    return total


def bench(label, shuffled):
    values, next_idx = build(shuffled)
    start = time.perf_counter()
    total = traverse(values, next_idx, LAPS)
    elapsed = time.perf_counter() - start
    ns_per_step = elapsed / (N * LAPS) * 1e9
    print(f"{label:10s}  total={total:>15d}  time={elapsed:6.3f}s  {ns_per_step:6.1f} ns/step")


if __name__ == "__main__":
    print(f"N={N:,} laps={LAPS} (python {__import__('platform').python_version()})")
    bench("sequential", shuffled=False)
    bench("shuffled", shuffled=True)
