// Layer 6 Topic 3 - Losing trace context at a Go concurrency boundary.
//
// What this demonstrates
// ----------------------
// Go refuses to guess. There is no ambient context: `context.Context` is a
// parameter, so nothing is ever lost silently. A function that calls
// `context.Background()` instead of accepting the caller's ctx starts a brand
// new trace -- and that is a bug you can see in a diff, not a mystery at 3am.
//
// So the interesting Go output is not "did it break" (it breaks in exactly the
// place you can point at) but WHERE the break is visible. Each block below
// prints the caller's trace ID, the callee's under the naive call, and the
// callee's when ctx is threaded through. The last column of the summary is the
// one that matters: for every boundary, Go tells you at the call site.
//
// The goroutine block is the one people expect to fail and it does not: a
// goroutine closes over ctx like any other value, so `go work(ctx)` is
// correct by construction. What Go cannot help with is the same thing nothing
// can help with -- a queue, where the context has to be serialised into the
// message body as a W3C `traceparent`.
//
// No OpenTelemetry SDK is imported (none is installed here). The span, the
// context key and the traceparent codec are about 40 lines of standard
// library, which is the point: this is a language property, not an SDK one.
//
// What to look for in the output
// ------------------------------
// Four blocks in the shared shape:
//
//	caller trace_id   <id>
//	callee trace_id   <id or "none">   naive
//	callee trace_id   <id>             propagated
//	verdict           lost | preserved
//
// Then the summary's "fails loudly?" column, which is the whole argument for
// Go's ergonomic trade: every loss here is a visible `context.Background()` or
// `context.TODO()` at a call site, and `go vet`'s lostcancel plus a linter
// like contextcheck will find them mechanically.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"runtime"
	"strconv"
	"strings"
	"sync"
)

// ---------------------------------------------------------------------------
// A minimal span, a context key, and the W3C traceparent codec.
// ---------------------------------------------------------------------------

type Span struct {
	Name    string
	TraceID string
	SpanID  string
	Sampled bool
}

type ctxKey struct{}

func newSpan(name string) *Span {
	traceID := make([]byte, 16)
	spanID := make([]byte, 8)
	_, _ = rand.Read(traceID)
	_, _ = rand.Read(spanID)
	return &Span{
		Name:    name,
		TraceID: hex.EncodeToString(traceID),
		SpanID:  hex.EncodeToString(spanID),
		Sampled: true,
	}
}

func (s *Span) Traceparent() string {
	flags := "00"
	if s.Sampled {
		flags = "01"
	}
	return fmt.Sprintf("00-%s-%s-%s", s.TraceID, s.SpanID, flags)
}

func spanFromTraceparent(header, name string) (*Span, error) {
	parts := strings.Split(header, "-")
	if len(parts) != 4 || parts[0] != "00" || len(parts[1]) != 32 || len(parts[2]) != 16 {
		return nil, fmt.Errorf("malformed traceparent: %q", header)
	}
	flags, err := strconv.ParseInt(parts[3], 16, 32)
	if err != nil {
		return nil, err
	}
	span := newSpan(name)
	span.TraceID = parts[1]
	span.Sampled = flags&1 == 1
	return span, nil
}

func withSpan(ctx context.Context, s *Span) context.Context {
	return context.WithValue(ctx, ctxKey{}, s)
}

// traceIDOf is the only reader in the file. It returns "none" for a context
// that never carried a span -- which is what context.Background() always is.
func traceIDOf(ctx context.Context) string {
	if s, ok := ctx.Value(ctxKey{}).(*Span); ok {
		return s.TraceID
	}
	return "none"
}

// ---------------------------------------------------------------------------
// Structured logging: the log call takes ctx, because in Go everything takes
// ctx. slog's own context-aware form (slog.InfoContext) exists for exactly
// this reason.
// ---------------------------------------------------------------------------

type logRecord struct {
	msg     string
	traceID string
}

var logs []logRecord

func logInfo(ctx context.Context, msg string) {
	id := traceIDOf(ctx)
	if id == "none" {
		id = ""
	}
	logs = append(logs, logRecord{msg: msg, traceID: id})
}

func report(boundary, caller, naive, propagated, note string) string {
	verdict := "lost"
	if naive == caller {
		verdict = "preserved"
	}
	fmt.Printf("boundary          %s\n", boundary)
	fmt.Printf("caller trace_id   %s\n", caller)
	fmt.Printf("callee trace_id   %-32s naive\n", naive)
	fmt.Printf("callee trace_id   %-32s propagated\n", propagated)
	if note != "" {
		fmt.Printf("verdict           %s   (%s)\n\n", verdict, note)
	} else {
		fmt.Printf("verdict           %s\n\n", verdict)
	}
	return verdict
}

// ---------------------------------------------------------------------------
// Boundary 1: a goroutine. The control, and it passes -- a goroutine closes
// over ctx like any other value.
// ---------------------------------------------------------------------------

func boundaryGoroutine() string {
	span := newSpan("GET /orders")
	ctx := withSpan(context.Background(), span)

	var wg sync.WaitGroup
	var observed string
	wg.Add(1)
	go func(ctx context.Context) {
		defer wg.Done()
		observed = traceIDOf(ctx)
	}(ctx)
	wg.Wait()

	return report("go func(ctx)", span.TraceID, observed, observed,
		"ctx is a value; a goroutine capturing it is correct by construction")
}

// ---------------------------------------------------------------------------
// Boundary 2: the actual Go bug. A helper that manufactures its own context
// because it was easier than changing the signature.
// ---------------------------------------------------------------------------

// fetchPricingNaive is the bug, and it is one identifier long. In a review this
// reads as "it needs a context, here is a context".
func fetchPricingNaive() string {
	ctx := context.Background() // <-- the whole defect
	logInfo(ctx, "GET pricing (naive)")
	return traceIDOf(ctx)
}

func fetchPricing(ctx context.Context) string {
	logInfo(ctx, "GET pricing (ctx threaded)")
	return traceIDOf(ctx)
}

func boundaryFreshContext() string {
	span := newSpan("GET /orders")
	ctx := withSpan(context.Background(), span)

	naive := fetchPricingNaive()
	propagated := fetchPricing(ctx)

	return report("helper calls context.Background()", span.TraceID, naive, propagated,
		"the fix is a parameter; the bug is greppable")
}

// ---------------------------------------------------------------------------
// Boundary 3: a worker pool fed by a channel. Whether context survives depends
// entirely on whether the channel's element type has room for it.
// ---------------------------------------------------------------------------

type jobNoCtx struct {
	id string
}

type jobWithCtx struct {
	id  string
	ctx context.Context
}

func boundaryChannel() string {
	span := newSpan("POST /orders")
	ctx := withSpan(context.Background(), span)

	// Naive: the job struct has no room for a context, so there is nowhere for
	// it to go. The worker calls context.Background() because it must.
	naiveCh := make(chan jobNoCtx, 1)
	naiveOut := make(chan string, 1)
	go func() {
		job := <-naiveCh
		_ = job
		naiveOut <- traceIDOf(context.Background())
	}()
	naiveCh <- jobNoCtx{id: "naive"}
	naive := <-naiveOut

	// Propagated: the job carries the caller's ctx. Go vet will not complain
	// about a context in a struct, but the community convention against it is
	// exactly why in-process queues lose context so often.
	goodCh := make(chan jobWithCtx, 1)
	goodOut := make(chan string, 1)
	go func() {
		job := <-goodCh
		goodOut <- traceIDOf(job.ctx)
	}()
	goodCh <- jobWithCtx{id: "propagated", ctx: ctx}
	propagated := <-goodOut

	return report("chan of jobs -> worker pool", span.TraceID, naive, propagated,
		"in-process: put ctx in the job; cross-process: serialise traceparent")
}

// ---------------------------------------------------------------------------
// Boundary 4: a Postgres-backed queue -- a different process reads a row, so
// only the message body crosses.
// ---------------------------------------------------------------------------

func boundaryQueue() string {
	span := newSpan("POST /orders")
	ctx := withSpan(context.Background(), span)

	type message struct {
		id          string
		traceparent string
	}

	consume := func(m message) string {
		// This runs in `worker`, a separate process. It starts from nothing.
		wctx := context.Background()
		if m.traceparent != "" {
			if s, err := spanFromTraceparent(m.traceparent, "job"); err == nil {
				wctx = withSpan(wctx, s)
			}
		}
		logInfo(wctx, "processing job "+m.id)
		return traceIDOf(wctx)
	}

	naive := consume(message{id: "naive"})
	propagated := consume(message{id: "propagated", traceparent: span.Traceparent()})
	_ = ctx

	return report("Postgres-backed queue (cross-process)", span.TraceID, naive, propagated,
		"the transport carries no headers; put traceparent in the body")
}

// ---------------------------------------------------------------------------
// Boundary 5: the outbound HTTP call -- the easy half, made concrete.
// ---------------------------------------------------------------------------

func boundaryHTTP() string {
	span := newSpan("GET /orders")
	header := span.Traceparent()
	downstream, err := spanFromTraceparent(header, "GET /price")
	if err != nil {
		panic(err)
	}
	fmt.Printf("boundary          HTTP request to pricing\n")
	fmt.Printf("caller trace_id   %s\n", span.TraceID)
	fmt.Printf("traceparent sent  %s\n", header)
	fmt.Printf("callee trace_id   %-32s parsed from the header\n", downstream.TraceID)
	fmt.Printf("verdict           preserved   (this is what being a W3C standard buys)\n\n")
	return "preserved"
}

func main() {
	fmt.Println("Layer 6 Topic 3 - losing trace context in Go (explicit context.Context)")
	fmt.Printf("go %s   %s/%s\n", runtime.Version(), runtime.GOOS, runtime.GOARCH)
	fmt.Println(strings.Repeat("=", 72))
	fmt.Println()

	type row struct {
		name, verdict, loud string
	}
	rows := []row{
		{"go func(ctx)", boundaryGoroutine(), "n/a - does not lose it"},
		{"context.Background() helper", boundaryFreshContext(), "yes - visible at the call site"},
		{"chan job -> worker", boundaryChannel(), "yes - the struct has no ctx field"},
		{"Postgres queue", boundaryQueue(), "no - a missing body field looks like nothing"},
		{"http traceparent", boundaryHTTP(), "n/a - does not lose it"},
	}

	fmt.Println("--- Summary: Go's trade, stated as a table ---")
	fmt.Printf("  %-28s %-10s %s\n", "boundary", "verdict", "fails loudly?")
	for _, r := range rows {
		fmt.Printf("  %-28s %-10s %s\n", r.name, r.verdict, r.loud)
	}
	fmt.Println()
	fmt.Println("  Go loses context in exactly the places you can point at, and the")
	fmt.Println("  one place it cannot help with is the one place no runtime can: a")
	fmt.Println("  queue, where the only thing that crosses is the message body.")
	fmt.Println()

	fmt.Println("--- The one-query test, on the log lines this run emitted ---")
	withID := 0
	for _, r := range logs {
		if r.traceID != "" {
			withID++
		}
	}
	fmt.Printf("  log lines emitted            %d\n", len(logs))
	fmt.Printf("  lines carrying a trace_id    %d\n", withID)
	fmt.Printf("  lines carrying nothing       %d   <- unqueryable by request\n", len(logs)-withID)
	for _, r := range logs {
		id := r.traceID
		if id == "" {
			id = "(empty)"
		}
		fmt.Printf("    %-28s trace_id=%s\n", r.msg, id)
	}
	fmt.Println()
	fmt.Println("  Every one of those calls took a ctx. The empty lines are the ones")
	fmt.Println("  where the ctx handed in was manufactured rather than passed down.")
}
