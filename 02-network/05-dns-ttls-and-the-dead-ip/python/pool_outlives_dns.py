"""
Layer 2 · Topic 5 - The cache that kills you is not the DNS cache.

The name moves. DNS is correct within a second. The client keeps talking to
the old address for as long as it holds an open socket to it -- because an
established TCP connection does not consult DNS, ever, and nothing will make
it except closing it.

This program is that failure with the DNS lookup made explicit so you can
watch every layer at once:

  - two servers, A and B, on two loopback ports. Two ports standing in for
    two IP addresses is the only simulated thing here; the sockets, the
    pools, the failure and the recovery are all real.
  - a `Resolver` with a real TTL cache, so you can see the TTL doing its job
    and see that it is NOT what is broken.
  - four clients with four connection-lifetime policies, hitting the name
    under steady load while the mapping is switched mid-run and server A is
    then killed.

The measurement is the ERROR WINDOW: seconds from the switch until that
client serves a successful request again, and whether it ever does.

What to look for in the output:
  - the resolver picks up the new address almost immediately in every case.
    DNS is not the problem and never was.
  - `pool_forever` never recovers. Not slowly. Never. Its socket is fine and
    its peer is answering -- with the wrong answer -- so nothing ever forces
    it to look the name up again. No amount of TTL tuning reaches it.
  - `idle_expiry` does no better than `pool_forever` under steady load,
    because a connection in constant use is never idle. That is not a bug in
    the experiment; it is why "lower the idle expiry" is the wrong fix.
  - `max_lifetime` recovers within a bounded time you chose, under load, with
    no gap required. That is the actual fix.

Run: python3 pool_outlives_dns.py
"""
import socket
import threading
import time

TTL = 1.0                 # seconds the resolver caches an answer
SWITCH_AT = 2.0           # seconds into the run when the name is re-pointed
RUN_FOR = 8.0
REQUEST_EVERY = 0.1
IDLE_EXPIRY = 1.5         # httpx's keepalive_expiry, scaled down
MAX_LIFETIME = 2.0        # the fix: retire a connection after this long, always


class Server(threading.Thread):
    """A keep-alive HTTP/1.1 server that names itself in every response."""

    daemon = True

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.served = 0
        self.demoted = False
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            conn.settimeout(30)
            while not self._stop.is_set():
                try:
                    if not conn.recv(4096):
                        return
                except OSError:
                    return
                self.served += 1
                # A demoted instance is the realistic failure. It does NOT
                # close your socket -- that is the entire problem. The old RDS
                # writer is still listening, still healthy at the TCP layer,
                # and now a read-only replica. Nothing about the connection
                # says anything is wrong, so nothing forces a re-resolve.
                if self.demoted:
                    body = (self.name + " (demoted: read-only)").encode()
                    status = b"HTTP/1.1 503 Service Unavailable"
                else:
                    body = self.name.encode()
                    status = b"HTTP/1.1 200 OK"
                try:
                    conn.sendall(status + b"\r\nContent-Length: %d\r\n"
                                 b"Connection: keep-alive\r\n\r\n%s" % (len(body), body))
                except OSError:
                    return

    def stop(self):
        self._stop.set()
        self.sock.close()


class Resolver:
    """A resolver that honours a TTL -- like CoreDNS, or a node-local cache,
    and unlike your process, which caches nothing at all in Python."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self.records: dict[str, int] = {}
        self.cache: dict[str, tuple[int, float]] = {}
        self.queries = 0
        self.cache_hits = 0

    def set_record(self, name: str, port: int):
        self.records[name] = port

    def resolve(self, name: str) -> int:
        hit = self.cache.get(name)
        if hit and time.monotonic() < hit[1]:
            self.cache_hits += 1
            return hit[0]
        self.queries += 1
        port = self.records[name]
        self.cache[name] = (port, time.monotonic() + self.ttl)
        return port


class Client:
    """One pooled connection with a lifetime policy. This is urllib3, httpx,
    undici and your Go transport, with the policy made visible."""

    def __init__(self, name, resolver, host, idle_expiry=None, max_lifetime=None):
        self.name = name
        self.resolver = resolver
        self.host = host
        self.idle_expiry = idle_expiry
        self.max_lifetime = max_lifetime
        self.conn = None
        self.opened_at = 0.0
        self.last_used = 0.0
        self.handshakes = 0
        self.results: list[tuple[float, bool, str]] = []

    def _should_retire(self, now) -> str | None:
        if self.conn is None:
            return None
        if self.max_lifetime is not None and now - self.opened_at > self.max_lifetime:
            return "max lifetime"
        if self.idle_expiry is not None and now - self.last_used > self.idle_expiry:
            return "idle expiry"
        return None

    def request(self, now: float):
        reason = self._should_retire(now)
        if reason:
            self.conn.close()
            self.conn = None

        if self.conn is None:
            # The ONLY moment DNS is consulted. Everything between two of
            # these moments is pinned to whatever the answer was.
            port = self.resolver.resolve(self.host)
            try:
                self.conn = socket.create_connection(("127.0.0.1", port), timeout=1.0)
                self.opened_at = now
                self.handshakes += 1
            except OSError as e:
                self.results.append((now, False, f"connect: {type(e).__name__}"))
                self.conn = None
                return

        try:
            self.conn.sendall(b"GET /work HTTP/1.1\r\nHost: %s\r\n\r\n" % self.host.encode())
            data = self.conn.recv(4096)
            if not data:
                raise ConnectionResetError("empty read")
            head, _, body = data.partition(b"\r\n\r\n")
            served_by = body.decode()
            self.last_used = now
            # A 503 is an APPLICATION failure on a perfectly healthy socket.
            # We keep the connection, exactly as every real pool does, because
            # nothing at the transport layer went wrong.
            ok = head.startswith(b"HTTP/1.1 200")
            self.results.append((now, ok, served_by))
        except OSError as e:
            self.conn.close()
            self.conn = None
            self.results.append((now, False, type(e).__name__))


def error_window(results, switch_at):
    """Seconds from the switch until this client next succeeds against B."""
    for t, ok, detail in results:
        if t >= switch_at and ok and detail == "B":
            return t - switch_at
    return None


def real_dns_note():
    print("  Before the experiment: what your OS actually does")
    print("  " + "-" * 74)
    import platform
    if platform.system() == "Darwin":
        print("    macOS: /etc/resolv.conf is NOT consulted for hostname resolution.")
        print("    Resolution goes through libinfo/mDNSResponder, so the ndots and")
        print("    `options` experiments in this topic's README only mean what they say")
        print("    inside the Linux container. Read the first lines of /etc/resolv.conf")
        print("    on this machine -- Apple put the warning there themselves.")
    else:
        print(f"    {platform.system()}: /etc/resolv.conf is consulted by glibc's resolver.")

    name = "example.com"
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        try:
            socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
        except OSError as e:
            print(f"    {name} did not resolve ({type(e).__name__}); skipping the timing.")
            print("    That is a recordable result, not a failure of the experiment.")
            print()
            return
        times.append((time.perf_counter() - t0) * 1000)
    print(f"    getaddrinfo('{name}') x3: " + ", ".join(f"{t:.1f} ms" for t in times))
    print("    Python caches none of those. Any speed-up you see between the first and")
    print("    the rest belongs to a resolver BETWEEN you and the authority -- the OS")
    print("    resolver daemon, a node-local cache, CoreDNS -- honouring the record's")
    print("    TTL on your behalf. Your process is not honouring anything.")
    print()


def main():
    print("=" * 78)
    print("The name moved. Who noticed, and when?")
    print("=" * 78)
    print()
    real_dns_note()

    a, b = Server("A"), Server("B")
    a.start()
    b.start()

    resolver = Resolver(ttl=TTL)
    resolver.set_record("upstream", a.port)

    clients = [
        Client("pool_forever", resolver, "upstream"),
        Client("idle_expiry", resolver, "upstream", idle_expiry=IDLE_EXPIRY),
        Client("max_lifetime", resolver, "upstream", max_lifetime=MAX_LIFETIME),
        Client("no_pool", resolver, "upstream", max_lifetime=0.0),
    ]

    print("  Running steady load, switching the name at t=%.1fs and killing A with it." % SWITCH_AT)
    print(f"    A on port {a.port}   B on port {b.port}   resolver TTL {TTL:.1f}s")
    print()

    start = time.monotonic()
    switched = False
    while True:
        now = time.monotonic() - start
        if now > RUN_FOR:
            break
        if not switched and now >= SWITCH_AT:
            resolver.set_record("upstream", b.port)
            # A is NOT killed. It is demoted -- still listening, still
            # answering, now wrong. That is what an RDS failover, a blue/green
            # cutover and a rescheduled pod all look like from a client's side,
            # and it is why the socket never errors and nothing re-resolves.
            a.demoted = True
            switched = True
            print(f"    t={now:4.1f}s  name repointed to B; A demoted (still listening, now read-only)")
            # Prove DNS itself is fine, immediately:
            time.sleep(TTL + 0.05)
            print(f"    t={time.monotonic() - start:4.1f}s  resolver now answers with port "
                  f"{resolver.resolve('upstream')} (B) -- DNS is already correct")
        for c in clients:
            c.request(time.monotonic() - start)
        time.sleep(REQUEST_EVERY)

    b.stop()
    time.sleep(0.05)

    print()
    print("  %-14s %10s %10s %10s %14s  %s" %
          ("client", "requests", "ok", "failed", "handshakes", "error window after the switch"))
    for c in clients:
        ok = sum(1 for _, o, _ in c.results if o)
        bad = len(c.results) - ok
        w = error_window(c.results, SWITCH_AT)
        window = "never recovered" if w is None else f"{w:.1f}s"
        print("  %-14s %10d %10d %10d %14d  %s" %
              (c.name, len(c.results), ok, bad, c.handshakes, window))

    print()
    print(f"  resolver: {resolver.queries} queries, {resolver.cache_hits} served from its TTL cache")
    print()
    print("  The ranking that matters, worst first:")
    print("    1. THE CONNECTION POOL. pool_forever never recovers, and no TTL")
    print("       change anywhere in the world would have helped it. Its socket stayed")
    print("       perfectly healthy the entire time -- that is the problem, not an")
    print("       accident of this simulation. A demoted instance keeps answering, so")
    print("       nothing at the transport layer ever fails, so the client never")
    print("       reconnects, so it never asks DNS anything again.")
    print("    2. A client-side resolver cache with its own fixed TTL (aiohttp's")
    print("       ttl_dns_cache=10 by default), which ignores the record's TTL.")
    print("    3. CoreDNS and node-local caches, which do honour the TTL.")
    print("    4. Your process, which caches nothing at all -- and is therefore the")
    print("       layer people spend the incident tuning.")
    print()
    print("  idle_expiry is the fix everyone reaches for first, and look at its row:")
    print("  identical to pool_forever. A connection under steady load is NEVER idle,")
    print("  so an idle expiry of %.1fs never fires. It is a fix that works in staging," % IDLE_EXPIRY)
    print("  works in the incident review, and stops working exactly when you are busy")
    print("  enough for the outage to matter. httpx's keepalive_expiry=5.0 is this.")
    print()
    print("  max_lifetime recovers within a bound YOU chose, under any load, with no")
    print("  idle gap required. It costs one handshake per connection per %.1fs and it" % MAX_LIFETIME)
    print("  converts an unbounded outage into a bounded one. That is the whole fix,")
    print("  and it is the one setting most HTTP clients do not enable by default.")


if __name__ == "__main__":
    main()
