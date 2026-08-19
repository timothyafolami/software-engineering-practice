// Layer 3, Topic 7 - Go's database/sql defaults, and the opposite failure.
//
// WHAT IT DEMONSTRATES: SQLAlchemy's pool is bounded and its failure is an
// exception in your process. Go's `database/sql` is the mirror image:
//
//	SetMaxOpenConns   UNLIMITED by default. There is no client-side ceiling, so
//	                  a Go service under load opens connections until POSTGRES
//	                  refuses -- "FATAL: sorry, too many clients already",
//	                  SQLSTATE 53300. There is no client-side queue to observe
//	                  because there is no client-side limit; the ceiling is the
//	                  server's, and it is shared with every other service.
//	SetMaxIdleConns   2 by default. A service that DOES bound its open
//	                  connections can still churn: it opens a connection, uses
//	                  it, returns it to a pool with room for two, and closes it.
//	                  Under steady load that is a new backend process, a new TCP
//	                  handshake and a new authentication per query, showing up as
//	                  unexplained latency with a pool that looks healthy.
//
// Setting MaxOpenConns in Go is not tuning. It is the difference between your
// queue and the server's.
//
// WHAT TO LOOK FOR:
//  1. `peak open` in the default run, against the server's max_connections.
//  2. `53300` errors -- the server rejecting you, rather than your pool
//     queueing you. Note which side of the client/server line the failure is on.
//  3. `idle conns closed` in the churn run -- Go's own MaxIdleClosed counter,
//     which is the number of returned connections that were closed because the
//     idle pool was full. Alongside it, `server sessions`, read from
//     pg_stat_database, is the same churn measured from the other end.
//
// Run:  cd 07-connection-pools/golang/pool_defaults && go run .
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
//
// This program deliberately tries to exhaust max_connections on the server it
// connects to. Point it at the lab database and nothing else.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
)

const (
	concurrency = 150             // more in-flight requests than max_connections
	duration    = 6 * time.Second // per variant
	workSQL     = `SELECT count(*), sum(total_cents) FROM orders
	               WHERE total_cents > $1 AND status <> 'refunded'`
)

func dsn() string {
	if v := os.Getenv("LAB_PG_URL"); v != "" {
		return v
	}
	return "postgres:///sep_lab_03_data?host=/tmp"
}

type outcome struct {
	completed  int
	tooMany    int
	otherErr   int
	sampleErr  string
	peakOpen   int
	waitCount  int64
	waitTime   time.Duration
	idleClosed int64
	sessions   int64
	p99        time.Duration
}

// sessionsOpened reads the SERVER's count of backends started against this
// database. pg_stat_database.sessions exists from PG14 and is the honest way to
// measure churn -- the client's own view cannot see connections it closed and
// reopened as anything but normal operation.
func sessionsOpened(ctx context.Context, db *sql.DB) int64 {
	var n int64
	err := db.QueryRowContext(ctx,
		`SELECT sessions FROM pg_stat_database WHERE datname = current_database()`).Scan(&n)
	if err != nil {
		return -1
	}
	return n
}

func percentile(d []time.Duration, q float64) time.Duration {
	if len(d) == 0 {
		return 0
	}
	sorted := append([]time.Duration(nil), d...)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	idx := int(q/100*float64(len(sorted))+0.5) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

func runVariant(ctx context.Context, label string, tune func(*sql.DB)) (outcome, error) {
	db, err := sql.Open("pgx", dsn())
	if err != nil {
		return outcome{}, err
	}
	defer db.Close()
	tune(db)

	before := sessionsOpened(ctx, db)

	var (
		mu        sync.Mutex
		out       outcome
		latencies []time.Duration
		wg        sync.WaitGroup
	)
	stop := time.Now().Add(duration)

	// Peak-open sampler. db.Stats().OpenConnections is the number of
	// connections this process currently holds -- the number that, multiplied
	// by workers and replicas, decides whether you fit under max_connections.
	sampleDone := make(chan struct{})
	go func() {
		defer close(sampleDone)
		for time.Now().Before(stop) {
			if n := db.Stats().OpenConnections; n > out.peakOpen {
				mu.Lock()
				if n > out.peakOpen {
					out.peakOpen = n
				}
				mu.Unlock()
			}
			time.Sleep(20 * time.Millisecond)
		}
	}()

	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for time.Now().Before(stop) {
				t0 := time.Now()
				var n, total sql.NullInt64
				err := db.QueryRowContext(ctx, workSQL, 250000).Scan(&n, &total)
				elapsed := time.Since(t0)
				mu.Lock()
				latencies = append(latencies, elapsed)
				switch {
				case err == nil:
					out.completed++
				case isTooManyClients(err):
					out.tooMany++
				default:
					out.otherErr++
					if out.sampleErr == "" {
						out.sampleErr = err.Error()
					}
				}
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	<-sampleDone

	// The stats machinery flushes on a timer, so give it a moment before reading
	// pg_stat_database. Reading it immediately reports a number that is true and
	// stale, which is worse than waiting 300ms for one that is true and current.
	time.Sleep(300 * time.Millisecond)
	stats := db.Stats()
	out.waitCount = stats.WaitCount
	out.waitTime = stats.WaitDuration
	out.idleClosed = stats.MaxIdleClosed
	out.p99 = percentile(latencies, 99)
	if after := sessionsOpened(ctx, db); after >= 0 && before >= 0 {
		out.sessions = after - before
	}
	return out, nil
}

// isTooManyClients recognises SQLSTATE 53300, which is the SERVER telling you
// it is out of connection slots. In a bounded pool you never see this: you see
// your own timeout instead. Which of the two you get is decided entirely by
// whether SetMaxOpenConns was called.
func isTooManyClients(err error) bool {
	var pgErr *pgconn.PgError
	if ok := errorsAs(err, &pgErr); ok {
		return pgErr.Code == "53300"
	}
	return strings.Contains(err.Error(), "too many clients")
}

func errorsAs(err error, target **pgconn.PgError) bool {
	for err != nil {
		if pe, ok := err.(*pgconn.PgError); ok {
			*target = pe
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func truncate(s string, n int) string {
	s = strings.ReplaceAll(s, "\n", " ")
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func main() {
	ctx := context.Background()
	probe, err := sql.Open("pgx", dsn())
	if err != nil {
		fmt.Fprintln(os.Stderr, "open:", err)
		os.Exit(1)
	}
	var version, maxConn string
	if err := probe.QueryRowContext(ctx, "SELECT version()").Scan(&version); err != nil {
		fmt.Fprintf(os.Stderr, "could not reach %s: %v\n", dsn(), err)
		fmt.Fprintln(os.Stderr, "unblock: python3 lab/local/check_env.py")
		os.Exit(1)
	}
	_ = probe.QueryRowContext(ctx, "SHOW max_connections").Scan(&maxConn)
	probe.Close()

	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Go database/sql: unlimited by default, and what that means")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(strings.SplitN(version, " on ", 2)[0])
	fmt.Printf("server max_connections = %s; this program offers %d concurrent requests\n",
		maxConn, concurrency)
	fmt.Println("for", duration, "per variant. Predict the peak connection count for each.")

	variants := []struct {
		label string
		tune  func(*sql.DB)
	}{
		{"defaults (unlimited)", func(db *sql.DB) {}},
		{"MaxOpen=10, MaxIdle=2", func(db *sql.DB) {
			db.SetMaxOpenConns(10)
			// MaxIdle deliberately left at its default of 2 while MaxOpen is 10:
			// eight of every ten returned connections have nowhere to go and get
			// closed. This is the churn configuration, and it is what you get by
			// setting only MaxOpenConns -- which is the advice everybody follows.
		}},
		{"MaxOpen=10, MaxIdle=10", func(db *sql.DB) {
			db.SetMaxOpenConns(10)
			db.SetMaxIdleConns(10)
			db.SetConnMaxLifetime(30 * time.Minute)
			db.SetConnMaxIdleTime(5 * time.Minute)
		}},
	}

	fmt.Printf("\n  %-24s%9s%9s%8s%9s%12s%11s%10s\n",
		"variant", "done", "peak", "53300", "other", "waits", "wait total", "p99")
	fmt.Println("  " + strings.Repeat("-", 92))
	results := make([]outcome, 0, len(variants))
	for _, v := range variants {
		out, err := runVariant(ctx, v.label, v.tune)
		if err != nil {
			fmt.Fprintln(os.Stderr, v.label, err)
			os.Exit(1)
		}
		results = append(results, out)
		fmt.Printf("  %-24s%9d%9d%8d%9d%12d%11s%10s\n",
			v.label, out.completed, out.peakOpen, out.tooMany, out.otherErr,
			out.waitCount, out.waitTime.Truncate(time.Millisecond), out.p99.Truncate(time.Millisecond))
		time.Sleep(time.Second)
	}

	fmt.Printf("\n  %-24s%18s%22s\n", "variant", "server sessions", "idle conns closed")
	for i, v := range variants {
		fmt.Printf("  %-24s%18d%22d\n", v.label, results[i].sessions, results[i].idleClosed)
	}

	def, churn, tuned := results[0], results[1], results[2]
	fmt.Println()
	if def.tooMany > 0 {
		fmt.Printf("  The default pool opened %d connections and the SERVER refused %d requests\n",
			def.peakOpen, def.tooMany)
		fmt.Printf("  with SQLSTATE 53300. Not your pool timing out -- Postgres running out of\n")
		fmt.Printf("  slots, which it shares with every other service pointed at it.\n")
	} else {
		fmt.Printf("  The default pool peaked at %d connections against max_connections=%s.\n",
			def.peakOpen, maxConn)
		fmt.Printf("  It did not hit the ceiling this run -- raise `concurrency` in this file,\n")
		fmt.Printf("  or note how little headroom is left and multiply by your replica count.\n")
	}
	if def.sampleErr != "" {
		fmt.Printf("\n  one of the %d 'other' errors, verbatim -- worth reading, because this is\n",
			def.otherErr)
		fmt.Printf("  what an exhausted server looks like from the client side:\n    %s\n",
			truncate(def.sampleErr, 100))
	}

	fmt.Printf("\n  idle-connection churn: MaxIdle=2 closed %d returned connections for having\n",
		churn.idleClosed)
	fmt.Printf("  nowhere to sit; MaxIdle=10 closed %d. Server sessions opened: %d and %d.\n",
		tuned.idleClosed, churn.sessions, tuned.sessions)
	fmt.Println("  `idle conns closed` is the direct evidence and it is client-side, so it is")
	fmt.Println("  exact. The server-side session count is the same measurement from the other")
	fmt.Println("  end and it can lag, because pg_stat_database is flushed on a timer -- when")
	fmt.Println("  the two disagree, trust the client counter for THIS run and the server")
	fmt.Println("  counter for a long one.")
	fmt.Println()
	fmt.Println("  Under sustained saturation the two configurations look similar, because a")
	fmt.Println("  connection returned to a full pool is immediately handed to a waiting")
	fmt.Println("  caller and never goes idle at all. The churn shows up under BURSTY load,")
	fmt.Println("  which is what production traffic is: raise `concurrency`, add a sleep")
	fmt.Println("  between requests, and watch idle-closed climb. Set MaxIdleConns equal to")
	fmt.Println("  MaxOpenConns unless you have a specific reason not to -- the default of 2")
	fmt.Println("  was chosen in 2013, for a different shape of service.")
	fmt.Println()
	fmt.Println("  The `waits` and `wait total` columns are Go's own queue, and they only")
	fmt.Println("  exist once you set MaxOpenConns. That is the trade this whole topic is")
	fmt.Println("  about: a visible queue you own, or an invisible one inside the server.")
}
