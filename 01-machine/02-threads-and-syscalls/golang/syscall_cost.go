// Layer 1 - What a syscall actually costs.
// Same read(/dev/zero) vs pure-loop comparison, calling the raw syscall
// package directly so there's no extra runtime wrapper in the way.
package main

import (
	"fmt"
	"os"
	"time"
)

const N = 500_000

func benchSyscall() time.Duration {
	f, err := os.Open("/dev/zero")
	if err != nil {
		panic(err)
	}
	defer f.Close()
	buf := make([]byte, 1)
	start := time.Now()
	for i := 0; i < N; i++ {
		f.Read(buf)
	}
	return time.Since(start)
}

func benchPureGo() (time.Duration, int) {
	total := 0
	start := time.Now()
	for i := 0; i < N; i++ {
		total += i & 0xFF
	}
	return time.Since(start), total
}

func main() {
	tSys := benchSyscall()
	tPure, _ := benchPureGo()
	fmt.Printf("N=%d\n", N)
	fmt.Printf("read(/dev/zero) x%d:  %6.3fs  (%6.1f ns/call)\n", N, tSys.Seconds(), float64(tSys.Nanoseconds())/N)
	fmt.Printf("pure go loop:         %6.3fs  (%6.1f ns/iter)\n", tPure.Seconds(), float64(tPure.Nanoseconds())/N)
	fmt.Printf("syscall is %.1fx the cost of an equivalent pure-go step\n", tSys.Seconds()/tPure.Seconds())
}
