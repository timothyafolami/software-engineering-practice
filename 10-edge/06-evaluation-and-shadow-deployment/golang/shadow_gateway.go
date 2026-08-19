// Layer 10 - Topic 6: shadowing that structurally cannot hurt the live path.
//
// What this demonstrates
//
//	Mirroring traffic to a candidate model is described as free. It is
//	free only if the shadow path shares nothing with the primary that
//	can run out. This program runs the same shadow load two ways against
//	the same gateway and measures what it does to the PRIMARY:
//
//	  shared pool     the shadow call reuses http.DefaultClient, so it
//	                  draws from the same connection pool and the same
//	                  per-host connection limit as the primary. When the
//	                  candidate is slow -- which candidates are, that is
//	                  why they are candidates -- shadow requests occupy
//	                  connections the primary needs.
//	  separate pool   the shadow call uses its own http.Client with its
//	                  own Transport, its own MaxConnsPerHost, and its
//	                  own context.WithTimeout. Nothing it can exhaust is
//	                  something the primary needs.
//
//	The separate pool is the load-bearing detail, and it is one struct
//	literal. Sharing the primary's pool is how "shadowing is free"
//	becomes an incident.
//
// What to look for
//   - primary p99 in the two modes. The shadow load is identical; the
//     only change is which Transport the shadow requests draw from.
//   - `shadow completed` versus `shadow timed out`. The shadow is
//     SUPPOSED to be allowed to fail: its results are logged for
//     offline scoring and its errors never reach the caller. A shadow
//     that can fail the request is not a shadow.
//   - The result is discarded on purpose. Reading it into anything the
//     primary response depends on -- even a shared logger with a bounded
//     queue -- reintroduces the coupling this design removed.
//
// No dependencies, binds 127.0.0.1 only. Runs with no arguments:
//
//	cd golang && go run shadow_gateway.go
package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const (
	requests         = 400
	arrivalInterval  = 5 * time.Millisecond
	primaryLatency   = 20 * time.Millisecond
	candidateLatency = 900 * time.Millisecond // the candidate is slow. They are.
	shadowTimeout    = 250 * time.Millisecond
	sharedConnLimit  = 16 // the per-host connection limit both paths would share
)

type counters struct {
	shadowOK      atomic.Int64
	shadowTimeout atomic.Int64
}

func stubServer(latency time.Duration) (*http.Server, net.Listener) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-time.After(latency):
			io.WriteString(w, "ok")
		case <-r.Context().Done():
		}
	})}
	go srv.Serve(ln)
	return srv, ln
}

func pct(values []time.Duration, q float64) time.Duration {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]time.Duration(nil), values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	i := int(q * float64(len(sorted)))
	if i >= len(sorted) {
		i = len(sorted) - 1
	}
	return sorted[i]
}

// run issues `requests` primary calls on an open-loop clock, mirroring each
// one to the candidate. `sharedPool` decides whether the shadow draws from
// the same Transport as the primary.
func run(primaryURL, candidateURL string, sharedPool bool) ([]time.Duration, *counters) {
	// The primary's client. Its connection limit is the resource in
	// question, so it is set explicitly rather than left at the default.
	primaryTransport := &http.Transport{
		MaxConnsPerHost:     sharedConnLimit,
		MaxIdleConnsPerHost: sharedConnLimit,
	}
	primary := &http.Client{Transport: primaryTransport}

	// The gateway's in-flight limit. This, not the Transport, is what
	// actually bites here: the primary and the candidate are different
	// hosts, so MaxConnsPerHost buckets them separately, while a gateway's
	// own concurrency limiter is one bucket for everything it does. Real
	// gateways have both, and it is usually the second one that is shared
	// without anybody deciding to share it.
	limiter := make(chan struct{}, sharedConnLimit)

	var shadow *http.Client
	shadowLimiter := limiter
	if sharedPool {
		// The bug: one client and one limiter for both paths. Reads as
		// tidy in review -- fewer objects, less configuration.
		shadow = primary
	} else {
		// The fix: its own Transport and its own, smaller, limiter. Nothing
		// the shadow can exhaust is something the primary needs.
		shadow = &http.Client{Transport: &http.Transport{
			MaxConnsPerHost:     4,
			MaxIdleConnsPerHost: 4,
		}}
		shadowLimiter = make(chan struct{}, 4)
	}

	c := &counters{}
	latencies := make([]time.Duration, 0, requests)
	var mu sync.Mutex
	var wg sync.WaitGroup

	for i := 0; i < requests; i++ {
		time.Sleep(arrivalInterval)
		wg.Add(1)
		go func() {
			defer wg.Done()

			// Mirror first, on its own goroutine, with its own deadline.
			// Never awaited by the primary path, and its result is dropped.
			go func() {
				shadowLimiter <- struct{}{}
				defer func() { <-shadowLimiter }()
				ctx, cancel := context.WithTimeout(context.Background(), shadowTimeout)
				defer cancel()
				req, _ := http.NewRequestWithContext(ctx, http.MethodGet, candidateURL, nil)
				resp, err := shadow.Do(req)
				if err != nil {
					c.shadowTimeout.Add(1)
					return
				}
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				c.shadowOK.Add(1)
			}()

			start := time.Now()
			limiter <- struct{}{}
			resp, err := primary.Get(primaryURL)
			<-limiter
			if err == nil {
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
			}
			mu.Lock()
			latencies = append(latencies, time.Since(start))
			mu.Unlock()
		}()
	}
	wg.Wait()
	return latencies, c
}

func main() {
	primarySrv, primaryLn := stubServer(primaryLatency)
	candidateSrv, candidateLn := stubServer(candidateLatency)
	primaryURL := "http://" + primaryLn.Addr().String() + "/v1/completions"
	candidateURL := "http://" + candidateLn.Addr().String() + "/v1/completions"

	fmt.Println("Shadow deployment - does the shadow path share anything that can run out?")
	fmt.Printf("  primary responds in    %v\n", primaryLatency)
	fmt.Printf("  candidate responds in  %v  (slower than the shadow timeout of %v)\n",
		candidateLatency, shadowTimeout)
	fmt.Printf("  %d requests at one every %v, each mirrored to the candidate\n",
		requests, arrivalInterval)
	fmt.Printf("  gateway in-flight limit: %d\n\n", sharedConnLimit)

	fmt.Printf("  %-28s %11s %11s %11s %12s %12s\n",
		"shadow isolation", "primary p50", "primary p95", "primary p99",
		"shadow ok", "shadow t/o")
	fmt.Println("  " + repeat('-', 92))

	for _, cfg := range []struct {
		label  string
		shared bool
	}{
		{"shared with primary", true},
		{"its own http.Transport", false},
	} {
		lat, c := run(primaryURL, candidateURL, cfg.shared)
		fmt.Printf("  %-28s %11v %11v %11v %12d %12d\n", cfg.label,
			pct(lat, 0.50).Round(time.Millisecond),
			pct(lat, 0.95).Round(time.Millisecond),
			pct(lat, 0.99).Round(time.Millisecond),
			c.shadowOK.Load(), c.shadowTimeout.Load())
	}

	fmt.Println()
	fmt.Println("  The shadow load is identical in both rows. The only difference is")
	fmt.Println("  whether it draws from the same in-flight budget the primary needs.")
	fmt.Println("  Every shadow request times out in both rows -- that is correct and")
	fmt.Println("  expected; a shadow is allowed to fail and its errors never reach")
	fmt.Println("  the caller. What must not happen is the primary paying for it.")
	fmt.Println()
	fmt.Println("  Note the shadow counts differ too: the isolated shadow has a")
	fmt.Println("  smaller budget, so it sheds some of its own load. That is a")
	fmt.Println("  sampling decision about shadow COVERAGE, and it belongs to the")
	fmt.Println("  shadow. Shedding primary traffic to keep shadow coverage up is")
	fmt.Println("  the trade the shared row is making without being asked.")
	fmt.Println()
	fmt.Println("  Three properties make this a shadow rather than an untested canary:")
	fmt.Println("    1. its own transport, so it cannot exhaust the primary's pool;")
	fmt.Println("    2. its own context.WithTimeout, so a hung candidate is bounded;")
	fmt.Println("    3. its result is discarded, so nothing the caller sees depends")
	fmt.Println("       on it -- log it for offline scoring, never read it inline.")

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	primarySrv.Shutdown(ctx)
	candidateSrv.Shutdown(ctx)
}

func repeat(c byte, n int) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}
