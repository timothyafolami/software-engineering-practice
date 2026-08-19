// 7.6 -- Go: GOMEMLIMIT, a soft limit, and the death spiral it can cause.
//
// WHAT THIS DEMONSTRATES
//   Go 1.19 added GOMEMLIMIT: instead of dying, the collector works harder
//   as you approach it. That is the right SHAPE of behaviour -- degrade,
//   do not disappear -- and it is the same idea as memory.high, implemented
//   in the runtime instead of the kernel.
//
//   But Go did NOT extend its 1.25 container-awareness work to memory.
//   There is no cgroup-derived default: GOMEMLIMIT is math.MaxInt64 unless
//   you set it, conventionally to around 90% of memory.max, leaving
//   headroom for the parts of RSS the Go heap does not cover -- goroutine
//   stacks, mmap'd files, cgo allocations, the runtime's own bookkeeping.
//   This program reads the actual value rather than quoting one.
//
//   The failure mode to know is the one nobody advertises. If LIVE data
//   genuinely exceeds the limit, the GC cannot satisfy it by collecting, so
//   it runs continuously and the process burns all its CPU collecting
//   instead of working. That is a soft limit doing exactly what it
//   promised, and it looks like a CPU problem -- which is how it collides
//   with everything in 7.2. Under a CPU quota, a GC death spiral drives the
//   throttle ratio up while an average-utilisation dashboard shows a
//   process pinned at its limit doing no work.
//
//     go run oom.go                            no limit: allocate to SIGKILL
//     GOMEMLIMIT=230MiB go run oom.go          the soft limit
//     GOMEMLIMIT=64MiB go run oom.go -pointers the soft limit with a heap the
//                                              GC actually has to trace
//
//   The -pointers flag is not decoration. The mark phase costs time per
//   POINTER, not per byte, so a []byte heap can sit far above GOMEMLIMIT
//   with GCCPUFraction near 1%. Verified on this machine both ways.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. Without GOMEMLIMIT: RSS climbs past memory.max and the process
//     vanishes. No panic, no defer, no output. Exit 137.
//  2. With GOMEMLIMIT below the live set AND -pointers: the process
//     SURVIVES and the "GC CPU %" column climbs, along with the GC count
//     and the wall clock. It is alive, it is making less progress per
//     second, and no memory alert will fire. Compare the same run without
//     GOMEMLIMIT -- that delta is the whole measurement.
//  3. The deferred function. It runs on a panic and on a normal return.
//     It does not run on SIGKILL. That is the entire lesson.
//
// RUN
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w golang:1.25 go run /w/oom.go
//   echo "exit code: $?"      # 137
//   docker run --rm --memory=256m -e GOMEMLIMIT=200MiB -v "$PWD:/w" -w /w \
//     golang:1.25 go run /w/oom.go
//
//   go run oom.go             # on this Mac: no cgroup, so a self-imposed cap
//
// On macOS there is no cgroup memory controller and nothing can OOM-kill
// this process, so with no limit to read it imposes its own and says so.
// GOMEMLIMIT works everywhere, though, so the GC-pressure half is
// reproducible on the host with -pointers, and it is worth seeing there.
package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const chunkMB = 8

// bytesPerNode is sizeof(gcNode): one pointer plus 48 bytes of padding.
const bytesPerNode = 64

// gcNode exists because of something this program originally got wrong, and
// the correction is the lesson. The GC's mark phase costs time proportional
// to the number of POINTERS it has to trace, not to the bytes retained. A
// heap made of []byte contains no pointers at all, so it can sit far above
// GOMEMLIMIT while GCCPUFraction stays near 1% -- GOMEMLIMIT makes the
// collector run more OFTEN, and each of those runs is nearly free.
//
// So -pointers retains a pointer-dense structure instead, which is what a
// real service's live set looks like (maps, structs, strings, slices of
// pointers). Run it both ways and compare the GC CPU column: that
// difference, not the byte count, is where the "GC death spiral" lives.
type gcNode struct {
	next *gcNode
	pad  [bytesPerNode - 8]byte
}

// ---------------------------------------------------------------- the kernel

func readOrEmpty(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// Bytes the cgroup will let this container charge, or 0 for no limit.
func memoryMax() int64 {
	if raw := readOrEmpty("/sys/fs/cgroup/memory.max"); raw != "" && raw != "max" {
		if value, err := strconv.ParseInt(raw, 10, 64); err == nil {
			return value
		}
	}
	if raw := readOrEmpty("/sys/fs/cgroup/memory/memory.limit_in_bytes"); raw != "" {
		// v1 spells "unlimited" as a number near 2^63, not as a word.
		if value, err := strconv.ParseInt(raw, 10, 64); err == nil && value < 1<<62 {
			return value
		}
	}
	return 0
}

func memoryEvents() string {
	raw := readOrEmpty("/sys/fs/cgroup/memory.events")
	if raw == "" {
		return "n/a"
	}
	return strings.ReplaceAll(raw, "\n", " ")
}

// RSS in MiB. /proc does not exist on Darwin, so the two branches use
// genuinely different mechanisms -- and ru_maxrss is a PEAK, in KiB on
// Linux and bytes on macOS.
func rssMB() float64 {
	if raw := readOrEmpty("/proc/self/status"); raw != "" {
		for _, line := range strings.Split(raw, "\n") {
			if strings.HasPrefix(line, "VmRSS:") {
				fields := strings.Fields(line)
				if len(fields) >= 2 {
					kb, _ := strconv.ParseFloat(fields[1], 64)
					return kb / 1024
				}
			}
		}
	}
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		return 0
	}
	if runtime.GOOS == "darwin" {
		return float64(usage.Maxrss) / (1024 * 1024) // bytes on Darwin
	}
	return float64(usage.Maxrss) / 1024 // KiB on Linux
}

func mib(bytes int64) string {
	if bytes == 0 {
		return "n/a"
	}
	return fmt.Sprintf("%.0f MiB", float64(bytes)/(1<<20))
}

func main() {
	selfLimitMB := flag.Int("limit-mb", 512,
		"self-imposed ceiling when there is no cgroup limit to hit")
	pointerHeap := flag.Bool("pointers", false,
		"retain a pointer-DENSE heap instead of []byte, so GOMEMLIMIT has "+
			"mark work to do and the GC-CPU column actually moves")
	flag.Parse()

	limit := memoryMax()
	memLimit := debug.SetMemoryLimit(-1) // -1 reads without setting

	// Every piece of shutdown handling a careful engineer would install.
	// Watch which of them get a chance to run.
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		sig := <-signals
		fmt.Printf("  [signal handler] caught %v -- shutting down cleanly\n", sig)
		os.Exit(128 + int(sig.(syscall.Signal)))
	}()
	defer func() {
		fmt.Printf("  [defer] final RSS %.0f MiB -- returning from main normally\n", rssMB())
	}()

	fmt.Println("7.6 -- memory: Go")
	fmt.Printf("  runtime      : %s %s/%s\n", runtime.Version(), runtime.GOOS, runtime.GOARCH)
	fmt.Printf("  memory.max   : %s\n", func() string {
		if limit == 0 {
			return "no limit / no cgroupfs"
		}
		return mib(limit)
	}())
	fmt.Printf("  memory.high  : %s   <- degrades instead of killing; no Compose key\n",
		func() string {
			raw := readOrEmpty("/sys/fs/cgroup/memory.high")
			if raw == "" || raw == "max" {
				return "unset"
			}
			return raw + " bytes"
		}())
	fmt.Printf("  GOMEMLIMIT   : %s\n", func() string {
		if memLimit == math.MaxInt64 {
			return "UNSET (math.MaxInt64). Go has no cgroup-derived default -- " +
				"1.25 fixed CPU and left this alone"
		}
		return mib(memLimit)
	}())
	fmt.Printf("  GOGC         : %s\n", func() string {
		if value, ok := os.LookupEnv("GOGC"); ok {
			return value
		}
		return "100 (default)"
	}())
	fmt.Printf("  starting RSS : %.0f MiB\n", rssMB())
	fmt.Println()

	if limit != 0 && memLimit == math.MaxInt64 {
		suggested := int64(float64(limit) * 0.9)
		fmt.Printf("  There IS a container limit (%s) and NO GOMEMLIMIT. That is the\n", mib(limit))
		fmt.Printf("  default state of essentially every Go service in production, and\n")
		fmt.Printf("  it means the OOM killer is your heap limit. Try:\n")
		fmt.Printf("    GOMEMLIMIT=%d go run oom.go\n", suggested)
		fmt.Printf("  (~90%% of memory.max, leaving headroom for stacks, mmap'd files\n")
		fmt.Printf("  and cgo -- the parts of RSS the Go heap does not cover.)\n")
		fmt.Println()
	}

	if limit == 0 {
		fmt.Printf("  !! No cgroup memory limit on this host, so nothing can OOM-kill\n")
		fmt.Printf("  !! this process. It will stop ITSELF at %d MiB and say so.\n", *selfLimitMB)
		fmt.Printf("  !! GOMEMLIMIT still works here, though. To make its cost VISIBLE\n")
		fmt.Printf("  !! you need a heap the collector has to trace, so combine it with\n")
		fmt.Printf("  !! -pointers:\n")
		fmt.Printf("  !!   GOMEMLIMIT=64MiB go run oom.go -pointers\n")
		fmt.Printf("  !! Compare the GC CPU column against the same run without\n")
		fmt.Printf("  !! GOMEMLIMIT. For the KILL:\n")
		fmt.Printf("  !!   docker run --rm --memory=256m -v \"$PWD:/w\" -w /w \\\n")
		fmt.Printf("  !!     golang:1.25 go run /w/oom.go\n")
		fmt.Println()
	}

	ceilingMB := *selfLimitMB
	if limit != 0 {
		ceilingMB = int(float64(limit) / (1 << 20) * 1.5)
		fmt.Printf("  Allocating toward %d MiB against a %s limit.\n", ceilingMB, mib(limit))
	}
	fmt.Println("  Every chunk is written to. Under Linux's default overcommit the")
	fmt.Println("  allocation itself is free -- the cgroup charge lands on the WRITE.")
	if *pointerHeap {
		fmt.Printf("  Heap shape: POINTER-DENSE (%d-byte nodes, %d per %d MiB chunk).\n",
			bytesPerNode, (chunkMB<<20)/bytesPerNode, chunkMB)
		fmt.Println("  The GC has to trace every one of them, so GOMEMLIMIT costs CPU here.")
	} else {
		fmt.Println("  Heap shape: []byte, which holds NO pointers. The GC has almost")
		fmt.Println("  nothing to trace, so expect GC CPU to stay low even under a")
		fmt.Println("  GOMEMLIMIT far below the live set. Re-run with -pointers to see")
		fmt.Println("  the other half.")
	}
	fmt.Println()

	var stats runtime.MemStats
	var gcStats debug.GCStats
	start := time.Now()
	var blocks [][]byte
	var nodes []*gcNode
	var tail *gcNode
	allocated := 0

	for allocated < ceilingMB {
		if *pointerHeap {
			for i := 0; i < (chunkMB<<20)/bytesPerNode; i++ {
				node := &gcNode{next: tail}
				node.pad[0] = 1 // touch the page, same as the []byte path
				tail = node
				nodes = append(nodes, node)
			}
		} else {
			block := make([]byte, chunkMB<<20)
			// Touch every page. One byte per 4 KiB is enough; the charge is
			// per page, not per byte.
			for offset := 0; offset < len(block); offset += 4096 {
				block[offset] = 1
			}
			blocks = append(blocks, block)
		}
		allocated += chunkMB

		if allocated%32 == 0 || (limit != 0 && int64(allocated)<<20 > limit*8/10) {
			runtime.ReadMemStats(&stats)
			debug.ReadGCStats(&gcStats)
			elapsed := time.Since(start).Seconds()
			// GCCPUFraction is the runtime's own estimate of the share of
			// total CPU spent in GC since the program started. Above ~0.25
			// you are in the spiral; at 0.5 the process is collecting more
			// than it is working.
			fmt.Printf("    allocated %5d MiB   RSS %6.0f MiB   heap %6.0f MiB   "+
				"GC CPU %5.1f%%   GCs %3d   %5.1fs   %s\n",
				allocated, rssMB(), float64(stats.HeapAlloc)/(1<<20),
				stats.GCCPUFraction*100, gcStats.NumGC, elapsed, memoryEvents())
		}
	}

	fmt.Println()
	runtime.ReadMemStats(&stats)
	fmt.Printf("  Reached %d MiB without being killed. GC CPU fraction %.1f%%.\n",
		allocated, stats.GCCPUFraction*100)
	if limit == 0 {
		fmt.Println("  Expected: no cgroup here to kill anything, and the self-imposed")
		fmt.Println("  ceiling stopped the loop. Nothing was enforced.")
	} else if memLimit != math.MaxInt64 {
		fmt.Println("  With GOMEMLIMIT set below the live set, this is the SOFT limit")
		fmt.Println("  working: the process survived and paid for it in CPU. Read the")
		fmt.Println("  GC CPU column as the price -- but only -pointers gives the")
		fmt.Println("  collector enough mark work for that price to show up. If it")
		fmt.Println("  stayed near 1% on a []byte heap, that is correct, not a bug:")
		fmt.Println("  bytes are not what the mark phase costs. If it climbed while")
		fmt.Println("  throughput went to nothing, you have the death spiral -- and")
		fmt.Println("  under a CPU quota (7.2) that same CPU is now draining your")
		fmt.Println("  bucket, so the memory problem arrives at your on-call as a")
		fmt.Println("  latency problem with a healthy-looking memory graph.")
	} else {
		fmt.Println("  NOT expected under a memory limit with no GOMEMLIMIT. The kernel")
		fmt.Println("  reclaimed enough to keep up -- allocate faster, or check that")
		fmt.Println("  memory.max is what you think it is.")
	}

	fmt.Println()
	fmt.Println("  Where the evidence lives when this DOES get killed:")
	fmt.Println("    docker inspect <c> --format '{{.State.OOMKilled}}'   -> true")
	fmt.Println("    exit code                                           -> 137 (128 + SIGKILL)")
	fmt.Println("    cat /sys/fs/cgroup/memory.events                    -> oom_kill incremented")
	fmt.Println("    dmesg on the host                                   -> the kill decision")
	fmt.Println("    this program's own output                           -> nothing after the")
	fmt.Println("                                                           last allocated line")
	fmt.Println()
	fmt.Println("  Note there is no panic and no recover() involved anywhere. A Go")
	fmt.Println("  process that is OOM-killed does not panic -- panics are a Go")
	fmt.Println("  concept and SIGKILL happens outside the runtime entirely.")

	// Keep the slice alive to the very end so the GC cannot quietly rescue
	// us and turn this into a different, much less interesting program.
	runtime.KeepAlive(blocks)
	runtime.KeepAlive(nodes)
}
