// Layer 2 · Topic 1 - Go pools by default, and then throws the pool away.
//
// Go is here because its default is the subtle one. http.DefaultTransport
// does keep connections alive -- but MaxIdleConnsPerHost defaults to 2
// (DefaultMaxIdleConnsPerHost), while MaxIdleConns is 100 across all hosts.
// So a service that fans out N concurrent requests to ONE host keeps two
// connections and closes the other N-2 the moment they go idle. The next
// burst re-handshakes all of them.
//
// This is invisible in a benchmark that does one request at a time (you
// never exceed 2 idle) and invisible in latency on a datacenter link
// (handshakes are cheap at 0.3 ms RTT). It shows up as a SYN rate and a
// pile of TIME_WAIT sockets on the CLIENT -- Topic 7's diagnostic.
//
// The server counts connections via httptest's ConnState hook, which fires
// StateNew once per accepted TCP connection.
//
// What to look for in the output: connections opened per burst, default
// transport vs tuned. Requests per connection near 1.0 means you are
// paying for a handshake on nearly every request while believing you pool.
//
// Run: go run max_idle_conns_per_host.go
package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

const (
	fanOut = 50 // concurrent requests to one host, i.e. a /fanout handler
	bursts = 10 // repeated, because the pool is only observable across bursts
)

func main() {
	var connectionsOpened int64

	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"ok":true}`)
	}))
	server.Config.ConnState = func(c net.Conn, state http.ConnState) {
		if state == http.StateNew {
			atomic.AddInt64(&connectionsOpened, 1)
		}
	}
	server.Start()
	defer server.Close()

	fmt.Println("==============================================================================")
	fmt.Println("Go: http.DefaultTransport keeps 2 idle connections per host")
	fmt.Println("==============================================================================")
	fmt.Printf("  server %s   %d bursts of %d concurrent requests\n\n", server.URL, bursts, fanOut)

	defaultTransport := http.DefaultTransport.(*http.Transport)
	fmt.Printf("  http.DefaultTransport defaults on this build:\n")
	fmt.Printf("    MaxIdleConns          %d   (across ALL hosts)\n", defaultTransport.MaxIdleConns)
	fmt.Printf("    MaxIdleConnsPerHost   %d   (this is the one that bites)\n", http.DefaultMaxIdleConnsPerHost)
	fmt.Printf("    MaxConnsPerHost       %d   (0 means unlimited -- Topic 2)\n", defaultTransport.MaxConnsPerHost)
	fmt.Printf("    IdleConnTimeout       %s\n\n", defaultTransport.IdleConnTimeout)

	run("DEFAULT   - http.DefaultTransport as shipped",
		&http.Client{Transport: defaultTransport.Clone()},
		server.URL, &connectionsOpened)

	fmt.Println()
	tuned := defaultTransport.Clone()
	tuned.MaxIdleConns = 200
	tuned.MaxIdleConnsPerHost = fanOut // sized to the actual fan-out
	run("TUNED     - MaxIdleConnsPerHost = fan-out",
		&http.Client{Transport: tuned},
		server.URL, &connectionsOpened)

	fmt.Println()
	// The Go equivalent of building httpx.AsyncClient() inside a handler:
	// a fresh Transport per request. Every Transport carries its own pool,
	// so per-request transports mean per-request handshakes forever.
	run("PER-REQUEST TRANSPORT - a new http.Transport each time (the bug)",
		nil, server.URL, &connectionsOpened)

	fmt.Println("\n  Why MaxIdleConnsPerHost=2 is defensible and still wrong for you:")
	fmt.Println("    Go's default assumes a client talking to many hosts (a crawler, a")
	fmt.Println("    proxy), where holding 100 idle sockets per host would exhaust fds.")
	fmt.Println("    A service that calls ONE upstream 50 times concurrently is the")
	fmt.Println("    opposite shape. Set MaxIdleConnsPerHost to your real fan-out.")
	fmt.Println("    The closed connections do not vanish either -- the side that")
	fmt.Println("    closes holds TIME_WAIT for ~60 s, and here that side is you.")
}

func run(label string, client *http.Client, url string, counter *int64) {
	before := atomic.LoadInt64(counter)
	var mu sync.Mutex
	latencies := make([]float64, 0, bursts*fanOut)

	for burst := 0; burst < bursts; burst++ {
		var wg sync.WaitGroup
		for i := 0; i < fanOut; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				c := client
				if c == nil {
					// A brand new Transport, and therefore a brand new pool,
					// discarded at the end of this request.
					c = &http.Client{Transport: &http.Transport{}}
					defer c.CloseIdleConnections()
				}
				started := time.Now()
				resp, err := c.Get(url)
				if err != nil {
					fmt.Printf("    request error: %v\n", err)
					return
				}
				io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				mu.Lock()
				latencies = append(latencies, float64(time.Since(started).Microseconds())/1000)
				mu.Unlock()
			}()
		}
		wg.Wait()
		// A real service has gaps. Without one, connections never go idle and
		// the idle-pool limit is never consulted -- which is exactly why this
		// bug hides in a saturating benchmark.
		time.Sleep(20 * time.Millisecond)
	}

	opened := atomic.LoadInt64(counter) - before
	sort.Float64s(latencies)
	at := func(f float64) float64 {
		if len(latencies) == 0 {
			return 0
		}
		i := int(float64(len(latencies)) * f)
		if i >= len(latencies) {
			i = len(latencies) - 1
		}
		return latencies[i]
	}
	fmt.Printf("  %s\n", label)
	fmt.Printf("    requests issued        %d\n", len(latencies))
	fmt.Printf("    TCP connections opened %d\n", opened)
	fmt.Printf("    requests per connection %.1f\n", float64(len(latencies))/float64(max64(opened, 1)))
	fmt.Printf("    latency p50 %.3f ms   p95 %.3f ms   p99 %.3f ms\n", at(0.50), at(0.95), at(0.99))
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
