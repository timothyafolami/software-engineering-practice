// Layer 4 Topic 2 -- the same three handlers as python/idempotency_race.py, in Go.
//
// WHAT THIS DEMONSTRATES: Go's version of "did I win the insert?". With pgx there
// is no exception hierarchy to over-catch, so the unique-violation branch has to
// be written on purpose:
//
//	var pgErr *pgconn.PgError
//	if errors.As(err, &pgErr) && pgErr.Code == "23505" { ... }
//
// That explicitness is the reason Go is in this topic. Everything else here is
// deliberately a transcription of the Python file, because the mechanism lives in
// Postgres and not in the language -- which is itself the point.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the DUPLICATE CHARGES line, and that it matches
// what Python's run of the same implementation produced. If the two languages
// disagree, one of them is not doing what it says.
//
//	cd golang && go run idempotency_race.go -impl A -keys 200 -concurrency 5
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"sort"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const tenant = "acme"

// gen_random_uuid(), not uuidv7(): uuidv7 is a Postgres 18 function and the
// local fallback server is 17.5. See lab/README.md -- it costs B-tree insert
// locality, so no insert-throughput number from a fallback run is comparable
// with a container run.
const ddl = `
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     text        NOT NULL,
    key           text        NOT NULL,
    fingerprint   text        NOT NULL,
    state         text        NOT NULL
                  CHECK (state IN ('in_flight', 'succeeded', 'failed_permanently')),
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    UNIQUE (tenant_id, key)
);
CREATE TABLE IF NOT EXISTS charges (
    id              bigserial PRIMARY KEY,
    run_id          text        NOT NULL,
    impl            text        NOT NULL,
    tenant_id       text        NOT NULL,
    idempotency_key text        NOT NULL,
    amount_cents    integer     NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
    -- NO UNIQUE (tenant_id, idempotency_key). On purpose; see the Python header.
);
CREATE INDEX IF NOT EXISTS charges_run_idx ON charges (run_id);`

func dsn() string {
	if v := os.Getenv("LAB_DSN"); v != "" {
		return v
	}
	return "postgresql:///sep_lab_04_dist"
}

func fingerprint(body map[string]any) string {
	b, _ := json.Marshal(body) // encoding/json sorts map keys, so this is canonical
	sum := sha256.Sum256([]byte("POST /payments " + string(b)))
	return hex.EncodeToString(sum[:])
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

type outcome struct {
	status int
	note   string
	ms     float64
}

type handler func(ctx context.Context, c *pgx.Conn, runID, key string,
	body map[string]any, hold time.Duration) (int, string)

// A -- check-then-insert. Three separate transactions, and the effect happens
// BEFORE the key row is recorded, because charge_the_card() is an HTTP call to a
// processor with no transaction to join. Putting the two in one transaction is
// implementation B's structural rule, so doing it here would be writing B.
func handleA(ctx context.Context, c *pgx.Conn, runID, key string,
	body map[string]any, hold time.Duration) (int, string) {
	var state string
	err := c.QueryRow(ctx,
		`SELECT state FROM idempotency_keys WHERE tenant_id = $1 AND key = $2`,
		tenant, key).Scan(&state)
	if err == nil {
		return 200, "replay"
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return 500, err.Error()
	}
	// Both concurrent requests land here: under READ COMMITTED neither sees the
	// other's uncommitted insert.
	time.Sleep(hold)
	if _, err := c.Exec(ctx,
		`INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
		 VALUES ($1, 'A', $2, $3, $4)`,
		runID, tenant, key, body["amount_cents"]); err != nil {
		return 500, err.Error()
	}
	// The money has moved. Only now does the unique index object.
	if _, err := c.Exec(ctx,
		`INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
		 VALUES ($1, $2, $3, 'succeeded')`,
		tenant, key, fingerprint(body)); err != nil {
		if isUniqueViolation(err) {
			return 500, "23505 AFTER charging -- the card was already charged"
		}
		return 500, err.Error()
	}
	return 201, "charged"
}

// B -- atomic insert. Key row and effect commit in the SAME transaction.
func handleB(ctx context.Context, c *pgx.Conn, runID, key string,
	body map[string]any, hold time.Duration) (int, string) {
	fp := fingerprint(body)
	tx, err := c.Begin(ctx)
	if err != nil {
		return 500, err.Error()
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after a successful Commit

	tag, err := tx.Exec(ctx,
		`INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
		 VALUES ($1, $2, $3, 'in_flight')
		 ON CONFLICT (tenant_id, key) DO NOTHING`, tenant, key, fp)
	if err != nil {
		return 500, err.Error()
	}
	// ON CONFLICT DO NOTHING RETURNING id yields ZERO rows on conflict, so
	// RETURNING never hands you the row that already exists. "Did I win?" is
	// RowsAffected() == 1, and the loser has to SELECT separately.
	//
	// If we lost, that Exec already BLOCKED on the unique index until the winner
	// committed or rolled back. It did not fail fast. The duplicate's latency is
	// bounded below by the winner's entire transaction.
	if tag.RowsAffected() == 1 {
		time.Sleep(hold)
		var chargeID int64
		if err := tx.QueryRow(ctx,
			`INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
			 VALUES ($1, 'B', $2, $3, $4) RETURNING id`,
			runID, tenant, key, body["amount_cents"]).Scan(&chargeID); err != nil {
			return 500, err.Error()
		}
		resp, _ := json.Marshal(map[string]any{"charge_id": chargeID, "status": "succeeded"})
		if _, err := tx.Exec(ctx,
			`UPDATE idempotency_keys SET state = 'succeeded', response = $1
			 WHERE tenant_id = $2 AND key = $3`, string(resp), tenant, key); err != nil {
			return 500, err.Error()
		}
		if err := tx.Commit(ctx); err != nil {
			return 500, err.Error()
		}
		return 201, "charged"
	}

	var state, storedFP string
	var resp *string
	if err := tx.QueryRow(ctx,
		`SELECT state, fingerprint, response::text FROM idempotency_keys
		 WHERE tenant_id = $1 AND key = $2`, tenant, key).Scan(&state, &storedFP, &resp); err != nil {
		return 500, err.Error()
	}
	if err := tx.Commit(ctx); err != nil {
		return 500, err.Error()
	}
	switch {
	case storedFP != fp:
		// Same key, different body. Replaying the stored response would tell the
		// caller that a request which never existed had succeeded.
		return 422, "fingerprint mismatch"
	case state == "succeeded":
		return 200, "replay"
	case state == "failed_permanently":
		return 409, "previous attempt failed permanently"
	default:
		// Still in_flight and we are here: the winner rolled back, so nobody owns
		// this key. Retryable, and it has to say so.
		return 409, "in flight, retry"
	}
}

// C -- advisory lock. Correct too, different cost profile.
func handleC(ctx context.Context, c *pgx.Conn, runID, key string,
	body map[string]any, hold time.Duration) (int, string) {
	tx, err := c.Begin(ctx)
	if err != nil {
		return 500, err.Error()
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op after a successful Commit

	// xact, not session: a session-level advisory lock behind pgbouncer in
	// transaction pooling mode outlives your ownership of the server connection.
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`,
		tenant+":"+key); err != nil {
		return 500, err.Error()
	}
	var state string
	err = tx.QueryRow(ctx,
		`SELECT state FROM idempotency_keys WHERE tenant_id = $1 AND key = $2`,
		tenant, key).Scan(&state)
	if err == nil {
		return 200, "replay"
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return 500, err.Error()
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO idempotency_keys (tenant_id, key, fingerprint, state)
		 VALUES ($1, $2, $3, 'succeeded')`, tenant, key, fingerprint(body)); err != nil {
		return 500, err.Error()
	}
	time.Sleep(hold)
	if _, err := tx.Exec(ctx,
		`INSERT INTO charges (run_id, impl, tenant_id, idempotency_key, amount_cents)
		 VALUES ($1, 'C', $2, $3, $4)`,
		runID, tenant, key, body["amount_cents"]); err != nil {
		return 500, err.Error()
	}
	if err := tx.Commit(ctx); err != nil {
		return 500, err.Error()
	}
	return 201, "charged"
}

func percentile(v []float64, q float64) float64 {
	if len(v) == 0 {
		return 0
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	i := int(q*float64(len(s))+0.5) - 1
	if i < 0 {
		i = 0
	}
	if i >= len(s) {
		i = len(s) - 1
	}
	return s[i]
}

func main() {
	impl := flag.String("impl", "", "A, B or C")
	nkeys := flag.Int("keys", 200, "distinct idempotency keys")
	conc := flag.Int("concurrency", 5, "simultaneous requests per key, separate conns")
	holdMS := flag.Int("hold-ms", 10, "deliberate delay inside the winner's transaction")
	varySlot := flag.Int("vary-slot", -1, "make this slot send a DIFFERENT body under "+
		"the same key (-1 = off); exercises B's fingerprint check")
	reset := flag.Bool("reset", false, "truncate both tables first")
	flag.Parse()

	handlers := map[string]handler{"A": handleA, "B": handleB, "C": handleC}
	h, ok := handlers[*impl]
	if !ok {
		fmt.Fprintln(os.Stderr, "usage: go run idempotency_race.go -impl A|B|C "+
			"[-keys N] [-concurrency N] [-hold-ms N] [-reset]")
		os.Exit(2)
	}
	hold := time.Duration(*holdMS) * time.Millisecond

	ctx := context.Background()
	admin, err := pgx.Connect(ctx, dsn())
	if err != nil {
		fmt.Fprintf(os.Stderr,
			"cannot reach %s: %v\n\nThe local fallback needs a Postgres that is already\n"+
				"listening. Check with: python3 ../../lab/local/check_env.py\n", dsn(), err)
		os.Exit(1)
	}
	if _, err := admin.Exec(ctx, ddl); err != nil {
		fmt.Fprintln(os.Stderr, "ddl:", err)
		os.Exit(1)
	}
	if *reset {
		if _, err := admin.Exec(ctx, `TRUNCATE charges, idempotency_keys`); err != nil {
			fmt.Fprintln(os.Stderr, "truncate:", err)
			os.Exit(1)
		}
		fmt.Println("[setup] truncated charges and idempotency_keys")
	}
	var serverVersion string
	_ = admin.QueryRow(ctx, `SELECT split_part(version(), ' on ', 1)`).Scan(&serverVersion)

	runID := fmt.Sprintf("%s-go-%06x", *impl, rand.Int31n(1<<24))
	keys := make([]string, *nkeys)
	for i := range keys {
		keys[i] = fmt.Sprintf("%s-key-%05d", runID, i)
	}

	fmt.Println()
	fmt.Println("==============================================================================")
	fmt.Printf("Topic 2 (Go) -- IMPL %s   %d keys x %d simultaneous requests\n",
		*impl, *nkeys, *conc)
	fmt.Println("==============================================================================")
	fmt.Printf("  server        : %s\n", serverVersion)
	fmt.Printf("  run id        : %s\n", runID)
	fmt.Printf("  hold in txn   : %d ms (identical across A, B and C)\n", *holdMS)
	if *varySlot >= 0 {
		fmt.Printf("  vary slot     : %d sends a different body under the same key\n", *varySlot)
	}
	fmt.Println("  charges index : NO unique constraint on idempotency_key -- deliberate")

	// One goroutine per slot, each with its own connection; a barrier per key so
	// every slot leaves at the same instant. Firing duplicates sequentially from
	// one client tests nothing, because the first has already committed.
	results := make([][]outcome, *nkeys)
	for i := range results {
		results[i] = make([]outcome, *conc)
	}
	var wg sync.WaitGroup
	gate := make([]chan struct{}, *nkeys)
	for i := range gate {
		gate[i] = make(chan struct{})
	}
	arrived := make([]chan struct{}, *nkeys)
	for i := range arrived {
		arrived[i] = make(chan struct{}, *conc)
	}

	start := time.Now()
	for slot := 0; slot < *conc; slot++ {
		wg.Add(1)
		go func(slot int) {
			defer wg.Done()
			conn, err := pgx.Connect(ctx, dsn())
			if err != nil {
				// Fatal rather than `return`: a slot that drops out never arrives
				// at the barrier, and the run would hang instead of failing.
				fmt.Fprintln(os.Stderr, "connect:", err)
				os.Exit(1)
			}
			defer conn.Close(ctx)
			for i, key := range keys {
				arrived[i] <- struct{}{}
				<-gate[i] // released by the coordinator once all slots have arrived
				// --vary-slot: same key, DIFFERENT body -- a client that reused an
				// idempotency key for a new request. Only B stores a fingerprint
				// and can tell; A and C replay the wrong thing at 200.
				body := map[string]any{"amount_cents": 4200 + i, "currency": "GBP"}
				if slot == *varySlot {
					body = map[string]any{"amount_cents": 999999, "currency": "GBP"}
				}
				t0 := time.Now()
				status, note := h(ctx, conn, runID, key, body, hold)
				results[i][slot] = outcome{status, note, float64(time.Since(t0).Microseconds()) / 1000}
			}
		}(slot)
	}
	go func() {
		for i := range keys {
			for j := 0; j < *conc; j++ {
				<-arrived[i]
			}
			close(gate[i])
		}
	}()
	wg.Wait()
	wall := time.Since(start)

	byStatus := map[int]int{}
	notes := map[string]int{}
	var winners, losers []float64
	for _, rs := range results {
		for _, r := range rs {
			byStatus[r.status]++
			if r.status == 201 {
				winners = append(winners, r.ms)
			} else {
				losers = append(losers, r.ms)
				notes[r.note]++
			}
		}
	}

	var dupKeys, extra, total int
	_ = admin.QueryRow(ctx, `SELECT count(*) FROM (SELECT 1 FROM charges
		WHERE run_id = $1 GROUP BY idempotency_key HAVING count(*) > 1) d`, runID).Scan(&dupKeys)
	_ = admin.QueryRow(ctx, `SELECT coalesce(sum(c - 1), 0) FROM (SELECT count(*) c
		FROM charges WHERE run_id = $1 GROUP BY idempotency_key) d`, runID).Scan(&extra)
	_ = admin.QueryRow(ctx, `SELECT count(*) FROM charges WHERE run_id = $1`, runID).Scan(&total)

	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("correctness")
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Printf("  requests issued          %d\n", *nkeys**conc)
	fmt.Printf("  charge rows written      %d   (must equal %d)\n", total, *nkeys)
	fmt.Printf("  KEYS CHARGED MORE THAN ONCE   %d\n", dupKeys)
	fmt.Printf("  DUPLICATE CHARGES (extra rows) %d\n", extra)
	if extra > 0 {
		fmt.Println("  ^ every one of these is a customer charged twice for one request.")
	}

	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("what each request saw")
	fmt.Println("------------------------------------------------------------------------------")
	for _, s := range []int{201, 200, 409, 422, 500} {
		fmt.Printf("  %-18d%d\n", s, byStatus[s])
	}
	type kv struct {
		k string
		n int
	}
	var top []kv
	for k, n := range notes {
		top = append(top, kv{k, n})
	}
	sort.Slice(top, func(i, j int) bool { return top[i].n > top[j].n })
	for i, e := range top {
		if i == 5 {
			break
		}
		if len(e.k) > 60 {
			e.k = e.k[:60]
		}
		fmt.Printf("      %5dx  %s\n", e.n, e.k)
	}

	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("latency -- winners vs duplicates (the price of the design)")
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Printf("  %-14s%12s%15s%15s\n", "", "p50", "p99", "max")
	for _, row := range []struct {
		name string
		v    []float64
	}{{"winner", winners}, {"duplicate", losers}} {
		if len(row.v) == 0 {
			fmt.Printf("  %-14s%12s%15s%15s\n", row.name, "-", "-", "-")
			continue
		}
		fmt.Printf("  %-14s%9.1f ms%12.1f ms%12.1f ms\n", row.name,
			percentile(row.v, 0.50), percentile(row.v, 0.99), percentile(row.v, 1.0))
	}
	fmt.Printf("\n  wall clock %.2fs for %d requests\n", wall.Seconds(), *nkeys**conc)
	fmt.Println()
	fmt.Println("  full breakdown:  psql -d sep_lab_04_dist -f sql/topic2_assert.sql")
	admin.Close(ctx)
}
