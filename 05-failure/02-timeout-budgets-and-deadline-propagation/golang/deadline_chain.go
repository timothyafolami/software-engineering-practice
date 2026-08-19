// Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
// Go process.
//
// Go is genuinely ahead of every other runtime in this layer on this topic,
// and it is worth reading the source of `context` -- it is short -- as the
// reference implementation of the idea. context.WithTimeout produces a value
// that database/sql, net/http and every well-behaved library obey;
// r.Context() is ALREADY cancelled when the client disconnects; cancellation
// propagates down the tree without anyone opting in.
//
// Which is why Go's failure mode is the inverse of everyone else's. Nobody
// forgets to pass a context in Go; the compiler and every linter shout. What
// people do instead is write context.Background() somewhere in the middle of
// a handler -- to "start something that outlives the request", or because a
// helper's signature did not take one -- and silently detach an entire
// subtree from the deadline. Variant 2 below is exactly that one line.
//
// WHAT THIS DEMONSTRATES
//
//	gateway -> serviceB -> serviceC, where C holds a pooled connection for a
//	controlled service time. The gateway's budget is 500ms.
//
//	 1. healthy               everything succeeds; the bug is invisible
//	 2. detached at B         serviceB calls context.Background(): one line,
//	                          no error, no warning, no deadline at all below
//	                          it, and C never learns that the gateway gave up
//	 3. context threaded      the deadline reaches C, which refuses work it
//	                          cannot finish and hands a connection back the
//	                          moment it finds the request behind it is dead
//	 4. + QueryContext        the query itself honours the context, so the
//	                          connection is released when the caller gives
//	                          up rather than when the query finishes
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. Rows 1 and 2 are the same code with one line different, and the
//     difference is a service that works and a service that does not.
//  2. `zombie/s` -- completions C finished after the gateway had already
//     returned 504. Each one is a pool slot and a service time spent
//     producing a response nobody will read.
//  3. `C pool in use` pinned at the pool size in row 2. That is topic 1's
//     L, consumed entirely by dead work.
//  4. Row 3 versus row 4: threading the context is not the same as the
//     driver honouring it. A pool slot is held until the QUERY returns,
//     not until your goroutine gives up waiting for it.
//
// The load generator is OPEN MODEL: Poisson arrivals, and it does not wait
// for a response before sending the next request.
//
// RUN
//
//	go run deadline_chain.go
package main

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// ------------------------------------------------------------------ config

const (
	gatewayBudget = 500 * time.Millisecond
	slack         = 20 * time.Millisecond
	hopOverhead   = 5 * time.Millisecond
	cServiceFast  = 40 * time.Millisecond
	cServiceSlow  = 800 * time.Millisecond
	slowFraction  = 0.25
	cPoolSize     = 8
	rate          = 50.0
	duration      = 12 * time.Second
	gaugeEvery    = 20 * time.Millisecond
)

// ----------------------------------------------------------------- metrics

type metrics struct {
	mu        sync.Mutex
	ok        int64
	failed    int64
	zombie    int64
	killed    int64
	abandoned int64
	cLatency  []float64
	gauge     []float64
}

func (m *metrics) observeC(latency float64, zombie bool) {
	m.mu.Lock()
	m.cLatency = append(m.cLatency, latency)
	m.mu.Unlock()
	if zombie {
		atomic.AddInt64(&m.zombie, 1)
	}
}

// -------------------------------------------------------------- the pool

// pool is what database/sql's MaxOpenConns is underneath, plus a database
// server on the other end of it. The server does not care about your
// goroutines: query holds a connection for its whole duration unless the
// driver was given a context it actually honours.
type pool struct {
	tokens chan struct{}
	inUse  atomic.Int64
	m      *metrics
}

func newPool(size int, m *metrics) *pool {
	p := &pool{tokens: make(chan struct{}, size), m: m}
	for i := 0; i < size; i++ {
		p.tokens <- struct{}{}
	}
	return p
}

// query acquires a connection and runs for d.
//
// honourContext models the difference between db.Query(...) and
// db.QueryContext(ctx, ...). With it false the statement runs to completion
// no matter what the caller does -- which is also what happens to a real
// QueryContext once the rows are already coming back, and what happens to
// anything the driver has handed to the server.
func (p *pool) query(ctx context.Context, d time.Duration, honourContext bool) bool {
	// Waiting for a connection is itself cancellable, which is the part
	// database/sql gets right and most hand-rolled pools do not.
	select {
	case <-p.tokens:
	case <-ctx.Done():
		return false
	}
	defer func() { p.tokens <- struct{}{} }()

	// Checked out. If the request that queued for this connection died while
	// it was queueing, give the connection straight back rather than spend a
	// service time on a corpse. Under overload this is where most of the
	// recovered capacity comes from.
	if dl, ok := ctx.Deadline(); ok && time.Until(dl) < slack {
		atomic.AddInt64(&p.m.abandoned, 1)
		return false
	}

	p.inUse.Add(1)
	defer p.inUse.Add(-1)

	if !honourContext {
		time.Sleep(d)
		return true
	}
	select {
	case <-time.After(d):
		return true
	case <-ctx.Done():
		atomic.AddInt64(&p.m.killed, 1)
		return false
	}
}

// --------------------------------------------------------------- the hops

func serviceC(ctx context.Context, p *pool, m *metrics, slow bool,
	gatewayDeadline time.Time, honourContext bool) error {

	if dl, ok := ctx.Deadline(); ok && time.Until(dl) < slack {
		// Refuse to START work that cannot finish. A request rejected here
		// costs no pool slot, no queue position, nothing at all.
		return context.DeadlineExceeded
	}
	time.Sleep(hopOverhead)

	d := cServiceFast
	if slow {
		d = cServiceSlow
	}
	started := time.Now()

	// The query runs in its own goroutine so that this hop can stop waiting
	// without the query stopping. That is not an artefact of the test: it is
	// what a database server does, and what any driver call already in
	// flight does, in every language in this layer.
	done := make(chan bool, 1)
	go func() {
		completed := p.query(ctx, d, honourContext)
		finished := time.Now()
		m.observeC(float64(finished.Sub(started).Milliseconds()),
			completed && finished.After(gatewayDeadline))
		done <- completed
	}()

	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func serviceB(ctx context.Context, p *pool, m *metrics, slow bool,
	gatewayDeadline time.Time, detach bool, honourContext bool) error {

	if dl, ok := ctx.Deadline(); ok && time.Until(dl) < slack {
		return context.DeadlineExceeded
	}
	time.Sleep(hopOverhead)

	outCtx := ctx
	var cancel context.CancelFunc
	if detach {
		// ==================== THE ONE LINE ====================
		// It compiles, it passes review, `go vet` is silent, and the
		// linter that complains about a missing context is satisfied,
		// because there IS a context. It just is not the caller's -- and
		// context.Background() carries no deadline, exactly like the zero
		// value of http.Client, so everything below this point now has
		// infinite patience for a request the gateway already abandoned.
		outCtx, cancel = context.WithCancel(context.Background())
		// ======================================================
	} else {
		// budget_out = budget_in - elapsed_here - slack. With a context you
		// mostly do not even write this: passing ctx down already carries
		// the absolute deadline, and WithTimeout here only ever TIGHTENS it.
		outCtx, cancel = context.WithTimeout(ctx, time.Until(gatewayDeadline)-slack)
	}
	defer cancel()

	return serviceC(outCtx, p, m, slow, gatewayDeadline, honourContext)
}

func gateway(p *pool, m *metrics, slow, detach, honourContext bool) {
	// In a real server this is r.Context() with a timeout on top, and it is
	// already cancelled if the client hangs up. Nobody has to remember.
	ctx, cancel := context.WithTimeout(context.Background(), gatewayBudget)
	defer cancel()
	deadline, _ := ctx.Deadline()

	// The gateway enforces its own budget locally as well as passing it on,
	// because a caller that only trusts the callee to be timely has no
	// timeout at all. This select is also the moment the gateway stops
	// waiting -- and, in variant 2, the moment nothing downstream notices.
	errc := make(chan error, 1)
	go func() { errc <- serviceB(ctx, p, m, slow, deadline, detach, honourContext) }()
	select {
	case err := <-errc:
		if err != nil {
			atomic.AddInt64(&m.failed, 1)
		} else {
			atomic.AddInt64(&m.ok, 1)
		}
	case <-ctx.Done():
		atomic.AddInt64(&m.failed, 1)
	}
}

// -------------------------------------------------------------- the driver

func runVariant(slowFrac float64, detach, honourContext bool) *metrics {
	m := &metrics{}
	p := newPool(cPoolSize, m)
	// Identical arrivals and an identical set of slow requests in every
	// variant, so what differs between the rows is policy and only policy.
	rng := rand.New(rand.NewSource(20250502))

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		t := time.NewTicker(gaugeEvery)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				m.mu.Lock()
				m.gauge = append(m.gauge, float64(p.inUse.Load()))
				m.mu.Unlock()
			}
		}
	}()

	begin := time.Now()
	end := begin.Add(duration)
	at := begin
	var reqs sync.WaitGroup
	for {
		at = at.Add(time.Duration(rng.ExpFloat64() / rate * float64(time.Second)))
		if at.After(end) {
			break
		}
		if d := time.Until(at); d > 0 {
			time.Sleep(d)
		}
		slow := rng.Float64() < slowFrac
		reqs.Add(1)
		go func() {
			defer reqs.Done()
			gateway(p, m, slow, detach, honourContext)
		}()
	}
	reqs.Wait()
	// Drain. Zombies are by definition still running after everyone gave up,
	// so a report taken at the end of the load would undercount them.
	time.Sleep(cServiceSlow + 300*time.Millisecond)
	close(stop)
	wg.Wait()
	return m
}

// -------------------------------------------------------------- reporting

const header = "variant                      gw success  zombie/s  C pool in use  C p99 ms  killed/s  gaveback/s"

func printRow(label string, m *metrics) {
	seconds := duration.Seconds()
	total := m.ok + m.failed
	success := 0.0
	if total > 0 {
		success = 100 * float64(m.ok) / float64(total)
	}
	m.mu.Lock()
	lat := append([]float64(nil), m.cLatency...)
	gauge := append([]float64(nil), m.gauge...)
	m.mu.Unlock()
	sort.Float64s(lat)

	fmt.Printf("%-28s %9.1f%% %9.1f %13s %9.0f %9.1f %11.1f\n",
		label, success, float64(m.zombie)/seconds,
		fmt.Sprintf("%.1f/%d", mean(gauge), cPoolSize),
		percentile(lat, 99), float64(m.killed)/seconds,
		float64(m.abandoned)/seconds)
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	k := int(math.Round(p / 100 * float64(len(sorted)-1)))
	if k >= len(sorted) {
		k = len(sorted) - 1
	}
	return sorted[k]
}

func mean(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	s := 0.0
	for _, x := range v {
		s += x
	}
	return s / float64(len(v))
}

func main() {
	fastDemand := rate * (1 - slowFraction) * cServiceFast.Seconds()
	slowDemand := rate * slowFraction * cServiceSlow.Seconds()

	fmt.Println("Deadline propagation through gateway -> serviceB -> serviceC, in Go.")
	fmt.Printf("Gateway budget %v, slack %v per hop, C pool %d, offered %.0f rps for %v.\n",
		gatewayBudget, slack, cPoolSize, rate, duration)
	fmt.Printf("When C is unwell, %.0f%% of queries take %v and the rest take %v.\n",
		slowFraction*100, cServiceSlow, cServiceFast)
	fmt.Printf("Demand on the pool is then %.1f + %.1f = %.1f connection-seconds per second\n",
		slowDemand, fastDemand, slowDemand+fastDemand)
	fmt.Printf("against %d available, i.e. rho = %.2f. None of the slow queries can beat the budget.\n\n",
		cPoolSize, (slowDemand+fastDemand)/cPoolSize)
	fmt.Println(header)
	fmt.Println(dashes(len(header)))

	printRow("1 healthy", runVariant(0, false, false))
	printRow("2 detached at B", runVariant(slowFraction, true, false))
	printRow("3 context threaded", runVariant(slowFraction, false, false))
	printRow("4 + QueryContext", runVariant(slowFraction, false, true))

	fmt.Println()
	fmt.Println("Rows 2 and 3 differ by exactly one line -- context.Background() instead")
	fmt.Println("of the caller's ctx -- and that line is the difference between a chain")
	fmt.Println("that sheds dead work and one that works hardest precisely when nothing")
	fmt.Println("it produces can be delivered. There is no error, no warning, no vet")
	fmt.Println("diagnostic. Grep your codebase for context.Background() below a handler")
	fmt.Println("boundary; that grep is worth more than most of this program.")
	fmt.Println()
	fmt.Println("Rows 3 and 4 are the part context alone does not buy you. Threading a")
	fmt.Println("deadline tells your code when to stop waiting; a connection is held")
	fmt.Println("until the QUERY returns. db.QueryContext closes that gap -- and even")
	fmt.Println("then only up to the point where the statement is already executing on")
	fmt.Println("the server, which is why Postgres has statement_timeout at all.")
}

func dashes(n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = '-'
	}
	return string(b)
}
