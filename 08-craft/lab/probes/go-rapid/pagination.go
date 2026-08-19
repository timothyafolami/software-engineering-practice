// A line-for-line port of app/core/pagination.py, so three shrinkers get the
// SAME bug.
//
// WHAT THIS DEMONSTRATES: the variable in topic 5's cross-check is shrinker
// OUTPUT, not language. Every ecosystem here can generate random inputs; they
// differ almost entirely in what they hand you AFTER the failure.
//
// WHAT TO LOOK FOR: `go test -run TestPagination -v` prints rapid's shrunk
// counterexample. Compare it with Hypothesis's and fast-check's. It is the
// cheapest way to learn what "integrated shrinking" buys, and it inoculates you
// against assuming every language's tool behaves like Hypothesis.
package gorapid

import "sort"

// Row mirrors app.core.pagination.Row. CreatedAt is NOT unique; ID is.
type Row struct {
	CreatedAt int
	ID        int
}

// Page mirrors page(): WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit
//
// PRECONDITION: rows sorted by CreatedAt descending. A caller that passes an
// unsorted slice is testing a function that does not exist.
func Page(rows []Row, cursor *int, limit int) ([]Row, *int) {
	if limit < 1 {
		panic("limit must be >= 1")
	}
	filtered := rows
	if cursor != nil {
		filtered = nil
		for _, r := range rows {
			if r.CreatedAt < *cursor { // <-- the bug: strict, on a non-unique column
				filtered = append(filtered, r)
			}
		}
	}
	n := limit
	if len(filtered) < n {
		n = len(filtered)
	}
	out := append([]Row(nil), filtered[:n]...)
	if len(out) == limit {
		next := out[len(out)-1].CreatedAt
		return out, &next
	}
	return out, nil
}

// PageComposite is the real fix: bound on the whole sort key, not its first column.
func PageComposite(rows []Row, before *Row, limit int) ([]Row, *Row) {
	if limit < 1 {
		panic("limit must be >= 1")
	}
	filtered := rows
	if before != nil {
		filtered = nil
		for _, r := range rows {
			if r.CreatedAt < before.CreatedAt ||
				(r.CreatedAt == before.CreatedAt && r.ID < before.ID) {
				filtered = append(filtered, r)
			}
		}
	}
	n := limit
	if len(filtered) < n {
		n = len(filtered)
	}
	out := append([]Row(nil), filtered[:n]...)
	if len(out) == limit {
		last := out[len(out)-1]
		return out, &last
	}
	return out, nil
}

// SortDesc establishes Page's precondition.
func SortDesc(rows []Row) []Row {
	out := append([]Row(nil), rows...)
	sort.SliceStable(out, func(i, j int) bool { return out[i].CreatedAt > out[j].CreatedAt })
	return out
}

// SortDescComposite establishes PageComposite's stronger precondition.
func SortDescComposite(rows []Row) []Row {
	out := append([]Row(nil), rows...)
	sort.Slice(out, func(i, j int) bool {
		if out[i].CreatedAt != out[j].CreatedAt {
			return out[i].CreatedAt > out[j].CreatedAt
		}
		return out[i].ID > out[j].ID
	})
	return out
}
