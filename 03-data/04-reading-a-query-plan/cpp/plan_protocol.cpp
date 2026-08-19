// Layer 3, Topic 4 - the three ways to send a query, chosen by hand.
//
// WHAT IT DEMONSTRATES: libpq is what psycopg, pgx and node-postgres are all
// built on, and it is the only client in this topic where you pick the protocol
// message yourself rather than inheriting somebody's default:
//
//   PQexec           simple query protocol. One round trip. No parameter
//                    binding at all -- values are text inside the SQL, so
//                    every distinct value is a distinct statement to the
//                    server, and interpolating anything untrusted here is a
//                    SQL injection. Always custom-planned, because every
//                    execution is a brand new statement.
//   PQexecParams     extended protocol, UNNAMED statement. Parameters are sent
//                    out of band, typed, never parsed as SQL. Parsed and
//                    planned every execution, with the real values in front of
//                    the planner. This is what node-postgres does by default.
//   PQprepare +      extended protocol, NAMED statement. A real object with a
//   PQexecPrepared   name, living on the SERVER, inside this SESSION, until
//                    this session ends or you DEALLOCATE it. Parsed and planned
//                    once. This is what pgx does by default, and what psycopg3
//                    does after prepare_threshold executions.
//
// WHAT TO LOOK FOR:
//  1. The timing table: three ways to run the same query, differing in how much
//     work the server repeats per execution.
//  2. pg_prepared_statements before and after PQprepare, then again from a
//     SECOND connection opened at the end. The named statement is invisible
//     from the other connection, because it never belonged to the database --
//     it belongs to a session. That single fact is why a transaction-mode
//     pooler, which hands your next transaction a different session, breaks
//     drivers that prepare and does not break drivers that do not.
//  3. `Index Cond: (status = $1)` once a plan goes generic, versus the literal
//     value under PQexec -- and the fact that the named statement driven with a
//     RARE value never goes generic at all, while the one driven with the COMMON
//     value does at execution six. Same server, same SQL, opposite decision,
//     and the only variable is which parameter value arrived first.
//
// Build and run (from the 03-data directory):
//   g++ -O2 -std=c++17 -I"$(pg_config --includedir)" -L"$(pg_config --libdir)" \
//       -Wl,-rpath,"$(pg_config --libdir)" \
//       -o /tmp/plan_protocol 04-reading-a-query-plan/cpp/plan_protocol.cpp -lpq && /tmp/plan_protocol
//
// The -rpath is not optional on macOS: without it the binary links but cannot
// find libpq.5.dylib at run time, because Homebrew's Postgres is keg-only and
// not on the default loader path.
//
// pg_config comes with any Postgres client install, Homebrew's included. If it
// is not on PATH, `python3 lab/local/check_env.py` says so.
//
// DSN: LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
// PORTABILITY: plain libpq and <chrono>. Nothing here is Linux-specific.

#include <libpq-fe.h>

#include <chrono>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr int kRepeats = 300;
const char* kRare = "failed";      // ~1% of orders
const char* kCommon = "complete";  // ~92% of orders

const char* kSqlParams =
    "SELECT count(*), sum(total_cents) FROM orders WHERE status = $1";

std::string dsn() {
  const char* env = std::getenv("LAB_PG_URL");
  return env && *env ? env : "postgres:///sep_lab_03_data?host=/tmp";
}

// Every libpq result must be cleared, and the failure path is where people
// forget. One guard type removes the question.
class Result {
 public:
  explicit Result(PGresult* r) : r_(r) {}
  ~Result() { PQclear(r_); }
  Result(const Result&) = delete;
  Result& operator=(const Result&) = delete;
  PGresult* get() const { return r_; }
  bool ok() const {
    ExecStatusType s = PQresultStatus(r_);
    return s == PGRES_TUPLES_OK || s == PGRES_COMMAND_OK;
  }
  std::string field(int row, int col) const {
    const char* v = PQgetvalue(r_, row, col);
    return v ? v : "";
  }
  int rows() const { return PQntuples(r_); }

 private:
  PGresult* r_;
};

void must(PGconn* conn, const Result& res, const std::string& what) {
  if (!res.ok()) {
    std::cerr << "\n" << what << ": " << PQerrorMessage(conn);
    std::cerr << "unblock: python3 lab/local/check_env.py, "
                 "then python3 lab/local/setup_lab.py\n";
    std::exit(1);
  }
}

void exec(PGconn* conn, const std::string& sql) {
  Result r(PQexec(conn, sql.c_str()));
  must(conn, r, sql);
}

double ms_since(std::chrono::steady_clock::time_point t0) {
  return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
      .count();
}

// The plan of a statement, reduced to the scan type and whether it is generic.
struct PlanInfo {
  std::string scan = "seq scan";
  bool generic = false;
  std::string cond = "-";
};

PlanInfo plan_of(PGconn* conn, const std::string& explain_sql) {
  PlanInfo info;
  Result r(PQexec(conn, explain_sql.c_str()));
  must(conn, r, explain_sql);
  for (int i = 0; i < r.rows(); ++i) {
    std::string line = r.field(i, 0);
    if (line.find("Index Scan") != std::string::npos) info.scan = "index scan";
    else if (line.find("Bitmap") != std::string::npos && info.scan == "seq scan")
      info.scan = "bitmap";
    if (line.find("$1") != std::string::npos) info.generic = true;
    if (info.cond == "-" &&
        (line.find("Cond:") != std::string::npos || line.find("Filter:") != std::string::npos)) {
      size_t start = line.find_first_not_of(" \t->");
      info.cond = start == std::string::npos ? line : line.substr(start);
    }
  }
  return info;
}

void print_prepared(PGconn* conn, const std::string& label) {
  Result r(PQexec(conn,
                  "SELECT name, generic_plans, custom_plans FROM pg_prepared_statements "
                  "ORDER BY name"));
  must(conn, r, "pg_prepared_statements");
  std::cout << "    " << label << ": ";
  if (r.rows() == 0) {
    std::cout << "(none)\n";
    return;
  }
  std::cout << "\n";
  for (int i = 0; i < r.rows(); ++i) {
    std::cout << "      " << std::left << std::setw(16) << r.field(i, 0)
              << " generic=" << r.field(i, 1) << " custom=" << r.field(i, 2) << "\n";
  }
}

}  // namespace

int main() {
  PGconn* conn = PQconnectdb(dsn().c_str());
  if (PQstatus(conn) != CONNECTION_OK) {
    std::cerr << "could not connect to " << dsn() << ": " << PQerrorMessage(conn);
    std::cerr << "unblock: python3 lab/local/check_env.py\n";
    PQfinish(conn);
    return 1;
  }

  std::cout << std::string(78, '=') << "\n";
  std::cout << "libpq: three protocol messages, by hand\n";
  std::cout << std::string(78, '=') << "\n";
  {
    Result v(PQexec(conn, "SELECT version()"));
    must(conn, v, "version");
    std::string s = v.field(0, 0);
    std::cout << s.substr(0, s.find(" on ")) << "\n";
  }

  exec(conn, "SET random_page_cost = 1.1");
  exec(conn, "SET effective_cache_size = '1GB'");
  exec(conn, "CREATE INDEX IF NOT EXISTS idx_cpp_protocol_status ON orders (status)");
  exec(conn, "ANALYZE orders");
  exec(conn, "DEALLOCATE ALL");

  std::cout << "\n  before anything is prepared:\n";
  print_prepared(conn, "pg_prepared_statements");

  // ------------------------------------------------------------------------
  // 1. PQexec -- simple query protocol, values inside the SQL text.
  //
  // kRare is a compile-time constant here. If it were user input this line
  // would be a SQL injection, and that is not a stylistic point: the simple
  // query protocol has no mechanism for binding a value, so a program that
  // needs one has to leave this protocol, not escape harder.
  // ------------------------------------------------------------------------
  const std::string literal_sql =
      std::string("SELECT count(*), sum(total_cents) FROM orders WHERE status = '") + kRare + "'";

  auto t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < kRepeats; ++i) {
    Result r(PQexec(conn, literal_sql.c_str()));
    must(conn, r, "PQexec");
  }
  const double simple_ms = ms_since(t0);
  const PlanInfo simple_plan = plan_of(conn, "EXPLAIN " + literal_sql);

  // ------------------------------------------------------------------------
  // 2. PQexecParams -- extended protocol, unnamed statement.
  // ------------------------------------------------------------------------
  const char* values[1] = {kRare};
  t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < kRepeats; ++i) {
    Result r(PQexecParams(conn, kSqlParams, 1, nullptr, values, nullptr, nullptr, 0));
    must(conn, r, "PQexecParams");
  }
  const double params_ms = ms_since(t0);

  std::cout << "\n  after " << kRepeats << " PQexec and " << kRepeats
            << " PQexecParams executions:\n";
  print_prepared(conn, "pg_prepared_statements");
  std::cout << "    Neither protocol leaves anything behind. PQexecParams sent an\n"
               "    unnamed statement, which the server discards after each execution.\n";

  // ------------------------------------------------------------------------
  // 3. PQprepare + PQexecPrepared -- a named object with a session lifetime.
  // ------------------------------------------------------------------------
  {
    Result p(PQprepare(conn, "cpp_ps", kSqlParams, 1, nullptr));
    must(conn, p, "PQprepare");
  }
  t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < kRepeats; ++i) {
    Result r(PQexecPrepared(conn, "cpp_ps", 1, values, nullptr, nullptr, 0));
    must(conn, r, "PQexecPrepared");
  }
  const double prepared_ms = ms_since(t0);

  std::cout << "\n  after " << kRepeats << " PQexecPrepared executions:\n";
  print_prepared(conn, "pg_prepared_statements");

  std::string rare_generic = "?", rare_custom = "?";
  {
    Result r(PQexec(conn, "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
                          "WHERE name = 'cpp_ps'"));
    must(conn, r, "cpp_ps counters");
    if (r.rows() == 1) {
      rare_generic = r.field(0, 0);
      rare_custom = r.field(0, 1);
    }
  }

  // The plan the prepared statement is actually running. A SQL-level twin is
  // used because EXPLAIN cannot reach into a statement prepared over the
  // protocol -- both live in the same per-session plan cache and follow the
  // same custom-then-generic rule.
  exec(conn, std::string("PREPARE cpp_sql(text) AS ") + kSqlParams);
  for (int i = 0; i < 6; ++i) {
    Result r(PQexec(conn, (std::string("EXECUTE cpp_sql('") + kCommon + "')").c_str()));
    must(conn, r, "EXECUTE cpp_sql");
  }
  const PlanInfo prepared_plan =
      plan_of(conn, std::string("EXPLAIN EXECUTE cpp_sql('") + kCommon + "')");

  // ------------------------------------------------------------------------
  // Results.
  // ------------------------------------------------------------------------
  std::cout << "\n  " << kRepeats << " executions of the same query, three ways:\n";
  std::cout << "    " << std::left << std::setw(24) << "protocol message"
            << std::right << std::setw(10) << "total ms" << std::setw(12) << "per exec"
            << "  " << std::left << "plan\n";
  auto row = [](const char* name, double total, const std::string& plan) {
    std::cout << "    " << std::left << std::setw(24) << name << std::right << std::fixed
              << std::setprecision(1) << std::setw(10) << total << std::setprecision(3)
              << std::setw(12) << total / kRepeats << "  " << std::left << plan << "\n";
  };
  row("PQexec (simple)", simple_ms, simple_plan.scan + ", custom (" + simple_plan.cond + ")");
  row("PQexecParams (unnamed)", params_ms, "custom every time, nothing cached");
  row("PQexecPrepared (named)", prepared_ms, std::string("see the two lines below"));

  // Two different statements are involved, and saying so is the point rather
  // than a footnote: the TIMING above is cpp_ps, executed 300 times with the
  // RARE value; the PLAN below is the SQL-level twin, executed six times with
  // the COMMON one. EXPLAIN cannot reach into a statement prepared over the
  // protocol, so the twin is the only way to see a plan at all -- and the two
  // land on opposite sides of the custom/generic decision, which is the more
  // useful result.
  std::cout << "      cpp_ps, driven 300x with the rare value '" << kRare
            << "': " << rare_generic << " generic / " << rare_custom << " custom plans\n";
  std::cout << "      cpp_sql (twin), driven 6x with the common value '" << kCommon
            << "': " << prepared_plan.scan
            << (prepared_plan.generic ? ", GENERIC (" : ", custom (") << prepared_plan.cond
            << ")\n";
  std::cout << "      The server switched one and not the other. For a rare value the\n"
               "      custom plan keeps winning the cost comparison, so it is kept; for\n"
               "      the common value the generic plan wins at execution six and is then\n"
               "      used for EVERY value, including the rare one. Which of your\n"
               "      parameter values arrives first decides the plan the other ones get.\n";

  // ------------------------------------------------------------------------
  // The session-scope proof: a second connection cannot see the first one's
  // prepared statement, because it is not in the database.
  // ------------------------------------------------------------------------
  PGconn* other = PQconnectdb(dsn().c_str());
  if (PQstatus(other) == CONNECTION_OK) {
    std::cout << "\n  the same catalogue view, read from a SECOND connection:\n";
    print_prepared(other, "pg_prepared_statements");
    std::cout << "    Empty. `cpp_ps` is still there on the first connection and does not\n"
                 "    exist on this one. A named prepared statement is a property of a\n"
                 "    SESSION -- so a transaction-mode pooler, which hands your next\n"
                 "    transaction whichever session is free, cannot honour it. That is the\n"
                 "    whole of the PgBouncer-plus-prepared-statements problem, and you can\n"
                 "    see it here before you ever meet it in production.\n";
    PQfinish(other);
  }

  exec(conn, "DEALLOCATE ALL");
  exec(conn, "DROP INDEX IF EXISTS idx_cpp_protocol_status");
  PQfinish(conn);
  std::cout << "\n(index dropped, statements deallocated -- the lab is as it was)\n";
  return 0;
}
