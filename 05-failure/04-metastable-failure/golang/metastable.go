// Layer 5 - Topic 4: metastable failure, in one Go process.
//
// THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
// The claim is that the thing which TRIGGERS an outage and the thing which
// SUSTAINS it are different mechanisms, so removing the trigger does not end
// the outage. This file removes the trigger, keeps offered load exactly where
// it was, waits, and shows you nothing improving.
//
// Go needs a deliberate mistake to get here, and that is the lesson rather
// than an excuse. Two of Go's defaults are genuinely protective and both are
// used below, unaltered:
//
//   - The pool is a BUFFERED CHANNEL, which is a real bounded queue with real
//     backpressure -- `d.slots <- struct{}{}` blocks when the pool is full,
//     which is the correct behaviour and is what `SetMaxOpenConns` is
//     underneath.
//   - Cancellation is a CONTEXT, and it actually removes abandoned work from
//     the system: when a caller's deadline passes, the query it was waiting
//     on stops and hands its connection back, rather than running to
//     completion for nobody. Python's `wait_for` and Rust's `timeout` arrange
//     the same thing; C++ cannot.
//
// So this file has to opt out of exactly one default to get into trouble, and
// it is the one Go tempts everybody with: an unbounded `go` per arriving
// request. Nothing anywhere bounds the number of goroutines waiting on that
// channel, which is why the `gor` column below exists -- the queue did not
// disappear, it relocated into the scheduler. Go's argument is that this is a
// choice you made rather than a default you inherited, and that is exactly
// right; it is also why the collapse looks identical to Python's once made.
//
// WHAT THIS DEMONSTRATES
//
//	A cache in front of a database, at a 90% hit rate, comfortably stable.
//	The trigger is one instantaneous, fully reversible command: FLUSHALL.
//	The cache is BACK the moment it starts refilling -- except that it never
//	starts, because refilling requires a query to finish before its caller
//	gives up, and no query does any more.
//
//	HotOS '25 vocabulary, which this file is built to make concrete:
//	  trigger                 the cache flush, over in one millisecond
//	  amplification mechanism naive retries (topic 3) plus the miss rate
//	                          going from 10% to 100%
//	  sustaining effect       a cache that cannot refill, because fills only
//	                          happen on completions that beat the deadline
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `goodput` versus `thruput`. Throughput stays high while goodput goes to
//     zero: the process is busy, the pool is full, requests are flowing, and
//     almost none of them produce a response anybody receives.
//  2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
//     sustaining effect, and it is why scenario 0 never recovers.
//  3. `gor` -- live goroutines. This is Go's version of Python's climbing
//     in-flight count, and it is the number that tells you the bound you did
//     not set is the bound that mattered. Context cancellation keeps it from
//     being a leak; nothing keeps it from being a queue.
//  4. Which escapes are SUFFICIENT rather than merely helpful. The verdict
//     lines at the end are computed from THIS run, not asserted here.
//
// RUN
//
//	go run metastable.go
//
// Roughly four minutes: five scenarios, the four with an escape running
// longer because "did it recover" is a question about minutes, not seconds.
package main

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------- config
//
// Identical to python/metastable.py's constants, deliberately: the point of
// six languages here is that the same system-level dynamic appears in all of
// them, so the constants are not allowed to drift.

const (
	offeredRPS   = 180.0 // constant. It never changes. That is the point.
	keys         = 400   // the cache keyspace
	evictPerSec  = 18    // TTL churn -> equilibrium hit rate 1 - 18/180 = 90%
	dbService    = 200 * time.Millisecond
	cacheService = 1 * time.Millisecond
	poolSize     = 6 // 6 / 0.200 = 30 misses per second of capacity

	clientTimeout = 500 * time.Millisecond // longer than normal service time,
	attempts      = 3                      // shorter than degraded. On purpose.

	triggerAt   = 6.0  // redis-cli FLUSHALL
	escapeAt    = 16.0 // ten seconds of watching nothing improve first
	endAt       = 30.0 // long enough to prove scenario 0 does not recover
	escapeEndAt = 50.0
	reportEvery = 2.0

	shedLimit       = 8    // escape (c). Topic 5, borrowed early.
	budgetRatio     = 0.10 // escape (b). Topic 3's token bucket.
	rampBackSeconds = 8.0  // escape (a) lets load back SLOWLY. It matters.
	dropSeconds     = 5.0
)

// --------------------------------------------------------------- the cache

// Cache is Redis, modelled as the only thing about Redis that matters here: a
// set of keys that are present, and the fact that emptying it is instant and
// refilling it is not.
type Cache struct {
	mu      sync.Mutex
	present map[int]bool
	hits    int64
	misses  int64
}

func NewCache() *Cache {
	c := &Cache{present: make(map[int]bool, keys)}
	for k := 0; k < keys; k++ {
		c.present[k] = true
	}
	return c
}

func (c *Cache) Get(key int) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.present[key] {
		c.hits++
		return true
	}
	c.misses++
	return false
}

func (c *Cache) Put(key int) {
	c.mu.Lock()
	c.present[key] = true
	c.mu.Unlock()
}

// Flushall is one command. Instantaneous. Fully reversible. This is the
// entire trigger, and ten seconds later it will be completely irrelevant to
// why the system is down.
func (c *Cache) Flushall() {
	c.mu.Lock()
	c.present = make(map[int]bool, keys)
	c.mu.Unlock()
}

// Evict is ordinary TTL churn, which is what holds the hit rate at 90%
// instead of letting it climb to 100% and make the experiment lie.
func (c *Cache) Evict(n int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := 0; i < n; i++ {
		for k := range c.present { // map iteration order is the randomness
			delete(c.present, k)
			break
		}
	}
}

func (c *Cache) TakeRates() (int64, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	h, m := c.hits, c.misses
	c.hits, c.misses = 0, 0
	return h, m
}

// ------------------------------------------------------------ the database

// Database is a real bounded pool: 6 connections at 200ms is 30 queries a
// second, and nothing anybody does to the application changes that number.
// The channel IS the semaphore, and the `select` on ctx.Done() is the whole
// of Go's advantage on this topic -- a waiter whose caller has given up
// leaves the queue instead of eventually being handed a connection nobody
// wants.
type Database struct {
	slots chan struct{}
	inUse int64
}

func NewDatabase() *Database {
	return &Database{slots: make(chan struct{}, poolSize)}
}

func (d *Database) Query(ctx context.Context) error {
	select {
	case d.slots <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	atomic.AddInt64(&d.inUse, 1)
	defer func() {
		atomic.AddInt64(&d.inUse, -1)
		<-d.slots
	}()
	t := time.NewTimer(dbService)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (d *Database) InUse() int { return int(atomic.LoadInt64(&d.inUse)) }

// ------------------------------------------------------------ retry budget

// RetryBudget is topic 3's token bucket, used here only as escape (b). Held
// in milli-tokens so the 0.1-per-success refill fits in an atomic.
type RetryBudget struct{ tokens int64 }

func NewRetryBudget() *RetryBudget { return &RetryBudget{tokens: 3000} }

func (b *RetryBudget) Deposit() {
	for {
		cur := atomic.LoadInt64(&b.tokens)
		next := cur + int64(budgetRatio*1000)
		if next > 103000 {
			next = 103000
		}
		if atomic.CompareAndSwapInt64(&b.tokens, cur, next) {
			return
		}
	}
}

func (b *RetryBudget) Withdraw() bool {
	for {
		cur := atomic.LoadInt64(&b.tokens)
		if cur < 1000 {
			return false
		}
		if atomic.CompareAndSwapInt64(&b.tokens, cur, cur-1000) {
			return true
		}
	}
}

// ------------------------------------------------------------- the server

type Server struct {
	cache     *Cache
	db        *Database
	m         *Metrics
	inflight  int64
	budget    atomic.Value // *RetryBudget, escape (b)
	shedLimit int64        // escape (c); 0 means no shedding
}

func NewServer(c *Cache, db *Database, m *Metrics) *Server {
	return &Server{cache: c, db: db, m: m}
}

func (s *Server) Budget() *RetryBudget {
	if v := s.budget.Load(); v != nil {
		return v.(*RetryBudget)
	}
	return nil
}

// Handle is one attempt. Returns true if the caller got an answer in time.
func (s *Server) Handle(ctx context.Context, key int, deadline time.Time) bool {
	// Escape (c), and topic 5 in one line: refuse work you have no capacity
	// for, immediately, instead of accepting it and being late.
	if lim := atomic.LoadInt64(&s.shedLimit); lim > 0 && atomic.LoadInt64(&s.inflight) >= lim {
		atomic.AddInt64(&s.m.shed, 1)
		return false
	}
	atomic.AddInt64(&s.inflight, 1)
	defer atomic.AddInt64(&s.inflight, -1)

	if s.cache.Get(key) {
		t := time.NewTimer(cacheService)
		defer t.Stop()
		select {
		case <-t.C:
		case <-ctx.Done():
			return false
		}
		return !time.Now().After(deadline)
	}
	if err := s.db.Query(ctx); err != nil {
		return false
	}
	inTime := !time.Now().After(deadline)
	if inTime {
		// THE SUSTAINING EFFECT, in one `if`. The fill happens in the handler,
		// after the query returns -- and under overload the handler has
		// already been abandoned by then, so the fill never happens. The cache
		// cannot refill precisely because the database is slow, and the
		// database is slow precisely because the cache is empty.
		s.cache.Put(key)
	}
	return inTime
}

// -------------------------------------------------------------- the client

// clientRequest is topic 3's naive retry client: no jitter, no budget unless
// escape (b) turned one on, and a per-attempt timeout that is comfortable
// when the system is well and hopeless when it is not.
func clientRequest(parent context.Context, s *Server, m *Metrics, key int) {
	for attempt := 0; attempt < attempts; attempt++ {
		if attempt > 0 {
			if b := s.Budget(); b != nil && !b.Withdraw() {
				break
			}
			atomic.AddInt64(&m.retries, 1)
		}
		ctx, cancel := context.WithTimeout(parent, clientTimeout)
		deadline, _ := ctx.Deadline()
		ok := s.Handle(ctx, key, deadline)
		// The cancel is not politeness. It is what returns the pool slot and
		// stops the timer for an attempt this client has stopped waiting for
		// -- the mechanism that keeps abandoned work from being additive.
		cancel()
		atomic.AddInt64(&m.thruputAttempts, 1)
		if ok {
			// GOODPUT: a response delivered to a caller that was still waiting
			// for it. Not "requests handled". This is the only number in this
			// file worth alerting on.
			atomic.AddInt64(&m.goodput, 1)
			if b := s.Budget(); b != nil {
				b.Deposit()
			}
			return
		}
		if parent.Err() != nil {
			return // the app "restarted" underneath this request
		}
	}
	atomic.AddInt64(&m.failed, 1)
}

// ------------------------------------------------------------- the harness

type Metrics struct {
	goodput         int64
	thruputAttempts int64
	retries         int64
	failed          int64
	shed            int64
	endAt           float64
	rows            []row
}

type row struct {
	t, offered, thruput, goodput, hit float64
	pg, inflight, goroutines          int
	retry                             float64
}

// offeredRate is constant everywhere except escape (a), which is the only
// intervention in this file that touches the client side at all.
func offeredRate(t float64, escape string) float64 {
	if escape != "a" || t < escapeAt {
		return offeredRPS
	}
	since := t - escapeAt
	if since < dropSeconds {
		return 0 // take the load away
	}
	ramp := (since - dropSeconds) / rampBackSeconds // ... and let it back
	return offeredRPS * math.Min(1.0, ramp)         // SLOWLY
}

func runScenario(escape string) *Metrics {
	end := endAt
	if escape != "" {
		end = escapeEndAt
	}
	m := &Metrics{endAt: end}
	cache := NewCache()
	db := NewDatabase()
	server := NewServer(cache, db, m)
	rng := rand.New(rand.NewSource(20250504))

	// The app's own context. Cancelling it is a process restart: every
	// request in flight dies with it, which is exactly what escape (d)
	// simulates and exactly what a container restart does.
	appCtx, appCancel := context.WithCancel(context.Background())

	begin := time.Now()
	lastReport := begin
	lastEvict := begin
	at := begin
	var last [3]int64
	triggered, escaped := false, false

	for {
		if at.Sub(begin).Seconds() > end {
			break
		}
		rate := offeredRate(at.Sub(begin).Seconds(), escape)
		if rate <= 0 {
			at = at.Add(50 * time.Millisecond)
		} else {
			at = at.Add(time.Duration(rng.ExpFloat64() / rate * float64(time.Second)))
		}
		if d := time.Until(at); d > 0 {
			time.Sleep(d)
		}
		now := time.Now()
		t := now.Sub(begin).Seconds()

		if !triggered && t >= triggerAt {
			cache.Flushall()
			triggered = true
		}
		if !escaped && t >= escapeAt {
			escaped = true
			switch escape {
			case "b":
				server.budget.Store(NewRetryBudget())
			case "c":
				atomic.StoreInt64(&server.shedLimit, shedLimit)
			case "d":
				// "Restart the app containers." Everything in the process
				// goes: the goroutines, the in-flight requests, the pool. The
				// cache is external and stays exactly as cold as it was, and
				// the clients never stopped retrying.
				appCancel()
				appCtx, appCancel = context.WithCancel(context.Background())
				// Rebind rather than reset in place. A restart replaces the
				// process: the new one starts with an empty pool and a zero
				// gauge, while the dying requests unwind against the old
				// objects. Zeroing the counters underneath them would drive
				// the gauges NEGATIVE, which is a bug in the instrument
				// rather than a finding.
				db = NewDatabase()
				server = NewServer(cache, db, m)
			}
		}

		if now.Sub(lastEvict) >= time.Second {
			cache.Evict(evictPerSec)
			lastEvict = now
		}

		if rate > 0 {
			// No backpressure anywhere in that line. `go` always succeeds,
			// whatever the state of the system it is feeding. This is the one
			// default this file opts out of, and it is enough.
			go clientRequest(appCtx, server, m, rng.Intn(keys))
		}

		if now.Sub(lastReport) >= time.Duration(reportEvery*float64(time.Second)) {
			span := now.Sub(lastReport).Seconds()
			g := atomic.LoadInt64(&m.goodput)
			th := atomic.LoadInt64(&m.thruputAttempts)
			r := atomic.LoadInt64(&m.retries)
			hits, misses := cache.TakeRates()
			m.rows = append(m.rows, row{
				t:          t,
				offered:    rate,
				thruput:    float64(th-last[1]) / span,
				goodput:    float64(g-last[0]) / span,
				hit:        100 * float64(hits) / math.Max(1, float64(hits+misses)),
				pg:         db.InUse(),
				inflight:   int(atomic.LoadInt64(&server.inflight)),
				goroutines: runtime.NumGoroutine(),
				retry:      float64(r-last[2]) / math.Max(1e-9, float64(th-last[1])),
			})
			last = [3]int64{g, th, r}
			lastReport = now
		}
	}

	appCancel()
	time.Sleep(50 * time.Millisecond)
	return m
}

// -------------------------------------------------------------- reporting

const header = "      t   offered   thruput   goodput   hit%   pg  inflight    gor  retry/req   goodput as % of offered"

func render(title, note string, m *Metrics) (float64, float64) {
	fmt.Printf("\n=== %s ===\n", title)
	fmt.Printf("    %s\n", note)
	fmt.Println(header)
	fmt.Println(strings.Repeat("-", len(header)))
	for _, r := range m.rows {
		frac := r.goodput / offeredRPS
		bar := strings.Repeat("#", int(math.Max(0, math.Round(24*math.Min(1, frac)))))
		mark := ""
		if math.Abs(r.t-triggerAt) < reportEvery/2 {
			mark = "  <-- FLUSHALL"
		} else if math.Abs(r.t-escapeAt) < reportEvery/2 {
			mark = "  <-- escape applied"
		}
		fmt.Printf("  %5.1f %9.1f %9.1f %9.1f %6.1f %4d %9d %6d %10.2f   |%s%s\n",
			r.t, r.offered, r.thruput, r.goodput, r.hit, r.pg, r.inflight,
			r.goroutines, r.retry, bar, mark)
	}
	var gBefore, gAfter float64
	var nBefore, nAfter int
	for _, r := range m.rows {
		if r.t < triggerAt {
			gBefore += r.goodput
			nBefore++
		}
		if r.t >= m.endAt-6 {
			gAfter += r.goodput
			nAfter++
		}
	}
	if nBefore > 0 {
		gBefore /= float64(nBefore)
	}
	if nAfter > 0 {
		gAfter /= float64(nAfter)
	}
	fmt.Printf("    goodput before the trigger %6.1f rps (%.0f%% of offered)   "+
		"final 6 seconds %6.1f rps (%.0f%% of offered)\n",
		gBefore, 100*gBefore/offeredRPS, gAfter, 100*gAfter/offeredRPS)
	return gBefore, gAfter
}

// verdict is COMPUTED from the run that just happened, never asserted here.
// Sufficient means "goodput came back", not "the intervention did something
// measurable" -- that distinction is the whole of step 5 in the README.
func verdict(before, after float64) string {
	if before <= 1 {
		return "baseline never established -- see README"
	}
	pct := 100 * after / before
	switch {
	case pct >= 70:
		return fmt.Sprintf("SUFFICIENT   (recovered to %.0f%% of pre-trigger goodput)", pct)
	case pct >= 20:
		return fmt.Sprintf("partial      (only %.0f%% of pre-trigger goodput)", pct)
	default:
		return fmt.Sprintf("not sufficient (%.0f%% of pre-trigger goodput)", pct)
	}
}

func main() {
	fmt.Println("Metastable failure: a cache flush that stops mattering long before the outage does.")
	fmt.Printf("Offered load is constant at %.0f rps and is never raised. Cache hit rate %.0f%% when warm.\n",
		offeredRPS, 100-100*evictPerSec/offeredRPS)
	capacity := poolSize / dbService.Seconds()
	fmt.Printf("Database capacity is %d/%.3f = %.0f queries per second. Warm, the miss rate needs %d of them (%.0f%% utilised).\n",
		poolSize, dbService.Seconds(), capacity, evictPerSec, 100*evictPerSec/capacity)
	fmt.Printf("Cold, it needs all %.0f -- %.0fx capacity, before a single retry. Client timeout %.0fms, %d attempts, no jitter, no budget, no shedding.\n",
		offeredRPS, offeredRPS/capacity, float64(clientTimeout.Milliseconds()), attempts)
	fmt.Printf("FLUSHALL at t=%.0fs. Escapes, where a scenario has one, at t=%.0fs.\n", triggerAt, escapeAt)

	scenarios := []struct{ title, note, escape string }{
		{"0 no escape: remove the trigger and wait",
			"The trigger was over in a millisecond. Watch the next 24 seconds.", ""},
		{"a drop offered load to zero, then ramp it back slowly",
			fmt.Sprintf("The one nobody wants to authorise. %.0fs of zero, then %.0fs of ramp. Watch the ramp, not the drop.", dropSeconds, rampBackSeconds), "a"},
		{"b enable topic 3's 10% retry budget, load unchanged",
			"Removes the amplification. Does not remove the sustaining effect.", "b"},
		{"c enable topic 5's load shedder, load unchanged",
			fmt.Sprintf("Admit at most %d in flight; 503 the rest, immediately.", shedLimit), "c"},
		{"d restart the app, load unchanged",
			"Clears the goroutines, the in-flight work and the pool. Not the cache.", "d"},
	}

	type result struct {
		title         string
		before, after float64
	}
	var results []result
	for _, sc := range scenarios {
		m := runScenario(sc.escape)
		before, after := render(sc.title, sc.note, m)
		results = append(results, result{sc.title, before, after})
	}

	fmt.Println("\n" + strings.Repeat("=", 78))
	fmt.Printf("%-52s%15s%11s\n", "scenario", "goodput before", "after")
	fmt.Println(strings.Repeat("-", 78))
	for _, r := range results {
		fmt.Printf("%-52s%14.1f%11.1f\n", r.title, r.before, r.after)
	}

	fmt.Println("\nScenario 0 is the whole topic. The trigger -- one FLUSHALL -- was over")
	fmt.Println("instantly and reversibly, offered load never changed by a single request,")
	fmt.Printf("and goodput half a minute later is %.1f rps -- which is what THIS run\n", results[0].after)
	fmt.Println("measured, not a sentence written before it. If it is not near zero, read")
	fmt.Println("the README's 'what would mean the experiment is broken' before reading")
	fmt.Println("anything else. Nothing is broken. Nothing needs rolling back. The system")
	fmt.Println("has settled into a second stable state, where the cache cannot refill")
	fmt.Println("because the database is saturated and the database is saturated because")
	fmt.Println("the cache is empty.")
	fmt.Println("\nEscapes, judged against THIS run rather than against a story:")
	for _, r := range results[1:] {
		fmt.Printf("  %s %s\n", r.title[:2], verdict(r.before, r.after))
	}
	fmt.Printf("  (scenario 0 finished at %.1f rps of goodput, for comparison)\n", results[0].after)
	fmt.Println("\nWhat each escape actually touches, which is why they do not rank the way")
	fmt.Println("intuition ranks them:")
	fmt.Println("  (a) drop and ramp    removes load, not the loop. The drop always works;")
	fmt.Println("      the RAMP is the experiment. Full load returning to a cache that is")
	fmt.Println("      still empty walks straight back into the same state, so \"let it back")
	fmt.Println("      slowly\" is a QUANTITATIVE claim -- the ramp has to be slower than the")
	fmt.Printf("      cache can refill, which here is %.0f keys per second against %d keys.\n",
		poolSize/dbService.Seconds(), keys)
	fmt.Printf("      Raise rampBackSeconds from %.0f and find the threshold yourself.\n", rampBackSeconds)
	fmt.Println("  (b) retry budget     removes topic 3's amplification and leaves the")
	fmt.Println("      sustaining effect untouched. \"We turned the retries off\" is a sentence")
	fmt.Println("      people say in incidents that are still ongoing twenty minutes later.")
	fmt.Println("  (c) load shedding    is the only one that breaks the FEEDBACK LOOP: it is")
	fmt.Println("      the only intervention that lets the ADMITTED requests finish inside")
	fmt.Println("      their deadline, which is the exact condition the cache needs to")
	fmt.Println("      refill. Watch its hit% climb while retry/req falls -- that is the loop")
	fmt.Println("      running backwards.")
	fmt.Println("  (d) restart the app  clears everything the process owns and nothing the")
	fmt.Println("      clients own. The amplifier is in the clients. They did not restart,")
	fmt.Println("      and in Go the `gor` column goes to nothing and then straight back up.")
	fmt.Println("\nIn HotOS '25 vocabulary, worth writing down for your own system before")
	fmt.Println("you need it:")
	fmt.Println("  trigger                 a cache flush, over in one millisecond")
	fmt.Println("  amplification mechanism naive retries, plus the miss rate going from 10%")
	fmt.Println("                          to 100% on a database that was 60% utilised")
	fmt.Println("  sustaining effect       fills only happen on completions that beat the")
	fmt.Println("                          caller's deadline, and under overload none do")
}
