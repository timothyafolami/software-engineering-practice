// Layer 1 - Go's concurrency model: goroutines are multiplexed M:N onto OS
// threads by the Go runtime scheduler. Unlike Python's asyncio (single OS
// thread) or naive Node (single OS thread for JS), Go's scheduler moves a
// goroutine that's about to block on a syscall off its OS thread and hands
// that thread to another runnable goroutine -- and since Go 1.14, even a
// goroutine stuck in a tight CPU loop with no function calls gets
// asynchronously preempted via signals. Run this with GOMAXPROCS=1 and
// watch the ticker survive both cases anyway. This is the payoff of the
// scheduler design, and it's the reason "just use goroutines" works so
// much more often in Go than the equivalent advice does in Python or Node.
package main

import (
	"fmt"
	"runtime"
	"sync"
	"sync/atomic"
	"time"
)

const (
	TickInterval = 100 * time.Millisecond
	BlockFor     = 1 * time.Second
	LeadIn       = 200 * time.Millisecond
	LeadOut      = 200 * time.Millisecond
)

func demo(name string, blockFn func()) {
	var ticks int64
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		ticker := time.NewTicker(TickInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				atomic.AddInt64(&ticks, 1)
			case <-done:
				return
			}
		}
	}()

	start := time.Now()
	time.Sleep(LeadIn)
	blockFn()
	time.Sleep(LeadOut)
	close(done)
	wg.Wait()

	elapsed := time.Since(start)
	expected := elapsed.Seconds() / TickInterval.Seconds()
	fmt.Printf("[%s] ticks counted: %d  over %.2fs  (expected ~%.0f if the ticker were never blocked)\n",
		name, atomic.LoadInt64(&ticks), elapsed.Seconds(), expected)
}

func main() {
	runtime.GOMAXPROCS(1) // deliberately hostile: only one OS thread for all goroutines
	fmt.Println("GOMAXPROCS=1 -- everything below shares a single OS thread")

	demo("blocking syscall (time.Sleep)", func() {
		time.Sleep(BlockFor)
	})

	demo("CPU-bound busy loop", func() {
		end := time.Now().Add(BlockFor)
		x := 0
		for time.Now().Before(end) {
			x++
		}
		_ = x
	})
}
