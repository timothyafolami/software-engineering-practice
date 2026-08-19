// Layer 8 Topic 4 - Go: sqlmock's mechanism, and why a hand-written fake is
// systematically better for a boring reason.
//
// WHAT THIS DEMONSTRATES: the four tests in this file pass against a function
// with two bugs (no ORDER BY, filter applied after LIMIT). They pass because the
// scripted driver in `fakes.go` records the SQL and then ignores it -- so the
// fixture, written by hand in the order the author meant, IS the answer.
// `TestScriptedFakeNeverReadsTheQuery` proves that directly: the correct query
// and the broken query produce byte-identical results.
//
// WHAT TO LOOK FOR: run with -v. The last test in `handfake_test.go` runs the
// same two functions against a fake that models storage order, and the buggy
// one returns 2 rows out of a requested 4, in the wrong order.
//
//	cd golang && go test -v ./...
package t4mocks

import (
	"context"
	"reflect"
	"testing"
)

func TestMocked_ReturnsNewestFirst(t *testing.T) {
	// The fixture is newest-first because that is what the author MEANT. The
	// function has no ORDER BY at all; the ordering in this assertion came from
	// the line above it, not from the code under test.
	db, _, err := openFake("script", newestFirst())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	got, err := RecentOrders(context.Background(), db, 1, 4)
	if err != nil {
		t.Fatal(err)
	}
	if want := []int{5, 4, 3, 2}; !reflect.DeepEqual(IDs(got), want) {
		t.Fatalf("got %v, want %v", IDs(got), want)
	}
	t.Logf("PASS on a function with no ORDER BY: %v", IDs(got))
}

func TestMocked_ExcludesCancelled(t *testing.T) {
	// The filter genuinely runs. What the fake cannot show is WHERE it runs --
	// in Go, after the database already spent the limit on the cancelled rows.
	rows := append(newestFirst(), Order{ID: 7, CustomerID: 1, Status: "cancelled", CreatedAt: at(70)})
	db, _, err := openFake("script", rows)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	got, err := RecentOrders(context.Background(), db, 1, 10)
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range got {
		if o.Status == "cancelled" {
			t.Fatalf("cancelled order %d leaked", o.ID)
		}
	}
	t.Logf("PASS: %d rows, none cancelled", len(got))
}

func TestMocked_RespectsLimit(t *testing.T) {
	// The most misleading test in the file. The fixture holds exactly 3 rows
	// because the author applied the limit BY HAND when writing it. LIMIT is
	// never exercised, so bug 2 -- asking for 4 and getting 2 because the
	// database spent two of them on cancelled rows -- cannot be expressed here.
	db, _, err := openFake("script", newestFirst()[:3])
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	got, err := RecentOrders(context.Background(), db, 1, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) > 3 {
		t.Fatalf("returned %d rows for limit 3", len(got))
	}
	t.Logf("PASS: %d rows for limit 3 -- and the limit was applied by the fixture, not the code", len(got))
}

func TestScriptedFakeNeverReadsTheQuery(t *testing.T) {
	// The mechanism, isolated. Same fixture, two different SQL statements: one
	// with WHERE/ORDER BY/LIMIT and one with none of it. Identical results.
	// A fake that does not execute cannot distinguish correct SQL from wrong SQL,
	// which means no assertion built on it can either.
	db, fix, err := openFake("script", newestFirst())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()

	broken, err := RecentOrders(ctx, db, 1, 4)
	if err != nil {
		t.Fatal(err)
	}
	fixed, err := RecentOrdersFixed(ctx, db, 1, 4)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(IDs(broken), IDs(fixed)) {
		t.Fatalf("expected the scripted fake to be blind to the difference: %v vs %v",
			IDs(broken), IDs(fixed))
	}

	seen := fix.queries()
	t.Logf("both returned %v", IDs(broken))
	for i, q := range seen {
		t.Logf("SQL the driver was handed [%d]: %q", i, q)
	}
	t.Logf("one of those has ORDER BY and a WHERE clause, the other has neither, " +
		"and the driver returned the same rows for both")
}
