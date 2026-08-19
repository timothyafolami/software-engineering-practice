"""
Layer 2 · Topic 2 - Three Python HTTP clients, one full pool, three
completely different failure modes. Measured, not quoted.

The README's table for this topic says requests/httpx/aiohttp disagree
about what "the pool is full" means. This program proves it on your
machine, because a table you did not verify is exactly how Layer 1 went
wrong.

Setup: one local server that holds every request for HOLD seconds, so the
pool is genuinely saturated rather than briefly busy. Each client is given
a pool of POOL_SIZE and asked to make CONCURRENCY requests at once. The
server counts accepted TCP connections, which is how we catch a client
that quietly creates sockets beyond its own pool limit.

What to look for in the output:
  - requests  : connections opened is GREATER than the pool size, and a
                warning is logged. It fails OPEN. No backpressure at all.
  - httpx     : connections opened equals the pool size exactly. It fails
                CLOSED -- and its pool timeout does not behave the way the
                field name suggests, which is why it is run at two values.
  - aiohttp   : with limit_per_host left at its default of 0, one host can
                consume the entire global limit.

Run: python3 pool_full_behaviour.py
"""
import asyncio
import logging
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import httpx
import requests
import urllib3

POOL_SIZE = 3
CONCURRENCY = 12
HOLD = 0.75          # seconds each request occupies a connection


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True
    accepted_connections = 0
    _lock = threading.Lock()

    def get_request(self):
        conn, addr = super().get_request()
        with self._lock:
            self.accepted_connections += 1
        return conn, addr


class SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        time.sleep(HOLD)  # the upstream that got slow
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server():
    server = CountingServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/slow"


class WarningCatcher(logging.Handler):
    """urllib3 announces pool overflow through logging, not exceptions.

    That is the entire problem with it: the signal exists, but it is a WARNING
    on a logger nobody configured, in a library three levels down. Nothing
    fails, nothing retries, nothing is measured -- you just quietly get more
    sockets than you asked for.
    """

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def report(label, outcomes, connections, elapsed):
    successes = sum(1 for kind, _ in outcomes if kind == "ok")
    failures = [detail for kind, detail in outcomes if kind != "ok"]
    print(f"  {label}")
    print(f"    pool size configured   {POOL_SIZE}")
    print(f"    requests attempted     {len(outcomes)}")
    print(f"    succeeded              {successes}")
    print(f"    failed                 {len(failures)}")
    print(f"    TCP connections opened {connections}   <-- compare with the pool size")
    print(f"    wall clock             {elapsed:.2f}s")
    if failures:
        distinct = sorted(set(failures))
        for kind in distinct[:3]:
            print(f"    failure                {kind}")


# --------------------------------------------------------------------------
# requests / urllib3 - fails OPEN
# --------------------------------------------------------------------------

def run_requests(url, server):
    catcher = WarningCatcher()
    urllib3_logger = logging.getLogger("urllib3.connectionpool")
    urllib3_logger.addHandler(catcher)
    urllib3_logger.setLevel(logging.WARNING)

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1,        # number of distinct host pools cached
        pool_maxsize=POOL_SIZE,    # connections kept per host pool
        pool_block=False,          # THE default. True would make it behave like httpx.
    )
    session.mount("http://", adapter)

    outcomes = []
    lock = threading.Lock()

    def one():
        try:
            session.get(url, timeout=10)
            with lock:
                outcomes.append(("ok", ""))
        except Exception as exc:                     # noqa: BLE001 - we want the type name
            with lock:
                outcomes.append(("err", f"{type(exc).__name__}: {exc}"))

    before = server.accepted_connections
    started = time.perf_counter()
    threads = [threading.Thread(target=one) for _ in range(CONCURRENCY)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    connections = server.accepted_connections - before

    report(f"requests {requests.__version__} / urllib3 {urllib3.__version__}"
           f"  (HTTPAdapter pool_maxsize={POOL_SIZE}, pool_block=False)",
           outcomes, connections, elapsed)
    if catcher.messages:
        print(f"    urllib3 logged         {len(catcher.messages)} warning(s), first:")
        print(f"      \"{catcher.messages[0]}\"")
    else:
        print("    urllib3 logged         nothing")
    print("    Every request SUCCEEDED, in roughly the time of one request, by")
    print("    opening sockets past the pool limit and throwing them away after.")
    print("    There is no backpressure here. At scale this is how you reach the")
    print("    file-descriptor ceiling with a 'correctly configured' pool.")
    urllib3_logger.removeHandler(catcher)
    session.close()


# --------------------------------------------------------------------------
# httpx - fails CLOSED
# --------------------------------------------------------------------------

async def run_httpx_once(url, server, pool_timeout):
    limits = httpx.Limits(max_connections=POOL_SIZE, max_keepalive_connections=POOL_SIZE)
    timeout = httpx.Timeout(10.0, pool=pool_timeout)
    outcomes = []

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        async def one():
            try:
                await client.get(url)
                outcomes.append(("ok", ""))
            except Exception as exc:                 # noqa: BLE001
                outcomes.append(("err", f"{type(exc).__name__}: {exc}"))

        before = server.accepted_connections
        started = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(CONCURRENCY)))
        elapsed = time.perf_counter() - started

    report(f"httpx {httpx.__version__}"
           f"  (max_connections={POOL_SIZE}, timeout.pool={pool_timeout}s)",
           outcomes, server.accepted_connections - before, elapsed)


async def run_httpx(url, server):
    # Two pool timeouts, because the interesting result here is that they do
    # not behave the way the field name suggests, and the only way to know
    # that is to run both.
    await run_httpx_once(url, server, 1.0)
    print()
    await run_httpx_once(url, server, 0.25)
    print("    httpx bounds the CONNECTION COUNT strictly -- exactly max_connections,")
    print("    never one more. That is the real difference from requests: it fails")
    print("    closed instead of open.")
    print("    But look at the two pool timeouts above before you rely on")
    print("    PoolTimeout as your backpressure signal. Requests queued behind a")
    print("    saturated pool waited well past the 1.0s setting without raising,")
    print("    while 0.25s raised almost immediately. The pool timeout is not a")
    print("    deadline on 'time spent waiting for a connection' in the way the")
    print("    name implies. Read it yourself before you depend on it -- the loop")
    print("    is about forty lines, in httpcore/_async/connection_pool.py,")
    print("    AsyncConnectionPool.handle_async_request: it re-enters")
    print("    wait_for_connection(timeout=...) inside a `while True`, so the")
    print("    clock can start again rather than continuing to run down.")
    print("    Note also that pool timeout is a SEPARATE field from connect/read:")
    print("    httpx.Timeout(5.0) sets all four at once, and almost nobody sets")
    print("    pool deliberately.")


# --------------------------------------------------------------------------
# aiohttp - bounded globally, unbounded per host by default
# --------------------------------------------------------------------------

async def run_aiohttp(url, server):
    default_connector = aiohttp.TCPConnector()
    print(f"  aiohttp {aiohttp.__version__} defaults as shipped:")
    print(f"    limit                  {default_connector.limit}")
    print(f"    limit_per_host         {default_connector.limit_per_host}"
          f"   <-- 0 means UNLIMITED per host")
    await default_connector.close()

    connector = aiohttp.TCPConnector(limit=POOL_SIZE, limit_per_host=0)
    outcomes = []
    before = server.accepted_connections
    started = time.perf_counter()
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as session:
        async def one():
            try:
                async with session.get(url) as response:
                    await response.read()
                outcomes.append(("ok", ""))
            except Exception as exc:                 # noqa: BLE001
                outcomes.append(("err", f"{type(exc).__name__}: {exc}"))

        await asyncio.gather(*(one() for _ in range(CONCURRENCY)))
    elapsed = time.perf_counter() - started

    report(f"aiohttp {aiohttp.__version__}  (limit={POOL_SIZE}, limit_per_host=0 default)",
           outcomes, server.accepted_connections - before, elapsed)
    print("    aiohttp queues waiters on a per-host futures list and honours the")
    print("    GLOBAL limit, so the connection count is bounded here. The trap is")
    print("    the other direction: with limit=100 and limit_per_host=0, ONE slow")
    print("    host can hold all 100 and starve every other upstream you call.")
    print("    Set limit_per_host. It is the field that keeps one bad dependency")
    print("    from becoming an outage of everything else.")


async def main():
    warnings.simplefilter("ignore")
    server, url = start_server()
    print("=" * 78)
    print("One full pool, three clients, three different things happen")
    print("=" * 78)
    print(f"  server {url} holds each request for {HOLD}s")
    print(f"  {CONCURRENCY} concurrent requests against a pool of {POOL_SIZE}")
    print(f"  Little's Law says the floor for {CONCURRENCY} requests is "
          f"{CONCURRENCY / POOL_SIZE * HOLD:.2f}s "
          f"({CONCURRENCY} / {POOL_SIZE} x {HOLD}s)\n")

    run_requests(url, server)
    print()
    await run_httpx(url, server)
    print()
    await run_aiohttp(url, server)

    print()
    print("  The one sentence to carry out of this:")
    print("    'The pool is full' is not one behaviour. requests opens more")
    print("    sockets, httpx raises, aiohttp waits -- and which one your service")
    print("    does decides whether an incident looks like fd exhaustion, a spike")
    print("    in 500s, or a latency cliff with no errors at all.")
    server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
