// 7.2 -- Go: the runtime that sizes itself, and the version where it started
// sizing itself from the right number.
//
// WHAT THIS DEMONSTRATES
//
//	Go's scheduler multiplexes goroutines onto GOMAXPROCS OS threads. Under
//	a 1.0-CPU quota on an 8-core host, a pre-1.25 toolchain sets GOMAXPROCS
//	to 8 and cheerfully runs eight threads flat out inside a cgroup allowed
//	100ms of CPU per 100ms. Those eight threads drain the bucket in roughly
//	12.5ms of wall clock and the kernel then freezes all of them for the
//	remaining 87.5ms. Throughput is unchanged -- the quota was always the
//	ceiling -- but every latency measurement acquires an 87ms cliff.
//
//	Go 1.25 (August 2025) made the default GOMAXPROCS the minimum of the
//	logical CPU count, the affinity mask, and the cgroup CPU bandwidth
//	limit, rounding fractional limits up, never below 2, re-checked about
//	once a second. This program prints which side of that line your
//	toolchain is on rather than assuming, because the answer changes what
//	the table below means.
//
//	"fix 1" here is literally what Go 1.25 (and Uber's automaxprocs before
//	it) does for you: set GOMAXPROCS to the quota.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. The GOMAXPROCS line at the top versus the enforced quota. On a
//     pre-1.25 toolchain, or with GODEBUG=containermaxprocs=0, those two
//     numbers disagree, and the gap is the bug.
//  2. Row 1 vs row 2: same offered load, same quota, only GOMAXPROCS and
//     the concurrency change. Throughput is identical; the throttle ratio
//     and the heartbeat gap are not.
//  3. The heartbeat. It burns no measurable CPU and is frozen anyway,
//     because throttling dequeues every task in the cgroup.
//
// RUN
//
//	go run quota_freeze.go
//	GODEBUG=containermaxprocs=0 go run quota_freeze.go   # the old behaviour
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"fmt"
	"math"
	mrand "math/rand"
	"os"
	"runtime"
	"runtime/pprof"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	workMS       = 40.0 // CPU cost of one request
	offeredRate  = 9.0  // req/s -> ~0.36 CPU of demand, well under the quota
	runSeconds   = 15.0
	heartbeatDur = 10 * time.Millisecond
	periodUS     = 100000
	arrivalSeed  = 20260818
)

var hashBlock = func() []byte {
	buf := make([]byte, 256*1024)
	rand.Read(buf)
	return buf
}()

// ------------------------------------------------------------- the budget

// cpuBudget is one cgroup's worth of CFS bandwidth: a bucket refilled with
// quotaUS microseconds every periodUS, and every task parked when it is
// empty. The refill goroutine deliberately never parks -- the kernel is not
// inside your cgroup.
//
// Go needs a different accounting method from the other five languages in
// this topic, and the reason is worth knowing. Elsewhere a worker can time
// its own chunk of work with a stopwatch, because a real OS thread running
// a tight loop is running for the whole of that wall time. A goroutine is
// not: it is multiplexed onto GOMAXPROCS threads and, since Go 1.14, can be
// preempted mid-loop by a signal. Charge a goroutine's wall time and you
// bill it for time it spent descheduled -- which, at GOMAXPROCS=1, inflates
// four goroutines' usage roughly fourfold and makes the fix look like it
// did nothing.
//
// So this budget accounts the way the kernel actually does: at the GROUP
// level, by sampling the whole process's CPU time (getrusage) and charging
// the delta. Enforcement stays per-task -- workers check in between chunks
// and park when the bucket is empty, the same shape as the kernel dequeuing
// a task at its next scheduling point.
type cpuBudget struct {
	quotaUS  int64
	periodUS int64

	mu        sync.Mutex
	cond      *sync.Cond
	balance   int64
	usageUS   int64
	periods   int64
	throttled int64
	frozeThis bool
	running   bool
	lastCPUUS int64
}

func processCPUMicros() int64 {
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		return 0
	}
	user := int64(usage.Utime.Sec)*1e6 + int64(usage.Utime.Usec)
	sys := int64(usage.Stime.Sec)*1e6 + int64(usage.Stime.Usec)
	return user + sys
}

func newBudget(quotaCPUs float64, periodUS int64) *cpuBudget {
	b := &cpuBudget{
		quotaUS:   int64(quotaCPUs * float64(periodUS)),
		periodUS:  periodUS,
		running:   true,
		lastCPUUS: processCPUMicros(),
	}
	b.cond = sync.NewCond(&b.mu)
	b.balance = b.quotaUS
	go b.refillLoop()
	go b.accountantLoop()
	return b
}

// accountantLoop is the cgroup's CPU accounting. It samples more often than
// the kernel's 5ms bandwidth slice, so the group can overshoot its quota by
// at most one sample -- which is also true of the real thing, and is why a
// container's usage_usec can slightly exceed its quota within a period.
func (b *cpuBudget) accountantLoop() {
	ticker := time.NewTicker(1 * time.Millisecond)
	defer ticker.Stop()
	for range ticker.C {
		b.mu.Lock()
		if !b.running {
			b.mu.Unlock()
			return
		}
		now := processCPUMicros()
		delta := now - b.lastCPUUS
		b.lastCPUUS = now
		if delta > 0 {
			b.balance -= delta
			b.usageUS += delta
			if b.balance <= 0 {
				b.frozeThis = true
			}
		}
		b.mu.Unlock()
	}
}

func (b *cpuBudget) refillLoop() {
	ticker := time.NewTicker(time.Duration(b.periodUS) * time.Microsecond)
	defer ticker.Stop()
	for range ticker.C {
		b.mu.Lock()
		if !b.running {
			b.mu.Unlock()
			return
		}
		b.periods++
		if b.frozeThis {
			b.throttled++
		}
		b.frozeThis = false
		b.balance = b.quotaUS
		b.cond.Broadcast()
		b.mu.Unlock()
	}
}

// checkpoint is a task's scheduling point: park here if the group's bucket
// is empty. Nothing is charged -- the accountant already did that.
func (b *cpuBudget) checkpoint() {
	b.mu.Lock()
	defer b.mu.Unlock()
	for b.balance <= 0 && b.running {
		b.frozeThis = true
		b.cond.Wait()
	}
}

// park waits out a freeze without consuming quota, and reports how long it
// was frozen. This is what happens to your heartbeat, your health check and
// your metrics scraper -- punished for CPU somebody else spent.
//
// Returning the duration matters: it separates "the cgroup froze me" from
// "the Go scheduler did not get round to me". Those are different problems
// with different fixes and identical symptoms.
func (b *cpuBudget) park() float64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.balance > 0 {
		return 0
	}
	start := time.Now()
	for b.balance <= 0 && b.running {
		b.cond.Wait()
	}
	return float64(time.Since(start).Microseconds()) / 1000.0
}

func (b *cpuBudget) stop() {
	b.mu.Lock()
	b.running = false
	b.cond.Broadcast()
	b.mu.Unlock()
}

func (b *cpuBudget) snapshot() (usage, periods, throttled int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.usageUS, b.periods, b.throttled
}

// --------------------------------------------------------------- the work

// burnCPU does a fixed amount of real work: a fixed number of hash blocks,
// not "hash until the stopwatch says 40ms". Under a quota the stopwatch
// measures freezes as well as work, so a time-bounded loop would silently
// do LESS work the more it was throttled, and throughput would stop being
// comparable between rows.
func burnCPU(blocks int, b *cpuBudget) {
	digest := sha256.New()
	for i := 0; i < blocks; i++ {
		digest.Write(hashBlock)
		if i%4 == 3 && b != nil {
			b.checkpoint() // a scheduling point, like the kernel's
		}
	}
	digest.Sum(nil)
	if b != nil {
		b.checkpoint()
	}
}

// blocksForMillis measures how many hash blocks cost targetMS on THIS
// machine. Never hardcode this: an M1 core and a c6i core are not the same
// core, and a wrong constant changes what every row below means.
func blocksForMillis(targetMS float64) int {
	start := time.Now()
	digest := sha256.New()
	for i := 0; i < 64; i++ {
		digest.Write(hashBlock)
	}
	digest.Sum(nil)
	perBlockMS := float64(time.Since(start).Microseconds()) / 1000.0 / 64
	n := int(targetMS/perBlockMS + 0.5)
	if n < 1 {
		n = 1
	}
	return n
}

// ------------------------------------------------------------- one variant

type result struct {
	completed int
	reqPerS   float64
	avgCPU    float64
	periods   int64
	throttled int64
	p50, p99  float64
	hbGap     float64
	hbFrozen  float64
}

func poissonSchedule(rate, seconds float64, seed int64) []float64 {
	// Poisson, not evenly spaced. Throttling at low average utilisation is a
	// burstiness effect: the bucket is drained by demand that clumps inside
	// one 100ms window. Evenly-spaced arrivals cannot reproduce it at all,
	// which is why hand-rolled load loops so reliably fail to find it.
	rng := mrand.New(mrand.NewSource(seed))
	var out []float64
	t := 0.0
	for t < seconds {
		out = append(out, t*1000)
		t += rng.ExpFloat64() / rate
	}
	return out
}

var workBlocks int

func runVariant(gomaxprocs, concurrency int, quotaCPUs float64) result {
	previous := runtime.GOMAXPROCS(gomaxprocs)
	defer runtime.GOMAXPROCS(previous)

	budget := newBudget(quotaCPUs, periodUS)
	defer budget.stop()

	schedule := poissonSchedule(offeredRate, runSeconds, arrivalSeed)
	start := time.Now().Add(200 * time.Millisecond)

	var mu sync.Mutex
	latencies := make([]float64, 0, len(schedule))

	cursor := make(chan int, len(schedule))
	for i := range schedule {
		cursor <- i
	}
	close(cursor)

	var wg sync.WaitGroup
	for w := 0; w < concurrency; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range cursor {
				due := start.Add(time.Duration(schedule[i]) * time.Millisecond)
				if wait := time.Until(due); wait > 0 {
					time.Sleep(wait)
				}
				burnCPU(workBlocks, budget)
				mu.Lock()
				latencies = append(latencies, float64(time.Since(due).Microseconds())/1000.0)
				mu.Unlock()
			}
		}()
	}

	// Heartbeat: no CPU of its own, frozen anyway.
	hbDone := make(chan [2]float64, 1)
	hbStop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(heartbeatDur)
		defer ticker.Stop()
		last := time.Now()
		maxGap, maxFrozen := 0.0, 0.0
		for {
			select {
			case <-ticker.C:
				frozen := budget.park()
				if frozen > maxFrozen {
					maxFrozen = frozen
				}
				gap := float64(time.Since(last).Microseconds()) / 1000.0
				if gap > maxGap {
					maxGap = gap
				}
				last = time.Now()
			case <-hbStop:
				hbDone <- [2]float64{maxGap, maxFrozen}
				return
			}
		}
	}()

	wg.Wait()
	close(hbStop)
	hb := <-hbDone
	hbGap, hbFrozen := hb[0], hb[1]

	usage, periods, throttled := budget.snapshot()
	sort.Float64s(latencies)
	return result{
		completed: len(latencies),
		reqPerS:   float64(len(latencies)) / runSeconds,
		avgCPU:    100 * float64(usage) / (runSeconds * 1e6),
		periods:   periods,
		throttled: throttled,
		p50:       percentile(latencies, 50),
		p99:       percentile(latencies, 99),
		hbGap:     hbGap,
		hbFrozen:  hbFrozen,
	}
}

func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return math.NaN()
	}
	idx := int(math.Round(p / 100 * float64(len(sorted)-1)))
	return sorted[idx]
}

// ------------------------------------------------------------- environment

func readCPUMax() (float64, bool) {
	raw, err := os.ReadFile("/sys/fs/cgroup/cpu.max")
	if err != nil {
		return 0, false
	}
	fields := strings.Fields(string(raw))
	if len(fields) < 2 || fields[0] == "max" {
		return 0, false
	}
	quota, _ := strconv.ParseFloat(fields[0], 64)
	period, _ := strconv.ParseFloat(fields[1], 64)
	if period == 0 {
		return 0, false
	}
	return quota / period, true
}

func containerAwareToolchain() bool {
	// Go 1.25 is where the default GOMAXPROCS started consulting cpu.max.
	// Parse rather than assume: this is exactly the kind of claim that goes
	// stale, and the whole table below means something different either way.
	v := strings.TrimPrefix(runtime.Version(), "go")
	parts := strings.SplitN(v, ".", 3)
	if len(parts) < 2 {
		return false
	}
	major, _ := strconv.Atoi(parts[0])
	minor, _ := strconv.Atoi(parts[1])
	return major > 1 || (major == 1 && minor >= 25)
}

func main() {
	workBlocks = blocksForMillis(workMS)
	quota, haveQuota := readCPUMax()

	fmt.Println("7.2 -- throttled at 30% CPU: Go")
	fmt.Printf("  runtime                : %s %s/%s\n",
		runtime.Version(), runtime.GOOS, runtime.GOARCH)
	fmt.Printf("  runtime.NumCPU()       : %d   <- host/affinity cores\n", runtime.NumCPU())
	fmt.Printf("  runtime.GOMAXPROCS(0)  : %d   <- OS threads that may run Go code at once\n",
		runtime.GOMAXPROCS(0))
	if haveQuota {
		fmt.Printf("  quota actually enforced: %.2f CPU (cpu.max)\n", quota)
	} else {
		fmt.Println("  quota actually enforced: none (no cgroup on this host)")
	}
	if containerAwareToolchain() {
		fmt.Println("  GOMAXPROCS default     : container-aware (toolchain >= 1.25)")
		fmt.Println("                           GODEBUG=containermaxprocs=0 restores the old behaviour.")
	} else {
		fmt.Println("  GOMAXPROCS default     : NOT container-aware (toolchain < 1.25)")
		fmt.Println("                           On this toolchain GOMAXPROCS ignores cpu.max entirely.")
		fmt.Println("                           In production you would import uber-go/automaxprocs,")
		fmt.Println("                           or set GOMAXPROCS from the same place you set the limit.")
	}
	fmt.Printf("  OS threads created     : %d   (runtime/pprof threadcreate; cumulative)\n",
		pprof.Lookup("threadcreate").Count())
	fmt.Println()

	if !haveQuota {
		fmt.Println("  !! FALLBACK: no /sys/fs/cgroup on this host")
		fmt.Println("  !! This is a userspace MODEL of cpu.max, not the Linux kernel.")
		fmt.Println("  !! Real numbers come from /sys/fs/cgroup/cpu.stat inside a container.")
		fmt.Println()
	}

	fmt.Printf("  offered load: %.0f req/s x %.0fms CPU (%d hash blocks, measured) = %.2f CPU of demand\n",
		offeredRate, workMS, workBlocks, offeredRate*workMS/1000)
	fmt.Println("  quota:        1.00 CPU. The demand is comfortably under the limit.")
	fmt.Printf("  heartbeat wants a tick every %v; %.0fs per row\n", heartbeatDur, runSeconds)
	fmt.Println()

	variants := []struct {
		label       string
		gomaxprocs  int
		concurrency int
		quotaCPUs   float64
	}{
		{fmt.Sprintf("GOMAXPROCS=%d, 4 in flight, 1.0 CPU", runtime.NumCPU()), runtime.NumCPU(), 4, 1.0},
		{"fix 1: GOMAXPROCS=1 (what 1.25 does), 1.0 CPU", 1, 4, 1.0},
		{fmt.Sprintf("fix 2: GOMAXPROCS=%d, 2.0 CPU", runtime.NumCPU()), runtime.NumCPU(), 4, 2.0},
	}

	type row struct {
		cells []string
	}
	headers := []string{"variant", "n", "req/s", "avg CPU", "throttled", "ratio",
		"p50 ms", "p99 ms", "hb gap ms", "hb frozen ms"}
	rows := make([]row, 0, len(variants))
	for _, v := range variants {
		r := runVariant(v.gomaxprocs, v.concurrency, v.quotaCPUs)
		ratio := 0.0
		if r.periods > 0 {
			ratio = float64(r.throttled) / float64(r.periods)
		}
		rows = append(rows, row{[]string{
			v.label,
			strconv.Itoa(r.completed),
			fmt.Sprintf("%.1f", r.reqPerS),
			fmt.Sprintf("%.0f%%", r.avgCPU),
			fmt.Sprintf("%d/%d", r.throttled, r.periods),
			fmt.Sprintf("%.3f", ratio),
			fmt.Sprintf("%.0f", r.p50),
			fmt.Sprintf("%.0f", r.p99),
			fmt.Sprintf("%.0f", r.hbGap),
			fmt.Sprintf("%.0f", r.hbFrozen),
		}})
		fmt.Printf("  ran: %s\n", v.label)
	}

	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = len(h)
		for _, r := range rows {
			if len(r.cells[i]) > widths[i] {
				widths[i] = len(r.cells[i])
			}
		}
	}
	fmt.Println()
	printRow := func(cells []string) {
		parts := make([]string, len(cells))
		for i, c := range cells {
			parts[i] = c + strings.Repeat(" ", widths[i]-len(c))
		}
		fmt.Println(strings.Join(parts, "  "))
	}
	printRow(headers)
	rule := make([]string, len(headers))
	for i := range headers {
		rule[i] = strings.Repeat("-", widths[i])
	}
	fmt.Println(strings.Join(rule, "  "))
	for _, r := range rows {
		printRow(r.cells)
	}

	fmt.Println()
	fmt.Println("  Go is usually the language that does not have the problem. Here")
	fmt.Println("  it does, and for a reason worth remembering: the scheduler is")
	fmt.Println("  excellent at not wasting threads, and completely uninterested in")
	fmt.Println("  how many CPU-seconds the kernel will sell you. Those are two")
	fmt.Println("  different questions, and until 1.25 the runtime only answered one.")
	fmt.Println()
	fmt.Println("  Read the last two columns together; they are the point of this file.")
	fmt.Println("   * 'hb frozen' is time the CGROUP held the heartbeat down.")
	fmt.Println("   * 'hb gap' is total lateness, from any cause.")
	fmt.Println("  At GOMAXPROCS=1 the frozen column collapses -- one thread cannot")
	fmt.Println("  drain the bucket faster than the kernel refills it -- while the gap")
	fmt.Println("  column may not, because now the GO SCHEDULER is the queue: four")
	fmt.Println("  CPU-bound goroutines and a heartbeat are sharing one P.")
	fmt.Println()
	fmt.Println("  Two mechanisms, one symptom, different fixes. That is the whole")
	fmt.Println("  reason cpu.stat is the first file to read: it tells the two apart")
	fmt.Println("  in ten seconds, and no latency graph ever will.")
	fmt.Println()
	fmt.Println("  Throughput is identical on every row. The quota was never the")
	fmt.Println("  throughput ceiling; it was only ever the source of the freezes.")
}
