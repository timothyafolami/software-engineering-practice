"""
Layer 5 · Topic 6 - plot the fan-out tail against K, and open against closed loop.

WHAT THIS DEMONSTRATES
  Reads every `06_fanout_*.csv` and `06_closed_loop_*.csv` in a directory and
  produces two comparisons:

  1. End-to-end p50 and p99 against K, with the predicted tail probability
     1 - (1-p)^K beside the measured one. p is taken from the K=1 run, which
     is the only run where a backend's tail and the request's tail are the
     same thing - so the prediction is built from this experiment's own
     measurement rather than from the number in the table.

  2. The same K, the same nominal load, open model against closed. This is
     the coordinated-omission chart, and it is the most useful thing in this
     layer for arguing with people.

WHAT TO LOOK FOR IN THE OUTPUT
  p99 rising with K while nothing about any backend changed. And the closed
  loop reporting a much better p99 from many fewer requests: the requests it
  did not send are precisely the ones that would have been slow, because it
  was busy waiting on the slow one.

  With hedging on, read the backend load figure printed by the k6 run itself
  next to the p99 improvement here. Hedging buys tail latency with capacity,
  and the trade is only defensible if you know the exchange rate.

RUN
    python3 tools/plot_tail.py out/

  Without k6, check the plotter against the synthetic fixtures:
    python3 tools/make_fixtures.py
    python3 tools/plot_tail.py out/fixtures/
"""
from __future__ import annotations

import os
import re
import sys

import k6csv

NAME_RE = re.compile(r"06_(fanout|closed_loop)_k(\d+)(?:_hedge(on|off))?")


def parse_name(path: str) -> tuple[str, int, str] | None:
    match = NAME_RE.search(os.path.basename(path))
    if not match:
        return None
    kind, k, hedge = match.group(1), int(match.group(2)), match.group(3) or "off"
    return ("closed" if kind == "closed_loop" else "open", k, hedge)


def main(argv: list[str]) -> int:
    directory = argv[1] if len(argv) > 1 else "out/"
    paths = k6csv.find_csvs(directory, "06_")

    runs = []
    synthetic = False
    for path in paths:
        parsed = parse_name(path)
        if parsed is None:
            continue
        model, k, hedge = parsed
        synthetic = synthetic or k6csv.is_synthetic(path)
        samples = k6csv.load(path)
        durations = k6csv.values_of(samples, "http_req_duration")
        if not durations:
            continue
        window = k6csv.timespan([s for s in samples if s.metric == "http_req_duration"]) or 1.0
        runs.append({
            "path": path, "model": model, "k": k, "hedge": hedge,
            "n": len(durations), "rps": len(durations) / window,
            "p50": k6csv.percentile(durations, 50),
            "p99": k6csv.percentile(durations, 99),
            "p999": k6csv.percentile(durations, 99.9),
            "durations": durations,
            "backend": k6csv.values_of(samples, "slowest_backend_ms"),
        })

    if not runs:
        raise SystemExit(f"no recognisable 06_fanout_k*/06_closed_loop_k* files in {directory}")
    if synthetic:
        print(k6csv.SYNTHETIC_WARNING)

    open_runs = sorted([r for r in runs if r["model"] == "open" and r["hedge"] == "off"],
                       key=lambda r: r["k"])
    hedged = sorted([r for r in runs if r["model"] == "open" and r["hedge"] == "on"],
                    key=lambda r: r["k"])
    closed = sorted([r for r in runs if r["model"] == "closed"], key=lambda r: r["k"])

    # p is measured at K=1, where the request tail IS the backend tail. Without
    # a K=1 run there is nothing honest to predict from, and the column stays
    # blank rather than being filled from the theory it is meant to test.
    baseline = next((r for r in open_runs if r["k"] == 1), None)
    threshold = baseline["p99"] if baseline else None

    print(f"\nLayer 5 / topic 6 - fan-out tail, from {directory}\n")
    if threshold is None:
        print("No K=1 run found, so `predicted` and `measured tail` are left blank.")
        print("They are only meaningful against a single-backend baseline.\n")

    rows = []
    for run in open_runs + hedged:
        if threshold is not None:
            predicted = 100.0 * (1 - (1 - 0.01) ** run["k"])
            measured = 100.0 * sum(1 for d in run["durations"] if d > threshold) / run["n"]
            predicted_s, measured_s = k6csv.fmt(predicted, 2), k6csv.fmt(measured, 2)
        else:
            predicted_s = measured_s = "-"
        rows.append([
            f"K={run['k']}", run["hedge"], str(run["n"]), k6csv.fmt(run["rps"]),
            k6csv.fmt(run["p50"]), k6csv.fmt(run["p99"]), k6csv.fmt(run["p999"]),
            predicted_s, measured_s,
        ])
    print(k6csv.table(
        ["run", "hedge", "requests", "achieved rps", "e2e p50", "e2e p99", "e2e p99.9",
         "predicted tail %", "measured tail %"], rows))
    if threshold is not None:
        print(f"`tail` here means slower than {threshold:.1f}ms, which is the K=1 run's own p99.")
        print("`predicted` assumes p=1% per backend, independent; the gap between the two")
        print("columns is how independent your backends actually are.\n")

    series = {
        "p99 (open)": [(r["k"], r["p99"]) for r in open_runs],
        "p50 (open)": [(r["k"], r["p50"]) for r in open_runs],
    }
    if hedged:
        series["p99 (hedged)"] = [(r["k"], r["p99"]) for r in hedged]
    print(k6csv.xy_plot(series, xlabel="K (backends fanned out to, all awaited)",
                        ylabel="end-to-end latency ms", logy=True, height=16))

    if closed:
        print("\nCoordinated omission - the same K, the same nominal load, two load models:\n")
        co_rows = []
        for run in closed:
            twin = next((r for r in open_runs if r["k"] == run["k"]), None)
            co_rows.append(["closed (ramping-vus)", f"K={run['k']}", str(run["n"]),
                            k6csv.fmt(run["rps"]), k6csv.fmt(run["p50"]), k6csv.fmt(run["p99"])])
            if twin:
                co_rows.append(["open (arrival-rate)", f"K={twin['k']}", str(twin["n"]),
                                k6csv.fmt(twin["rps"]), k6csv.fmt(twin["p50"]),
                                k6csv.fmt(twin["p99"])])
        print(k6csv.table(["model", "K", "requests", "achieved rps", "p50", "p99"], co_rows))
        # Say what is in THIS data, not what the effect usually looks like.
        for run in closed:
            twin = next((r for r in open_runs if r["k"] == run["k"]), None)
            if not twin:
                continue
            fewer = run["n"] < twin["n"]
            flattering = run["p99"] < twin["p99"]
            print(f"K={run['k']}: the closed loop sent "
                  f"{'fewer' if fewer else 'more'} requests ({run['n']} vs {twin['n']}) and "
                  f"reported a p99 that is {'lower' if flattering else 'higher'} "
                  f"({run['p99']:.1f}ms vs {twin['p99']:.1f}ms).")
            if fewer and flattering:
                print("  Those two facts are the same fact: it stopped sending while the server")
                print("  was slow, so the slow period is missing from its histogram entirely.")
            elif not flattering:
                print("  No omission visible here. Either the server never slowed enough for the")
                print("  generator to fall behind, or the run was too short for it to matter -")
                print("  check the achieved rps column before concluding anything either way.")
        print()
    else:
        print("\nNo closed-loop run found. Produce one - it is the comparison this topic")
        print("exists for:  docker compose run --rm k6 run /scripts/06_closed_loop.js -e K=10\n")

    png = k6csv.save_png(k6csv.png_path(directory, "tail"), "Fan-out tail vs K",
                         series, "K", "latency ms", logy=True)
    print(k6csv.note_png(png))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
