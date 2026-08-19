// Layer 10 - Topic 3: the pool is the concurrency limit. (Go)
//
// What this demonstrates
//
//	Part 1  L = λW as a wall, against Go's actual limiting primitive: a
//	        buffered channel used as a semaphore, which is what
//	        database/sql's SetMaxOpenConns is underneath. c slots and a
//	        mean service time W pin maximum throughput at c/W. Service
//	        time never changes; everything that moves is acquire wait.
//	Part 2  The Go-specific trap, and it is a good one.
//	        SetMaxIdleConns defaults to 2. It is not a limit on
//	        concurrency -- SetMaxOpenConns is that -- it is a limit on
//	        how many connections may be KEPT when they are returned.
//	        Above 2 concurrent users, every extra connection is closed
//	        on release and re-opened on the next acquire, so a
//	        high-throughput service pays connection setup on most
//	        requests and Postgres sees a connection storm. Throughput
//	        falls with no query getting slower and no pool being full.
//
// What to look for
//   - Part 1: `svc p50` flat across every row while `acq p99` explodes.
//   - Part 2: same c, same service time, same λ. The only change is how
//     many connections may sit idle, and it costs you a setup round trip
//     on most requests. `db_conns_opened` is the metric that shows it;
//     pool-full counters show nothing at all.
//   - The arrival process is Poisson and OPEN LOOP. A closed-loop
//     generator cannot produce an unbounded queue, because it stops
//     issuing exactly when the system is in trouble.
//
// The Kingman variance arm lives in python/pool_queueing.py -- that part is
// arithmetic about distributions and is not a property of any runtime.
// What is a property of the runtime is everything in this file.
//
// No dependencies, no database required. Runs with no arguments:
//
//	cd golang && go run pool_queueing.go
package main

import (
	"fmt"
	"math/rand"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const seed = 20260818

type sample struct {
	acquire time.Duration
	service time.Duration
	total   time.Duration
}

type result struct {
	label       string
	lambda      float64
	slots       int
	meanService time.Duration
	samples     []sample
	completed   int64
	// Completions that landed INSIDE the arrival window. Throughput has to
	// be counted over the same interval as wall: completed keeps rising
	// during the drain, and dividing the post-drain total by the arrival
	// window reports a rate above c/W -- above the wall itself.
	completedInWindow int64
	opened            int64
	wall              time.Duration
}

func (r *result) rho() float64 {
	return r.lambda * r.meanService.Seconds() / float64(r.slots)
}

func (r *result) lambdaMax() float64 {
	return float64(r.slots) / r.meanService.Seconds()
}

func (r *result) pct(pick func(sample) time.Duration, q float64) time.Duration {
	if len(r.samples) == 0 {
		return 0
	}
	vals := make([]time.Duration, len(r.samples))
	for i, s := range r.samples {
		vals[i] = pick(s)
	}
	sort.Slice(vals, func(i, j int) bool { return vals[i] < vals[j] })
	idx := int(q * float64(len(vals)))
	if idx >= len(vals) {
		idx = len(vals) - 1
	}
	return vals[idx]
}

func acq(s sample) time.Duration { return s.acquire }
func svc(s sample) time.Duration { return s.service }
func tot(s sample) time.Duration { return s.total }

// pool models database/sql's two independent knobs.
//
//	maxOpen  the concurrency limit -- the c in L = λW
//	maxIdle  how many connections may be KEPT on release. Anything beyond
//	         this is closed, and re-opened (at setupCost) on next acquire.
type pool struct {
	slots  chan struct{}
	idle   chan struct{}
	setup  time.Duration
	opened *int64
}

func newPool(maxOpen, maxIdle int, setup time.Duration, opened *int64) *pool {
	p := &pool{
		slots:  make(chan struct{}, maxOpen),
		idle:   make(chan struct{}, maxIdle),
		setup:  setup,
		opened: opened,
	}
	for i := 0; i < maxOpen; i++ {
		p.slots <- struct{}{}
	}
	for i := 0; i < maxIdle; i++ {
		p.idle <- struct{}{}
	}
	return p
}

// acquire takes a concurrency slot, and then either reuses an idle
// connection or pays to open a new one.
func (p *pool) acquire() bool {
	<-p.slots
	select {
	case <-p.idle:
		return true // reused
	default:
		atomic.AddInt64(p.opened, 1)
		time.Sleep(p.setup) // the connection storm, priced
		return false
	}
}

func (p *pool) release(reused bool) {
	if reused {
		select {
		case p.idle <- struct{}{}:
		default:
		}
	} else {
		select {
		case p.idle <- struct{}{}:
		default: // idle list full: this connection is closed, not kept
		}
	}
	p.slots <- struct{}{}
}

func drive(label string, lambda float64, maxOpen, maxIdle int,
	meanService, setup, duration time.Duration) *result {

	rng := rand.New(rand.NewSource(seed))
	var opened int64
	p := newPool(maxOpen, maxIdle, setup, &opened)
	r := &result{label: label, lambda: lambda, slots: maxOpen, meanService: meanService}

	var mu sync.Mutex
	var wg sync.WaitGroup
	start := time.Now()
	next := start

	for time.Since(start) < duration {
		next = next.Add(time.Duration(rng.ExpFloat64() / lambda * float64(time.Second)))
		if d := time.Until(next); d > 0 {
			time.Sleep(d)
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			arrived := time.Now()
			reused := p.acquire()
			acquired := time.Now()
			time.Sleep(meanService)
			done := time.Now()
			p.release(reused)
			atomic.AddInt64(&r.completed, 1)
			mu.Lock()
			r.samples = append(r.samples, sample{
				acquire: acquired.Sub(arrived),
				service: done.Sub(acquired),
				total:   done.Sub(arrived),
			})
			mu.Unlock()
		}()
	}
	r.wall = time.Since(start)
	r.completedInWindow = atomic.LoadInt64(&r.completed)

	// Drain with a bound: past the wall the queue never drains, and that is
	// the result rather than a bug.
	drained := make(chan struct{})
	go func() { wg.Wait(); close(drained) }()
	select {
	case <-drained:
	case <-time.After(duration):
	}
	r.opened = atomic.LoadInt64(&opened)
	return r
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000.0 }

func main() {
	fmt.Println("Go - pool queueing and Little's Law")
	fmt.Printf("  arrivals: Poisson (c_a = 1), open loop, seed %d\n", seed)

	const slots = 20
	const service = 50 * time.Millisecond
	fmt.Printf("\nPart 1 - L = λW. c = SetMaxOpenConns = %d, W = %v, "+
		"so λ_max = c/W = %.0f req/s\n", slots, service,
		float64(slots)/service.Seconds())
	fmt.Println(string(dashes(78)))
	fmt.Printf("  %-10s %5s %9s %9s %9s %9s %9s\n",
		"run", "ρ", "acq p50", "acq p99", "svc p50", "tot p99", "done/s")
	for _, lambda := range []float64{200, 360, 400, 440} {
		// maxIdle == maxOpen here: no churn, so Part 1 isolates queueing.
		r := drive(fmt.Sprintf("λ=%.0f", lambda), lambda, slots, slots,
			service, 0, 3*time.Second)
		fmt.Printf("  %-10s %5.2f %8.1f %8.1f %8.1f %8.1f %9.0f\n",
			r.label, r.rho(), ms(r.pct(acq, 0.5)), ms(r.pct(acq, 0.99)),
			ms(r.pct(svc, 0.5)), ms(r.pct(tot, 0.99)),
			float64(r.completedInWindow)/r.wall.Seconds())
	}
	fmt.Println("\n  Service time is identical in every row. Everything that moved is")
	fmt.Println("  waiting for a slot, which is why acquire wait needs its own timer.")

	fmt.Println("\nPart 2 - SetMaxIdleConns, the knob that is not a concurrency limit")
	fmt.Println(string(dashes(78)))
	fmt.Println("  Same c, same service time, same λ. The only difference is how many")
	fmt.Println("  connections may be KEPT when they are returned. Default is 2.")
	// A real Postgres connect is a TCP handshake, a startup packet, auth,
	// and often TLS. 10ms is conservative for that over a local network.
	const setup = 10 * time.Millisecond
	fmt.Printf("  connection setup cost: %v\n\n", setup)
	fmt.Printf("  %-24s %9s %9s %9s %12s %14s\n",
		"config", "acq p99", "tot p50", "tot p99", "done/s", "conns opened")
	for _, cfg := range []struct {
		label   string
		maxIdle int
	}{
		{"MaxIdleConns=2 (default)", 2},
		{"MaxIdleConns=MaxOpenConns", slots},
	} {
		r := drive(cfg.label, 300, slots, cfg.maxIdle, service, setup, 3*time.Second)
		fmt.Printf("  %-24s %8.1f %8.1f %8.1f %12.0f %14d\n",
			cfg.label, ms(r.pct(acq, 0.99)), ms(r.pct(tot, 0.5)),
			ms(r.pct(tot, 0.99)), float64(r.completedInWindow)/r.wall.Seconds(), r.opened)
	}
	fmt.Println("\n  Read the last column first. No pool-full counter fires, no query")
	fmt.Println("  is slower, and depending on how expensive a connect is on your")
	fmt.Println("  network the latency columns may barely move -- which is exactly")
	fmt.Println("  what makes this one hard to find. What changed is that the")
	fmt.Println("  database is being handed a connection storm by a client that is")
	fmt.Println("  comfortably under its own concurrency limit. Raise `setup` to what")
	fmt.Println("  a connect really costs you (TLS and auth included) and re-run: the")
	fmt.Println("  latency columns move when that cost is a real fraction of W.")
	fmt.Println("  SetMaxOpenConns is the c in Little's Law. SetMaxIdleConns is a")
	fmt.Println("  separate decision about churn, and its default assumes you are not")
	fmt.Println("  doing this much traffic.")
	fmt.Println()
	fmt.Println("  The Kingman variance arm is in python/pool_queueing.py: it is")
	fmt.Println("  arithmetic about distributions, not a property of any runtime.")
}

func dashes(n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = '-'
	}
	return b
}
