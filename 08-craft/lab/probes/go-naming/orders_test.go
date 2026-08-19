package orders

import (
	"errors"
	"testing"
)

func store() *Store {
	return NewStore([]uint64{7, 8}, []Order{
		{ID: 1, CustomerID: 7, Status: "paid", TotalCents: 500, CreatedAt: 30},
		{ID: 2, CustomerID: 7, Status: "cancelled", TotalCents: 100, CreatedAt: 20},
		{ID: 3, CustomerID: 7, Status: "paid", TotalCents: 900, CreatedAt: 10},
		{ID: 4, CustomerID: 8, Status: "paid", TotalCents: 100, CreatedAt: 40},
		{ID: 5, CustomerID: 7, Status: "paid", TotalCents: 100, CreatedAt: 30}, // tie with ID 1
	})
}

func TestForCustomerIsNewestFirstAndTotallyOrdered(t *testing.T) {
	got, err := store().ForCustomer(7, 10)
	if err != nil {
		t.Fatal(err)
	}
	// IDs 5 and 1 both have CreatedAt=30; the tiebreaker makes the order defined.
	want := []uint64{5, 1, 2, 3}
	for i, id := range want {
		if got[i].ID != id {
			t.Fatalf("position %d: got id %d, want %d (%+v)", i, got[i].ID, id, got)
		}
	}
}

func TestRecentFiltersBeforeLimiting(t *testing.T) {
	// The Python original filtered AFTER the limit, so asking for 3 returned 2.
	got, err := store().Recent(7, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("asked for 3 non-cancelled orders, got %d: %+v", len(got), got)
	}
	for _, o := range got {
		if o.Status == "cancelled" {
			t.Fatalf("Recent returned a cancelled order: %+v", o)
		}
	}
}

func TestSentinelErrorsAreTestableWithErrorsIs(t *testing.T) {
	if _, err := store().ForCustomer(99, 5); !errors.Is(err, ErrCustomerNotFound) {
		t.Fatalf("want ErrCustomerNotFound, got %v", err)
	}
	if _, err := store().Recent(7, 0); !errors.Is(err, ErrInvalidLimit) {
		t.Fatalf("want ErrInvalidLimit, got %v", err)
	}
}

func TestPageTotalMatchesTheFilter(t *testing.T) {
	p, err := store().Page(7, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Items) != 2 || p.Total != 4 {
		t.Fatalf("got %d items of %d total, want 2 of 4", len(p.Items), p.Total)
	}
}
