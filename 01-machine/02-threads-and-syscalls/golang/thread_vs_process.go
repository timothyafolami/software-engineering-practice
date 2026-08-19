// Layer 1 - Goroutine vs OS thread vs process creation cost.
// Go gives us three tiers to compare: a goroutine (green thread, ~2KB
// starting stack, scheduled by the Go runtime), a real OS thread (via
// runtime.LockOSThread forcing a 1:1 goroutine-to-thread), and a full
// process (exec).
package main

import (
	"fmt"
	"os/exec"
	"runtime"
	"sync"
	"time"
)

const N = 200

func benchGoroutines() time.Duration {
	start := time.Now()
	for i := 0; i < N; i++ {
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
		}()
		wg.Wait()
	}
	return time.Since(start)
}

func benchOSThreads() time.Duration {
	start := time.Now()
	for i := 0; i < N; i++ {
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Pins this goroutine to its own OS thread for its lifetime,
			// forcing the runtime to actually create one instead of reusing
			// an idle thread from its pool.
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()
		}()
		wg.Wait()
	}
	return time.Since(start)
}

func benchProcesses() time.Duration {
	start := time.Now()
	for i := 0; i < N; i++ {
		cmd := exec.Command("true")
		cmd.Run()
	}
	return time.Since(start)
}

func main() {
	tGo := benchGoroutines()
	tThread := benchOSThreads()
	tProc := benchProcesses()
	fmt.Printf("N=%d\n", N)
	fmt.Printf("goroutine spawn+join: %6.3fs  (%7.1f us/goroutine)\n", tGo.Seconds(), float64(tGo.Microseconds())/N)
	fmt.Printf("locked OS thread:     %6.3fs  (%7.1f us/thread)\n", tThread.Seconds(), float64(tThread.Microseconds())/N)
	fmt.Printf("process spawn+wait:   %6.3fs  (%7.1f us/process)\n", tProc.Seconds(), float64(tProc.Microseconds())/N)
	fmt.Printf("OS thread is %.1fx a goroutine; process is %.1fx a goroutine\n",
		tThread.Seconds()/tGo.Seconds(), tProc.Seconds()/tGo.Seconds())
}
