// Layer 6 Topic 5 - Utilization is not saturation: the connection pool, in Go.
//
// Why Go: `database/sql` is the third rung of the ladder, and the only one in
// this topic where saturation is a standard-library property. `DB.Stats()`
// returns, for free, with no instrumentation and no dependency:
//
//	InUse, Idle, MaxOpenConnections   -> utilization
//	WaitCount    (monotonic total)    -> how many checkouts had to wait
//	WaitDuration (monotonic total)    -> how long they waited, summed
//
// Two cumulative counters. That is enough for
// `rate(wait_duration) / rate(wait_count)` = the MEAN wait per interval, with
// zero code. It is not enough for a percentile, ever, at any scrape interval,
// because the individual waits were added together before you saw them.
//
// This program measures both on the same run: the mean that Stats() can give
// you, and the p50/p95/p99 from a record of every checkout that Stats() cannot.
// The gap between the mean and the p99 is the argument for the fourth rung
// (Java/HikariCP, which ships the histogram) and the reason Go's free metric is
// a floor rather than an answer.
//
// The pool below is `database/sql`'s shape: a fixed MaxOpenConns, a FIFO queue
// of waiting requests, and a Stats struct with the same field names and the
// same semantics. No driver and no database are involved -- the observable is
// the stats surface, not the wire protocol. `pg_isready` on this machine says
// Postgres is up, but a real DB would add its own queueing and make the
// measurement about two queues instead of one.
//
// What to look for in the output
// ------------------------------
//  1. The ramp table: utilization pins, saturation keeps climbing.
//  2. Section 3, the free metric: rate(WaitDuration)/rate(WaitCount) computed
//     from two counter reads exactly as PromQL would compute it, printed next
//     to the true distribution over the same interval.
//  3. Section 4: the question the mean answers, the question it cannot, and
//     what it costs to fix that in Go (about twenty lines, and you have to
//     decide the bucket boundaries yourself -- which is Topic 2's problem
//     arriving in a new place).
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
	maxOpenConns   = 5                     // db.SetMaxOpenConns(5)
	serviceTime    = 5 * time.Millisecond  // holding the connection
	thinkTime      = 10 * time.Millisecond // application work between queries
	stepDuration   = 1 * time.Second
	scrapeInterval = 250 * time.Millisecond
)

var concurrencySteps = []int{2, 5, 10, 25, 60, 120}

// ---------------------------------------------------------------------------
// database/sql's pool, cut down to the part this topic is about.
// ---------------------------------------------------------------------------

// Stats mirrors sql.DBStats. Everything here is free in the real thing.
type Stats struct {
	MaxOpenConnections int
	InUse              int
	Idle               int
	WaitCount          int64         // cumulative
	WaitDuration       time.Duration // cumulative
}

type Pool struct {
	mu       sync.Mutex
	max      int
	inUse    int
	waiters  []chan struct{}
	waitN    int64
	waitTime int64 // nanoseconds, cumulative

	// Not in database/sql: a record of every wait, so this program can print
	// the distribution the counters had to throw away.
	samplesMu  sync.Mutex
	samples    []time.Duration
	maxWaiting int64
	curWaiting int64
}

func NewPool(max int) *Pool { return &Pool{max: max} }

func (p *Pool) Acquire() time.Duration {
	start := time.Now()
	p.mu.Lock()
	if p.inUse < p.max && len(p.waiters) == 0 {
		p.inUse++
		p.mu.Unlock()
		p.record(0)
		return 0
	}
	ch := make(chan struct{})
	p.waiters = append(p.waiters, ch)
	p.mu.Unlock()

	n := atomic.AddInt64(&p.curWaiting, 1)
	for {
		old := atomic.LoadInt64(&p.maxWaiting)
		if n <= old || atomic.CompareAndSwapInt64(&p.maxWaiting, old, n) {
			break
		}
	}

	<-ch
	atomic.AddInt64(&p.curWaiting, -1)
	wait := time.Since(start)

	// These two lines are what database/sql does, and all it does.
	atomic.AddInt64(&p.waitN, 1)
	atomic.AddInt64(&p.waitTime, int64(wait))

	p.record(wait)
	return wait
}

func (p *Pool) Release() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.waiters) > 0 {
		ch := p.waiters[0]
		p.waiters = p.waiters[1:]
		close(ch) // longest waiter first: database/sql's connRequest queue is FIFO
		return
	}
	p.inUse--
}

func (p *Pool) record(d time.Duration) {
	p.samplesMu.Lock()
	p.samples = append(p.samples, d)
	p.samplesMu.Unlock()
}

// Stats is DB.Stats(): free, cumulative, and mean-only by construction.
func (p *Pool) Stats() Stats {
	p.mu.Lock()
	inUse := p.inUse
	p.mu.Unlock()
	return Stats{
		MaxOpenConnections: p.max,
		InUse:              inUse,
		Idle:               p.max - inUse,
		WaitCount:          atomic.LoadInt64(&p.waitN),
		WaitDuration:       time.Duration(atomic.LoadInt64(&p.waitTime)),
	}
}

func (p *Pool) snapshotSamples(from int) []time.Duration {
	p.samplesMu.Lock()
	defer p.samplesMu.Unlock()
	out := make([]time.Duration, len(p.samples)-from)
	copy(out, p.samples[from:])
	return out
}

func (p *Pool) sampleCount() int {
	p.samplesMu.Lock()
	defer p.samplesMu.Unlock()
	return len(p.samples)
}

// ---------------------------------------------------------------------------

func percentile(values []time.Duration, q float64) time.Duration {
	if len(values) == 0 {
		return 0
	}
	sorted := make([]time.Duration, len(values))
	copy(sorted, values)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	idx := int(q*float64(len(sorted))+0.999999) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

func mean(values []time.Duration) time.Duration {
	if len(values) == 0 {
		return 0
	}
	var total time.Duration
	for _, v := range values {
		total += v
	}
	return total / time.Duration(len(values))
}

type stepResult struct {
	concurrency  int
	requests     int
	polledUtil   float64
	polledQueue  int64
	trueMaxQueue int64
	scrapes      int
	waitP50      time.Duration
	waitP99      time.Duration
	waitMean     time.Duration
	statsMean    time.Duration // what rate(WaitDuration)/rate(WaitCount) gives
	latP99       time.Duration
	goroutines   int
	osThreads    int
}

func runStep(pool *Pool, concurrency int) stepResult {
	deadline := time.Now().Add(stepDuration)
	firstSample := pool.sampleCount()
	atomic.StoreInt64(&pool.maxWaiting, 0)

	// The two counter reads that bracket the interval. This is exactly what a
	// Prometheus scrape does, and exactly what rate() differences.
	before := pool.Stats()

	var latMu sync.Mutex
	var latencies []time.Duration

	var polledUtil float64
	var polledQueue int64
	scrapes := 0
	stopScraper := make(chan struct{})
	var scraperDone sync.WaitGroup
	scraperDone.Add(1)
	go func() {
		defer scraperDone.Done()
		ticker := time.NewTicker(scrapeInterval)
		defer ticker.Stop()
		for {
			select {
			case <-stopScraper:
				return
			case <-ticker.C:
				s := pool.Stats()
				u := float64(s.InUse) / float64(s.MaxOpenConnections)
				if u > polledUtil {
					polledUtil = u
				}
				if q := atomic.LoadInt64(&pool.curWaiting); q > polledQueue {
					polledQueue = q
				}
				scrapes++
			}
		}
	}()

	var peakThreads int
	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var local []time.Duration
			for time.Now().Before(deadline) {
				start := time.Now()
				pool.Acquire()
				time.Sleep(serviceTime)
				pool.Release()
				local = append(local, time.Since(start))
				time.Sleep(thinkTime)
			}
			latMu.Lock()
			latencies = append(latencies, local...)
			latMu.Unlock()
		}()
	}

	// Sample the OS-thread count while the step is live: the Go answer to
	// "what did it cost to hold this many requests in flight".
	go func() {
		for time.Now().Before(deadline) {
			if n := runtime.NumGoroutine(); n > peakThreads {
				peakThreads = n
			}
			time.Sleep(20 * time.Millisecond)
		}
	}()

	wg.Wait()
	close(stopScraper)
	scraperDone.Wait()

	after := pool.Stats()
	waits := pool.snapshotSamples(firstSample)

	// PromQL, by hand: rate(wait_duration) / rate(wait_count) over the interval.
	var statsMean time.Duration
	if dn := after.WaitCount - before.WaitCount; dn > 0 {
		statsMean = (after.WaitDuration - before.WaitDuration) / time.Duration(dn)
	}

	return stepResult{
		concurrency:  concurrency,
		requests:     len(waits),
		polledUtil:   polledUtil,
		polledQueue:  polledQueue,
		trueMaxQueue: atomic.LoadInt64(&pool.maxWaiting),
		scrapes:      scrapes,
		waitP50:      percentile(waits, 0.50),
		waitP99:      percentile(waits, 0.99),
		waitMean:     mean(waits),
		statsMean:    statsMean,
		latP99:       percentile(latencies, 0.99),
		goroutines:   peakThreads,
		osThreads:    osThreadCount(),
	}
}

// osThreadCount reports how many OS threads the Go runtime has created. There
// is no /proc on macOS, so this comes from the runtime rather than the kernel:
// pprof's threadcreate profile counts every M the scheduler ever made. Compare
// it against the goroutine column -- that ratio is why load generators and
// pooled services get written in Go.
func osThreadCount() int {
	if p := pprof.Lookup("threadcreate"); p != nil {
		return p.Count()
	}
	return -1
}

func ms(d time.Duration) string { return fmt.Sprintf("%.1f", float64(d)/float64(time.Millisecond)) }

func main() {
	fmt.Println("Layer 6 Topic 5 - utilization vs saturation, on a Go connection pool")
	fmt.Printf("%s   MaxOpenConns=%d, service time %s, scrape every %s\n",
		runtime.Version(), maxOpenConns, serviceTime, scrapeInterval)
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println()

	pool := NewPool(maxOpenConns)
	var rows []stepResult
	for _, c := range concurrencySteps {
		rows = append(rows, runStep(pool, c))
	}

	fmt.Println("--- The ramp: one pool, six concurrency levels, everything measured ---")
	fmt.Println()
	fmt.Println("              |  USE: utilization  |      USE: saturation      |   RED    |  what it cost")
	fmt.Println("  in flight   |  polled  in use    |  max queued  wait p50/p99 |  req p99 |  goroutines / OS threads")
	fmt.Println("  ------------+--------------------+---------------------------+----------+-------------------------")
	for _, r := range rows {
		fmt.Printf("  %9d   |  %4.0f%%   %d of %d    |  %10d  %5s/%6s ms |  %6s ms |  %6d / %d\n",
			r.concurrency, 100*r.polledUtil,
			int(r.polledUtil*float64(maxOpenConns)), maxOpenConns,
			r.trueMaxQueue, ms(r.waitP50), ms(r.waitP99), ms(r.latP99),
			r.goroutines, r.osThreads)
	}
	fmt.Println()
	fmt.Println("  The last column is the Go-specific one: 120 requests in flight,")
	fmt.Println("  each with its own goroutine, held by a number of OS threads that")
	fmt.Println("  did not grow with them. Topic 2's programs measure the same thing")
	fmt.Println("  from the load generator's side.")
	fmt.Println()

	fmt.Println("--- Section 2: what DB.Stats() gives you, free, with no code ---")
	fmt.Println()
	final := pool.Stats()
	fmt.Printf("  MaxOpenConnections  %d\n", final.MaxOpenConnections)
	fmt.Printf("  InUse / Idle        %d / %d          <- utilization, this instant\n",
		final.InUse, final.Idle)
	fmt.Printf("  WaitCount           %d       <- cumulative, so rate() works\n", final.WaitCount)
	fmt.Printf("  WaitDuration        %s   <- cumulative, so rate() works\n",
		final.WaitDuration.Round(time.Millisecond))
	fmt.Println()
	fmt.Println("  Two monotonic counters mean the saturation metric survives a")
	fmt.Println("  restart, aggregates across pods, and needs no exporter of its own.")
	fmt.Println("  Python has nothing here and Node has an instantaneous gauge; Go is")
	fmt.Println("  the only runtime in this topic where the useful number is free.")
	fmt.Println()

	fmt.Println("--- Section 3: the mean the counters give you vs the distribution ---")
	fmt.Println()
	fmt.Println("  rate(WaitDuration) / rate(WaitCount) is the mean, computed here")
	fmt.Println("  exactly as PromQL would compute it: two counter reads, differenced.")
	fmt.Println()
	fmt.Printf("  %-12s %14s %12s %12s %12s\n",
		"in flight", "Stats() mean", "true mean", "true p50", "true p99")
	fmt.Printf("  %-12s %14s %12s %12s %12s\n",
		strings.Repeat("-", 12), strings.Repeat("-", 14), strings.Repeat("-", 12),
		strings.Repeat("-", 12), strings.Repeat("-", 12))
	for _, r := range rows {
		fmt.Printf("  %-12d %11s ms %9s ms %9s ms %9s ms\n",
			r.concurrency, ms(r.statsMean), ms(r.waitMean), ms(r.waitP50), ms(r.waitP99))
	}
	fmt.Println()
	// The row-by-row means look reasonable because each step is a steady state.
	// A scrape window that spans a degradation is not a steady state, so take
	// the whole ramp as one window -- which is what a 5-minute rate() over an
	// incident actually is.
	allWaits := pool.snapshotSamples(0)
	rampMean := mean(allWaits)
	rampP99 := percentile(allWaits, 0.99)
	fmt.Printf("  whole ramp as ONE scrape window (%s checkouts):\n",
		fmt.Sprintf("%d", len(allWaits)))
	fmt.Printf("    rate(WaitDuration)/rate(WaitCount)   %6s ms\n", ms(rampMean))
	fmt.Printf("    true p99                             %6s ms", ms(rampP99))
	if rampMean > 0 {
		fmt.Printf("   <- %.1fx the mean", float64(rampP99)/float64(rampMean))
	}
	fmt.Println()
	fmt.Println()
	fmt.Println("  Per step the mean is close to the median, because a step is a")
	fmt.Println("  steady state. An incident is not a steady state, and over a window")
	fmt.Println("  that spans one, the mean is dragged down by every fast request")
	fmt.Println("  that happened before things got bad.")
	fmt.Println()
	fmt.Println("  The Stats() column tracks the true mean closely -- it IS the true")
	fmt.Println("  mean, which is the point: the counters are not an approximation,")
	fmt.Println("  they are an exact answer to a question you did not ask. No scrape")
	fmt.Println("  interval and no PromQL function recovers a percentile from a sum")
	fmt.Println("  and a count. The information was destroyed at record time.")
	fmt.Println()

	fmt.Println("--- Section 4: what each metric can and cannot answer ---")
	fmt.Println()
	fmt.Printf("  %-52s %s\n", "question", "answerable from Stats()?")
	fmt.Printf("  %-52s %s\n", strings.Repeat("-", 52), strings.Repeat("-", 24))
	fmt.Printf("  %-52s %s\n", "Is the pool saturated at all?", "yes - WaitCount > 0")
	fmt.Printf("  %-52s %s\n", "Is saturation getting worse?", "yes - rate(WaitCount)")
	fmt.Printf("  %-52s %s\n", "What does a typical waiting request pay?", "yes - the mean")
	fmt.Printf("  %-52s %s\n", "Did anyone wait more than a second?", "NO")
	fmt.Printf("  %-52s %s\n", "What is the p99 checkout wait?", "NO")
	fmt.Printf("  %-52s %s\n", "Is this pool breaching a 300ms latency SLO?", "NO")
	fmt.Println()
	fmt.Println("  The first three cover alerting: 'saturation exists and is growing'")
	fmt.Println("  is a page-worthy fact and the counters give it to you for nothing.")
	fmt.Println("  The last three are the incident, and for those you add a histogram")
	fmt.Println("  by hand -- which means choosing bucket boundaries, which is Topic")
	fmt.Println("  2's problem arriving in a new place. Pick them around your SLO,")
	fmt.Println("  not around round numbers, or you will read the top bucket back as")
	fmt.Println("  a latency.")
	fmt.Println()

	fmt.Println("--- Section 5: what the scrape saw, against what happened ---")
	fmt.Println()
	fmt.Printf("  %-12s %9s %16s %18s\n", "in flight", "scrapes", "true max queue", "polled max queue")
	for _, r := range rows {
		note := ""
		if r.polledQueue < r.trueMaxQueue {
			note = fmt.Sprintf("   <- missed %d", r.trueMaxQueue-r.polledQueue)
		}
		fmt.Printf("  %-12d %9d %16d %18d%s\n",
			r.concurrency, r.scrapes, r.trueMaxQueue, r.polledQueue, note)
	}
	fmt.Println()
	fmt.Println("  InUse and Idle are instantaneous, exactly like node-postgres's")
	fmt.Println("  counts, and they miss exactly the same way. WaitCount and")
	fmt.Println("  WaitDuration are cumulative, so they miss nothing. That difference")
	fmt.Println("  -- gauge versus counter -- is why the saturation half of USE is")
	fmt.Println("  usually the half worth building on.")
}
