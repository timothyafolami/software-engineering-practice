// Layer 5 - Topic 7: idempotency, and degradation decided in advance (Go).
//
// Runs the whole of the topic's experiment against a REAL local Postgres, with
// no containers involved: the naive double-charge, the atomic
// insert-on-unique-constraint that makes a retry safe, the ambiguous result
// where the client never learns it succeeded, a crash between the claim and the
// work, the fingerprint check, and a degradation matrix that is a table you
// wrote in advance rather than an argument you have at 3am. Same scenarios,
// same columns and the same conclusions as ../python/idempotency.py.
//
// THE GO-SPECIFIC CONTENT, WHICH IS ALL AT THE DRIVER LEVEL
//
//  1. `pgx.ErrNoRows` from the `RETURNING` clause is the lost-the-race signal.
//     There is no exception to catch and no session to poison: the insert
//     either returns a row or it does not, and "did not" is an ordinary error
//     value you compare with `errors.Is`. That is the whole control flow.
//
//  2. `*pgconn.PgError.ConstraintName` tells you WHICH unique constraint
//     fired. This matters the moment you have two, and this program has two on
//     purpose: `idempotency_keys_pkey` on the key, and
//     `charges_merchant_ref_uniq` on the merchant's own reference number. The
//     correct handling differs -- the first is a retry and must replay, the
//     second is a genuine business conflict and must be a 409 the client is
//     expected to act on. A handler that catches "unique violation" without
//     reading the name will replay someone else's answer for the second case.
//     Section 5 fires both and shows them being told apart by name.
//
//  3. `context` cancellation on the losing path actually returns the
//     connection to the pool. The 409 path here runs under a context with a
//     short deadline; when it fires, pgx cancels the query, the connection
//     goes back, and the pool does not leak. Everything in topics 2, 3 and 6
//     depended on that property; this is where it stops being about latency
//     and starts being about correctness.
//
// WHAT THIS DEMONSTRATES, IN ORDER
//
//  1. Setup      Two key tables: one with a UNIQUE index on `key`, one
//     without. Same SQL shape, one constraint apart.
//  2. The race   50 concurrent requests released together by a barrier,
//     sharing ONE idempotency key, in five implementations.
//  3. The ambiguous result: responses lost on the way back, clients retry.
//  4. The crash test: dying between the claim and the work, then the TTL.
//  5. Two unique constraints, told apart by name.
//  6. The fingerprint test.
//  7. Degradation decided in advance: the matrix, a kill switch flipped
//     mid-run, and the row that is not a kill switch at all.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//
//   - `charge_rows` in the naive concurrent row. Anything above 1 is money.
//   - The `pool=1` row against the one above it: a SMALLER limit hiding the
//     bug completely, which is the most dangerous kind of green test.
//   - `409s` in claim+execute against single-txn. Both are correct; one makes
//     the loser retry and the other makes it wait, and `loser_p99` prices the
//     waiting.
//   - `orphaned` in the crash rows, before and after the TTL expires.
//   - Section 5's two constraint names, and the two different answers.
//
// REQUIREMENTS
//
//	A local Postgres accepting connections (`pg_isready`). This program creates
//	the `failure_lab` database if it is missing and owns the tables it makes
//	inside it; `dropdb failure_lab` when you are done with the layer.
//
// RUN
//
//	go run idempotency.go
//
// Takes about ten seconds. Takes no arguments.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ------------------------------------------------------------------ config

const (
	concurrency        = 50 // the README's number: 50 requests sharing one key
	holdMs             = 25 // widens the window; it does not create the race
	ttl                = 60 * time.Second
	crashTTL           = 2 * time.Second
	ambiguousKeys      = 20
	ambiguousLossP     = 0.5
	maxClientAttempts  = 5
	loserQueryDeadline = 3 * time.Second
)

func dbName() string {
	if v := os.Getenv("FAILURE_LAB_DB"); v != "" {
		return v
	}
	return "failure_lab"
}

// Empty host means the libpq default: a local unix socket as the current user,
// which is what `psql` does and therefore what "a local Postgres" means here.
func dsn(db string) string { return "postgres:///" + db }

type response struct {
	status   int
	body     string // canonical JSON, so "byte-identical replay" is checkable
	replayed bool
}

// canonical re-serialises a JSON document with sorted keys.
//
// Necessary because a replayed response comes back out of `jsonb`, which stores
// a parsed document and prints it in its own key order -- so a byte comparison
// against the string this process produced would differ for reasons that have
// nothing to do with idempotency. `distinct_responses` is supposed to count
// answers, not encodings. (`jsonb` is also why a stored response is not, in the
// strictest sense, replayed byte-for-byte: if that matters to your API
// contract, store the body as `text` or `bytea`.)
func canonical(raw string) string {
	var v any
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		return raw
	}
	b, err := json.Marshal(v)
	if err != nil {
		return raw
	}
	return string(b)
}

func fingerprint(body map[string]any) string {
	b, _ := json.Marshal(body) // encoding/json sorts map keys, which is the point
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// ------------------------------------------------------------------ schema

const schemaSQL = `
DROP TABLE IF EXISTS charges;
DROP TABLE IF EXISTS idempotency_keys;
DROP TABLE IF EXISTS idempotency_keys_naive;

CREATE TABLE charges (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idem_key      text NOT NULL,
  merchant_ref  text,
  amount_cents  integer NOT NULL,
  currency      text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- The SECOND unique constraint. It is a business rule ("one charge per
-- merchant reference"), not an idempotency mechanism, and section 5 is about
-- why a handler must tell the two apart by name.
CREATE UNIQUE INDEX charges_merchant_ref_uniq ON charges (merchant_ref)
  WHERE merchant_ref IS NOT NULL;

-- The correct table. The PRIMARY KEY on the key column IS the unique index, and
-- that unique index is the entire mechanism -- not the SELECT, not the
-- application logic, not the driver.
CREATE TABLE idempotency_keys (
  key         text PRIMARY KEY,
  fingerprint text NOT NULL,
  state       text NOT NULL,
  response    jsonb,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);

-- The naive table. The same columns, and no unique index on key. That single
-- difference is what the first three scenarios measure.
CREATE TABLE idempotency_keys_naive (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key         text NOT NULL,
  fingerprint text NOT NULL,
  state       text NOT NULL,
  response    jsonb,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);
`

func ensureDatabase(ctx context.Context) error {
	conn, err := pgx.Connect(ctx, dsn("postgres"))
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	var one int
	err = conn.QueryRow(ctx, "SELECT 1 FROM pg_database WHERE datname = $1", dbName()).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		if _, err := conn.Exec(ctx, fmt.Sprintf(`CREATE DATABASE %q`, dbName())); err != nil {
			return err
		}
		fmt.Printf("  created database %s\n", dbName())
		return nil
	}
	return err
}

// ------------------------------------------------------------------ server

type server struct {
	pool     *pgxpool.Pool
	holdMs   int
	ttl      time.Duration
	crashSet bool
}

// doWork is the side effect. In real life a card is charged here.
func (s *server) doWork(ctx context.Context, tx pgx.Tx, key string, body map[string]any) (string, error) {
	time.Sleep(time.Duration(s.holdMs) * time.Millisecond)
	var id int64
	err := tx.QueryRow(ctx,
		`INSERT INTO charges (idem_key, amount_cents, currency) VALUES ($1,$2,$3) RETURNING id`,
		key, body["amount_cents"], body["currency"]).Scan(&id)
	if err != nil {
		return "", err
	}
	b, _ := json.Marshal(map[string]any{
		"charge_id": id, "amount_cents": body["amount_cents"],
		"currency": body["currency"], "status": "succeeded",
	})
	return string(b), nil
}

// chargeNaive is wrong at READ COMMITTED, and wrong in a way that reads as
// careful. Two concurrent transactions both SELECT, both see no row, and both
// proceed: READ COMMITTED gives each statement a fresh snapshot of *committed*
// data, and neither transaction has committed anything the other can see. The
// unique index is the only thing that would have serialised them, and this
// table does not have one.
func (s *server) chargeNaive(ctx context.Context, key string, body map[string]any) (response, error) {
	fp := fingerprint(body)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return response{}, err
	}
	defer tx.Rollback(ctx)

	var state string
	var stored *string
	err = tx.QueryRow(ctx,
		`SELECT state, response::text FROM idempotency_keys_naive WHERE key = $1`,
		key).Scan(&state, &stored)
	switch {
	case err == nil && state == "completed" && stored != nil:
		return response{status: 200, body: canonical(*stored), replayed: true}, tx.Commit(ctx)
	case errors.Is(err, pgx.ErrNoRows):
		if _, err := tx.Exec(ctx,
			`INSERT INTO idempotency_keys_naive (key, fingerprint, state, expires_at)
			 VALUES ($1,$2,'in_progress', now() + $3::interval)`,
			key, fp, s.ttl.String()); err != nil {
			return response{}, err
		}
	case err != nil:
		return response{}, err
	}

	resp, err := s.doWork(ctx, tx, key, body)
	if err != nil {
		return response{}, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE idempotency_keys_naive SET state='completed', response=$2 WHERE key=$1`,
		key, resp); err != nil {
		return response{}, err
	}
	return response{status: 200, body: resp}, tx.Commit(ctx)
}

// claimSQL is the whole mechanism, in one statement.
//
// The DO UPDATE arm is not a convenience: it is how a claim whose holder died
// gets taken over, and it fires only for a stale in_progress row whose body
// matches. A `completed` row can never satisfy the WHERE, so a finished key is
// never re-executed no matter how late the retry arrives.
const claimSQL = `
INSERT INTO idempotency_keys (key, fingerprint, state, expires_at)
VALUES ($1, $2, 'in_progress', now() + $3::interval)
ON CONFLICT (key) DO UPDATE
   SET state = 'in_progress', claimed_at = now(), expires_at = EXCLUDED.expires_at
 WHERE idempotency_keys.state = 'in_progress'
   AND idempotency_keys.expires_at < now()
   AND idempotency_keys.fingerprint = EXCLUDED.fingerprint
RETURNING key`

// chargeCorrect claims in its own transaction, then executes.
//
// The claim commits before the work starts, which is what lets a concurrent
// request find `in_progress` and answer 409 immediately instead of holding a
// connection open waiting for someone else's card charge. The price is the
// crash window: if the executor dies between the two transactions the claim
// outlives it and blocks every retry until the TTL expires.
func (s *server) chargeCorrect(ctx context.Context, key string, body map[string]any) (response, error) {
	fp := fingerprint(body)
	var got string
	err := s.pool.QueryRow(ctx, claimSQL, key, fp, s.ttl.String()).Scan(&got)
	if errors.Is(err, pgx.ErrNoRows) {
		// pgx.ErrNoRows IS the lost-the-race signal. No exception, no poisoned
		// session, no rollback to remember: an ordinary error value.
		return s.replayOrConflict(ctx, key, fp)
	}
	if err != nil {
		return response{}, err
	}
	if s.crashSet {
		// The claim is committed and this process is about to stop existing.
		// Nothing rolls back, because there is no open transaction to roll
		// back: that is precisely why the row is now an orphan.
		return response{}, errors.New("simulated crash after claim, before work")
	}

	// The side effect and the stored response, in ONE transaction. If these
	// were two, a crash between them leaves a charge nobody can replay -- which
	// is worse than the orphan above, because the money moved.
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return response{}, err
	}
	defer tx.Rollback(ctx)
	resp, err := s.doWork(ctx, tx, key, body)
	if err != nil {
		return response{}, err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE idempotency_keys SET state='completed', response=$2 WHERE key=$1`,
		key, resp); err != nil {
		return response{}, err
	}
	return response{status: 200, body: resp}, tx.Commit(ctx)
}

// replayOrConflict runs under its own deadline. When it fires, pgx cancels the
// query and the connection goes back to the pool -- the losing path does not
// leak the resource, which is the property topics 2, 3 and 6 all leaned on.
func (s *server) replayOrConflict(ctx context.Context, key, fp string) (response, error) {
	ctx, cancel := context.WithTimeout(ctx, loserQueryDeadline)
	defer cancel()
	var state, gotFp string
	var stored *string
	err := s.pool.QueryRow(ctx,
		`SELECT state, fingerprint, response::text FROM idempotency_keys WHERE key=$1`,
		key).Scan(&state, &gotFp, &stored)
	if errors.Is(err, pgx.ErrNoRows) {
		return response{status: 409}, nil
	}
	if err != nil {
		return response{}, err
	}
	if gotFp != fp {
		return response{status: 422}, nil
	}
	if state == "completed" && stored != nil {
		return response{status: 200, body: canonical(*stored), replayed: true}, nil
	}
	return response{status: 409}, nil
}

// chargeCorrectSingleTxn is equally correct and never produces an orphan --
// because there is no window between the claim and the work for a crash to land
// in. What it produces instead is waiting: a loser's INSERT blocks on the
// winner's uncommitted tuple until the winner commits, so every concurrent
// duplicate holds a connection for the full duration of the work. Read
// `loser_p99`: at a large enough duplicate rate that is a pool exhaustion
// (topic 5) wearing an idempotency costume.
func (s *server) chargeCorrectSingleTxn(ctx context.Context, key string, body map[string]any) (response, error) {
	fp := fingerprint(body)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return response{}, err
	}
	defer tx.Rollback(ctx)

	var got string
	err = tx.QueryRow(ctx,
		`INSERT INTO idempotency_keys (key, fingerprint, state, expires_at)
		 VALUES ($1,$2,'in_progress', now() + $3::interval)
		 ON CONFLICT (key) DO NOTHING RETURNING key`,
		key, fp, s.ttl.String()).Scan(&got)
	if err == nil {
		resp, err := s.doWork(ctx, tx, key, body)
		if err != nil {
			return response{}, err
		}
		if _, err := tx.Exec(ctx,
			`UPDATE idempotency_keys SET state='completed', response=$2 WHERE key=$1`,
			key, resp); err != nil {
			return response{}, err
		}
		return response{status: 200, body: resp}, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return response{}, err
	}
	// The INSERT above already waited for the winner to commit, so the row is
	// visible to this statement: READ COMMITTED takes a fresh snapshot per
	// statement, which is the same property that made the naive version wrong
	// and makes this one work.
	var state, gotFp string
	var stored *string
	if err := tx.QueryRow(ctx,
		`SELECT state, fingerprint, response::text FROM idempotency_keys WHERE key=$1`,
		key).Scan(&state, &gotFp, &stored); err != nil {
		return response{}, err
	}
	if gotFp != fp {
		return response{status: 422}, tx.Commit(ctx)
	}
	if state == "completed" && stored != nil {
		return response{status: 200, body: canonical(*stored), replayed: true}, tx.Commit(ctx)
	}
	return response{status: 409}, tx.Commit(ctx)
}

// ------------------------------------------------------------------ client

type result struct {
	mu        sync.Mutex
	statuses  []int
	latencies []float64
	bodies    map[string]int
	errs      int
	attempts  int
}

func newResult() *result { return &result{bodies: map[string]int{}} }

func (r *result) record(status int, latMs float64, body string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.statuses = append(r.statuses, status)
	r.latencies = append(r.latencies, latMs)
	r.attempts++
	if body != "" {
		r.bodies[body]++
	}
}

func (r *result) fail() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.errs++
	r.attempts++
}

// fireTogether releases n callers at the same instant.
//
// The barrier is not decoration. Without it the goroutines ramp up, the first
// request finishes before the last one starts, and the naive version passes --
// which is the top entry on the README's list of ways this experiment breaks
// rather than the prediction being wrong.
func fireTogether(n int, fn func(i int) (response, error)) *result {
	res := newResult()
	gate := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-gate
			t0 := time.Now()
			r, err := fn(i)
			if err != nil {
				res.fail()
				return
			}
			res.record(r.status, float64(time.Since(t0).Microseconds())/1000.0, r.body)
		}(i)
	}
	close(gate)
	wg.Wait()
	return res
}

// ------------------------------------------------------------------ counts

type tally struct{ charges, orphaned int }

func countRows(ctx context.Context, pool *pgxpool.Pool, key string) tally {
	var t tally
	if key == "" {
		_ = pool.QueryRow(ctx, `SELECT count(*) FROM charges`).Scan(&t.charges)
	} else {
		_ = pool.QueryRow(ctx, `SELECT count(*) FROM charges WHERE idem_key=$1`, key).Scan(&t.charges)
	}
	_ = pool.QueryRow(ctx,
		`SELECT count(*) FROM idempotency_keys WHERE state='in_progress' AND expires_at > now()`).
		Scan(&t.orphaned)
	return t
}

func pctile(vals []float64, q float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	s := append([]float64(nil), vals...)
	sort.Float64s(s)
	idx := int(float64(len(s))*q+0.9999) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(s) {
		idx = len(s) - 1
	}
	return s[idx]
}

func report(mode string, res *result, t tally, extra string) {
	n409, n422 := 0, 0
	for _, s := range res.statuses {
		if s == 409 {
			n409++
		}
		if s == 422 {
			n422++
		}
	}
	fmt.Printf("  mode=%-32s charge_rows=%-4d distinct_responses=%-4d 409s=%-4d 422s=%-4d "+
		"orphaned_in_progress=%-4d attempts=%-4d%s\n",
		mode, t.charges, len(res.bodies), n409, n422, t.orphaned, res.attempts, extra)
}

func rule(title string) {
	fmt.Println()
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(title)
	fmt.Println(strings.Repeat("=", 78))
}

func latenciesWhere(res *result, want func(int) bool) []float64 {
	out := []float64{}
	for i, s := range res.statuses {
		if want(s) {
			out = append(out, res.latencies[i])
		}
	}
	return out
}

// --------------------------------------------- two constraints, by name

// A unique violation is not one situation. `idempotency_keys_pkey` means "this
// is a retry, replay the answer". `charges_merchant_ref_uniq` means "you are
// trying to charge the same merchant reference twice with a DIFFERENT
// idempotency key", which is a genuine conflict the client has to resolve. Both
// arrive as SQLSTATE 23505; only the constraint name separates them, and pgx
// hands it to you on *pgconn.PgError.
func classifyUnique(err error) (constraint string, ok bool) {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return pgErr.ConstraintName, true
	}
	return "", false
}

func twoConstraints(ctx context.Context, pool *pgxpool.Pool) {
	ref := fmt.Sprintf("merchant-ref-%d", time.Now().UnixNano())
	insert := func(key string) error {
		_, err := pool.Exec(ctx,
			`INSERT INTO charges (idem_key, merchant_ref, amount_cents, currency)
			 VALUES ($1,$2,4200,'usd')`, key, ref)
		return err
	}
	k1 := "twoc-a-" + ref
	if err := insert(k1); err != nil {
		fmt.Println("  unexpected:", err)
		return
	}
	fmt.Printf("  first charge for merchant_ref=%s: inserted\n", ref)

	// (a) the same idempotency key again -> the KEY constraint fires. The first
	//     insert is the claim that succeeded; the second is the retry.
	claim := `INSERT INTO idempotency_keys (key, fingerprint, state, expires_at)
	          VALUES ($1,'fp','in_progress', now() + interval '60 seconds')`
	if _, err := pool.Exec(ctx, claim, k1); err != nil {
		fmt.Println("  unexpected:", err)
		return
	}
	_, err := pool.Exec(ctx, claim, k1)
	if name, ok := classifyUnique(err); ok {
		fmt.Printf("  duplicate idempotency key    -> 23505 on %-28s => replay the stored answer (retry)\n", name)
	}

	// (b) a DIFFERENT idempotency key, same merchant reference -> the BUSINESS
	//     constraint fires. Replaying here would be wrong: there is no stored
	//     answer for this key, and the client did not retry -- it double-booked.
	err = insert("twoc-b-" + ref)
	if name, ok := classifyUnique(err); ok {
		fmt.Printf("  duplicate merchant reference -> 23505 on %-28s => 409, the client must resolve it\n", name)
	}
	fmt.Println()
	fmt.Println("  Both are SQLSTATE 23505. A handler that branches on the SQLSTATE alone")
	fmt.Println("  gives the second case the first case's answer, and tells a client that")
	fmt.Println("  double-booked that its charge succeeded. pgx surfaces ConstraintName;")
	fmt.Println("  use it.")
}

// ------------------------------------------- degradation, decided in advance

type matrixRow struct {
	tier                          int
	feature, off, switchName, who string
	blast                         string
}

var degradationMatrix = []matrixRow{
	{0, "authorise + capture", "nothing works; this is the product", "none - never shed", "nobody", "total"},
	{1, "3-D Secure step-up", "non-authenticated auth; issuer may decline more", "flag: risk.stepup", "on-call", "higher decline rate"},
	{2, "fraud enrichment", "cached features only; wider manual review queue", "flag: risk.enrich", "on-call", "review backlog"},
	{2, "currency rate refresh", "last-known rate, capped staleness", "config: fx.freeze", "on-call", "small FX drift"},
	{3, "receipt email", "queued, sent late; nothing is lost", "flag: notify.receipt", "on-call", "support tickets"},
	{3, "analytics fan-out", "dashboards go stale for the duration", "flag: analytics.emit", "on-call", "reporting only"},
}

// flags are read at request time, never at init time. That is the whole design.
type flags struct {
	mu sync.RWMutex
	v  map[string]bool
}

func newFlags() *flags {
	return &flags{v: map[string]bool{"risk.enrich": true, "notify.receipt": true, "analytics.emit": true}}
}
func (f *flags) get(name string) bool {
	f.mu.RLock()
	defer f.mu.RUnlock()
	v, ok := f.v[name]
	return !ok || v
}
func (f *flags) set(name string, val bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.v[name] = val
}

// A flag read once, at init, into a package-level variable. It is in the matrix
// and it has an owner and it looks exactly like the others. It is not a kill
// switch: flipping it changes nothing until someone deploys.
var bakedInEnrichEnabled = true

func degradationDemo(f *flags) {
	depUp := false

	handle := func() bool {
		if f.get("risk.enrich") {
			time.Sleep(60 * time.Millisecond) // the sick dependency
			if !depUp {
				return false // tier 2 failing takes tier 0 down with it
			}
		}
		if f.get("notify.receipt") {
			time.Sleep(2 * time.Millisecond)
		}
		return true
	}
	measure := func(n int) (float64, float64) {
		t0 := time.Now()
		ok := 0
		for i := 0; i < n; i++ {
			if handle() {
				ok++
			}
		}
		el := time.Since(t0).Seconds()
		return 100.0 * float64(ok) / float64(n), float64(n) / el
	}

	fmt.Println("  The matrix, written before the incident:")
	fmt.Printf("    %-5s%-24s%-50s%-22s%s\n", "tier", "feature", "off looks like", "kill switch", "blast radius")
	rows := append([]matrixRow(nil), degradationMatrix...)
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].tier < rows[j].tier })
	for _, r := range rows {
		fmt.Printf("    %-5d%-24s%-50s%-22s%s\n", r.tier, r.feature, r.off, r.switchName, r.blast)
	}
	fmt.Println()
	fmt.Println("  Shed order follows the tier column, which is business importance --")
	fmt.Println("  not code structure, and not whatever is easiest to switch off.")
	fmt.Println()

	ok, rps := measure(120)
	fmt.Printf("  dependency down, matrix not applied:  success=%5.1f%%  goodput=%6.1f/s\n", ok, rps)
	f.set("risk.enrich", false)    // tier 2 first
	f.set("analytics.emit", false) // tier 3
	ok, rps = measure(120)
	fmt.Println("  after flipping risk.enrich + analytics.emit (no deploy, no restart):")
	fmt.Printf("                                        success=%5.1f%%  goodput=%6.1f/s\n", ok, rps)
	fmt.Println()
	fmt.Printf("  bakedInEnrichEnabled is still %v. It is in the matrix, it has an owner,\n", bakedInEnrichEnabled)
	fmt.Println("  and it cannot be changed without a deploy -- so it is not a kill switch.")
	fmt.Println("  Any row like it is a plan, not a control.")
}

// -------------------------------------------------------------------- main

func main() {
	ctx := context.Background()
	rule("Layer 5 - Topic 7: idempotency and degradation, decided in advance (Go)")
	if err := ensureDatabase(ctx); err != nil {
		fmt.Println("  cannot reach Postgres:", err)
		fmt.Println("  this program needs a local server: check `pg_isready`.")
		os.Exit(1)
	}

	cfg, err := pgxpool.ParseConfig(dsn(dbName()))
	if err != nil {
		panic(err)
	}
	cfg.MaxConns = concurrency
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		panic(err)
	}
	defer pool.Close()

	if _, err := pool.Exec(ctx, schemaSQL); err != nil {
		panic(err)
	}
	fmt.Printf("  database          %s (local, no containers)\n", dbName())
	fmt.Printf("  concurrency       %d requests sharing ONE idempotency key\n", concurrency)
	fmt.Printf("  work window       %d ms inside the executing transaction\n", holdMs)
	fmt.Println("  isolation         READ COMMITTED (Postgres' default; not changed anywhere)")
	fmt.Println("  driver            pgx v5; lost-the-race is pgx.ErrNoRows, not an exception")

	body := map[string]any{"amount_cents": 4200, "currency": "usd"}
	srv := &server{pool: pool, holdMs: holdMs, ttl: ttl}

	// ---------------------------------------------------------- scenarios
	rule("THE RACE: 50 requests, one key, released together")

	key := fmt.Sprintf("naive-seq-%d", time.Now().UnixNano())
	res := newResult()
	for i := 0; i < concurrency; i++ {
		t0 := time.Now()
		r, err := srv.chargeNaive(ctx, key, body)
		if err != nil {
			res.fail()
			continue
		}
		res.record(r.status, float64(time.Since(t0).Microseconds())/1000.0, r.body)
	}
	report("naive / sequential", res, countRows(ctx, pool, key), "   <- correct, and it proves nothing")

	key = fmt.Sprintf("naive-conc-%d", time.Now().UnixNano())
	res = fireTogether(concurrency, func(int) (response, error) { return srv.chargeNaive(ctx, key, body) })
	report("naive / 50 concurrent", res, countRows(ctx, pool, key), "   <- every row is a charge to a real card")

	smallCfg, _ := pgxpool.ParseConfig(dsn(dbName()))
	smallCfg.MaxConns = 1
	small, err := pgxpool.NewWithConfig(ctx, smallCfg)
	if err != nil {
		panic(err)
	}
	srvSmall := &server{pool: small, holdMs: holdMs, ttl: ttl}
	key = fmt.Sprintf("naive-pool1-%d", time.Now().UnixNano())
	res = fireTogether(concurrency, func(int) (response, error) { return srvSmall.chargeNaive(ctx, key, body) })
	report("naive / 50 concurrent / pool=1", res, countRows(ctx, pool, key), "   <- same bug, hidden by a SMALLER limit")
	small.Close()

	key = fmt.Sprintf("correct-%d", time.Now().UnixNano())
	res = fireTogether(concurrency, func(int) (response, error) { return srv.chargeCorrect(ctx, key, body) })
	lp := pctile(latenciesWhere(res, func(s int) bool { return s != 200 }), 0.99)
	report("correct / claim + execute", res, countRows(ctx, pool, key), fmt.Sprintf("   loser_p99=%6.1fms", lp))

	key = fmt.Sprintf("correct1txn-%d", time.Now().UnixNano())
	res = fireTogether(concurrency, func(int) (response, error) { return srv.chargeCorrectSingleTxn(ctx, key, body) })
	lat := latenciesWhere(res, func(s int) bool { return s == 200 })
	sort.Float64s(lat)
	if len(lat) > 1 {
		lat = lat[1:] // drop the winner; the rest are the ones that waited
	}
	report("correct / single transaction", res, countRows(ctx, pool, key),
		fmt.Sprintf("   loser_p99=%6.1fms  <- they waited instead of 409ing", pctile(lat, 0.99)))

	// ------------------------------------------------- the ambiguous result
	rule("THE AMBIGUOUS RESULT: the server succeeded, the client never heard")
	fmt.Printf("  Each client retries its own key up to %d times; every response has a "+
		"%.0f%% chance of being lost on the way back.\n", maxClientAttempts, ambiguousLossP*100)
	fmt.Println("  The client cannot tell 'did not happen' from 'happened, answer lost'.")
	fmt.Println("  That is not a bug to fix; it is the situation. Idempotency is what")
	fmt.Println("  makes the only available action -- retry -- safe.")
	fmt.Println()
	before := countRows(ctx, pool, "").charges
	ambKeys := make([]string, ambiguousKeys)
	for i := range ambKeys {
		ambKeys[i] = fmt.Sprintf("amb-%d-%d", i, time.Now().UnixNano())
	}
	var attemptMu sync.Mutex
	attempts := 0
	rng := rand.New(rand.NewSource(20260819))
	var rngMu sync.Mutex
	res = fireTogether(ambiguousKeys, func(i int) (response, error) {
		k := ambKeys[i]
		for a := 0; a < maxClientAttempts; a++ {
			attemptMu.Lock()
			attempts++
			attemptMu.Unlock()
			r, err := srv.chargeCorrect(ctx, k, body)
			if err != nil {
				return response{}, err
			}
			if r.status == 409 {
				time.Sleep(20 * time.Millisecond)
				continue
			}
			rngMu.Lock()
			lost := rng.Float64() < ambiguousLossP
			rngMu.Unlock()
			if lost {
				continue // the response is lost; the charge already happened
			}
			return r, nil
		}
		return response{status: 504}, nil
	})
	res.attempts = attempts
	after := countRows(ctx, pool, "")
	delta := after.charges - before
	verdict := "FAIL"
	if delta == ambiguousKeys {
		verdict = "PASS"
	}
	report("correct + retries + lost responses", res, tally{charges: delta, orphaned: after.orphaned},
		fmt.Sprintf("   distinct keys=%d  [%s]", ambiguousKeys, verdict))

	// ------------------------------------------------------- the crash test
	rule("THE CRASH TEST: dying between the claim and the work")
	crashSrv := &server{pool: pool, holdMs: holdMs, ttl: crashTTL, crashSet: true}
	key = fmt.Sprintf("crash-%d", time.Now().UnixNano())
	if _, err := crashSrv.chargeCorrect(ctx, key, body); err != nil {
		fmt.Println("  executor died:", err)
	}
	crashSrv.crashSet = false
	res = fireTogether(5, func(int) (response, error) { return crashSrv.chargeCorrect(ctx, key, body) })
	report("correct + crash, TTL not yet expired", res, countRows(ctx, pool, key),
		"   <- every retry blocked by a dead holder's claim")
	fmt.Printf("  the claim's TTL is %.0fs. Waiting it out...\n", crashTTL.Seconds())
	time.Sleep(crashTTL + 300*time.Millisecond)
	res = fireTogether(5, func(int) (response, error) { return crashSrv.chargeCorrect(ctx, key, body) })
	report("correct + crash, after TTL expiry", res, countRows(ctx, pool, key),
		"   <- reclaimed by ON CONFLICT DO UPDATE, still one charge")
	fmt.Println()
	fmt.Println("  The TTL is the only thing that unblocks a claim whose holder is gone, so")
	fmt.Println("  the client's retry window must be SHORTER than the retention, or the")
	fmt.Println("  guarantee evaporates at exactly the moment it is needed.")

	// -------------------------------------------------- two constraints
	rule("TWO UNIQUE CONSTRAINTS, TOLD APART BY NAME (the pgx-specific part)")
	twoConstraints(ctx, pool)

	// -------------------------------------------------------- fingerprints
	rule("THE FINGERPRINT TEST: same key, different body")
	key = fmt.Sprintf("fp-%d", time.Now().UnixNano())
	first, _ := srv.chargeCorrect(ctx, key, body)
	other := map[string]any{"amount_cents": 99900, "currency": "usd"}
	second, _ := srv.chargeCorrect(ctx, key, other)
	t := countRows(ctx, pool, key)
	fmt.Printf("  first  request  amount=%7v  -> %d  %s\n", body["amount_cents"], first.status, first.body)
	secondBody := second.body
	if secondBody == "" {
		secondBody = "(no body)"
	}
	fmt.Printf("  second request  amount=%7v  -> %d  %s\n", other["amount_cents"], second.status, secondBody)
	fpVerdict := "FAIL"
	if second.status == 422 && t.charges == 1 {
		fpVerdict = "PASS"
	}
	fmt.Printf("  charge rows for this key: %d   [%s]\n", t.charges, fpVerdict)
	fmt.Println("  Without the fingerprint the second request replays the FIRST answer, so")
	fmt.Println("  a client that reused a key by accident is told its $999 charge succeeded.")

	// ------------------------------------------------------- degradation
	rule("DEGRADATION DECIDED IN ADVANCE")
	degradationDemo(newFlags())
	fmt.Println()
}
