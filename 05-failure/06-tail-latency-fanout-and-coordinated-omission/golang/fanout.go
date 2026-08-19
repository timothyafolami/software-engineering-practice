// Layer 5 - Topic 6: fan-out tails, hedging, and coordinated omission (Go).
//
// One process holds a gateway, up to 50 backends and BOTH load models, so the
// only thing missing versus the containerised version is real network
// variance. Everything else -- the arithmetic of percentiles under fan-out,
// the cost of hedging, and the lie a closed-loop generator tells -- is here.
// Same constants, same phases and same columns as the Python sibling in
// ../python/fanout.py, so the tables line up; the mechanism below is the Go-specific part.
//
// # WHY GO GETS THE CLEANEST VERSION OF THIS TOPIC
//
// Hedging is only cheap if the copy you gave up on actually stops. In Go that
// is the default rather than the careful path: every leg runs under its own
// context.Context, `cancel()` on the loser makes its `<-ctx.Done()` fire, and
// the backend releases the worker it was holding on the way out. Python has to
// remember `.cancel()`, Node has to wire up an AbortController that stops only
// its own side, Rust gets it from dropping the future -- Go gets it from a
// value that was already being threaded through the call for deadline reasons
// (topic 2), and it genuinely propagates over the wire to a Go server on the
// far end. Phase B measures the difference between cancelling and not, so the
// claim is a column rather than a paragraph.
//
// The fan-out itself is deliberately hand-rolled from a WaitGroup and a
// channel rather than errgroup: layer 5's Go programs are single files run
// with `go run`, and `golang.org/x/sync/errgroup` would need a go.mod for no
// mechanism this topic is about. `errgroup.Group` is the same shape with
// error short-circuiting attached -- and its `WithContext` variant is exactly
// the cancel-the-siblings discipline modelled below.
//
// WHAT THIS DEMONSTRATES
//
//	Phase A  A gateway fans out to K identical backends and waits for all of
//	         them, K in {1,2,5,10,20,50}, against two service-time
//	         distributions that share a p50 of 10ms and a p99 of 200ms:
//	         log-normal, and bimodal with a 1% slow mode. Backends are
//	         deliberately unsaturated here, so the only thing acting is the
//	         arithmetic.
//	Phase B  Hedging at the MEASURED backend p95, under a 5% token bucket,
//	         run three ways: no hedge, hedge with the loser's context
//	         cancelled, and hedge with the loser left running.
//	Phase C  The same server, the same nominal rate, measured twice: once by
//	         an open-model generator (arrivals on a fixed schedule) and once
//	         by a closed-loop one (a fixed number of virtual users, each
//	         waiting for a response before sending again).
//
// WHAT TO LOOK FOR IN THE OUTPUT
//
//  1. Phase A's `measured` column against `predicted`, which is 1 - 0.99^K
//     and is arithmetic, not measurement. If the two disagree badly, read the
//     README's "what would mean the experiment is broken" list before you
//     believe either.
//  2. The two distributions' `e2e_p50` columns diverging as K grows while
//     their tail columns stay together. Same p50, same p99, same tail
//     probability, different shape -- and the shape is what the user feels.
//  3. Phase B's `backend_rps` and `+load` columns next to `e2e_p99`. Hedging
//     is not free; the point of the column is to quantify what it cost. Then
//     read the cancelled and NOT-cancelled rows against each other: same
//     policy, same budget, and one of them is a retry storm.
//  4. Phase C's two p99s and the two histograms underneath them. The closed
//     loop also prints an omission-corrected p99, measured from when each
//     request was DUE rather than when the generator got round to sending it.
//     The gap between the raw and the corrected p99 is the size of the lie.
//
// A NOTE ON THE TIMER FLOOR: Go's timers resolve to roughly a millisecond on
// macOS and the p50 here is 10ms. Read the calibration block first -- it
// prints what the backend distribution actually measured as, not what it was
// configured as, and every later table is relative to those measured numbers.
//
// RUN
//
//	go run fanout.go
//
// Standard library only. Takes roughly three minutes.
package main

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ------------------------------------------------------------------ config

const (
	backendP50Ms = 10.0
	tailRatio    = 20.0 // p99 / p50, per the README's specification
	z99          = 2.3263478740408408
	z95          = 1.6448536269514722

	samplesPerCell            = 1500
	maxRate                   = 400.0    // requests/s ceiling for a cell
	maxBackendCallsPerS       = 10_000.0 // keeps the scheduler from being the tail
	statWorkers               = 512      // phase A: backends must NOT queue
	hedgeBudgetRatio          = 0.05     // "at most 5% of backend calls may hedge"
	hedgeBucketCapacity       = 20.0
	coK                       = 10
	coWorkers                 = 4 // phase C: backends that CAN saturate
	coRho                     = 0.90
	coSeconds                 = 25.0
	calibSamples              = 20_000
	calibBatch                = 500
	seed                int64 = 20260819
)

var (
	lognormalSigma  = math.Log(tailRatio) / z99
	tailThresholdMs = backendP50Ms * tailRatio // 200.0ms, by construction
	kValues         = []int{1, 2, 5, 10, 20, 50}
	hedgeK          = []int{10, 50}
)

// pctile is the nearest-rank percentile: no interpolation, and above all no
// averaging of percentiles, which is the arithmetic sin this topic is about.
func pctile(sorted []float64, q float64) float64 {
	if len(sorted) == 0 {
		return math.NaN()
	}
	idx := int(math.Ceil(q*float64(len(sorted)))) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

// Rng is a mutex-guarded *rand.Rand. math/rand's global source is safe for
// concurrent use but cannot be seeded reproducibly per program; this keeps the
// seed explicit. Contention is irrelevant at these rates -- the numbers are
// drawn once per backend call and the calls sleep for milliseconds.
type Rng struct {
	mu sync.Mutex
	r  *rand.Rand
}

func newRng(s int64) *Rng { return &Rng{r: rand.New(rand.NewSource(s))} }

func (g *Rng) norm() float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.r.NormFloat64()
}

func (g *Rng) unit() float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.r.Float64()
}

func (g *Rng) expo(rate float64) float64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.r.ExpFloat64() / rate
}

// ----------------------------------------------------------- distributions

type Dist interface {
	Name() string
	Sample(*Rng) time.Duration
	P95Ms() float64
}

// LogNormal: p50 = exp(mu); p99 = exp(mu + z99*sigma). Sigma is chosen so that
// p99/p50 is exactly 20.
type LogNormal struct {
	mu, sigma float64
}

func (d LogNormal) Name() string { return "lognormal" }
func (d LogNormal) Sample(g *Rng) time.Duration {
	return time.Duration(math.Exp(d.mu+d.sigma*g.norm()) * float64(time.Second))
}
func (d LogNormal) P95Ms() float64 { return math.Exp(d.mu+z95*d.sigma) * 1000.0 }

// Bimodal: 99% fast and tight, 1% slow -- and the slow mode's FLOOR is the p99.
//
// Putting the slow mode's minimum exactly at 20x the p50 is what makes
// P(leg > 200ms) equal 1% on the nose, so the same tail threshold works for
// both distributions and `predicted` stays honest. A slow mode centred on
// 200ms instead would put only half of 1% above the threshold, and the
// predicted/measured comparison would be comparing two different things.
type Bimodal struct {
	fastMu, fastSigma float64
	slowFloor         float64 // seconds
	slowExtra         float64 // seconds
	pSlow             float64
}

func (d Bimodal) Name() string { return "bimodal" }
func (d Bimodal) Sample(g *Rng) time.Duration {
	if g.unit() < d.pSlow {
		return time.Duration((d.slowFloor + g.expo(1.0/d.slowExtra)) * float64(time.Second))
	}
	return time.Duration(math.Exp(d.fastMu+d.fastSigma*g.norm()) * float64(time.Second))
}
func (d Bimodal) P95Ms() float64 { return math.Exp(d.fastMu+z95*d.fastSigma) * 1000.0 }

// --------------------------------------------------------------- the server

// Backend is one backend: a fixed number of workers, a queue, and a service
// time. `workers` is what makes phase C possible. Set it high and the backend
// is a pure delay generator, which is what phase A wants; set it to 4 and the
// thing has a capacity, a queue in front of it, and therefore an opinion about
// how fast you are allowed to send.
type Backend struct {
	sem       chan struct{}
	started   int64
	completed int64
	cancelled int64
	busyNs    int64 // service time actually consumed, cancelled copies included
}

func newBackend(workers int) *Backend {
	return &Backend{sem: make(chan struct{}, workers)}
}

// call blocks for one service time, or returns early if ctx is cancelled.
//
// The two `<-b.sem` on the way out are the whole reason cancelling the loser
// of a hedge is cheap: a cancelled call gives its worker back. Over a real
// network that is only true if the far end hears about the cancellation --
// topic 2's zombie requests, one layer down, and the reason Go's automatic
// context propagation is worth more here than it looks.
func (b *Backend) call(ctx context.Context, d Dist, g *Rng) error {
	atomic.AddInt64(&b.started, 1)
	select {
	case b.sem <- struct{}{}: // queueing, if there is any
	case <-ctx.Done():
		atomic.AddInt64(&b.cancelled, 1)
		return ctx.Err()
	}
	t := time.NewTimer(d.Sample(g))
	defer t.Stop()
	t0 := time.Now()
	select {
	case <-t.C:
		atomic.AddInt64(&b.busyNs, int64(time.Since(t0)))
		<-b.sem
		atomic.AddInt64(&b.completed, 1)
		return nil
	case <-ctx.Done():
		// A cancelled call stops consuming service time HERE. That is the
		// difference phase B's svc_ms/req column exists to price.
		atomic.AddInt64(&b.busyNs, int64(time.Since(t0)))
		<-b.sem
		atomic.AddInt64(&b.cancelled, 1)
		return ctx.Err()
	}
}

// TokenBucket is the gRPC/Envoy-shaped retry throttle: every primary call
// earns `ratio` of a token, every hedge spends a whole one. Steady state is
// therefore "hedges are at most `ratio` of primary calls", with `capacity`
// worth of burst. This is the difference between a hedge and a retry storm
// with better branding.
type TokenBucket struct {
	mu       sync.Mutex
	ratio    float64
	capacity float64
	tokens   float64
}

func newTokenBucket(ratio, capacity float64) *TokenBucket {
	return &TokenBucket{ratio: ratio, capacity: capacity, tokens: capacity}
}

func (t *TokenBucket) onPrimary() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.tokens = math.Min(t.capacity, t.tokens+t.ratio)
}

func (t *TokenBucket) take() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.tokens >= 1.0 {
		t.tokens -= 1.0
		return true
	}
	return false
}

// Gateway fans out to K backends and waits for every one of them.
type Gateway struct {
	backends     []*Backend
	dist         Dist
	rng          *Rng
	hedgeDelay   time.Duration // 0 disables hedging
	cancelLosers bool
	bucket       *TokenBucket

	legs         int64
	legsHedged   int64
	budgetDenied int64

	orphans sync.WaitGroup // uncancelled hedges, so the run can wait them out
}

func newGateway(backends []*Backend, d Dist, g *Rng, hedgeDelay time.Duration, cancelLosers bool) *Gateway {
	return &Gateway{
		backends: backends, dist: d, rng: g,
		hedgeDelay: hedgeDelay, cancelLosers: cancelLosers,
		bucket: newTokenBucket(hedgeBudgetRatio, hedgeBucketCapacity),
	}
}

type attempt struct {
	done   chan error
	cancel context.CancelFunc
}

func (gw *Gateway) start(ctx context.Context, b *Backend) *attempt {
	c, cancel := context.WithCancel(ctx)
	ch := make(chan error, 1)
	go func() { ch <- b.call(c, gw.dist, gw.rng) }()
	return &attempt{done: ch, cancel: cancel}
}

// leg runs one leg of the fan-out and reports whether it fired a hedge.
func (gw *Gateway) leg(ctx context.Context, b *Backend) bool {
	atomic.AddInt64(&gw.legs, 1)
	if gw.hedgeDelay <= 0 {
		_ = b.call(ctx, gw.dist, gw.rng)
		return false
	}

	gw.bucket.onPrimary()
	a1 := gw.start(ctx, b)
	timer := time.NewTimer(gw.hedgeDelay)
	select {
	case <-a1.done:
		timer.Stop()
		a1.cancel()
		return false
	case <-timer.C:
	}

	// Past the measured p95 and still nothing. Hedge -- if the budget says so.
	if !gw.bucket.take() {
		atomic.AddInt64(&gw.budgetDenied, 1)
		<-a1.done
		a1.cancel()
		return false
	}

	atomic.AddInt64(&gw.legsHedged, 1)
	a2 := gw.start(ctx, b)
	var loser *attempt
	select {
	case <-a1.done:
		loser = a2
	case <-a2.done:
		loser = a1
	}
	if gw.cancelLosers {
		loser.cancel() // the line everybody forgets, and Go's is one call
	} else {
		// The bug, made visible: the loser keeps its worker for its full
		// service time. Track it so the run can wait for it to finish being
		// expensive rather than measuring a half-drained backend.
		gw.orphans.Add(1)
		go func(a *attempt) { <-a.done; a.cancel(); gw.orphans.Done() }(loser)
	}
	return true
}

// handle is the fan-out: K legs, wait for all of them, so end-to-end latency
// IS the max of the legs. That is not an implementation detail here; it is the
// experiment.
func (gw *Gateway) handle(ctx context.Context, k int) bool {
	var wg sync.WaitGroup
	hedged := make([]bool, k)
	for i := 0; i < k; i++ {
		wg.Add(1)
		go func(i int) { defer wg.Done(); hedged[i] = gw.leg(ctx, gw.backends[i]) }(i)
	}
	wg.Wait()
	for _, h := range hedged {
		if h {
			return true
		}
	}
	return false
}

// ------------------------------------------------------------- the harness

// Cell is one measured configuration.
type Cell struct {
	mu             sync.Mutex
	latMs          []float64
	lateMs         []float64
	correctedMs    []float64
	arrivalWall    time.Duration
	hedgedRequests int
	backendStarted int64
	backendBusyMs  float64
	gw             *Gateway
}

type summary struct {
	n                      int
	p50, p99, max, tail    float64
	lateP99, backendRps    float64
	callsPerReq, hedgeRate float64
	svcMsPerReq            float64 // backend service time consumed per request
}

func (c *Cell) summary() summary {
	lat := append([]float64(nil), c.latMs...)
	sort.Float64s(lat)
	late := append([]float64(nil), c.lateMs...)
	sort.Float64s(late)
	over := 0
	for _, v := range lat {
		if v > tailThresholdMs {
			over++
		}
	}
	n := len(lat)
	den := float64(max(1, n))
	s := summary{
		n: n, p50: pctile(lat, 0.50), p99: pctile(lat, 0.99),
		tail: 100.0 * float64(over) / den, lateP99: pctile(late, 0.99),
		backendRps:  float64(c.backendStarted) / math.Max(1e-9, c.arrivalWall.Seconds()),
		callsPerReq: float64(c.backendStarted) / den,
		hedgeRate:   100.0 * float64(c.hedgedRequests) / den,
		svcMsPerReq: c.backendBusyMs / den,
	}
	if n > 0 {
		s.max = lat[n-1]
	}
	return s
}

// runOpenCell is the open model: arrivals happen on a precomputed schedule,
// full stop.
//
// The schedule is absolute and computed before the run, so the generator's own
// overhead cannot leak into it -- a generator that sleeps for expovariate(rate)
// BETWEEN dispatches slows down exactly when the server does, and has quietly
// become the closed-loop generator this topic is about. Latency is measured
// from each request's DUE time, not from when the dispatch loop got round to
// it, for the same reason.
func runOpenCell(k int, d Dist, workers int, rate float64, n int, hedgeDelay time.Duration, cancelLosers bool) *Cell {
	rngArr := rand.New(rand.NewSource(seed))
	backends := make([]*Backend, k)
	for i := range backends {
		backends[i] = newBackend(workers)
	}
	gw := newGateway(backends, d, newRng(seed+1), hedgeDelay, cancelLosers)
	cell := &Cell{gw: gw}
	ctx := context.Background()

	schedule := make([]time.Duration, n)
	acc := 0.0
	for i := 0; i < n; i++ {
		acc += rngArr.ExpFloat64() / rate
		schedule[i] = time.Duration(acc * float64(time.Second))
	}

	t0 := time.Now()
	var wg sync.WaitGroup
	for _, off := range schedule {
		due := t0.Add(off)
		if wait := time.Until(due); wait > 0 {
			time.Sleep(wait)
		}
		cell.mu.Lock()
		cell.lateMs = append(cell.lateMs, float64(time.Since(due).Microseconds())/1000.0)
		cell.mu.Unlock()
		wg.Add(1)
		go func(due time.Time) {
			defer wg.Done()
			hedged := gw.handle(ctx, k)
			lat := float64(time.Since(due).Microseconds()) / 1000.0
			cell.mu.Lock()
			cell.latMs = append(cell.latMs, lat)
			if hedged {
				cell.hedgedRequests++
			}
			cell.mu.Unlock()
		}(due)
	}
	cell.arrivalWall = time.Since(t0)

	// Everything in flight at the end is counted. Dropping it would be its own
	// flavour of omission, and the requests still running are the slow ones.
	wg.Wait()
	gw.orphans.Wait() // let uncancelled hedges finish being expensive
	for _, b := range backends {
		cell.backendStarted += atomic.LoadInt64(&b.started)
		cell.backendBusyMs += float64(atomic.LoadInt64(&b.busyNs)) / 1e6
	}
	return cell
}

// runClosedCell is the closed model: `vus` virtual users, each waiting before
// sending again. This is `ramping-vus`, the executor the rest of this layer
// forbids. It is permitted here and only here, because seeing it lie is the
// point.
//
// Two numbers are recorded per request. The raw one is what a closed-loop
// generator reports: finish minus send. The corrected one is finish minus the
// time the request was DUE under the nominal schedule -- because a VU stuck
// waiting on a slow response is not sending the requests it owed, and those
// unsent requests are exactly the ones that would have been slow.
func runClosedCell(k int, d Dist, workers int, vus int, nominalRate float64, seconds float64) *Cell {
	backends := make([]*Backend, k)
	for i := range backends {
		backends[i] = newBackend(workers)
	}
	gw := newGateway(backends, d, newRng(seed+1), 0, true)
	cell := &Cell{gw: gw}
	ctx := context.Background()
	perVUInterval := float64(vus) / nominalRate

	t0 := time.Now()
	deadline := t0.Add(time.Duration(seconds * float64(time.Second)))
	var wg sync.WaitGroup
	for v := 0; v < vus; v++ {
		wg.Add(1)
		go func(v int) {
			defer wg.Done()
			for j := 0; ; j++ {
				start := time.Now()
				if !start.Before(deadline) {
					return
				}
				dueOff := float64(v)/nominalRate + float64(j)*perVUInterval
				due := t0.Add(time.Duration(dueOff * float64(time.Second)))
				gw.handle(ctx, k)
				fin := time.Now()
				raw := float64(fin.Sub(start).Microseconds()) / 1000.0
				from := start
				if due.Before(from) {
					from = due
				}
				corrected := float64(fin.Sub(from).Microseconds()) / 1000.0
				cell.mu.Lock()
				cell.latMs = append(cell.latMs, raw)
				cell.correctedMs = append(cell.correctedMs, corrected)
				cell.mu.Unlock()
			}
		}(v)
	}
	wg.Wait()
	cell.arrivalWall = time.Since(t0)
	for _, b := range backends {
		cell.backendStarted += atomic.LoadInt64(&b.started)
		cell.backendBusyMs += float64(atomic.LoadInt64(&b.busyNs)) / 1e6
	}
	return cell
}

// calibrate measures ONE backend directly. Everything downstream is relative
// to this, including the hedge delay in phase B.
func calibrate(d Dist, workers, n int) map[string]float64 {
	g := newRng(seed + 7)
	b := newBackend(workers)
	lat := make([]float64, 0, n)
	var mu sync.Mutex
	ctx := context.Background()
	for start := 0; start < n; start += calibBatch {
		batch := min(calibBatch, n-start)
		var wg sync.WaitGroup
		for i := 0; i < batch; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				t0 := time.Now()
				_ = b.call(ctx, d, g)
				v := float64(time.Since(t0).Microseconds()) / 1000.0
				mu.Lock()
				lat = append(lat, v)
				mu.Unlock()
			}()
		}
		wg.Wait()
	}
	sort.Float64s(lat)
	sum, over := 0.0, 0
	for _, v := range lat {
		sum += v
		if v > tailThresholdMs {
			over++
		}
	}
	return map[string]float64{
		"p50": pctile(lat, 0.50), "p95": pctile(lat, 0.95), "p99": pctile(lat, 0.99),
		"mean": sum / float64(len(lat)), "over": 100.0 * float64(over) / float64(len(lat)),
	}
}

// ----------------------------------------------------------------- output

var histEdges = []float64{0, 5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120}

func histogram(label string, vals []float64) {
	if len(vals) == 0 {
		return
	}
	counts := make([]int, len(histEdges))
	for _, v := range vals {
		placed := false
		for i := 0; i < len(histEdges)-1; i++ {
			if histEdges[i] <= v && v < histEdges[i+1] {
				counts[i]++
				placed = true
				break
			}
		}
		if !placed {
			counts[len(counts)-1]++
		}
	}
	peak := 1
	for _, c := range counts {
		if c > peak {
			peak = c
		}
	}
	fmt.Printf("  %s   (n=%d)\n", label, len(vals))
	for i := range histEdges {
		var rangeLabel string
		if i+1 < len(histEdges) {
			rangeLabel = fmt.Sprintf("%6.0f - %6.0f ms", histEdges[i], histEdges[i+1])
		} else {
			rangeLabel = fmt.Sprintf("%6.0f +%8sms", histEdges[i], "")
		}
		bar := strings.Repeat("#", int(math.Round(40.0*float64(counts[i])/float64(peak))))
		fmt.Printf("    %s |%-40s| %6d\n", rangeLabel, bar, counts[i])
	}
}

func rule(title string) {
	fmt.Println()
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println(title)
	fmt.Println(strings.Repeat("=", 78))
}

func cellRate(k int) float64 { return math.Min(maxRate, maxBackendCallsPerS/float64(k)) }

// ------------------------------------------------------------------- main

func main() {
	lognormal := LogNormal{mu: math.Log(backendP50Ms / 1000.0), sigma: lognormalSigma}
	bimodal := Bimodal{
		fastMu:    math.Log(backendP50Ms / 1000.0),
		fastSigma: 0.15, // tight: the fast mode never reaches the floor
		slowFloor: tailThresholdMs / 1000.0,
		slowExtra: 0.050,
		pSlow:     0.01,
	}
	dists := []Dist{lognormal, bimodal}

	rule("Layer 5 - Topic 6: fan-out, hedging and coordinated omission (Go)")
	fmt.Printf("  backend p50 configured   %.1f ms\n", backendP50Ms)
	fmt.Printf("  backend p99 configured   %.1f ms   (p99/p50 = %.0fx, log-normal sigma = %.4f)\n",
		tailThresholdMs, tailRatio, lognormalSigma)
	fmt.Printf("  tail threshold t         %.1f ms   chosen so P(one leg > t) = 1%% for BOTH distributions, by construction\n",
		tailThresholdMs)
	fmt.Println("  predicted below          1 - 0.99^K, arithmetic rather than measurement")

	// ------------------------------------------------------------ calibration
	rule("CALIBRATION: one backend, unsaturated, measured directly")
	fmt.Printf("  %-12s%9s%9s%9s%9s%13s\n", "distribution", "p50", "p95", "p99", "mean", "P(leg > t)")
	calib := map[string]map[string]float64{}
	for _, d := range dists {
		c := calibrate(d, statWorkers, calibSamples)
		calib[d.Name()] = c
		fmt.Printf("  %-12s%7.1fms%7.1fms%7.1fms%7.1fms%12.2f%%\n",
			d.Name(), c["p50"], c["p95"], c["p99"], c["mean"], c["over"])
	}
	fmt.Println()
	fmt.Println("  P(leg > t) is the measured check on the configured 1%. The hedge delay")
	fmt.Println("  in phase B is the MEASURED p95 above, not the analytic one.")

	// ---------------------------------------------------------------- phase A
	rule("PHASE A: fan-out to K backends, wait for all, no hedging")
	fmt.Printf("  backends have %d workers each -- they do not queue, so the only\n", statWorkers)
	fmt.Println("  mechanism acting on these numbers is the arithmetic of maxima.")
	fmt.Println()
	fmt.Printf("  %-11s%4s%7s%7s%10s%10s%10s%11s%10s%14s\n",
		"dist", "K", "rate", "n", "e2e_p50", "e2e_p99", "e2e_max", "predicted", "measured", "gen_late_p99")
	baseline := map[string]summary{}
	for _, d := range dists {
		for _, k := range kValues {
			rate := cellRate(k)
			cell := runOpenCell(k, d, statWorkers, rate, samplesPerCell, 0, true)
			s := cell.summary()
			baseline[fmt.Sprintf("%s/%d", d.Name(), k)] = s
			predicted := 100.0 * (1.0 - math.Pow(0.99, float64(k)))
			fmt.Printf("  %-11s%4d%7.0f%7d%8.1fms%8.1fms%8.1fms%10.1f%%%9.1f%%%12.2fms\n",
				d.Name(), k, rate, s.n, s.p50, s.p99, s.max, predicted, s.tail, s.lateP99)
		}
		fmt.Println()
	}

	// ---------------------------------------------------------------- phase B
	rule("PHASE B: hedging at the measured backend p95, under a 5% token bucket")
	fmt.Println("  Three rows per configuration, identical except for what happens to the")
	fmt.Println("  losing copy: nothing (no hedge), context cancelled, or left running.")
	fmt.Println()
	fmt.Println("  svc_ms/req is the backend service time actually consumed per request. It is")
	fmt.Println("  the column that separates the last two rows: they issue the same calls, and")
	fmt.Println("  only one of them stops paying for the copy it threw away.")
	fmt.Println()
	fmt.Printf("  %-10s%3s %-26s%9s%9s%11s%7s%11s%8s%7s\n",
		"dist", "K", "mode", "e2e_p50", "e2e_p99", "be_rps", "+load", "svc_ms/req", "hedge%", "denied")
	for _, d := range dists {
		hedgeDelayMs := calib[d.Name()]["p95"]
		for _, k := range hedgeK {
			rate := cellRate(k)
			base := baseline[fmt.Sprintf("%s/%d", d.Name(), k)]
			fmt.Printf("  %-10s%3d %-26s%7.1fms%7.1fms%10.0f/s%7s%11.1f%8s%7s\n",
				d.Name(), k, "no hedge", base.p50, base.p99, base.backendRps, "-",
				base.svcMsPerReq, "-", "-")
			for _, cancel := range []bool{true, false} {
				label := "hedge @p95, cancelled"
				if !cancel {
					label = "hedge @p95, NOT cancelled"
				}
				cell := runOpenCell(k, d, statWorkers, rate, samplesPerCell,
					time.Duration(hedgeDelayMs*float64(time.Millisecond)), cancel)
				s := cell.summary()
				loadPct := 100.0 * (s.backendRps/base.backendRps - 1.0)
				fmt.Printf("  %-10s%3s %-26s%7.1fms%7.1fms%10.0f/s%6.1f%%%11.1f%7.1f%%%7d\n",
					"", "", label, s.p50, s.p99, s.backendRps, loadPct, s.svcMsPerReq,
					s.hedgeRate, atomic.LoadInt64(&cell.gw.budgetDenied))
			}
			fmt.Printf("  %-10s%3s  hedge delay = measured p95 = %.1f ms\n", "", "", hedgeDelayMs)
			fmt.Println()
		}
	}

	// ---------------------------------------------------------------- phase C
	rule("PHASE C: the same server measured twice -- open model vs closed loop")
	meanServiceS := calib["lognormal"]["mean"] / 1000.0
	capacity := coWorkers / meanServiceS
	rate := coRho * capacity
	fmt.Printf("  K = %d, log-normal, and this time each backend has only %d workers.\n", coK, coWorkers)
	fmt.Printf("  measured mean service time  %.1f ms\n", meanServiceS*1000.0)
	fmt.Printf("  => capacity per backend     %.1f rps (%d workers / mean service)\n", capacity, coWorkers)
	fmt.Printf("  => nominal offered rate     %.1f rps  (rho = %.2f)\n", rate, coRho)
	fmt.Println()
	fmt.Println("  rho is deliberately below 1. Above capacity the open model's queue grows")
	fmt.Println("  without bound and its p99 becomes a statement about how long you ran,")
	fmt.Println("  not about the server. Below capacity both numbers mean something.")

	// A short unsaturated pass to size the VU pool by Little's Law.
	warm := runOpenCell(coK, lognormal, statWorkers, rate, 600, 0, true)
	ws := warm.summary()
	sum := 0.0
	for _, v := range warm.latMs {
		sum += v
	}
	baseMeanE2ES := (sum / float64(len(warm.latMs))) / 1000.0
	vus := int(math.Round(rate * baseMeanE2ES))
	if vus < 1 {
		vus = 1
	}
	fmt.Println()
	fmt.Printf("  unsaturated e2e mean at K=%d: %.1f ms (p99 %.1f ms)\n", coK, baseMeanE2ES*1000.0, ws.p99)
	fmt.Printf("  => closed loop gets %d VUs, from Little's Law: %.1f rps x %.1f ms.\n",
		vus, rate, baseMeanE2ES*1000.0)
	fmt.Println("     At the healthy latency those VUs issue the nominal rate exactly. That")
	fmt.Println("     is the whole trick: the generator is calibrated on a good day.")

	openCell := runOpenCell(coK, lognormal, coWorkers, rate, int(rate*coSeconds), 0, true)
	os_ := openCell.summary()
	closedCell := runClosedCell(coK, lognormal, coWorkers, vus, rate, coSeconds)
	cs := closedCell.summary()
	corrected := append([]float64(nil), closedCell.correctedMs...)
	sort.Float64s(corrected)

	fmt.Println()
	fmt.Printf("  %-34s%7s%11s%10s%10s%11s\n", "model", "n", "achieved", "p50", "p99", "max")
	fmt.Printf("  %-34s%7d%9.0f/s%8.1fms%8.1fms%9.1fms\n", "open  (arrival schedule)",
		os_.n, float64(os_.n)/openCell.arrivalWall.Seconds(), os_.p50, os_.p99, os_.max)
	fmt.Printf("  %-34s%7d%9.0f/s%8.1fms%8.1fms%9.1fms\n",
		fmt.Sprintf("closed (%d VUs), as reported", vus),
		cs.n, float64(cs.n)/closedCell.arrivalWall.Seconds(), cs.p50, cs.p99, cs.max)
	fmt.Printf("  %-34s%7d%11s%8.1fms%8.1fms%9.1fms\n", "closed, omission-corrected",
		len(corrected), "", pctile(corrected, 0.50), pctile(corrected, 0.99), corrected[len(corrected)-1])
	fmt.Println()
	fmt.Printf("  open-model generator lateness p99: %.2f ms\n", os_.lateP99)
	fmt.Println("  (if that number is large the generator itself fell behind and is now")
	fmt.Println("   coordinating omission too, arrival schedule or not -- k6's warning")
	fmt.Println("   about not being able to allocate enough VUs is the same tell.)")
	fmt.Println()
	histogram("open model  ", openCell.latMs)
	fmt.Println()
	histogram("closed loop ", closedCell.latMs)
	fmt.Println()
	fmt.Println("  Same server. Same nominal rate. Read the two histograms' right-hand")
	fmt.Println("  ends against each other, then read the closed loop's raw p99 against")
	fmt.Println("  its corrected p99.")
	fmt.Println()
}
