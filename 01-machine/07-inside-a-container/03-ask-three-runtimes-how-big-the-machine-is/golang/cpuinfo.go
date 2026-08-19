// 7.3 -- Go: fixed as of 1.25, and worth knowing precisely which side of
// that line your toolchain is on.
//
// WHAT THIS DEMONSTRATES
//   Go has two numbers and they answer different questions:
//
//     runtime.NumCPU()      -> logical CPUs constrained by the AFFINITY mask.
//                              Question (2). Moves under cpuset.cpus, never
//                              under --cpus.
//     runtime.GOMAXPROCS(0) -> how many OS threads may run Go code at once.
//                              Since 1.25 this defaults to the minimum of
//                              logical CPUs, the affinity mask, and the
//                              cgroup CPU bandwidth limit -- rounding
//                              fractional limits UP, never below 2 unless the
//                              machine has fewer, re-checked about once a
//                              second so a live limit change is picked up.
//
//   This program does not assume which behaviour you have. It detects it,
//   because the answer changes what every row below means -- and because
//   this repo's own machine is on go1.24, where the old behaviour is the
//   only behaviour, which is exactly the situation most production images
//   are still in.
//
//   The interesting cell is the rounding. At `--cpus=1.5` Go deliberately
//   runs 2 threads against 1.5 CPUs of quota, so the fixed default is still
//   throttleable. Predict that number before you look at it.
//
//   Note the asymmetry with memory: Go fixed CPU and left memory alone.
//   There is still no cgroup-derived GOMEMLIMIT default -- this probe reads
//   the actual value rather than quoting one.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. NumCPU() next to GOMAXPROCS(0) next to the enforced quota. On a
//      container-aware toolchain under a quota, the second and third agree
//      and the first does not.
//   2. Re-run with GODEBUG=containermaxprocs=0 to get the pre-1.25 answer
//      from the same binary. On a pre-1.25 toolchain that flag does nothing,
//      and the program says so instead of pretending.
//   3. The thread count. GOMAXPROCS is not the whole story: GC mark workers
//      (a fraction of GOMAXPROCS) and sysmon are in the same cgroup.
//
// RUN
//   go run cpuinfo.go
//   GODEBUG=containermaxprocs=0 go run cpuinfo.go   # the pre-1.25 answer
//   GOMAXPROCS=2 go run cpuinfo.go                  # explicit: disables both
//
//   Inside a Linux container, which is where the columns separate:
//     docker run --rm --cpus=1.5 -v "$PWD:/w" -w /w golang:1.25 go run /w/cpuinfo.go
package main

import (
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
)

// ---------------------------------------------------------------- the kernel

// CPUs of bandwidth the cgroup actually enforces, or ok=false for no ceiling.
// Twenty lines and no dependencies. Since 1.25 the Go runtime has its own
// copy of this function; before 1.25 you imported uber-go/automaxprocs to get
// one, and that import in a service today is redundant rather than wrong.
func readCPUMax() (float64, bool) {
	if raw, err := os.ReadFile("/sys/fs/cgroup/cpu.max"); err == nil {
		fields := strings.Fields(string(raw))
		if len(fields) >= 1 && fields[0] != "max" {
			quota, err1 := strconv.ParseFloat(fields[0], 64)
			period := 100000.0
			if len(fields) > 1 {
				if p, err2 := strconv.ParseFloat(fields[1], 64); err2 == nil {
					period = p
				}
			}
			if err1 == nil && period > 0 {
				return quota / period, true
			}
		}
		return 0, false // "max <period>": a cgroup exists, with no ceiling
	}
	quota, err1 := readInt("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
	period, err2 := readInt("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
	if err1 == nil && err2 == nil && quota > 0 && period > 0 {
		return float64(quota) / float64(period), true
	}
	return 0, false
}

func readInt(path string) (int64, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
}

func readString(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "n/a"
	}
	return strings.TrimSpace(string(raw))
}

// containerAwareToolchain reports whether GOMAXPROCS defaults to a
// cgroup-derived value on this toolchain (Go 1.25+, August 2025).
//
// Parse the version rather than trusting the go.mod language version: the
// toolchain that BUILT the binary is what decides, and a go.mod saying
// "go 1.25" compiled by a 1.24 toolchain still has 1.24's default.
func containerAwareToolchain() bool {
	version := runtime.Version() // e.g. "go1.24.5" or "devel ..."
	if !strings.HasPrefix(version, "go1.") {
		return false
	}
	rest := strings.TrimPrefix(version, "go1.")
	end := strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' })
	if end == -1 {
		end = len(rest)
	}
	minor, err := strconv.Atoi(rest[:end])
	return err == nil && minor >= 25
}

// ------------------------------------------------------------------- output

func printTable(headers []string, rows [][]string) {
	widths := make([]int, len(headers))
	for i, h := range headers {
		widths[i] = len(h)
		for _, row := range rows {
			if len(row[i]) > widths[i] {
				widths[i] = len(row[i])
			}
		}
	}
	emit := func(cells []string) {
		parts := make([]string, len(cells))
		for i, c := range cells {
			parts[i] = c + strings.Repeat(" ", widths[i]-len(c))
		}
		fmt.Println(strings.Join(parts, "  "))
	}
	emit(headers)
	rule := make([]string, len(headers))
	for i := range headers {
		rule[i] = strings.Repeat("-", widths[i])
	}
	emit(rule)
	for _, row := range rows {
		emit(row)
	}
}

func main() {
	quota, haveQuota := readCPUMax()
	numCPU := runtime.NumCPU()
	maxProcs := runtime.GOMAXPROCS(0)
	aware := containerAwareToolchain()

	fmt.Println("7.3 -- how big is this machine? Go's answers")
	fmt.Printf("  runtime     : %s on %s/%s\n", runtime.Version(), runtime.GOOS, runtime.GOARCH)
	fmt.Printf("  GODEBUG     : %q\n", os.Getenv("GODEBUG"))
	fmt.Printf("  GOMAXPROCS env: %q\n", os.Getenv("GOMAXPROCS"))
	fmt.Println()

	quotaCell := "n/a"
	if haveQuota {
		quotaCell = fmt.Sprintf("%.2f", quota)
	}
	printTable(
		[]string{"what people call", "the call", "answer here", "which question it answers", "what it tracks"},
		[][]string{
			{"runtime.NumCPU()", "runtime.NumCPU()", strconv.Itoa(numCPU),
				"(2) which CPUs may I use", "affinity mask, NOT cpu.max"},
			{"runtime.GOMAXPROCS(0)", "runtime.GOMAXPROCS(0)", strconv.Itoa(maxProcs),
				"(3) since 1.25 only", "min(logical, affinity, cpu.max)"},
			{"/sys/fs/cgroup/cpu.max", "os.ReadFile(...)", quotaCell,
				"(3) how much CPU TIME may I consume", "cpu.max -- THE ENFORCED NUMBER"},
		})
	fmt.Println()

	fmt.Println("  ground truth on this host:")
	fmt.Printf("    cpu.max               %s\n", readString("/sys/fs/cgroup/cpu.max"))
	fmt.Printf("    cpuset.cpus.effective %s\n", readString("/sys/fs/cgroup/cpuset.cpus.effective"))
	fmt.Printf("    memory.max            %s\n", readString("/sys/fs/cgroup/memory.max"))
	fmt.Println()

	if aware {
		fmt.Println("  This toolchain (>= 1.25) DOES derive GOMAXPROCS from the cgroup.")
		fmt.Println("    * fractional limits round UP, so --cpus=1.5 gives GOMAXPROCS=2:")
		fmt.Println("      two threads against 1.5 CPUs of quota, still throttleable.")
		fmt.Println("    * never below 2 unless the machine genuinely has fewer.")
		fmt.Println("    * re-checked up to once a second, so a live limit change lands.")
		fmt.Println("    * GODEBUG=containermaxprocs=0 restores the pre-1.25 behaviour;")
		fmt.Println("      GODEBUG=updatemaxprocs=0 keeps the initial value but stops the")
		fmt.Println("      re-check; setting GOMAXPROCS explicitly disables both.")
	} else {
		fmt.Println("  This toolchain is PRE-1.25: GOMAXPROCS ignores cpu.max entirely.")
		fmt.Println("    GOMAXPROCS above is the affinity-constrained CPU count and")
		fmt.Println("    nothing else, so on an 8-core host under --cpus=1.0 this binary")
		fmt.Println("    runs eight threads flat out inside a bucket sized for one --")
		fmt.Println("    the textbook version of 7.2's failure, drained in ~12.5ms.")
		fmt.Println("    GODEBUG=containermaxprocs=0 does nothing here; there is no new")
		fmt.Println("    behaviour to turn off. The fixes available on this toolchain:")
		fmt.Println("      * import go.uber.org/automaxprocs (the standard pre-1.25 fix)")
		fmt.Println("      * set GOMAXPROCS from whatever sets `cpus:` in the manifest")
		fmt.Println("      * upgrade the toolchain, which is the real answer")
	}
	fmt.Println()

	if haveQuota {
		rounded := int(quota)
		if float64(rounded) < quota {
			rounded++
		}
		if rounded < 2 {
			rounded = 2
		}
		if numCPU < rounded {
			rounded = numCPU
		}
		fmt.Printf("  What 1.25's rule would compute here: min(%d logical, affinity, "+
			"ceil(%.2f)) -> %d\n", numCPU, quota, rounded)
		if !aware {
			fmt.Printf("  What this toolchain actually set:    %d\n", maxProcs)
			fmt.Println("  The gap between those two lines is the bug, and it is silent.")
		}
	} else {
		fmt.Println("  NOTE: no CPU quota is enforced here, so GOMAXPROCS and NumCPU agree")
		fmt.Println("        and the matrix has one column. That is the correct result on")
		fmt.Println("        this host. Run it under --cpus=1.5 and the rows separate.")
	}
	fmt.Println()

	// GOMAXPROCS is not the thread count. The GC's mark workers are a
	// fraction of GOMAXPROCS, sysmon is always there, and every one of them
	// draws on the same bucket as your handlers.
	fmt.Println("  GOMAXPROCS is not the whole thread footprint:")
	fmt.Printf("    GOMAXPROCS               %d   (threads that may run Go code at once)\n", maxProcs)
	fmt.Printf("    + GC mark workers        ~%d  (25%% of GOMAXPROCS, by design)\n", (maxProcs+3)/4)
	fmt.Println("    + sysmon                 1   (always, and it never sleeps for long)")
	fmt.Println("    + one thread per blocked cgo call or blocking syscall")
	fmt.Println("    Every one of those is in the same cgroup, spending the same quota.")
	fmt.Println()

	// The asymmetry worth remembering: Go fixed CPU and did not fix memory.
	memLimit := debug.SetMemoryLimit(-1) // -1 reads without setting
	fmt.Println("  And the axis Go did NOT fix:")
	fmt.Printf("    GOMEMLIMIT               %s\n", func() string {
		if memLimit == 1<<63-1 {
			return "unset (math.MaxInt64 -- no soft limit)"
		}
		return fmt.Sprintf("%.0f MiB", float64(memLimit)/(1<<20))
	}())
	fmt.Println("    There is no cgroup-derived default. Go's 1.25 container-awareness")
	fmt.Println("    work covered CPU and stopped there -- set GOMEMLIMIT explicitly,")
	fmt.Println("    conventionally ~90% of memory.max. That is 7.6.")
}
