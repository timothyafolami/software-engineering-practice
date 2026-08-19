"""
Layer 5 · Topic 1 - plot the latency knee from a k6 ramp.

WHAT THIS DEMONSTRATES
  Reads one `--out csv=` file from 01_ramp.js, groups every sample by its
  `rho` tag, and puts four things next to each other for each step:

    achieved lambda   counted from the samples themselves, not from the target
    p50 and p99       computed from the samples, never from a k6 summary
    S/(1-rho)         the arithmetic, so measurement and prediction share a row
    in-flight L       and lambda x W beside it, which is Little's Law checked

WHAT TO LOOK FOR IN THE OUTPUT
  1. `achieved` stops tracking `offered` at the plateau. The plateau is
     capacity; the gap is the backlog.
  2. p99 tracks the `S/(1-rho)` column while rho < 1 and leaves it entirely
     once rho >= 1 - the formula assumes a stable system and there is not one.
  3. `pool wait` is ~0 at rho=0.2. If it is not, capacity was measured wrong
     and the whole sweep ran at a different rho than the labels claim.
  4. `L` and `lambda x W` agree, or they do not. Disagreement is a finding:
     your gauge counts something your histogram does not time.

RUN
    python3 tools/plot_knee.py out/ramp.csv

  With no k6 available, check that this script works against a synthetic file:
    python3 tools/make_fixtures.py
    python3 tools/plot_knee.py out/fixtures/ramp.csv
  Those fixtures are a MODEL. Nothing from them belongs in a results table.
"""
from __future__ import annotations

import sys

import k6csv


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "out/ramp.csv"
    samples = k6csv.load(path)
    if k6csv.is_synthetic(path):
        print(k6csv.SYNTHETIC_WARNING)

    by_rho: dict[str, list[k6csv.Sample]] = {}
    for sample in samples:
        rho = sample.tag("rho")
        if rho:
            by_rho.setdefault(rho, []).append(sample)
    if not by_rho:
        raise SystemExit(
            f"{path} has no `rho` tag on any sample.\n"
            "01_ramp.js tags each step's scenario with its rho; a file without that tag\n"
            "is from a different script, or from a k6 run whose tags were stripped."
        )

    rows = []
    series_p50, series_p99, series_pred, series_wait = [], [], [], []
    for rho_str in sorted(by_rho, key=float):
        rho = float(rho_str)
        group = by_rho[rho_str]
        durations = k6csv.values_of(group, "http_req_duration")
        if not durations:
            continue
        offered = group[0].tag("offered_rps", "")
        achieved = k6csv.rate_of(group, "http_req_duration")
        p50 = k6csv.percentile(durations, 50)
        p99 = k6csv.percentile(durations, 99)
        waits = k6csv.values_of(group, "pool_wait_ms")
        inflight = k6csv.values_of(group, "inflight")
        # S is the service time with no queue in front of it: the fastest
        # thing this sweep ever saw. Measuring it under load would fold the
        # queue into the very number the prediction is built from.
        service_ms = min(durations)
        predicted = service_ms / (1 - rho) if rho < 1 else float("inf")
        mean_l = sum(inflight) / len(inflight) if inflight else float("nan")
        mean_w = sum(durations) / len(durations) / 1000.0
        lambda_w = achieved * mean_w
        rows.append([
            rho_str, offered, k6csv.fmt(achieved), k6csv.fmt(p50), k6csv.fmt(p99),
            k6csv.fmt(k6csv.percentile(waits, 50)) if waits else "-",
            k6csv.fmt(mean_l, 2), k6csv.fmt(lambda_w, 2),
            "inf" if predicted == float("inf") else k6csv.fmt(predicted),
        ])
        series_p50.append((rho, p50))
        series_p99.append((rho, p99))
        if predicted != float("inf"):
            series_pred.append((rho, predicted))
        if waits:
            series_wait.append((rho, k6csv.percentile(waits, 50)))

    print(f"\nLayer 5 / topic 1 - latency knee, from {path}\n")
    print(k6csv.table(
        ["rho", "offered", "achieved", "p50 ms", "p99 ms", "pool p50", "L", "lam x W", "S/(1-r)"],
        rows))

    print(k6csv.xy_plot(
        {"p99": series_p99, "p50": series_p50, "S/(1-rho)": series_pred},
        xlabel="rho (offered / capacity)", ylabel="latency ms", logy=True))

    if series_wait:
        print(k6csv.xy_plot({"pool wait p50": series_wait},
                            xlabel="rho", ylabel="pool checkout wait ms", height=10))

    print("Read the two columns that matter together: `achieved` stops rising while p99")
    print("does not. Nothing about the code changed at that point - the arithmetic of")
    print("waiting did. And if `L` and `lam x W` disagree, that is the finding, not noise.")

    png = k6csv.save_png(k6csv.png_path(path, "knee"), "Latency knee",
                         {"p99": series_p99, "p50": series_p50, "S/(1-rho)": series_pred},
                         "rho", "latency ms", logy=True)
    print(k6csv.note_png(png))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
