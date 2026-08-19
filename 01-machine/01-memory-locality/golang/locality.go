// Layer 1 - Memory & cache locality
// Same pointer-chasing benchmark: two physical layouts of the same logical
// traversal.
//
// sequential -> node i's successor lives at i+1 (cache-friendly)
// shuffled   -> node i's successor is a random other node (cache-hostile)
package main

import (
	"fmt"
	"math/rand"
	"runtime"
	"time"
)

const N = 2_000_000
const Laps = 5

func build(shuffled bool) (values []int32, next []int32) {
	values = make([]int32, N)
	next = make([]int32, N)
	for i := 0; i < N; i++ {
		values[i] = int32(i)
	}
	if !shuffled {
		for i := 0; i < N; i++ {
			next[i] = int32((i + 1) % N)
		}
		return
	}
	perm := rand.Perm(N)
	for i := 0; i < N; i++ {
		next[perm[i]] = int32(perm[(i+1)%N])
	}
	return
}

func traverse(values, next []int32, laps int) int64 {
	var total int64
	idx := int32(0)
	steps := N * laps
	for s := 0; s < steps; s++ {
		total += int64(values[idx])
		idx = next[idx]
	}
	return total
}

func bench(label string, shuffled bool) {
	values, next := build(shuffled)
	start := time.Now()
	total := traverse(values, next, Laps)
	elapsed := time.Since(start)
	nsPerStep := float64(elapsed.Nanoseconds()) / float64(N*Laps)
	fmt.Printf("%-10s  total=%15d  time=%6.3fs  %6.1f ns/step\n",
		label, total, elapsed.Seconds(), nsPerStep)
}

func main() {
	fmt.Printf("N=%d laps=%d (go %s)\n", N, Laps, runtime.Version())
	bench("sequential", false)
	bench("shuffled", true)
}
