"""
Layer 2 · Topic 4 - The 502 nobody's logs can explain, reproduced in one
process in about a second.

uvicorn's --timeout-keep-alive default is 5 seconds. An AWS ALB's default
idle timeout is 60. Deploy FastAPI behind an ALB, change nothing, and the
backend is always the side that closes an idle connection the load balancer
is still holding in a pool it is about to reuse.

This program is that race with the numbers scaled down so you can watch it:
the "backend" closes idle connections after BACKEND_IDLE seconds, and the
"load balancer" is a client that keeps its pooled connection for
POOL_IDLE seconds and then writes a request onto it. Nothing here is
simulated -- these are two real sockets and a real FIN.

Three configurations, matching the three KEEPALIVE_PROFILE values in the
compose lab:

  mismatched  backend 0.3s, pool 1.0s   the deployment you get by default
  ordered     backend 2.0s, pool 1.0s   the rule: backend strictly longer
  bounded     ordered, plus the pool retiring connections after N requests

What to look for in the output:
  - mismatched: every reuse after the idle gap fails, and the failure is
    NOT an exception on the backend. The backend did exactly what it was
    told. So did the client. The bug lives in the gap between them.
  - ordered: zero failures, same code, one number changed.
  - the "defaults on this machine" block: read your own installed uvicorn
    rather than trusting this docstring or any blog post.

Run: python3 idle_timeout_defaults.py
"""
import socket
import threading
import time

BACKEND_IDLE_MISMATCHED = 0.3     # stands in for uvicorn's 5 s default
BACKEND_IDLE_ORDERED = 2.0        # stands in for --timeout-keep-alive 75
POOL_IDLE = 1.0                   # stands in for the LB's 60 s idle timeout
REQUESTS_PER_CONFIG = 5
KEEPALIVE_REQUESTS = 2            # for the bounded config


class Backend(threading.Thread):
    """A keep-alive HTTP/1.1 server that closes a connection after
    `idle` seconds of silence -- which is precisely what
    `uvicorn --timeout-keep-alive` does, and precisely what nginx's
    `keepalive_timeout` does on the other side."""

    daemon = True

    def __init__(self, idle: float):
        super().__init__()
        self.idle = idle
        self.closed_by_us = 0
        self.requests = 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        with conn:
            while True:
                conn.settimeout(self.idle)
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    # The idle timer fired. We send a FIN. This is correct
                    # behaviour, it is what we were configured to do, and it
                    # will appear in no log anywhere.
                    self.closed_by_us += 1
                    return
                if not data:
                    return
                self.requests += 1
                body = b"ok"
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                    b"Connection: keep-alive\r\n\r\n%s" % (len(body), body)
                )

    def stop(self):
        self._stop.set()
        self.sock.close()


class PooledClient:
    """A one-connection pool with its own idle timeout -- the load balancer,
    or urllib3, or undici, or your Go transport. It holds the connection and
    reuses it, which is the entire point of a pool and the entire cause of
    this bug."""

    def __init__(self, port: int, max_requests: int | None = None):
        self.port = port
        self.max_requests = max_requests
        self.conn: socket.socket | None = None
        self.used = 0
        self.handshakes = 0

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.handshakes += 1
        self.used = 0
        return s

    def request(self) -> tuple[bool, str]:
        if self.conn is None:
            self.conn = self._connect()
        try:
            self.conn.sendall(b"GET /work HTTP/1.1\r\nHost: lab\r\n\r\n")
            resp = self.conn.recv(4096)
        except (BrokenPipeError, ConnectionResetError) as e:
            self._discard()
            return False, type(e).__name__
        if not resp:
            # The read returned nothing: the peer had already closed. urllib3
            # raises RemoteDisconnected here; nginx logs a 502 and tells the
            # client nothing useful, because it genuinely does not know
            # whether the request was processed.
            self._discard()
            return False, "RemoteDisconnected (empty read after FIN)"

        self.used += 1
        if self.max_requests is not None and self.used >= self.max_requests:
            # nginx's keepalive_requests / Tomcat's maxKeepAliveRequests:
            # retire the connection on purpose so the pool keeps rediscovering
            # where the backend actually is (Topic 5).
            self._discard()
        return True, "200"

    def _discard(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def run_config(name: str, backend_idle: float, keepalive_requests=None):
    backend = Backend(backend_idle)
    backend.start()
    client = PooledClient(backend.port, max_requests=keepalive_requests)

    failures, log = 0, []
    for i in range(REQUESTS_PER_CONFIG):
        ok, detail = client.request()
        if not ok:
            failures += 1
        log.append((i, ok, detail))
        # The idle gap. This is the whole experiment: high sustained load
        # never leaves one, which is why this defect gets MORE likely as your
        # traffic gets quieter and is close to unreproducible in a busy
        # staging load test.
        time.sleep(POOL_IDLE)

    backend.stop()
    time.sleep(0.05)

    print(f"  {name}")
    print(f"    backend idle timeout   {backend_idle:.1f}s")
    print(f"    pool idle gap          {POOL_IDLE:.1f}s")
    if keepalive_requests:
        print(f"    pool retires after     {keepalive_requests} requests")
    ordering = "backend closes first  <-- the bug" if backend_idle < POOL_IDLE \
        else "pool closes first     <-- correct"
    print(f"    ordering               {ordering}")
    for i, ok, detail in log:
        mark = "ok " if ok else "502"
        print(f"      request {i}: {mark}  {detail}")
    print(f"    failures {failures}/{REQUESTS_PER_CONFIG}   "
          f"backend-initiated closes {backend.closed_by_us}   "
          f"handshakes {client.handshakes}")
    print()
    return failures


def print_defaults():
    print("  Defaults on THIS machine (read, not quoted):")
    try:
        import uvicorn
        from uvicorn.config import Config
        cfg = Config(app=None)
        print(f"    uvicorn {uvicorn.__version__}: timeout_keep_alive = {cfg.timeout_keep_alive}s")
    except Exception as exc:                                  # noqa: BLE001
        print(f"    uvicorn not importable here ({type(exc).__name__}).")
        print("    Read it yourself with: uvicorn --help | grep -A2 timeout-keep-alive")
    print("    The load balancer half is not readable from here and must not be")
    print("    guessed: check your ALB's idle timeout in the console, or nginx's")
    print("    `keepalive_timeout` in the upstream block. Both numbers or neither.")
    print()


def main():
    print("=" * 78)
    print("Two idle timers, one connection, and the 502 in the gap between them")
    print("=" * 78)
    print()
    print_defaults()

    mismatched = run_config("mismatched -- backend 0.3s, pool 1.0s",
                            BACKEND_IDLE_MISMATCHED)
    ordered = run_config("ordered -- backend 2.0s, pool 1.0s",
                         BACKEND_IDLE_ORDERED)
    bounded = run_config("ordered_bounded -- ordered, pool retires every 2 requests",
                         BACKEND_IDLE_ORDERED, keepalive_requests=KEEPALIVE_REQUESTS)

    print("  The rule, in one line:")
    print("    the backend's idle timeout must be strictly longer than the proxy's.")
    print()
    print(f"    mismatched      {mismatched} failures")
    print(f"    ordered         {ordered} failures")
    print(f"    ordered_bounded {bounded} failures, with connections rotating on purpose")
    print()
    print("  Note what is NOT in the mismatched output: an error on the backend.")
    print("  It closed an idle connection, exactly as configured. The pool wrote a")
    print("  request onto a socket that was already half-closed, which it had no")
    print("  way to know. Neither component did anything wrong, and that is why no")
    print("  single component's logs can explain this and why it survives so long.")
    print()
    print("  And notice the handshake count in the bounded run. Retiring connections")
    print("  costs you a handshake every N requests on purpose -- that is the price")
    print("  of a pool that keeps rediscovering where the backend actually is.")


if __name__ == "__main__":
    main()
