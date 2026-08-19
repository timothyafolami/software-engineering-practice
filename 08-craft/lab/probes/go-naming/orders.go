// Package orders is topic 9's Go rewrite, and the package name is half the exercise.
//
// WHAT THIS DEMONSTRATES: in Go the reader always sees `package.Identifier`, so
// the package name is part of EVERY name in it and repeating it is an error
// rather than a nicety -- `http.HTTPServer` reads badly, `http.Server` reads
// well. Three of the lab's worst-named Python functions are ported here, and
// the interesting outcome is not that the names got shorter. It is WHICH WORDS
// MOVED OUT of the function name and INTO the package name.
//
//	Python (app/repositories/orders.py, app/shallow/service.py)
//	                                        ->  Go (this package)
//	orders_for_customer(session, id, ...)   ->  orders.ForCustomer(store, id, ...)
//	count_orders_for_customer(session, id)  ->  orders.CountForCustomer(store, id)
//	recent_orders(session, id, limit)       ->  orders.Recent(store, id, limit)
//	list_customer_orders(session, id, ...)  ->  orders.Page(store, id, ...)
//
// In every case the word that moved is `order(s)`. That relocation WOULD also
// have worked in Python -- `from app import orders` then `orders.for_customer(...)`
// -- and the reason it usually does not happen is that Python lets the importer
// discard the module name entirely (`from .orders import orders_for_customer`,
// or `import numpy as np`). Go does not offer that escape, so the qualification
// survives contact with the call site. That is the transferable finding, and
// topic 9 question 4 asks what buying it in Python costs.
//
// WHAT TO LOOK FOR: `go doc -all .` prints the surface as a caller sees it.
// (`go doc ./...` is NOT valid go doc syntax -- it exits 2 with a usage
// message. `go doc` takes a package, a symbol or nothing; the `./...` pattern
// belongs to go build/test/vet.)
// Read it aloud. Every entry reads as a sentence about orders, and not one of
// them says "order" twice.
//
//	go doc -all .
//	go test ./...
package orders

import (
	"errors"
	"fmt"
	"sort"
)

// ErrCustomerNotFound is a sentinel: callers test it with errors.Is and do
// something specific about it. Category 1 of topic 3's taxonomy, expressed the
// way Go expresses it.
var ErrCustomerNotFound = errors.New("customer not found")

// ErrInvalidLimit is returned when limit < 1. Also caller-actionable, and also
// a sentinel rather than a string, because "the caller can branch on it" is the
// only test that matters.
var ErrInvalidLimit = errors.New("limit must be >= 1")

// Order is what a caller sees. Named `Order` and not `OrderModel`, `OrderDTO`
// or `OrderEntity`: the qualification is already in the package name, and each
// of those suffixes describes the implementation's lineage rather than what the
// thing does for a caller.
type Order struct {
	ID         uint64
	CustomerID uint64
	Status     string
	TotalCents int64
	CreatedAt  int64
}

// Store holds orders and the customers they belong to.
type Store struct {
	customers map[uint64]bool
	orders    []Order
}

// NewStore builds a Store. Named `NewStore` rather than `NewOrderStore`,
// because `orders.NewOrderStore` says "order" twice at every call site.
func NewStore(customerIDs []uint64, orders []Order) *Store {
	set := make(map[uint64]bool, len(customerIDs))
	for _, id := range customerIDs {
		set[id] = true
	}
	return &Store{customers: set, orders: orders}
}

// ForCustomer returns customer's orders, newest first, at most limit of them.
//
// Was: `orders_for_customer`. The word `orders` moved into the package name and
// the preposition carried the rest -- `orders.ForCustomer(...)` reads as one
// phrase, which is exactly what a good name is supposed to do.
//
// Returns ErrCustomerNotFound when the customer does not exist; an existing
// customer with no orders yields an empty slice and a nil error. Naming those
// two outcomes differently in the DOC is the part that survives translation to
// any language: the name predicts the happy path, the doc states the contract.
func (s *Store) ForCustomer(customerID uint64, limit int) ([]Order, error) {
	if err := s.requireCustomer(customerID); err != nil {
		return nil, err
	}
	if limit < 1 {
		return nil, fmt.Errorf("ForCustomer(limit=%d): %w", limit, ErrInvalidLimit)
	}
	matching := s.matching(customerID)
	sortNewestFirst(matching)
	if len(matching) > limit {
		matching = matching[:limit]
	}
	return matching, nil
}

// CountForCustomer returns how many orders customer has.
//
// Was: `count_orders_for_customer`. Two words shorter and one word clearer,
// and the count is expressed with the SAME filter ForCustomer uses -- see
// matching -- so the two can never disagree.
func (s *Store) CountForCustomer(customerID uint64) (int, error) {
	if err := s.requireCustomer(customerID); err != nil {
		return 0, err
	}
	return len(s.matching(customerID)), nil
}

// Recent returns customer's most recent non-cancelled orders, at most limit.
//
// Was: `recent_orders`, whose name promised "recent" and delivered "whatever
// the database happened to hand back first, minus the cancelled ones, from an
// already-truncated page". The rename is the fix's first half: once the name
// says `Recent`, the missing sort is a question a reader asks unprompted.
//
// Note what `Recent` still does not say: it does not say the cancelled ones are
// excluded. That is a real limit on what a name can carry, and it is why the
// doc comment exists rather than a name like RecentExcludingCancelled.
func (s *Store) Recent(customerID uint64, limit int) ([]Order, error) {
	if err := s.requireCustomer(customerID); err != nil {
		return nil, err
	}
	if limit < 1 {
		return nil, fmt.Errorf("Recent(limit=%d): %w", limit, ErrInvalidLimit)
	}
	var live []Order
	for _, o := range s.matching(customerID) {
		if o.Status != "cancelled" {
			live = append(live, o) // filter BEFORE the limit, unlike the Python original
		}
	}
	sortNewestFirst(live)
	if len(live) > limit {
		live = live[:limit]
	}
	return live, nil
}

// Page is one page of customer's orders plus the total that matches the filter.
//
// Was: `list_customer_orders`. `list` said nothing the return type does not
// already say, and `customer` is a parameter, not part of the operation.
type PageResult struct {
	Items []Order
	Total int
}

func (s *Store) Page(customerID uint64, limit int) (PageResult, error) {
	items, err := s.ForCustomer(customerID, limit)
	if err != nil {
		return PageResult{}, err
	}
	total, err := s.CountForCustomer(customerID)
	if err != nil {
		return PageResult{}, err
	}
	return PageResult{Items: items, Total: total}, nil
}

// --- unexported. Short names in a small scope are CORRECT, which is the other
// half of Go's rule: name length scales with scope. ---

func (s *Store) requireCustomer(id uint64) error {
	if !s.customers[id] {
		return fmt.Errorf("customer %d: %w", id, ErrCustomerNotFound)
	}
	return nil
}

func (s *Store) matching(id uint64) []Order {
	var out []Order
	for _, o := range s.orders {
		if o.CustomerID == id {
			out = append(out, o)
		}
	}
	return out
}

// sortNewestFirst orders by (CreatedAt, ID) descending -- a TOTAL order.
// Sorting by CreatedAt alone is a partial order, and "any order" includes a
// different one on the next run. Same defect as topic 5's single-column cursor.
func sortNewestFirst(o []Order) {
	sort.Slice(o, func(i, j int) bool {
		if o[i].CreatedAt != o[j].CreatedAt {
			return o[i].CreatedAt > o[j].CreatedAt
		}
		return o[i].ID > o[j].ID
	})
}
