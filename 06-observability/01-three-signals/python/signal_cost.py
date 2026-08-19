"""
Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.

Why Python: this is the production runtime. Every cost below is paid by the
interpreter, in the request path, on the same thread as the handler. Python is
also the language where the single most common instrumentation bug -- an
argument evaluated for a log line that is never written -- is invisible in
review and expensive at runtime.

What this demonstrates
----------------------
`three_signals.py` shows what each signal can *answer*. This shows what each
signal *costs to emit*, which is the other half of the cardinality/retention
tradeoff and the half that lands in your p99 rather than in your bill.

Five operations, timed on this machine:

  1. counter add      - dict lookup on a bounded label tuple + increment
  2. span record      - object allocation, timestamps, six attributes, append
  3. log line (INFO)  - json.dumps + a real logging.Logger + handler + stream
  4. debug, DISABLED, argument built eagerly    <- the bug
  5. debug, DISABLED, guarded by isEnabledFor   <- the fix

Operations 4 and 5 emit nothing at all. Their cost difference is pure waste,
and it is paid on every request in production forever.

This measures the *shape* of the cost with hand-rolled metric and span stores,
not the OpenTelemetry SDK (which is not installed here). A real SDK adds work
on top of these numbers -- context lookup, attribute validation, view
matching, batching -- it never subtracts any.

What to look for in the output
------------------------------
- The ratio between a counter add and a log line. It is not 2x.
- Rows 4 and 5. Row 4 does exactly as much useful work as row 5: none.
- The last block: telemetry as a percentage of one core at a given request
  rate. That is the number to take to a code review.

Run:  python3 signal_cost.py
"""

import json
import logging
import sys
import time

ITERATIONS = 200_000

# Defeats nothing in CPython (there is no optimizer to defeat here), but kept
# symmetrical with the Rust/C++/Java versions of this file, where it matters.
SINK = [0]


class CounterStore:
    """A bounded-cardinality counter, the way a metrics SDK stores one: a map
    from an ordered label tuple to a number. The cost is the map lookup, which
    is why bounded label sets are cheap and unbounded ones are Topic 4."""

    def __init__(self):
        self.series = {}

    def add(self, labels, value=1):
        self.series[labels] = self.series.get(labels, 0) + value


class Span:
    """__slots__ because a real span is allocated per operation per request and
    a __dict__ per span is measurable at four figures of RPS."""

    __slots__ = ("name", "trace_id", "span_id", "start_ns", "end_ns", "attributes")

    def __init__(self, name, trace_id, span_id, attributes):
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes = attributes
        self.start_ns = time.perf_counter_ns()
        self.end_ns = 0

    def end(self):
        self.end_ns = time.perf_counter_ns()


class CountingStream:
    """Stands in for the pipe to your log shipper. Counts bytes so we can also
    report the volume side of the tradeoff, and discards them so we are not
    benchmarking a terminal."""

    def __init__(self):
        self.bytes_written = 0

    def write(self, chunk):
        self.bytes_written += len(chunk)
        return len(chunk)

    def flush(self):
        pass


def build_logger(stream):
    logger = logging.getLogger("signal_cost")
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    # INFO, so every logger.debug() below is disabled -- exactly the production
    # configuration in which the eager-argument bug hides.
    logger.setLevel(logging.INFO)
    return logger


def bench(label, fn, iterations=ITERATIONS):
    fn()  # warm the code path so we are not timing first-call import work
    start = time.perf_counter_ns()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter_ns() - start
    return label, elapsed / iterations


def main():
    stream = CountingStream()
    logger = build_logger(stream)
    counter = CounterStore()

    # One request's worth of realistic data: an order being priced.
    order = {
        "order_id": "ord_8f31c2",
        "customer_id": "cus_00194",
        "items": [{"sku": "SKU-1", "qty": 2}, {"sku": "SKU-7", "qty": 1}],
        "discount": 0.15,
        "currency": "GBP",
    }
    labels = (("http.request.method", "GET"),
              ("http.route", "/orders/{id}"),
              ("http.response.status_code", "200"))
    attributes = {
        "http.request.method": "GET",
        "http.route": "/orders/{id}",
        "http.response.status_code": 200,
        "db.system.name": "postgresql",
        "customer.id": order["customer_id"],
        "order.id": order["order_id"],
    }

    def counter_add():
        counter.add(labels)

    def span_record():
        span = Span("GET /orders/{id}", "4bf92f3577b34da6a3ce929d0e0e4736",
                    "00f067aa0ba902b7", attributes)
        span.end()
        SINK[0] += span.end_ns - span.start_ns

    def log_info():
        logger.info(json.dumps({
            "level": "info",
            "msg": "order priced",
            "order_id": order["order_id"],
            "customer_id": order["customer_id"],
            "duration_ms": 12.4,
        }))

    def log_debug_eager():
        # THE BUG. The logger is at INFO, so this line is never written. The
        # json.dumps() still runs, on every request, forever. Nothing in the
        # code review flags it: it reads as "a debug log".
        logger.debug("pricing payload=" + json.dumps(order))

    def log_debug_guarded():
        # THE FIX. One branch. Lift this into any PR where a log argument is
        # anything other than an already-materialised value.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("pricing payload=" + json.dumps(order))

    rows = [
        bench("counter.add (3 bounded labels)", counter_add),
        bench("span create + end (6 attrs)", span_record),
        bench("log INFO, one JSON line", log_info),
        bench("log DEBUG (disabled), eager argument", log_debug_eager),
        bench("log DEBUG (disabled), isEnabledFor guard", log_debug_guarded),
    ]

    print("=" * 74)
    print("COST OF EMITTING ONE UNIT OF TELEMETRY   (Python %s, n=%d)"
          % (".".join(map(str, sys.version_info[:3])), ITERATIONS))
    print("=" * 74)
    print("%-42s %12s" % ("operation", "ns/op"))
    for label, ns in rows:
        print("%-42s %12.0f" % (label, ns))

    eager = rows[3][1]
    guarded = rows[4][1]
    print("\nRows 4 and 5 both emit nothing. Row 4 costs %.0f ns more than row 5"
          % (eager - guarded))
    print("for exactly zero output. At 8 disabled debug calls per request and")
    print("1000 req/s that is %.1f ms/s of CPU spent producing nothing."
          % (8 * 1000 * (eager - guarded) / 1e6))

    # A realistic per-request instrumentation budget for a FastAPI handler:
    # one RED counter, three spans (server + db + downstream), two INFO lines,
    # eight disabled debug lines.
    per_request_ns = (rows[0][1] + 3 * rows[1][1] + 2 * rows[2][1]
                      + 8 * rows[3][1])
    per_request_fixed_ns = (rows[0][1] + 3 * rows[1][1] + 2 * rows[2][1]
                            + 8 * rows[4][1])
    print("\nOne request = 1 counter + 3 spans + 2 INFO logs + 8 disabled debug logs")
    print("  as written : %8.1f us/request   -> %.1f%% of one core at 1000 req/s"
          % (per_request_ns / 1000, per_request_ns / 1e9 * 1000 * 100))
    print("  with guards: %8.1f us/request   -> %.1f%% of one core at 1000 req/s"
          % (per_request_fixed_ns / 1000, per_request_fixed_ns / 1e9 * 1000 * 100))
    print("\nBytes written by the INFO logs alone: %d for %d lines (%.0f B/line)."
          % (stream.bytes_written, ITERATIONS + 1,
             stream.bytes_written / (ITERATIONS + 1)))
    print("That is the retention side. The ns/op column is the latency side.")
    print("(sink=%d, printed so nothing above can be optimised away)" % SINK[0])


if __name__ == "__main__":
    main()
