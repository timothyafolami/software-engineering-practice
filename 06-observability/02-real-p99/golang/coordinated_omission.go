// Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.
//
// Why Go: it is the runtime that makes the honest generator both cheap AND
// blocking-shaped. Node makes open-loop cheap by making you write callbacks and
// promises; Go makes it cheap while the code still reads `submit(); wait()`, one
// goroutine per in-flight request, which is the mental model people actually
// have when they write a load test. This file reports the number of OS threads
// the Go runtime actually created to hold ~100 concurrent in-flight requests --
// that number, next to Python's and C++'s one-thread-per-request, is why
// vegeta, k6 (Go, until it moved to JS-on-Go), ghz and hey are all written in
// Go rather than in the language of the service they test.
//
// What this demonstrates
// ----------------------
//
//   - Service: single server, FIFO queue, 3ms per request -> ~333 req/s.
//
//   - Offered load: 200 req/s, a comfortable 60% of capacity.
//
//   - At T+2.5s exactly one request takes 500ms. One request.
//
//   - CLOSED-LOOP: 4 virtual users, send -> wait -> think 30ms -> repeat.
//     This is `k6 run --vus 4`, and almost every load test ever written.
//
//   - OPEN-LOOP: requests issued at a fixed 200/s regardless of what came back.
//     This is k6's constant-arrival-rate executor, or `vegeta -rate=200`.
//
// What to look for in the output
// ------------------------------
//  1. "requests started IN the stall window": ~4 closed-loop, ~100 open-loop.
//     That one line is the entire mechanism.
//  2. The p99 rows. Same service, same fault, two answers.
//  3. Closed-loop iteration duration vs request duration -- k6's tell.
//  4. Peak goroutines against OS threads created. They are not the same number.
//
// Run:  go run coordinated_omission.go
package main

import (
	"fmt"
	"runtime"
	"runtime/pprof"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	serviceTime    = 3 * time.Millisecond    // -> ~333 req/s capacity
	stallAfter     = 2500 * time.Millisecond // when the one slow request happens
	stallDuration  = 500 * time.Millisecond  // how long that one request takes
	runFor         = 5000 * time.Millisecond
	openRatePerSec = 200 // offered load, ~60% of capacity
	closedVUs      = 4
)

var closedThink = time.Duration(float64(closedVUs) / float64(openRatePerSec) * float64(time.Second))

type request struct {
	seq     int
	arrival time.Time // when it *should* have been sent
	sent    time.Time // when it actually was sent
	done    time.Time
	settled chan struct{}
}

func newRequest(seq int, arrival time.Time) *request {
	return &request{seq: seq, arrival: arrival, settled: make(chan struct{})}
}

// service is a single server with a FIFO queue. The queue is where the latency
// a closed-loop generator cannot see accumulates.
type service struct {
	inbox   chan *request
	epoch   time.Time
	stalled bool
	wg      sync.WaitGroup
}

func newService(epoch time.Time) *service {
	s := &service{inbox: make(chan *request, 4096), epoch: epoch}
	s.wg.Add(1)
	go s.serve()
	return s
}

func (s *service) submit(r *request) {
	r.sent = time.Now()
	s.inbox <- r
}

func (s *service) serve() {
	defer s.wg.Done()
	for r := range s.inbox {
		if !s.stalled && time.Since(s.epoch) >= stallAfter {
			s.stalled = true
			time.Sleep(stallDuration) // the one bad request
		} else {
			time.Sleep(serviceTime)
		}
		r.done = time.Now()
		close(r.settled)
	}
}

func (s *service) stop() {
	close(s.inbox)
	s.wg.Wait()
}

func percentile(values []float64, q float64) float64 {
	if len(values) == 0 {
		return 0
	}
	ordered := append([]float64(nil), values...)
	sort.Float64s(ordered)
	idx := int(q*float64(len(ordered)-1) + 0.5)
	if idx >= len(ordered) {
		idx = len(ordered) - 1
	}
	return ordered[idx]
}

func startedInStall(requests []*request, epoch time.Time) int {
	n := 0
	for _, r := range requests {
		offset := r.sent.Sub(epoch)
		if offset >= stallAfter && offset < stallAfter+stallDuration {
			n++
		}
	}
	return n
}

type run struct {
	requests       int
	latencyMs      []float64
	iterationMs    []float64
	startedInStall int
	peakInFlight   int
	peakGoroutines int
	osThreads      int
}

func threadsCreated() int { return pprof.Lookup("threadcreate").Count() }

func runClosedLoop() run {
	epoch := time.Now()
	svc := newService(epoch)
	var mu sync.Mutex
	var requests []*request
	var iterationMs []float64
	var seq int64
	threadsBefore := threadsCreated()

	var wg sync.WaitGroup
	for i := 0; i < closedVUs; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Since(epoch) < runFor {
				iterStart := time.Now()
				r := newRequest(int(atomic.AddInt64(&seq, 1)), time.Now())
				svc.submit(r)
				<-r.settled // this virtual user is now blocked
				mu.Lock()
				requests = append(requests, r)
				mu.Unlock()
				time.Sleep(closedThink)
				mu.Lock()
				iterationMs = append(iterationMs, float64(time.Since(iterStart).Microseconds())/1000)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	svc.stop()

	latency := make([]float64, 0, len(requests))
	for _, r := range requests {
		latency = append(latency, float64(r.done.Sub(r.sent).Microseconds())/1000)
	}
	return run{
		requests:       len(requests),
		latencyMs:      latency,
		iterationMs:    iterationMs,
		startedInStall: startedInStall(requests, epoch),
		peakInFlight:   closedVUs,
		peakGoroutines: closedVUs,
		osThreads:      threadsCreated() - threadsBefore,
	}
}

func runOpenLoop() run {
	epoch := time.Now()
	svc := newService(epoch)
	var mu sync.Mutex
	var requests []*request
	var inFlight, peakInFlight int
	peakGoroutines := 0
	threadsBefore := threadsCreated()

	var wg sync.WaitGroup
	interval := time.Second / openRatePerSec
	for seq := 0; time.Duration(seq)*interval < runFor; seq++ {
		target := epoch.Add(time.Duration(seq) * interval)
		if d := time.Until(target); d > 0 {
			time.Sleep(d)
		}
		r := newRequest(seq+1, target)
		mu.Lock()
		inFlight++
		if inFlight > peakInFlight {
			peakInFlight = inFlight
		}
		mu.Unlock()
		if g := runtime.NumGoroutine(); g > peakGoroutines {
			peakGoroutines = g
		}
		wg.Add(1)
		// One goroutine per in-flight request. Costs a few KB of stack, no
		// kernel object, and reads exactly like the blocking code it replaces.
		go func(r *request) {
			defer wg.Done()
			svc.submit(r)
			<-r.settled
			mu.Lock()
			inFlight--
			requests = append(requests, r)
			mu.Unlock()
		}(r)
	}
	wg.Wait()
	svc.stop()

	// Latency from INTENDED arrival, not from when the generator got round to
	// sending. In a working open-loop generator these agree, and that agreement
	// is the property being demonstrated.
	latency := make([]float64, 0, len(requests))
	for _, r := range requests {
		latency = append(latency, float64(r.done.Sub(r.arrival).Microseconds())/1000)
	}
	return run{
		requests:       len(requests),
		latencyMs:      latency,
		startedInStall: startedInStall(requests, epoch),
		peakInFlight:   peakInFlight,
		peakGoroutines: peakGoroutines,
		osThreads:      threadsCreated() - threadsBefore,
	}
}

func main() {
	bar := strings.Repeat("=", 74)
	fmt.Println(bar)
	fmt.Println("COORDINATED OMISSION   (Go, single-server FIFO service)")
	fmt.Println(bar)
	fmt.Printf("service capacity ~%.0f req/s (%v/request), offered load %d req/s\n",
		float64(time.Second)/float64(serviceTime), serviceTime, openRatePerSec)
	fmt.Printf("one request at T+%v takes %v instead of %v\n", stallAfter, stallDuration, serviceTime)
	fmt.Printf("run length %v, GOMAXPROCS=%d\n\n", runFor, runtime.GOMAXPROCS(0))

	fmt.Printf("running closed-loop (%d virtual users, %v think time)...\n", closedVUs, closedThink)
	closed := runClosedLoop()
	fmt.Printf("running open-loop (%d req/s arrival rate)...\n\n", openRatePerSec)
	open := runOpenLoop()

	fmt.Printf("%-38s %14s %14s\n", "", "CLOSED-LOOP", "OPEN-LOOP")
	fmt.Printf("%-38s %14d %14d\n", "requests completed", closed.requests, open.requests)
	fmt.Printf("%-38s %14d %14d\n", "requests started IN the stall window",
		closed.startedInStall, open.startedInStall)
	fmt.Printf("%-38s %14d %14d\n", "peak requests in flight", closed.peakInFlight, open.peakInFlight)
	fmt.Printf("%-38s %14d %14d\n", "peak goroutines", closed.peakGoroutines, open.peakGoroutines)
	fmt.Printf("%-38s %14d %14d\n", "OS threads the runtime created", closed.osThreads, open.osThreads)
	fmt.Println()
	for _, p := range []struct {
		label string
		q     float64
	}{{"p50", 0.50}, {"p75", 0.75}, {"p95", 0.95}, {"p99", 0.99}, {"p99.9", 0.999}, {"max", 1.0}} {
		fmt.Printf("%-38s %12.1fms %12.1fms\n", "latency "+p.label,
			percentile(closed.latencyMs, p.q), percentile(open.latencyMs, p.q))
	}

	fmt.Println("\nThe closed-loop column measures request duration: send -> response.")
	fmt.Println("The open-loop column measures from the moment the request was DUE.")
	fmt.Printf("Note the first row too: closed-loop completed %d requests to open-loop's\n", closed.requests)
	fmt.Printf("%d. It did not go slower -- it asked for less, precisely while the\n", open.requests)
	fmt.Println("service was worst.")

	fmt.Println("\nThe tell, inside the closed-loop run alone:")
	fmt.Printf("  request duration p99   : %8.1fms\n", percentile(closed.latencyMs, 0.99))
	fmt.Printf("  iteration duration p99 : %8.1fms\n", percentile(closed.iterationMs, 0.99))
	fmt.Println("  If iteration_duration climbs while http_req_duration does not, your")
	fmt.Println("  generator stopped asking. That is k6's version of this same line.")

	c99 := percentile(closed.latencyMs, 0.99)
	o99 := percentile(open.latencyMs, 0.99)
	if c99 > 0 {
		fmt.Printf("\nVERDICT: open-loop p99 is %.1fx the closed-loop p99 for the identical\n", o99/c99)
		fmt.Println("service and the identical fault.")
	}
	fmt.Printf("The closed-loop generator sampled the stall %d times out of %d requests\n",
		closed.startedInStall, closed.requests)
	fmt.Printf("(%.2f%%), which is why it never reaches the 99th percentile.\n",
		100*float64(closed.startedInStall)/float64(max(1, closed.requests)))
	fmt.Println("\nGo footnote: read the last three rows of the table together. Around a")
	fmt.Println("hundred requests were in flight at once, each held by its own goroutine,")
	fmt.Println("and the number of *additional* OS threads the runtime needed to create")
	fmt.Println("to manage that is the row above -- usually zero or one, because the")
	fmt.Println("threads it already had were enough. In the Python, Rust and C++")
	fmt.Println("versions of this file that column reads ~1000 threads. That ratio is")
	fmt.Println("why load generators are written in Go.")
}
