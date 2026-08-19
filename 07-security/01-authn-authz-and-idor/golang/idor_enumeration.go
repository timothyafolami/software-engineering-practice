// Layer 7 · Topic 1 — IDOR as a missing authorization check (Go).
//
// One command, no arguments, no deps: `go run idor_enumeration.go`.
// Seeds 1,500 invoices across three interleaved tenants, logs in as alice,
// fires 1,000 enumeration requests at three handler variants, and counts the
// wrong-owner rows leaked per 1,000.
//
// Go's contribution here is ergonomic, not a safety property: with no ORM
// doing implicit fetches, the SELECT is written out in front of you, so the
// missing `AND owner_id = $2` is visible in the diff. invoiceID is an int64
// whether or not it is *yours* -- static typing does not help at all. That is
// the entire difference from the other runtimes, and it is a readability win,
// not a security one.
//
// What to look for: vulnerable leaks ~667/1000; both fixes leak 0 and return
// 404 (not 403) on a wrong-owner id.
package main

import "fmt"

const (
	nInvoices  = 1500
	caller     = 1 // alice
	nRequests  = 1000
)

type invoice struct {
	id      int
	ownerID int
	amount  int
}

type store struct {
	rows        map[int]invoice
	requestUser int // 0 == unset; the RLS "session variable"
}

func newStore() *store {
	s := &store{rows: make(map[int]invoice, nInvoices), requestUser: 0}
	for i := 1; i <= nInvoices; i++ {
		s.rows[i] = invoice{id: i, ownerID: ((i - 1) % 3) + 1, amount: i * 100}
	}
	return s
}

// vulnerable: db.QueryRow("SELECT ... WHERE id=$1", id) -- no caller.
func (s *store) get(id int) (invoice, bool) { r, ok := s.rows[id]; return r, ok }

// query-layer fix: ... WHERE id=$1 AND owner_id=$2
func (s *store) getScoped(id, owner int) (invoice, bool) {
	r, ok := s.rows[id]
	if ok && r.ownerID == owner {
		return r, true
	}
	return invoice{}, false
}

// data-layer fix (RLS analogue): policy enforced below the handler.
func (s *store) getRLS(id int) (invoice, bool) {
	r, ok := s.rows[id]
	if !ok || s.requestUser == 0 || r.ownerID != s.requestUser {
		return invoice{}, false
	}
	return r, true
}

type handler func(s *store, caller, id int) (int, *invoice)

func vulnerable(s *store, caller, id int) (int, *invoice) {
	if r, ok := s.get(id); ok {
		return 200, &r
	}
	return 404, nil
}
func fixedQuery(s *store, caller, id int) (int, *invoice) {
	if r, ok := s.getScoped(id, caller); ok {
		return 200, &r
	}
	return 404, nil
}
func fixedRLS(s *store, caller, id int) (int, *invoice) {
	s.requestUser = caller // SET LOCAL app.current_user
	if r, ok := s.getRLS(id); ok {
		return 200, &r
	}
	return 404, nil
}

// Deterministic LCG in uint32 space -> reproducible, measured numbers.
func enumIDs(n int) []int {
	ids := make([]int, n)
	var x uint32 = 1
	for i := 0; i < n; i++ {
		x = 1103515245*x + 12345
		ids[i] = 1 + int(x%nInvoices)
	}
	return ids
}

func runVariant(h handler, label string) {
	s := newStore()
	leaked, own, notFound := 0, 0, 0
	for _, id := range enumIDs(nRequests) {
		status, row := h(s, caller, id)
		if status == 200 {
			if row.ownerID != caller {
				leaked++
			} else {
				own++
			}
		} else {
			notFound++
		}
	}
	wrongStatus, _ := h(newStore(), caller, 2) // id 2 is bob's
	fmt.Printf("  %-14s leaked=%4d/%d   own=%4d  not_found=%4d   wrong-owner id -> HTTP %d\n",
		label, leaked, nRequests, own, notFound, wrongStatus)
}

func main() {
	fmt.Println("Layer 7 · Topic 1 — IDOR enumeration (logged in as alice, id=1)")
	fmt.Printf("seed: %d invoices, 3 tenants interleaved, alice owns %d\n", nInvoices, nInvoices/3)
	fmt.Printf("attack: %d requests, ids uniform over 1..%d\n\n", nRequests, nInvoices)
	fmt.Println("  handler        wrong-owner rows leaked per 1,000 requests")
	runVariant(vulnerable, "vulnerable")
	runVariant(fixedQuery, "fixed_query")
	runVariant(fixedRLS, "fixed_rls")
	fmt.Println("\nRead: the SELECT is visible, so the missing owner filter is " +
		"visible too -- but nothing forces you to write it. The fix that survives " +
		"a team is the one the next author cannot omit: the data layer (RLS).")
}
