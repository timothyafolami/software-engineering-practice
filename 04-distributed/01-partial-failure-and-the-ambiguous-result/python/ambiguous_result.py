"""
Layer 4 · Topic 1 — the third outcome, in Python (httpx).

WHAT THIS DEMONSTRATES
  A local call has two outcomes. A remote call has three, and the third --
  "I do not know whether it happened" -- is not an error you can engineer away.
  This program runs a real ledger server in-process so there is a *server-side
  truth* to compare the client's belief against, then injects five faults and
  shows the diff.

  Phase 1 retries on any exception, which is what `except httpx.HTTPError:`
  produces in almost every codebase. Phase 2 retries only the errors that prove
  the request never landed. Same faults, same load, both phases measured.

WHAT TO LOOK FOR
  1. Phase 1's "duplicate charges" count. Every one of those was created by the
     client's own retry, not by the fault.
  2. Phase 2's duplicate count, and the "unresolved ambiguous" count next to it.
     The duplicates go to zero; the ambiguity does not. That residue is
     irreducible at this layer and is the entire reason Topic 2 exists.
  3. `connect refused` is the only fault where a retry is provably safe -- the
     bytes never left the machine. Everything else is a guess.

WHY httpx: the exception taxonomy is the lesson. ConnectError/ConnectTimeout
mean the request provably never landed; ReadTimeout/RemoteProtocolError/ReadError
mean it may well have. Most code catches httpx.HTTPError, which is the parent of
all of them, and that single line is the bug.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from collections import Counter

try:
    import httpx
except ImportError:
    sys.exit("needs httpx:  python3 -m pip install httpx")

CLIENT_TIMEOUT = 0.3      # seconds; the deadline the caller is willing to wait
SLOW_RESPONSE = 1.0       # seconds; deliberately longer than CLIENT_TIMEOUT
REQUESTS_PER_MODE = 4
MAX_ATTEMPTS = 3

# The five faults, plus a baseline. `commits` records whether the server writes
# the charge to its ledger *before* the fault fires -- which is the whole point:
# four of these five commit and then fail to tell the caller.
MODES = [
    ("ok", "no fault"),
    ("slow", "commits, then replies after the client has given up"),
    ("hang", "commits, then never replies at all (blackhole)"),
    ("reset", "commits, then RSTs the connection"),
    ("crash_after_commit", "commits, then dies before writing a byte of response"),
    ("refused", "nothing is listening; the request provably never landed"),
]


class Ledger:
    """Server-side truth. Every accepted charge is appended, duplicates and all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[str] = []

    def commit(self, charge_id: str) -> None:
        with self._lock:
            self.rows.append(charge_id)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.rows)


class LedgerServer:
    """Minimal HTTP/1.1 over raw sockets.

    Raw sockets rather than http.server because two of the faults (an RST, and a
    close with no response at all) are things a WSGI-shaped handler cannot
    express. Being able to inject them precisely is worth the forty lines.

    Composition rather than subclassing threading.Thread on purpose: Thread
    already owns attribute names like `_handle` on 3.13, and silently shadowing
    one of them fails at runtime inside the thread where you will not see it.
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self._held: list[socket.socket] = []   # `hang` connections, closed at shutdown
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _accept_loop(self) -> None:
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            request = conn.recv(4096).decode("latin-1", "replace")
            if not request:
                conn.close()
                return
            path = request.split(" ", 2)[1]           # /charge/<mode>/<charge_id>
            _, _, mode, charge_id = path.split("/", 3)

            if mode == "ok":
                self.ledger.commit(charge_id)
                self._reply(conn, charge_id)
            elif mode == "slow":
                self.ledger.commit(charge_id)
                time.sleep(SLOW_RESPONSE)
                self._reply(conn, charge_id)
            elif mode == "hang":
                self.ledger.commit(charge_id)
                self._held.append(conn)             # never replied to, never closed
                return
            elif mode == "reset":
                self.ledger.commit(charge_id)
                # SO_LINGER with a zero timeout makes close() send RST, not FIN.
                # This is what a peer that panics or a middlebox that gives up
                # looks like from the client side.
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
                conn.close()
                return
            elif mode == "crash_after_commit":
                # The case no timeout tuning can ever fix: the work is durable
                # and the only process that knew about it is gone.
                self.ledger.commit(charge_id)
                conn.close()
                return
            else:
                self._reply(conn, charge_id, status="400 Bad Request")
        except Exception:
            pass
        finally:
            if conn not in self._held:
                try:
                    conn.close()
                except OSError:
                    pass

    @staticmethod
    def _reply(conn: socket.socket, charge_id: str, status: str = "200 OK") -> None:
        body = f'{{"charge_id":"{charge_id}"}}'.encode()
        head = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        conn.sendall(head + body)

    def shutdown(self) -> None:
        self.running = False
        for held in self._held:
            try:
                held.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- the classification that is the whole point -----------------------------
#
# SAFE means the request provably never reached the server, so a retry cannot
# duplicate work. AMBIGUOUS means it may have: the connection was established
# and the request bytes were written, and after that the client learns nothing.
#
# Note where the line falls. It is NOT "connection errors are safe, timeouts are
# not". It is "did we get far enough to have sent the request".
SAFE_TO_RETRY = (
    httpx.ConnectError,      # ECONNREFUSED / EHOSTUNREACH: never left the host
    httpx.ConnectTimeout,    # TCP handshake never completed
)
AMBIGUOUS = (
    httpx.ReadTimeout,       # request sent; we gave up waiting for the answer
    httpx.WriteTimeout,      # partially sent; the server may have a full copy
    httpx.RemoteProtocolError,   # server disconnected without a response
    httpx.ReadError,         # ECONNRESET mid-read
    httpx.PoolTimeout,       # never acquired a connection -- also safe, see note
)


def classify(exc: Exception) -> tuple[str, str]:
    """Return (verdict, short label). Verdict is SAFE, AMBIGUOUS or SUCCESS."""
    if isinstance(exc, httpx.PoolTimeout):
        # Pedantically safe: we never got a connection, so nothing was sent.
        # Kept separate because it is a *client* saturation signal, not a
        # dependency failure, and conflating the two hides an outage.
        return "SAFE", "PoolTimeout"
    if isinstance(exc, SAFE_TO_RETRY):
        return "SAFE", type(exc).__name__
    if isinstance(exc, AMBIGUOUS):
        return "AMBIGUOUS", type(exc).__name__
    # Anything unrecognised must be treated as ambiguous. Defaulting the other
    # way is how duplicate charges get shipped.
    return "AMBIGUOUS", type(exc).__name__


def attempt(client: httpx.Client, url: str) -> tuple[str, str]:
    try:
        response = client.get(url)
        return ("SUCCESS", str(response.status_code))
    except Exception as exc:  # noqa: BLE001 - classification is the subject
        return classify(exc)


def run_phase(name: str, port: int, retry_ambiguous: bool, note: str) -> dict:
    """One full pass over every fault mode.

    retry_ambiguous=True is the naive client: any exception, try again.
    retry_ambiguous=False only retries errors that prove nothing was sent.
    """
    ledger = SERVER.ledger
    before = len(ledger.snapshot())
    timeout = httpx.Timeout(
        connect=CLIENT_TIMEOUT, read=CLIENT_TIMEOUT,
        write=CLIENT_TIMEOUT, pool=CLIENT_TIMEOUT,
    )
    per_mode = {}

    print()
    print(f"  {name}")
    print(f"  {note}")
    print(f"  {'fault':<20} {'client verdict':<34} {'attempts':>9} {'ledger rows':>12}")

    with httpx.Client(timeout=timeout, limits=httpx.Limits(max_connections=200)) as client:
        for mode, _description in MODES:
            mode_before = len(ledger.snapshot())
            attempts = 0
            verdicts: Counter[str] = Counter()

            for i in range(REQUESTS_PER_MODE):
                charge_id = f"{name[:2]}-{mode}-{i}"
                target_port = port if mode != "refused" else CLOSED_PORT
                url = f"http://127.0.0.1:{target_port}/charge/{mode}/{charge_id}"

                for _ in range(MAX_ATTEMPTS):
                    attempts += 1
                    verdict, label = attempt(client, url)
                    if verdict == "SUCCESS":
                        break
                    if verdict == "SAFE":
                        continue                    # provably safe: try again
                    if retry_ambiguous:
                        continue                    # the bug, made explicit
                    break                           # correct: stop, escalate
                verdicts[f"{verdict}({label})"] += 1

            mode_rows = len(ledger.snapshot()) - mode_before
            summary = ", ".join(f"{n}x {v}" for v, n in verdicts.most_common())
            print(f"  {mode:<20} {summary:<34} {attempts:>9} {mode_rows:>12}")
            per_mode[mode] = verdicts

    rows = ledger.snapshot()[before:]
    counts = Counter(rows)
    duplicates = sum(n - 1 for n in counts.values() if n > 1)
    unresolved = sum(
        n for verdicts in per_mode.values()
        for v, n in verdicts.items() if v.startswith("AMBIGUOUS")
    )
    print(f"  {'':<20} {'':<34} {'':>9} {'':>12}")
    print(f"  ledger rows written this phase : {len(rows)}")
    print(f"  DUPLICATE CHARGES              : {duplicates}"
          f"   <- created by this client's retries")
    print(f"  unresolved ambiguous outcomes  : {unresolved}"
          f"   <- caller cannot tell whether these happened")
    return {"duplicates": duplicates, "unresolved": unresolved, "rows": len(rows)}


def find_closed_port() -> int:
    """A port nothing is listening on, so connect() gets ECONNREFUSED."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SERVER: LedgerServer
CLOSED_PORT: int


def main() -> None:
    global SERVER, CLOSED_PORT
    ledger = Ledger()
    SERVER = LedgerServer(ledger)
    SERVER.start()
    CLOSED_PORT = find_closed_port()

    print("=" * 78)
    print("Layer 4 · Topic 1 — partial failure and the ambiguous result (Python/httpx)")
    print("=" * 78)
    print(f"  ledger        : 127.0.0.1:{SERVER.port}  (in-process, holds server-side truth)")
    print(f"  closed port   : 127.0.0.1:{CLOSED_PORT}  (for the connect-refused case)")
    print(f"  client timeout: {CLIENT_TIMEOUT}s   slow response: {SLOW_RESPONSE}s   "
          f"max attempts: {MAX_ATTEMPTS}")

    naive = run_phase(
        "phase 1 — retry on any exception",
        SERVER.port,
        retry_ambiguous=True,
        note="what `except httpx.HTTPError: retry` actually does",
    )
    fixed = run_phase(
        "phase 2 — retry only provably-safe errors",
        SERVER.port,
        retry_ambiguous=False,
        note="ConnectError/ConnectTimeout are retried; everything else escalates",
    )

    print()
    print("-" * 78)
    print(f"  duplicate charges   phase 1: {naive['duplicates']:<6} "
          f"phase 2: {fixed['duplicates']}")
    print(f"  unresolved ambiguity phase 1: {naive['unresolved']:<6} "
          f"phase 2: {fixed['unresolved']}")
    print()
    print("  The fix removes the duplicates the client was causing. It does not")
    print("  remove the ambiguity -- nothing at this layer can, because the")
    print("  information needed is on the far side of the thing that broke.")
    print("  Making a retry of an ambiguous outcome *safe* is Topic 2.")
    SERVER.shutdown()


if __name__ == "__main__":
    main()
