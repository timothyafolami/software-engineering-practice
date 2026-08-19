"""
Layer 10 - Topic 7: MFU, and the economics of the run that produced it.

What this demonstrates
    Three numbers from one measurement, each of which answers a different
    question you will be asked:

      MFU        achieved FLOP/s divided by the hardware's DENSE peak for
                 the dtype you actually ran. This is the "am I using the
                 machine" number, and the two traps are using a sparsity-
                 inflated peak (NVIDIA's headline BF16 figures carry an
                 asterisk meaning 2:4 structural sparsity; dense is half)
                 and using a peak for a dtype you did not run.
      regime     achieved FLOP/s per byte of weights moved, against the
                 hardware's ridge point from topic 1. Below the ridge you
                 are bandwidth-bound and no amount of kernel fusion will
                 help; above it you are compute-bound and it will.
      cost       $/1M tokens, derived from the rate YOU paid, which this
                 tool will not guess. Per-hour GPU prices move monthly and
                 an uncited price is the same defect as a fabricated
                 measurement.

    The FLOP estimate is the 6ND rule -- 6 FLOPs per parameter per token,
    2 forward and 4 backward -- plus, reported separately, attention's
    quadratic term, which 6ND ignores. Seeing the two side by side tells
    you when the approximation has stopped holding, which is exactly when
    people keep quoting it.

What to look for
    - The attention share of total FLOPs. At short context it is a
      rounding error and 6ND is fine. At long context it is not, and an
      MFU computed from 6ND alone is understated by that share.
    - MFU against the regime line. A low MFU with a bandwidth-bound
      verdict is not a tuning opportunity; it is physics, and topic 1
      already told you the ceiling.

Standard library only. Runs with no arguments on a worked example, and
takes your own measurement:

    python3 python/mfu.py
    python3 python/mfu.py --params 25000000 --tokens 4e8 --seconds 3600 \\
        --peak-tflops 2.6 --seq 512
    python3 python/mfu.py --params 25000000 --tokens 4e8 --seconds 3600 \\
        --peak-tflops 312 --dollars-per-hour <the rate you actually paid>
"""

from __future__ import annotations

import argparse

# Vendor-published DENSE figures, for reference only. Check the datasheet
# for the part and dtype you actually have before using any of them.
REFERENCE_PEAKS = {
    "M1 (base), FP32":      (2.6, 68.25),
    "M1 Pro, FP32":         (5.2, 200.0),
    "M1 Max, FP32":         (10.4, 400.0),
    "A100 80GB, BF16 dense": (312.0, 2039.0),
    "H100 SXM, BF16 dense": (989.0, 3350.0),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--params", type=float, default=25e6)
    ap.add_argument("--tokens", type=float, default=4e8)
    ap.add_argument("--seconds", type=float, default=3600.0)
    ap.add_argument("--peak-tflops", type=float, default=None,
                    help="DENSE peak for the dtype you ran. Not the marketing "
                         "number with the sparsity asterisk.")
    ap.add_argument("--bandwidth-gbs", type=float, default=None,
                    help="achievable GB/s, from topic 1's stream.py")
    ap.add_argument("--seq", type=int, default=512)
    ap.add_argument("--layers", type=int, default=6)
    ap.add_argument("--d-model", type=int, default=384)
    ap.add_argument("--bytes-per-param", type=float, default=2.0)
    ap.add_argument("--tokens-per-step", type=float, default=8192.0,
                    help="batch x seq. This is the batch-size lever: the "
                         "weights are read once per STEP and serve every token "
                         "in it.")
    ap.add_argument("--dollars-per-hour", type=float, default=None,
                    help="the rate you actually paid, from the provider's page "
                         "at the moment you rented")
    args = ap.parse_args()

    n, tokens, seconds = args.params, args.tokens, args.seconds

    # 6ND: 2 FLOPs per parameter forward, 4 backward.
    dense_flops = 6 * n * tokens
    # Attention's quadratic term, which 6ND omits: per layer, QK^T and
    # (attn @ V) each cost 2 * seq * d_model FLOPs per token, forward; the
    # backward pass roughly doubles it again.
    attn_flops = 6 * 2 * args.layers * args.seq * args.d_model * tokens
    total_flops = dense_flops + attn_flops

    print("MFU and the economics of the run")
    print(f"  parameters        : {n:,.0f}")
    print(f"  tokens            : {tokens:,.0f}")
    print(f"  wall time         : {seconds:,.1f}s ({seconds / 3600:.2f}h)")
    print(f"  throughput        : {tokens / seconds:,.0f} tokens/s")
    print()
    print(f"  6ND dense FLOPs   : {dense_flops:.3e}")
    print(f"  attention FLOPs   : {attn_flops:.3e}   "
          f"({100 * attn_flops / total_flops:.1f}% of total at seq={args.seq}, "
          f"layers={args.layers}, d_model={args.d_model})")
    print(f"  total FLOPs       : {total_flops:.3e}")
    if attn_flops / total_flops > 0.15:
        print("    ^ attention is no longer a rounding error at this context")
        print("      length. An MFU computed from 6ND alone understates the")
        print("      work you did by that share.")
    print()
    achieved = total_flops / seconds
    print(f"  achieved          : {achieved / 1e12:.3f} TFLOP/s")

    if args.peak_tflops:
        mfu = achieved / (args.peak_tflops * 1e12)
        print(f"  dense peak given  : {args.peak_tflops:.1f} TFLOP/s")
        print(f"  MFU               : {100 * mfu:.1f}%")
        if mfu > 1.0:
            print("    ^ above 100% is impossible. Either the peak is a sparsity")
            print("      number (halve it), or it is for a dtype you did not run,")
            print("      or the token count is wrong.")
    else:
        print("  MFU               : pass --peak-tflops for the DENSE peak of the")
        print("                      dtype you ran. Reference figures, vendor-")
        print("                      published, to check against a datasheet:")
        for name, (tf, bw) in REFERENCE_PEAKS.items():
            print(f"                        {name:<24} {tf:>7.1f} TFLOP/s  "
                  f"{bw:>7.1f} GB/s   ridge {tf * 1e12 / (bw * 1e9):>5.1f} FLOP/byte")

    if args.bandwidth_gbs:
        weight_bytes = n * args.bytes_per_param
        # The weights are read once per STEP and serve every token in the
        # step, so intensity is (FLOPs per step) / (weight bytes per step):
        #   6 * N * tokens_per_step  /  (N * bytes_per_param)
        #   = 6 * tokens_per_step / bytes_per_param
        # N cancels, which is why model size does not change the regime and
        # batch size is the only lever that does. Activation traffic is
        # ignored, so this is an upper bound on intensity.
        flops_per_step = 6 * n * args.tokens_per_step
        intensity = flops_per_step / weight_bytes
        print()
        print(f"  weight bytes      : {weight_bytes / 1e9:.3f} GB at "
              f"{args.bytes_per_param} bytes/param")
        print(f"  tokens per step   : {args.tokens_per_step:,.0f}")
        print(f"  arithmetic intensity: {intensity:.1f} FLOP/byte  "
              f"(= 6 x tokens_per_step / bytes_per_param; N cancels)")
        if args.peak_tflops:
            ridge = args.peak_tflops * 1e12 / (args.bandwidth_gbs * 1e9)
            print(f"  ridge point       : {ridge:.1f} FLOP/byte")
            verdict = ("BANDWIDTH-bound" if intensity < ridge else "COMPUTE-bound")
            print(f"  regime            : {verdict}")
            if intensity < ridge:
                print("    Kernel fusion will not help. The answer is fewer bytes")
                print("    (quantization) or more work per byte (bigger batch).")
            else:
                print("    Fusion and better kernels are the lever here.")

    if args.dollars_per_hour:
        cost = args.dollars_per_hour * seconds / 3600
        print()
        print(f"  rate you paid     : ${args.dollars_per_hour:.4f}/hour  "
              f"(your figure, not this file's)")
        print(f"  cost of this run  : ${cost:.2f}")
        print(f"  $ per 1M tokens   : ${cost / (tokens / 1e6):.4f}")
        print(f"  $ per 1B tokens   : ${cost / (tokens / 1e9):.2f}")
    else:
        print()
        print("  cost              : pass --dollars-per-hour with the rate from")
        print("                      the provider's page at the moment you rented.")
        print("                      Per-hour prices move monthly, so this file")
        print("                      will not guess one -- an uncited price is the")
        print("                      same defect as a fabricated measurement.")


if __name__ == "__main__":
    main()
