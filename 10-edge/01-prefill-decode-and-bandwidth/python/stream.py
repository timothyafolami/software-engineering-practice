"""
Layer 10 - Topic 1: achievable memory bandwidth, measured in NumPy.

What this demonstrates
    Decode at batch 1 is a memory transfer with a little arithmetic
    attached, so the ceiling that matters is the bytes/second this machine
    actually sustains -- not the spec-sheet figure. These are the STREAM
    kernels (copy / add / triad) over buffers far larger than any cache,
    run through NumPy so the timed loop underneath is C and SIMD and the
    interpreter never enters it.

What to look for
    - GB/s roughly flat across reps. Rep 0 is reported separately because
      it pays first-touch page faults; if rep 0 is the only slow one, that
      is the allocator, not the memory controller.
    - "triad out=" vs "triad naive" differ only in whether NumPy allocates
      temporaries. The gap is the temporary-allocation tax and it is the
      most common reason a NumPy pipeline misses the roof.
    - The best figure here is your bandwidth ceiling. Feed it to
      predict_decode.py. It should agree within ~10% with cpp/stream.cpp
      and rust/stream at the same thread count (one) -- a bigger gap is a
      benchmark bug, not a language finding.

Byte accounting is printed per kernel. Classic STREAM counts a copy of N
bytes as 2N (one read, one write). On a write-allocate cache the store
also pulls the destination line in, so true DRAM traffic can be 3N; every
implementation in this topic uses the 2N/3N convention so the three agree.

Runs with no arguments. Working set: 3 x 512 MiB = 1.5 GiB of float64.
"""

import platform
import sys
import time

import numpy as np

BYTES_PER_ARRAY = 512 * 1024 * 1024
DTYPE = np.float64
ELEMS = BYTES_PER_ARRAY // np.dtype(DTYPE).itemsize
REPS = 7
SCALAR = 3.0


def timed(fn, reps=REPS):
    """Return (first_rep_seconds, best_of_remaining_seconds)."""
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return samples[0], min(samples[1:])


def gbps(byte_factor, seconds):
    return (byte_factor * BYTES_PER_ARRAY) / seconds / 1e9


def main():
    print(f"machine        : {platform.machine()} / {platform.system()} "
          f"{platform.release()}")
    print(f"numpy          : {np.__version__}   python {sys.version.split()[0]}")
    print(f"dtype          : {np.dtype(DTYPE).name}, {ELEMS:,} elements per array")
    print(f"working set    : 3 x {BYTES_PER_ARRAY / 2**20:.0f} MiB = "
          f"{3 * BYTES_PER_ARRAY / 2**30:.2f} GiB")
    print("threads        : 1 (NumPy element-wise ufuncs are single-threaded)")
    print(f"reps           : {REPS}, rep 0 reported separately (first touch)")
    print()

    a = np.full(ELEMS, 1.0, dtype=DTYPE)
    b = np.full(ELEMS, 2.0, dtype=DTYPE)
    c = np.zeros(ELEMS, dtype=DTYPE)

    # Touch every page once before timing anything, so the numbers below
    # measure the memory controller and not the page-fault handler.
    a += 0.0
    b += 0.0
    c += 0.0

    kernels = [
        # label, byte factor (multiples of one array), callable
        ("copy       b[:] = a", 2, lambda: np.copyto(b, a)),
        ("add        c[:] = a + b", 3, lambda: np.add(a, b, out=c)),
        ("scale      b[:] = q * a", 2, lambda: np.multiply(a, SCALAR, out=b)),
    ]

    results = {}
    print(f"{'kernel':<24} {'bytes':>6}  {'rep0 GB/s':>10}  {'best GB/s':>10}  "
          f"{'best ms':>8}")
    print("-" * 66)
    for label, factor, fn in kernels:
        first, best = timed(fn)
        results[label.split()[0]] = gbps(factor, best)
        print(f"{label:<24} {factor:>5}N  {gbps(factor, first):>10.1f}  "
              f"{gbps(factor, best):>10.1f}  {best * 1e3:>8.1f}")

    # Triad has no single fused NumPy kernel, so it is measured two ways.
    # out= form: two passes, 2N + 3N = 5N of traffic, no allocation.
    def triad_out():
        np.multiply(b, SCALAR, out=c)
        np.add(c, a, out=c)

    first, best = timed(triad_out)
    results["triad_out"] = gbps(5, best)
    print(f"{'triad out= (2 passes)':<24} {5:>5}N  {gbps(5, first):>10.1f}  "
          f"{gbps(5, best):>10.1f}  {best * 1e3:>8.1f}")

    # Naive form: `a + q * b` allocates two full-size temporaries per call.
    # Reported against the 3N *logical* traffic a fused kernel would move,
    # so the number is "effective bandwidth for the work you asked for" --
    # the shortfall versus triad out= is the allocation tax, not DRAM.
    def triad_naive():
        c[:] = a + SCALAR * b

    first, best = timed(triad_naive)
    results["triad_naive"] = gbps(3, best)
    print(f"{'triad naive (temps)':<24} {3:>5}N* {gbps(3, first):>10.1f}  "
          f"{gbps(3, best):>10.1f}  {best * 1e3:>8.1f}")
    print("   * effective: 3N is the logical traffic; the real traffic is "
          "higher by two temporaries")
    print()

    ceiling = max(results[k] for k in ("copy", "add", "scale", "triad_out"))
    tax = 100.0 * (1.0 - results["triad_naive"] / results["triad_out"])
    print(f"best sustained            : {ceiling:.1f} GB/s   <- your ceiling; "
          f"use this in predict_decode.py")
    print(f"temporary-allocation tax  : {tax:.0f}% of triad out= throughput")
    print()
    print("Next: divide this ceiling by your model's weight bytes to predict")
    print("      batch-1 decode tok/s, then measure it. Write the prediction")
    print("      down first -- see ../../PREDICTIONS.md.")


if __name__ == "__main__":
    main()
