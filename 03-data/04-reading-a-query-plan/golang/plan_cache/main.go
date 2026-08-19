// Layer 3, Topic 4 - the generic-plan trap, and why it is a Go section.
//
// WHAT IT DEMONSTRATES: pgx uses the extended protocol with NAMED server-side
// prepared statements by default. That is a performance win and a correctness
// hazard on skewed data, because of a rule that lives in the server rather than
// in the driver:
//
//	Postgres runs a CUSTOM plan (built with your actual parameter values) for a
//	prepared statement's first five executions. From the sixth it compares the
//	cost of a GENERIC plan -- built without knowing your values at all -- against
//	the average of those five custom plans, and if the generic plan is not worse
//	it switches to it PERMANENTLY for that statement.
//
// On orders.status, ~92% of rows are 'complete' and ~1% are 'failed'. A plan
// that is right for one is badly wrong for the other. Whichever value the first
// five executions happened to use decides what every later execution gets --
// including executions with the other value.
//
// WHAT TO LOOK FOR:
//  1. The execution-by-execution table: the plan text changes from real literals
//     to `$1` at execution six. That is the switch happening, visible.
//  2. The generic-vs-custom timing comparison for the rare value. Same query,
//     same data, same connection, two plans, and the difference is not small.
//  3. pg_prepared_statements listing an object pgx created without being asked.
//     That object belongs to this CONNECTION -- which is why a transaction-mode
//     pooler and a driver that prepares need to be introduced to each other
//     carefully. That is Topic 7's problem, arriving here first.
//
// Run:  cd 04-reading-a-query-plan/golang/plan_cache && go run .
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
//
// The lab seed must exist. Run any Python program in this layer once first if
// this is a fresh machine: python3 lab/local/setup_lab.py
package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	rareValue   = "failed"   // ~1% of orders
	commonValue = "complete" // ~92% of orders
	// The server's threshold is five custom plans before it considers a generic
	// one, so eight executions is enough to see the switch and then a few after.
	executions = 8
	repeats    = 200 // timed repetitions per plan mode
)

func dsn() string {
	if v := os.Getenv("LAB_PG_URL"); v != "" {
		return v
	}
	return "postgres:///sep_lab_03_data?host=/tmp"
}

func die(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "\n%s: %v\n", what, err)
		if strings.Contains(err.Error(), "does not exist") ||
			strings.Contains(err.Error(), "connect") {
			fmt.Fprintf(os.Stderr, "unblock: python3 lab/local/check_env.py, "+
				"then python3 lab/local/setup_lab.py\n")
		}
		os.Exit(1)
	}
}

// explainText runs EXPLAIN and returns the plan as one line per node, joined.
func explainText(ctx context.Context, conn *pgx.Conn, sql string, args ...any) (string, error) {
	rows, err := conn.Query(ctx, "EXPLAIN "+sql, args...)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			return "", err
		}
		out = append(out, strings.TrimSpace(line))
	}
	return strings.Join(out, " | "), rows.Err()
}

// planKind reduces a plan to the two facts worth tabulating.
func planKind(plan string) (scan string, generic bool) {
	switch {
	case strings.Contains(plan, "Index Scan"):
		scan = "index scan"
	case strings.Contains(plan, "Bitmap"):
		scan = "bitmap"
	default:
		scan = "seq scan"
	}
	// A generic plan cannot show the parameter's value, because it was built
	// without one. `$1` in the condition is the tell.
	return scan, strings.Contains(plan, "$1")
}

// showPlan prints one row of the execution-by-execution table.
//
// The value is interpolated rather than bound: `$1` inside EXECUTE belongs to
// EXECUTE's own argument list, so it is not a bindable parameter of the outer
// EXPLAIN. Both values are program constants; anywhere they were not, this
// would be a SQL injection and would have to be built differently.
func showPlan(ctx context.Context, conn *pgx.Conn, n int, value string) {
	plan, err := explainText(ctx, conn, fmt.Sprintf("EXECUTE ps_status('%s')", value))
	die(err, "explain execute")
	scan, generic := planKind(plan)
	kind := "custom"
	if generic {
		kind = "GENERIC"
	}
	cond := "-"
	for _, part := range strings.Split(plan, " | ") {
		if strings.Contains(part, "Cond:") || strings.Contains(part, "Filter:") {
			cond = part
			break
		}
	}
	fmt.Printf("    %-6d %-11s %-12s %-9s %s\n", n, value, scan, kind, cond)
}

func main() {
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn())
	die(err, "connect")
	defer conn.Close(ctx)

	var version string
	die(conn.QueryRow(ctx, "SELECT version()").Scan(&version), "version")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("The generic-plan trap -- pgx, named prepared statements, skewed data")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(strings.SplitN(version, " on ", 2)[0])

	// Session tuning, same as lab_db.tune_session. Without it every plan below
	// is a plan about a spinning disk this machine does not have.
	for _, s := range []string{"SET random_page_cost = 1.1", "SET effective_cache_size = '1GB'"} {
		_, err = conn.Exec(ctx, s)
		die(err, s)
	}

	_, err = conn.Exec(ctx, "CREATE INDEX IF NOT EXISTS idx_plan_cache_status ON orders (status)")
	die(err, "create index")
	_, err = conn.Exec(ctx, "ANALYZE orders")
	die(err, "analyze")
	defer func() {
		_, _ = conn.Exec(context.Background(), "DROP INDEX IF EXISTS idx_plan_cache_status")
		fmt.Println("\n(index dropped -- this program leaves the lab as it found it)")
	}()

	rows, err := conn.Query(ctx,
		`SELECT status, count(*), round(100.0 * count(*) / sum(count(*)) OVER (), 2)
		 FROM orders GROUP BY status ORDER BY 2 DESC`)
	die(err, "distribution")
	fmt.Println("\n  the skew this whole experiment turns on:")
	for rows.Next() {
		var s string
		var n int64
		var pct float64
		die(rows.Scan(&s, &n, &pct), "scan")
		fmt.Printf("    %-10s %9d rows  %6.2f%%\n", s, n, pct)
	}
	rows.Close()

	// -----------------------------------------------------------------------
	// 1. Watch the switch happen, execution by execution.
	//
	// SQL-level PREPARE/EXECUTE is used here rather than pgx's own prepare so
	// that EXPLAIN can see the same cached plan. The mechanism is the server's
	// either way -- pgx's named statements land in exactly this cache.
	// -----------------------------------------------------------------------
	_, err = conn.Exec(ctx, "DEALLOCATE ALL")
	die(err, "deallocate")
	_, err = conn.Exec(ctx,
		`PREPARE ps_status(text) AS
		 SELECT count(*), sum(total_cents) FROM orders WHERE status = $1`)
	die(err, "prepare")

	fmt.Printf("\n  executing the SAME prepared statement %d times with the COMMON value %q,\n",
		executions, commonValue)
	fmt.Printf("  the way a warm production process does:\n")
	fmt.Printf("    %-6s %-11s %-12s %-9s %s\n", "exec", "value", "scan", "plan", "condition as the plan states it")
	for i := 1; i <= executions; i++ {
		showPlan(ctx, conn, i, commonValue)
	}
	fmt.Println("    Execution six is where it switches. The scan changes, and the condition")
	fmt.Println("    stops naming your value -- `$1` means the server built this plan without")
	fmt.Println("    knowing it, from the average selectivity of the column instead.")

	fmt.Printf("\n  and now the RARE value %q, on that same already-switched statement:\n", rareValue)
	showPlan(ctx, conn, executions+1, rareValue)
	fmt.Println("    Nobody asked for this plan for this value. It was decided five executions")
	fmt.Println("    ago by whichever value happened to arrive first.")

	// The contrast: a statement that has never seen an execution gets a custom
	// plan built for the value in front of it. This is the plan you see in psql,
	// and it is why "it is fast when I test it" is such a common report.
	_, err = conn.Exec(ctx, "DEALLOCATE ps_status")
	die(err, "deallocate ps_status")
	_, err = conn.Exec(ctx,
		`PREPARE ps_status(text) AS
		 SELECT count(*), sum(total_cents) FROM orders WHERE status = $1`)
	die(err, "re-prepare")
	fmt.Printf("\n  the same rare value on a FRESH statement -- what psql shows you:\n")
	showPlan(ctx, conn, 1, rareValue)

	// -----------------------------------------------------------------------
	// 2. What that costs, for each value, forced both ways.
	//
	// plan_cache_mode makes the choice explicit instead of emergent, which turns
	// "it is fast when I test it and slow in production" from a mystery into a
	// two-line check you can run on any query in ninety seconds.
	// -----------------------------------------------------------------------
	fmt.Printf("\n  %d executions of the same statement, each plan mode x each value:\n", repeats)
	fmt.Printf("    %-22s %-10s %10s %10s  %s\n", "plan_cache_mode", "value", "total ms", "per exec", "scan")
	// This half uses pgx's OWN parameter binding rather than SQL-level EXECUTE,
	// because that is the path a Go service actually takes: pgx prepares the
	// statement server-side on first use and reuses it. plan_cache_mode is
	// consulted on every execution, so flipping it mid-session decides which
	// plan that already-prepared statement runs.
	const timedSQL = "SELECT count(*), sum(total_cents) FROM orders WHERE status = $1"
	for _, mode := range []string{"force_custom_plan", "force_generic_plan"} {
		for _, value := range []string{rareValue, commonValue} {
			_, err = conn.Exec(ctx, "SET plan_cache_mode = "+mode)
			die(err, "set plan_cache_mode")

			var n int64
			var sum *int64
			die(conn.QueryRow(ctx, timedSQL, value).Scan(&n, &sum), "warm")

			start := time.Now()
			for i := 0; i < repeats; i++ {
				die(conn.QueryRow(ctx, timedSQL, value).Scan(&n, &sum), "execute")
			}
			elapsed := time.Since(start)

			// EXPLAIN the PREPARED statement, not a literal query -- a literal
			// query is always custom-planned and would report the wrong thing
			// for the force_generic_plan rows.
			plan, err := explainText(ctx, conn, fmt.Sprintf("EXECUTE ps_status('%s')", value))
			die(err, "explain timed")
			scan, generic := planKind(plan)
			if generic {
				scan += " (generic)"
			}
			fmt.Printf("    %-22s %-10s %10.1f %10.3f  %s\n", mode, value,
				float64(elapsed.Microseconds())/1000.0,
				float64(elapsed.Microseconds())/1000.0/float64(repeats), scan)
		}
	}
	_, err = conn.Exec(ctx, "SET plan_cache_mode = auto")
	die(err, "reset plan_cache_mode")

	// -----------------------------------------------------------------------
	// 3. pgx's own behaviour: it prepares without being asked.
	// -----------------------------------------------------------------------
	_, err = conn.Exec(ctx, "DEALLOCATE ALL")
	die(err, "deallocate")
	for i := 0; i < 3; i++ {
		var n int64
		die(conn.QueryRow(ctx,
			"SELECT count(*) FROM orders WHERE status = $1 AND total_cents > $2",
			rareValue, 1000).Scan(&n), "pgx query")
	}
	fmt.Println("\n  what pgx left on the server after three ordinary Query calls:")
	rows, err = conn.Query(ctx,
		"SELECT name, generic_plans, custom_plans, left(statement, 52) FROM pg_prepared_statements ORDER BY name")
	die(err, "pg_prepared_statements")
	found := false
	for rows.Next() {
		var name, stmt string
		var generic, custom int64
		die(rows.Scan(&name, &generic, &custom, &stmt), "scan prepared")
		fmt.Printf("    %-22s generic=%d custom=%d  %s\n", name, generic, custom, stmt)
		found = true
	}
	rows.Close()
	if !found {
		fmt.Println("    (none -- this build of pgx is configured with QueryExecMode not")
		fmt.Println("     defaulting to cache_statement; check pgx.ConnConfig.DefaultQueryExecMode)")
	}
	fmt.Println("    Nobody wrote PREPARE. pgx did, because that is its default, and the")
	fmt.Println("    object it created belongs to THIS connection and dies with it.")

	fmt.Println("\n  The two sentences to carry away:")
	fmt.Println("    * The plan you see in psql (custom, real values) is not necessarily the")
	fmt.Println("      plan production runs (generic, no values), and the gap is worst exactly")
	fmt.Println("      where your data is most skewed.")
	fmt.Println("    * `SET plan_cache_mode = force_generic_plan` reproduces it on demand.")
}
