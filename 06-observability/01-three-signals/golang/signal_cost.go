// Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.
//
// Why Go: it is the only runtime in this lab that will tell you, from inside
// the program and without a profiler, how many heap allocations each of these
// operations cost. That matters here because the cost of telemetry in a
// garbage-collected service is not really the nanoseconds -- it is the
// allocation rate, which becomes GC pressure, which becomes a p99 that moves
// for reasons the request path cannot explain. So this file reports B/op and
// allocs/op next to ns/op, and those two extra columns are the whole reason Go
// is in this topic.
//
// Go is also the only one of the six whose standard library ships a structured
// logger (log/slog, Go 1.21+) with a first-class Enabled() check, and with two
// call shapes -- ...any and LogAttrs -- that produce byte-identical output by
// different allocation routes. Both are below; compare them yourself.
//
// What this demonstrates
// ----------------------
//  1. counter add       - map lookup on a bounded label key + increment
//  2. span record       - struct allocation, timestamps, six attributes
//  3. log line (INFO)   - slog JSON handler to a counting sink
//  4. debug, DISABLED, arguments built eagerly       <- the bug
//  5. debug, DISABLED, guarded by logger.Enabled     <- the fix
//  6. log INFO via LogAttrs (no []any boxing)        <- the allocation fix
//
// Operations 4 and 5 emit nothing at all. Compare their allocs/op: the
// disabled call in row 4 still allocates, because Go boxes every variadic
// ...any argument into a slice at the call site, before slog can decide the
// level is off.
//
// This measures the shape of the cost with a hand-rolled metric and span
// store, not the OpenTelemetry SDK. A real SDK adds work; it never subtracts.
//
// What to look for in the output
// ------------------------------
//   - allocs/op on rows 4 and 5. Row 5 should be zero.
//   - rows 3 and 6: same log line, same bytes out. Are the allocations the
//     same too? The answer on this machine is in the output, not in the advice.
//   - the ns/op column against the Python and Node runs of this same file.
//
// Run:  go run signal_cost.go
package main

import (
	"context"
	"fmt"
	"log/slog"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

const iterations = 200_000

// Printed at the end so the compiler cannot conclude the work below is dead.
var sink atomic.Int64

type counterStore struct {
	series map[string]int64
}

func (c *counterStore) add(key string) {
	c.series[key]++
}

type span struct {
	name       string
	traceID    string
	spanID     string
	attributes map[string]any
	startNs    int64
	endNs      int64
}

// countingSink stands in for the pipe to the log shipper: counts bytes,
// discards them, so we are not benchmarking a terminal.
type countingSink struct {
	bytes int64
	lines int64
}

func (s *countingSink) Write(p []byte) (int, error) {
	s.bytes += int64(len(p))
	s.lines++
	return len(p), nil
}

type result struct {
	label    string
	nsPerOp  float64
	bytesOp  float64
	allocsOp float64
}

func bench(label string, fn func()) result {
	for i := 0; i < 1000; i++ { // warm caches and grow any maps once
		fn()
	}
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	start := time.Now()
	for i := 0; i < iterations; i++ {
		fn()
	}
	elapsed := time.Since(start)
	runtime.ReadMemStats(&after)
	return result{
		label:    label,
		nsPerOp:  float64(elapsed.Nanoseconds()) / iterations,
		bytesOp:  float64(after.TotalAlloc-before.TotalAlloc) / iterations,
		allocsOp: float64(after.Mallocs-before.Mallocs) / iterations,
	}
}

func main() {
	sinkStream := &countingSink{}
	// INFO, so every Debug call below is disabled -- the production config in
	// which the eager-argument bug hides.
	logger := slog.New(slog.NewJSONHandler(sinkStream, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	counter := &counterStore{series: make(map[string]int64)}
	ctx := context.Background()

	orderPayload := `{"order_id":"ord_8f31c2","customer_id":"cus_00194",` +
		`"items":[{"sku":"SKU-1","qty":2},{"sku":"SKU-7","qty":1}],` +
		`"discount":0.15,"currency":"GBP"}`
	labelKey := "GET|/orders/{id}|200"
	attributes := map[string]any{
		"http.request.method":       "GET",
		"http.route":                "/orders/{id}",
		"http.response.status_code": 200,
		"db.system.name":            "postgresql",
		"customer.id":               "cus_00194",
		"order.id":                  "ord_8f31c2",
	}

	// expensiveArgument stands in for the json.Marshal / fmt.Sprintf that a
	// real debug line calls on its way into the logger.
	expensiveArgument := func() string {
		return "pricing payload=" + strings.ToUpper(orderPayload)
	}

	results := []result{
		bench("counter.add (3 bounded labels)", func() {
			counter.add(labelKey)
		}),
		bench("span create + end (6 attrs)", func() {
			s := &span{
				name:       "GET /orders/{id}",
				traceID:    "4bf92f3577b34da6a3ce929d0e0e4736",
				spanID:     "00f067aa0ba902b7",
				attributes: attributes,
				startNs:    time.Now().UnixNano(),
			}
			s.endNs = time.Now().UnixNano()
			sink.Add(s.endNs - s.startNs)
		}),
		bench("log INFO, slog JSON, ...any args", func() {
			logger.Info("order priced",
				"order_id", "ord_8f31c2",
				"customer_id", "cus_00194",
				"duration_ms", 12.4)
		}),
		bench("log DEBUG (disabled), eager argument", func() {
			// THE BUG. The handler is at INFO so nothing is written, but
			// expensiveArgument() runs first -- Go evaluates arguments at the
			// call site -- and the variadic ...any boxes them into a heap
			// slice before slog gets a chance to say "level off".
			logger.Debug("pricing", "payload", expensiveArgument())
		}),
		bench("log DEBUG (disabled), Enabled() guard", func() {
			// THE FIX. Costs one interface call and a comparison.
			if logger.Enabled(ctx, slog.LevelDebug) {
				logger.Debug("pricing", "payload", expensiveArgument())
			}
		}),
		bench("log INFO via LogAttrs (typed, no boxing)", func() {
			// Same output as row 3. slog.Attr is a typed value, so nothing is
			// boxed into interface{} and nothing escapes to the heap for the
			// argument list itself.
			logger.LogAttrs(ctx, slog.LevelInfo, "order priced",
				slog.String("order_id", "ord_8f31c2"),
				slog.String("customer_id", "cus_00194"),
				slog.Float64("duration_ms", 12.4))
		}),
	}

	bar := strings.Repeat("=", 74)
	fmt.Println(bar)
	fmt.Printf("COST OF EMITTING ONE UNIT OF TELEMETRY   (%s, n=%d)\n",
		runtime.Version(), iterations)
	fmt.Println(bar)
	fmt.Printf("%-42s %10s %9s %10s\n", "operation", "ns/op", "B/op", "allocs/op")
	for _, r := range results {
		fmt.Printf("%-42s %10.0f %9.0f %10.2f\n", r.label, r.nsPerOp, r.bytesOp, r.allocsOp)
	}

	eager, guarded := results[3], results[4]
	fmt.Printf("\nRows 4 and 5 both emit nothing at all.\n")
	fmt.Printf("  row 4 costs %.0f ns and %.2f allocations per call\n", eager.nsPerOp, eager.allocsOp)
	fmt.Printf("  row 5 costs %.0f ns and %.2f allocations per call\n", guarded.nsPerOp, guarded.allocsOp)
	fmt.Printf("At 8 disabled debug calls per request and 1000 req/s, row 4's shape\n")
	fmt.Printf("allocates %.0f bytes/second that the GC must then collect, for no output.\n",
		eager.bytesOp*8*1000)

	fmt.Printf("\nRows 3 and 6 write the identical JSON line by two different call\n")
	fmt.Printf("shapes. Read them against each other rather than against a claim:\n")
	fmt.Printf("  ...any args : %6.0f ns/op  %5.0f B/op  %.2f allocs/op\n",
		results[2].nsPerOp, results[2].bytesOp, results[2].allocsOp)
	fmt.Printf("  LogAttrs    : %6.0f ns/op  %5.0f B/op  %.2f allocs/op\n",
		results[5].nsPerOp, results[5].bytesOp, results[5].allocsOp)
	fmt.Printf("LogAttrs exists so the argument list is not boxed into interface{}.\n")
	fmt.Printf("Whether that shows up as a delta here depends on whether escape\n")
	fmt.Printf("analysis had already kept the ...any slice on the stack -- which is\n")
	fmt.Printf("the honest lesson: the optimisation is real, and it is not always the\n")
	fmt.Printf("one your benchmark is measuring. Compare allocs/op, not the advice.\n")

	fmt.Printf("\nRead row 2 sceptically. %.2f allocs/op for a struct with a map field\n",
		results[1].allocsOp)
	fmt.Printf("means escape analysis proved the span never leaves this function and\n")
	fmt.Printf("put it on the stack. In a real SDK the span is handed to an exporter,\n")
	fmt.Printf("so it escapes and is heap-allocated. Row 2 is therefore a floor, not\n")
	fmt.Printf("an estimate -- a difference between the experiment and production, not\n")
	fmt.Printf("a result about production.\n")

	fmt.Printf("\nBytes written by the INFO logs: %d over %d lines (%.0f B/line).\n",
		sinkStream.bytes, sinkStream.lines, float64(sinkStream.bytes)/float64(sinkStream.lines))
	fmt.Printf("(sink=%d, printed so nothing above can be optimised away)\n", sink.Load())
}
