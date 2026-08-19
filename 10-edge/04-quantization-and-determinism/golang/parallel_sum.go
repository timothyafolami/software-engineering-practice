// Layer 10 - Topic 4: the batch-invariance finding, on a CPU, with no
// kernels and no GPU. (Go)
//
// What this demonstrates
//
//	Thinking Machines Lab's Defeating Nondeterminism in LLM Inference
//	(September 2025) traced temperature-0 nondeterminism to kernels that
//	are not BATCH-INVARIANT: a reduction split differently across a
//	different batch shape sums the same numbers in a different order,
//	floating-point addition is not associative, and the last bits differ.
//	Occasionally those bits cross an argmax boundary between two close
//	logits and the sequences diverge completely.
//
//	This file is that mechanism with everything else removed. Ten million
//	float32 values, one slice, summed by W goroutines and combined. W is
//	the only thing that changes. The Go spec is strict about float
//	semantics -- no reassociation, no surprise FMA -- so what you are
//	seeing is purely partition order.
//
// What to look for
//
//   - `distinct sums` across the W values. More than one means the
//     result of a pure function of a fixed input depended on how the
//     work was divided.
//
//   - Bit patterns (%b prints the exact mantissa and exponent). Two sums
//     that print the same in %f can differ here, which is why decimal
//     formatting is not a way to check determinism.
//
//   - `same W, repeated` at the bottom: each W is DETERMINISTIC on its
//     own. This is not a race. Run it a hundred times and W=8 always
//     gives the same answer -- it is just a different answer from W=4.
//
//   - The relative spread against float32's epsilon. It is tiny, and
//     tiny is exactly the size of an argmax flip between two close
//     logits.
//
//     Then the sentence to carry out of this topic: your result depended
//     on how the work was partitioned, and the partitioning depended on
//     load. Go find that bug in something you already own.
//
// No dependencies. Runs with no arguments (-workers overrides the default
// list):
//
//	cd golang && go run parallel_sum.go
//	cd golang && go run parallel_sum.go -workers 1,3,7,64
package main

import (
	"flag"
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strconv"
	"strings"
	"sync"
)

const (
	n    = 10_000_000
	seed = 20260818
)

// sumPartitioned splits data across w goroutines, sums each chunk in
// float32, and adds the partials in order. Deterministic for a given w.
func sumPartitioned(data []float32, w int) float32 {
	if w <= 1 {
		var s float32
		for _, v := range data {
			s += v
		}
		return s
	}
	partials := make([]float32, w)
	chunk := (len(data) + w - 1) / w
	var wg sync.WaitGroup
	for i := 0; i < w; i++ {
		lo := i * chunk
		if lo >= len(data) {
			break
		}
		hi := lo + chunk
		if hi > len(data) {
			hi = len(data)
		}
		wg.Add(1)
		go func(i, lo, hi int) {
			defer wg.Done()
			var s float32
			for _, v := range data[lo:hi] {
				s += v
			}
			partials[i] = s
		}(i, lo, hi)
	}
	wg.Wait()
	// Combining in index order keeps this deterministic per w: the only
	// variable in the whole program is how many pieces there were.
	var total float32
	for _, p := range partials {
		total += p
	}
	return total
}

func main() {
	workersFlag := flag.String("workers", "1,2,4,8,16,32,64",
		"comma-separated goroutine counts")
	flag.Parse()

	var workers []int
	for _, f := range strings.Split(*workersFlag, ",") {
		v, err := strconv.Atoi(strings.TrimSpace(f))
		if err != nil {
			panic(err)
		}
		workers = append(workers, v)
	}

	rng := rand.New(rand.NewSource(seed))
	data := make([]float32, n)
	for i := range data {
		// Values around 1.0 so the running sum grows to ~1e7 while each
		// addend stays ~1 -- the regime where float32 starts losing the
		// low bits of every addition, which is the regime a real reduction
		// over activations lives in.
		data[i] = float32(rng.Float64() + 0.5)
	}

	// The reference: the same numbers summed in float64, which has enough
	// bits that partition order does not reach the printed digits.
	var exact float64
	for _, v := range data {
		exact += float64(v)
	}

	fmt.Println("Partition-order nondeterminism -- Go, float32, no GPU involved")
	fmt.Printf("  %d values ~U(0.5, 1.5), seed %d\n", n, seed)
	fmt.Printf("  float64 reference sum: %.6f\n\n", exact)

	fmt.Printf("  %8s %20s %26s %14s\n", "workers", "sum (float32)", "exact bits", "rel err")
	fmt.Println("  " + strings.Repeat("-", 72))
	seen := map[float32]bool{}
	var values []float64
	for _, w := range workers {
		s := sumPartitioned(data, w)
		seen[s] = true
		values = append(values, float64(s))
		fmt.Printf("  %8d %20.6f %26b %14.3e\n", w, s, s,
			math.Abs(float64(s)-exact)/exact)
	}

	fmt.Printf("\n  distinct sums across %d partitionings: %d\n", len(workers), len(seen))
	sort.Float64s(values)
	spread := (values[len(values)-1] - values[0]) / exact
	fmt.Printf("  relative spread: %.3e   (float32 epsilon: %.3e)\n",
		spread, math.Nextafter32(1, 2)-1)

	fmt.Println("\n  same W, repeated -- this is not a race:")
	for _, w := range []int{4, 8} {
		a := sumPartitioned(data, w)
		b := sumPartitioned(data, w)
		c := sumPartitioned(data, w)
		fmt.Printf("    W=%-3d %b  %b  %b   identical: %v\n", w, a, b, c,
			a == b && b == c)
	}

	fmt.Println("\n  Each partitioning is perfectly reproducible on its own. What is")
	fmt.Println("  not reproducible is WHICH partitioning you get, because on a real")
	fmt.Println("  inference server that is chosen by the batch shape, and the batch")
	fmt.Println("  shape is chosen by other people's traffic. That is the whole of")
	fmt.Println("  the temperature-0 nondeterminism finding, with the GPU removed.")
}
