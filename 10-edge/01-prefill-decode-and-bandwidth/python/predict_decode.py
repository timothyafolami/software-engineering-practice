"""
Layer 10 - Topic 1: the paper prediction, before you generate a token.

What this demonstrates
    The whole model of batch-1 decode is two divisions:

        decode tok/s  ~=  achievable bandwidth  /  weight bytes
        KV bytes/token =  2 * layers * kv_heads * head_dim * dtype_bytes

    This script does both against a real model directory so the prediction
    is written down before `mlx_lm.generate` prints an answer. Predicting
    after measuring is not predicting.

What to look for
    - The predicted tok/s is an upper bound: it assumes decode moves the
      weights and nothing else, at the full ceiling stream.py measured.
      Measuring *above* it means something is mislabelled -- prefill tok/s
      reported as decode, or speculative decoding silently on.
    - KV bytes per token do not depend on batch size; total KV bytes scale
      with batch x context. The "concurrent conversations" line is where
      topic 2's block allocator comes from.
    - Compare q4 and q8 of the same model: the arithmetic is identical, only
      the bytes change, so the speed ratio should track the byte ratio.

Usage (runs with no arguments):
    python3 python/predict_decode.py                       # worked example
    python3 python/predict_decode.py ./q4 ./q8 --bandwidth 58.7
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".npz", ".gguf", ".pth")
BANDWIDTH_GRID = (25, 50, 100, 200, 400, 800, 3350)


def host_memory_bytes():
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5)
        return int(out.stdout.strip())
    except Exception:
        return None


def weight_bytes(model_dir: pathlib.Path) -> int:
    total = 0
    for path in model_dir.rglob("*"):
        if path.is_file() and path.suffix in WEIGHT_SUFFIXES:
            total += path.stat().st_size
    return total


def load_config(model_dir: pathlib.Path) -> dict:
    cfg_path = model_dir / "config.json"
    if not cfg_path.exists():
        raise SystemExit(f"{cfg_path} not found -- is {model_dir} a converted model?")
    return json.loads(cfg_path.read_text())


def kv_shape(cfg: dict):
    """(layers, kv_heads, head_dim, dtype_bytes) from a HF-style config."""
    layers = cfg["num_hidden_layers"]
    heads = cfg["num_attention_heads"]
    kv_heads = cfg.get("num_key_value_heads", heads)
    hidden = cfg.get("hidden_size")
    head_dim = cfg.get("head_dim") or (hidden // heads)
    # The KV cache is not quantized by default even when the weights are;
    # it is held at the activation dtype.
    dtype = str(cfg.get("torch_dtype", "float16"))
    dtype_bytes = 4 if "32" in dtype else 2
    return layers, kv_heads, head_dim, dtype_bytes


def report(name, w_bytes, layers, kv_heads, head_dim, dtype_bytes, bandwidth, mem_bytes):
    print(f"=== {name} ===")
    print(f"  weights on disk        : {w_bytes / 1e9:.2f} GB "
          f"({w_bytes / 2**30:.2f} GiB)")
    print(f"  KV geometry            : {layers} layers x {kv_heads} kv_heads x "
          f"head_dim {head_dim} x {dtype_bytes} B")
    kv_per_token = 2 * layers * kv_heads * head_dim * dtype_bytes
    print(f"  KV bytes per token     : 2 x {layers} x {kv_heads} x {head_dim} x "
          f"{dtype_bytes} = {kv_per_token:,} B ({kv_per_token / 1024:.0f} KiB)")
    for ctx in (256, 4096, 16384, 131072):
        print(f"    at {ctx:>6} tokens    : {kv_per_token * ctx / 2**30:>8.3f} GiB "
              f"per sequence")
    print()

    if bandwidth is not None:
        bw = bandwidth * 1e9
        print(f"  predicted decode tok/s at {bandwidth:.1f} GB/s")
        print(f"    weights only         : {bw / w_bytes:>8.1f} tok/s   "
              f"(= bandwidth / weight bytes)")
        for ctx in (256, 4096, 16384):
            per_step = w_bytes + kv_per_token * ctx
            print(f"    + KV at {ctx:>6} ctx   : {bw / per_step:>8.1f} tok/s   "
                  f"(= bandwidth / (weights + KV))")
    else:
        print("  predicted decode tok/s = bandwidth / weight bytes, evaluated")
        print("  across a grid so you can find the figure stream.py measured:")
        print(f"    {'GB/s':>8}  {'tok/s (weights only)':>22}")
        for bw in BANDWIDTH_GRID:
            print(f"    {bw:>8}  {bw * 1e9 / w_bytes:>22.1f}")
        print("  (pass --bandwidth <your measured GB/s> to collapse this to one row)")
    print()

    if mem_bytes:
        budget = mem_bytes - w_bytes
        kv_8k = kv_per_token * 8192
        print(f"  host memory            : {mem_bytes / 2**30:.1f} GiB")
        print(f"  left after weights     : {budget / 2**30:.1f} GiB")
        if budget > 0:
            print(f"  concurrent 8k contexts : {int(budget // kv_8k)} "
                  f"(before any framework overhead, and before the OS wants "
                  f"any of it back)")
        else:
            print("  weights do not fit in host memory at this quantization")
    print()


def worked_example(bandwidth, mem_bytes):
    print("No model directory given, so this is the worked example from the")
    print("topic README -- Llama-3-8B, 32 layers, 8 KV heads under GQA,")
    print("head_dim 128, fp16 KV. The arithmetic is executable here so you")
    print("can check the README rather than trust it.\n")
    layers, kv_heads, head_dim, dtype_bytes = 32, 8, 128, 2
    kv_per_token = 2 * layers * kv_heads * head_dim * dtype_bytes
    print(f"  KV bytes per token = 2 x {layers} x {kv_heads} x {head_dim} x "
          f"{dtype_bytes} = {kv_per_token:,} B = {kv_per_token / 1024:.0f} KiB")
    print(f"  8192-token context = 8192 x {kv_per_token / 1024:.0f} KiB = "
          f"{kv_per_token * 8192 / 2**30:.2f} GiB, re-read every decode step")
    print()
    print("  Same model, same KV geometry, different weight quantizations.")
    print("  8B parameters at B bytes each; the point is that only the byte")
    print("  column moves, the FLOP count is identical in every row:")
    print(f"    {'dtype':>8} {'bytes/param':>12} {'weights GB':>12}", end="")
    if bandwidth is not None:
        print(f" {'tok/s @ ' + format(bandwidth, '.0f') + ' GB/s':>20}")
    else:
        print()
    for label, bpp in (("fp16", 2.0), ("int8", 1.0), ("int4", 0.5)):
        gb = 8e9 * bpp / 1e9
        print(f"    {label:>8} {bpp:>12.1f} {gb:>12.1f}", end="")
        if bandwidth is not None:
            print(f" {bandwidth / gb:>20.1f}")
        else:
            print()
    print()
    print("  4-bit weights are 4x fewer bytes than fp16 and therefore about")
    print("  4x the decode rate. Measuring 2.2x instead is question 2 of the")
    print("  topic's 'answer before moving on'.")
    print()
    print("Point this at a real model to get the real numbers:")
    print("  python3 -m mlx_lm.convert --hf-path Qwen/Qwen3-8B -q --q-bits 4 "
          "--mlx-path ./q4")
    print("  python3 python/predict_decode.py ./q4 ./q8 --bandwidth "
          "<GB/s from stream.py>")
    if mem_bytes:
        print(f"\n  (this host has {mem_bytes / 2**30:.1f} GiB of unified memory)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model_dir", nargs="*", type=pathlib.Path,
                    help="converted model directory (config.json + weight files)")
    ap.add_argument("--bandwidth", type=float, default=None,
                    help="achievable GB/s, from stream.py")
    args = ap.parse_args()

    mem_bytes = host_memory_bytes()

    if not args.model_dir:
        worked_example(args.bandwidth, mem_bytes)
        return

    for d in args.model_dir:
        if not d.is_dir():
            print(f"skipping {d}: not a directory", file=sys.stderr)
            continue
        cfg = load_config(d)
        wb = weight_bytes(d)
        if wb == 0:
            print(f"skipping {d}: no weight files "
                  f"({', '.join(WEIGHT_SUFFIXES)})", file=sys.stderr)
            continue
        report(str(d), wb, *kv_shape(cfg), args.bandwidth, mem_bytes)

    if len(args.model_dir) == 2:
        a, b = args.model_dir
        try:
            ratio = weight_bytes(a) / weight_bytes(b)
        except ZeroDivisionError:
            return
        print(f"byte ratio {a} : {b} = {ratio:.2f}x")
        print("Decode speed should track this ratio, because the arithmetic is")
        print("identical between the two and only the bytes moved change. This")
        print("is the cleanest single confirmation of the bandwidth model.")


if __name__ == "__main__":
    main()
