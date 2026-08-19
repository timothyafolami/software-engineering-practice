"""
The whiteboard version: write skew in two lock-step psql sessions.

    python3 01-isolation-levels/python/lockstep_psql.py

WHAT IT DEMONSTRATES: exactly the interleaving the roadmap asks you to be able to
draw -- two transactions, one statement at a time, in a fixed order, with both
sessions' output printed as it happens. It drives two REAL psql processes rather
than simulating them, so what you see is what you would see in two terminals.

WHAT TO LOOK FOR: the same eight steps at three isolation levels. At READ
COMMITTED and REPEATABLE READ every statement succeeds and the shift ends with
nobody on call. At SERIALIZABLE the second COMMIT fails with SQLSTATE 40001 --
and note WHERE it fails: not at the UPDATE (no row conflicts, nothing to block
on) but at commit time, when SSI finds the dependency cycle.

To do this by hand in two terminals instead, use sql/write_skew_session_a.sql
and sql/write_skew_session_b.sql.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

SHIFT = 1
MARKER = "__STEP_DONE__"


class Session:
    """One psql process, driven one statement at a time."""

    def __init__(self, name: str, dsn: str):
        self.name = name
        self.proc = subprocess.Popen(
            ["psql", "-X", "-q", "-A", "-t", "-P", "pager=off", "-v", "ON_ERROR_STOP=0", dsn],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

    def run(self, sql: str) -> str:
        self.proc.stdin.write(sql + "\n")
        self.proc.stdin.write(f"\\echo {MARKER}\n")
        self.proc.stdin.flush()
        out = []
        for line in self.proc.stdout:
            if line.strip() == MARKER:
                break
            if line.strip():
                out.append(line.rstrip())
        return "\n".join(out)

    def close(self) -> None:
        try:
            self.proc.stdin.write("\\q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def step(session: Session, sql: str) -> None:
    print(f"  {session.name} | {sql}")
    out = session.run(sql)
    for line in out.splitlines():
        print(f"  {' ' * len(session.name)} | {line}")


def scenario(isolation: str, dsn: str, setup) -> None:
    lab_db.reset_oncall(setup, shifts=8)
    print(f"\n--- isolation level: {isolation} " + "-" * (58 - len(isolation)))
    a = Session("A", dsn)
    b = Session("B", dsn)
    try:
        for s in (a, b):
            step(s, f"BEGIN TRANSACTION ISOLATION LEVEL {isolation};")
            step(s, "SHOW transaction_isolation;")
        # Both read the same rows, and both see two doctors on call.
        for s in (a, b):
            step(s, f"SELECT count(*) FROM oncall WHERE shift_id = {SHIFT} AND on_call;")
        # Each writes a DIFFERENT row, so no row-level conflict exists.
        step(a, f"UPDATE oncall SET on_call = false WHERE shift_id = {SHIFT} AND doctor_id = 1;")
        step(b, f"UPDATE oncall SET on_call = false WHERE shift_id = {SHIFT} AND doctor_id = 2;")
        step(a, "COMMIT;")
        step(b, "COMMIT;")
    finally:
        a.close()
        b.close()

    left = setup.execute(
        "SELECT count(*) FROM oncall WHERE shift_id = %s AND on_call", (SHIFT,)
    ).fetchone()[0]
    verdict = "INVARIANT BROKEN: nobody is on call" if left == 0 else "invariant held"
    print(f"  => doctors still on call for shift {SHIFT}: {left}   ({verdict})")


def main() -> None:
    if shutil.which("psql") is None:
        sys.exit("psql not found on PATH -- this program drives two real psql sessions")
    lab_db.ensure_database()
    with lab_db.connect() as setup:
        lab_db.ensure_core_tables(setup)
        lab_db.banner(f"Write skew in lock step -- {lab_db.describe_server(setup)}")
        print("Two psql sessions, one statement at a time, in the order printed.")
        for isolation in ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"):
            scenario(isolation, lab_db.DSN, setup)
        print("\nNote where SERIALIZABLE fails: at B's COMMIT, not at B's UPDATE. The UPDATEs")
        print("touch different rows, so there is nothing to block on -- SSI is tracking the")
        print("read/write dependency between the two transactions, not the rows themselves.")


if __name__ == "__main__":
    main()
