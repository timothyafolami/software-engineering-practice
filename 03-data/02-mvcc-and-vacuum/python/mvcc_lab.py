"""
Shared table for the MVCC/vacuum programs in this topic.

Deliberately NOT the shared `orders` table: these programs bloat what they touch
on purpose, and topics 3 and 4 read `orders` expecting the seeded shape. So this
topic gets its own copy, `mvcc_orders`, and is free to wreck it.

The schema matters in one specific way. `status` is indexed, so an UPDATE that
changes `status` can never be a HOT update -- it must write a new index entry in
every index on the table. That is what produces dead tuples that survive on the
same page, which is what this whole topic is about. Update a NON-indexed column
instead and Postgres uses HOT, cleans up on the same page, and you will sit there
watching n_dead_tup stay near zero wondering why the experiment does nothing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

TABLE = "mvcc_orders"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id          bigint PRIMARY KEY,
    customer_id bigint      NOT NULL,
    status      text        NOT NULL,
    total_cents bigint      NOT NULL,
    note        text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mvcc_status   ON {TABLE} (status);
CREATE INDEX IF NOT EXISTS idx_mvcc_customer ON {TABLE} (customer_id) INCLUDE (total_cents);
"""


def ensure_table(conn, rows: int) -> None:
    conn.execute(DDL)
    have = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    if have == rows:
        return
    conn.execute(f"TRUNCATE {TABLE}")
    conn.execute(
        f"""
        INSERT INTO {TABLE} (id, customer_id, status, total_cents, created_at)
        SELECT g,
               1 + mod(g * 2654435761::bigint, 50000),
               CASE WHEN mod(g, 100) < 92 THEN 'complete' ELSE 'pending' END,
               100 + mod(g::bigint * 7919, 500000),
               timestamptz '2024-01-01' + mod(g, 365) * interval '1 day'
        FROM generate_series(1, %s) g
        """,
        (rows,),
    )
    conn.execute(f"VACUUM (ANALYZE) {TABLE}")


def stats(conn) -> dict:
    """One sample of everything this topic cares about."""
    conn.execute("SELECT pg_stat_clear_snapshot()")  # stats are snapshotted per txn
    row = conn.execute(
        """
        SELECT n_live_tup, n_dead_tup, n_tup_upd, n_tup_hot_upd, autovacuum_count
        FROM pg_stat_user_tables WHERE relname = %s
        """,
        (TABLE,),
    ).fetchone() or (0, 0, 0, 0, 0)
    table_bytes = conn.execute(f"SELECT pg_table_size('{TABLE}')").fetchone()[0]
    index_bytes = conn.execute(f"SELECT pg_indexes_size('{TABLE}')").fetchone()[0]
    return {
        "live": row[0], "dead": row[1], "upd": row[2], "hot": row[3], "autovacuums": row[4],
        "table_bytes": table_bytes, "index_bytes": index_bytes,
    }


def churn(conn, updates: int, rows: int) -> None:
    """Update an INDEXED column, so none of these can be HOT updates."""
    conn.execute(
        f"""
        UPDATE {TABLE} SET status = CASE WHEN status = 'complete' THEN 'pending' ELSE 'complete' END
        WHERE id IN (SELECT 1 + mod((random() * %s)::bigint, %s) FROM generate_series(1, %s))
        """,
        (rows, rows, updates),
    )


def horizon_holders(conn) -> list[tuple]:
    """Everything currently capable of holding back the cluster's xmin horizon.

    Three separate mechanisms, three separate places to look. A session that
    looks perfectly idle in `pg_stat_activity` and a replication slot whose
    consumer died produce identical symptoms and neither of them looks like an
    error anywhere in your monitoring.
    """
    sessions = conn.execute(
        """
        SELECT pid, state, backend_xmin::text,
               COALESCE(round(extract(epoch FROM now() - xact_start))::text, '-') AS xact_age_s,
               left(COALESCE(query, ''), 40)
        FROM pg_stat_activity
        WHERE backend_xmin IS NOT NULL AND pid <> pg_backend_pid()
        ORDER BY age(backend_xmin) DESC
        """
    ).fetchall()
    slots = conn.execute(
        """
        SELECT slot_name, slot_type, active, xmin::text, catalog_xmin::text
        FROM pg_replication_slots ORDER BY slot_name
        """
    ).fetchall()
    return sessions, slots


def vacuum_verbose(conn, table: str = TABLE) -> list[str]:
    """Run VACUUM VERBOSE and return the server's own notices.

    The line to read is `dead row versions cannot be removed yet, oldest xmin: N`
    -- that is vacuum telling you, in production, exactly which transaction id is
    holding it back. Most people never see it because nobody runs vacuum by hand.
    """
    notices: list[str] = []
    conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
    try:
        conn.execute(f"VACUUM (VERBOSE) {table}")
    finally:
        conn.remove_notice_handler  # psycopg keeps the handler on this connection only
    return notices


def interesting_notice_lines(notices: list[str]) -> list[str]:
    keep = ("cannot be removed yet", "removable", "dead row versions", "oldest xmin",
            "removed", "tuples:", "pages:")
    out = []
    for message in notices:
        for line in message.splitlines():
            if any(k in line for k in keep):
                out.append(line.strip())
    return out
