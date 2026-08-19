"""
Layer 5 · Topic 4 - plot goodput against throughput, before and after the trigger.

WHAT THIS DEMONSTRATES
  One chart, five lines, one axis: offered rate, throughput, goodput, cache
  hit rate and retry ratio, over the whole run. The trigger is a single
  FLUSHALL somewhere in the middle and the offered rate never changes.

WHAT TO LOOK FOR IN THE OUTPUT
  The GAP between throughput and goodput. Throughput counts work the server
  did; goodput counts work someone was still waiting for. A system can be
  busier than it has ever been and delivering nothing, and every dashboard
  built on requests-per-second will call that healthy.

  Then look at the hit rate coming back while goodput does not. That is the
  whole topic in one crossing: the trigger is gone, the cache is warm again,
  and the failure is now sustained by traffic the failure itself created.

  If goodput recovers on its own, say so and write down when. A metastable
  state that self-heals in 40 seconds at these constants is a real result,
  and it means your amplification is weaker than the version in the paper -
  not that the experiment failed.

RUN
    python3 tools/plot_goodput.py out/metastable.csv

  Without k6, check the plotter against a synthetic file:
    python3 tools/make_fixtures.py
    python3 tools/plot_goodput.py out/fixtures/metastable.csv
"""
from __future__ import annotations

import sys

import k6csv

BUCKET_S = 5.0


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "out/metastable.csv"
    samples = k6csv.load(path)
    if k6csv.is_synthetic(path):
        print(k6csv.SYNTHETIC_WARNING)
    start = k6csv.run_start(samples)

    def line(metric: str) -> list[tuple[float, float]]:
        return [(t, sum(v) / len(v))
                for t, v in k6csv.bucket_by_time(samples, metric, BUCKET_S, t0=start)]

    throughput = line("throughput_rps")
    goodput = line("goodput_rps")
    hit_rate = line("hit_rate_pct")
    retry_ratio = line("retry_ratio")
    pg = line("pg_conns")

    if not throughput and not goodput:
        raise SystemExit(
            f"{path} has no throughput_rps/goodput_rps samples.\n"
            "Those come from 04_metastable.js's poller scenario, which diffs\n"
            "/admin/counters once a second. A run without it has latencies but no rates."
        )

    print(f"\nLayer 5 / topic 4 - goodput vs throughput, from {path}\n")

    print(k6csv.xy_plot({"throughput": throughput, "goodput": goodput},
                        xlabel="seconds since start of run", ylabel="requests/second",
                        height=18))

    print(k6csv.xy_plot({"cache hit %": hit_rate, "retry ratio x10": [(t, y * 10) for t, y in retry_ratio],
                         "pg conns": pg},
                        xlabel="seconds since start of run",
                        ylabel="hit % / retry ratio x10 / connections", height=14))

    rows = []
    marks = [t for t, _ in throughput][::max(1, len(throughput) // 12)] if throughput else []
    lookup = {name: dict(series) for name, series in
              (("throughput", throughput), ("goodput", goodput), ("hit", hit_rate),
               ("retry", retry_ratio), ("pg", pg))}
    for t in marks:
        rows.append([
            f"{t:.0f}",
            k6csv.fmt(lookup["throughput"].get(t, float("nan"))),
            k6csv.fmt(lookup["goodput"].get(t, float("nan"))),
            k6csv.fmt(lookup["hit"].get(t, float("nan"))),
            k6csv.fmt(lookup["pg"].get(t, float("nan")), 0),
            k6csv.fmt(lookup["retry"].get(t, float("nan")), 2),
        ])
    print(k6csv.table(["t (s)", "throughput", "goodput", "hit %", "pg conns", "retry ratio"], rows))

    if goodput and throughput:
        worst = min(goodput, key=lambda p: p[1])
        print(f"Lowest goodput: {worst[1]:.1f} rps at t={worst[0]:.0f}s, "
              f"while throughput there was {dict(throughput).get(worst[0], float('nan')):.1f} rps.")
        print("The difference between those two numbers is work with no recipient.\n")

    print("Now write the three sentences the experiment asks for, in HotOS '25 vocabulary:")
    print("  trigger - what pushed it over, and note that it is already gone")
    print("  amplification mechanism - what turned one failure into more work")
    print("  sustaining effect - what keeps it there now that the trigger is not")

    png = k6csv.save_png(k6csv.png_path(path, "goodput"), "Goodput vs throughput",
                         {"throughput": throughput, "goodput": goodput},
                         "seconds", "requests/second")
    print(k6csv.note_png(png))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
