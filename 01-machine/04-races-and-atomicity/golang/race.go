// Layer 1 - Why `counter++` is not atomic.
// counter++ is a read-modify-write: load the value, add one, store it back.
// With multiple goroutines doing this on the same variable with no
// synchronization, two goroutines can both read the same value before
// either writes back, and one increment is silently lost. Build and run
// with `go run -race race.go` to have Go's race detector prove it
// instrumentally instead of just by counting lost updates.
package main

import (
	"fmt"
	"sync"
	"sync/atomic"
)

const Goroutines = 8
const Increments = 300_000

func runUnsafe() int64 {
	var counter int64
	var wg sync.WaitGroup
	for i := 0; i < Goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < Increments; j++ {
				counter++ // racy: not synchronized
			}
		}()
	}
	wg.Wait()
	return counter
}

func runMutex() int64 {
	var counter int64
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i := 0; i < Goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < Increments; j++ {
				mu.Lock()
				counter++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	return counter
}

func runAtomic() int64 {
	var counter int64
	var wg sync.WaitGroup
	for i := 0; i < Goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < Increments; j++ {
				atomic.AddInt64(&counter, 1)
			}
		}()
	}
	wg.Wait()
	return counter
}

func main() {
	expected := int64(Goroutines * Increments)
	unsafeResult := runUnsafe()
	mutexResult := runMutex()
	atomicResult := runAtomic()
	fmt.Printf("expected:               %d\n", expected)
	fmt.Printf("unsafe (counter++):     %d  (lost %d updates)\n", unsafeResult, expected-unsafeResult)
	fmt.Printf("safe (sync.Mutex):      %d\n", mutexResult)
	fmt.Printf("safe (atomic.AddInt64): %d\n", atomicResult)
}
