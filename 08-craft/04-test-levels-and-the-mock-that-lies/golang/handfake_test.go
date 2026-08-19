// The other half of Go's answer: a fake you wrote, which models behaviour.
//
// WHAT THIS DEMONSTRATES: the same two functions, same seam, against a fake that
// applies WHERE, then ORDER BY, then LIMIT, over rows held in STORAGE order.
// It catches both bugs. It also refuses to guess: `executeish` panics on an
// ORDER BY it does not implement, rather than silently returning something.
//
// WHAT TO LOOK FOR: the assertions below are written the other way round -- they
// assert that the broken function IS broken -- so the package exits 0 and the
// evidence is in the log rather than in a stack trace. The numbers to read are
// "asked for 4, got 2" and the two ID sequences.
package t4mocks

import (
	"context"
	"reflect"
	"testing"
)

func TestHandWrittenFake_CatchesBothBugs(t *testing.T) {
	db, _, err := openFake("heaptable", storageOrder())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	ctx := context.Background()

	const limit = 4
	broken, err := RecentOrders(ctx, db, 1, limit)
	if err != nil {
		t.Fatal(err)
	}
	fixed, err := RecentOrdersFixed(ctx, db, 1, limit)
	if err != nil {
		t.Fatal(err)
	}

	wantFixed := []int{5, 4, 3, 2}
	if !reflect.DeepEqual(IDs(fixed), wantFixed) {
		t.Fatalf("fixed version wrong: got %v, want %v", IDs(fixed), wantFixed)
	}

	// Bug 1: not newest-first. Bug 2: short, because the database spent two of
	// the four limit slots on cancelled rows before Go ever saw them.
	if reflect.DeepEqual(IDs(broken), wantFixed) {
		t.Fatalf("expected the hand-written fake to expose the bugs, got %v", IDs(broken))
	}
	if len(broken) >= limit {
		t.Fatalf("expected fewer than %d rows from the broken version, got %d", limit, len(broken))
	}

	t.Logf("limit=%d", limit)
	t.Logf("  RecentOrders      -> %v   (%d rows: bug 1 wrong order, bug 2 short)",
		IDs(broken), len(broken))
	t.Logf("  RecentOrdersFixed -> %v   (%d rows)", IDs(fixed), len(fixed))
	t.Logf("  the scripted suite in mocked_test.go is green on the same RecentOrders")
}

func TestTheFakeRefusesToInventSemantics(t *testing.T) {
	// The reason a hand-written fake is better is not that it is more faithful.
	// It is that writing it makes you answer questions. This is what that looks
	// like at runtime: an ORDER BY the fake does not implement is an error, not
	// a guess. A scripted fake has no place to put this refusal, because it
	// never reads the clause.
	db, _, err := openFake("heaptable", storageOrder())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	defer func() {
		r := recover()
		if r == nil {
			t.Fatal("expected the fake to refuse an ORDER BY it does not model")
		}
		t.Logf("fake refused, correctly: %v", r)
	}()

	rows, err := db.QueryContext(context.Background(),
		`SELECT id, customer_id, status, total_cents, created_at
FROM orders WHERE customer_id = ? ORDER BY total_cents ASC LIMIT ?`, 1, 4)
	if err == nil {
		rows.Close()
	}
}
