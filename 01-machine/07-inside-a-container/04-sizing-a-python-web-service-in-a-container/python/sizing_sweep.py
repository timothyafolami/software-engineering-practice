"""
7.4 -- the worker sweep, and the arithmetic you should have done first.

WHAT THIS DEMONSTRATES
  Worker count is not a CPU decision. It is simultaneously a CPU-quota
  decision, a database-connection decision, a thread-pool decision and a
  memory decision, and the binding constraint is usually not the one you
  were tuning.

  This program has two halves, and the first one is the more valuable:

  1. THE ARITHMETIC (runs anywhere, needs nothing). Given a spec --
     replicas, workers, pool size, quota -- it computes all four ceilings
     and names which one binds. Every number is derived and shown, so you
     can do it on a napkin next time instead of running this. That is the
     actual skill: being handed `cpus: "2"`, 4 workers, pool 10, 3 replicas
     and saying what breaks before touching a terminal.

  2. THE MEASUREMENT (needs Docker). It sweeps uvicorn workers over
     {1,2,4,8} at a fixed cpus: "2.0", against /db and /cpu separately,
     and records p99, throughput, throttle ratio, RSS and the live backend
     count from pg_stat_activity -- read AT PEAK, because pools are lazy
     and a post-run reading shows connections that have already been
     returned.

  The two endpoints are the experiment. /cpu is bounded by quota, so its
  best worker count should be near floor(Q). /db spends most of its time
  waiting, so its best worker count should be higher -- until the
  connection math or the memory limit stops it. Predicting BOTH numbers,
  and being able to say why they differ, is the point.

WHAT TO LOOK FOR IN THE OUTPUT
  1. The ceiling table in part 1: four numbers, one of them smallest. The
     smallest is what actually limits you, and it is very often the
     connection count rather than the CPU.
  2. In part 2, /cpu throughput should stop improving at about floor(Q)
     workers while /db keeps improving past it. If /cpu throughput RISES
     from 4 to 8 workers under a 2.0 quota, something is not CPU-bound.
  3. PG connections rising linearly with workers while throughput does
     not. That is the shape of "we scaled up and it got slower".

RUN
    python3 sizing_sweep.py                    # the arithmetic only
    python3 sizing_sweep.py --measure          # the full sweep (needs Docker)
    python3 sizing_sweep.py --replicas 3 --pool 10 --overflow 10 --quota 2.0

  Part 1 runs on macOS and needs nothing at all -- it is arithmetic, not a
  measurement, and it is honest everywhere. Part 2 drives Compose from the
  host; the quota half of it does not exist on Darwin, so the containers do
  the work and this script only reads what they report. Give Docker
  Desktop's VM at least 4 CPUs or a 2.0-CPU quota is not a constraint.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "00-harness"
sys.path.insert(0, str(HARNESS / "local"))

from openloop import table  # noqa: E402

# Postgres reserves connections for superusers so an operator can still get
# in when the pool has eaten everything. That reservation is why the budget
# is 97 and not 100, and it is the difference between "degraded" and "you
# cannot log in to fix it".
SUPERUSER_RESERVED = 3


@dataclass
class Spec:
    replicas: int = 1
    workers: int = 4
    quota_cpus: float = 2.0
    pool_size: int = 10
    max_overflow: int = 10
    max_connections: int = 100
    anyio_tokens: int = 40
    mem_limit_mb: int = 1024
    rss_per_worker_mb: float = 90.0   # measured, not guessed -- see --measure
    db_rate: int = 120                # k6 arrival rate for /db
    # /cpu is offered ABOVE the quota's capacity on purpose. An open-loop
    # generator below capacity completes exactly what it offers, so a req/s
    # column taken below capacity reads the same for every worker count and
    # the "throughput stops improving at floor(quota) workers" claim has
    # nothing to show. 2.0 CPU on a ~15ms handler is ~130 req/s of capacity;
    # 200 is comfortably past it. Delivered as bursts of BURST so k6 needs
    # tens of VUs rather than hundreds -- a load generator that needs more
    # RAM than the service is its own experiment.
    cpu_rate: int = 200               # k6 arrival rate for /cpu
    cpu_burst: int = 10


# --------------------------------------------------------------- ceiling 1

def quota_ceiling(spec: Spec) -> tuple[float, str]:
    """CPU-bound throughput ceiling, and the worker count that reaches it.

    A CPU-saturated worker consumes one CPU. Q CPUs of quota therefore
    support floor(Q) of them continuously; every worker beyond that buys no
    throughput -- the quota was always the ceiling -- and costs tail
    latency, because more runnable threads drain the bucket faster. That
    is 7.2, measured rather than asserted.
    """
    best = max(1, math.floor(spec.quota_cpus))
    if spec.workers <= best:
        note = f"{spec.workers} workers <= floor({spec.quota_cpus:.1f}) = {best}: not throttled by count"
    else:
        note = (f"{spec.workers} workers > floor({spec.quota_cpus:.1f}) = {best}: "
                f"the extra {spec.workers - best} buy nothing and cost p99")
    return spec.quota_cpus, note


# --------------------------------------------------------------- ceiling 2

def connection_ceiling(spec: Spec) -> tuple[int, int, str]:
    """The arithmetic that gets skipped.

        replicas x workers x (pool_size + max_overflow) <= max_connections - reserved

    Nothing in Docker, Kubernetes, uvicorn, SQLAlchemy or FastAPI computes
    this product for you or warns when it exceeds a limit. Each backend is
    a real Postgres process with real memory, so blowing through it does
    not only mean "connection refused" -- it means the database slows down
    for everyone else first. That is precisely how scaling up makes things
    slower.
    """
    per_worker = spec.pool_size + spec.max_overflow
    worst_case = spec.replicas * spec.workers * per_worker
    budget = spec.max_connections - SUPERUSER_RESERVED
    if worst_case <= budget:
        note = f"{worst_case} <= {budget}: fits, with {budget - worst_case} spare"
    else:
        note = (f"{worst_case} > {budget}: OVER BUDGET by {worst_case - budget}. "
                "Postgres refuses the excess, and slows down before it does")
    return worst_case, budget, note


# --------------------------------------------------------------- ceiling 3

def thread_ceiling(spec: Spec) -> tuple[int, str]:
    """FastAPI runs plain `def` endpoints on an anyio thread pool whose
    default limiter holds 40 tokens PER PROCESS. Request 41 waits for a
    token -- no log line, no metric, no exception, the latency simply
    appears somewhere you are not looking.

    The limit being per-process is the part people get wrong: WORKERS=4
    means 160 tokens across the container, all drawing on one CPU bucket.
    7.5 is that ceiling under load.
    """
    total = spec.workers * spec.anyio_tokens
    return total, (f"{spec.anyio_tokens} tokens x {spec.workers} workers = {total} "
                   "concurrent sync handlers before requests queue silently")


# --------------------------------------------------------------- ceiling 4

def memory_ceiling(spec: Spec) -> tuple[float, str]:
    """Every worker is a separate interpreter: its own bytecode, its own
    imported module objects, its own connection pool, its own copy of any
    in-process cache. Copy-on-write after fork() shares some of that at
    first and then un-shares it as refcounts get written into the very
    pages the objects live in -- so RSS measured ten seconds after startup
    is an underestimate of the steady state, and memory.max does not
    negotiate (7.6).
    """
    projected = spec.workers * spec.rss_per_worker_mb
    if projected <= spec.mem_limit_mb:
        note = (f"{projected:.0f} MiB of {spec.mem_limit_mb} MiB "
                f"({projected / spec.mem_limit_mb * 100:.0f}%)")
    else:
        note = (f"{projected:.0f} MiB projected vs {spec.mem_limit_mb} MiB limit: "
                "OOM-kill territory, exit 137, no traceback")
    return projected, note


def analyse(spec: Spec) -> None:
    """Part 1: the four ceilings, and which one actually binds."""
    print("  the spec under analysis:")
    print(f"    replicas          {spec.replicas}")
    print(f"    workers/replica   {spec.workers}")
    print(f"    cpus              {spec.quota_cpus:.1f}")
    print(f"    pool_size         {spec.pool_size} (+ max_overflow {spec.max_overflow})")
    print(f"    max_connections   {spec.max_connections} (- {SUPERUSER_RESERVED} reserved)")
    print(f"    anyio tokens      {spec.anyio_tokens} per process")
    print(f"    mem_limit         {spec.mem_limit_mb} MiB")
    print(f"    RSS per worker    {spec.rss_per_worker_mb:.0f} MiB (assumed; --measure fills this in)")
    print()

    _, quota_note = quota_ceiling(spec)
    conns, budget, conn_note = connection_ceiling(spec)
    threads, thread_note = thread_ceiling(spec)
    rss, mem_note = memory_ceiling(spec)

    rows = [
        ["1 quota", f"{spec.quota_cpus:.1f} CPU", quota_note,
         "OK" if spec.workers <= max(1, math.floor(spec.quota_cpus)) else "TAIL LATENCY"],
        ["2 connections", f"{conns} backends", conn_note,
         "OK" if conns <= budget else "BREACH"],
        ["3 threads", f"{threads} tokens", thread_note, "info"],
        ["4 memory", f"{rss:.0f} MiB", mem_note,
         "OK" if rss <= spec.mem_limit_mb else "BREACH"],
    ]
    print(table(rows, ["ceiling", "value", "the arithmetic", "verdict"]))
    print()

    breaches = [row[0] for row in rows if row[3] == "BREACH"]
    if breaches:
        print(f"  BINDING CONSTRAINT: {', '.join(breaches)}")
        print("  Note that this is not the CPU. Every instinct says 'add workers' or")
        print("  'raise the CPU limit', and neither would move the number that broke.")
    elif spec.workers > max(1, math.floor(spec.quota_cpus)):
        print("  Nothing breaches, but the worker count exceeds the quota: you are")
        print("  paying tail latency (7.2) for throughput you cannot have, because")
        print("  the quota was always the ceiling.")
    else:
        print("  All four fit. Now say what you gave up to make them fit -- that is")
        print("  the other half of the exercise.")
    print()

    # What would have to change, in the order a sane person would change it.
    if conns > budget:
        max_workers = budget // max(1, spec.replicas * (spec.pool_size + spec.max_overflow))
        max_pool = budget // max(1, spec.replicas * spec.workers) - spec.max_overflow
        print("  To fit the connection budget, pick one:")
        print(f"    * workers <= {max_workers} at this pool size")
        print(f"    * pool_size <= {max(0, max_pool)} at this worker count")
        print("    * pgbouncer in transaction mode, which decouples the two numbers")
        print("      entirely -- at the cost of anything that assumes a session lives")
        print("      across transactions: session-level SET, advisory locks, temp")
        print("      tables, prepared statements. Know which of those you use.")
        print()

    print("  The opinionated default for an IO-bound FastAPI service at a 2-CPU")
    print("  quota: 2 uvicorn workers, an async driver, pool size 5-10 per worker,")
    print("  and a connection cap you can compute on paper. Prefer one process per")
    print("  container and let the orchestrator replicate.")


# ------------------------------------------------------------ part 2: docker

def docker_available() -> bool:
    return shutil.which("docker") is not None and \
        subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def compose(*args: str, capture: bool = True, env: dict | None = None) -> str:
    import os

    merged = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["docker", "compose", *args], cwd=HARNESS, capture_output=capture,
        text=True, env=merged,
    )
    return (result.stdout or "") + (result.stderr or "")


def backend_count() -> int | None:
    """Live Postgres backends for this database, read from pg_stat_activity.

    Read this DURING the run. Pools are lazy: a reading taken afterwards
    shows connections that have already been returned or closed, which is
    how people conclude their pool math is fine when it is not.
    """
    out = compose("exec", "-T", "db", "psql", "-U", "lab", "-d", "container_lab",
                  "-tAc", "select count(*) from pg_stat_activity "
                          "where datname='container_lab';")
    match = re.search(r"^\s*(\d+)\s*$", out, re.MULTILINE)
    return int(match.group(1)) if match else None


def container_rss_mb() -> float | None:
    out = compose("ps", "-q", "api").strip().splitlines()
    if not out:
        return None
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", out[0]],
        capture_output=True, text=True)
    match = re.match(r"([\d.]+)([KMG]i?B)", stats.stdout.strip())
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    factor = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024,
              "KB": 1 / 1024, "MB": 1, "GB": 1024}.get(unit, 1)
    return value * factor


def cpu_stat() -> dict[str, int]:
    out = compose("exec", "-T", "api", "cat", "/sys/fs/cgroup/cpu.stat")
    stat: dict[str, int] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        try:
            stat[key] = int(value)
        except ValueError:
            continue
    return stat


def run_k6(endpoint: str, rate: int, duration: str, burst: int = 1) -> dict:
    """One k6 run. Returns the percentiles and the drop count.

    dropped_iterations is not optional reading. If it is not near zero, k6
    ran out of VUs and every latency below it is understated -- you would
    be measuring the load generator's own container.
    """
    # --no-deps is load-bearing. k6 `depends_on: api`, so without it
    # `compose run` re-creates the api container -- with the harness DEFAULTS,
    # not this row's WORKERS -- immediately before the measurement. The sweep
    # then measures one worker count four times and reports it as four.
    out = compose("--profile", "load", "run", "--rm", "--no-deps",
                  "-e", f"ENDPOINT={endpoint}", "-e", f"RATE={rate}",
                  "-e", f"DURATION={duration}", "-e", f"BURST={burst}",
                  "k6", "run", "/scripts/steady.js")

    def find(pattern: str) -> float:
        # MULTILINE, because every pattern below anchors on a line start and
        # the summary is one long multi-line blob. Without it `^p50` only ever
        # matches at offset 0 and every column comes back NaN -- silently, in a
        # table that otherwise looks finished.
        match = re.search(pattern, out, re.MULTILINE)
        return float(match.group(1)) if match else float("nan")

    # steady.js defines handleSummary, which REPLACES k6's built-in end-of-test
    # summary rather than adding to it -- so `med=...ms` and `p(99)=...ms` are
    # not in this output at all and every column came back NaN. Parse the
    # summary the script actually prints, and fall back to k6's default shape
    # for anyone running an older copy of steady.js.
    p50 = find(r"^p50\s+([\d.]+) ms")
    if p50 != p50:  # NaN
        p50 = find(r"med=([\d.]+)ms")
    p99 = find(r"^p99\s+([\d.]+) ms")
    if p99 != p99:
        p99 = find(r"p\(99\)=([\d.]+)ms")
    completed = find(r"^completed\s+(\d+)")
    seconds = float(re.sub(r"[^\d.]", "", duration) or "0") or float("nan")
    rps = completed / seconds if completed == completed else find(r"http_reqs.*?([\d.]+)/s")
    dropped = find(r"^dropped_iterations\s+(\d+)")
    if dropped != dropped:
        dropped = find(r"dropped_iterations.*?(\d+)")

    return {"p50": p50, "p99": p99, "rps": rps, "dropped": dropped, "raw": out}


PINNED_ROUNDS: int | None = None


def wait_for_api(timeout_s: float = 60.0) -> None:
    """Wait for the port, then warm the workers.

    A fixed `time.sleep(5)` is not enough: main.py calibrates /cpu at import,
    once per uvicorn worker, and at WORKERS=8 inside a 2.0-CPU cgroup that
    calibration is still finishing when the measurement starts. The leftover
    warm-up lands in the first seconds of the run as a tail that belongs to
    process startup rather than to the worker count under test.
    """
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen("http://localhost:8000/healthz", timeout=2).read()
            break
        except Exception:
            time.sleep(1)
    for _ in range(40):
        try:
            urllib.request.urlopen("http://localhost:8000/cpu", timeout=15).read()
        except Exception:
            pass


def pin_cpu_rounds() -> int | None:
    """Hold /cpu's cost fixed across the sweep.

    main.py calibrates the handler to ~15ms at startup, per worker. Eight
    workers calibrating simultaneously in one cgroup each measure a cost
    inflated by the other seven and settle on a CHEAPER handler than one
    worker does -- so an unpinned sweep varies the workload and the worker
    count together and cannot attribute the result to either.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:8000/stat", timeout=5) as fh:
            return int(json.load(fh)["cpu_rounds"])
    except Exception:
        return None


def measure(spec: Spec, worker_counts: list[int], duration: str) -> None:
    """Part 2: the sweep. One variable moves -- the worker count."""
    if not docker_available():
        print("  docker daemon is not running, so part 2 cannot run.")
        print()
        print("  There is no host-side substitute: the quota half of this experiment")
        print("  does not exist on Darwin, and the connection half needs the harness's")
        print("  Postgres. Start Docker Desktop and re-run with --measure.")
        print()
        print("  Part 1 above is not a consolation prize -- it is the half you should")
        print("  be able to do without a terminal at all.")
        return

    global PINNED_ROUNDS
    compose("up", "-d", "--force-recreate", "api",
            env={"WORKERS": "1", "API_CPUS": f"{spec.quota_cpus}"})
    wait_for_api()
    PINNED_ROUNDS = pin_cpu_rounds()
    if PINNED_ROUNDS:
        print(f"  /cpu pinned to {PINNED_ROUNDS} hash rounds for every row below")

    print("  sweeping WORKERS over", worker_counts, f"at cpus: {spec.quota_cpus}")
    print(f"  /db at {spec.db_rate} req/s, /cpu at {spec.cpu_rate} req/s "
          f"(in bursts of {spec.cpu_burst}, above capacity on purpose), {duration} each")
    print()

    rows = []
    for workers in worker_counts:
        env = {"WORKERS": str(workers), "API_CPUS": f"{spec.quota_cpus}",
               "POOL_MAX": str(spec.pool_size)}
        # Recreate, never restart. A restart reuses the old cgroup, and you
        # would measure the previous config while believing you changed it.
        if PINNED_ROUNDS:
            env["CPU_ROUNDS"] = str(PINNED_ROUNDS)
        compose("up", "-d", "--force-recreate", "api", env=env)
        wait_for_api()

        enforced = compose("exec", "-T", "api", "cat", "/sys/fs/cgroup/cpu.max").strip()
        expected = f"{int(spec.quota_cpus * 100000)} 100000"
        if enforced != expected:
            print(f"  BROKEN: cpu.max is {enforced!r}, expected {expected!r}.")
            print("  Compose did not apply the limit. Every number below would be")
            print("  meaningless, so this stops here rather than producing them.")
            return

        before = cpu_stat()
        db_result = run_k6("/db", spec.db_rate, duration)
        peak_conns = backend_count()          # DURING the run's tail, not after
        cpu_result = run_k6("/cpu", spec.cpu_rate, duration, burst=spec.cpu_burst)
        after = cpu_stat()
        rss = container_rss_mb()

        d_periods = after.get("nr_periods", 0) - before.get("nr_periods", 0)
        d_throttled = after.get("nr_throttled", 0) - before.get("nr_throttled", 0)
        ratio = d_throttled / d_periods if d_periods else float("nan")

        rows.append([
            str(workers),
            f"{db_result['p99']:.0f}", f"{db_result['rps']:.0f}",
            f"{cpu_result['p99']:.0f}", f"{cpu_result['rps']:.0f}",
            f"{ratio:.3f}",
            "?" if peak_conns is None else str(peak_conns),
            "?" if rss is None else f"{rss:.0f}",
            f"{db_result['dropped']:.0f}/{cpu_result['dropped']:.0f}",
        ])
        print(f"  ran: WORKERS={workers}")

    print()
    print(table(rows, ["workers", "/db p99", "/db req/s", "/cpu p99", "/cpu req/s",
                       "throttle", "PG conns", "RSS MiB", "dropped db/cpu"]))
    print()
    print("  /db req/s will read back the offered rate: an open-loop generator")
    print("  below capacity completes what it offers, for every worker count.")
    print("  The /db story is in the PG conns and p99 columns, not in req/s.")
    print()
    print("  Read the two req/s columns against each other. /cpu should stop")
    print(f"  improving at about floor({spec.quota_cpus:.1f}) = "
          f"{max(1, math.floor(spec.quota_cpus))} workers, because the quota was")
    print("  always its ceiling. /db should keep improving past that, because a")
    print("  worker waiting on a socket is not spending quota -- until the")
    print("  connection count or the memory limit stops it.")
    print()
    print("  The /cpu leg is deliberately offered ABOVE capacity, so its dropped")
    print("  count is SUPPOSED to be nonzero -- that is the capacity signal, not a")
    print("  broken load generator. Its p99 is queue depth rather than service")
    print("  time and is not worth reading; read the req/s column. The /db leg is")
    print("  offered below capacity, so a nonzero drop count THERE does mean k6")
    print("  ran out of VUs and every /db latency above it is understated.")
    print()
    print("  Now feed the measured RSS back into part 1:")
    print(f"    python3 sizing_sweep.py --rss-per-worker <the RSS/workers you just saw>")


def main() -> None:
    parser = argparse.ArgumentParser(description="7.4 -- four ceilings, one worker count")
    parser.add_argument("--measure", action="store_true",
                        help="run the Docker sweep as well as the arithmetic")
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--quota", type=float, default=2.0)
    parser.add_argument("--pool", type=int, default=10)
    parser.add_argument("--overflow", type=int, default=10)
    parser.add_argument("--max-connections", type=int, default=100)
    parser.add_argument("--anyio-tokens", type=int, default=40)
    parser.add_argument("--mem-limit-mb", type=int, default=1024)
    parser.add_argument("--rss-per-worker", type=float, default=90.0)
    parser.add_argument("--db-rate", type=int, default=Spec.db_rate)
    # Keep this in step with Spec.cpu_rate. It was 60 while the dataclass said
    # 200, and argparse wins -- so the "offered above capacity" comment in Spec
    # described a load that was never actually sent.
    parser.add_argument("--cpu-rate", type=int, default=Spec.cpu_rate)
    parser.add_argument("--cpu-burst", type=int, default=Spec.cpu_burst)
    parser.add_argument("--duration", default="30s")
    args = parser.parse_args()

    spec = Spec(replicas=args.replicas, workers=args.workers, quota_cpus=args.quota,
                pool_size=args.pool, max_overflow=args.overflow,
                max_connections=args.max_connections, anyio_tokens=args.anyio_tokens,
                mem_limit_mb=args.mem_limit_mb, rss_per_worker_mb=args.rss_per_worker,
                db_rate=args.db_rate, cpu_rate=args.cpu_rate,
                cpu_burst=args.cpu_burst)

    print("7.4 -- sizing a Python web service in a container")
    print()
    print("=== part 1: the arithmetic (needs nothing, and is the half that matters) ===")
    print()
    analyse(spec)
    print()

    print("=== part 2: the measurement ===")
    print()
    if args.measure:
        measure(spec, [1, 2, 4, 8], args.duration)
    else:
        print("  not run. Pass --measure to sweep {1,2,4,8} workers against the")
        print("  harness at a fixed quota. It needs a running Docker daemon and")
        print("  takes several minutes -- part 1 is the part you should internalise")
        print("  anyway, because it is the one you can do in a design review.")


if __name__ == "__main__":
    main()
