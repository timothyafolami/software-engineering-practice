// Topic 5 cross-check: the same property, in rapid.
//
//   go test -run TestPagination -v
//
// rapid is Go's Hypothesis-shaped library: generators compose, shrinking is
// integrated, and a failure prints the shrunk input plus the exact command to
// replay it. `testing/quick` in the standard library is frozen and has NO
// shrinking -- do not use it for this.
package gorapid

import (
	"testing"

	"pgregory.net/rapid"
)

// rowsGen mirrors the Hypothesis strategy exactly, including the two decisions
// that carry the whole topic: ties are made LIKELY (CreatedAt drawn from 0..3),
// and the precondition is established by MAPPING rather than by filtering.
func rowsGen(t *rapid.T) []Row {
	n := rapid.IntRange(0, 8).Draw(t, "n")
	seen := map[int]bool{}
	var rows []Row
	for i := 0; i < n; i++ {
		id := rapid.IntRange(0, 50).Draw(t, "id")
		if seen[id] {
			continue // unique_by ID, NOT by CreatedAt -- making CreatedAt unique
		}            // would delete the bug from the input space, not from the code
		seen[id] = true
		rows = append(rows, Row{
			CreatedAt: rapid.IntRange(0, 3).Draw(t, "created_at"),
			ID:        id,
		})
	}
	return SortDesc(rows)
}

func walk(rows []Row, limit int, composite bool) []Row {
	var seen []Row
	if composite {
		var before *Row
		for i := 0; i < 1000; i++ {
			out, next := PageComposite(rows, before, limit)
			seen = append(seen, out...)
			if next == nil {
				return seen
			}
			before = next
		}
	} else {
		var cursor *int
		for i := 0; i < 1000; i++ {
			out, next := Page(rows, cursor, limit)
			seen = append(seen, out...)
			if next == nil {
				return seen
			}
			cursor = next
		}
	}
	panic("walk did not terminate")
}

func assertExactlyOnce(t *rapid.T, rows, seen []Row) {
	want := map[int]int{}
	for _, r := range rows {
		want[r.ID]++
	}
	got := map[int]int{}
	for _, r := range seen {
		got[r.ID]++
	}
	for id, n := range want {
		if got[id] != n {
			t.Fatalf("row id=%d appeared %d times, want %d (input=%v)", id, got[id], n, rows)
		}
	}
	for id, n := range got {
		if want[id] != n {
			t.Fatalf("row id=%d served %d times but was not in the input", id, n)
		}
	}
}

// TestPagination FAILS. Read what rapid prints and compare it with Hypothesis's
// and fast-check's shrunk output -- that comparison is the point of the probe.
func TestPagination(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		rows := rowsGen(t)
		limit := rapid.IntRange(1, 5).Draw(t, "limit")
		assertExactlyOnce(t, rows, walk(rows, limit, false))
	})
}

// TestPaginationComposite PASSES: the fix, under the same tie-heavy generator.
func TestPaginationComposite(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		n := rapid.IntRange(0, 8).Draw(t, "n")
		seen := map[int]bool{}
		var rows []Row
		for i := 0; i < n; i++ {
			id := rapid.IntRange(0, 50).Draw(t, "id")
			if seen[id] {
				continue
			}
			seen[id] = true
			rows = append(rows, Row{CreatedAt: rapid.IntRange(0, 3).Draw(t, "created_at"), ID: id})
		}
		rows = SortDescComposite(rows)
		limit := rapid.IntRange(1, 5).Draw(t, "limit")
		assertExactlyOnce(t, rows, walk(rows, limit, true))
	})
}

// TestPaginationWideRange is the probe's most useful surprise, and it is the
// reason to run a cross-check at all instead of assuming.
//
// In Hypothesis, widening the timestamp range to something datetime-sized makes
// ties astronomically unlikely and the property passes forever while the bug
// stays. Port the SAME widening to rapid and it STILL FAILS -- because rapid's
// IntRange biases hard toward boundary values and draws 0 constantly, so ties
// appear within a few dozen tests regardless of how wide you declared the range.
//
// Observed here: fails after ~15 tests, and shrinks to a two-row minimum --
// which is a SMALLER counterexample than the narrow generator above produced.
// Record what your run prints rather than what this comment says; the point is
// that "same declared range" does not mean "same distribution", and a property
// is only as good as the probability that ITS TOOL produces the shape the bug
// needs.
func TestPaginationWideRange(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		n := rapid.IntRange(0, 8).Draw(t, "n")
		seen := map[int]bool{}
		var rows []Row
		for i := 0; i < n; i++ {
			id := rapid.IntRange(0, 50).Draw(t, "id")
			if seen[id] {
				continue
			}
			seen[id] = true
			rows = append(rows, Row{
				CreatedAt: rapid.IntRange(0, 1<<40).Draw(t, "created_at"),
				ID:        id,
			})
		}
		rows = SortDesc(rows)
		limit := rapid.IntRange(1, 5).Draw(t, "limit")
		assertExactlyOnce(t, rows, walk(rows, limit, false))
	})
}

// FuzzPage is Go's OTHER answer, and a genuinely different technique:
// coverage-guided mutation of a seed corpus rather than sampling a declared
// distribution. Better than a property library at finding parser crashes on
// bytes, worse at expressing a structured invariant over typed records -- which
// is exactly what you can see by comparing this with TestPagination above.
//
//	go test -fuzz FuzzPage -fuzztime 30s
func FuzzPage(f *testing.F) {
	f.Add([]byte{0, 0, 0, 1}, 1)   // the known counterexample, as a seed
	f.Fuzz(func(t *testing.T, data []byte, limit int) {
		if limit < 1 || limit > 5 || len(data) > 16 {
			t.Skip()
		}
		var rows []Row
		for i, b := range data {
			rows = append(rows, Row{CreatedAt: int(b) % 4, ID: i})
		}
		rows = SortDesc(rows)
		want := len(rows)
		got := len(walk(rows, limit, false))
		if got != want {
			t.Fatalf("walk yielded %d rows from %d inputs: %v", got, want, rows)
		}
	})
}
