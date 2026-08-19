"""
The migration that took the site down, reproduced in twelve seconds.

    python3 05-locking-and-deadlocks/python/migration_lock_queue.py

WHAT IT DEMONSTRATES: an `ALTER TABLE orders ADD COLUMN notes text` -- a
millisecond of work, one of the safest DDL statements there is -- stopping every
read of `orders` dead, without ever running.

The mechanism is the LOCK QUEUE, and it is the part the compatibility matrix
does not tell you. `SELECT` takes ACCESS SHARE. `ALTER TABLE` wants ACCESS
EXCLUSIVE, which conflicts with everything. So:

  1. a long-running SELECT holds ACCESS SHARE on orders;
  2. the ALTER asks for ACCESS EXCLUSIVE and has to WAIT;
  3. every SELECT arriving after it -- each one perfectly compatible with the
     lock currently held -- queues BEHIND the waiting ALTER, because Postgres
     grants locks in roughly request order and will not let later weak requests
     jump a pending strong one.

Traffic stops within a second, and the statement everybody will blame ran for a
millisecond, hours later, once it finally got its lock.

WHAT TO LOOK FOR:
  * the reads-per-250ms timeline going to zero, and how quickly;
  * the lock queue sampled live: ONE ungranted ACCESS EXCLUSIVE row, and a
    growing pile of ungranted ACCESS SHARE rows behind it. `granted` is the
    whole story;
  * phase 2, with `SET lock_timeout = '3s'` before the ALTER: the migration
    fails harmlessly instead of taking the site down, and the stall is bounded
    by a number you chose. That is the entire trade, and it is why the fix is
    `lock_timeout` rather than "run migrations at night".

Run `sql/lock_queue.sql` in a psql session while this is mid-flight. Watching
the queue build in another terminal is the part that makes it stick.

Knobs: READERS, HOLD_S (how long the blocking transaction holds its lock),
LOCK_TIMEOUT.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402
import psycopg  # noqa: E402

READERS = int(os.environ.get("READERS", "6"))
HOLD_S = float(os.environ.get("HOLD_S", "6"))
LOCK_TIMEOUT = os.environ.get("LOCK_TIMEOUT", "3s")
BUCKET_S = 0.25
RUN_S = HOLD_S + 4

LOCK_QUERY = """
SELECT a.pid, a.state, a.wait_event_type, l.mode, l.granted, left(a.query, 46) AS query
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.relation = 'orders'::regclass
ORDER BY l.granted DESC, a.query_start
"""


class Timeline:
    """Reads completed per 250ms bucket, plus latencies. Shared across threads."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.buckets = defaultdict(int)
        self.latencies: list[float] = []
        self.lock = threading.Lock()

    def record(self, at: float, latency_ms: float) -> None:
        with self.lock:
            self.buckets[int((at - self.t0) / BUCKET_S)] += 1
            self.latencies.append(latency_ms)

    def render(self, n_buckets: int) -> str:
        peak = max(self.buckets.values(), default=1)
        blocks = " .:-=+*#%@"
        out = []
        for i in range(n_buckets):
            v = self.buckets.get(i, 0)
            out.append("_" if v == 0 else blocks[min(len(blocks) - 1, 1 + int(8 * v / peak))])
        return "".join(out)


def reader(stop: threading.Event, timeline: Timeline, errors: list) -> None:
    """One client doing exactly what a web request does: a cheap read of orders."""
    conn = lab_db.connect()
    conn.execute("SELECT set_config('application_name', 'sep-reader', false)")
    rng = random.Random(threading.get_ident())
    try:
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                conn.execute("SELECT id, status FROM orders WHERE id = %s",
                             (rng.randint(1, 1_000_000),)).fetchone()
                timeline.record(time.time(), (time.perf_counter() - t0) * 1000)
            except psycopg.Error as exc:
                errors.append(lab_db.sqlstate(exc) or str(exc))
                time.sleep(0.05)
    finally:
        conn.close()


def blocker(started: threading.Event) -> None:
    """A long-running transaction that has READ from orders.

    It must actually touch the table. `SELECT pg_sleep(30)` on its own takes no
    lock on anything, which is the most common way this experiment is set up
    wrong and appears to disprove itself.
    """
    conn = lab_db.connect(autocommit=False)
    conn.execute("SELECT set_config('application_name', 'sep-blocker', false)")
    conn.execute("SELECT count(*) FROM (SELECT id FROM orders LIMIT 1) s").fetchone()
    started.set()
    time.sleep(HOLD_S)
    conn.commit()
    conn.close()


def migrator(use_timeout: bool, result: dict) -> None:
    conn = lab_db.connect(autocommit=False)
    conn.execute("SELECT set_config('application_name', 'sep-migration', false)")
    t0 = time.perf_counter()
    try:
        if use_timeout:
            # SET LOCAL scopes it to this transaction, which is exactly what you
            # want in a migration: it cannot leak into whatever runs next on
            # this connection.
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        conn.execute("ALTER TABLE orders ADD COLUMN notes text")
        conn.commit()
        result["outcome"] = "committed"
    except psycopg.Error as exc:
        conn.rollback()
        result["outcome"] = f"failed with SQLSTATE {lab_db.sqlstate(exc)} ({exc.diag.message_primary})"
    result["waited_s"] = time.perf_counter() - t0
    conn.close()


def sampler(stop: threading.Event, samples: list) -> None:
    conn = lab_db.connect()
    try:
        while not stop.is_set():
            rows = conn.execute(LOCK_QUERY).fetchall()
            ungranted = [r for r in rows if not r[4]]
            if ungranted:
                samples.append((time.time(), rows))
            time.sleep(0.2)
    finally:
        conn.close()


def run_phase(label: str, use_timeout: bool) -> dict:
    print(f"\n{label}")
    with lab_db.connect() as conn:
        conn.execute("ALTER TABLE orders DROP COLUMN IF EXISTS notes")

    t0 = time.time()
    timeline = Timeline(t0)
    stop = threading.Event()
    errors: list = []
    samples: list = []
    threads = [threading.Thread(target=reader, args=(stop, timeline, errors), daemon=True)
               for _ in range(READERS)]
    threads.append(threading.Thread(target=sampler, args=(stop, samples), daemon=True))
    for t in threads:
        t.start()

    time.sleep(1.0)                       # a second of healthy baseline traffic
    started = threading.Event()
    b = threading.Thread(target=blocker, args=(started,), daemon=True)
    b.start()
    started.wait(5)
    blocker_at = time.time() - t0

    time.sleep(0.5)
    mig_result: dict = {}
    m = threading.Thread(target=migrator, args=(use_timeout, mig_result), daemon=True)
    m.start()
    migration_at = time.time() - t0

    m.join(RUN_S)
    b.join(RUN_S)
    time.sleep(1.0)                       # recovery, so the timeline shows it
    stop.set()
    for t in threads:
        t.join(2)

    n_buckets = int((time.time() - t0) / BUCKET_S) + 1
    with lab_db.connect() as conn:
        conn.execute("ALTER TABLE orders DROP COLUMN IF EXISTS notes")

    return {
        "timeline": timeline,
        "n_buckets": n_buckets,
        "blocker_at": blocker_at,
        "migration_at": migration_at,
        "result": mig_result,
        "errors": errors,
        "samples": samples,
    }


def report(phase: dict) -> None:
    tl = phase["timeline"]
    print(f"  reads per {int(BUCKET_S * 1000)}ms, over {phase['n_buckets'] * BUCKET_S:.1f}s "
          f"( _ = zero reads completed ):")
    print(f"    {tl.render(phase['n_buckets'])}")
    marker = [" "] * phase["n_buckets"]
    for at, ch in ((phase["blocker_at"], "B"), (phase["migration_at"], "A")):
        idx = int(at / BUCKET_S)
        if 0 <= idx < len(marker):
            marker[idx] = ch
    print(f"    {''.join(marker)}")
    print("     B = blocking transaction reads orders    A = ALTER TABLE issued")

    total = sum(tl.buckets.values())
    stalled = sum(1 for i in range(phase["n_buckets"]) if tl.buckets.get(i, 0) == 0)
    print(f"  reads completed: {total:,}   "
          f"p50 {lab_db.percentile(tl.latencies, 50):.1f}ms   "
          f"p99 {lab_db.percentile(tl.latencies, 99):.1f}ms   "
          f"max {max(tl.latencies, default=0):.0f}ms")
    print(f"  buckets with ZERO reads completed: {stalled} "
          f"({stalled * BUCKET_S:.2f}s of the run)")
    print(f"  migration: {phase['result'].get('outcome', 'still running')} "
          f"after waiting {phase['result'].get('waited_s', 0):.2f}s")
    if phase["errors"]:
        print(f"  reader errors: {len(phase['errors'])} "
              f"({', '.join(sorted(set(phase['errors'])))})")

    if phase["samples"]:
        _at, rows = phase["samples"][len(phase["samples"]) // 2]
        print("  the queue, sampled while it was building:")
        print(f"    {'pid':>7}  {'granted':<8}{'mode':<22}{'wait':<10}query")
        for pid, _state, wait, mode, granted, query in rows[:8]:
            print(f"    {pid:>7}  {str(granted):<8}{mode:<22}{str(wait or '-'):<10}"
                  f"{' '.join(query.split())[:42]}")
        n_waiting = sum(1 for r in rows if not r[4])
        print(f"    {n_waiting} of {len(rows)} lock requests on `orders` were NOT granted.")


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.ensure_big_seed(conn)
        lab_db.banner(f"The migration incident -- {lab_db.describe_server(conn)}")
        print(f"{READERS} readers doing point lookups on orders, a transaction holding")
        print(f"ACCESS SHARE for {HOLD_S}s, and one ALTER TABLE arriving in the middle.")
        print("\nRun this in another terminal while it goes, and watch `granted`:")
        print("  psql -d sep_lab_03_data -f 05-locking-and-deadlocks/sql/lock_queue.sql")

    a = run_phase("PHASE 1 -- ALTER TABLE with no lock_timeout (the incident)", False)
    report(a)

    b = run_phase(f"PHASE 2 -- SET LOCAL lock_timeout = '{LOCK_TIMEOUT}' before the ALTER", True)
    report(b)

    print("\n  Read the two timelines against each other.")
    print("  Phase 1: reads stop when the ALTER starts waiting -- not when it runs -- and")
    print("  do not resume until the blocking transaction commits. The duration of your")
    print("  outage is the duration of the LONGEST transaction already running, which is")
    print("  a number you do not control and probably do not measure.")
    print(f"  Phase 2: the same stall, bounded at {LOCK_TIMEOUT}, and then a failed migration.")
    print("  A failed migration is a Slack message. The other one is an incident review.")
    print()
    print("  lock_timeout does NOT give you a zero-impact migration. There is still a")
    print("  window -- up to the timeout -- where requests queue. What it buys is that the")
    print("  window is bounded by a number you chose, and the retry loop around the")
    print("  migration is yours to write. Pair it with a retry-with-backoff and a")
    print("  statement_timeout on the web role, and DDL stops being an outage risk.")


if __name__ == "__main__":
    main()
