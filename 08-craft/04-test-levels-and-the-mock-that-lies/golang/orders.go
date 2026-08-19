// The unit under test: the same two planted bugs as the Python arm, in Go.
//
// BUG 1: no ORDER BY. A table scan returns storage order, and storage order is
// not insertion order once a row has been updated.
// BUG 2: the cancelled filter runs in Go, after the database has already applied
// LIMIT -- so a caller asking for 4 gets however many of the first 4 survive.
package t4mocks

import (
	"context"
	"database/sql"
	"time"
)

type Order struct {
	ID         int
	CustomerID int
	Status     string
	TotalCents int
	CreatedAt  time.Time
}

const recentSQL = `SELECT id, customer_id, status, total_cents, created_at
FROM orders WHERE customer_id = ? LIMIT ?`

const recentFixedSQL = `SELECT id, customer_id, status, total_cents, created_at
FROM orders WHERE customer_id = ? AND status <> 'cancelled'
ORDER BY created_at DESC, id DESC LIMIT ?`

// RecentOrders is broken twice. Every mocked test in this package passes on it.
func RecentOrders(ctx context.Context, db *sql.DB, customerID, limit int) ([]Order, error) {
	rows, err := db.QueryContext(ctx, recentSQL, customerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Order
	for rows.Next() {
		var o Order
		if err := rows.Scan(&o.ID, &o.CustomerID, &o.Status, &o.TotalCents, &o.CreatedAt); err != nil {
			return nil, err
		}
		if o.Status == "cancelled" { // BUG 2: filtering after the database limited
			continue
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// RecentOrdersFixed does the filtering, the ordering and the limiting in one
// place, in that order. Note the total order -- created_at alone is not one.
func RecentOrdersFixed(ctx context.Context, db *sql.DB, customerID, limit int) ([]Order, error) {
	rows, err := db.QueryContext(ctx, recentFixedSQL, customerID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Order
	for rows.Next() {
		var o Order
		if err := rows.Scan(&o.ID, &o.CustomerID, &o.Status, &o.TotalCents, &o.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// IDs is a readability helper for the test logs.
func IDs(orders []Order) []int {
	ids := make([]int, 0, len(orders))
	for _, o := range orders {
		ids = append(ids, o.ID)
	}
	return ids
}
