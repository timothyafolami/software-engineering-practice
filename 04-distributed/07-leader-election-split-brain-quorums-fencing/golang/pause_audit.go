// Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.
//
// WHAT THIS DEMONSTRATES: Go is the runtime that mostly does NOT have this
// problem, and that is the finding rather than a disappointment. The netpoller
// parks a goroutine that blocks on IO instead of its thread, and asynchronous
// preemption (Go 1.14+) interrupts a goroutine stuck in a tight CPU loop via an
// OS signal. So a lease-renewal goroutine keeps getting scheduled even when
// every other goroutine is busy or blocked.
//
// Three runs, in increasing order of hostility:
//
//  1. CPU-bound goroutine flood at the default GOMAXPROCS. The hazard that
//     starves Python's and Node's renewal outright.
//  2. The same flood with GOMAXPROCS=1 -- one OS thread for everything.
//  3. GOMAXPROCS=1 plus runtime.LockOSThread in the workers, which pins each
//     worker to its own thread and is the closest thing here to defeating the
//     scheduler on purpose.
//
// WHAT TO LOOK FOR IN THE OUTPUT: whether any run exceeds the 10s TTL. If run 1
// does, do not believe it yet -- check the machine is not already loaded, because
// Go's whole claim is that it takes an EXTERNAL fault (a CFS throttle, a
// SIGSTOP, a migrating VM) to starve a goroutine. That is exactly why parts 1-4
// of this topic use `docker kill -s SIGSTOP` rather than a workload.
//
//	cd golang && go run pause_audit.go
package main

import (
	"crypto/sha256"
	"fmt"
	"runtime"
	"sync"
	"sync/atomic"
	"time"
)

const (
	leaseTTL      = 10 * time.Second
	renewInterval = 1 * time.Second
	hazardFor     = 12 * time.Second
)

// renewals records the gap between consecutive renewals. time.Since uses the
// monotonic reading a time.Time carries, so an NTP step cannot masquerade as a
// pause here -- see Topic 3 for what that costs when you get it wrong, and note
// that this is one of the few places Go's hidden monotonic reading is doing you
// a favour without being asked.
type renewals struct {
	mu   sync.Mutex
	gaps []time.Duration
	last time.Time
}

func newRenewals() *renewals { return &renewals{last: time.Now()} }

func (r *renewals) tick() {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := time.Now()
	r.gaps = append(r.gaps, now.Sub(r.last))
	r.last = now
}

func (r *renewals) longest() time.Duration {
	r.mu.Lock()
	defer r.mu.Unlock()
	var worst time.Duration
	for _, g := range r.gaps {
		if g > worst {
			worst = g
		}
	}
	return worst
}

// hazard runs `workers` CPU-bound goroutines for the whole window. No IO, no
// channel operations, no function calls the scheduler could use as a yield
// point in the pre-1.14 sense -- the loop body is deliberately hostile.
func hazard(workers int, d time.Duration, lockThread bool, rounds *int64) {
	var wg sync.WaitGroup
	deadline := time.Now().Add(d)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if lockThread {
				// Pins this goroutine to its own OS thread for its lifetime.
				// The scheduler can no longer move other goroutines onto that
				// thread, which is as close as this program gets to defeating
				// the runtime on purpose.
				runtime.LockOSThread()
				defer runtime.UnlockOSThread()
			}
			digest := []byte("seed")
			n := int64(0)
			for time.Now().Before(deadline) {
				for i := 0; i < 20000; i++ {
					sum := sha256.Sum256(digest)
					digest = sum[:]
				}
				n += 20000
			}
			atomic.AddInt64(rounds, n)
		}()
	}
	wg.Wait()
}

func run(label string, procs, workers int, lockThread bool) bool {
	prev := runtime.GOMAXPROCS(procs)
	defer runtime.GOMAXPROCS(prev)

	r := newRenewals()
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		t := time.NewTicker(renewInterval)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				r.tick()
			case <-done:
				return
			}
		}
	}()

	time.Sleep(2 * renewInterval)
	var rounds int64
	start := time.Now()
	hazard(workers, hazardFor, lockThread, &rounds)
	took := time.Since(start)
	time.Sleep(2 * renewInterval)
	close(done)
	wg.Wait()

	worst := r.longest()
	lost := worst > leaseTTL
	verdict := "held"
	if lost {
		verdict = "LOST THE LEASE"
	}
	fmt.Printf("  %-38s%9.2fs    %8.2fs    %-16s%12d rounds\n",
		label, worst.Seconds(), took.Seconds(), verdict, rounds)
	return lost
}

func main() {
	fmt.Println("==============================================================================")
	fmt.Println("Layer 4 Topic 7 -- Go pause audit")
	fmt.Println("==============================================================================")
	fmt.Printf("  %s on %s/%s, NumCPU=%d, GOMAXPROCS=%d\n", runtime.Version(),
		runtime.GOOS, runtime.GOARCH, runtime.NumCPU(), runtime.GOMAXPROCS(0))
	fmt.Printf("  lease TTL %v, renewal every %v, hazard %v\n",
		leaseTTL, renewInterval, hazardFor)
	fmt.Println("  hazard: CPU-bound goroutines, no IO and no yield points in the loop")
	fmt.Println("  clock : time.Since, which uses time.Time's monotonic reading")
	fmt.Println()
	fmt.Printf("  %-38s%10s    %9s    %-16s\n", "run", "longest gap", "hazard took", "verdict")

	cpus := runtime.NumCPU()
	lost := false
	lost = run(fmt.Sprintf("goroutine flood, GOMAXPROCS=%d", cpus), cpus, cpus*4, false) || lost
	lost = run("goroutine flood, GOMAXPROCS=1", 1, cpus*4, false) || lost
	lost = run("+ LockOSThread, GOMAXPROCS=1", 1, cpus*4, true) || lost

	fmt.Println()
	if !lost {
		fmt.Println("  No run exceeded the TTL. That is the expected result and it is the")
		fmt.Println("  point of including Go: asynchronous preemption keeps scheduling the")
		fmt.Println("  renewal goroutine even with one OS thread and a hostile CPU loop.")
		fmt.Println()
		fmt.Println("  So in Go the pause has to come from OUTSIDE the runtime: a CFS throttle")
		fmt.Println("  when the cgroup quota runs out, a SIGSTOP, a host that starts swapping,")
		fmt.Println("  a live-migrating VM. That is why parts 1-4 of this topic reach for")
		fmt.Println("  `docker kill -s SIGSTOP` and `cpus: '0.1'` rather than for a workload.")
	} else {
		fmt.Println("  A run exceeded the TTL. Before recording that as a finding: check the")
		fmt.Println("  machine was not already loaded, and note which run it was. Runs 2 and 3")
		fmt.Println("  are deliberately pathological -- GOMAXPROCS=1 with LockOSThread'd")
		fmt.Println("  workers is a configuration you would have to choose. Run 1 blowing the")
		fmt.Println("  TTL on an idle machine would be the genuinely surprising result.")
	}
	fmt.Println()
	fmt.Println("  And the fix is the same one every runtime in this topic ends at: fencing.")
	fmt.Println("  Go's advantage is that it needs it LESS OFTEN, not that it does not need")
	fmt.Println("  it. etcd's clientv3/concurrency hands you the token for free -- the key's")
	fmt.Println("  CreateRevision -- and its session Done() channel is the 'you have lost the")
	fmt.Println("  lock' signal that a leader which does not select on it will ignore.")
}
