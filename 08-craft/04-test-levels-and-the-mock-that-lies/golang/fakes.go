// Two fakes behind the same seam (`*sql.DB`), and the difference between them
// is the whole Go section of this topic.
//
//	"script"    -- what sqlmock is. It is handed the SQL text and never looks at
//	               it. You configure an ANSWER. It cannot model ORDER BY, LIMIT,
//	               or anything else, because it does not execute anything.
//	"heaptable" -- a small hand-written fake that models BEHAVIOUR: rows live in
//	               storage order, WHERE and ORDER BY and LIMIT are applied in the
//	               order a database applies them.
//
// The second one is sixty lines longer, and writing those sixty lines is the
// point: you cannot fake a database's ordering semantics without first having
// to decide what a scan with no ORDER BY returns. Configuring an answer never
// asks you that question, which is why it never tells you that you were wrong.
package t4mocks

import (
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

func init() {
	sql.Register("script", fakeDriver{mode: "script"})
	sql.Register("heaptable", fakeDriver{mode: "heaptable"})
}

// --- shared fixture registry, keyed by DSN ---------------------------------

type fixture struct {
	mu   sync.Mutex
	rows []Order  // in STORAGE order, which is deliberately not sort order
	seen []string // every SQL text the driver was handed
}

var (
	fixtures  sync.Map // dsn -> *fixture
	dsnSerial atomic.Int64
)

// openFake registers `rows` under a fresh DSN and returns a *sql.DB backed by
// the named driver, plus the fixture so a test can read back what SQL arrived.
func openFake(driverName string, rows []Order) (*sql.DB, *fixture, error) {
	dsn := fmt.Sprintf("%s-%d", driverName, dsnSerial.Add(1))
	f := &fixture{rows: append([]Order(nil), rows...)}
	fixtures.Store(dsn, f)
	db, err := sql.Open(driverName, dsn)
	if err != nil {
		return nil, nil, err
	}
	return db, f, nil
}

func (f *fixture) queries() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.seen...)
}

// --- the driver ------------------------------------------------------------

type fakeDriver struct{ mode string }

func (d fakeDriver) Open(dsn string) (driver.Conn, error) {
	v, ok := fixtures.Load(dsn)
	if !ok {
		return nil, fmt.Errorf("no fixture registered for dsn %q", dsn)
	}
	return &fakeConn{mode: d.mode, fix: v.(*fixture)}, nil
}

type fakeConn struct {
	mode string
	fix  *fixture
}

func (c *fakeConn) Prepare(query string) (driver.Stmt, error) {
	return &fakeStmt{conn: c, query: query}, nil
}
func (c *fakeConn) Close() error              { return nil }
func (c *fakeConn) Begin() (driver.Tx, error) { return nil, errors.New("fake: no transactions") }

type fakeStmt struct {
	conn  *fakeConn
	query string
}

func (s *fakeStmt) Close() error  { return nil }
func (s *fakeStmt) NumInput() int { return -1 } // do not check placeholder counts

func (s *fakeStmt) Exec(args []driver.Value) (driver.Result, error) {
	return nil, errors.New("fake: read-only")
}

func (s *fakeStmt) Query(args []driver.Value) (driver.Rows, error) {
	f := s.conn.fix
	f.mu.Lock()
	f.seen = append(f.seen, s.query)
	rows := append([]Order(nil), f.rows...)
	f.mu.Unlock()

	if s.conn.mode == "script" {
		// THE LIE, in one line: the query text was recorded and discarded. Every
		// caller gets the fixture, in the order the fixture was written, whether
		// or not the SQL asked for an order, a filter or a limit.
		return &fakeRows{rows: rows}, nil
	}
	return &fakeRows{rows: executeish(s.query, args, rows)}, nil
}

// executeish is the hand-written fake's whole contribution: it applies the
// clauses a database applies, in the order a database applies them. Writing it
// forces three decisions that the scripted fake let you skip.
func executeish(query string, args []driver.Value, rows []Order) []Order {
	q := strings.ToUpper(query)

	customerID, limit := int64(-1), int64(-1)
	if len(args) > 0 {
		customerID, _ = args[0].(int64)
	}
	if len(args) > 1 {
		limit, _ = args[1].(int64)
	}

	out := make([]Order, 0, len(rows))
	for _, o := range rows { // DECISION 1: with no ORDER BY, a scan yields
		if int64(o.CustomerID) == customerID { //  STORAGE order. Not insertion
			out = append(out, o) //                order, and not sorted.
		}
	}

	if strings.Contains(q, "STATUS <> 'CANCELLED'") { // DECISION 2: WHERE runs
		kept := out[:0:0] //                            before LIMIT, always.
		for _, o := range out {
			if o.Status != "cancelled" {
				kept = append(kept, o)
			}
		}
		out = kept
	}

	if strings.Contains(q, "ORDER BY CREATED_AT DESC, ID DESC") {
		sort.SliceStable(out, func(i, j int) bool {
			if !out[i].CreatedAt.Equal(out[j].CreatedAt) {
				return out[i].CreatedAt.After(out[j].CreatedAt)
			}
			return out[i].ID > out[j].ID
		})
	} else if strings.Contains(q, "ORDER BY") {
		panic("hand-written fake: unsupported ORDER BY -- " +
			"which is the fake telling you it does not know this semantics, " +
			"instead of quietly inventing it")
	}

	if strings.Contains(q, "LIMIT") && limit >= 0 && int64(len(out)) > limit {
		out = out[:limit] // DECISION 3: LIMIT is last, and it counts ROWS THE
	} //                     DATABASE KEPT, not rows the caller will keep.
	return out
}

// --- rows ------------------------------------------------------------------

type fakeRows struct {
	rows []Order
	i    int
}

func (r *fakeRows) Columns() []string {
	return []string{"id", "customer_id", "status", "total_cents", "created_at"}
}
func (r *fakeRows) Close() error { return nil }

func (r *fakeRows) Next(dest []driver.Value) error {
	if r.i >= len(r.rows) {
		return io.EOF
	}
	o := r.rows[r.i]
	r.i++
	dest[0] = int64(o.ID)
	dest[1] = int64(o.CustomerID)
	dest[2] = o.Status
	dest[3] = int64(o.TotalCents)
	dest[4] = o.CreatedAt
	return nil
}

// --- the shared fixture ----------------------------------------------------

var t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func at(min int) time.Time { return t0.Add(time.Duration(min) * time.Minute) }

// newestFirst is what a person writes when asked for "the recent orders": the
// answer, already in the right order, already filtered. It is a plausible
// fixture and it is the bug's alibi.
func newestFirst() []Order {
	return []Order{
		{ID: 5, CustomerID: 1, Status: "paid", TotalCents: 500, CreatedAt: at(50)},
		{ID: 4, CustomerID: 1, Status: "paid", TotalCents: 400, CreatedAt: at(40)},
		{ID: 3, CustomerID: 1, Status: "paid", TotalCents: 300, CreatedAt: at(30)},
		{ID: 2, CustomerID: 1, Status: "paid", TotalCents: 200, CreatedAt: at(20)},
	}
}

// storageOrder is the same customer's rows as a heap holds them: inserted in a
// shuffled order, and orders 5 and 4 were UPDATEd afterwards (a status change),
// which on a real heap moves them to the end of the table.
func storageOrder() []Order {
	return []Order{
		{ID: 2, CustomerID: 1, Status: "paid", TotalCents: 200, CreatedAt: at(20)},
		{ID: 6, CustomerID: 1, Status: "cancelled", TotalCents: 600, CreatedAt: at(60)},
		{ID: 3, CustomerID: 1, Status: "paid", TotalCents: 300, CreatedAt: at(30)},
		{ID: 1, CustomerID: 1, Status: "cancelled", TotalCents: 100, CreatedAt: at(10)},
		{ID: 5, CustomerID: 1, Status: "paid", TotalCents: 500, CreatedAt: at(50)},
		{ID: 4, CustomerID: 1, Status: "paid", TotalCents: 400, CreatedAt: at(40)},
		{ID: 9, CustomerID: 2, Status: "paid", TotalCents: 900, CreatedAt: at(90)},
	}
}
