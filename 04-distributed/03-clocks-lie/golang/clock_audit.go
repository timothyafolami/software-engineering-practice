// Layer 4 Topic 3 (Part A) -- Go's clocks, audited rather than assumed.
//
// WHAT THIS DEMONSTRATES: four things, in order.
//  1. the clock inventory. Go hides the distinction on purpose: a time.Time
//     carries BOTH a wall reading and a monotonic reading, and t2.Sub(t1)
//     silently uses the monotonic one. Idiomatic Go is correct here without the
//     author knowing why -- which is exactly what makes item 3 dangerous.
//  2. one span timed twice, through the application's own now() (wall) and
//     through the monotonic reading, with an NTP-style step inside two spans.
//  3. the footgun specific to this runtime: the monotonic reading is STRIPPED by
//     t.Round, t.Truncate, t.UTC, t.Local, t.AddDate, JSON marshal/unmarshal and
//     anything from time.Parse. A duration computed after any of those falls back
//     to the wall clock silently and can come out negative. This program shows
//     each stripper, and then computes the same span both ways so the two paths
//     visibly diverge.
//  4. the summary line for the README's record table.
//
// WHAT TO LOOK FOR IN THE OUTPUT: section 3's table of strippers -- the "m=+..."
// suffix in a printed time.Time is the monotonic reading, and its disappearance
// is the whole bug. Then the last two lines of section 3, where the same span
// measured through a round-tripped Time comes out NEGATIVE while the untouched
// one is correct.
//
//	cd golang && go run clock_audit.go
package main

import (
	"encoding/json"
	"fmt"
	"runtime"
	"sort"
	"time"
)

const (
	stepBack  = -40 * time.Second // an NTP correction, applied backwards, mid-run
	spans     = 400
	spanWorkU = 200 * time.Microsecond
)

// ------------------------------------------------------------- 1. inventory

// measureResolution returns the smallest non-zero delta this clock reports.
// Measured, not documented: a clock can advertise nanoseconds and tick in
// microseconds, and on Darwin several of them do.
func measureResolution(read func() int64, trials int) int64 {
	smallest := int64(1 << 62)
	for i := 0; i < trials; i++ {
		a := read()
		for {
			b := read()
			if b != a {
				d := b - a
				if d < 0 {
					d = -d
				}
				if d < smallest {
					smallest = d
				}
				break
			}
		}
	}
	return smallest
}

func inventory() {
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("1. the clocks Go offers, and the one it hides")
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Printf("  %-40s%-12s%s\n", "expression", "kind", "measured resolution")

	start := time.Now()
	rows := []struct {
		name string
		kind string
		read func() int64
	}{
		{"time.Now().UnixNano()", "realtime", func() int64 { return time.Now().UnixNano() }},
		{"time.Since(start) [monotonic]", "monotonic", func() int64 { return int64(time.Since(start)) }},
		{"time.Now().UTC().UnixNano()", "realtime", func() int64 { return time.Now().UTC().UnixNano() }},
		{"runtime.nanotime via time.Since", "monotonic", func() int64 { return int64(time.Since(start)) }},
	}
	for _, r := range rows {
		fmt.Printf("  %-40s%-12s%12d ns\n", r.name, r.kind, measureResolution(r.read, 20))
	}

	now := time.Now()
	fmt.Println()
	fmt.Printf("  time.Now()          %v\n", now)
	fmt.Printf("  time.Now().UTC()    %v\n", now.UTC())
	fmt.Println("  ^ the 'm=+0.000...' suffix on the first one IS the monotonic reading.")
	fmt.Println("    It is not decoration. Its absence on the second line is section 3.")
}

// ------------------------------------------------- 2. one span, two clocks

// appClock is the application's own now(). Every service has one; most read the
// wall clock. The offset stands in for an NTP step -- we never touch the system
// clock, and lab/README.md explains why per-container skew is not possible here.
type appClock struct{ offset time.Duration }

func (c *appClock) now() time.Time       { return time.Now().Add(c.offset) }
func (c *appClock) step(d time.Duration) { c.offset += d }

func burn(d time.Duration) {
	end := time.Now().Add(d)
	for time.Now().Before(end) {
	}
}

func spanComparison(c *appClock) (wall, mono []float64) {
	// Fixed indices rather than a timer: a timer racing an 80ms loop is how you
	// get a run where the step lands between spans and the experiment silently
	// proves nothing -- which the README lists as a broken experiment, not a
	// wrong prediction.
	stepBackAt, stepFwdAt := spans/3, 2*spans/3
	for i := 0; i < spans; i++ {
		w0 := c.now()
		m0 := time.Now()
		burn(spanWorkU)
		if i == stepBackAt {
			c.step(stepBack)
		} else if i == stepFwdAt {
			c.step(-stepBack)
		}
		// w1.Sub(w0) would still use the monotonic readings both Times carry, so
		// the offset would be invisible. Going through UnixNano() is what an
		// application does when it stores or ships a timestamp -- and it is the
		// only honest way to model a service whose now() really is wall time.
		wall = append(wall, float64(c.now().UnixNano()-w0.UnixNano())/1e6)
		mono = append(mono, float64(time.Since(m0))/1e6)
	}
	return wall, mono
}

func pct(v []float64, q float64) float64 {
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	i := int(q*float64(len(s))+0.5) - 1
	if i < 0 {
		i = 0
	}
	if i >= len(s) {
		i = len(s) - 1
	}
	return s[i]
}

func minMax(v []float64) (float64, float64) {
	lo, hi := v[0], v[0]
	for _, x := range v {
		if x < lo {
			lo = x
		}
		if x > hi {
			hi = x
		}
	}
	return lo, hi
}

func spanReport(wall, mono []float64) int {
	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Printf("2. %d identical spans, timed twice, with a %v step and a %v step\n",
		spans, stepBack, -stepBack)
	fmt.Println("   landing INSIDE two of them")
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Printf("  %-30s%10s%12s%14s%14s%10s\n", "clock", "p50", "p99", "max", "min", "negative")
	negatives := 0
	for _, row := range []struct {
		name string
		v    []float64
	}{{"wall (app now().UnixNano)", wall}, {"monotonic (time.Since)", mono}} {
		neg := 0
		for _, x := range row.v {
			if x < 0 {
				neg++
			}
		}
		if row.name[0] == 'w' {
			negatives = neg
		}
		lo, hi := minMax(row.v)
		fmt.Printf("  %-30s%10.3f%12.3f%14.1f%14.1f%10d\n",
			row.name, pct(row.v, 0.50), pct(row.v, 0.99), hi, lo, neg)
	}
	fmt.Println("  (milliseconds; 'negative' counts spans that finished before they started)")

	hot := 0
	for i, x := range wall {
		if x > wall[hot] {
			hot = i
		}
	}
	lo := hot - 19
	if lo < 0 {
		lo = 0
	}
	hi := hot + 21
	if hi > len(wall) {
		hi = len(wall)
	}
	wlo, whi := minMax(wall)
	fmt.Println()
	fmt.Printf("  Two samples out of %d were touched: %.0f ms and %.0f ms, against a p50\n",
		spans, wlo, whi)
	fmt.Printf("  of %.3f ms. Over all %d spans that is only the max -- one sample in %d\n",
		pct(wall, 0.50), spans, spans)
	fmt.Println("  cannot move a p99 by rank. But dashboards aggregate windows, not runs:")
	fmt.Printf("  over the %d spans around the step the wall-clock p99 is %.1f ms against\n",
		hi-lo, pct(wall[lo:hi], 0.99))
	fmt.Printf("  a monotonic p99 of %.3f ms. Only the clock differed.\n", pct(mono[lo:hi], 0.99))
	return negatives
}

// ------------------------------------------------------- 3. the Go footgun

func footguns() bool {
	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("3. the footgun specific to this runtime: the stripped monotonic reading")
	fmt.Println("------------------------------------------------------------------------------")

	base := time.Now()
	var roundTripped time.Time
	if b, err := json.Marshal(base); err == nil {
		_ = json.Unmarshal(b, &roundTripped)
	}
	parsed, _ := time.Parse(time.RFC3339Nano, base.Format(time.RFC3339Nano))

	strippers := []struct {
		op string
		t  time.Time
	}{
		{"time.Now()            (untouched)", base},
		{"t.Round(0)", base.Round(0)},
		{"t.Truncate(time.Second)", base.Truncate(time.Second)},
		{"t.UTC()", base.UTC()},
		{"t.Local()", base.Local()},
		{"t.AddDate(0, 0, 0)", base.AddDate(0, 0, 0)},
		{"t.Add(time.Second)   (KEEPS it)", base.Add(time.Second)},
		{"JSON marshal + unmarshal", roundTripped},
		{"time.Parse(RFC3339Nano)", parsed},
	}
	fmt.Printf("  %-38s%-12s%s\n", "operation", "monotonic?", "printed form")
	for _, s := range strippers {
		// The only public way to ask is to print it: the "m=+..." suffix appears
		// exactly when a monotonic reading is present. There is no API for this,
		// which is a large part of why the bug survives code review.
		text := s.t.String()
		has := "STRIPPED"
		if idx := indexOf(text, " m=+"); idx >= 0 {
			has = "kept"
			text = text[:idx] + " " + text[idx+1:]
		}
		if len(text) > 34 {
			text = text[:34]
		}
		fmt.Printf("  %-38s%-12s%s\n", s.op, has, text)
	}

	// Now the bug itself. Same span, two ways: one keeps its monotonic readings,
	// the other round-trips the start through .UTC() the way any code that logs,
	// serialises or stores a timestamp would.
	fmt.Println()
	start := time.Now()
	stripped := start.UTC()
	burn(2 * time.Millisecond)
	end := time.Now()

	good := end.Sub(start)
	bad := end.Sub(stripped)
	fmt.Printf("  end.Sub(start)          %v      [monotonic path -- correct]\n", good)
	fmt.Printf("  end.Sub(start.UTC())    %v      [wall path -- same instants!]\n", bad)
	fmt.Println("  Both operands describe the same two moments. One call to .UTC() moved")
	fmt.Println("  the subtraction onto the wall clock, and nothing anywhere said so.")

	// And with a wall clock that has stepped, the wall path goes negative while
	// the monotonic path is untouched. We cannot step the real clock, so we do
	// what a stepped clock does to the arithmetic: subtract Times whose wall
	// readings disagree with their ordering.
	fmt.Println()
	c := &appClock{}
	w0 := c.now().UTC() // stripped: only the wall reading survives
	burn(2 * time.Millisecond)
	c.step(stepBack) // NTP decides we are ahead and jumps back
	w1 := c.now().UTC()
	fmt.Printf("  with a %v step inside the span:\n", stepBack)
	fmt.Printf("    w1.Sub(w0) on stripped Times   %v   <-- NEGATIVE DURATION\n", w1.Sub(w0))
	m0 := time.Now()
	burn(2 * time.Millisecond)
	fmt.Printf("    time.Since(m0), monotonic      %v\n", time.Since(m0))
	fmt.Println("  A negative time.Duration is a legal value in Go. It will divide, average")
	fmt.Println("  and export to your metrics pipeline without complaint.")
	return w1.Sub(w0) < 0
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func main() {
	fmt.Println("==============================================================================")
	fmt.Println("Layer 4 Topic 3 -- Go clock audit")
	fmt.Println("==============================================================================")
	fmt.Printf("  %s on %s/%s\n\n", runtime.Version(), runtime.GOOS, runtime.GOARCH)

	inventory()
	c := &appClock{}
	wall, mono := spanComparison(c)
	negatives := spanReport(wall, mono)
	reproduced := footguns()

	fmt.Println()
	fmt.Println("------------------------------------------------------------------------------")
	fmt.Println("4. one line for the record table in the README")
	fmt.Println("------------------------------------------------------------------------------")
	start := time.Now()
	res := measureResolution(func() int64 { return int64(time.Since(start)) }, 20)
	verdict := "NO -- investigate"
	if reproduced {
		verdict = "yes"
	}
	plural := "s"
	if negatives == 1 {
		plural = ""
	}
	fmt.Printf("  | Go | time.Time's hidden monotonic reading | %d ns | %s (%d negative wall-clock span%s) |\n",
		res, verdict, negatives, plural)
	fmt.Println()
	fmt.Println("  The table in the README stays blank until you fill it in. This line is")
	fmt.Println("  the measurement, not the answer -- copy it across yourself.")
}
