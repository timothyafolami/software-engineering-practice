"""
Layer 2 · Topic 7 - Six clients, one workload, one number each: how many TCP
connections did you actually open?

The topic's tooling is tcpdump and ss, and both of them live inside the Linux
container (see sniff/ in this directory for those). This program is the half
that runs anywhere, needs no root and no capture: it counts connections at the
OTHER END. A server that counts accept() calls sees exactly what a SYN counter
in tcpdump would see, minus retransmitted SYNs, and it needs no privileges at
all.

That equivalence is worth stating once, because it is the reason this file
exists: `tcpdump 'tcp[tcpflags] & tcp-syn != 0 and not tcp[tcpflags] & tcp-ack
!= 0'` counts connection INITIATIONS from the outside; accept() counts them
from the inside. If they disagree, the difference is retransmitted SYNs and
connections that never completed -- which is itself a finding worth having.

Each language directory holds one client that makes REQUESTS requests to the
same URL using that runtime's default pooled client, and prints one line. This
program runs all six, one at a time, with the server's accept counter reset
between them, and prints the table Topic 1 claimed and never proved.

  A runtime that claims to pool and opens one connection per request does not
  pool.

Missing toolchains are reported as blocked, with the command to run, and are
NOT silently skipped -- an absent row and a zero row mean different things.

Run: python3 pools_as_advertised.py
"""
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REQUESTS = 30
HERE = Path(__file__).resolve().parent.parent


class Counter:
    def __init__(self):
        self.accepted = 0
        self.requests = 0
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            self.accepted = 0
            self.requests = 0


COUNTER = Counter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"       # keep-alive, so pooling is possible at all

    def do_GET(self):
        with COUNTER.lock:
            COUNTER.requests += 1
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        # One accept() = one TCP connection = one SYN on the wire.
        with COUNTER.lock:
            COUNTER.accepted += 1
        super().process_request(request, client_address)


def start_server():
    server = CountingServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/work"


# Each entry: (label, the tool that must exist, how to build, how to run)
def clients(url: str):
    env = dict(os.environ, LAB_URL=url, LAB_REQUESTS=str(REQUESTS))
    build = "/tmp/l2t7"
    os.makedirs(build, exist_ok=True)
    return [
        ("Python (httpx)", "python3", None,
         ["python3", str(HERE / "python" / "syn_client.py")], env,
         "pip install httpx"),
        ("Node (undici/fetch)", "node", None,
         ["node", str(HERE / "nodejs" / "syn_client.js")], env,
         "install Node 24"),
        ("Go (net/http)", "go", None,
         ["go", "run", "syn_client.go"], dict(env, PWD=str(HERE / "golang")),
         "install Go"),
        ("Rust (std::net)", "cargo",
         ["cargo", "build", "--release", "--quiet", "--offline"],
         [str(HERE / "rust" / "syn_client" / "target" / "release" / "syn_client")], env,
         "install Rust (rustup)"),
        ("C++ (libcurl)", "c++",
         ["c++", "-O2", "-std=c++17", "-o", f"{build}/syn_client_cpp",
          str(HERE / "cpp" / "syn_client.cpp"), "-lcurl"],
         [f"{build}/syn_client_cpp"], env,
         "install libcurl development headers"),
        ("Java (HttpClient)", "javac",
         ["javac", str(HERE / "java" / "SynClient.java"), "-d", f"{build}/javabuild"],
         ["java", "-cp", f"{build}/javabuild", "SynClient"], env,
         "install a JDK 21+"),
    ]


def run_one(label, tool, build_cmd, run_cmd, env, unblock):
    if shutil.which(tool) is None:
        return label, None, None, f"BLOCKED: {tool} not found -- {unblock}"

    cwd = None
    if label.startswith("Go"):
        cwd = str(HERE / "golang")
    if label.startswith("Rust"):
        cwd = str(HERE / "rust" / "syn_client")

    if build_cmd:
        r = subprocess.run(build_cmd, cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            first = (r.stderr.strip().splitlines() or ["(no output)"])[0]
            return label, None, None, f"BLOCKED: build failed -- {first}"

    COUNTER.reset()
    t0 = time.perf_counter()
    r = subprocess.run(run_cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    elapsed = time.perf_counter() - t0
    if r.returncode != 0:
        first = (r.stderr.strip().splitlines() or ["(no output)"])[0]
        return label, None, None, f"BLOCKED: run failed -- {first}"

    with COUNTER.lock:
        accepted, served = COUNTER.accepted, COUNTER.requests
    note = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return label, accepted, served, f"{elapsed * 1000:6.0f} ms   {note}"


def main():
    server, url = start_server()
    print("=" * 78)
    print("Does it pool? One number per runtime, measured from the other end")
    print("=" * 78)
    print(f"  {REQUESTS} sequential requests per client, against {url}")
    print("  The server counts accept() calls. One accept = one TCP connection =")
    print("  one SYN that tcpdump would have shown you, without needing root.")
    print()
    print("  %-22s %10s %10s   %s" % ("client", "conns", "requests", "verdict"))

    rows = []
    for label, tool, build_cmd, run_cmd, env, unblock in clients(url):
        label, accepted, served, note = run_one(label, tool, build_cmd, run_cmd, env, unblock)
        if accepted is None:
            print("  %-22s %10s %10s   %s" % (label, "-", "-", note))
            rows.append((label, None, None))
            continue
        if served != REQUESTS:
            verdict = f"CHECK: server saw {served} requests, expected {REQUESTS}"
        elif accepted == 1:
            verdict = "pools: one connection for every request"
        elif accepted >= REQUESTS:
            verdict = "DOES NOT POOL: a connection per request"
        else:
            verdict = f"partial: {served / accepted:.1f} requests per connection"
        print("  %-22s %10d %10d   %s" % (label, accepted, served, verdict))
        rows.append((label, accepted, served))

    server.shutdown()

    print()
    print("  What this table is and is not:")
    print()
    print("    It IS the claim from Topic 1, checked. Every 'this client pools'")
    print("    sentence in that topic is one number here, measured the same way for")
    print("    all six, with nothing to argue about.")
    print()
    print("    It is NOT a capture. It cannot show you a retransmitted SYN, a")
    print("    ClientHello spanning two segments, a FIN followed by a request on the")
    print("    same four-tuple, or a duplicate ACK. For those you need tcpdump, and")
    print("    on this machine that means the sniff sidecar in the lab -- see")
    print("    ../sniff/ in this directory for the exact commands.")
    print()
    print("    A row reading BLOCKED is a result. Record it with the unblock command")
    print("    beside it; a missing row and a zero row mean different things and the")
    print("    difference matters when you come back to this table in six months.")
    print()
    print("  The single most useful version of this measurement is the one you run")
    print("  against your own service in staging: capture SYNs for sixty seconds and")
    print("  divide by the request rate. If that ratio is near 1, you have found your")
    print("  latency problem and Topic 1 is the fix.")


if __name__ == "__main__":
    main()
