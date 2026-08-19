"""
Layer 6 Topic 5 - Utilization is not saturation: the connection pool, in Python.

Why Python: SQLAlchemy's pool is the bottom rung of the ladder. It exposes
`checkout` and `checkin` events and a `status()` string, and there is no timer
anywhere in it. The number you need during an incident -- how long a request
waited for a connection -- does not exist until you write it. Worse, and this
is Topic 2's finding arriving as a metric: the wait happens BEFORE any
statement is issued, so it produces no span either. Auto-instrumentation gives
you the server span and the DB spans and nothing in between.

What this program does
----------------------
A real pool -- five permits handed out FIFO, which is what `pool_size=5,
max_overflow=0` is -- under a real ramp of real threads, each holding a
connection for a fixed service time and doing a little application work in
between. Every number below is measured in this process; nothing is modelled.

Three views of the same ramp, printed side by side:

  1. UTILIZATION  -- checked-out / size. What every default dashboard shows.
     It pins at 100% and stops carrying information, which is exactly when the
     incident starts.
  2. SATURATION   -- queue depth and checkout WAIT TIME. Unbounded, still
     moving, and the thing that correlates with what users feel.
  3. What SQLAlchemy gives you for free: `pool.status()`, a string, sampled.
     Compare it against the ground truth in the same row.

Then two closing sections:

  * the hand-written instrumentation that produces the metric that does not
    exist by default: a `checkout` event handler, a timestamp, a subtraction,
    a histogram, and `db.client.connection.count{state=used|idle}` beside it.
  * one slow request's span timeline, showing the pool wait as a GAP with no
    span in it -- the reason this is invisible in a trace UI until you go and
    instrument checkout yourself.

What to look for in the output
------------------------------
The step where utilization hits 100% and the step where checkout waits begin.
They are not the same step, and the order they arrive in is the finding: by the
time saturation is visible in the utilization column, the column has been
saturated for a while and stopped changing. Then read the last column of the
ramp table: the mean is not the p99, and the pool is exactly the place where
the mean is the least interesting statistic.
"""
import sys
import threading
import time
from collections import deque

POOL_SIZE = 5          # SQLAlchemy: pool_size=5, max_overflow=0
SERVICE_TIME = 0.005   # 5ms holding the connection: a fast indexed query
THINK_TIME = 0.010     # 10ms of application work between queries
STEP_SECONDS = 1.0
POLL_INTERVAL = 0.25   # the dashboard's scrape, scaled to the step length
WORKER_STEPS = [2, 5, 10, 25, 60, 120]   # the lab's ramp, compressed


class InstrumentedPool:
    """A connection pool with the instrumentation SQLAlchemy does not ship.

    The `checkout`/`checkin` hooks are exactly SQLAlchemy's events; everything
    inside them is the code you have to write yourself. That is the lesson: the
    hooks exist, the metric does not.
    """

    def __init__(self, size):
        self.size = size
        # A FIFO queue of waiters, not a Semaphore: SQLAlchemy's QueuePool
        # hands the connection to the longest waiter, and an unfair pool would
        # produce a starvation distribution rather than a queueing one.
        self._available = size
        self._waiters = deque()
        self._lock = threading.Lock()
        self.checked_out = 0
        self.waiting = 0
        # Instrumentation -- none of this exists by default.
        self.wait_times = []          # the histogram nobody ships
        self.max_waiting = 0
        self.checkouts = 0
        self.timeouts = 0

    def acquire(self, timeout=30.0):
        """Returns the wait in seconds, or None on pool_timeout."""
        start = time.perf_counter()
        with self._lock:
            if self._available and not self._waiters:
                self._available -= 1
                self.checked_out += 1
                self.checkouts += 1
                self.wait_times.append(0.0)
                return 0.0
            ticket = threading.Event()
            self._waiters.append(ticket)
            self.waiting += 1
            self.max_waiting = max(self.max_waiting, self.waiting)

        granted = ticket.wait(timeout)
        wait = time.perf_counter() - start

        with self._lock:
            self.waiting -= 1
            if not granted:
                # pool_timeout: SQLAlchemy raises TimeoutError here, and this
                # is the one pool event that DOES show up in your logs.
                self._waiters.remove(ticket)
                self.timeouts += 1
                return None
            self.checked_out += 1
            self.checkouts += 1
            self.wait_times.append(wait)   # <- the metric that does not exist
        return wait

    def release(self):
        with self._lock:
            self.checked_out -= 1
            if self._waiters:
                self._waiters.popleft().set()   # longest waiter first
            else:
                self._available += 1

    # --- the two things SQLAlchemy actually gives you -----------------------

    def status(self):
        """SQLAlchemy's QueuePool.status(). A string. Sampled, not recorded."""
        return ("Pool size: %d  Connections in pool: %d  Current Overflow: %d  "
                "Current Checked out connections: %d"
                % (self.size, self.size - self.checked_out, 0, self.checked_out))

    def utilization(self):
        return self.checked_out / self.size


def percentile(values, q):
    """Nearest-rank percentile. No numpy on this machine, and none needed."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(q * len(ordered) + 0.999999) - 1))
    return ordered[index]


def run_step(pool, workers, seconds):
    """`workers` threads hammering the pool for `seconds`. Returns measurements."""
    stop = time.perf_counter() + seconds
    first_index = len(pool.wait_times)
    pool.max_waiting = 0
    samples = deque()          # the 15-second-poll dashboard, sped up
    latencies = []
    lat_lock = threading.Lock()

    def worker():
        local = []
        while time.perf_counter() < stop:
            request_start = time.perf_counter()
            wait = pool.acquire()
            if wait is None:
                continue
            time.sleep(SERVICE_TIME)   # holding the connection: the query
            pool.release()
            local.append(time.perf_counter() - request_start)
            time.sleep(THINK_TIME)     # application work, connection returned
        with lat_lock:
            latencies.extend(local)

    def sampler():
        # A dashboard scraping the gauge. Every sample is a moment; everything
        # between two samples is invisible to it.
        while time.perf_counter() < stop:
            samples.append((pool.utilization(), pool.waiting))
            time.sleep(POLL_INTERVAL)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sampler_thread.join()

    waits = pool.wait_times[first_index:]
    sampled_util = max((s[0] for s in samples), default=0.0)
    sampled_queue = max((s[1] for s in samples), default=0)
    return {
        "workers": workers,
        "requests": len(waits),
        "sampled_util": sampled_util,
        "true_max_queue": pool.max_waiting,
        "sampled_queue": sampled_queue,
        "wait_mean": sum(waits) / len(waits) if waits else 0.0,
        "wait_p50": percentile(waits, 0.50),
        "wait_p99": percentile(waits, 0.99),
        "lat_p99": percentile(latencies, 0.99),
        "timeouts": pool.timeouts,
    }


def main():
    print("Layer 6 Topic 5 - utilization vs saturation, on a Python connection pool")
    print("python %s   pool_size=%d, max_overflow=0, service time %.0f ms"
          % (sys.version.split()[0], POOL_SIZE, SERVICE_TIME * 1000))
    print("=" * 78)
    print()

    pool = InstrumentedPool(POOL_SIZE)
    rows = []
    for workers in WORKER_STEPS:
        rows.append(run_step(pool, workers, STEP_SECONDS))

    print("--- The ramp: one pool, six concurrency levels, everything measured ---")
    print()
    print("           |  USE: utilization  |        USE: saturation        |   RED   ")
    print("  workers  |  polled  in use    |  max queued   wait p50 / p99  |  req p99")
    print("  ---------+--------------------+-------------------------------+---------")
    for r in rows:
        print("  %7d  |  %5.0f%%   %2d of %d   |  %10d   %5.1f / %6.1f ms |  %6.1f ms"
              % (r["workers"], 100 * r["sampled_util"],
                 round(r["sampled_util"] * POOL_SIZE), POOL_SIZE,
                 r["true_max_queue"], 1000 * r["wait_p50"], 1000 * r["wait_p99"],
                 1000 * r["lat_p99"]))
    print()

    first_pinned = next((r for r in rows if r["sampled_util"] >= 0.999), None)
    first_waited = next((r for r in rows if r["wait_p99"] > 0.001), None)
    pinned_at = first_pinned["workers"] if first_pinned else None
    waited_at = first_waited["workers"] if first_waited else None
    print("  polled utilization first reads 100%%   at %s workers"
          % (pinned_at if pinned_at else "never"))
    print("  checkout wait p99 first exceeds 1ms   at %s workers"
          % (waited_at if waited_at else "never"))
    print()
    if pinned_at and waited_at and waited_at < pinned_at:
        print("  On this run the queue formed BEFORE the utilization gauge ever")
        print("  read 100%. That is not a paradox and it is not luck: utilization")
        print("  is sampled, and a pool that is full for 40 ms at a time is full")
        print("  in between two scrapes. The queue, meanwhile, was recorded on")
        print("  every checkout, so it had nothing to miss.")
    elif pinned_at and waited_at and waited_at == pinned_at:
        print("  Both crossed at the same step, which is the least interesting")
        print("  of the three orders and still makes the point: after this step")
        print("  the utilization column never changes again, and every later row")
        print("  is described entirely by the saturation columns.")
    elif pinned_at and waited_at:
        print("  Utilization pinned first, then stopped moving. Everything after")
        print("  that point is described only by the saturation columns.")
    print()
    print("  Either way, read the util% column downward: it reaches its maximum")
    print("  and stays there, with no room left to describe anything, while the")
    print("  wait columns keep climbing with no upper bound. That is the whole")
    print("  distinction, and it is why the green number is the one that lies.")
    print()

    print("--- What SQLAlchemy gives you for free ---")
    print()
    print("  pool.status():")
    print("    %s" % pool.status())
    print()
    print("  That is the entire built-in observable: a string, describing this")
    print("  instant, with no history and no timing. Nothing in SQLAlchemy counts")
    print("  how long anybody waited. Ask it 'what was the p99 checkout wait during")
    print("  the incident' and there is no object in the library that holds an")
    print("  answer.")
    print()

    print("--- What a 15-second-poll dashboard would have seen ---")
    print()
    print("  %-12s %-16s %-16s" % ("workers", "true max queue", "polled max queue"))
    for r in rows:
        missed = "" if r["sampled_queue"] >= r["true_max_queue"] else "   <- missed"
        print("  %-12d %-16d %-16d%s"
              % (r["workers"], r["true_max_queue"], r["sampled_queue"], missed))
    print()
    print("  This program polls every %.0f ms and still misses peaks. A real"
          % (1000 * POLL_INTERVAL))
    print("  dashboard polls every 15 seconds. A 400ms queue between two scrapes")
    print("  did not happen, as far as your graph is concerned. A gauge is a")
    print("  sample; a histogram is a record. Saturation needs the record.")
    print()

    print("--- The instrumentation that produces the missing metric ---")
    print()
    print("  Fifteen lines, in the events SQLAlchemy already gives you:")
    print()
    print("    @event.listens_for(engine, 'checkout')")
    print("    def on_checkout(dbapi_conn, conn_record, conn_proxy):")
    print("        wait = time.perf_counter() - conn_record.info['requested_at']")
    print("        checkout_wait_histogram.record(wait)          # the missing one")
    print("        pool_state.add(1, {'state': 'used'})")
    print()
    print("  and the semconv names to publish them under:")
    print("    db.client.connection.count{state=\"used\"|\"idle\"}   (gauge)")
    print("    db.client.connection.wait_time                     (histogram)")
    print()
    all_waits = pool.wait_times
    print("  What that histogram holds after this run, from %s checkouts:"
          % f"{len(all_waits):,}")
    print("    p50   %7.2f ms" % (1000 * percentile(all_waits, 0.50)))
    print("    p95   %7.2f ms" % (1000 * percentile(all_waits, 0.95)))
    print("    p99   %7.2f ms" % (1000 * percentile(all_waits, 0.99)))
    print("    max   %7.2f ms" % (1000 * max(all_waits)))
    print("    mean  %7.2f ms   <- the only one a counter-pair could have given you"
          % (1000 * sum(all_waits) / len(all_waits)))
    print()
    print("  The mean is %.1fx smaller than the p99. Pool waits are exactly the"
          % ((percentile(all_waits, 0.99) / (sum(all_waits) / len(all_waits)))
             if all_waits else 0))
    print("  distribution where the mean is the least interesting statistic, which")
    print("  is worth remembering when you get to the Go program in this topic and")
    print("  find that a mean is all `database/sql` can give you.")
    print()

    print("--- Why you cannot see this in a trace (Topic 2's finding, as a metric) ---")
    print()
    slow = max(rows, key=lambda r: r["wait_p99"])
    wait_ms = 1000 * slow["wait_p99"]
    query_ms = SERVICE_TIME * 1000
    total = wait_ms + query_ms
    scale = 60.0 / total if total else 1.0
    print("  One request at the p99 of the %d-worker step, drawn to scale:" % slow["workers"])
    print()
    print("    server span   |%s|  %.1f ms" % ("=" * int(total * scale), total))
    print("    (no span)     |%s%s|  %.1f ms  <- pool wait"
          % (" " * 0, "?" * max(1, int(wait_ms * scale)), wait_ms))
    print("    db span       |%s%s|  %.1f ms"
          % (" " * max(1, int(wait_ms * scale)), "=" * max(1, int(query_ms * scale)),
             query_ms))
    print()
    print("  %.0f%% of that request is a gap with no span in it. SQLAlchemy checks a"
          % (100 * wait_ms / total))
    print("  connection out BEFORE issuing any statement, so there is nothing for")
    print("  auto-instrumentation to hook: no statement, no span. In Tempo this")
    print("  reads as dead time between the server span starting and the first DB")
    print("  span, and the natural conclusion -- 'Python was slow' -- is wrong.")
    print()
    print("  Instrument checkout, and the gap gets a name.")


if __name__ == "__main__":
    main()
