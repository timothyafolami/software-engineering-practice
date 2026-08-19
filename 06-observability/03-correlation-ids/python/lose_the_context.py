"""
Layer 6 Topic 3 - Losing trace context at a Python concurrency boundary.

What this demonstrates
----------------------
The cross-process half of propagation is a wire format (`traceparent`) and it
is boring. The in-process half is the runtime's problem, and Python's answer is
`contextvars`:

  * asyncio understands contextvars. `asyncio.create_task` COPIES the current
    context into the new task, so the span you started is still current inside
    the task. This is the control run: it works, and it works silently, which
    is why the next one surprises people.
  * `loop.run_in_executor` does not. The callable runs on a thread-pool thread
    that was created before your request existed and never saw your context, so
    `CURRENT_SPAN.get()` there returns the default. The fix is to hand the
    executor `contextvars.copy_context().run` instead of the bare callable.
  * A queue is not a concurrency boundary the runtime can help with at all:
    there is no wire and no context, only a message body. If you do not put
    `traceparent` in the body yourself, nothing carries it.

Nothing here imports OpenTelemetry -- no SDK is installed on this machine. The
context variable, the span and the `traceparent` codec are ~40 lines of
standard library, which is the point: this failure is not a bug in an SDK, it
is a property of where the runtime does and does not copy context for you.

What to look for in the output
------------------------------
Four blocks, all in the same shape:

  caller trace_id   <id>
  callee trace_id   <id or "none">   naive
  callee trace_id   <id>             propagated
  verdict           lost | preserved

Read the verdict column top to bottom. `create_task` is preserved with no
effort; `run_in_executor` is lost with no warning; the queue is lost even
though both ends are in the same process. Then read the last section: the log
lines emitted from inside the executor thread carry no trace_id in the naive
run, which is exactly the "one-query test" failing.
"""
import asyncio
import contextvars
import json
import logging
import os
import queue
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# A minimal span + W3C traceparent codec. This is the whole of the
# cross-process half of propagation, and it fits in a screen.
# ---------------------------------------------------------------------------

_RNG = random.Random(20260818)  # fixed seed: IDs differ per span, not per run


class Span:
    __slots__ = ("trace_id", "span_id", "name", "sampled")

    def __init__(self, name, trace_id=None, sampled=True):
        self.name = name
        self.trace_id = trace_id or "%032x" % _RNG.getrandbits(128)
        self.span_id = "%016x" % _RNG.getrandbits(64)
        self.sampled = sampled

    def traceparent(self):
        return "00-%s-%s-%02x" % (self.trace_id, self.span_id, 1 if self.sampled else 0)

    @staticmethod
    def from_traceparent(header, name):
        # version-traceid-spanid-flags, e.g.
        # 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
        version, trace_id, parent_id, flags = header.split("-")
        if version != "00" or len(trace_id) != 32 or len(parent_id) != 16:
            raise ValueError("malformed traceparent: %r" % header)
        return Span(name, trace_id=trace_id, sampled=bool(int(flags, 16) & 1))


# The context variable. Its default is what "no context" looks like: None.
CURRENT_SPAN: "contextvars.ContextVar[Span | None]" = contextvars.ContextVar(
    "current_span", default=None
)


def current_trace_id():
    span = CURRENT_SPAN.get()
    return span.trace_id if span else "none"


# ---------------------------------------------------------------------------
# Logging: a filter that reads the CURRENT context per record, and a JSON
# formatter. Both halves are required for the one-query test; a filter that
# reads the span once at import time is the classic broken version, and it is
# broken in a way that looks like it works (every line gets an id, the same id).
# ---------------------------------------------------------------------------

CAPTURED_LOGS = []


class TraceContextFilter(logging.Filter):
    def filter(self, record):
        span = CURRENT_SPAN.get()  # per record, not per process
        record.trace_id = span.trace_id if span else ""
        record.span_id = span.span_id if span else ""
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "level": record.levelname,
            "msg": record.getMessage(),
            "thread": threading.current_thread().name,
            "trace_id": getattr(record, "trace_id", ""),
        }
        line = json.dumps(payload)
        CAPTURED_LOGS.append(payload)
        return line


def build_logger():
    logger = logging.getLogger("lose_the_context")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(open(os.devnull, "w"))
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceContextFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOG = build_logger()


def report(boundary, caller, naive, propagated, note=""):
    verdict = "preserved" if naive == caller else "lost"
    print("boundary          %s" % boundary)
    print("caller trace_id   %s" % caller)
    print("callee trace_id   %-32s naive" % naive)
    print("callee trace_id   %-32s propagated" % propagated)
    print("verdict           %s%s" % (verdict, ("   (%s)" % note) if note else ""))
    print()
    return verdict


# ---------------------------------------------------------------------------
# Boundary 1: asyncio.create_task -- the control. Context is COPIED into the
# task at creation, so this one works and keeps working.
# ---------------------------------------------------------------------------

async def boundary_create_task():
    span = Span("GET /orders")
    CURRENT_SPAN.set(span)

    async def child():
        return current_trace_id()

    observed = await asyncio.create_task(child())
    return report(
        "asyncio.create_task",
        span.trace_id,
        observed,
        observed,
        note="asyncio copies the context at task creation; nothing to fix",
    )


# ---------------------------------------------------------------------------
# Boundary 2: run_in_executor -- the one that bites. The pool thread predates
# the request and has its own (empty) context.
# ---------------------------------------------------------------------------

def pricing_call_sync(label):
    """A synchronous downstream call, the reason you reached for an executor."""
    LOG.info("calling pricing (%s)", label)
    return current_trace_id()


async def boundary_run_in_executor(pool):
    span = Span("GET /orders")
    CURRENT_SPAN.set(span)
    loop = asyncio.get_running_loop()

    # Naive: the bare callable. The pool thread runs it in ITS context.
    naive = await loop.run_in_executor(pool, pricing_call_sync, "naive")

    # Propagated: copy_context() snapshots this task's context; ctx.run
    # installs it on the pool thread for the duration of the call.
    ctx = contextvars.copy_context()
    fixed = await loop.run_in_executor(pool, ctx.run, pricing_call_sync, "propagated")

    return report(
        "loop.run_in_executor",
        span.trace_id,
        naive,
        fixed,
        note="fix = run_in_executor(pool, contextvars.copy_context().run, fn, ...)",
    )


# ---------------------------------------------------------------------------
# Boundary 3: a queue. No wire, no runtime help. The message body is the only
# place a traceparent can live, and putting it there is entirely on you.
# ---------------------------------------------------------------------------

def worker_consume(job):
    """Runs on the worker thread. Only the message body crosses the boundary."""
    token = None
    if "traceparent" in job:
        token = CURRENT_SPAN.set(Span.from_traceparent(job["traceparent"], "job"))
    try:
        LOG.info("processing job %s", job["id"])
        return current_trace_id()
    finally:
        if token is not None:
            CURRENT_SPAN.reset(token)


def boundary_queue():
    span = Span("POST /orders")
    CURRENT_SPAN.set(span)

    q = queue.Queue()
    results = {}

    def worker():
        while True:
            job = q.get()
            if job is None:
                return
            results[job["id"]] = worker_consume(job)

    thread = threading.Thread(target=worker, name="worker-1", daemon=True)
    thread.start()

    q.put({"id": "naive", "customer": "cust-0042"})
    q.put({"id": "propagated", "customer": "cust-0042",
           "traceparent": span.traceparent()})
    q.put(None)
    thread.join()

    CURRENT_SPAN.set(span)  # the consumer ran on another thread; restore ours
    return report(
        "Postgres-backed queue",
        span.trace_id,
        results["naive"],
        results["propagated"],
        note="the transport carries no headers; put traceparent in the body",
    )


# ---------------------------------------------------------------------------
# Boundary 4: the outbound HTTP call. The easy half, shown so the wire format
# is concrete rather than a paragraph.
# ---------------------------------------------------------------------------

def boundary_http():
    span = Span("GET /orders")
    CURRENT_SPAN.set(span)
    header = span.traceparent()

    # ...over the wire to `pricing`, which parses it and continues the trace.
    downstream = Span.from_traceparent(header, "GET /price")

    print("boundary          HTTP request to pricing")
    print("caller trace_id   %s" % span.trace_id)
    print("traceparent sent  %s" % header)
    print("callee trace_id   %-32s parsed from the header" % downstream.trace_id)
    print("verdict           preserved   (this is what being a W3C standard buys)")
    print()
    return "preserved"


def main():
    print("Layer 6 Topic 3 - losing trace context in Python (contextvars)")
    print("python %s   %s" % (sys.version.split()[0], sys.platform))
    print("=" * 72)
    print()

    verdicts = {}
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pool")
    try:
        # The pool threads are created here, before any request exists. That
        # single fact is the whole of boundary 2.
        list(pool.map(lambda _: None, range(2)))

        verdicts["asyncio.create_task"] = asyncio.run(boundary_create_task())
        verdicts["run_in_executor"] = asyncio.run(boundary_run_in_executor(pool))
    finally:
        pool.shutdown()

    verdicts["queue"] = boundary_queue()
    verdicts["http traceparent"] = boundary_http()

    print("--- Summary: which boundaries Python covers for you ---")
    for name, verdict in verdicts.items():
        covered = "runtime carries it" if verdict == "preserved" else "YOU carry it"
        print("  %-22s %-10s %s" % (name, verdict, covered))
    print()

    print("--- The one-query test, on the log lines this run emitted ---")
    with_id = [rec for rec in CAPTURED_LOGS if rec["trace_id"]]
    without = [rec for rec in CAPTURED_LOGS if not rec["trace_id"]]
    print("  log lines emitted            %d" % len(CAPTURED_LOGS))
    print("  lines carrying a trace_id    %d" % len(with_id))
    print("  lines carrying nothing       %d   <- unqueryable by request" % len(without))
    for rec in CAPTURED_LOGS:
        print("    %-9s %-24s trace_id=%s"
              % (rec["thread"].split("_")[0][:9], rec["msg"],
                 rec["trace_id"] or "(empty)"))
    print()
    print("  Every line above was emitted by the same logger with the same")
    print("  filter attached. The empty ones are not a logging bug: the filter")
    print("  read the context correctly and there was nothing in it. Fix the")
    print("  propagation and the logging fixes itself.")


if __name__ == "__main__":
    main()
