"""
Layer 7 · Topic 1 — Row-Level Security for real, on Postgres (Python/psycopg3).

One command, no arguments. It bootstraps an ephemeral database `sec_lab`
with two roles -- `lab_owner` (owns the table) and `app_user` (a NON-owner,
no BYPASSRLS, the role a real `api` connects as) -- an `invoices` table with
an RLS policy, and 30 interleaved rows. Then it demonstrates three things
the in-memory demo cannot, because they are properties of Postgres itself:

  A. THE FAKE FIX. Connect as the table OWNER and the identical RLS policy
     returns every row of every tenant. A table owner bypasses RLS silently
     by default -- no error, no warning. If you seed and test as the same
     role, every policy looks like it works while enforcing nothing. The
     only tell is `SELECT current_user, current_setting('row_security')`.

  B. THE REAL THING. Connect as app_user, SET LOCAL app.current_user inside
     the request transaction, and the same query returns only that tenant's
     rows -- even though the SQL never mentions owner_id.

  C. THE POOLING FOOTGUN. `SET` (session-scoped) survives a transaction
     boundary; `SET LOCAL` (transaction-scoped) does not. Under a pooler in
     transaction mode, the connection is handed to the next request between
     transactions -- so a plain `SET` bleeds one tenant's identity into
     whoever lands on that backend next. This script simulates the reused
     backend with one connection and two sequential transactions.

Requires: a running Postgres you can reach as a superuser (the lab machine
has one on localhost:5432). If it is down, this prints the exact reason and
exits 0 -- the code is the artifact.

What to look for: Demo A prints 30 (all rows) as the owner; Demo B prints 10
(alice only) as app_user; Demo C prints a LEAK line for `SET` and a
zero-rows "fails closed" line for `SET LOCAL`.
"""
import sys

try:
    import psycopg
except ImportError:
    print("psycopg3 not installed -> pip install 'psycopg[binary]'")
    sys.exit(0)

SUPER_DSN = "postgresql://localhost:5432/postgres"
APP_DSN = "postgresql://app_user@localhost:5432/sec_lab"
OWNER_DSN = "postgresql://lab_owner@localhost:5432/sec_lab"
TENANTS = {1: "alice", 2: "bob", 3: "carol"}


def bootstrap():
    """Idempotent: create db, roles, table, policy, rows. Superuser only."""
    with psycopg.connect(SUPER_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS sec_lab WITH (FORCE)")
        # Roles are cluster-global; create if absent.
        for role in ("lab_owner", "app_user"):
            exists = c.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()
            if not exists:
                # LOGIN so we can connect as them; NOBYPASSRLS is the default
                # and is the load-bearing property for app_user.
                c.execute(f"CREATE ROLE {role} LOGIN NOBYPASSRLS")
        c.execute("CREATE DATABASE sec_lab OWNER lab_owner")

    with psycopg.connect(OWNER_DSN, autocommit=True) as c:
        c.execute("""
            CREATE TABLE invoices (
                id        int PRIMARY KEY,
                owner_id  int NOT NULL,
                amount    int NOT NULL
            )""")
        for i in range(1, 31):
            c.execute("INSERT INTO invoices VALUES (%s,%s,%s)",
                      (i, ((i - 1) % 3) + 1, i * 100))
        c.execute("ALTER TABLE invoices ENABLE ROW LEVEL SECURITY")
        c.execute("""
            CREATE POLICY tenant_isolation ON invoices
            USING (owner_id = current_setting('app.current_user')::int)""")
        c.execute("GRANT SELECT, INSERT ON invoices TO app_user")


def demo_a_owner_bypass():
    print("A. Connect as the table OWNER (the fake fix):")
    with psycopg.connect(OWNER_DSN) as c:
        cur = c.cursor()
        who = cur.execute("SELECT current_user, current_setting('row_security')").fetchone()
        cur.execute("SELECT set_config('app.current_user','1',false)")  # session-scoped
        n = cur.execute("SELECT count(*) FROM invoices").fetchone()[0]
        print(f"   current_user={who[0]}  row_security={who[1]}")
        print(f"   rows visible while 'being alice': {n}  "
              f"<- ALL 30. RLS is silently OFF for the owner.\n")


def demo_b_app_user():
    print("B. Connect as app_user with SET LOCAL (the real control):")
    with psycopg.connect(APP_DSN) as c:
        cur = c.cursor()
        who = cur.execute("SELECT current_user, current_setting('row_security')").fetchone()
        cur.execute("SELECT set_config('app.current_user','1',true)")  # alice, txn-scoped (LOCAL)
        n = cur.execute("SELECT count(*) FROM invoices").fetchone()[0]
        owners = cur.execute(
            "SELECT DISTINCT owner_id FROM invoices ORDER BY 1").fetchall()
        print(f"   current_user={who[0]}  row_security={who[1]}")
        print(f"   rows visible as alice: {n}  owners seen: "
              f"{[o[0] for o in owners]}  <- only alice's 10\n")


def demo_c_pooling_footgun():
    print("C. One reused backend, two sequential transactions "
          "(the pooling footgun):")
    # SET (session-scoped): leaks across the transaction boundary.
    with psycopg.connect(APP_DSN) as c:
        cur = c.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT set_config('app.current_user','1',false)")  # request 1 = alice (session)
        cur.execute("COMMIT")                          # connection returned to pool
        cur.execute("BEGIN")                           # request 2 lands on same backend
        # request 2 forgot to set anything (or is a different tenant); the
        # session variable from request 1 is still here:
        try:
            n = cur.execute("SELECT count(*) FROM invoices").fetchone()[0]
            print(f"   SET (session):   request 2 sees {n} rows as 'alice' "
                  f"-> LEAK: request 1's identity survived the commit")
        except psycopg.errors.Error as e:
            print(f"   SET (session):   {e}")
        cur.execute("ROLLBACK")

    # SET LOCAL (transaction-scoped): dies at commit, fails closed.
    with psycopg.connect(APP_DSN) as c:
        cur = c.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT set_config('app.current_user','1',true)")   # LOCAL
        cur.execute("COMMIT")
        cur.execute("BEGIN")
        try:
            n = cur.execute("SELECT count(*) FROM invoices").fetchone()[0]
            print(f"   SET LOCAL:       request 2 sees {n} rows "
                  f"-> fails CLOSED: the setting did not survive the commit")
        except psycopg.errors.Error as e:
            # current_setting of an unset var raises unless a default exists;
            # that too is "fails closed" -- no rows leak.
            print(f"   SET LOCAL:       request 2 errors (no identity set) "
                  f"-> fails CLOSED, zero rows leak  [{e.diag.message_primary}]")
        cur.execute("ROLLBACK")


def main():
    print("Layer 7 · Topic 1 — Postgres RLS: the owner bypass and the "
          "SET/SET LOCAL leak\n")
    try:
        bootstrap()
    except psycopg.OperationalError as e:
        print(f"Postgres unreachable -> {str(e).strip()}")
        print("Start one (e.g. `pg_ctl start`) and re-run. Code is the artifact.")
        return
    demo_a_owner_bypass()
    demo_b_app_user()
    demo_c_pooling_footgun()
    print("\nTakeaway: the control that survives a growing team lives at the "
          "data layer -- but only when (1) the app connects as a NON-owner "
          "and (2) identity is set with SET LOCAL, inside the request's "
          "transaction. Miss either and the policy is decorative.")


if __name__ == "__main__":
    main()
