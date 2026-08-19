// Layer 3 - the same write-skew workload as python/write_skew.py, in Go with
// pgx, to test a claim the layer README makes about ERGONOMICS rather than
// about Postgres: that Go's explicit errors make the SERIALIZABLE retry loop a
// natural unit of code, which is why Go services run SERIALIZABLE more often
// than Python ones do.
//
// WHAT TO LOOK FOR: two things.
//  1. The same table of results as the Python program -- broken shifts at read
//     committed and repeatable read, zero at serializable. Postgres does not
//     care which client is talking to it, and seeing that is the point.
//  2. inTx() below. The retry wrapper takes the whole transaction as a closure,
//     so retrying re-runs the READ as well as the write. In Python the same
//     mistake (retrying only the failed UPDATE) is easy to make and compiles
//     fine; here the transaction handle only exists inside the closure, so the
//     shape that reintroduces the anomaly is awkward to even write.
//
// Run:  cd 01-isolation-levels/golang/write_skew && go run .
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	shifts        = 100
	pairsInFlight = 10
	maxRetries    = 5
	// The window the anomaly lives in: what the handler does between its read
	// and its write. Pretending it is zero is what makes write skew look rare.
	thinkTime = 3 * time.Millisecond
)

type counters struct {
	aborts, retries, gaveUp int
	latencies               []time.Duration
}

func dsn() string {
	if v := os.Getenv("LAB_PG_URL"); v != "" {
		return v
	}
	return "postgres:///sep_lab_03_data?host=/tmp"
}

// inTx runs fn inside a transaction at the given isolation level and retries the
// WHOLE closure on SQLSTATE 40001. This is the unit that has to be retried: a
// retry that re-runs only the write would decide from the stale read that caused
// the abort in the first place.
func inTx(ctx context.Context, pool *pgxpool.Pool, iso pgx.TxIsoLevel, c *counters,
	fn func(context.Context, pgx.Tx) error) error {
	for attempt := 0; ; attempt++ {
		tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: iso})
		if err != nil {
			return err
		}
		err = fn(ctx, tx)
		if err == nil {
			err = tx.Commit(ctx)
		} else {
			_ = tx.Rollback(ctx)
		}
		if err == nil {
			return nil
		}
		var pgErr *pgconn.PgError
		if !errors.As(err, &pgErr) || pgErr.Code != "40001" {
			return err
		}
		c.aborts++
		if attempt == maxRetries {
			c.gaveUp++
			return nil // the caller was told the request failed; that is the cost
		}
		c.retries++
	}
}

func goOffCall(ctx context.Context, pool *pgxpool.Pool, iso pgx.TxIsoLevel, c *counters,
	shift, doctor int) error {
	return inTx(ctx, pool, iso, c, func(ctx context.Context, tx pgx.Tx) error {
		var onCall int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM oncall WHERE shift_id = $1 AND on_call`, shift).Scan(&onCall); err != nil {
			return err
		}
		time.Sleep(thinkTime)
		if onCall > 1 {
			_, err := tx.Exec(ctx,
				`UPDATE oncall SET on_call = false WHERE shift_id = $1 AND doctor_id = $2`, shift, doctor)
			return err
		}
		return nil
	})
}

func resetOncall(ctx context.Context, pool *pgxpool.Pool) error {
	if _, err := pool.Exec(ctx, `TRUNCATE oncall`); err != nil {
		return err
	}
	_, err := pool.Exec(ctx,
		`INSERT INTO oncall (shift_id, doctor_id, on_call)
		 SELECT s, d, true FROM generate_series(1, $1) s, generate_series(1, 2) d`, shifts)
	return err
}

type result struct {
	label                  string
	broken, aborts, gaveUp int
	retriesPerReq          float64
	p50, p99               time.Duration
	rps                    float64
}

func runVariant(ctx context.Context, pool *pgxpool.Pool, label string, iso pgx.TxIsoLevel) (result, error) {
	if err := resetOncall(ctx, pool); err != nil {
		return result{}, err
	}
	workers := pairsInFlight * 2
	rounds := shifts / pairsInFlight

	var wg sync.WaitGroup
	// A barrier: every worker waits here so both doctors of a shift start at the
	// same moment. Nothing after the start is arranged.
	startRound := make([]chan struct{}, rounds)
	for i := range startRound {
		startRound[i] = make(chan struct{})
	}
	all := make([]counters, workers)

	start := time.Now()
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			c := &all[index]
			for r := 0; r < rounds; r++ {
				<-startRound[r]
				shift := r*pairsInFlight + index/2 + 1
				doctor := index%2 + 1
				t0 := time.Now()
				if err := goOffCall(ctx, pool, iso, c, shift, doctor); err != nil {
					fmt.Fprintln(os.Stderr, "request failed:", err)
				}
				c.latencies = append(c.latencies, time.Since(t0))
			}
		}(w)
	}
	for r := 0; r < rounds; r++ {
		time.Sleep(2 * time.Millisecond)
		close(startRound[r])
	}
	wg.Wait()
	elapsed := time.Since(start)

	res := result{label: label}
	var lat []time.Duration
	for i := range all {
		res.aborts += all[i].aborts
		res.gaveUp += all[i].gaveUp
		lat = append(lat, all[i].latencies...)
	}
	var retries int
	for i := range all {
		retries += all[i].retries
	}
	res.retriesPerReq = float64(retries) / float64(len(lat))
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	res.p50 = lat[len(lat)*50/100]
	res.p99 = lat[len(lat)*99/100]
	res.rps = float64(len(lat)) / elapsed.Seconds()

	err := pool.QueryRow(ctx, `SELECT count(*) FROM (
		SELECT shift_id FROM oncall GROUP BY shift_id HAVING sum(on_call::int) = 0) t`).Scan(&res.broken)
	return res, err
}

func main() {
	ctx := context.Background()
	cfg, err := pgxpool.ParseConfig(dsn())
	if err != nil {
		fmt.Fprintln(os.Stderr, "bad DSN:", err)
		os.Exit(1)
	}
	cfg.MaxConns = pairsInFlight * 2
	cfg.MinConns = pairsInFlight * 2
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "connect:", err, "\nSet LAB_PG_URL, or run python3 lab/local/setup_lab.py first.")
		os.Exit(1)
	}
	defer pool.Close()

	var version string
	if err := pool.QueryRow(ctx, "SELECT version()").Scan(&version); err != nil {
		fmt.Fprintln(os.Stderr, "connect:", err, "\nRun python3 lab/local/setup_lab.py first.")
		os.Exit(1)
	}
	fmt.Printf("\n%s\nWrite skew in Go (pgx) -- %s\n%s\n\n", pad(78), serverName(version), pad(78))
	fmt.Printf("%d shifts x 2 doctors, %d connections, %v of think time between read and write.\n\n",
		shifts, pairsInFlight*2, thinkTime)

	variants := []struct {
		label string
		iso   pgx.TxIsoLevel
	}{
		{"read committed", pgx.ReadCommitted},
		{"repeatable read", pgx.RepeatableRead},
		{"serializable", pgx.Serializable},
	}
	fmt.Printf("%-20s%8s%8s%9s%13s%10s%10s%9s\n",
		"variant", "broken", "40001", "gave up", "retries/req", "p50 ms", "p99 ms", "req/s")
	fmt.Println(pad(87))
	for _, v := range variants {
		r, err := runVariant(ctx, pool, v.label, v.iso)
		if err != nil {
			fmt.Fprintln(os.Stderr, "variant failed:", err)
			os.Exit(1)
		}
		fmt.Printf("%-20s%8d%8d%9d%13.2f%10.1f%10.1f%9.1f\n", r.label, r.broken, r.aborts, r.gaveUp,
			r.retriesPerReq, float64(r.p50.Microseconds())/1000, float64(r.p99.Microseconds())/1000, r.rps)
	}
	fmt.Println()
	fmt.Println("Compare against python/write_skew.py: same anomaly, same fix, same cost. The")
	fmt.Println("difference is in inTx() -- the retry takes the whole transaction as a closure,")
	fmt.Println("so the version of this code that retries only the UPDATE is the awkward one to")
	fmt.Println("write. In Python it is the easy one, which is how that bug gets shipped.")
}

func pad(n int) string {
	s := make([]byte, n)
	for i := range s {
		s[i] = '='
	}
	return string(s)
}

// serverName trims "PostgreSQL 17.5 (Homebrew) on aarch64-..." to the part that
// identifies the server.
func serverName(version string) string {
	if i := strings.Index(version, " on "); i > 0 {
		return version[:i]
	}
	return version
}
