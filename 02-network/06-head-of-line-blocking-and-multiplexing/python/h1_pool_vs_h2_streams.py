"""
Layer 2 · Topic 6 - Switching to HTTP/2 does not remove the pool limit. It
renames it, and hands the new one to somebody else's config file.

Under HTTP/1.1 your concurrency ceiling is the number of connections in your
pool -- a number you set, in your code, and can see with `ss`. Under HTTP/2
the pool holds roughly ONE connection per origin and your ceiling becomes
SETTINGS_MAX_CONCURRENT_STREAMS, which the SERVER announces in a frame you
never see, which is invisible to `ss`, invisible to your connection-count
dashboard, and set by whoever runs the other service.

This program runs the identical fan-out against two servers in this process:

  h1   an HTTP/1.1 keep-alive server, with httpx limited to POOL connections
  h2   an HTTP/2 (h2c, prior knowledge) server that advertises
       SETTINGS_MAX_CONCURRENT_STREAMS = STREAM_LIMIT, with httpx on ONE
       connection

Both servers hold every request for DELAY seconds, so the elapsed time tells
you directly how many requests were genuinely in flight at once:

    effective concurrency  =  requests x DELAY / wall time

What to look for in the output:
  - connections accepted: POOL for h1, one for h2. Your dashboard shows this
    dropping to 1 and you conclude the pool problem is solved.
  - effective concurrency: it lands on the pool size for h1 and on the
    advertised stream limit for h2. The ceiling did not go away. It moved
    into a frame nobody in your organisation configured.
  - the "who sets it" column at the end. That is the whole topic.

What this program deliberately does NOT measure: the head-of-line blocking
half. TCP-level HOL blocking needs real packet loss on the path, and loopback
has none. Do that half in the lab with `tc netem loss 5%` in the `sniff`
sidecar, as the topic README describes -- and record "not measured here"
rather than inferring it from these numbers.

Needs: httpx[http2] (pip install -r requirements.txt)
Run:   python3 h1_pool_vs_h2_streams.py
"""
import asyncio
import time

import httpx

try:
    import h2.config
    import h2.connection
    import h2.events
except ImportError:  # pragma: no cover
    raise SystemExit(
        "This program needs the h2 package: pip install -r requirements.txt\n"
        "(httpx[http2] pulls it in; without it httpx silently stays on HTTP/1.1,\n"
        " which would make this comparison measure nothing.)"
    )

REQUESTS = 40
DELAY = 0.25          # how long the server holds every request
POOL = 8              # HTTP/1.1 connection pool size
STREAM_LIMIT = 5      # what the h2 server advertises as its stream maximum
BODY = b"x" * 1024


class Counters:
    def __init__(self):
        self.connections = 0
        self.max_concurrent = 0
        self.in_flight = 0
        self.served = 0

    def start(self):
        self.in_flight += 1
        self.served += 1
        self.max_concurrent = max(self.max_concurrent, self.in_flight)

    def finish(self):
        self.in_flight -= 1


# --------------------------------------------------------------- HTTP/1.1 --
async def h1_server(counters: Counters):
    async def handle(reader, writer):
        counters.connections += 1
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                while True:                       # drain headers
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                counters.start()
                await asyncio.sleep(DELAY)
                counters.finish()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                    b"Connection: keep-alive\r\n\r\n%s" % (len(BODY), BODY)
                )
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


# ----------------------------------------------------------------- HTTP/2 --
async def h2_server(counters: Counters, max_streams: int):
    """A minimal h2c server, written out rather than imported, because the one
    setting this topic is about is a field in the SETTINGS frame and importing
    a framework would hide it."""

    async def handle(reader, writer):
        counters.connections += 1
        config = h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        conn = h2.connection.H2Connection(config=config)
        conn.initiate_connection()
        # THE LINE THIS WHOLE TOPIC IS ABOUT. One number, sent by the server,
        # in a frame the client never surfaces to you, and it is now your
        # concurrency ceiling.
        conn.update_settings({
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: max_streams,
        })
        writer.write(conn.data_to_send())
        await writer.drain()

        lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        async def respond(stream_id: int):
            counters.start()
            await asyncio.sleep(DELAY)
            counters.finish()
            async with lock:
                try:
                    conn.send_headers(stream_id, [
                        (":status", "200"),
                        ("content-length", str(len(BODY))),
                    ])
                    conn.send_data(stream_id, BODY, end_stream=True)
                    writer.write(conn.data_to_send())
                except h2.exceptions.StreamClosedError:
                    return
            await writer.drain()

        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                async with lock:
                    events = conn.receive_data(data)
                    out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
                for event in events:
                    if isinstance(event, h2.events.RequestReceived):
                        t = asyncio.create_task(respond(event.stream_id))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            for t in tasks:
                t.cancel()
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def drive(client: httpx.AsyncClient, url: str, gate: asyncio.Semaphore | None = None):
    async def one():
        if gate is None:
            return await client.get(url)
        async with gate:
            return await client.get(url)

    t0 = time.perf_counter()
    responses = await asyncio.gather(*(one() for _ in range(REQUESTS)),
                                     return_exceptions=True)
    elapsed = time.perf_counter() - t0
    ok = [r for r in responses if not isinstance(r, BaseException)]
    failures = [r for r in responses if isinstance(r, BaseException)]
    return elapsed, len(ok), failures


def report(label, elapsed, ok, failures, counters, ceiling_name, ceiling_value, http_version):
    print(f"    {label}")
    print(f"      negotiated              {http_version}")
    print(f"      wall time               {elapsed:.2f} s")
    print(f"      succeeded / failed      {ok} / {len(failures)}")
    if failures:
        print(f"      first failure           {type(failures[0]).__name__}: {failures[0]}")
    print(f"      connections accepted    {counters.connections}")
    print(f"      max concurrent at server{counters.max_concurrent:>4}")
    if failures:
        print("      effective concurrency   NOT COMPUTED -- requests failed, so wall")
        print("                              time measures rejection, not throughput")
    else:
        print(f"      effective concurrency   {ok * DELAY / elapsed:.1f}   "
              f"(= {ok} x {DELAY}s / {elapsed:.2f}s)")
    print(f"      ceiling                 {ceiling_value}  ({ceiling_name})")
    print()


async def main():
    print("=" * 78)
    print("HTTP/2 did not remove your pool limit. It renamed it.")
    print("=" * 78)
    print(f"  {REQUESTS} concurrent requests, server holds each for {DELAY}s")
    print(f"  h1 pool size {POOL}   h2 advertised SETTINGS_MAX_CONCURRENT_STREAMS {STREAM_LIMIT}")
    print()

    c1 = Counters()
    s1, p1 = await h1_server(c1)
    c2 = Counters()
    s2, p2 = await h2_server(c2, STREAM_LIMIT)

    print("  Three runs, identical workload:")
    print()

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=POOL, max_keepalive_connections=POOL),
        timeout=60.0,
    ) as client:
        elapsed, ok, failures = await drive(client, f"http://127.0.0.1:{p1}/work")
        version = (await client.get(f"http://127.0.0.1:{p1}/work")).http_version
        report("h1, pool of %d" % POOL, elapsed, ok, failures, c1,
               "your httpx Limits(max_connections)", POOL, version)

    # http1=False is what makes httpx use HTTP/2 with prior knowledge over
    # cleartext. With http1 left enabled, httpx negotiates h2 only via ALPN
    # over TLS -- and silently stays on HTTP/1.1 here, which is the single
    # most common way to run this comparison and measure nothing.
    async with httpx.AsyncClient(
        http1=False, http2=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
        timeout=60.0,
    ) as client:
        elapsed, ok, failures = await drive(client, f"http://127.0.0.1:{p2}/work")
        version = (await client.get(f"http://127.0.0.1:{p2}/work")).http_version
        report("h2, max_connections=100, nothing bounding streams",
               elapsed, ok, failures, c2,
               "the SERVER's SETTINGS_MAX_CONCURRENT_STREAMS", STREAM_LIMIT, version)
        h2_failures = failures

        # The same run with a client-side gate of exactly the advertised limit.
        # This is the fix, and note what it costs you: a constant you had to
        # learn from somewhere else and now have to keep in sync by hand.
        c2.max_concurrent = 0
        elapsed, ok, failures = await drive(
            client, f"http://127.0.0.1:{p2}/work", gate=asyncio.Semaphore(STREAM_LIMIT))
        report("h2, with a client-side semaphore of %d" % STREAM_LIMIT,
               elapsed, ok, failures, c2,
               "the same limit, now enforced by you", STREAM_LIMIT, version)

    s1.close()
    s2.close()

    print("  For this topic's second table:")
    print()
    print("    %-22s %-20s %-24s %s" % ("client", "conns to upstream", "concurrency ceiling", "who sets it"))
    print("    %-22s %-20d %-24d %s" % ("httpx h1", c1.connections, POOL, "you, in Limits()"))
    print("    %-22s %-20d %-24d %s" % ("httpx h2", c2.connections, STREAM_LIMIT, "the server, in a SETTINGS frame"))
    print()
    if h2_failures:
        print("  The h2 run without a semaphore did not QUEUE. It RAISED:")
        print(f"    {type(h2_failures[0]).__name__}: {h2_failures[0]}")
        print()
        print("  That is worth more than the table. httpx's pool limit and the server's")
        print("  stream limit are two different limits, and httpx enforces only the one")
        print("  it owns: it will happily open 40 streams on a connection whose peer said")
        print("  five, and then fail locally. Go's HTTP/2 transport makes the opposite")
        print("  choice and QUEUES the excess on the same connection -- same protocol,")
        print("  same SETTINGS frame, two client libraries, and one of them turns your")
        print("  overload into errors while the other turns it into invisible latency.")
        print("  Neither of them tells you the number.")
        print()
    print("  Notice what a connection-count dashboard would have told you: the h2 run")
    print("  uses ONE connection where h1 used %d. That looks like a pure win, and the" % c1.connections)
    print("  ceiling it replaced is not on any graph you own. `ss -tan` cannot see a")
    print("  stream. Neither can your pool metrics, because the pool is no longer where")
    print("  the queueing happens -- Topic 2's queue moved INTO the connection.")
    print()
    print("  How to find the real number in production:")
    print("    - read the server's SETTINGS frame from a capture (Topic 7), or")
    print("    - ask the team that runs it what their h2 max concurrent streams is,")
    print("      and watch how long it takes them to find out. That delay is the")
    print("      point: it is a limit on your service, set in someone else's config.")
    print()
    print("  Not measured here, on purpose: head-of-line blocking. That needs real")
    print("  packet loss on the path, and loopback has none. Run the lab half with")
    print("  `tc netem loss 5%` and record those rows separately.")


if __name__ == "__main__":
    asyncio.run(main())
