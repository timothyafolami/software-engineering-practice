// Layer 1 - The race that actually bites in practice: check-then-act on a
// shared cache. Also demonstrates something Go does that neither Python
// nor Rust do: its built-in maps are explicitly NOT safe for concurrent
// access, and the runtime actively detects unsynchronized concurrent
// read+write and crashes the program on the spot with "fatal error:
// concurrent map read and map write" rather than silently corrupting the
// map. That's a deliberate design choice -- fail loudly, immediately,
// every time -- and it's why you'll see this panic in real Go programs
// far more often than a quietly-wrong result.
package main

import (
	"fmt"
	"sync"
	"time"
)

const Goroutines = 8

func computeFactory() (func() int, *int) {
	calls := 0
	compute := func() int {
		calls++
		time.Sleep(2 * time.Millisecond) // stands in for a DB query / API call
		return 42
	}
	return compute, &calls
}

func stampedeUnsafe() (calls int) {
	compute, callsPtr := computeFactory()
	cache := map[string]int{}
	var wg sync.WaitGroup
	defer func() {
		if r := recover(); r != nil {
			fmt.Printf("  -> runtime panic: %v\n", r)
			calls = *callsPtr
		}
	}()
	for i := 0; i < Goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, ok := cache["shared-key"]; !ok {
				cache["shared-key"] = compute()
			}
		}()
	}
	wg.Wait()
	return *callsPtr
}

func stampedeSafe() int {
	compute, callsPtr := computeFactory()
	cache := map[string]int{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for i := 0; i < Goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			mu.Lock()
			defer mu.Unlock()
			if _, ok := cache["shared-key"]; !ok {
				cache["shared-key"] = compute()
			}
		}()
	}
	wg.Wait()
	return *callsPtr
}

func main() {
	fmt.Println("unsafe (no lock, plain map) -- may crash, may just double-compute:")
	unsafeCalls := stampedeUnsafe()
	fmt.Printf("  compute() ran %d times (should be 1)\n", unsafeCalls)

	fmt.Println("safe (sync.Mutex around the check-and-fill):")
	safeCalls := stampedeSafe()
	fmt.Printf("  compute() ran %d times (should be 1)\n", safeCalls)
}
