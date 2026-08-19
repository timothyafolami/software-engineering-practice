// Layer 5 - Topic 3: retry amplification, in one Go process.
//
// Go has no retry in the standard library at all -- you reach for
// failsafe-go or cenkalti/backoff/v5 -- but gRPC-Go implements service-config
// retry policies WITH throttling, which is the closest thing to a
// batteries-included retry budget anywhere in this lab's six languages. The
// bucket below is that idea written out in thirty lines of stdlib.
//
// Go's other advantage is topic 2's. Because context carries an absolute
// deadline that every well-behaved library obeys, a correctly written retry
// loop cannot outlive its caller's budget without somebody explicitly
// detaching it -- and the loop below never has to compute "how long do I
// have left", it just asks the context.
//
// WHAT THIS DEMONSTRATES
//
//	gateway -> serviceB -> serviceC -> database, each hop retrying up to 3
//	times. The database refuses connections for a window in the middle of
//	the run. The leaf counter counts DATABASE CALLS, so the theoretical
//	worst case is 3 hops x 3 attempts = 27x the offered rate.
//
//	 A naive      exponential backoff, no jitter, no budget
//	 B + jitter   full jitter: sleep = random(0, min(cap, base * 2**n))
//	 C + budget   a 10% token bucket at every hop, Envoy/gRPC style
//	 D edge only  only the hop adjacent to the database retries, and it
//	              marks the error non-retryable on the way up
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `amp` during the fault window, and -- much more importantly -- what
//     it does AFTER the fault clears. Once retries have built a queue, the
//     queue causes the next round of retries, and that loop can sustain
//     itself with the fault long gone. Read YOUR run: `mean amp from 16s
//     onward` and `success after` are the two numbers, and this program is
//     not going to promise you which way they land. The chain is BISTABLE
//     at these constants -- 150 rps offered against 200 rps of leaf
//     capacity, so a backlog can be worked off, but only if it is small
//     enough when the fault clears. Rerunning, or running the same policy
//     in another language in this folder, can land in the other basin.
//     That is not the experiment being flaky; that is the finding, and it
//     is topic 4 arriving early and uninvited.
//     What is NOT bistable is variant C. Look at it before you conclude
//     anything about runtimes.
//  2. Variant C's retry traffic going to zero on its own as failures climb.
//  3. Variant D's peak being one hop's attempts rather than three hops'
//     product.
//  4. The synchronised-cohort histogram at the end, which is the only place
//     in this file where jitter looks like a good idea.
//
// RUN
//
//	go run retry_storm.go
package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"
)

// ------------------------------------------------------------------ config

const (
	offeredRPS     = 150.0
	duration       = 24 * time.Second
	faultOn        = 5 * time.Second
	faultOff       = 12 * time.Second
	reportBucket   = 2 * time.Second
	attempts       = 3
	baseBackoff    = 50 * time.Millisecond
	backoffCap     = 400 * time.Millisecond
	attemptTimeout = 300 * time.Millisecond
	requestBudget  = 1500 * time.Millisecond
	leafPool       = 8
	leafService    = 40 * time.Millisecond
	budgetRatio    = 0.10
	budgetFloor    = 3.0
)

// ------------------------------------------------------------ retry budget

// retryBudget is a token bucket that permits retries only while retries stay
// under some fraction of successes -- Envoy's budget_percent, gRPC's
// retryThrottling, the thing Yandex settled on at 10%.
//
// The property is qualitative rather than numeric: at low failure rates this
// is indistinguishable from an ordinary retrying client, and as failures
// climb its retry traffic goes to ZERO by itself. Backoff delays
// amplification; only this bounds it.
type retryBudget struct {
	mu      sync.Mutex
	tokens  float64
	ceiling float64
}

func newBudget() *retryBudget {
	return &retryBudget{tokens: budgetFloor, ceiling: budgetFloor + 100}
}

// deposit refills on SUCCESSES, never on wall-clock. A clock-refilled bucket
// gives an idle service free retries it never earned, and hands a service in
// total outage a steady drip of amplification forever.
func (b *retryBudget) deposit() {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.tokens = math.Min(b.tokens+budgetRatio, b.ceiling)
}

func (b *retryBudget) withdraw() bool {
	if b == nil {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

// ------------------------------------------------------------- the errors

var errUnavailable = errors.New("connection refused")

// errNonRetryable is variant D's entire mechanism: an error that says "I
// already spent the attempts, do not spend yours".
type nonRetryable struct{ err error }

func (e nonRetryable) Error() string { return "non-retryable: " + e.err.Error() }
func (e nonRetryable) Unwrap() error { return e.err }

// retryableErr is the predicate. Retry connect errors, connect timeouts, 429
// and 503; never retry a 400, 401, 403, 404 or 422, because the same request
// will fail the same way and the retry is pure waste.
func retryableErr(err error) bool {
	var nr nonRetryable
	if errors.As(err, &nr) {
		return false
	}
	return errors.Is(err, errUnavailable) || errors.Is(err, context.DeadlineExceeded)
}

// ---------------------------------------------------------------- the leaf

type metrics struct {
	leafReceived atomic.Int64
	ok           atomic.Int64
	failed       atomic.Int64
	retries      atomic.Int64
	budgetDenied atomic.Int64
	samples      []sample
}

type sample struct {
	t, received, amp, success float64
}

type leaf struct {
	tokens chan struct{}
	m      *metrics
	faulty atomic.Bool
}

func newLeaf(m *metrics) *leaf {
	l := &leaf{tokens: make(chan struct{}, leafPool), m: m}
	for i := 0; i < leafPool; i++ {
		l.tokens <- struct{}{}
	}
	return l
}

func (l *leaf) call(ctx context.Context) error {
	// THE COUNTER THAT MATTERS. Requests RECEIVED, not requests succeeded.
	// Divided by the client's offered rate it is the live amplification
	// factor, and it is the one number in this topic worth a dashboard.
	l.m.leafReceived.Add(1)

	if l.faulty.Load() {
		// Connection refused: fast, cheap, and therefore the worst kind of
		// failure for a retrying client, because the retry arrives almost
		// immediately.
		return errUnavailable
	}

	select {
	case <-l.tokens:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { l.tokens <- struct{}{} }()

	select {
	case <-time.After(leafService):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// -------------------------------------------------------------- the policy

// withRetries is one hop's loop. Note what it does NOT do: it never computes
// a remaining budget, because ctx already knows. That is the whole of Go's
// advantage on this topic.
func withRetries(ctx context.Context, m *metrics, b *retryBudget, jitter bool,
	rng *rand.Rand, mu *sync.Mutex, call func(context.Context) error) error {

	delay := baseBackoff
	last := errUnavailable

	for attempt := 0; attempt < attempts; attempt++ {
		if attempt > 0 {
			// (4) The budget, checked BEFORE the sleep, so a denied retry
			// costs nothing at all -- not even the wait.
			if !b.withdraw() {
				m.budgetDenied.Add(1)
				return last
			}
			m.retries.Add(1)

			bounded := delay
			if bounded > backoffCap {
				bounded = backoffCap
			}
			wait := bounded
			if jitter {
				// Full jitter, the AWS Builders' Library recommendation:
				// spread a synchronised cohort across the WHOLE interval
				// rather than around a common centre.
				mu.Lock()
				wait = time.Duration(rng.Float64() * float64(bounded))
				mu.Unlock()
			}
			delay *= 2

			// (3) A hard cap that fits inside the caller's budget. In Go you
			// get this by asking the context rather than by tracking it.
			if dl, ok := ctx.Deadline(); ok && time.Now().Add(wait).After(dl) {
				return last
			}
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return last
			}
		}

		if ctx.Err() != nil {
			return last
		}
		attemptCtx, cancel := context.WithTimeout(ctx, attemptTimeout)
		err := call(attemptCtx)
		cancel()
		if err == nil {
			b.deposit()
			return nil
		}
		// (1) Only retry what is genuinely transient.
		if !retryableErr(err) {
			return err
		}
		last = errUnavailable
	}
	return last
}

// --------------------------------------------------------------- the chain

type chain struct {
	leaf     *leaf
	m        *metrics
	jitter   bool
	edgeOnly bool
	budgets  [3]*retryBudget
	rng      *rand.Rand
	rngMu    sync.Mutex
}

func (c *chain) serviceC(ctx context.Context) error {
	err := withRetries(ctx, c.m, c.budgets[2], c.jitter, c.rng, &c.rngMu, c.leaf.call)
	if err != nil && c.edgeOnly {
		// THE STRUCTURAL FIX. The hop next to the failure has already spent
		// its attempts; saying so upward turns the worst case from 3**3 back
		// into 3. It composes cleanly with topic 2 and is far easier to
		// reason about than any amount of tuning.
		return nonRetryable{err}
	}
	return err
}

func (c *chain) serviceB(ctx context.Context) error {
	return withRetries(ctx, c.m, c.budgets[1], c.jitter, c.rng, &c.rngMu, c.serviceC)
}

func (c *chain) gateway() {
	ctx, cancel := context.WithTimeout(context.Background(), requestBudget)
	defer cancel()
	if err := withRetries(ctx, c.m, c.budgets[0], c.jitter, c.rng, &c.rngMu, c.serviceB); err != nil {
		c.m.failed.Add(1)
	} else {
		c.m.ok.Add(1)
	}
}

// -------------------------------------------------------------- the driver

func runVariant(jitter, budgeted, edgeOnly bool) *metrics {
	m := &metrics{}
	l := newLeaf(m)
	c := &chain{leaf: l, m: m, jitter: jitter, edgeOnly: edgeOnly,
		rng: rand.New(rand.NewSource(777))}
	if budgeted {
		// One bucket per hop, shared across every request that hop handles.
		// Per-request state would defeat the whole idea: the budget exists to
		// make one client's retries visible to the next client's.
		for i := range c.budgets {
			c.budgets[i] = newBudget()
		}
	}

	arrivals := rand.New(rand.NewSource(20250503))
	begin := time.Now()
	end := begin.Add(duration)
	at := begin
	lastBucket := begin
	var lastReceived, lastOK, lastTotal int64
	var wg sync.WaitGroup

	for {
		at = at.Add(time.Duration(arrivals.ExpFloat64() / offeredRPS * float64(time.Second)))
		if at.After(end) {
			break
		}
		if d := time.Until(at); d > 0 {
			time.Sleep(d)
		}
		t := time.Since(begin)
		l.faulty.Store(t >= faultOn && t < faultOff)

		wg.Add(1)
		go func() {
			defer wg.Done()
			c.gateway()
		}()

		if time.Since(lastBucket) >= reportBucket {
			span := time.Since(lastBucket).Seconds()
			received := float64(m.leafReceived.Load()-lastReceived) / span
			total := m.ok.Load() + m.failed.Load()
			done := total - lastTotal
			ok := m.ok.Load() - lastOK
			success := 0.0
			if done > 0 {
				success = 100 * float64(ok) / float64(done)
			}
			m.samples = append(m.samples, sample{t.Seconds(), received, received / offeredRPS, success})
			lastBucket = time.Now()
			lastReceived = m.leafReceived.Load()
			lastOK = m.ok.Load()
			lastTotal = total
		}
	}
	wg.Wait()
	return m
}

// -------------------------------------------------------------- reporting

type summary struct {
	label                   string
	peak, tail, tailSuccess float64
}

func render(label string, m *metrics) summary {
	fmt.Printf("\n=== %s ===\n", label)
	fmt.Println("     t   leaf rps      amp   success                 amplification")
	peak := 0.0
	for _, s := range m.samples {
		if s.amp > peak {
			peak = s.amp
		}
	}
	scale := math.Max(peak, 1)
	for _, s := range m.samples {
		n := int(math.Round(34 * s.amp / scale))
		fault := "      "
		if s.t >= faultOn.Seconds() && s.t < faultOff.Seconds() {
			fault = " FAULT"
		}
		fmt.Printf("  %5.1f %10.1f %8.2f %8.1f%%%s |%s\n",
			s.t, s.received, s.amp, s.success, fault, repeat('#', n))
	}
	var tail, tailSuccess float64
	n := 0
	for _, s := range m.samples {
		if s.t >= faultOff.Seconds()+4 {
			tail += s.amp
			tailSuccess += s.success
			n++
		}
	}
	if n > 0 {
		tail /= float64(n)
		tailSuccess /= float64(n)
	}
	fmt.Printf("  peak amp %.2fx   mean amp from %.0fs onward %.2fx   success after %.1f%%   retries %d   budget-denied %d\n",
		peak, faultOff.Seconds()+4, tail, tailSuccess, m.retries.Load(), m.budgetDenied.Load())
	return summary{label, peak, tail, tailSuccess}
}

// synchronisedCohort explains why the table above makes jitter look useless.
//
// In the sweep, arrivals are a Poisson process: every client fails at a
// different moment already, so their retries were never going to collide.
// Jitter has nothing to decorrelate, and full jitter's shorter average wait
// actually lets MORE attempts fit inside the budget -- which is why variant B
// can amplify harder than variant A.
//
// Production is not that. Production is a thousand clients that were all
// talking to the same dependency when it fell over at the same instant.
func synchronisedCohort() {
	rng := rand.New(rand.NewSource(20250503))
	const clients = 1000
	delay := baseBackoff * 2
	if delay > backoffCap {
		delay = backoffCap
	}

	histogram := func(title string, draw func() time.Duration) {
		buckets := make([]int, 10)
		width := backoffCap / time.Duration(len(buckets))
		for i := 0; i < clients; i++ {
			b := int(draw() / width)
			if b >= len(buckets) {
				b = len(buckets) - 1
			}
			buckets[b]++
		}
		fmt.Printf("\n  %s\n", title)
		peak := 0
		for i, count := range buckets {
			if count > peak {
				peak = count
			}
			fmt.Printf("   %5d-%-5dms |%s %d\n",
				(time.Duration(i)*width)/time.Millisecond,
				(time.Duration(i+1)*width)/time.Millisecond,
				repeat('#', int(math.Round(48*float64(count)/clients))), count)
		}
		fmt.Printf("   peak instantaneous retry rate: %.0f rps from %d clients\n",
			float64(peak)/width.Seconds(), clients)
	}

	fmt.Printf("\n%s\n", repeat('=', 78))
	fmt.Println("Why the table above makes jitter look pointless: 1000 clients, one")
	fmt.Println("simultaneous failure, arrival times of their first retry.")
	histogram("no jitter -- sleep = min(cap, base * 2**n)", func() time.Duration { return delay })
	histogram("full jitter -- sleep = random(0, min(cap, base * 2**n))",
		func() time.Duration { return time.Duration(rng.Float64() * float64(delay)) })
	fmt.Println("\n  Same number of retries either way. Jitter does not reduce the")
	fmt.Println("  area, it reduces the PEAK, and the peak is what a service trying")
	fmt.Println("  to recover actually has to survive. The benefit is about")
	fmt.Println("  correlation, not about randomness, which is exactly why it is")
	fmt.Println("  invisible in a single-process test with independent arrivals.")
}

func repeat(c byte, n int) string {
	if n < 0 {
		n = 0
	}
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}

func main() {
	fmt.Println("Retry amplification through gateway -> serviceB -> serviceC -> database, in Go.")
	fmt.Printf("Offered %.0f rps for %v, database refuses connections from t=%v to t=%v.\n",
		offeredRPS, duration, faultOn, faultOff)
	fmt.Printf("%d attempts per hop over 3 hops = %dx worst case at the leaf; the leaf's real capacity is %d/%v = %.0f rps.\n",
		attempts, attempts*attempts*attempts, leafPool, leafService, leafPool/leafService.Seconds())
	fmt.Println("amp = database calls per second / offered rps. Watch what it does AFTER the fault clears.")

	rows := []summary{
		render("A naive: exponential backoff, no jitter", runVariant(false, false, false)),
		render("B + full jitter", runVariant(true, false, false)),
		render("C + 10% retry budget at every hop", runVariant(true, true, false)),
		render("D retry at the edge only", runVariant(true, false, true)),
	}

	fmt.Printf("\n%s\n", repeat('=', 78))
	fmt.Printf("%-44s%10s%11s%14s\n", "variant", "peak amp", "amp after", "success after")
	fmt.Println(repeat('-', 78))
	for _, r := range rows {
		fmt.Printf("%-44s%9.2fx%10.2fx%13.1f%%\n", r.label, r.peak, r.tail, r.tailSuccess)
	}

	fmt.Println()
	fmt.Println("The 27x worst case does not appear, and why it does not is the useful")
	fmt.Println("part: the per-attempt timeout and the context deadline expire before the")
	fmt.Println("deepest retries can be attempted. Timeouts cap amplification by accident.")
	fmt.Println("Do not rely on an accident.")
	fmt.Println()
	fmt.Println("Variant B amplifying harder than A is not a bug in the experiment.")
	fmt.Println("Arrivals here are a Poisson process, so nothing was synchronised for")
	fmt.Println("jitter to decorrelate -- and full jitter's shorter average wait lets more")
	fmt.Println("attempts fit inside the same budget. Keep reading.")
	fmt.Println()
	fmt.Println("C is the only variant whose retry traffic falls as failures climb, and the")
	fmt.Println("only one that is a bound rather than a delay. D gets most of the same")
	fmt.Println("benefit structurally, by making the answer to 'which layer owns retries' a")
	fmt.Println("single layer.")

	synchronisedCohort()
}
