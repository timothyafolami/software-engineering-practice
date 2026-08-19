"""
Drop the layer-3 scratch database. One command, no arguments.

    python3 lab/local/teardown_lab.py

Nothing else in this layer writes to any database but sep_lab_03_data, so this is
the whole cleanup. It will refuse to run if LAB_DSN points somewhere else.
"""
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab_db


def main() -> None:
    if lab_db.DB_NAME not in lab_db.DSN:
        sys.exit(f"refusing: LAB_DSN={lab_db.DSN} is not the lab database")
    with psycopg.connect(lab_db.ADMIN_DSN, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (lab_db.DB_NAME,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{lab_db.DB_NAME}"')
    print(f"dropped database {lab_db.DB_NAME}")


if __name__ == "__main__":
    main()
