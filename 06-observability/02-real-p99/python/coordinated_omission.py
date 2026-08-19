"""
Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.

Why Python: this is the production stack, and it is also the runtime where the
open-loop generator is most expensive to build. Holding 250 requests in flight
means 250 OS threads (or 250 coroutines and an async client you may not have),
which is exactly why so many Python load tests are written closed-loop with a
small fixed worker count -- and a closed-loop test structurally cannot observe
the failure it was written to catch.

What this demonstrates
----------------------
One service. One stall. Two load generators. The same p99 question.

  * The service is a single server with a FIFO queue, 3ms per request, so its
    capacity is ~333 req/s. Offered load is 200 req/s, a comfortable 60%.
  * At T+2.5s exactly one request takes 500ms instead of 3ms. A GC pause, a
    lock, a slow downstream, a cold page. One request.

  * CLOSED-LOOP generator: 4 virtual users. Each sends a request, waits for the
    response, thinks for 30ms, repeats. This is `k6 run --vus 4`, Locust's
    default, JMeter's default, and almost every load test ever written.
  * OPEN-LOOP generator: requests are issued at a fixed 200/s regardless of
    whether earlier ones came back. This is k6's constant-arrival-rate
    executor, or `vegeta -rate=200`.

Both measure latency. They disagree, and the disagreement is not noise: during
the stall the closed-loop generator STOPS SENDING, because all six of its users
are blocked waiting. It cannot observe a queue it is not filling.

What to look for in the output
------------------------------
1. "requests started during the stall window". Closed-loop starts about 4.
   Open-loop starts about 100. That single line is the entire mechanism.
2. Closed-loop p99 versus open-loop p99. Same service, same stall.
3. Closed-loop's iteration duration versus its request duration. The stall is
   visible in the first and not the second -- that is the tell k6 gives you,
   and the reason the layer README tells you to check it.
4. Peak in-flight requests and the thread count that bought it.

Run:  python3 coordinated_omission.py
"""

import queue
import threading
import time

SERVICE_MS = 3.0        # normal service time -> ~333 req/s capacity
STALL_AFTER_MS = 2500.0  # when the one slow request happens
STALL_MS = 500.0        # how long that one request takes
RUN_MS = 5000.0
OPEN_RATE_PER_SEC = 200  # offered load, ~60% of capacity
CLOSED_VUS = 4
CLOSED_THINK_MS = CLOSED_VUS / OPEN_RATE_PER_SEC * 1000.0  # same offered load


class Request:
    __slots__ = ("seq", "arrival_ns", "sent_ns", "done_ns", "done")

    def __init__(self, seq, arrival_ns):
        self.seq = seq
        self.arrival_ns = arrival_ns   # when it *should* have been sent
        self.sent_ns = 0               # when it actually was sent
        self.done_ns = 0
        self.done = threading.Event()


class Service:
    """A single server with a FIFO queue. The queue is the point: it is where
    the latency that a closed-loop generator cannot see accumulates."""

    def __init__(self, epoch_ns):
        self.inbox = queue.Queue()
        self.epoch_ns = epoch_ns
        self.stalled = False
        self.depth_samples = []
        self._stop = object()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.inbox.put(self._stop)
        self._thread.join()

    def submit(self, request):
        request.sent_ns = time.perf_counter_ns()
        self.inbox.put(request)

    def _serve(self):
        while True:
            item = self.inbox.get()
            if item is self._stop:
                return
            self.depth_samples.append(self.inbox.qsize())
            elapsed_ms = (time.perf_counter_ns() - self.epoch_ns) / 1e6
            if not self.stalled and elapsed_ms >= STALL_AFTER_MS:
                self.stalled = True
                time.sleep(STALL_MS / 1000.0)   # the one bad request
            else:
                time.sleep(SERVICE_MS / 1000.0)
            item.done_ns = time.perf_counter_ns()
            item.done.set()


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def in_stall_window(request, epoch_ns):
    start_ms = (request.sent_ns - epoch_ns) / 1e6
    return STALL_AFTER_MS <= start_ms < STALL_AFTER_MS + STALL_MS


def run_closed_loop():
    epoch_ns = time.perf_counter_ns()
    service = Service(epoch_ns)
    service.start()
    requests = []
    lock = threading.Lock()
    iteration_ms = []
    seq = [0]

    def virtual_user():
        while (time.perf_counter_ns() - epoch_ns) / 1e6 < RUN_MS:
            iter_start = time.perf_counter_ns()
            with lock:
                seq[0] += 1
                request = Request(seq[0], time.perf_counter_ns())
            service.submit(request)
            request.done.wait()          # <- the generator is now blocked
            with lock:
                requests.append(request)
            time.sleep(CLOSED_THINK_MS / 1000.0)
            iteration_ms.append((time.perf_counter_ns() - iter_start) / 1e6)

    users = [threading.Thread(target=virtual_user) for _ in range(CLOSED_VUS)]
    for u in users:
        u.start()
    for u in users:
        u.join()
    service.stop()

    request_ms = [(r.done_ns - r.sent_ns) / 1e6 for r in requests]
    started_in_stall = sum(1 for r in requests if in_stall_window(r, epoch_ns))
    return {
        "requests": len(requests),
        "request_ms": request_ms,
        "iteration_ms": iteration_ms,
        "started_in_stall": started_in_stall,
        "threads": CLOSED_VUS,
        "peak_in_flight": CLOSED_VUS,
    }


def run_open_loop():
    epoch_ns = time.perf_counter_ns()
    service = Service(epoch_ns)
    service.start()
    requests = []
    lock = threading.Lock()
    in_flight = [0]
    peak_in_flight = [0]
    threads = []

    def issue(request):
        with lock:
            in_flight[0] += 1
            peak_in_flight[0] = max(peak_in_flight[0], in_flight[0])
        service.submit(request)
        request.done.wait()
        with lock:
            in_flight[0] -= 1
            requests.append(request)

    interval_ns = int(1e9 / OPEN_RATE_PER_SEC)
    seq = 0
    while True:
        target_ns = epoch_ns + seq * interval_ns
        if (target_ns - epoch_ns) / 1e6 >= RUN_MS:
            break
        sleep_s = (target_ns - time.perf_counter_ns()) / 1e9
        if sleep_s > 0:
            time.sleep(sleep_s)
        seq += 1
        request = Request(seq, target_ns)
        # One thread per in-flight request. This is the cost of open-loop in a
        # thread-per-request runtime, and the reason people avoid writing it.
        t = threading.Thread(target=issue, args=(request,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()
    service.stop()

    # Latency measured from INTENDED arrival, not from when the generator got
    # around to sending. That difference is the whole correction.
    latency_ms = [(r.done_ns - r.arrival_ns) / 1e6 for r in requests]
    service_ms = [(r.done_ns - r.sent_ns) / 1e6 for r in requests]
    started_in_stall = sum(1 for r in requests if in_stall_window(r, epoch_ns))
    return {
        "requests": len(requests),
        "latency_ms": latency_ms,
        "service_ms": service_ms,
        "started_in_stall": started_in_stall,
        "threads": len(threads),
        "peak_in_flight": peak_in_flight[0],
    }


def main():
    print("=" * 74)
    print("COORDINATED OMISSION   (Python, single-server FIFO service)")
    print("=" * 74)
    print("service capacity ~%.0f req/s (%.0fms/request), offered load %d req/s"
          % (1000.0 / SERVICE_MS, SERVICE_MS, OPEN_RATE_PER_SEC))
    print("one request at T+%.0fms takes %.0fms instead of %.0fms"
          % (STALL_AFTER_MS, STALL_MS, SERVICE_MS))
    print("run length %.0fms\n" % RUN_MS)

    print("running closed-loop (%d virtual users, %.0fms think time)..."
          % (CLOSED_VUS, CLOSED_THINK_MS))
    closed = run_closed_loop()
    print("running open-loop (%d req/s arrival rate)...\n" % OPEN_RATE_PER_SEC)
    opened = run_open_loop()

    print("%-38s %14s %14s" % ("", "CLOSED-LOOP", "OPEN-LOOP"))
    print("%-38s %14d %14d" % ("requests completed", closed["requests"], opened["requests"]))
    print("%-38s %14d %14d" % ("requests started IN the stall window",
                               closed["started_in_stall"], opened["started_in_stall"]))
    print("%-38s %14d %14d" % ("peak requests in flight",
                               closed["peak_in_flight"], opened["peak_in_flight"]))
    print("%-38s %14d %14d" % ("OS threads used by the generator",
                               closed["threads"], opened["threads"]))
    print()
    for label, q in (("p50", 0.50), ("p75", 0.75), ("p95", 0.95),
                     ("p99", 0.99), ("p99.9", 0.999), ("max", 1.0)):
        print("%-38s %13.1fms %13.1fms"
              % ("latency " + label,
                 percentile(closed["request_ms"], q),
                 percentile(opened["latency_ms"], q)))

    print("\nThe closed-loop column measures request duration: send -> response.")
    print("The open-loop column measures from the moment the request was DUE.")
    print("That second definition is the only one a user experiences.")
    print("Note the first row too: the closed-loop run completed %d requests to"
          % closed["requests"])
    print("the open-loop run's %d. It did not go faster or slower -- it asked"
          % opened["requests"])
    print("for less, precisely while the service was worst.")

    print("\nThe tell, inside the closed-loop run alone:")
    print("  request duration p99   : %8.1fms" % percentile(closed["request_ms"], 0.99))
    print("  iteration duration p99 : %8.1fms" % percentile(closed["iteration_ms"], 0.99))
    print("  (iteration = request + think time; the stall lands here first)")
    print("  If iteration_duration climbs while http_req_duration does not, your")
    print("  generator stopped asking. That is k6's version of this same line.")

    print("\nOpen-loop, measured two ways from the same requests:")
    print("  from send    (send -> response)    p99 : %8.1fms"
          % percentile(opened["service_ms"], 0.99))
    print("  from arrival (due  -> response)    p99 : %8.1fms"
          % percentile(opened["latency_ms"], 0.99))
    print("  These agree, and that agreement is the definition of open loop: the")
    print("  generator never fell behind, so there is nothing to correct for.")
    print("  The correction only matters when the two differ -- which is exactly")
    print("  what a closed-loop generator hides, because it has no notion of a")
    print("  request being *due* at all.")

    closed99 = percentile(closed["request_ms"], 0.99)
    open99 = percentile(opened["latency_ms"], 0.99)
    if closed99 > 0:
        print("\nVERDICT: open-loop p99 is %.1fx the closed-loop p99 for the identical"
              % (open99 / closed99))
        print("service and the identical fault.")
    print("A 500ms stall in a 5s run is 10% of the wall clock. The closed-loop")
    print("generator sampled it %d times out of %d requests (%.2f%%), which is why"
          % (closed["started_in_stall"], closed["requests"],
             100.0 * closed["started_in_stall"] / max(1, closed["requests"])))
    print("it does not reach the 99th percentile at all.")


if __name__ == "__main__":
    main()
