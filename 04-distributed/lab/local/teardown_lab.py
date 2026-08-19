"""
Layer 4 -- drop the scratch database this layer's local fallback created.

WHAT THIS DEMONSTRATES: nothing about distributed systems. It exists so that the
local fallback leaves no residue, which is the only reason it was safe to let
topic programs create a database without asking.

WHAT TO LOOK FOR IN THE OUTPUT: the list of sessions it had to terminate. If it
is not empty, something in this layer was still connected -- a relay from topic 6
or a lease holder from topic 7 is the usual answer, and both of those are
supposed to be long-running. Stop them rather than assuming the drop was clean.

  python3 lab/local/teardown_lab.py          # asks first
  python3 lab/local/teardown_lab.py --yes    # does not
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lab_db  # noqa: E402  - after the sys.path fix, on purpose


def main(argv: list[str]) -> int:
    assume_yes = "--yes" in argv or "-y" in argv

    with lab_db.connect(lab_db.ADMIN_DSN) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (lab_db.DB_NAME,)
        ).fetchone()
        if not exists:
            print(f"[teardown] {lab_db.DB_NAME} does not exist -- nothing to do")
            return 0

        size, = admin.execute(
            "SELECT pg_size_pretty(pg_database_size(%s))", (lab_db.DB_NAME,)
        ).fetchone()
        sessions = admin.execute(
            "SELECT pid, application_name, state, query "
            "FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (lab_db.DB_NAME,),
        ).fetchall()

        print(f"[teardown] database {lab_db.DB_NAME}  size {size}  "
              f"open sessions {len(sessions)}")
        for pid, app, state, query in sessions:
            snippet = " ".join((query or "").split())[:60]
            print(f"           pid {pid:<8}{app or '-':<20}{state:<12}{snippet}")

        if not assume_yes:
            answer = input(f"[teardown] drop {lab_db.DB_NAME}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("[teardown] left alone")
                return 1

        if sessions:
            killed = admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (lab_db.DB_NAME,),
            ).fetchall()
            print(f"[teardown] terminated {len(killed)} session(s)")

        admin.execute(f'DROP DATABASE IF EXISTS "{lab_db.DB_NAME}"')
        print(f"[teardown] dropped {lab_db.DB_NAME}")

    # Nothing else in this layer writes outside that database -- lab_db.py's
    # promise. If you find layer-4 state anywhere else, that is a defect.
    print("[teardown] done; nothing outside that database was touched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
