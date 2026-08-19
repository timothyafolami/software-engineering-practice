"""
Partition pruning, and what partition count costs you in planning time.

    python3 08-replication-lag/python/partition_pruning.py

WHAT IT DEMONSTRATES: the same table, partitioned three ways -- 12, 36 and 120
RANGE partitions over the same three years of data -- and four query shapes run
against each.

  prune       filters on the PARTITION KEY. One partition survives.
  no prune    filters on `id` only. Every partition is touched, because the
              planner has no way to know which one holds that id.
  order+limit ORDER BY created_at DESC LIMIT 10. Does pruning survive it?
              Predict this one before you look; it is the one people get wrong.
  runtime     the partition key compared against a value the planner cannot see
              until execution. Pruning still happens -- at RUN time, which
              EXPLAIN reports separately as `Subplans Removed`.

WHAT TO LOOK FOR:
  * `Subplans Removed: N` in the plan, not the timing. A fast query over 12
    partitions proves nothing; the counter is the evidence.
  * PLANNING time against partition count. Execution time barely moves and
    planning time does, because the planner considers every partition before it
    discards them. That cost is paid on every execution, and it is the reason
    "partition everything by day" is a decision with a bill attached.

Uses its own `orders_part` table so the seeded `orders` that Topics 3, 4 and 6
read is never converted underneath them. sql/partition_orders.sql builds the
same 36-partition version as a readable artifact.

Knobs: PARTITION_COUNTS (comma-separated), REPEATS, KEEP (leave the last table
in place instead of dropping it).
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lab", "local"))
import lab_db  # noqa: E402

PARTITION_COUNTS = [int(x) for x in os.environ.get("PARTITION_COUNTS", "12,36,120").split(",")]
REPEATS = int(os.environ.get("REPEATS", "5"))
KEEP = os.environ.get("KEEP", "") not in ("", "0", "false")
TABLE = "orders_part_bench"

QUERIES = [
    ("prune (partition key)",
     "SELECT count(*), sum(total_cents) FROM {t} "
     "WHERE created_at >= timestamptz '2024-06-01' AND created_at < timestamptz '2024-06-15'"),
    ("no prune (id only)",
     "SELECT count(*), sum(total_cents) FROM {t} WHERE id = 424242"),
    ("order by key desc limit",
     "SELECT id, created_at FROM {t} ORDER BY created_at DESC LIMIT 10"),
    # The boundary comes from a subquery over a DIFFERENT table, so the planner
    # cannot fold it into a constant and has to defer pruning to execution. The
    # subquery deliberately does not reference {t}: pointing it at the
    # partitioned table itself makes that table appear twice in the plan and
    # measures something else.
    ("runtime pruning",
     "SELECT count(*) FROM {t} WHERE created_at >= "
     "(SELECT max(created_at) - interval '10 days' FROM orders)"),
]


def build(conn, n_parts: int) -> None:
    """Rebuild the benchmark table with n_parts equal RANGE partitions.

    Partition boundaries are computed from the data's own min and max, so 12,
    36 and 120 all cover exactly the same rows -- which is what makes the
    planning-time comparison mean anything.
    """
    conn.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
    lo, hi = conn.execute("SELECT min(created_at), max(created_at) FROM orders").fetchone()
    conn.execute(
        f"""
        CREATE TABLE {TABLE} (
            id          bigint      NOT NULL,
            customer_id bigint      NOT NULL,
            status      text        NOT NULL,
            total_cents bigint      NOT NULL,
            created_at  timestamptz NOT NULL,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    # Boundaries computed in Python and issued one CREATE TABLE at a time. A
    # plpgsql DO block would have been tidier to look at and cannot take bound
    # parameters at all -- its body is a string literal to the server, so `%s`
    # inside it is not a placeholder, it is text.
    span = (hi - lo) / n_parts
    for i in range(n_parts):
        part_lo = lo + span * i
        part_hi = hi + timedelta(days=1) if i == n_parts - 1 else lo + span * (i + 1)
        # FOR VALUES takes literals, not bound parameters: partition bounds are
        # part of the catalogue definition, fixed when the table is created, not
        # values supplied at execution. Both timestamps here came out of the
        # database a moment ago, so there is nothing untrusted to interpolate.
        conn.execute(
            f"CREATE TABLE {TABLE}_{i:04d} PARTITION OF {TABLE} "
            f"FOR VALUES FROM ('{part_lo.isoformat()}') TO ('{part_hi.isoformat()}')")
    conn.execute(
        f"INSERT INTO {TABLE} (id, customer_id, status, total_cents, created_at) "
        "SELECT id, customer_id, status, total_cents, created_at FROM orders")
    conn.execute(f"ANALYZE {TABLE}")


def subplans_removed(explained: dict) -> int:
    return sum(n.get("Subplans Removed", 0) or 0
               for n in lab_db.walk_plan(lab_db.plan_root(explained)))


def scanned_partitions(explained: dict) -> tuple[int, int]:
    """(partitions in the plan, partitions that were never executed).

    Two counters because Postgres reports pruning two different ways depending
    on when it happened:

      PLAN-TIME pruning     the partition is not in the plan at all, so the
                            first counter drops.
      RUN-TIME pruning      the partition IS in the plan -- the planner could
                            not evaluate the boundary yet -- and the node shows
                            `(never executed)`, `Actual Loops: 0`. The plan
                            looks enormous and almost none of it ran.

    `Subplans Removed: N` is a third form, reported when the executor can drop
    subplans wholesale. Read all three; a query that prunes perfectly at run
    time still shows every partition in EXPLAIN, and reading only the plan size
    would tell you pruning failed.
    """
    parts = [n for n in lab_db.walk_plan(lab_db.plan_root(explained))
             if str(n.get("Relation Name", "")).startswith(f"{TABLE}_")]
    never = sum(1 for n in parts if (n.get("Actual Loops", 1) or 0) == 0)
    return len(parts), never


def measure(conn, sql: str) -> dict:
    for _ in range(2):
        lab_db.explain(conn, sql)
    plan_times, exec_times = [], []
    ex = None
    for _ in range(REPEATS):
        ex = lab_db.explain(conn, sql)
        plan_times.append(ex["Planning Time"])
        exec_times.append(ex["Execution Time"])
    in_plan, never = scanned_partitions(ex)
    return {
        "plan_ms": lab_db.percentile(plan_times, 50),
        "exec_ms": lab_db.percentile(exec_times, 50),
        "removed": subplans_removed(ex),
        "scanned": in_plan,
        "never": never,
    }


def main() -> None:
    lab_db.ensure_database()
    with lab_db.connect() as conn:
        lab_db.tune_session(conn)
        lab_db.banner(f"Partition pruning -- {lab_db.describe_server(conn)}")
        lab_db.ensure_big_seed(conn)
        rows = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
        print(f"  {rows:,} orders, repartitioned {len(PARTITION_COUNTS)} ways over the same")
        print("  three years. Every configuration holds identical data.")
        print("\n  Predict before reading: does pruning survive `ORDER BY created_at DESC")
        print("  LIMIT 10`? And is planning time from 12 to 120 partitions linear, or worse?")

        results = {}
        for n_parts in PARTITION_COUNTS:
            print(f"\n  building {n_parts} partitions...", flush=True)
            build(conn, n_parts)
            print(f"  {'query':<26}{'in plan':>9}{'never executed':>16}"
                  f"{'subplans removed':>18}{'plan ms':>10}{'exec ms':>10}")
            print("  " + "-" * 89)
            for label, template in QUERIES:
                r = measure(conn, template.format(t=TABLE))
                results[(label, n_parts)] = r
                print(f"  {label:<26}{r['scanned']:>9}{r['never']:>16}{r['removed']:>18}"
                      f"{r['plan_ms']:>10.3f}{r['exec_ms']:>10.2f}")

        print("\n  planning time against partition count -- the cost you pay every execution:")
        print(f"    {'query':<26}" + "".join(f"{n:>12}" for n in PARTITION_COUNTS))
        print("    " + "-" * (26 + 12 * len(PARTITION_COUNTS)))
        for label, _t in QUERIES:
            cells = "".join(f"{results[(label, n)]['plan_ms']:>12.3f}" for n in PARTITION_COUNTS)
            print(f"    {label:<26}{cells}")
        first, last = PARTITION_COUNTS[0], PARTITION_COUNTS[-1]
        count_growth = last / first
        pruned = [results[("prune (partition key)", n)]["plan_ms"] for n in (first, last)]
        unpruned = [results[("no prune (id only)", n)]["plan_ms"] for n in (first, last)]
        print(f"\n    {first} -> {last} partitions is {count_growth:.0f}x more partitions.")
        print(f"    pruned query:   {pruned[0]:.3f} -> {pruned[1]:.3f} ms planning "
              f"({pruned[1] / max(pruned[0], 1e-9):.1f}x)")
        print(f"    unpruned query: {unpruned[0]:.3f} -> {unpruned[1]:.3f} ms planning "
              f"({unpruned[1] / max(unpruned[0], 1e-9):.1f}x)")
        print("    Those two rows are the whole trade. When the partition key IS in the")
        print("    WHERE clause, pruning happens early enough that planning stays flat.")
        print("    When it is not, the planner builds a subplan per partition and the cost")
        print("    grows faster than the partition count does -- on EVERY execution, of a")
        print("    query whose execution time is a fraction of a millisecond.")

        ol = [results[("order by key desc limit", n)] for n in PARTITION_COUNTS]
        print()
        if all(r["scanned"] > 1 for r in ol):
            print("  `ORDER BY created_at DESC LIMIT 10` did NOT prune: it appears in the plan")
            print(f"  across {ol[-1]['scanned']} partitions. There is no WHERE clause on the")
            print("  partition key, so there is nothing to prune WITH -- the planner has to")
            print("  merge from every partition and then take ten rows. Ordering by the")
            print("  partition key is not the same as filtering on it, and that distinction")
            print("  costs the most on exactly the table you partitioned to make it cheap.")
        else:
            print("  `ORDER BY created_at DESC LIMIT 10` reached a single partition on this")
            print("  server -- read the plan and find out what let it: a MergeAppend over")
            print("  per-partition indexes can stop early once the LIMIT is satisfied.")

        rt = results[("runtime pruning", PARTITION_COUNTS[-1])]
        if rt["never"]:
            print(f"\n  runtime pruning: {rt['scanned']} partitions in the plan, "
                  f"{rt['never']} of them `(never executed)`.")
            print("  The boundary came from a subquery the planner could not evaluate, so it")
            print("  could not prune while planning -- it built a subplan per partition and")
            print("  then skipped almost all of them at execution. The plan looks enormous")
            print("  and hardly any of it ran. If you judge pruning by the size of the plan")
            print("  you will conclude it failed; `(never executed)` and `Actual Loops: 0`")
            print("  are where it actually shows.")

        if KEEP:
            print(f"\n  KEEP set -- {TABLE} left in place at {PARTITION_COUNTS[-1]} partitions.")
        else:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
            print(f"\n  ({TABLE} dropped -- the seeded `orders` was never touched)")

        print("\n  The connection back to Topic 2, which is the real payoff: dropping a")
        print("  partition is a catalogue operation. Deleting the same rows from an")
        print("  unpartitioned table creates a dead tuple for each one, and every one of")
        print("  those is vacuum work that happens later, under load, at a time you did")
        print("  not choose.")


if __name__ == "__main__":
    main()
