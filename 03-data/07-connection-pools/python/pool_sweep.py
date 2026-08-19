"""
Sweeping pool_size past the knee, and watching the queue move into the database.

    python3 07-connection-pools/python/pool_sweep.py

WHAT IT DEMONSTRATES: the same offered load -- a fixed arrival rate, above
capacity -- against pool sizes from 2 to 100, with everything else held still.
Past a certain size, throughput stops improving and p99 keeps climbing. That is
the knee, and it is the whole topic.

WHY IT HAPPENS: your pool is a queue, and so is the database. A small pool
queues requests IN YOUR PROCESS, where you can see the wait, bound it with
pool_timeout, and shed load if you choose to. A large pool lets every request
through to Postgres, which then has more runnable backends than cores and
time-slices between them -- so every request gets slower together, and the
queueing is now inside a system that will not tell you it is queueing.

  Little's Law: required concurrency = arrival rate x mean service time.
  This program measures the service time first and prints the arithmetic, so
  the "right" pool size is a number you derive before you look at the sweep.

WHAT TO LOOK FOR:
  * the row where req/s stops rising. Compare it to the Little's Law figure
    printed above the table.
  * p99 in the rows below that one. It keeps going up while throughput does not.
  * the last column: mean concurrent backends by state, sampled from
    pg_stat_activity DURING the run. `active` climbing while req/s is flat is
    the queue migrating from your process into the database.

Knobs: POOL_SIZES, ARRIVAL_RATE (default: about 2.5x measured capacity),
DURATION_S, MAX_OVERFLOW, POOL_TIMEOUT.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_lab  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

POOL_SIZES = [int(x) for x in os.environ.get("POOL_SIZES", "2,5,10,25,50,100").split(",")]
DURATION_S = float(os.environ.get("DURATION_S", "6"))
MAX_OVERFLOW = int(os.environ.get("MAX_OVERFLOW", "0"))
POOL_TIMEOUT = float(os.environ.get("POOL_TIMEOUT", "2"))


def handler(engine, scheduled_at, result) -> None:
    try:
        with engine.connect() as conn:
            pool_lab.do_work(conn)
        result.ok(scheduled_at)
    except Exception as exc:  # noqa: BLE001 - classified, then counted
        result.fail(pool_lab.classify(exc), scheduled_at)


def run_one(pool_size: int, rate: float) -> dict:
    engine = pool_lab.make_engine(pool_size, MAX_OVERFLOW, POOL_TIMEOUT,
                                  app_name=f"sep-pool-{pool_size}")
    result = pool_lab.Result()
    samples: list = []
    stop = threading.Event()
    sampler = threading.Thread(target=pool_lab.sample_activity, args=(stop, samples), daemon=True)
    sampler.start()
    try:
        elapsed = pool_lab.open_loop(engine, handler, rate, DURATION_S, result)
    finally:
        stop.set()
        sampler.join(2)
        engine.dispose()
    out = result.summary(elapsed)
    out["activity"] = pool_lab.activity_line(pool_lab.mean_activity(samples))
    out["pool_size"] = pool_size
    return out


def main() -> None:
    pool_lab.prepare()
    lab_db.banner("Pool sweep -- finding the knee")

    probe = pool_lab.make_engine(1, 0, 10, app_name="sep-probe")
    service_ms = pool_lab.measure_service_time(probe)
    probe.dispose()

    with lab_db.connect() as conn:
        max_conn = conn.execute("SHOW max_connections").fetchone()[0]
        cores = conn.execute("SELECT current_setting('max_parallel_workers')").fetchone()[0]

    # Capacity is not 1/service_time: Postgres runs one backend per core, so the
    # ceiling is roughly cores / service_time. Sizing the offered load off a
    # single-core figure would leave the database idle and the sweep flat --
    # which is exactly the "broken experiment" this topic warns about.
    cpus = os.cpu_count() or 4
    capacity = cpus * 1000.0 / service_ms if service_ms else 0.0
    rate = float(os.environ.get("ARRIVAL_RATE", f"{max(20.0, capacity * 2.5):.0f}"))

    print(f"  one request, uncontended, takes {service_ms:.1f} ms of database time.")
    print(f"  this machine has {cpus} cores, so Postgres can serve roughly "
          f"{capacity:.0f} req/s of it.")
    print(f"  Little's Law, at an arrival rate of {rate:.0f} req/s:")
    print(f"      required concurrency = {rate:.0f} x {service_ms / 1000:.4f}s"
          f" = {rate * service_ms / 1000:.1f} connections")
    print(f"  That is the pool size to beat. Anything larger is buying queueing, not")
    print(f"  throughput -- the sweep below is the check.")
    print(f"\n  server max_connections = {max_conn}; this machine reports "
          f"max_parallel_workers = {cores}")
    print(f"  offered load: {rate:.0f} req/s for {DURATION_S:.0f}s per pool size, OPEN loop")
    print(f"  (max_overflow = {MAX_OVERFLOW}, pool_timeout = {POOL_TIMEOUT}s)")

    print(f"\n  {'pool_size':>10}{'req/s':>9}{'p50 ms':>10}{'p99 ms':>10}"
          f"{'errors':>9}  {'error kinds':<28}{'active/idle-tx/idle':>21}")
    print("  " + "-" * 98)
    rows = []
    for pool_size in POOL_SIZES:
        r = run_one(pool_size, rate)
        rows.append(r)
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(r["errors"].items())) or "-"
        print(f"  {pool_size:>10}{r['rate']:>9.0f}{r['p50']:>10.0f}{r['p99']:>10.0f}"
              f"{r['error_count']:>9}  {kinds[:26]:<28}{r['activity']:>21}")
        if len(kinds) > 26:
            # Truncating this column would hide the finding: 'server: too many
            # clients' is a different incident from a pool timeout, and it is
            # the one that arrives at the largest pool sizes.
            print(f"{'':>10}{'':>9}{'':>10}{'':>10}{'':>9}  full: {kinds}")

    # The knee is the SMALLEST pool that reaches (within 3%) the best throughput
    # seen. Taking the literal maximum would pick whichever large pool happened
    # to win by noise, and the point of this experiment is that those pools are
    # not buying anything.
    peak = max(r["rate"] for r in rows)
    best = next(r for r in rows if r["rate"] >= peak * 0.97)
    print(f"\n  peak throughput {peak:.0f} req/s is first reached at pool_size = "
          f"{best['pool_size']} ({best['rate']:.0f} req/s, p99 {best['p99']:.0f}ms)")
    beyond = [r for r in rows if r["pool_size"] > best["pool_size"]]
    if beyond:
        worst = max(beyond, key=lambda r: r["p99"])
        gain = (worst["rate"] - best["rate"]) / max(best["rate"], 1e-9) * 100
        print(f"  at pool_size = {worst['pool_size']}: throughput {gain:+.0f}%, "
              f"p99 {best['p99']:.0f}ms -> {worst['p99']:.0f}ms")
        print("  More connections past the knee bought p99 and nothing else. The extra")
        print("  requests are not being served faster -- they are queueing inside Postgres,")
        print("  which has no pool_timeout to give you and no queue depth to alert on.")
    print()
    print("  Two things worth writing down from this run:")
    print(f"    * the arithmetic: {rate:.0f} req/s x {service_ms:.0f}ms = "
          f"{rate * service_ms / 1000:.0f} connections is what the work needs.")
    print("    * total possible connections = replicas x workers x (pool_size + max_overflow).")
    print(f"      At pool_size={POOL_SIZES[-1]}, four workers and ten replicas, that is "
          f"{POOL_SIZES[-1] * 4 * 10:,} connections")
    print(f"      against a server whose max_connections is {max_conn}. The failure there is")
    print("      not a database outage; it is an arithmetic error committed in a YAML file.")
    print()
    print("  If throughput rose all the way to the largest pool size, you did not saturate")
    print("  Postgres: raise ARRIVAL_RATE, or give the database fewer cores. The knee is")
    print("  real, but it only exists above capacity.")


if __name__ == "__main__":
    main()
