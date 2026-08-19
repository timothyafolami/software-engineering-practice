// Layer 2 · Topic 5 - Go: one binary, two resolvers, and a runtime heuristic
// choosing between them behind your back.
//
// Go ships a pure-Go resolver that reads /etc/resolv.conf and speaks DNS
// itself, AND a cgo resolver that calls the system getaddrinfo. Which one you
// get is decided by runtime heuristics at lookup time -- what is in
// resolv.conf, whether nsswitch.conf asks for anything the pure-Go resolver
// cannot do, whether cgo is even available in this build. You can force the
// choice with GODEBUG=netdns=go or netdns=cgo, and add +2 for the resolver's
// own debug output.
//
// This matters in containers more than anywhere else: the pure-Go resolver
// does not implement everything nsswitch.conf can express, so the SAME binary
// can resolve differently on a glibc host and in an Alpine image. A base-image
// change with no code change is a real and regularly-shipped bug.
//
// This program runs itself three times as a child process -- default,
// netdns=go, netdns=cgo -- because GODEBUG is read at startup and cannot be
// changed from inside a running process. Each child resolves the same names,
// times them, and reports what the resolver's own debug output said.
//
// What to look for in the output:
//   - the "resolver debug" line for each mode. That is Go telling you, in its
//     own words, which code path ran.
//   - repeated lookups of the same name, all costing about the same: Go
//     caches nothing, so every one of those is a real query to something.
//   - on macOS specifically: /etc/resolv.conf carries a notice saying it is
//     NOT consulted for hostname resolution. The pure-Go resolver reads it
//     anyway, because it was written for Linux. That is exactly the class of
//     surprise this file exists to make visible.
//
// Run: go run two_resolvers_one_binary.go
package main

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

var names = []string{"localhost", "example.com"}

const lookupsPerName = 3

func child() {
	fmt.Printf("    GODEBUG=%s\n", orNone(os.Getenv("GODEBUG")))
	var r net.Resolver
	for _, name := range names {
		var times []string
		var got string
		for i := 0; i < lookupsPerName; i++ {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			t0 := time.Now()
			addrs, err := r.LookupHost(ctx, name)
			cancel()
			d := time.Since(t0)
			if err != nil {
				times = append(times, fmt.Sprintf("ERR %v", err))
				continue
			}
			times = append(times, fmt.Sprintf("%.1fms", float64(d.Microseconds())/1000))
			if got == "" {
				got = strings.Join(addrs, ",")
				if len(got) > 44 {
					got = got[:44] + "..."
				}
			}
		}
		fmt.Printf("      %-14s %-28s %s\n", name, strings.Join(times, "  "), got)
	}
}

func orNone(s string) string {
	if s == "" {
		return "(unset -- the runtime heuristic decides)"
	}
	return s
}

func runChild(label, godebug string) {
	exe, err := os.Executable()
	if err != nil {
		fmt.Println("    could not find our own executable:", err)
		return
	}
	cmd := exec.Command(exe)
	cmd.Env = append(os.Environ(), "DNS_CHILD=1")
	if godebug != "" {
		cmd.Env = append(cmd.Env, "GODEBUG="+godebug)
	} else {
		// Drop any GODEBUG we inherited so "default" means default.
		cmd.Env = withoutGodebug(cmd.Env)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	fmt.Printf("  %s\n", label)
	if err := cmd.Run(); err != nil {
		fmt.Printf("    child exited with %v\n", err)
	}
	fmt.Print(stdout.String())

	// The +2 suffix makes the resolver print which path it took, on stderr.
	debug := strings.TrimSpace(stderr.String())
	if debug == "" {
		fmt.Println("    resolver debug: (none printed)")
	} else {
		for _, line := range strings.Split(debug, "\n") {
			fmt.Printf("    resolver debug: %s\n", line)
		}
	}
	fmt.Println()
}

func withoutGodebug(env []string) []string {
	out := env[:0]
	for _, kv := range env {
		if !strings.HasPrefix(kv, "GODEBUG=") {
			out = append(out, kv)
		}
	}
	return out
}

func resolvConfNote() {
	b, err := os.ReadFile("/etc/resolv.conf")
	if err != nil {
		fmt.Printf("  /etc/resolv.conf: unreadable (%v)\n", err)
		return
	}
	fmt.Println("  /etc/resolv.conf, first lines -- the file the PURE-GO resolver reads:")
	lines := strings.Split(string(b), "\n")
	for i, l := range lines {
		if i >= 6 {
			fmt.Println("    ...")
			break
		}
		fmt.Printf("    | %s\n", l)
	}
	if runtime.GOOS == "darwin" {
		fmt.Println("    ^ read that notice. On macOS this file is NOT consulted by the")
		fmt.Println("      system resolver; resolution goes through libinfo/mDNSResponder.")
		fmt.Println("      The pure-Go resolver reads it anyway, because it was written")
		fmt.Println("      against Linux. Two resolvers in one binary, disagreeing about")
		fmt.Println("      where configuration even lives.")
	}
	fmt.Println()
}

func main() {
	if os.Getenv("DNS_CHILD") != "" {
		child()
		return
	}

	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Go: two resolvers in one binary, and a heuristic picking between them")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Printf("  %s on %s/%s\n", runtime.Version(), runtime.GOOS, runtime.GOARCH)
	fmt.Println("  (A binary built with CGO_ENABLED=0 -- which is most container images --")
	fmt.Println("   has only the pure-Go resolver, and netdns=cgo is then silently ignored.")
	fmt.Println("   The resolver debug lines below are how you find out which one ran.)")
	fmt.Println()

	resolvConfNote()

	fmt.Printf("  Resolving %v, %d times each, under three resolver settings.\n",
		names, lookupsPerName)
	fmt.Println("  GODEBUG is read at startup, so each row is a separate child process.")
	fmt.Println()

	runChild("default    -- whatever the runtime heuristic picks", "")
	runChild("netdns=go  -- the pure-Go resolver, reading /etc/resolv.conf", "netdns=go+2")
	runChild("netdns=cgo -- the system getaddrinfo, via cgo", "netdns=cgo+2")

	fmt.Println("  Three things to take away:")
	fmt.Println()
	fmt.Println("    1. Go caches nothing. Every repeated lookup above cost roughly the")
	fmt.Println("       same as the first, because your process honours no TTL. Any")
	fmt.Println("       speed-up between the first and the rest came from a resolver")
	fmt.Println("       BETWEEN you and the authority.")
	fmt.Println()
	fmt.Println("    2. Which resolver you get is not in your code and not in your")
	fmt.Println("       Dockerfile. Change from a glibc base image to Alpine, or add an")
	fmt.Println("       nsswitch feature the pure-Go resolver does not implement, and the")
	fmt.Println("       same binary starts resolving differently. That is a five-minute")
	fmt.Println("       diagnosis if you know GODEBUG=netdns=go+2 exists, and a two-day")
	fmt.Println("       one if you do not.")
	fmt.Println()
	fmt.Println("    3. None of this is what kept your service talking to a dead address.")
	fmt.Println("       That was the connection pool. Resolver choice decides how you")
	fmt.Println("       LOOK a name up; only connection lifetime decides how often you")
	fmt.Println("       bother to ask.")
}
