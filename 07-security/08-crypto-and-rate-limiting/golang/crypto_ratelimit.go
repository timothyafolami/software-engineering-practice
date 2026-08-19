// Layer 7 · Topic 8 — Crypto hygiene and rate limiting (Go).
//
// `GOFLAGS=-mod=mod GOPROXY=off go run crypto_ratelimit.go` (x/crypto is
// cached). Go's stdlib acknowledges constant-time as a CATEGORY: crypto/subtle
// has ConstantTimeCompare, ConstantTimeSelect, ConstantTimeByteEq. Hashing is
// golang.org/x/crypto/argon2; rate limiting would be golang.org/x/time/rate
// (in-process, so the distributed-state problem is yours -- which Part C shows).
//
// Three parts, measured at runtime: (A) sha256 vs argon2id verify/sec + the
// crack-time model; (B) the timing signal, naive short-circuit vs
// subtle.ConstantTimeCompare; (C) rate-limiting effective limit and fake fixes.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"fmt"
	"time"

	"golang.org/x/crypto/argon2"
)

func partA() {
	fmt.Println("A. Hash cost (verifications/sec, measured)")
	pw := []byte("correct horse battery staple")

	reps := 500000
	t0 := time.Now()
	for i := 0; i < reps; i++ {
		sha256.Sum256(pw)
	}
	shaVps := float64(reps) / time.Since(t0).Seconds()
	fmt.Printf("   sha256          %14.0f verify/sec\n", shaVps)

	// argon2id at the OWASP baseline: m=19456 KiB, t=2, p=1.
	salt := make([]byte, 16)
	rand.Read(salt)
	reps = 20
	t0 = time.Now()
	for i := 0; i < reps; i++ {
		argon2.IDKey(pw, salt, 2, 19456, 1, 32)
	}
	argVps := float64(reps) / time.Since(t0).Seconds()
	fmt.Printf("   argon2id(19MiB) %14.1f verify/sec\n", argVps)

	N, K := 10000.0, 1000000.0
	fmt.Printf("   crack-time model: attacker rig N=%.0fx, list K=%.0f candidates\n", N, K)
	fmt.Printf("      sha256:   %.6f s to first crack\n", K/(shaVps*N))
	fmt.Printf("      argon2id: %.1f s to first crack  -- ~%.0fx slower per verify than sha256\n",
		K/(argVps*N), shaVps/argVps)
	fmt.Println()
}

var sink int // prevents dead-code elimination of timed compares

func naiveEq(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] { // short-circuit -> timing depends on the secret
			return false
		}
	}
	return true
}

func partB() {
	fmt.Println("B. Timing signal: naive short-circuit vs constant-time")
	secret := make([]byte, 32)
	rand.Read(secret)

	candidate := func(matching int) []byte {
		c := make([]byte, 32)
		rand.Read(c)
		copy(c[:matching], secret[:matching])
		if matching < 32 {
			c[matching] = secret[matching] ^ 0xFF
		}
		return c
	}
	// A single 32-byte compare is faster than the ns timer, so average ns/op
	// over a large batch; `sink` stops the compiler eliminating the call.
	avgNs := func(fn func(a, b []byte) int, cand []byte, reps int) float64 {
		t0 := time.Now()
		for i := 0; i < reps; i++ {
			sink += fn(secret, cand)
		}
		return float64(time.Since(t0).Nanoseconds()) / float64(reps)
	}
	naiveWrap := func(a, b []byte) int {
		if naiveEq(a, b) {
			return 1
		}
		return 0
	}

	fmt.Println("   matching leading bytes ->        avg ns/op")
	for _, v := range []struct {
		label string
		fn    func(a, b []byte) int
	}{{"naive_eq", naiveWrap}, {"ConstantTimeCompare", subtle.ConstantTimeCompare}} {
		out := fmt.Sprintf("   %-20s", v.label)
		for _, k := range []int{0, 8, 16, 31} {
			out += fmt.Sprintf(" k=%d:%.2f", k, avgNs(v.fn, candidate(k), 3000000))
		}
		fmt.Println(out)
	}
	_ = sink
	fmt.Println("   (naive trends up with k; ConstantTimeCompare flat. subtle also has")
	fmt.Println("    ConstantTimeSelect/ByteEq -- constant-time is a category, not one func.)")
	fmt.Println()
}

func partC() {
	fmt.Println("C. Rate limiting: attempts-to-first-success and effective limit")
	const listN, correctAt, configured = 1000, 500, 10

	run := func(mode string, workers, sourceIPs int) (int, bool) {
		if workers == 0 {
			workers = 1
		}
		if sourceIPs == 0 {
			sourceIPs = 1
		}
		allowed := 0
		reached := false
		buckets := map[interface{}]int{}
		for i := 1; i <= listN; i++ {
			ip := i % sourceIPs
			permitted := false
			switch mode {
			case "off":
				permitted = true
			case "redis_token_bucket":
				if _, ok := buckets["account"]; !ok {
					buckets["account"] = configured
				}
				if buckets["account"] > 0 {
					buckets["account"]--
					permitted = true
				}
			case "inproc":
				key := [2]int{0, i % workers}
				if _, ok := buckets[key]; !ok {
					buckets[key] = configured
				}
				if buckets[key] > 0 {
					buckets[key]--
					permitted = true
				}
			case "ip_keyed":
				if _, ok := buckets[ip]; !ok {
					buckets[ip] = configured
				}
				if buckets[ip] > 0 {
					buckets[ip]--
					permitted = true
				}
			}
			if permitted {
				allowed++
				if i == correctAt {
					reached = true
				}
			}
		}
		return allowed, reached
	}

	type row struct {
		mode              string
		workers, ips      int
		note              string
	}
	for _, r := range []row{
		{"off", 1, 1, "no limit"},
		{"redis_token_bucket", 1, 1, "shared bucket, configured=10"},
		{"inproc", 1, 1, "in-proc, 1 worker"},
		{"inproc", 4, 1, "in-proc, 4 workers -> effective 4x"},
		{"ip_keyed", 1, 50, "IP-keyed, attacker uses 50 IPs"},
	} {
		allowed, reached := run(r.mode, r.workers, r.ips)
		msg := "password NOT reached"
		if reached {
			msg = "reached password"
		}
		fmt.Printf("   %-18s %-34s allowed=%-4d %s\n", r.mode, r.note, allowed, msg)
	}
	fmt.Printf("\n   effective/configured: inproc workers=4 allows ~%d vs configured %d -> 4x.\n",
		4*configured, configured)
	fmt.Println("   IP-keyed with 50 IPs lets the password through -> keying on IP is a fake fix.")
	fmt.Println()
}

func main() {
	fmt.Println("Layer 7 · Topic 8 — hash cost, timing signal, rate limiting\n")
	partA()
	partB()
	partC()
	fmt.Println("Takeaway: password hash must be SLOW (argon2id), a secret compare " +
		"CONSTANT-TIME, and a rate limit keyed on the account with SHARED state.")
}
