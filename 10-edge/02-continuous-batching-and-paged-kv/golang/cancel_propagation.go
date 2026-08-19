// Layer 10 - Topic 2: does hanging up actually free the KV blocks? (Go)
//
// What this demonstrates
//
//	The same experiment as the Python and Node versions, in the runtime
//	that gets this right by construction. A stub model server streams 40
//	tokens at 100ms each while watching for its caller to leave; a
//	gateway sits in front with two handlers; a client hangs up after
//	500ms against each.
//
//	  /naive       builds the upstream request with context.Background().
//	               That is the entire bug -- one identifier. The request
//	               is now rooted in a context nothing will ever cancel,
//	               so the upstream generation runs to completion for a
//	               response that gets discarded on the next line.
//	  /cancelling  builds it with r.Context(). net/http already cancels
//	               that context when the client disconnects, and
//	               http.Client already aborts an in-flight request when
//	               its context is cancelled. No listener, no controller,
//	               no polling: the plumbing exists and you opt into it by
//	               passing the value you were already given.
//
// What to look for
//   - The diff between the two handlers is `context.Background()` versus
//     `r.Context()`. Nothing else. This is why gateways in front of
//     expensive backends are so often Go, and it is a better reason than
//     "Go is fast".
//   - /naive: upstream decodes all 40 tokens, ~3.5s of it after the
//     client stopped listening.
//   - /cancelling: upstream sees EOF within a token or two.
//
// No dependencies, binds 127.0.0.1 only, runs with no arguments:
//
//	cd golang && go run cancel_propagation.go
package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync"
	"time"
)

const (
	tokens               = 40
	tokenInterval        = 100 * time.Millisecond // 4.0s of "decode"
	clientHangsUpAfter   = 500 * time.Millisecond
	upstreamGraceTimeout = 6 * time.Second
)

type observation struct {
	aborted bool
	tokens  int
	elapsed time.Duration
}

type ledger struct {
	mu      sync.Mutex
	entries []observation
}

func (l *ledger) add(o observation) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = append(l.entries, o)
}

func (l *ledger) takeFirst() (observation, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.entries) == 0 {
		return observation{}, false
	}
	return l.entries[0], true
}

func (l *ledger) reset() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = nil
}

var seen = &ledger{}

// upstreamHandler is the stub model server. r.Context() is cancelled by
// net/http as soon as the caller's connection closes, which is precisely
// the signal a real engine uses to free a sequence's KV blocks.
func upstreamHandler(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "no flusher", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	sent := 0
	for i := 0; i < tokens; i++ {
		select {
		case <-r.Context().Done():
			seen.add(observation{aborted: true, tokens: sent, elapsed: time.Since(start)})
			return
		case <-time.After(tokenInterval):
		}
		fmt.Fprintf(w, "data: token %d\n\n", i)
		flusher.Flush()
		sent = i + 1
	}
	seen.add(observation{aborted: false, tokens: sent, elapsed: time.Since(start)})
}

// proxy forwards to the upstream and returns the whole response. Both
// handlers call it with identical arguments except the context, so the
// context is the only variable in the experiment.
//
// Buffering with io.ReadAll is the realistic shape for a non-streaming
// completions call, and it removes a second Go safety net that would
// otherwise muddy the result: if the gateway streamed instead, the first
// w.Write to a departed client returns an error, the handler returns, the
// deferred resp.Body.Close() runs, and the upstream is torn down that way
// even with context.Background(). Go hands you two independent chances to
// get this right. This experiment removes one of them so you can see what
// the remaining one is worth.
func proxy(ctx context.Context, w http.ResponseWriter, upstreamURL string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, upstreamURL, nil)
	if err != nil {
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return // context cancelled: the upstream request was aborted for us
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	w.Write(body)
}

// hangUpOn dials the gateway with a raw socket, sends a request, and closes
// the connection mid-response. A raw socket on purpose: a client library's
// timeout raises in your code without necessarily closing the TCP
// connection, so the server would see nothing and the experiment would
// measure something else entirely.
func hangUpOn(addr, path string) {
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		return
	}
	fmt.Fprintf(conn, "POST %s HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n", path)
	buf := make([]byte, 4096)
	conn.SetReadDeadline(time.Now().Add(clientHangsUpAfter))
	for {
		if _, err := conn.Read(buf); err != nil {
			break
		}
	}
	conn.Close()
}

func main() {
	upstreamSrv := &http.Server{Handler: http.HandlerFunc(upstreamHandler)}
	upstreamLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	go upstreamSrv.Serve(upstreamLn)
	upstreamURL := "http://" + upstreamLn.Addr().String() + "/completions"

	mux := http.NewServeMux()
	// The bug: a context with no relationship to this request. Every field
	// on r is still available; this handler just does not use the one that
	// matters.
	mux.HandleFunc("/naive", func(w http.ResponseWriter, r *http.Request) {
		proxy(context.Background(), w, upstreamURL)
	})
	// The fix: the request's own context, which net/http cancelled the
	// moment the client went away.
	mux.HandleFunc("/cancelling", func(w http.ResponseWriter, r *http.Request) {
		proxy(r.Context(), w, upstreamURL)
	})

	gatewayLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	gatewaySrv := &http.Server{Handler: mux}
	go gatewaySrv.Serve(gatewayLn)

	fmt.Println("Go / net/http - cancellation on client disconnect")
	fmt.Printf("  upstream streams %d tokens x %v = %v of decode\n",
		tokens, tokenInterval, time.Duration(tokens)*tokenInterval)
	fmt.Printf("  client hangs up after %v\n\n", clientHangsUpAfter)
	fmt.Printf("  %-14s %-16s %14s %13s %8s\n",
		"handler", "upstream saw", "tokens decoded", "upstream ran", "wasted")
	fmt.Println("  " + str(70, '-'))

	for _, path := range []string{"/naive", "/cancelling"} {
		seen.reset()
		hangUpOn(gatewayLn.Addr().String(), path)

		deadline := time.Now().Add(upstreamGraceTimeout)
		var obs observation
		for time.Now().Before(deadline) {
			if o, ok := seen.takeFirst(); ok {
				obs = o
				break
			}
			time.Sleep(50 * time.Millisecond)
		}

		wasted := obs.elapsed - clientHangsUpAfter
		if wasted < 0 {
			wasted = 0
		}
		state := "nothing"
		if obs.aborted {
			state = "cancelled"
		}
		fmt.Printf("  %-14s %-16s %14d %12.2fs %7.2fs\n",
			path, state, obs.tokens, obs.elapsed.Seconds(), wasted.Seconds())
	}

	fmt.Println()
	fmt.Println("  'wasted' is decode time spent on a response nobody read. On a")
	fmt.Println("  loaded server those KV blocks stayed allocated the whole time,")
	fmt.Println("  so the scheduler could not admit somebody who was still waiting.")
	fmt.Println()
	fmt.Println("  The whole difference between the two rows is which context was")
	fmt.Println("  passed to http.NewRequestWithContext.")

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	gatewaySrv.Shutdown(ctx)
	upstreamSrv.Shutdown(ctx)
}

func str(n int, c byte) string {
	b := make([]byte, n)
	for i := range b {
		b[i] = c
	}
	return string(b)
}
