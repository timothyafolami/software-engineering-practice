// Layer 2 · Topic 3 - Go: a deadline is a value in the call tree, and
// cancelling a parent cancels its children for free.
//
// Go is in this topic because it has the clearest existing model of deadline
// propagation, and because the zero value http.Client{} has NO timeout at all
// -- the same trap as Python's `requests`. Read this even if you write
// Python: context.Context is exactly what you are hand-rolling when you
// thread a deadline object through FastAPI services.
//
// Three questions, answered with real timings against a real server running
// in this process:
//
//	A. Does the budget shrink as you go deeper?  (three sequential hops)
//	B. When the deadline fires, what happens to the request that is already
//	   in flight at the server?
//	C. Is the connection reusable afterwards?
//
// B and C are the two columns of this topic's second table, and they are the
// two that people guess wrong. The server here counts requests it STARTED and
// requests it FINISHED, and counts accepted TCP connections separately, so
// both answers are measured rather than asserted.
//
// What to look for in the output:
//   - phase A: each hop's slice is smaller than the last, and the chain
//     refuses to start hop 3 rather than starting a call whose answer would
//     arrive too late to use
//   - phase B: server FINISHED count goes up even for the request the client
//     abandoned. Cancellation is a message to your own code, not to the
//     server
//   - phase C: connections accepted increases across the timed-out request.
//     A response that was never read to EOF cannot be returned to the pool
//
// Run: go run context_deadline_chain.go
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

const (
	slowDelay      = 400 * time.Millisecond // how long the server holds a /slow request
	outerBudget    = 900 * time.Millisecond // what we promised our caller
	ownWorkReserve = 100 * time.Millisecond // time kept back to write our own response
	perHopCap      = 500 * time.Millisecond // the library default we would otherwise use
)

type counters struct {
	accepted atomic.Int64
	started  atomic.Int64
	finished atomic.Int64
}

func startServer(c *counters) (string, *http.Server) {
	mux := http.NewServeMux()
	mux.HandleFunc("/slow", func(w http.ResponseWriter, r *http.Request) {
		c.started.Add(1)
		// Note what this handler does NOT do: check r.Context().Done(). A
		// server that ignores client cancellation keeps working on requests
		// nobody is waiting for, which is how a retry storm converts into
		// sustained load. Most handlers in the wild look exactly like this.
		time.Sleep(slowDelay)
		c.finished.Add(1)
		io.WriteString(w, "slow ok")
	})
	mux.HandleFunc("/fast", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "fast ok")
	})

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	srv := &http.Server{
		Handler: mux,
		ConnState: func(_ net.Conn, s http.ConnState) {
			if s == http.StateNew {
				c.accepted.Add(1)
			}
		},
	}
	go srv.Serve(ln)
	return "http://" + ln.Addr().String(), srv
}

// deadline is the whole pattern in ten lines: an absolute instant, a reserve
// you never spend on upstreams, and a per-call cap. Go gives you this in the
// standard library as context.WithDeadline; it is written out here so the
// arithmetic is visible.
type deadline struct {
	at      time.Time
	reserve time.Duration
}

func (d deadline) remaining() time.Duration { return time.Until(d.at) }

func (d deadline) forCall(cap time.Duration) time.Duration {
	if r := d.remaining() - d.reserve; r < cap {
		return r
	}
	return cap
}

func call(ctx context.Context, client *http.Client, url string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", err
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	return string(b), err
}

func phaseA(client *http.Client, base string) {
	fmt.Println("A. A budget, spent down three sequential hops")
	fmt.Printf("    promised to our caller     %5.0f ms\n", outerBudget.Seconds()*1000)
	fmt.Printf("    reserved for our own work  %5.0f ms\n", ownWorkReserve.Seconds()*1000)
	fmt.Printf("    each hop's library default %5.0f ms  <- what a flat config would use\n\n", perHopCap.Seconds()*1000)

	d := deadline{at: time.Now().Add(outerBudget), reserve: ownWorkReserve}
	start := time.Now()

	for hop := 1; hop <= 3; hop++ {
		slice := d.forCall(perHopCap)
		if slice <= 0 {
			fmt.Printf("    hop %d  slice %6.0f ms  -> NOT STARTED: the answer would arrive after\n",
				hop, slice.Seconds()*1000)
			fmt.Println("                              our caller has already given up. Failing now is")
			fmt.Println("                              the correct behaviour and the line people skip.")
			break
		}
		ctx, cancel := context.WithTimeout(context.Background(), slice)
		_, err := call(ctx, client, base+"/slow")
		cancel()
		outcome := "ok"
		if err != nil {
			outcome = "deadline exceeded"
		}
		fmt.Printf("    hop %d  slice %6.0f ms  -> %s (%.0f ms elapsed, %.0f ms left)\n",
			hop, slice.Seconds()*1000, outcome,
			time.Since(start).Seconds()*1000, d.remaining().Seconds()*1000)
	}

	fmt.Printf("\n    total spent %.0f ms against a %.0f ms promise, with %.0f ms left to answer\n",
		time.Since(start).Seconds()*1000, outerBudget.Seconds()*1000, d.remaining().Seconds()*1000)
	fmt.Println("    A flat 500 ms per hop would have spent 1200 ms on three hops and")
	fmt.Println("    handed the caller an answer 300 ms after it stopped caring.")
}

func phaseB(client *http.Client, base string, c *counters) {
	fmt.Println("\nB. What a fired deadline does to the request already in flight")

	// Let phase A's abandoned hop finish first, so the delta below belongs to
	// this request and no other. It will finish -- that is phase A's point
	// too, arriving early.
	time.Sleep(slowDelay + 100*time.Millisecond)
	before := c.finished.Load()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	t0 := time.Now()
	_, err := call(ctx, client, base+"/slow")
	elapsed := time.Since(t0)

	fmt.Printf("    client gave up after   %.0f ms\n", elapsed.Seconds()*1000)
	fmt.Printf("    error                  %v\n", err)
	fmt.Printf("    errors.Is(err, context.DeadlineExceeded) = %v   <- the typed answer to 'why'\n",
		errors.Is(err, context.DeadlineExceeded))

	// Give the server time to finish the work the client walked away from.
	time.Sleep(slowDelay + 200*time.Millisecond)
	fmt.Printf("    server FINISHED this request anyway: %d -> %d\n", before, c.finished.Load())
	fmt.Println("    Cancellation is a message to your own goroutine. The server was never")
	fmt.Println("    told, kept the CPU, kept the row lock, kept the database connection.")
	fmt.Println("    Your timeout protects you; it does not protect the thing you called.")
}

func phaseC(client *http.Client, base string, c *counters) {
	fmt.Println("\nC. Is the connection reusable after the deadline fires?")

	if _, err := call(context.Background(), client, base+"/fast"); err != nil {
		fmt.Println("    warm-up failed:", err)
	}
	warm := c.accepted.Load()
	fmt.Printf("    connections accepted after a warm-up request        %d\n", warm)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	_, _ = call(ctx, client, base+"/slow")
	cancel()
	afterTimeout := c.accepted.Load()

	if _, err := call(context.Background(), client, base+"/fast"); err != nil {
		fmt.Println("    follow-up failed:", err)
	}
	after := c.accepted.Load()

	fmt.Printf("    ...after one timed-out request                      %d\n", afterTimeout)
	fmt.Printf("    ...after the next successful request                %d\n", after)
	if after > warm {
		fmt.Println("    The connection was NOT reused. A response body that was never read")
		fmt.Println("    to EOF leaves the transport unable to find the start of the next")
		fmt.Println("    response on that socket, so it closes it. Every timeout therefore")
		fmt.Println("    costs you a handshake as well -- which is why a slow dependency")
		fmt.Println("    plus an aggressive timeout can produce MORE load, not less.")
	} else {
		fmt.Println("    The connection was reused. Check that the timed-out request really")
		fmt.Println("    fired before concluding anything from this row.")
	}
}

func main() {
	c := &counters{}
	base, srv := startServer(c)
	defer srv.Close()

	// The zero value has no timeout at all. Named here so the contrast with
	// the context deadlines below is explicit: Go's default is `requests`'
	// default, and it is a promise to hang forever.
	client := &http.Client{}

	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Go: context deadlines down a call chain, and what firing one actually does")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Printf("  server holds /slow for %.0f ms   http.Client{} timeout: none (the zero value)\n\n",
		slowDelay.Seconds()*1000)

	phaseA(client, base)
	phaseB(client, base, c)
	phaseC(client, base, c)

	fmt.Println("\n  For this topic's table:")
	fmt.Println("    what a fired timeout does to the in-flight request:")
	fmt.Println("      cancels the client side only; the server runs to completion unless")
	fmt.Println("      its handler checks r.Context().Done() -- and yours does not.")
	fmt.Println("    connection reused after?")
	fmt.Println("      no; the transport closes a connection whose response it did not")
	fmt.Println("      finish reading.")
}
