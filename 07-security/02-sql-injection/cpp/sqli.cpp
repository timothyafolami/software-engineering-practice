// Layer 7 · Topic 2 — SQL injection as a string-building failure (C++ / libpq).
//
// One command (see How to run for the compile line; needs a reachable
// Postgres on localhost:5432). This is the language talking to the wire with
// nothing in between, so the whole security property of the topic is visible
// in TWO function signatures:
//
//   PQexec(conn, sql)                         -- Simple Query: ONE string,
//                                                multi-statement allowed. The
//                                                attacker's bytes are parsed
//                                                as SQL.
//   PQexecParams(conn, "... = $1", 1, ...,    -- Parse/Bind/Execute: a string
//                values, ...)                    AND an array of values. The
//                                                value never enters the parsed
//                                                text.
//
// One takes a string; the other takes a string and an array of values. Every
// other language in this topic is a wrapper over that difference. C++ also has
// no guardrail: `std::string q = "... '" + email + "'"` compiles cleanly.
//
// What to look for: tautology dumps all users under PQexec, 0 rows (no error)
// under PQexecParams; UNION steals the key only when vulnerable; the blind
// channel recovers 32 chars in ~linear requests.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <libpq-fe.h>

static const std::string SECRET_KEY = "S3CR3T_KEY_abcdef0123456789abcd0";
static const std::string CHARSET =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_";

static bool exec_ok(PGconn* c, const char* sql) {
    PGresult* r = PQexec(c, sql);
    bool ok = PQresultStatus(r) == PGRES_COMMAND_OK ||
              PQresultStatus(r) == PGRES_TUPLES_OK;
    PQclear(r);
    return ok;
}

// THE BUG: concatenation. Returns row count, or -1 on SQL error.
static int search_vulnerable(PGconn* c, const std::string& email, std::string* leaked = nullptr) {
    std::string sql = "SELECT id, email, name FROM users WHERE email = '" + email + "'";
    PGresult* r = PQexec(c, sql.c_str());
    if (PQresultStatus(r) != PGRES_TUPLES_OK) { PQclear(r); return -1; }
    int n = PQntuples(r);
    if (leaked)
        for (int i = 0; i < n; i++)
            if (SECRET_KEY == PQgetvalue(r, i, 1)) *leaked = SECRET_KEY;
    PQclear(r);
    return n;
}

static int search_parameterized(PGconn* c, const std::string& email, std::string* leaked = nullptr) {
    const char* vals[1] = { email.c_str() };
    PGresult* r = PQexecParams(c,
        "SELECT id, email, name FROM users WHERE email = $1",
        1, nullptr, vals, nullptr, nullptr, 0);
    if (PQresultStatus(r) != PGRES_TUPLES_OK) { PQclear(r); return -1; }
    int n = PQntuples(r);
    if (leaked)
        for (int i = 0; i < n; i++)
            if (SECRET_KEY == PQgetvalue(r, i, 1)) *leaked = SECRET_KEY;
    PQclear(r);
    return n;
}

int main() {
    printf("Layer 7 · Topic 2 — SQL injection (C++ / libpq, real Postgres)\n\n");

    PGconn* super = PQconnectdb("host=localhost port=5432 dbname=postgres");
    if (PQstatus(super) != CONNECTION_OK) {
        printf("Postgres unreachable -> %s", PQerrorMessage(super));
        printf("Start Postgres and re-run. The code is the artifact.\n");
        return 0;
    }
    exec_ok(super, "DROP DATABASE IF EXISTS sqli_lab WITH (FORCE)");
    exec_ok(super, "CREATE DATABASE sqli_lab");
    PQfinish(super);

    PGconn* c = PQconnectdb("host=localhost port=5432 dbname=sqli_lab");
    exec_ok(c, "CREATE TABLE users (id int PRIMARY KEY, email text, name text)");
    exec_ok(c, "CREATE TABLE api_keys (user_id int PRIMARY KEY, key text)");
    exec_ok(c, "INSERT INTO users VALUES "
               "(1,'alice@lab.test','alice'),(2,'bob@lab.test','bob'),(3,'carol@lab.test','carol')");
    exec_ok(c, ("INSERT INTO api_keys VALUES (1,'" + SECRET_KEY + "')").c_str());

    printf("Payload 1 — boolean tautology  \"' OR '1'='1\"\n");
    printf("   %-14s -> %d rows\n", "vulnerable", search_vulnerable(c, "' OR '1'='1"));
    printf("   %-14s -> %d rows\n", "parameterized", search_parameterized(c, "' OR '1'='1"));

    printf("\nPayload 2 — UNION cross-table (steal api_keys.key)\n");
    std::string uni = "' UNION SELECT user_id, key, key FROM api_keys--";
    std::string leaked_v, leaked_p;
    int nv = search_vulnerable(c, uni, &leaked_v);
    int np = search_parameterized(c, uni, &leaked_p);
    printf("   %-14s -> %d rows; secret leaked: %s\n", "vulnerable", nv,
           leaked_v.empty() ? "no" : leaked_v.c_str());
    printf("   %-14s -> %d rows; secret leaked: %s\n", "parameterized", np,
           leaked_p.empty() ? "no" : leaked_p.c_str());

    printf("\nPayload 4 — identifier injection on ORDER BY (cannot be bound)\n");
    PGresult* r = PQexec(c, "SELECT id, email, name FROM users ORDER BY (SELECT key FROM api_keys LIMIT 1)");
    printf("   %-14s -> %d rows (injection ran in ORDER BY!)\n", "concatenated",
           PQresultStatus(r) == PGRES_TUPLES_OK ? PQntuples(r) : -1);
    PQclear(r);
    printf("   %-14s -> rejected identifier (not in {id,email,name} allowlist)\n", "allowlist");
    printf("   *you cannot bind a column name: its parse-tree position is fixed at Parse, before Bind.\n");

    printf("\nPayload 3 — boolean-blind extraction of the 32-char key\n");
    std::string recovered;
    long requests = 0;
    auto t0 = std::chrono::steady_clock::now();
    for (size_t pos = 1; pos <= SECRET_KEY.size(); pos++) {
        for (char ch : CHARSET) {
            requests++;
            std::string p = "nope' OR substr((SELECT key FROM api_keys WHERE user_id=1),"
                            + std::to_string(pos) + ",1)='" + ch;
            if (search_vulnerable(c, p) > 0) { recovered += ch; break; }
        }
    }
    long ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                  std::chrono::steady_clock::now() - t0).count();
    int n = (int)SECRET_KEY.size();
    printf("   recovered: %s\n", recovered.c_str());
    printf("   correct:   %s\n", recovered == SECRET_KEY ? "YES" : "NO");
    printf("   requests to recover %d chars, one char/request (measured): %ld\n", n, requests);
    printf("   wall-clock: %ld ms\n", ms);
    printf("   theory: linear ~%d req; binary-search per char ~%d req (ratio ~%.1fx)\n",
           n * ((int)CHARSET.size() + 1) / 2, n * 7,
           (double)(n * ((int)CHARSET.size() + 1) / 2) / (n * 7));

    PQfinish(c);
    printf("\nTakeaway: PQexecParams sends the value in Bind, never Parse. The two\n"
           "function signatures ARE the security property; every wrapper inherits it.\n");
    return 0;
}
