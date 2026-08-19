"""
Layer 5 · Topic 3 - plot retry amplification over time, for all four variants.

WHAT THIS DEMONSTRATES
  Reads every `03_retry_storm_*.csv` in a directory and charts the leaf's
  received rate divided by the offered rate, against time, with the fault
  window marked. Four lines, one per variant, on one axis.

WHAT TO LOOK FOR IN THE OUTPUT
  Not the peak. The peak is the obvious number and it is the least useful one:
  everything spikes during a fault. Read the `t=200s` column - two minutes
  after the fault was removed - and ask which variants came back to 1.0x.

  A system still amplifying long after its trigger is gone is not recovering
  slowly. It is in a different stable state, sustained by its own retries,
  and topic 4 is about what that costs you.

  Note which mitigation is not like the others. Jitter changes WHEN retries
  arrive. A cap changes how many per request. Only the budget changes the
  total, because it is refilled by successes - and when everything is
  failing, nothing refills it.

RUN
    python3 tools/plot_amplification.py out/

  Without k6, check the plotter itself against the synthetic fixtures:
    python3 tools/make_fixtures.py
    python3 tools/plot_amplification.py out/fixtures/
"""
from __future__ import annotations

import os
import sys

import k6csv

BUCKET_S = 5.0
READ_AT = (100.0, 200.0, 280.0)


def variant_of(path: str) -> str:
    name = os.path.basename(path)
    return name.replace("03_retry_storm_", "").replace(".csv.gz", "").replace(".csv", "")


def main(argv: list[str]) -> int:
    directory = argv[1] if len(argv) > 1 else "out/"
    paths = k6csv.find_csvs(directory, "03_retry_storm_")

    series: dict[str, list[tuple[float, float]]] = {}
    success: dict[str, list[tuple[float, float]]] = {}
    peaks, at_time = {}, {}
    synthetic = False

    for path in paths:
        synthetic = synthetic or k6csv.is_synthetic(path)
        samples = k6csv.load(path)
        variant = variant_of(path)
        start = k6csv.run_start(samples)
        buckets = k6csv.bucket_by_time(samples, "amplification", BUCKET_S, t0=start)
        if not buckets:
            print(f"{path}: no `amplification` samples. Did the poller scenario run?")
            continue
        line = [(t, sum(v) / len(v)) for t, v in buckets]
        series[variant] = line
        peaks[variant] = max(y for _, y in line)
        at_time[variant] = {mark: nearest(line, mark) for mark in READ_AT}

        ok = k6csv.bucket_by_time(samples, "http_req_duration", BUCKET_S, t0=start, status="200")
        alll = k6csv.bucket_by_time(samples, "http_req_duration", BUCKET_S, t0=start)
        if alll:
            ok_by_t = {t: len(v) for t, v in ok}
            success[variant] = [(t, 100.0 * ok_by_t.get(t, 0) / len(v)) for t, v in alll]

    if synthetic:
        print(k6csv.SYNTHETIC_WARNING)

    print(f"\nLayer 5 / topic 3 - retry amplification, from {directory}\n")
    rows = []
    for variant in sorted(series):
        rows.append([variant, k6csv.fmt(peaks[variant], 2)] +
                    [k6csv.fmt(at_time[variant][mark], 2) for mark in READ_AT])
    print(k6csv.table(["variant", "peak"] + [f"t={int(m)}s" for m in READ_AT], rows))

    print(k6csv.xy_plot(series, xlabel="seconds since start of run",
                        ylabel="amplification (leaf received / offered)", height=18))

    if success:
        print(k6csv.xy_plot(success, xlabel="seconds since start of run",
                            ylabel="success rate %", height=12, ymax=105))

    print("The fault is on from t=60s to t=80s by default. Everything after t=80s is")
    print("recovery, and it is the half of this experiment that decides anything.")
    print("If a variant's t=200s column is far above 1.0x, nothing is broken and the")
    print("system is still generating its own load. That is topic 4's precondition.")

    png = k6csv.save_png(k6csv.png_path(directory, "amplification"),
                         "Retry amplification over time", series,
                         "seconds", "amplification (leaf received / offered)")
    print(k6csv.note_png(png))
    return 0


def nearest(line: list[tuple[float, float]], mark: float) -> float:
    """The bucket closest to `mark`, or NaN if the run never got there."""
    candidates = [(abs(t - mark), y) for t, y in line if abs(t - mark) <= BUCKET_S * 2]
    if not candidates:
        return float("nan")
    return min(candidates)[1]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
