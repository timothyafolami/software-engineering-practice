"""
Create the layer-3 scratch database, schema and seed. One command, no arguments.

    python3 lab/local/setup_lab.py

You do not have to run this first -- every topic program provisions what it needs
on its own. Run it when you want the one-time seed cost paid up front rather than
inside the first experiment you happen to try.

Scale:  LAB_SCALE=small (default) or LAB_SCALE=full
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab_db


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.banner(f"Layer 3 lab -- {lab_db.describe_server(conn)}")
        print(f"database : {lab_db.DSN}")
        print(f"scale    : {lab_db.SCALE}")
        lab_db.ensure_core_tables(conn)
        lab_db.reset_accounts(conn)
        lab_db.reset_oncall(conn)
        print("[lab] accounts (10,000) and oncall (100 shifts x 2) ready")
        lab_db.ensure_big_seed(conn)
        for table in ("customers", "orders", "line_items"):
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            size = lab_db.human_bytes(lab_db.table_bytes(conn, table))
            print(f"  {table:<12} {n:>12,} rows   {size:>10}")


if __name__ == "__main__":
    main()
