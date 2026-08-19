// Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one Go
// process.
//
// You cannot serve more than capacity. The only choice you have is whether
// the excess is rejected in one millisecond or times out after thirty seconds
// having consumed a connection, a goroutine and a query. This file runs the
// same ramp seven times and changes only the admission decision.
//
// GO'S ADMISSION STORY is the cleanest composition of ideas in this layer,
// and it is two lines:
//
//	select {
//	case sem <- struct{}{}:   // admitted
//	case <-ctx.Done():        // 503, and the deadline did the deciding
//	}
//
// A buffered channel is a genuine counting semaphore, and because the
// acquire is a `select` with the request's own context, topic 2's DEADLINE
// performs the queue-wait rejection automatically: a request whose budget
// expires while it is queueing for admission is rejected without anybody
// writing timeout logic. `golang.org/x/sync/semaphore` packages exactly this
// with a context-aware Acquire; the channel version is here so the mechanism
// is visible rather than imported.
//
// What Go does NOT give you is the decision to make one. `net/http`'s
// zero-value server has no admission control of its own, `database/sql`
// leaves MaxOpenConns unlimited, and `go` never refuses to start a goroutine.
// Mode `none` below is not a strawman; it is the default shape of a Go
// service, and the `gor` column is what it costs.
//
// WHAT THIS DEMONSTRATES
//
//	A backend with 8 concurrent servers at 40ms each -- 200 requests/second
//	of capacity, measured the way topic 1 measures it -- behind six
//	different admission policies, at 80% and 130% of that capacity.
//
//	  none rho=0.8      the healthy baseline. Everything looks fine.
//	  none rho=1.3      an UNBOUNDED queue: goroutines parked on a channel
//	                    send, which is a queue with no name and no metric.
//	  static rho=1.3    a buffered channel of SHED_LIMIT plus a 50ms
//	                    queue-wait deadline -> 503 Retry-After.
//	  priority rho=1.3  the same limit, but /checkout (tier 0) may use all
//	                    of it and /search (tier 3) may not.
//	  adaptive rho=1.3  no configured number at all: a gradient controller
//	                    infers the limit from latency. Service time triples
//	                    half way through, on purpose.
//	  bulkhead          one pool of 8 shared between checkout and a slow
//	                    /report endpoint, then the SAME EIGHT split 6 + 2.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `p99_acc` and `goodput` in `none rho=1.3` against `static rho=1.3`.
//     Rejecting work should INCREASE the number of requests answered in
//     time. Check that rather than believe it.
//  2. `gor` in scenario 2. Every one of those goroutines is a request nobody
//     is going to answer in time, and Go will happily hold a million.
//  3. `tier0%` in the priority row.
//  4. `limit` in the adaptive row, before and after service time triples at
//     t=6s. Reason about Little's law before calling the controller broken:
//     the ideal in-flight limit for 8 servers is about 8 however long each
//     request takes. What must fall is the RATE, not the limit.
//  5. `reject_ms`, the cost of saying no.
//
// RUN
//
//	go run shedder.go
//
// Roughly two and a half minutes: seven scenarios of twenty seconds.
package main

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------- config
//
// Identical to python/shedder.py's constants: the six languages differ in how
// admission is expressed, not in what is being measured.

const (
	workers   = 8 // the real resource: 8 concurrent servers
	serviceMS = 40
	service   = serviceMS * time.Millisecond // 8 / 0.040 = 200 rps

	rhoLow  = 0.8
	rhoHigh = 1.3

	slo = 500 * time.Millisecond // later than this is not goodput
	// PERTURB_AT + minRTTReset + room to watch the adaptive limit come
	// back. At 12s the run ended during the dip and the return -- the
	// half that shows the reset working -- was invisible.
	durationS   = 20.0
	reportEvery = 2.0

	shedLimit  = 12                    // the knee's concurrency, measured
	shedWait   = 50 * time.Millisecond // queue-wait deadline before a 503
	tier3Limit = 10                    // priority: tier 3 may not use the last two
	tier0Share = 0.20

	adaptMin       = 2.0
	adaptMax       = 64.0
	adaptStart     = 10.0
	adaptWindow    = 250 * time.Millisecond
	adaptSmoothing = 0.2
	minRTTReset    = 5 * time.Second
	perturbAt      = 6.0
	perturbFactor  = 3

	checkoutRPS   = 120.0
	reportRPS     = 6.0
	reportService = 800 * time.Millisecond // 6 rps x 0.8s = 4.8 servers
	bulkCheckout  = 6                      // the same 8, split. Nothing added.
	bulkReport    = 2
)

const capacity = float64(workers) / (float64(serviceMS) / 1000.0)

// ----------------------------------------------------------- the backend

// The resource being protected. The channel IS the semaphore: `sem <-
// struct{}{}` blocks when it is full, which is correct backpressure, and
// which is also an UNBOUNDED queue of blocked senders when nothing in front
// of it is doing admission control. That is mode `none`.
type Backend struct {
	sem   chan struct{}
	inUse int64
	mu    sync.Mutex
}

func NewBackend(n int) *Backend { return &Backend{sem: make(chan struct{}, n)} }

func (b *Backend) Call(d time.Duration) {
	b.sem <- struct{}{}
	b.mu.Lock()
	b.inUse++
	b.mu.Unlock()
	time.Sleep(d)
	b.mu.Lock()
	b.inUse--
	b.mu.Unlock()
	<-b.sem
}

func (b *Backend) InUse() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.inUse
}

// ------------------------------------------------------ the gradient limit

// Netflix `concurrency-limits` in miniature, borrowed from TCP congestion
// control rather than from queueing theory: sample latency continuously,
// remember the minimum you have seen, raise the in-flight limit while current
// latency stays near that minimum, lower it when latency climbs. You never
// configure a number; the system discovers it, and rediscovers it when your
// code changes -- which matters because the hand-measured number from topic 1
// goes stale the day someone adds a join.
//
// The non-obvious parameter is the min-RTT RESET. Without it one fast sample
// from a quiet moment is remembered forever, so after a genuine permanent
// slowdown the gradient sticks near zero and the limit collapses to the floor
// and stays there. Vegas-style controllers all re-baseline.
type GradientLimit struct {
	mu         sync.Mutex
	limit      float64
	minRTT     time.Duration
	samples    []time.Duration
	lastUpdate time.Time
	lastReset  time.Time
}

func NewGradientLimit() *GradientLimit {
	return &GradientLimit{limit: adaptStart, minRTT: time.Hour}
}

func (g *GradientLimit) Limit() float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.limit
}

func (g *GradientLimit) Observe(rtt time.Duration) {
	g.mu.Lock()
	g.samples = append(g.samples, rtt)
	g.mu.Unlock()
}

func (g *GradientLimit) Update(now time.Time) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if now.Sub(g.lastUpdate) < adaptWindow {
		return
	}
	g.lastUpdate = now
	if len(g.samples) == 0 {
		return
	}
	sort.Slice(g.samples, func(i, j int) bool { return g.samples[i] < g.samples[j] })
	windowMin := g.samples[0]
	median := g.samples[len(g.samples)/2]
	g.samples = g.samples[:0]

	if now.Sub(g.lastReset) >= minRTTReset {
		g.minRTT = windowMin
		g.lastReset = now
	} else if windowMin < g.minRTT {
		g.minRTT = windowMin
	}

	// gradient < 1 means "we are queueing"; the limit comes down in
	// proportion. The sqrt term is the queue you are willing to keep, and is
	// what stops the limit collapsing to 1 the moment one request is slow.
	gradient := math.Max(0.5, math.Min(1.0, float64(g.minRTT)/math.Max(float64(median), 1)))
	target := g.limit*gradient + math.Sqrt(g.limit)
	g.limit = math.Max(adaptMin, math.Min(adaptMax,
		g.limit*(1-adaptSmoothing)+adaptSmoothing*target))
}

// ---------------------------------------------------------- the admission

// The fifty lines. Everything above the backend and below the router.
//
// The interesting part is what happens when you cannot have a permit
// immediately, and there are exactly three honest answers: wait a BOUNDED
// time (static, tier 0), refuse now (priority's tier 3, adaptive), or wait
// forever (mode `none`, which is what you ship when you do not decide).
type Admission struct {
	mode     string
	permits  chan struct{}
	mu       sync.Mutex
	inflight int64
	limiter  *GradientLimit
}

func NewAdmission(mode string) *Admission {
	a := &Admission{mode: mode, permits: make(chan struct{}, shedLimit)}
	if mode == "adaptive" {
		a.limiter = NewGradientLimit()
	}
	return a
}

func (a *Admission) Inflight() int64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.inflight
}

func (a *Admission) add(n int64) {
	a.mu.Lock()
	a.inflight += n
	a.mu.Unlock()
}

func (a *Admission) usesPermit(tier int) bool {
	// Everything except `none` and `adaptive` holds a channel permit for the
	// duration of the request, tier 3 included -- tier 3's extra restriction
	// is the reservation check in Admit, not a different resource.
	return a.mode != "none" && a.mode != "adaptive"
}

// Admit returns whether the request was admitted and how long the decision
// took. That second number is the cost of a rejection, and it belongs on a
// dashboard: a shedder that takes 50ms to say no has spent 10% of a 500ms
// budget on nothing.
func (a *Admission) Admit(tier int) (bool, time.Duration) {
	t0 := time.Now()

	switch {
	case a.mode == "none":
		// No admission control at all. Every request is accepted and waits in
		// the backend's queue for as long as that takes, and the queue has no
		// bound because nobody gave it one.
		a.add(1)
		return true, 0

	case a.mode == "adaptive":
		// Limit-based, no queueing: the controller's whole job is to hold the
		// limit at the value where waiting is unnecessary.
		if float64(a.Inflight()) >= a.limiter.Limit() {
			return false, time.Since(t0)
		}
		a.add(1)
		return true, time.Since(t0)

	case a.mode == "priority" && tier > 0:
		// Tier 3 gets try-acquire semantics against a LOWER limit -- the last
		// two permits are reserved for tier 0, and tier 3 does not get to
		// queue for the ones it may have. Shedding the same users everywhere
		// beats giving everybody a service that half works.
		if a.Inflight() >= tier3Limit {
			return false, time.Since(t0)
		}
		// A try-acquire in Go is a `select` with a `default`, and needing no
		// separate API for it is the point.
		select {
		case a.permits <- struct{}{}:
			a.add(1)
			return true, time.Since(t0)
		default:
			return false, time.Since(t0)
		}
	}

	// static, and priority's tier 0: a BOUNDED wait, expressed as a context.
	// This is the whole Go argument -- topic 2's deadline IS the queue-wait
	// rejection, and no timeout logic appears anywhere in this function.
	ctx, cancel := context.WithTimeout(context.Background(), shedWait)
	defer cancel()
	select {
	case a.permits <- struct{}{}:
		a.add(1)
		return true, time.Since(t0)
	case <-ctx.Done():
		return false, time.Since(t0)
	}
}

func (a *Admission) Release(usedPermit bool) {
	a.add(-1)
	if usedPermit {
		<-a.permits
	}
}

// ------------------------------------------------------------- the metrics

type Metrics struct {
	mu           sync.Mutex
	offered      int64
	accepted     int64
	rejected     int64
	goodput      int64
	latencies    []time.Duration
	latTier0     []time.Duration
	rejectCost   []time.Duration
	tier0Offered int64
	tier0Goodput int64
	wOffered     int64
	wAccepted    int64
	wRejected    int64
	wGoodput     int64
	wLat         []time.Duration
	rows         []Row
}

type Row struct {
	t, offered, accepted, reject, goodput float64
	p99                                   float64
	inflight                              int64
	limit                                 float64
	busy                                  int64
	goroutines                            int
}

func percentile(v []time.Duration, q float64) float64 {
	if len(v) == 0 {
		return 0
	}
	s := append([]time.Duration(nil), v...)
	sort.Slice(s, func(i, j int) bool { return s[i] < s[j] })
	idx := int(math.Ceil(q*float64(len(s)))) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(s) {
		idx = len(s) - 1
	}
	return float64(s[idx]) / float64(time.Millisecond)
}

// ------------------------------------------------------------- the server

type Server struct {
	mode            string
	m               *Metrics
	admission       *Admission
	checkoutBackend *Backend
	reportBackend   *Backend
	mu              sync.Mutex
	service         time.Duration
}

func NewServer(mode string, m *Metrics) *Server {
	admissionMode := mode
	if strings.HasPrefix(mode, "bulkhead") {
		admissionMode = "none" // bulkheads are structural, not admission
	}
	s := &Server{
		mode:      mode,
		m:         m,
		admission: NewAdmission(admissionMode),
		service:   service,
	}
	if mode == "bulkhead_split" {
		s.checkoutBackend = NewBackend(bulkCheckout)
		// The bulkhead: /report gets its own, smaller pool and is
		// structurally incapable of touching checkout's servers.
		s.reportBackend = NewBackend(bulkReport)
	} else {
		s.checkoutBackend = NewBackend(workers)
		s.reportBackend = s.checkoutBackend
	}
	return s
}

func (s *Server) Service() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.service
}

func (s *Server) SetService(d time.Duration) {
	s.mu.Lock()
	s.service = d
	s.mu.Unlock()
}

func (s *Server) Handle(tier int, isReport bool) {
	t0 := time.Now()
	m := s.m
	m.mu.Lock()
	m.offered++
	m.wOffered++
	if tier == 0 {
		m.tier0Offered++
	}
	m.mu.Unlock()

	usedPermit := s.admission.usesPermit(tier)
	admitted, cost := s.admission.Admit(tier)
	if !admitted {
		m.mu.Lock()
		m.rejected++
		m.wRejected++
		m.rejectCost = append(m.rejectCost, cost)
		m.mu.Unlock()
		// A 503 with Retry-After, having touched nothing. That is the entire
		// product.
		return
	}

	m.mu.Lock()
	m.accepted++
	m.wAccepted++
	m.mu.Unlock()

	backend := s.checkoutBackend
	d := s.Service()
	if isReport {
		backend = s.reportBackend
		d = reportService
	}
	backend.Call(d)
	s.admission.Release(usedPermit)

	latency := time.Since(t0)
	m.mu.Lock()
	m.latencies = append(m.latencies, latency)
	m.wLat = append(m.wLat, latency)
	if tier == 0 {
		m.latTier0 = append(m.latTier0, latency)
	}
	if latency <= slo {
		m.goodput++
		m.wGoodput++
		if tier == 0 {
			m.tier0Goodput++
		}
	}
	m.mu.Unlock()
	if s.admission.limiter != nil {
		s.admission.limiter.Observe(latency)
	}
}

// ------------------------------------------------------------- the harness

type Scenario struct {
	key, mode, label, note string
	rate                   float64
	tier0Share             float64
	reportRPS              float64
}

func runScenario(sc Scenario) *Metrics {
	m := &Metrics{}
	server := NewServer(sc.mode, m)
	rng := rand.New(rand.NewSource(20250505))

	begin := time.Now()
	lastReport := begin
	at := begin
	nextReport := begin
	perturbed := false

	for {
		if at.Sub(begin).Seconds() > durationS {
			break
		}
		at = at.Add(time.Duration(rng.ExpFloat64() / sc.rate * float64(time.Second)))
		if d := time.Until(at); d > 0 {
			time.Sleep(d)
		}
		now := time.Now()
		t := now.Sub(begin).Seconds()

		if sc.mode == "adaptive" && !perturbed && t >= perturbAt {
			// "Then change service time by 3x at runtime and watch it
			// re-converge." Nobody redeployed. Nobody changed the limit.
			server.SetService(service * perturbFactor)
			perturbed = true
		}

		tier := 3
		if rng.Float64() < sc.tier0Share {
			tier = 0
		}
		go server.Handle(tier, false)

		// The slow endpoint, offered as its own open-model stream rather than
		// as a fraction of checkout: reports do not arrive because checkouts
		// do.
		// Note the `for` and the `nextReport.Add`, not an `if` and a
		// `now.Add`: this is an ABSOLUTE schedule, exactly like `at` above.
		// Rescheduling from `now` throws away the lateness of every arrival,
		// and since the check only runs when a checkout arrives, the lateness
		// is real and it grows with load -- so the relative version quietly
		// offers LESS /report the more overloaded the server gets, which is
		// backwards and hides the very effect this scenario exists to show.
		for sc.reportRPS > 0 && !now.Before(nextReport) {
			nextReport = nextReport.Add(time.Duration(rng.ExpFloat64() / sc.reportRPS * float64(time.Second)))
			go server.Handle(3, true)
		}

		if server.admission.limiter != nil {
			server.admission.limiter.Update(now)
		}

		if now.Sub(lastReport).Seconds() >= reportEvery {
			span := now.Sub(lastReport).Seconds()
			limit := float64(shedLimit)
			if server.admission.limiter != nil {
				limit = server.admission.limiter.Limit()
			}
			m.mu.Lock()
			row := Row{
				t:          t,
				offered:    sc.rate,
				accepted:   float64(m.wAccepted) / span,
				reject:     100 * float64(m.wRejected) / math.Max(1, float64(m.wOffered)),
				goodput:    float64(m.wGoodput) / span,
				p99:        percentile(m.wLat, 0.99),
				inflight:   server.admission.Inflight(),
				limit:      limit,
				busy:       server.checkoutBackend.InUse(),
				goroutines: runtime.NumGoroutine(),
			}
			m.rows = append(m.rows, row)
			m.wOffered, m.wAccepted, m.wRejected, m.wGoodput = 0, 0, 0, 0
			m.wLat = m.wLat[:0]
			m.mu.Unlock()
			lastReport = now
		}
	}

	// Let the tail drain: requests still in flight at the end of the window
	// are neither goodput nor rejections, and counting them either way would
	// be a lie about the run.
	time.Sleep(time.Second)
	return m
}

// -------------------------------------------------------------- reporting

const header = "      t   offered  accepted  reject%   goodput  p99_acc  inflight  limit   busy    gor"

type result struct {
	key                                  string
	offered, accepted, rejected, goodput float64
	p99, p99t0, tier0, rejectMS          float64
}

func render(sc Scenario, m *Metrics) result {
	fmt.Printf("\n=== %s ===\n", sc.label)
	fmt.Printf("    %s\n", sc.note)
	fmt.Println(header)
	fmt.Println(strings.Repeat("-", len(header)))
	for _, r := range m.rows {
		mark := ""
		if sc.mode == "adaptive" && math.Abs(r.t-perturbAt) < reportEvery/2 {
			mark = "  <-- service time x3"
		}
		fmt.Printf("  %5.1f %9.1f %9.1f %8.0f %9.1f %8.0f %9d %6.1f %6d %6d%s\n",
			r.t, r.offered, r.accepted, r.reject, r.goodput, r.p99, r.inflight,
			r.limit, r.busy, r.goroutines, mark)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	var rejectMS float64
	for _, d := range m.rejectCost {
		rejectMS += float64(d) / float64(time.Millisecond)
	}
	if len(m.rejectCost) > 0 {
		rejectMS /= float64(len(m.rejectCost))
	}
	out := result{
		key:      sc.key,
		offered:  float64(m.offered) / durationS,
		accepted: float64(m.accepted) / durationS,
		rejected: 100 * float64(m.rejected) / math.Max(1, float64(m.offered)),
		goodput:  float64(m.goodput) / durationS,
		p99:      percentile(m.latencies, 0.99),
		p99t0:    percentile(m.latTier0, 0.99),
		tier0:    100 * float64(m.tier0Goodput) / math.Max(1, float64(m.tier0Offered)),
		rejectMS: rejectMS,
	}
	fmt.Printf("mode=%s  offered=%.0f  accepted=%.0f  rejected=%.0f%%  goodput=%.0f  "+
		"p99_accepted=%.0fms  tier0_success=%.0f%%  p99_tier0=%.0fms  reject_ms=%.1f\n",
		out.key, out.offered, out.accepted, out.rejected, out.goodput, out.p99,
		out.tier0, out.p99t0, out.rejectMS)
	return out
}

func main() {
	fmt.Println("Load shedding, backpressure and bulkheads: the same ramp, seven admission policies.")
	fmt.Printf("Backend capacity is %d/%.3f = %.0f rps, measured the way topic 1 measures it. "+
		"Anything above that is not servable by anybody.\n",
		workers, float64(serviceMS)/1000.0, capacity)
	fmt.Printf("Offered load is %.1fx and %.1fx that number. Goodput counts responses inside a "+
		"%.0fms SLO; p99_acc is the p99 of ACCEPTED requests, p99_tier0 the p99 of tier-0 "+
		"(/checkout) requests alone.\n", rhoLow, rhoHigh, float64(slo.Milliseconds()))
	fmt.Printf("The static limit is %d in flight with a %dms queue-wait deadline. The adaptive "+
		"one is not configured at all.\n", shedLimit, shedWait.Milliseconds())

	scenarios := []Scenario{
		{"none_0.8", "none", "1 none, rho=0.8",
			"The healthy baseline. Nothing is rejected because nothing needs to be.",
			rhoLow * capacity, tier0Share, 0},
		{"none_1.3", "none", "2 none, rho=1.3",
			"An unbounded queue at 130% of capacity. Watch p99_acc climb while reject% stays at zero and `gor` runs away.",
			rhoHigh * capacity, tier0Share, 0},
		{"static_1.3", "static", "3 static shedding, rho=1.3",
			fmt.Sprintf("A buffered channel of %d plus a %dms wait deadline -> 503 Retry-After.",
				shedLimit, shedWait.Milliseconds()),
			rhoHigh * capacity, tier0Share, 0},
		{"priority_1.3", "priority", "4 priority shedding, rho=1.3",
			fmt.Sprintf("/checkout is tier 0 (%.0f%% of traffic) and may use all %d; /search is tier 3 and may use %d.",
				tier0Share*100, shedLimit, tier3Limit),
			rhoHigh * capacity, tier0Share, 0},
		{"adaptive_1.3", "adaptive", "5 adaptive shedding, rho=1.3",
			fmt.Sprintf("No configured limit. Service time triples at t=%.0fs with nobody redeploying anything.", perturbAt),
			rhoHigh * capacity, tier0Share, 0},
		{"bulk_shared", "bulkhead_shared", "6 bulkhead: one shared pool",
			fmt.Sprintf("%.0f rps of checkout plus %.0f rps of %dms /report, all %d servers shared.",
				checkoutRPS, reportRPS, reportService.Milliseconds(), workers),
			checkoutRPS, 1.0, reportRPS},
		{"bulk_split", "bulkhead_split",
			fmt.Sprintf("7 bulkhead: the same 8, split %d + %d", bulkCheckout, bulkReport),
			"Nothing is added. /report is now structurally incapable of touching checkout's servers.",
			checkoutRPS, 1.0, reportRPS},
	}

	var results []result
	labels := map[string]string{}
	for _, sc := range scenarios {
		m := runScenario(sc)
		results = append(results, render(sc, m))
		labels[sc.key] = sc.label
	}

	fmt.Println("\n" + strings.Repeat("=", 104))
	fmt.Printf("%-38s%8s%9s%8s%8s%8s%9s%10s%10s\n",
		"mode", "offered", "accepted", "goodput", "p99_acc", "p99_t0", "reject%", "tier0_ok%", "reject_ms")
	fmt.Println(strings.Repeat("-", 104))
	byKey := map[string]result{}
	for _, r := range results {
		byKey[r.key] = r
		fmt.Printf("%-38s%8.0f%9.0f%8.0f%8.0f%8.0f%9.0f%10.0f%10.1f\n",
			labels[r.key], r.offered, r.accepted, r.goodput, r.p99, r.p99t0,
			r.rejected, r.tier0, r.rejectMS)
	}

	fmt.Println("\nRead rows 2 and 3 as one comparison and everything else is commentary:")
	fmt.Printf("  none     rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%\n",
		byKey["none_1.3"].goodput, byKey["none_1.3"].p99, byKey["none_1.3"].rejected)
	fmt.Printf("  static   rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%\n",
		byKey["static_1.3"].goodput, byKey["static_1.3"].p99, byKey["static_1.3"].rejected)
	fmt.Println("Same offered load, same backend, same 200 rps of capacity. The only")
	fmt.Println("difference is that one of them said no.")
	fmt.Println("\nThe bulkhead pair is the other comparison worth making, and it is the one")
	fmt.Println("that adds nothing at all:")
	fmt.Printf("  shared pool   checkout goodput %6.0f rps   checkout p99 %6.0f ms\n",
		byKey["bulk_shared"].goodput, byKey["bulk_shared"].p99t0)
	fmt.Printf("  split %d + %d   checkout goodput %6.0f rps   checkout p99 %6.0f ms\n",
		bulkCheckout, bulkReport, byKey["bulk_split"].goodput, byKey["bulk_split"].p99t0)
	fmt.Println("The split pool has FEWER servers available to checkout, and the boundary is")
	fmt.Printf("worth more than the two servers it costs -- because /report at %.0f rps x %dms\n",
		reportRPS, reportService.Milliseconds())
	fmt.Printf("wants %.1f servers' worth of the shared pool and takes them from whoever asks\n",
		reportRPS*reportService.Seconds())
	fmt.Printf("last. Note what it costs: /report itself can now only ever get %.1f rps through.\n",
		float64(bulkReport)/reportService.Seconds())
	fmt.Println("That is the bargain, and you should be able to say it out loud before you make it.")
	fmt.Println("\nThree things to carry out of this file:")
	fmt.Println("  1. An unbounded queue does not smooth load. It converts an availability")
	fmt.Println("     problem into a latency problem and hides it until latency exceeds every")
	fmt.Println("     timeout in the system at once.")
	fmt.Println("  2. Shed on WAIT TIME, not on queue length. Length is meaningless without a")
	fmt.Println("     service time attached: the same length is a healthy queue for a 1ms")
	fmt.Println("     handler and a catastrophe for a 500ms one.")
	fmt.Println("  3. In Go specifically: `select` on a buffered channel and a context is the")
	fmt.Println("     entire mechanism, and the reason to reach for x/sync/semaphore instead is")
	fmt.Println("     weighted acquisition, not correctness. The hard part was never the code.")
}
