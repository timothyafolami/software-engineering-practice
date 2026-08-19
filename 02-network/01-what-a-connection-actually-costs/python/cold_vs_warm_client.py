"""
Layer 2 · Topic 1 - The cost of a client built inside the request handler.

Python first because this is the exact bug shape in a FastAPI codebase:

    @app.get("/thing")
    async def thing():
        async with httpx.AsyncClient() as client:   # <-- a pool of one,
            return (await client.get(UPSTREAM)).json()   # thrown away

Both variants below issue the same number of HTTP requests to the same
local server. The server counts how many TCP connections it had to accept.
That count is the portable evidence: it is what a SYN counter in tcpdump
would show you (Topic 7), measured from the other end so it needs no root.

What to look for in the output:
  - connections accepted by COLD  vs  connections accepted by WARM
  - the per-request latency difference, and the caveat printed under it:
    over loopback a handshake costs microseconds of CPU and zero network
    round trips. Across a real link it costs one RTT for TCP and one more
    for TLS. The latency number here is machine-specific; the connection
    COUNT is the thing that transfers to production.

Run: python3 cold_vs_warm_client.py
"""
import asyncio
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

REQUESTS = 200
CONCURRENCY = 10


class CountingServer(ThreadingHTTPServer):
    """A normal HTTP server that also counts accepted TCP connections.

    get_request() is called once per accept(2), so incrementing here counts
    connections, not requests. A keep-alive connection carrying 50 requests
    increments this exactly once -- which is the whole point.
    """

    daemon_threads = True
    # The default listen backlog is 5. With CONCURRENCY cold connects arriving
    # at once, a backlog of 5 makes the kernel refuse connections and you
    # measure your own listen queue instead of the client. This is a real
    # production trap in its own right (Layer 1, accept backlog).
    request_queue_size = 256
    allow_reuse_address = True
    accepted_connections = 0
    _lock = threading.Lock()

    def get_request(self):
        conn, addr = super().get_request()
        with self._lock:
            self.accepted_connections += 1
        return conn, addr


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # without this, http.server closes every connection

    def do_GET(self):
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # the server's own logging would dominate the measurement


def start_server():
    server = CountingServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/thing"


async def drive(client_factory, url, reuse_client):
    """Issue REQUESTS requests at CONCURRENCY in flight, timing each one.

    reuse_client=False rebuilds the client inside the "handler" -- the bug.
    reuse_client=True  uses one long-lived client -- the fix.
    """
    latencies = []
    semaphore = asyncio.Semaphore(CONCURRENCY)
    shared = client_factory() if reuse_client else None

    async def one():
        async with semaphore:
            started = time.perf_counter()
            if reuse_client:
                await shared.get(url)
            else:
                async with client_factory() as client:
                    await client.get(url)
            latencies.append((time.perf_counter() - started) * 1000)

    try:
        await asyncio.gather(*(one() for _ in range(REQUESTS)))
    finally:
        if shared is not None:
            await shared.aclose()
    return latencies


def percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def report(label, latencies, connections):
    print(f"  {label}")
    print(f"    requests issued        {len(latencies)}")
    print(f"    TCP connections opened {connections}")
    print(f"    requests per connection {len(latencies) / max(connections, 1):.1f}")
    print(f"    latency p50 {percentile(latencies, 0.50):7.3f} ms   "
          f"p95 {percentile(latencies, 0.95):7.3f} ms   "
          f"p99 {percentile(latencies, 0.99):7.3f} ms   "
          f"mean {statistics.mean(latencies):7.3f} ms")


async def main():
    server, url = start_server()
    print("=" * 78)
    print("A client built per request vs a client that outlives the request")
    print("=" * 78)
    print(f"  server: {url}   {REQUESTS} requests, {CONCURRENCY} in flight\n")

    before = server.accepted_connections
    cold = await drive(lambda: httpx.AsyncClient(), url, reuse_client=False)
    cold_connections = server.accepted_connections - before
    report("COLD  - httpx.AsyncClient() constructed inside the handler", cold, cold_connections)

    print()
    before = server.accepted_connections
    warm = await drive(lambda: httpx.AsyncClient(), url, reuse_client=True)
    warm_connections = server.accepted_connections - before
    report("WARM  - one httpx.AsyncClient, created once, reused", warm, warm_connections)

    print()
    before = server.accepted_connections
    tuned = await drive(
        lambda: httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=CONCURRENCY,
                max_keepalive_connections=CONCURRENCY,
                keepalive_expiry=30.0,
            )
        ),
        url,
        reuse_client=True,
    )
    tuned_connections = server.accepted_connections - before
    report("WARM_TUNED - same, with explicit Limits sized to the concurrency",
           tuned, tuned_connections)

    # Where did COLD's time actually go? Do not assume "the handshake" --
    # that is the assumption Layer 1 got burned by. Measure the two halves
    # separately: constructing the client object, and the connect itself.
    construct_ms = []
    for _ in range(20):
        started = time.perf_counter()
        client = httpx.AsyncClient()
        construct_ms.append((time.perf_counter() - started) * 1000)
        await client.aclose()

    connect_ms = []
    for _ in range(20):
        started = time.perf_counter()
        sock = await asyncio.open_connection("127.0.0.1", server.server_address[1])
        connect_ms.append((time.perf_counter() - started) * 1000)
        sock[1].close()

    print("\n  Where COLD's time actually went (measure, don't assume):")
    print(f"    constructing one httpx.AsyncClient()   {statistics.mean(construct_ms):7.3f} ms mean")
    print(f"    one bare TCP connect over loopback     {statistics.mean(connect_ms):7.3f} ms mean")
    print("    If the first number dominates, most of COLD's cost on THIS machine")
    print("    is building an SSL context and loading the CA bundle, not the")
    print("    network -- loopback has no round trip to pay for. Across a real")
    print("    30 ms link the second number becomes ~30 ms for TCP plus another")
    print("    ~30 ms for TLS, on every request, and it dominates instead.")
    print("    Both costs are real, both are removed by the same fix, and the")
    print("    connection count above is the evidence that transfers unchanged.")

    print("\n  httpx defaults on this machine (the numbers that decide the above):")
    from httpx._config import DEFAULT_LIMITS  # the real defaults live here
    print(f"    max_connections            {DEFAULT_LIMITS.max_connections}")
    print(f"    max_keepalive_connections  {DEFAULT_LIMITS.max_keepalive_connections}")
    print(f"    keepalive_expiry           {DEFAULT_LIMITS.keepalive_expiry}s")
    print(f"    httpx version              {httpx.__version__}")

    print("\n  The fix, in the shape you would actually ship it:")
    print("""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Built here, INSIDE the running loop. An AsyncClient constructed at
        # import time binds to whatever loop exists then -- which is how this
        # passes tests and fails in production.
        app.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=2.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        yield
        await app.state.http.aclose()

    app = FastAPI(lifespan=lifespan)
""")
    server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
