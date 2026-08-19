"""
Layer 5 · Topic 5 - plot accepted p99, rejection rate and goodput per mode.

WHAT THIS DEMONSTRATES
  Reads every `05_shed_*.csv` in a directory and, for each mode and each rho
  step, computes four numbers:

    accepted rps      requests that got a real answer, per second
    rejected %        share of offered requests turned away
    p99 of ACCEPTED   the number the whole topic is about
    goodput           accepted rps again, named so you cannot mistake it for
                      throughput, which counts the rejections too

WHAT TO LOOK FOR IN THE OUTPUT
  p99 of accepted requests staying roughly FLAT past 100% offered, while the
  rejection rate rises to absorb the excess. That is the claim under test, and
  it is the difference between a service that degrades and one that collapses.

  Compute p99 over everything instead and the chart improves the instant
  shedding starts - because a 503 is fast. That is the single easiest way to
  produce a beautiful, meaningless load-shedding chart, so this script refuses
  to do it: rejections are counted, and never timed into the accepted p99.

  In `priority`, compare tier 0's success against tier 3's at the same rho. If
  both degrade together you have a limit, not a priority scheme.

RUN
    python3 tools/plot_shed.py out/

  Without k6, check the plotter against the synthetic fixtures:
    python3 tools/make_fixtures.py
    python3 tools/plot_shed.py out/fixtures/
"""
from __future__ import annotations

import os
import sys

import k6csv


def mode_of(path: str) -> str:
    return os.path.basename(path).replace("05_shed_", "").replace(".csv.gz", "").replace(".csv", "")


def accepted(sample: k6csv.Sample) -> bool:
    """A request that got a real answer. 503 is a rejection, not a slow success."""
    status = sample.tag("status", "")
    return status.startswith("2")


def main(argv: list[str]) -> int:
    directory = argv[1] if len(argv) > 1 else "out/"
    paths = k6csv.find_csvs(directory, "05_shed_")

    p99_series: dict[str, list[tuple[float, float]]] = {}
    goodput_series: dict[str, list[tuple[float, float]]] = {}
    reject_series: dict[str, list[tuple[float, float]]] = {}
    rows = []
    tier_rows = []
    synthetic = False

    for path in paths:
        synthetic = synthetic or k6csv.is_synthetic(path)
        samples = k6csv.load(path)
        mode = mode_of(path)
        by_rho: dict[str, list[k6csv.Sample]] = {}
        for sample in samples:
            if sample.metric != "http_req_duration":
                continue
            rho = sample.tag("rho")
            if rho:
                by_rho.setdefault(rho, []).append(sample)

        for rho_str in sorted(by_rho, key=float):
            group = by_rho[rho_str]
            window = k6csv.timespan(group) or 1.0
            ok = [s for s in group if accepted(s)]
            rejected = len(group) - len(ok)
            offered = group[0].tag("offered_rps", "-")
            p99_ok = k6csv.percentile([s.value for s in ok], 99) if ok else float("nan")
            rows.append([
                mode, rho_str, offered, k6csv.fmt(len(ok) / window),
                k6csv.fmt(100.0 * rejected / len(group)),
                k6csv.fmt(p99_ok), k6csv.fmt(len(ok) / window),
            ])
            rho = float(rho_str)
            p99_series.setdefault(mode, []).append((rho, p99_ok))
            goodput_series.setdefault(mode, []).append((rho, len(ok) / window))
            reject_series.setdefault(mode, []).append((rho, 100.0 * rejected / len(group)))

            tiers = {}
            for sample in group:
                tiers.setdefault(sample.tag("tier", "-"), []).append(sample)
            if len(tiers) > 1:
                for tier in sorted(tiers):
                    subset = tiers[tier]
                    ok_t = [s for s in subset if accepted(s)]
                    tier_rows.append([
                        mode, rho_str, tier, str(len(subset)),
                        k6csv.fmt(100.0 * len(ok_t) / len(subset)),
                        k6csv.fmt(k6csv.percentile([s.value for s in ok_t], 99)) if ok_t else "-",
                    ])

    if synthetic:
        print(k6csv.SYNTHETIC_WARNING)

    print(f"\nLayer 5 / topic 5 - admission control, from {directory}\n")
    print(k6csv.table(["mode", "rho", "offered", "accepted rps", "rejected %",
                       "p99 accepted ms", "goodput rps"], rows))

    if tier_rows:
        print("Per tier (tier 0 = /checkout, tier 3 = /search):\n")
        print(k6csv.table(["mode", "rho", "tier", "requests", "success %", "p99 accepted ms"],
                          tier_rows))

    print(k6csv.xy_plot(p99_series, xlabel="rho (offered / capacity)",
                        ylabel="p99 of ACCEPTED requests, ms", logy=True, height=16))
    print(k6csv.xy_plot(reject_series, xlabel="rho (offered / capacity)",
                        ylabel="rejected %", height=12, ymax=100))
    print(k6csv.xy_plot(goodput_series, xlabel="rho (offered / capacity)",
                        ylabel="goodput (accepted rps)", height=12))

    print("Read the first chart and the second one together. Flat p99 with a rising")
    print("rejection rate is the result. Flat p99 with NO rejections means the shedder")
    print("never engaged and you are looking at the baseline under another name.")

    png = k6csv.save_png(k6csv.png_path(directory, "shed"), "p99 of accepted requests",
                         p99_series, "rho", "p99 accepted ms", logy=True)
    print(k6csv.note_png(png))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
