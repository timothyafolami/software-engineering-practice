"""
What of this layer can run on THIS machine right now, and what is blocked.

    python3 lab/local/check_env.py

Prints one line per capability with the exact command that would unblock it. The
layer README is written against Postgres 18 (and one PG19 feature); this tells you
honestly which experiments degrade or disappear on the server you actually have.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab_db

OK, NO = "available", "BLOCKED"


def line(label: str, ok: bool, detail: str) -> None:
    print(f"  {label:<34} {OK if ok else NO:<10} {detail}")


def main() -> None:
    lab_db.banner("Layer 3 environment check")

    try:
        lab_db.ensure_database()
        conn = lab_db.connect()
    except Exception as exc:  # noqa: BLE001 - this is the report, not a crash
        print(f"  no Postgres reachable at {lab_db.DSN}: {exc}")
        print("  start one:  brew services start postgresql@17")
        return

    ver = lab_db.server_version(conn)
    print(f"  {lab_db.describe_server(conn)}   (server_version_num={ver})")
    print()

    line("core experiments (topics 1,2,4,5,6)", True, "any Postgres >= 12")
    line("EXPLAIN BUFFERS by default", ver >= 180000,
         "PG18 default; on older servers pass BUFFERS explicitly (the code does)")
    line("B-tree skip scan (topic 3)", ver >= 180000,
         "PG18 feature; on PG17 the same query shape must seq-scan")
    line("EXPLAIN 'Index Searches:' counter", ver >= 180000, "PG18 EXPLAIN field")
    line("WAIT FOR LSN (topic 8)", ver >= 190000, "PG19 beta feature")

    preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0]
    line("pg_stat_statements (topics 4,6)", "pg_stat_statements" in preload,
         "ALTER SYSTEM SET shared_preload_libraries='pg_stat_statements'; then restart")
    line("auto_explain (topic 4)", "auto_explain" in preload,
         "same: add auto_explain to shared_preload_libraries, then restart")

    wal_level = conn.execute("SHOW wal_level").fetchone()[0]
    line("logical replication slot (topic 2)", wal_level == "logical",
         f"wal_level={wal_level}; needs 'logical' + restart for the dead-slot demo")

    line("pg_basebackup replica (topic 8)", shutil.which("pg_basebackup") is not None,
         "used by 08-replication-lag/scripts/start_replica.sh")

    docker = shutil.which("docker")
    daemon = False
    if docker:
        daemon = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    line("docker daemon (lab/docker)", daemon, "open -a Docker, then docker compose up")
    line("k6 load generator", shutil.which("k6") is not None,
         "brew install k6 -- or use 07-connection-pools/golang, which replaces it")
    conn.close()


if __name__ == "__main__":
    main()
