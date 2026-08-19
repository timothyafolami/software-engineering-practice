"""
7.2 -- watch the one file that answers the question, from inside the container.

WHAT THIS DEMONSTRATES
  `docker stats` and every average-utilisation dashboard are structurally
  incapable of showing throttling. A container frozen 40% of the time and
  running flat out for the other 60% reports the same average CPU as one
  loafing along evenly. The delta of `nr_throttled` between two samples is
  the number that separates them, and it takes ten seconds to read.

  This is the sampler to run alongside any of the language versions in
  this folder, or alongside a k6 run against the harness. It reports the
  per-interval ratio (what is happening NOW) separately from the
  cumulative one (which lags, because it averages over the container's
  whole life including the idle minutes before your load started).

  It also prints cpu.pressure, which cpu.stat cannot replace: PSI catches
  BOTH throttling and plain host contention, so a high pressure with a
  zero throttle ratio tells you the neighbours are the problem, not you.

WHAT TO LOOK FOR IN THE OUTPUT
  1. `thr/int` -- throttled periods in the last interval, out of the ~10
     periods a second contains at the default 100ms period. Sustained
     above 0.05 is worth explaining; above 0.10 it is almost certainly
     your latency story.
  2. `cpu%` next to it. The whole point of this topic is that these two
     columns move independently. A row with 30% CPU and a 0.3 ratio is
     the headline failure, and it is the row nobody's dashboard shows.
  3. `frozen ms/int` -- throttled_usec's delta. This is the number to
     compare against your p99: if the container was frozen for 300ms in a
     second, some request waited a chunk of that.

RUN
    python3 cpu_stat_watch.py                 # 1s samples, until Ctrl-C
    python3 cpu_stat_watch.py --interval 0.5 --seconds 30

  Run it INSIDE the container whose quota you care about. The harness image
  does not contain this file -- it is built from 00-harness/, which cannot
  reach into a sibling topic directory -- so copy it in first:
    cd ../00-harness
    docker compose cp ../02-throttled-at-30-percent-cpu/python/cpu_stat_watch.py api:/srv/
    docker compose exec api python3 /srv/cpu_stat_watch.py --interval 1 --seconds 30
  or from the harness against a service, which is what observe/watch.sh does
  in shell:
    ../../00-harness/observe/watch.sh api 60

  On macOS there is no /sys/fs/cgroup at all, so this prints a single
  honest refusal rather than a table of zeros. That is the correct answer
  on Darwin, not a failure.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# This file has to import the harness's `cgroup` and `openloop` modules from
# two places that do not look alike: the repository, where they sit at
# ../../00-harness/local/, and the inside of the api container, where the
# image puts them beside main.py in /srv. Resolving only the repository
# layout made the documented in-container invocation
# (`docker compose exec api python3 .../cpu_stat_watch.py`) die on an
# IndexError from parents[2] before it printed anything -- the file could
# not run in the one environment it exists to be run in.
_CANDIDATES = []
_here = Path(__file__).resolve()
if len(_here.parents) >= 3:
    _CANDIDATES.append(_here.parents[2] / "00-harness" / "local")
_CANDIDATES += [_here.parent, Path("/srv"), Path("/app")]
for _d in _CANDIDATES:
    if (_d / "cgroup.py").exists():
        sys.path.insert(0, str(_d))

import cgroup  # noqa: E402
from openloop import table  # noqa: E402


def pressure_some_avg10() -> str:
    """The 10-second 'some' average from cpu.pressure, or n/a.

    PSI's 'some' line is the share of time at least one task was stalled
    waiting for CPU. Unlike nr_throttled it does not care WHY, which is
    exactly why it belongs next to it: throttling and host contention are
    different problems with the same symptom.
    """
    raw = cgroup.pressure("cpu")
    if not raw:
        return "n/a"
    for line in raw.splitlines():
        if line.startswith("some"):
            for field in line.split():
                if field.startswith("avg10="):
                    return field.split("=", 1)[1]
    return "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between samples (default 1.0)")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this many seconds (default: run until Ctrl-C)")
    args = parser.parse_args()

    if not cgroup.have_cgroup_v2():
        print("cpu_stat_watch: there is no /sys/fs/cgroup on this host.")
        print(cgroup.environment_banner())
        print()
        print("  Nothing to sample. This is not a failure -- Darwin has no cgroups,")
        print("  so there is no quota being enforced and no cpu.stat to read.")
        print("  Run this INSIDE a Linux container:")
        print("    docker compose cp <this file> api:/srv/ && \\")
        print("      docker compose exec api python3 /srv/cpu_stat_watch.py")
        print("  For a userspace model of the same accounting rule on this host,")
        print("  run python/quota_freeze.py, which prints a FALLBACK banner and says so.")
        raise SystemExit(1)

    quota = cgroup.cpu_quota()
    period_us = cgroup.cpu_period_us() or 100_000
    print("7.2 -- cpu.stat, sampled")
    print(f"  cpu.max          {'no ceiling' if quota is None else f'{quota:.2f} CPU'}"
          f"   (period {period_us} us -> ~{1e6 / period_us:.0f} periods/sec)")
    print(f"  cpuset.effective {cgroup.cpuset_effective()}")
    print(f"  cpu.weight       {cgroup.cpu_weight()}")
    if quota is None:
        print()
        print("  NOTE: no quota is set on this cgroup, so nr_throttled cannot move.")
        print("        A flat zero below is the correct answer, not a broken sampler.")
    print()

    headers = ["time", "cpu%", "thr/int", "ratio", "frozen ms/int",
               "nr_throttled", "nr_periods", "psi some avg10"]
    rows: list[list[str]] = []
    # The live stream is the point of this program -- the summary table only
    # arrives once the run is over. Print the header for the live rows too,
    # or the eight unlabelled columns scrolling past are unreadable exactly
    # when you are trying to read them.
    print("  ".join(headers))
    print("  ".join("-" * len(h) for h in headers))

    previous = cgroup.cpu_stat() or {}
    started = time.monotonic()
    try:
        while True:
            time.sleep(args.interval)
            current = cgroup.cpu_stat() or {}
            d_usage = current.get("usage_usec", 0) - previous.get("usage_usec", 0)
            d_periods = current.get("nr_periods", 0) - previous.get("nr_periods", 0)
            d_throttled = current.get("nr_throttled", 0) - previous.get("nr_throttled", 0)
            d_frozen = current.get("throttled_usec", 0) - previous.get("throttled_usec", 0)

            # usage_usec is CPU time summed across every task in the cgroup, so
            # this percentage is "CPUs' worth", not a share of one core. 180%
            # under a 2.0-CPU quota means both CPUs busy, which is the number
            # you want next to a throttle ratio.
            cpu_pct = d_usage / (args.interval * 1e6) * 100.0
            interval_ratio = (d_throttled / d_periods) if d_periods else float("nan")
            cumulative = cgroup.throttle_ratio(current)

            row = [
                time.strftime("%H:%M:%S"),
                f"{cpu_pct:.0f}%",
                f"{d_throttled}/{d_periods}",
                f"{interval_ratio:.3f}" if d_periods else "n/a",
                f"{d_frozen / 1000.0:.0f}",
                str(current.get("nr_throttled", 0)),
                str(current.get("nr_periods", 0)),
                pressure_some_avg10(),
            ]
            rows.append(row)
            print("  ".join(row))

            previous = current
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        print()

    if rows:
        print()
        print(table(rows, headers))
        print()
        worst = max(rows, key=lambda r: float(r[3]) if r[3] != "n/a" else -1.0)
        print(f"  worst interval: {worst[0]}  ratio {worst[3]}  at {worst[1]} CPU")
        print("  If that ratio is well above zero while the CPU column looks calm,")
        print("  you have reproduced the headline failure of this topic. Compare it")
        print("  against your p99: the freezes are quantised to the period length,")
        print(f"  so the tail should cluster near a multiple of {period_us / 1000:.0f}ms.")


if __name__ == "__main__":
    main()
