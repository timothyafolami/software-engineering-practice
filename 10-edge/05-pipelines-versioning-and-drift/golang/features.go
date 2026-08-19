// Layer 10 - Topic 5: how training/serving skew is actually born. (Go)
//
// What this demonstrates
//
//	This file is the bug, written on purpose and written the way it
//	really happens. An ingest service needs the same 7-day features the
//	Python pipeline computes. The Python module is not callable from
//	here, and reimplementing forty lines looked cheaper than standing up
//	an RPC. Nobody was careless. Three decisions simply got made again,
//	independently, and two of them came out differently:
//
//	  window boundary  Python's spec is half-open, [T-7d, T). This file
//	                   does (T-7d, T] -- exclusive lower, inclusive
//	                   upper -- which is the other obvious reading of
//	                   "the last seven days" and reads fine in review.
//	  rounding         math.Round is half-away-from-zero. Python's
//	                   round() and the spec are half-to-even. Nothing in
//	                   either language's documentation suggests the
//	                   other one is the default anywhere else.
//	  empty window     returns recency 0, because zero is what a Go
//	                   struct field starts as and no case in this
//	                   function ever assigns -1.
//
//	Then `-mode conform` implements the written spec exactly, and the
//	diff goes to zero. That is the evidence for the actual fix: the
//	transform gets ONE HOME and everything else calls it across a
//	boundary. A second correct copy is not a fix, it is a second thing
//	to keep correct.
//
// What to look for
//   - Run both modes and diff them against the Python output with
//     python3 python/three_way_diff.py. `native` disagrees on a
//     meaningful fraction of users; `conform` on none.
//   - The three causes are separable, and the diff tool attributes each
//     differing row to one of them. "It's a float thing" is not a
//     diagnosis.
//
// No dependencies. Reads the same events.csv every implementation reads:
//
//	cd golang && go run features.go
//	cd golang && go run features.go -mode conform -out ../data/features_go_conform.csv
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
)

const (
	dayMs    = 86_400_000
	windowMs = 7 * dayMs
	asOfMs   = 1_785_542_400_000 // 2026-08-01T00:00:00Z
)

type event struct {
	userID int
	tsMs   int64
	amount int64
}

type features struct {
	userID      int
	spend       int64
	count       int64
	avgCents    float64
	recencyDays int64
}

// roundHalfEven is what the spec asks for, and what Python's round() does.
// It has to be written out here because Go's math package does not ship it:
// math.Round is half-away-from-zero and math.RoundToEven only works on
// whole numbers, not on a chosen number of decimal places.
func roundHalfEven(v float64, places int) float64 {
	scale := math.Pow(10, float64(places))
	return math.RoundToEven(v*scale) / scale
}

// roundNative is what an unhurried Go developer reaches for.
func roundNative(v float64, places int) float64 {
	scale := math.Pow(10, float64(places))
	return math.Round(v*scale) / scale
}

func compute(events []event, asOf int64, conform bool) []features {
	byUser := map[int][]event{}
	for _, e := range events {
		byUser[e.userID] = append(byUser[e.userID], e)
	}
	ids := make([]int, 0, len(byUser))
	for id := range byUser {
		ids = append(ids, id)
	}
	sort.Ints(ids)

	lower := asOf - windowMs
	out := make([]features, 0, len(ids))
	for _, id := range ids {
		var spend, count, latest int64
		latest = -1
		for _, e := range byUser[id] {
			var inWindow bool
			if conform {
				// The spec: lower bound inclusive, upper exclusive.
				inWindow = e.tsMs >= lower && e.tsMs < asOf
			} else {
				// The other obvious reading of "the last seven days".
				inWindow = e.tsMs > lower && e.tsMs <= asOf
			}
			if !inWindow {
				continue
			}
			spend += e.amount
			count++
			if e.tsMs > latest {
				latest = e.tsMs
			}
		}
		f := features{userID: id, spend: spend, count: count}
		if count == 0 {
			f.avgCents = 0
			if conform {
				f.recencyDays = -1 // the spec
			} else {
				f.recencyDays = 0 // the zero value, never assigned otherwise
			}
		} else {
			avg := float64(spend) / float64(count)
			if conform {
				f.avgCents = roundHalfEven(avg, 2)
			} else {
				f.avgCents = roundNative(avg, 2)
			}
			f.recencyDays = (asOf - latest) / dayMs
		}
		out = append(out, f)
	}
	return out
}

func loadEvents(path string) ([]event, error) {
	fh, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer fh.Close()
	r := csv.NewReader(fh)
	rows, err := r.ReadAll()
	if err != nil {
		return nil, err
	}
	out := make([]event, 0, len(rows))
	for i, row := range rows {
		if i == 0 {
			continue // header
		}
		uid, _ := strconv.Atoi(row[0])
		ts, _ := strconv.ParseInt(row[1], 10, 64)
		amt, _ := strconv.ParseInt(row[2], 10, 64)
		out = append(out, event{uid, ts, amt})
	}
	return out, nil
}

func main() {
	in := flag.String("in", "../data/events.csv", "event log")
	out := flag.String("out", "../data/features_go_native.csv", "output")
	mode := flag.String("mode", "native", "native | conform")
	flag.Parse()
	conform := *mode == "conform"

	events, err := loadEvents(*in)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot read %s: %v\n", *in, err)
		fmt.Fprintln(os.Stderr, "run python3 python/seed_events.py first")
		os.Exit(1)
	}

	rows := compute(events, asOfMs, conform)

	fh, err := os.Create(*out)
	if err != nil {
		panic(err)
	}
	defer fh.Close()
	w := csv.NewWriter(fh)
	defer w.Flush()
	w.Write([]string{"user_id", "spend_7d", "txn_count_7d", "avg_amount_7d", "recency_days"})
	for _, f := range rows {
		w.Write([]string{
			strconv.Itoa(f.userID),
			strconv.FormatInt(f.spend, 10),
			strconv.FormatInt(f.count, 10),
			strconv.FormatFloat(f.avgCents, 'f', 2, 64),
			strconv.FormatInt(f.recencyDays, 10),
		})
	}

	fmt.Printf("Go feature implementation (%s mode)\n", *mode)
	fmt.Printf("  input  : %s (%d events)\n", *in, len(events))
	fmt.Printf("  output : %s (%d users)\n", *out, len(rows))
	if conform {
		fmt.Println("\n  This mode implements python/features.py's docstring exactly:")
		fmt.Println("  half-open window, round-half-to-even, recency -1 when empty.")
		fmt.Println("  The three-way diff against Python should be zero rows.")
	} else {
		fmt.Println("\n  This mode is the bug, written the way it really happens:")
		fmt.Println("    window        (T-7d, T]  instead of [T-7d, T)")
		fmt.Println("    rounding      math.Round (half away from zero)")
		fmt.Println("    empty window  recency 0 (the zero value) instead of -1")
		fmt.Println("  Every one of those is defensible in isolation, and none of")
		fmt.Println("  them would be caught by a code review of this file alone.")
	}
	fmt.Println("\n  Next: python3 python/three_way_diff.py")
}
