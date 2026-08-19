"""
Layer 2 · Topic 3 - A timeout is not a constant. It is a budget you spend.

Three hops, one caller, one promise: answer within OUTER_BUDGET seconds.
The upstream chain is slower than that. The question this program answers
with real timings is not "does a timeout fire" but "does the RIGHT one
fire, at the right moment, leaving enough time to answer".

Four configurations, all against the same slow chain:

  1. NO TIMEOUT      - `requests` as shipped. There is no timeout. None.
                       The caller waits for the whole chain.
  2. FLAT            - the same generous timeout at every hop. Fires, but
                       only after the caller's budget is already gone.
  3. BUDGET          - a Deadline created from the incoming request,
                       passed down, each hop getting min(remaining -
                       reserve, cap). Fails fast, and leaves time to write
                       an answer.
  4. BUDGET + HEDGE  - the budget, plus a reserve that is actually used:
                       when the deadline is blown we return a degraded
                       response instead of an error, inside the promise.

The three servers are real HTTP servers in this process, chained: hop1
calls hop2 calls hop3, so the propagation is genuine rather than
simulated.

What to look for in the output:
  - "caller waited" against the OUTER_BUDGET line. Only configs 3 and 4
    keep the promise.
  - the per-hop budget arithmetic printed by config 3. That arithmetic is
    the whole topic.

Run: python3 deadline_budget.py
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import requests

OUTER_BUDGET = 1.5     # what we promised our caller, in seconds
RESPONSE_RESERVE = 0.1  # time we keep back to serialize and write our answer
HOP_LATENCY = 0.9       # each upstream hop is this slow today
FLAT_TIMEOUT = 5.0      # the "we set timeouts, we're fine" configuration


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A client whose deadline fired closes the socket while this handler is
        # still writing. That is not a server fault -- it is the entire subject
        # of this file, seen from the server side -- and socketserver's default
        # is to print a full traceback for it, which buries the report below in
        # noise. Absorb exactly those two errors and let anything else through.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            self.abandoned_by_client += 1
            return
        super().handle_error(request, client_address)

    abandoned_by_client = 0


def make_handler(latency, next_url, session):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            time.sleep(latency)
            if next_url:
                # A real chained service. The deadline header is propagated
                # here if the caller sent one -- this is the server half of
                # deadline propagation, and it is the half people skip.
                headers = {}
                deadline = self.headers.get("X-Request-Deadline")
                timeout = None
                if deadline:
                    remaining = float(deadline) - time.time()
                    if remaining <= 0:
                        self.reply(504, b'{"error":"deadline exceeded before call"}')
                        return
                    headers["X-Request-Deadline"] = deadline
                    timeout = remaining
                try:
                    session.get(next_url, headers=headers, timeout=timeout)
                except Exception as exc:                       # noqa: BLE001
                    self.reply(504, f'{{"error":"{type(exc).__name__}"}}'.encode())
                    return
            self.reply(200, b'{"ok":true}')

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def start(latency, next_url, session):
    server = Server(("127.0.0.1", 0), make_handler(latency, next_url, session))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


class Deadline:
    """The whole pattern, in fifteen lines.

    Built once, at the edge, from whatever the caller gave you (a header, a
    gRPC deadline, or a default). Passed down. Every outgoing call asks it
    how much time is left, minus what you must keep back to answer.
    """

    def __init__(self, budget, reserve=RESPONSE_RESERVE):
        self.expires_at = time.monotonic() + budget
        self.reserve = reserve

    @classmethod
    def from_header(cls, header_value, default_budget):
        if header_value:
            remaining = float(header_value) - time.time()
            return cls(max(remaining, 0.0))
        return cls(default_budget)

    def remaining(self):
        return self.expires_at - time.monotonic()

    def for_call(self, cap=None):
        """Time to give one outgoing call. Never more than what is left."""
        budget = self.remaining() - self.reserve
        if cap is not None:
            budget = min(budget, cap)
        return budget

    def expired(self):
        return self.remaining() <= 0


def run(label, call):
    print(f"  {label}")
    started = time.monotonic()
    outcome = call()
    elapsed = time.monotonic() - started
    kept = "KEPT" if elapsed <= OUTER_BUDGET else "BROKEN"
    print(f"    caller waited          {elapsed:.2f}s   promise {OUTER_BUDGET:.2f}s -> {kept}")
    print(f"    result                 {outcome}")
    return elapsed


def main():
    # One session for the chained servers so they are not also demonstrating
    # Topic 1's bug while we try to measure Topic 3's.
    chain_session = requests.Session()
    hop3, hop3_url = start(HOP_LATENCY, None, chain_session)
    hop2, hop2_url = start(HOP_LATENCY, hop3_url, chain_session)
    hop1, hop1_url = start(HOP_LATENCY, hop2_url, chain_session)

    print("=" * 78)
    print("A timeout budget, spent down a three-hop chain")
    print("=" * 78)
    print(f"  each hop sleeps {HOP_LATENCY}s, so the chain needs "
          f"~{HOP_LATENCY * 3:.1f}s to answer")
    print(f"  we promised our caller {OUTER_BUDGET}s")
    import inspect
    default_timeout = inspect.signature(requests.Session.request).parameters["timeout"].default
    print(f"  requests.Session.request(timeout=...) default is {default_timeout!r} "
          f"-- there is no timeout at all\n")

    session = requests.Session()

    # ---------------------------------------------------------------- 1
    def no_timeout():
        try:
            # This is the single most consequential default in the Python
            # ecosystem. `requests.get(url)` waits forever.
            response = session.get(hop1_url)
            return f"HTTP {response.status_code} (we waited for the whole chain)"
        except Exception as exc:                                  # noqa: BLE001
            return f"{type(exc).__name__}"

    run("1. NO TIMEOUT - requests.get(url), as shipped", no_timeout)
    print("    Nothing failed. That is the problem: the caller's budget was")
    print("    blown and this service never noticed, because nothing in it was")
    print("    watching a clock. Every one of those waits is holding a worker,")
    print("    a pool slot and a socket (Topic 2).")

    # ---------------------------------------------------------------- 2
    print()

    def flat():
        try:
            response = session.get(hop1_url, timeout=FLAT_TIMEOUT)
            return f"HTTP {response.status_code} after a {FLAT_TIMEOUT}s timeout never fired"
        except Exception as exc:                                  # noqa: BLE001
            return f"{type(exc).__name__}"

    run(f"2. FLAT {FLAT_TIMEOUT}s - a timeout at every hop, the same at every hop", flat)
    print(f"    A {FLAT_TIMEOUT}s timeout on a {OUTER_BUDGET}s promise is not a timeout,")
    print("    it is a formality. An inner call must never be allowed to outlive")
    print("    the request waiting on it -- that is the rule the flat config breaks.")

    # ---------------------------------------------------------------- 3
    print()

    def budget():
        deadline = Deadline(OUTER_BUDGET)
        per_call = deadline.for_call()
        print(f"    budget arithmetic:     promised {OUTER_BUDGET:.2f}s")
        print(f"                           reserve for our own response "
              f"-{RESPONSE_RESERVE:.2f}s")
        print(f"                           -> this call gets {per_call:.2f}s")
        try:
            response = session.get(
                hop1_url,
                # Propagate an absolute deadline, not a duration. A duration
                # restarts the clock at every hop, which is how three hops
                # each "respecting the 1.4s timeout" take 4.2s.
                headers={"X-Request-Deadline": str(time.time() + per_call)},
                timeout=per_call,
            )
            return f"HTTP {response.status_code}"
        except requests.exceptions.Timeout:
            return (f"Timeout after {per_call:.2f}s -- failed fast, with "
                    f"{RESPONSE_RESERVE:.2f}s left to answer the caller")
        except Exception as exc:                                  # noqa: BLE001
            return f"{type(exc).__name__}"

    run("3. BUDGET - an absolute deadline, propagated, spent down", budget)

    # ---------------------------------------------------------------- 4
    print()

    def budget_with_fallback():
        deadline = Deadline(OUTER_BUDGET)
        try:
            session.get(
                hop1_url,
                headers={"X-Request-Deadline": str(time.time() + deadline.for_call())},
                timeout=deadline.for_call(),
            )
            return "HTTP 200 (fresh)"
        except requests.exceptions.Timeout:
            # The reserve exists precisely so this branch has time to run.
            # A deadline you cannot act on is just a different error message.
            if not deadline.expired():
                return (f"200 DEGRADED - served a cached/partial answer with "
                        f"{deadline.remaining():.2f}s to spare")
            return "504 - deadline gone, nothing left to do"

    run("4. BUDGET + a reserve you actually use", budget_with_fallback)

    print()
    print("  The httpx version of the same thing, for the async stack you run:")
    print("""
    async def handler(request: Request):
        deadline = Deadline.from_header(
            request.headers.get("x-request-deadline"), default_budget=3.0
        )
        try:
            response = await request.app.state.http.get(
                UPSTREAM,
                headers={"X-Request-Deadline": str(time.time() + deadline.for_call())},
                # httpx takes the four separately. connect should be SMALL:
                # a healthy host inside your VPC connects in single-digit ms.
                timeout=httpx.Timeout(deadline.for_call(cap=2.0), connect=1.0),
            )
        except httpx.TimeoutException:
            ...
""")
    print(f"  (httpx default timeout for comparison: "
          f"{httpx.Timeout(5.0)} -- 5s on all four axes, which is a genuinely")
    print("   good default and still four times your budget in the example above.)")

    print()
    print("  What would mean this run is broken rather than your prediction wrong:")
    print("    - config 1 finishes in well under 3x HOP_LATENCY: the hops are not")
    print("      really chained; check that hop1 calls hop2 calls hop3.")
    print("    - config 3 takes as long as config 2: the deadline was passed as a")
    print("      duration rather than an absolute time, so each hop restarted it.")
    print("    - everything returns instantly: the servers are answering from a")
    print("      keep-alive connection without sleeping. Check HOP_LATENCY.")

    for server in (hop1, hop2, hop3):
        server.shutdown()


if __name__ == "__main__":
    main()
