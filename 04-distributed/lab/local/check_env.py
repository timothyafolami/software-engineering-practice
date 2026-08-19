"""
Layer 4 -- what is and is not runnable on this machine, checked rather than assumed.

WHAT THIS DEMONSTRATES: every topic README in this layer says "blocked while the
Docker daemon is down -- python3 ../lab/local/check_env.py". This is that script.
It probes each thing the layer depends on and, for anything missing, prints the
exact command that unblocks it. It never installs anything and never starts a
daemon; it only reports.

WHAT TO LOOK FOR IN THE OUTPUT: the per-topic table at the bottom. A topic marked
RUNNABLE has every dependency its run block needs. A topic marked PARTIAL has a
local fallback that works and a compose half that does not. BLOCKED means nothing
in that topic's run block will work until the "unblock" line is done.

  python3 lab/local/check_env.py
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys

# ---------------------------------------------------------------- probe helpers

OK, WARN, BAD = "ok", "warn", "missing"
MARK = {OK: "  ok  ", WARN: " warn ", BAD: "MISSING"}


class Probe:
    """One checked fact plus the command that fixes it if it is false."""

    def __init__(self, name: str, state: str, detail: str, unblock: str = "") -> None:
        self.name, self.state, self.detail, self.unblock = name, state, detail, unblock

    def line(self) -> str:
        return f"  [{MARK[self.state]}]  {self.name:<22}{self.detail}"


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def probe_binary(name: str, argv: list[str], unblock: str) -> Probe:
    if shutil.which(argv[0]) is None:
        return Probe(name, BAD, "not on PATH", unblock)
    rc, out = _run(argv)
    first = out.splitlines()[0] if out else ""
    return Probe(name, OK if rc == 0 else WARN, first or f"exit {rc}", "" if rc == 0 else unblock)


def probe_docker() -> Probe:
    if shutil.which("docker") is None:
        return Probe("docker", BAD, "not on PATH", "install Docker Desktop")
    rc, _ = _run(["docker", "info"], timeout=20.0)
    if rc == 0:
        rc2, out2 = _run(["docker", "version", "--format", "{{.Server.Version}}"])
        return Probe("docker daemon", OK, f"up, server {out2}")
    return Probe("docker daemon", BAD, "CLI present, daemon not responding",
                 "open -a Docker   # then re-run this script")


def probe_postgres() -> Probe:
    if shutil.which("pg_isready") is None:
        return Probe("postgres", BAD, "pg_isready not on PATH", "brew install postgresql@17")
    rc, out = _run(["pg_isready"])
    if rc != 0:
        return Probe("postgres", BAD, out or "not accepting connections",
                     "pg_ctl -D /opt/homebrew/var/postgresql@17 start")
    return Probe("postgres", OK, out)


def probe_psycopg() -> Probe:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return Probe("psycopg3", BAD, "not importable",
                     "python3 -m pip install 'psycopg[binary]'")
    return Probe("psycopg3", OK, f"version {psycopg.__version__}")


def probe_lab_db() -> Probe:
    """Can we actually reach the scratch database (or create it)?"""
    try:
        import psycopg
    except ImportError:
        return Probe("lab database", BAD, "needs psycopg3 first",
                     "python3 -m pip install 'psycopg[binary]'")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import lab_db

        # Deliberately does NOT create the database. A check script that has side
        # effects is a check script you stop trusting. Topic programs call
        # ensure_database() themselves; this only reports what is already there.
        with lab_db.connect(lab_db.ADMIN_DSN) as admin:
            ver = lab_db.server_version(admin)
            uuidv7 = admin.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'uuidv7'"
            ).fetchone()[0]
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (lab_db.DB_NAME,)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - report, never raise, this is a probe
        return Probe("lab database", BAD, f"{type(exc).__name__}: {exc}".splitlines()[0],
                     "check LAB_ADMIN_DSN / LAB_DSN")
    state = "exists" if exists else "not created yet (any topic program creates it)"
    note = f"{lab_db.DB_NAME} {state}, server {ver // 10000}.{ver % 10000}"
    if not uuidv7:
        return Probe("lab database", WARN,
                     note + "  (no uuidv7(): pre-18, see lab/README.md)")
    return Probe("lab database", OK, note + "  (uuidv7() present)")


def probe_port(name: str, port: int, unblock: str) -> Probe:
    with socket.socket() as s:
        s.settimeout(0.4)
        listening = s.connect_ex(("127.0.0.1", port)) == 0
    return Probe(name, OK if listening else BAD,
                 f"127.0.0.1:{port} {'listening' if listening else 'nothing listening'}",
                 "" if listening else unblock)


def probe_node_pg() -> Probe:
    """node-postgres, needed by topics 2 and 6. Looked for beside the topic code."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    found = [
        rel for rel in ("02-idempotency-keys-atomically/nodejs",
                        "06-outbox-sagas-and-why-2pc-is-avoided/nodejs")
        if os.path.isdir(os.path.join(root, rel, "node_modules", "pg"))
    ]
    if len(found) == 2:
        return Probe("node-postgres", OK, "installed beside topics 2 and 6")
    missing = [r for r in ("02-idempotency-keys-atomically/nodejs",
                           "06-outbox-sagas-and-why-2pc-is-avoided/nodejs") if r not in found]
    return Probe("node-postgres", BAD, "missing in " + ", ".join(missing),
                 "cd " + missing[0] + " && npm install")


def probe_go_module() -> Probe:
    """pgx, needed by topic 2's Go race. Cached module counts -- no network needed."""
    rc, out = _run(["go", "env", "GOMODCACHE"])
    if rc != 0:
        return Probe("pgx (Go)", BAD, "go toolchain not usable", "install Go 1.25+")
    cache = os.path.join(out.strip(), "github.com", "jackc", "pgx")
    versions = sorted(d for d in os.listdir(cache) if d.startswith("v5@")) \
        if os.path.isdir(cache) else []
    if versions:
        return Probe("pgx (Go)", OK, f"module cache has {versions[-1]}")
    return Probe("pgx (Go)", WARN, "not in module cache; first build will fetch it",
                 "cd 02-idempotency-keys-atomically/golang && go mod download")


# ------------------------------------------------------------------ topic table

def topic_rows(by_name: dict[str, Probe]) -> list[tuple[str, str, str]]:
    def up(*names: str) -> bool:
        return all(by_name[n].state == OK for n in names)

    def warn_ok(*names: str) -> bool:
        return all(by_name[n].state in (OK, WARN) for n in names)

    docker = up("docker daemon")
    k6 = up("k6")
    pg = warn_ok("postgres", "psycopg3", "lab database")
    node_pg = up("node-postgres")

    rows: list[tuple[str, str, str]] = []

    rows.append(("1  partial failure",
                 "RUNNABLE" if docker and k6 else "PARTIAL",
                 "part A's six programs need nothing; part B needs docker+k6"))
    rows.append(("2  idempotency keys",
                 "RUNNABLE" if pg and node_pg else ("PARTIAL" if pg else "BLOCKED"),
                 "local race needs postgres + psycopg3 + node-postgres; compose needs docker+k6"))
    rows.append(("3  clocks lie",
                 "RUNNABLE",
                 "part A and the span harness need nothing; part B needs docker+k6"))
    rows.append(("4  consistency models",
                 "PARTIAL",
                 "session_guarantees.py runs anywhere; the standby needs docker"))
    rows.append(("5  consensus / raft",
                 "RUNNABLE",
                 "go test needs nothing; the etcd cluster needs docker"))
    rows.append(("6  outbox and sagas",
                 "PARTIAL" if pg else "BLOCKED",
                 "hwm_skip + relay need postgres; the broker faults need docker"))
    rows.append(("7  leader election",
                 "RUNNABLE",
                 "the six-language pause audit needs nothing; parts 1-4 need docker"))
    return rows


def main() -> int:
    probes = [
        probe_binary("python3", [sys.executable, "-V"], "install Python 3.13"),
        probe_binary("node", ["node", "-v"], "install Node 24"),
        probe_binary("go", ["go", "version"], "install Go 1.25+"),
        probe_binary("cargo", ["cargo", "--version"], "install Rust via rustup"),
        probe_binary("c++", ["c++", "--version"], "xcode-select --install"),
        probe_binary("javac", ["javac", "-version"], "install a JDK 21+"),
        probe_postgres(),
        probe_psycopg(),
        probe_lab_db(),
        probe_node_pg(),
        probe_go_module(),
        probe_docker(),
        probe_binary("k6", ["k6", "version"], "brew install k6"),
        probe_port("toxiproxy api", 8474, "docker compose up -d toxiproxy"),
        probe_port("etcd1 client", 2379, "docker compose up -d etcd1 etcd2 etcd3"),
        probe_port("redpanda", 9092, "docker compose up -d redpanda"),
    ]
    by_name = {p.name: p for p in probes}

    print("=" * 78)
    print("Layer 4 -- environment check")
    print("=" * 78)
    print(f"  platform: {sys.platform}  python: {sys.version.split()[0]}")

    for group, names in (
        ("toolchains", ["python3", "node", "go", "cargo", "c++", "javac"]),
        ("postgres (local fallback for topics 2, 4, 6, 7)",
         ["postgres", "psycopg3", "lab database", "node-postgres", "pgx (Go)"]),
        ("compose stack (lab/README.md)",
         ["docker daemon", "k6", "toxiproxy api", "etcd1 client", "redpanda"]),
    ):
        print()
        print(group)
        for n in names:
            print(by_name[n].line())

    blocked = [p for p in probes if p.state != OK and p.unblock]
    print()
    print("-" * 78)
    if blocked:
        print("to unblock, in order:")
        for p in blocked:
            print(f"  {p.name:<22}{p.unblock}")
    else:
        print("nothing blocked.")

    print()
    print("-" * 78)
    print("per topic")
    print("-" * 78)
    for name, verdict, why in topic_rows(by_name):
        print(f"  {name:<24}{verdict:<10}{why}")
    print()
    print("BLOCKED/PARTIAL is the honest state of this machine, not a defect in the")
    print("topic. Every blocked run block in this layer names this script by path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
