// Layer 5 - Topic 1: the latency knee in Go.
//
// WHAT THIS DEMONSTRATES
//
//	Go removes the limit most other runtimes hand you for free. Goroutines
//	are cheap enough that nobody caps them, `net/http`'s server spawns one
//	per request with no ceiling, and `database/sql`'s MaxOpenConns default
//	is *unlimited*. So the queue does not disappear -- it relocates, out of
//	your process and into whatever downstream resource does have a bound.
//	Usually that is Postgres' max_connections, where waiting is replaced by
//	"FATAL: sorry, too many clients already" and the failure stops being a
//	latency problem and becomes an availability one.
//
//	Sweeps 1 and 2 set a real bound (a buffered channel, which is what
//	SetMaxOpenConns is underneath) and reproduce the same 1/(1-rho) knee as
//	every other language here. Sweep 3 removes it, and the same offered
//	load produces connection errors from downstream instead.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `achieved` plateaus at pool / service time: lambda_max = L / W.
//  2. p99 tracks the S/(1-rho) column until the queue stops draining.
//  3. `wait p50` -- time queued for a slot -- is ~0 at rho=0.2 and is most
//     of the latency by rho=0.95. Nothing in the handler got slower.
//  4. Doubling the bound moves capacity and the knee proportionally.
//  5. In the unbounded sweep, `wait p50` is zero at every rate, in-flight L
//     is unbounded, and the errors column is non-zero. Zero queue wait
//     inside your process is not good news; it means the queue is
//     somewhere your metrics do not reach.
//
// RUN
//
//	go run latency_knee.go
package main

import (
	"fmt"
	"math"
	"math/rand"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const (
	serviceTime  = 40 * time.Millisecond
	stepDuration = 8 * time.Second
	gaugeEvery   = 20 * time.Millisecond
	// The downstream's own hard limit, standing in for Postgres'
	// max_connections. Postgres does not queue past it; it refuses.
	downstreamMaxConns = 25
)

var poolSizes = []int{5, 10}
var rhos = []float64{0.2, 0.5, 0.8, 0.9, 0.95, 1.1}

// pool is what database/sql's MaxOpenConns is underneath: a fixed number of
// tokens and an unbounded set of goroutines waiting for one. A buffered
// channel gives you a genuine bounded queue for free, which is the piece
// most runtimes in this lab make you build.
type pool struct {
	tokens chan struct{}
}

func newPool(size int) *pool {
	p := &pool{tokens: make(chan struct{}, size)}
	for i := 0; i < size; i++ {
		p.tokens <- struct{}{}
	}
	return p
}

func (p *pool) acquire() { <-p.tokens }
func (p *pool) release() { p.tokens <- struct{}{} }

// downstream models the thing at the other end of the pool. It accepts a
// bounded number of simultaneous connections and refuses the rest, exactly
// as Postgres does. With a correctly sized pool in front of it this code
// path is never reached, which is the entire argument for having one.
type downstream struct {
	inUse atomic.Int64
}

func (d *downstream) call() error {
	n := d.inUse.Add(1)
	defer d.inUse.Add(-1)
	if n > downstreamMaxConns {
		return fmt.Errorf("too many clients already")
	}
	time.Sleep(serviceTime)
	return nil
}

type result struct {
	target    float64
	offered   float64
	achieved  float64
	p50, p99  float64
	waitP50   float64
	meanTotal float64
	gaugeL    float64
	errors    int64
}

// step runs one measurement at a fixed offered rate.
//
// OPEN MODEL. Arrival times are computed up front from a Poisson process
// and each request's clock starts at the time it was *scheduled* to
// arrive, not at the time this program managed to dispatch it. A generator
// that starts the clock at dispatch forgives itself for being late; real
// users do not wait for your scheduler before deciding to click. Topic 6.
func step(p *pool, d *downstream, rate float64, dur time.Duration) result {
	var (
		mu          sync.Mutex
		total       []float64
		waits       []float64
		completions []time.Time
		gauge       []float64
		inflight    atomic.Int64
		errCount    atomic.Int64
		wg          sync.WaitGroup
	)

	begin := time.Now()
	deadline := begin.Add(dur)

	stopGauge := make(chan struct{})
	go func() {
		ticker := time.NewTicker(gaugeEvery)
		defer ticker.Stop()
		for {
			select {
			case <-stopGauge:
				return
			case <-ticker.C:
				mu.Lock()
				gauge = append(gauge, float64(inflight.Load()))
				mu.Unlock()
			}
		}
	}()

	sent := 0
	at := begin
	for {
		at = at.Add(time.Duration(rand.ExpFloat64() / rate * float64(time.Second)))
		if at.After(deadline) {
			break
		}
		if d := time.Until(at); d > 0 {
			time.Sleep(d)
		}
		sent++
		scheduled := at
		wg.Add(1)
		// One goroutine per arrival, spawned without any ceiling. This is
		// the ordinary shape of a Go service, and it is why the bound has
		// to live somewhere else.
		go func() {
			defer wg.Done()
			inflight.Add(1)
			defer inflight.Add(-1)
			if p != nil {
				p.acquire()
				defer p.release()
			}
			acquired := time.Now()
			err := d.call()
			done := time.Now()
			if err != nil {
				errCount.Add(1)
			}
			mu.Lock()
			waits = append(waits, math.Max(0, acquired.Sub(scheduled).Seconds()*1000))
			total = append(total, done.Sub(scheduled).Seconds()*1000)
			completions = append(completions, done)
			mu.Unlock()
		}()
	}

	// Drain. Past rho=1 this is where the backlog built up during the step
	// finally comes out, which is why those rows carry latencies larger
	// than the step itself.
	wg.Wait()
	close(stopGauge)

	mu.Lock()
	defer mu.Unlock()
	sort.Float64s(total)
	sort.Float64s(waits)
	inWindow := 0
	for _, c := range completions {
		if !c.After(deadline) {
			inWindow++
		}
	}
	seconds := dur.Seconds()
	return result{
		target:   rate,
		offered:  float64(sent) / seconds,
		achieved: float64(inWindow) / seconds,
		p50:      percentile(total, 50),
		p99:      percentile(total, 99),
		waitP50:  percentile(waits, 50),
		// Little's Law is a statement about MEANS. L = lambda * p50 is not
		// a law and stops holding exactly when the distribution skews,
		// which is exactly when you want to use it.
		meanTotal: mean(total),
		gaugeL:    mean(gauge),
		errors:    errCount.Load(),
	}
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

const header = "  rho   offered  achieved      p50      p99   wait p50   L (gauge)   lam*Wbar   S/(1-rho)   errors"

func printRow(rho float64, r result, service float64) {
	predicted := "      inf"
	if rho < 1 {
		predicted = fmt.Sprintf("%9.1f", service/(1-rho)*1000)
	}
	fmt.Printf("%5.2f %9.1f %9.1f %8.1f %8.1f %10.1f %11.1f %10.1f %s %8d\n",
		rho, r.offered, r.achieved, r.p50, r.p99, r.waitP50, r.gaugeL,
		r.achieved*r.meanTotal/1000, predicted, r.errors)
}

func sweep(size int) []float64 {
	p := newPool(size)
	d := &downstream{}

	// Measure S instead of assuming serviceTime. A capacity computed from a
	// constant nobody measured is the commonest way this experiment lies.
	warm := step(p, d, 5, 2*time.Second)
	service := warm.meanTotal / 1000
	capacity := float64(size) / service

	fmt.Printf("\n=== MaxOpenConns = %d, measured service time S = %.1f ms ===\n",
		size, service*1000)
	fmt.Printf("predicted capacity L/S = %.1f rps\n\n", capacity)
	fmt.Println(header)
	fmt.Println(dashes(len(header)))

	p99s := make([]float64, 0, len(rhos))
	for _, rho := range rhos {
		r := step(p, d, capacity*rho, stepDuration)
		printRow(rho, r, service)
		p99s = append(p99s, r.p99)
	}
	return p99s
}

// The knee is a shape, and a table of numbers hides shapes.
func chart(p99s []float64) {
	top := 0.0
	for _, v := range p99s {
		if v > top {
			top = v
		}
	}
	if top == 0 {
		top = 1
	}
	fmt.Println("\n  p99 (ms) against rho")
	for i, v := range p99s {
		n := int(math.Round(56 * v / top))
		if n < 1 {
			n = 1
		}
		fmt.Printf("  rho=%-6.2f|%s %.0f\n", rhos[i], repeat("#", n), v)
	}
	fmt.Printf("  %10s+%s %.0f ms full scale\n", "", dashes(56), top)
}

func repeat(s string, n int) string {
	out := make([]byte, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, s[0])
	}
	return string(out)
}

func dashes(n int) string { return repeat("-", n) }

// The Go-specific failure. database/sql's zero value for MaxOpenConns means
// unlimited, and a goroutine-per-request server has no ceiling of its own,
// so nothing in your process ever waits. That reads as good news on a
// dashboard: queue wait is zero, in-flight is whatever it needs to be. It
// is not good news. The bound still exists, it just belongs to Postgres
// now, and Postgres does not queue -- it refuses the connection, and every
// refused request becomes a retry (topic 3) against a database that is
// already the constrained resource.
func unboundedSweep() {
	d := &downstream{}
	service := serviceTime.Seconds()
	capacity := float64(downstreamMaxConns) / service

	fmt.Printf("\n\n=== MaxOpenConns unset (the database/sql default: unlimited) ===\n")
	fmt.Printf("downstream accepts %d simultaneous connections and refuses the rest\n",
		downstreamMaxConns)
	fmt.Printf("so real capacity is still %.1f rps -- it just is not yours any more\n\n", capacity)
	fmt.Println(header)
	fmt.Println(dashes(len(header)))
	for _, rho := range []float64{0.5, 0.9, 1.1} {
		r := step(nil, d, capacity*rho, stepDuration)
		printRow(rho, r, service)
	}
	fmt.Println("\n  Queue wait inside this process is zero at every rate, because there is")
	fmt.Println("  nothing here to wait for, and p99 in this table looks BETTER than in")
	fmt.Println("  either bounded sweep -- refusing a request is fast. The errors column")
	fmt.Println("  is where the queue went. A latency dashboard cannot see this failure at")
	fmt.Println("  all; only a success-rate one can. SetMaxOpenConns is the difference")
	fmt.Println("  between a queue you can see and one your database is holding for you.")
}

func main() {
	fmt.Printf("Latency knee in Go (GOMAXPROCS=%d, %d CPUs).\n",
		runtime.GOMAXPROCS(0), runtime.NumCPU())
	fmt.Println("One goroutine per arrival, open-model Poisson arrivals, and the only")
	fmt.Println("bound is the one we put there on purpose.")

	for _, size := range poolSizes {
		chart(sweep(size))
	}
	unboundedSweep()

	fmt.Println("\nThe two bounded sweeps ran identical code and an identical ramp; the")
	fmt.Println("only difference is the bound. Compare the two capacity lines and the")
	fmt.Println("two rho=0.9 rows: that is the whole topic in two numbers.")
}
