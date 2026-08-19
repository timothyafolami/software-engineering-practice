// Layer 4 · Topic 1 — the third outcome, in Go.
//
// WHAT THIS DEMONSTRATES
//
//	The same five faults as the Python and Node programs, against an in-process
//	ledger that records server-side truth, so the client's belief can be diffed
//	against what actually happened.
//
//	Go's contribution is net/http/httptrace. Every other client in this lab has
//	to infer which phase a failure happened in; Go can be told. ConnectDone and
//	WroteRequest fire at exactly the boundary that decides whether a retry is
//	safe, which makes the classification a fact rather than a guess.
//
// WHAT TO LOOK FOR
//  1. Phase 1's duplicate charges: created by the client's own retries, not by
//     the faults.
//  2. Phase 2's duplicates (gone) next to its unresolved ambiguity (unchanged).
//  3. The note at the end about context cancellation. Cancelling the client's
//     context tears down *your* side. The handler that already read the request
//     commits anyway, which is why the ledger keeps growing while the caller
//     believes everything failed.
//
// Run:  go run ambiguous_result.go
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	clientTimeout   = 300 * time.Millisecond // the deadline the caller will wait
	slowResponse    = 1000 * time.Millisecond
	requestsPerMode = 4
	maxAttempts     = 3
)

var modes = []string{"ok", "slow", "hang", "reset", "crash_after_commit", "refused"}

// --- server-side truth ------------------------------------------------------

type ledger struct {
	mu   sync.Mutex
	rows []string
}

func (l *ledger) commit(chargeID string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.rows = append(l.rows, chargeID)
}

func (l *ledger) len() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.rows)
}

func (l *ledger) since(n int) []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.rows[n:]...)
}

// ledgerServer speaks just enough HTTP/1.1 to be a fault injector. Raw sockets
// rather than net/http because two of the faults -- an RST, and closing with no
// response at all -- are below what a Handler can express.
type ledgerServer struct {
	listener net.Listener
	led      *ledger
	mu       sync.Mutex
	held     []net.Conn // `hang` connections, closed at shutdown
}

func newLedgerServer(led *ledger) (*ledgerServer, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	s := &ledgerServer{listener: ln, led: led}
	go s.acceptLoop()
	return s, nil
}

func (s *ledgerServer) port() int { return s.listener.Addr().(*net.TCPAddr).Port }

func (s *ledgerServer) acceptLoop() {
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			return
		}
		go s.serve(conn)
	}
}

func (s *ledgerServer) serve(conn net.Conn) {
	buf := make([]byte, 4096)
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := conn.Read(buf)
	if err != nil || n == 0 {
		conn.Close()
		return
	}
	line := strings.SplitN(string(buf[:n]), "\r\n", 2)[0]
	fields := strings.Fields(line)
	if len(fields) < 2 {
		conn.Close()
		return
	}
	parts := strings.SplitN(fields[1], "/", 4) // ["", "charge", mode, chargeID]
	if len(parts) < 4 {
		conn.Close()
		return
	}
	mode, chargeID := parts[2], parts[3]

	switch mode {
	case "ok":
		s.led.commit(chargeID)
		reply(conn, chargeID)
		conn.Close()
	case "slow":
		s.led.commit(chargeID)
		time.Sleep(slowResponse)
		reply(conn, chargeID)
		conn.Close()
	case "hang":
		s.led.commit(chargeID)
		s.mu.Lock()
		s.held = append(s.held, conn) // accepted, committed, never answered
		s.mu.Unlock()
	case "reset":
		s.led.commit(chargeID)
		// SetLinger(0) makes Close send RST instead of FIN: what a peer that
		// panics, or a middlebox that gives up, looks like from the client.
		if tcp, ok := conn.(*net.TCPConn); ok {
			_ = tcp.SetLinger(0)
		}
		conn.Close()
	case "crash_after_commit":
		// The case no timeout tuning can fix: durable work, dead reporter.
		s.led.commit(chargeID)
		conn.Close()
	default:
		conn.Close()
	}
}

func reply(conn net.Conn, chargeID string) {
	body := fmt.Sprintf(`{"charge_id":%q}`, chargeID)
	_, _ = io.WriteString(conn, fmt.Sprintf(
		"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"+
			"Content-Length: %d\r\nConnection: close\r\n\r\n%s", len(body), body))
}

func (s *ledgerServer) shutdown() {
	s.mu.Lock()
	for _, c := range s.held {
		c.Close()
	}
	s.mu.Unlock()
	s.listener.Close()
}

// --- classification ---------------------------------------------------------

type verdict struct {
	kind  string // SUCCESS, SAFE, AMBIGUOUS
	label string
}

// doRequest issues one request and reports which phase it died in.
//
// The decision is not "is this a timeout" -- it is "were the request bytes
// written". httptrace answers that exactly: WroteRequest fires once the
// transport has finished writing the request. Before it, a retry is provably
// safe. After it, the server may hold a committed charge you will never hear
// about.
func doRequest(client *http.Client, url string) verdict {
	var (
		mu           sync.Mutex
		wroteRequest bool
		connected    bool
	)
	trace := &httptrace.ClientTrace{
		ConnectDone: func(_, _ string, err error) {
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				connected = true
			}
		},
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			mu.Lock()
			defer mu.Unlock()
			if info.Err == nil {
				wroteRequest = true
			}
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), clientTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(
		httptrace.WithClientTrace(ctx, trace), http.MethodGet, url, nil)
	if err != nil {
		return verdict{"AMBIGUOUS", "malformed request"}
	}

	resp, err := client.Do(req)
	if err == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		return verdict{"SUCCESS", fmt.Sprintf("%d", resp.StatusCode)}
	}

	mu.Lock()
	sent, dialed := wroteRequest, connected
	mu.Unlock()

	label := describe(err)
	if !sent {
		// Nothing (or not all) of the request reached the socket. A retry
		// cannot duplicate work.
		if !dialed {
			return verdict{"SAFE", label + " [pre-connect]"}
		}
		return verdict{"SAFE", label + " [pre-write]"}
	}
	return verdict{"AMBIGUOUS", label + " [post-write]"}
}

func describe(err error) string {
	switch {
	case errors.Is(err, syscall.ECONNREFUSED):
		return "ECONNREFUSED"
	case errors.Is(err, syscall.ECONNRESET):
		return "ECONNRESET"
	case errors.Is(err, context.DeadlineExceeded):
		return "DeadlineExceeded"
	case errors.Is(err, io.EOF) || strings.Contains(err.Error(), "EOF"):
		return "EOF"
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return "net.OpError(" + opErr.Op + ")"
	}
	return "unclassified"
}

// --- phases -----------------------------------------------------------------

type phaseResult struct{ duplicates, unresolved, rows int }

func runPhase(tag, name, note string, port, closedPort int, led *ledger, retryAmbiguous bool) phaseResult {
	before := led.len()
	// A transport that reuses nothing: each attempt gets a fresh connection, so
	// a fault on one cannot be mistaken for a fault on another.
	client := &http.Client{
		Transport: &http.Transport{
			DialContext:       (&net.Dialer{Timeout: clientTimeout}).DialContext,
			DisableKeepAlives: true,
		},
	}

	fmt.Println()
	fmt.Printf("  %s\n  %s\n", name, note)
	fmt.Printf("  %-20s %-44s %9s %12s\n", "fault", "client verdict", "attempts", "ledger rows")

	unresolved := 0
	for _, mode := range modes {
		modeBefore := led.len()
		attempts := 0
		counts := map[string]int{}
		targetPort := port
		if mode == "refused" {
			targetPort = closedPort
		}

		for i := 0; i < requestsPerMode; i++ {
			chargeID := fmt.Sprintf("%s-%s-%d", tag, mode, i)
			url := fmt.Sprintf("http://127.0.0.1:%d/charge/%s/%s", targetPort, mode, chargeID)
			var v verdict
			for a := 0; a < maxAttempts; a++ {
				attempts++
				v = doRequest(client, url)
				if v.kind == "SUCCESS" {
					break
				}
				if v.kind == "SAFE" {
					continue // provably safe: try again
				}
				if retryAmbiguous {
					continue // the bug, made explicit
				}
				break // correct: stop, escalate
			}
			counts[v.kind+"("+v.label+")"]++
			if v.kind == "AMBIGUOUS" {
				unresolved++
			}
		}

		keys := make([]string, 0, len(counts))
		for k := range counts {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%dx %s", counts[k], k))
		}
		fmt.Printf("  %-20s %-44s %9d %12d\n",
			mode, strings.Join(parts, ", "), attempts, led.len()-modeBefore)
	}

	written := led.since(before)
	seen := map[string]int{}
	for _, id := range written {
		seen[id]++
	}
	duplicates := 0
	for _, n := range seen {
		if n > 1 {
			duplicates += n - 1
		}
	}
	fmt.Printf("  ledger rows written this phase : %d\n", len(written))
	fmt.Printf("  DUPLICATE CHARGES              : %d   <- created by this client's retries\n", duplicates)
	fmt.Printf("  unresolved ambiguous outcomes  : %d   <- caller cannot tell whether these happened\n", unresolved)
	return phaseResult{duplicates, unresolved, len(written)}
}

func findClosedPort() int {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 1
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

func main() {
	led := &ledger{}
	server, err := newLedgerServer(led)
	if err != nil {
		panic(err)
	}
	defer server.shutdown()
	closedPort := findClosedPort()

	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Layer 4 · Topic 1 — partial failure and the ambiguous result (Go)")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Printf("  ledger        : 127.0.0.1:%d  (in-process, holds server-side truth)\n", server.port())
	fmt.Printf("  closed port   : 127.0.0.1:%d  (for the connect-refused case)\n", closedPort)
	fmt.Printf("  client timeout: %v   slow response: %v   max attempts: %d\n",
		clientTimeout, slowResponse, maxAttempts)
	fmt.Println("  phase detection: net/http/httptrace ConnectDone + WroteRequest")

	naive := runPhase("p1",
		"phase 1 — retry on any error",
		"`if err != nil { retry() }`, which is what most Go clients do",
		server.port(), closedPort, led, true)
	fixed := runPhase("p2",
		"phase 2 — retry only when httptrace proves the request was never written",
		"pre-connect and pre-write failures are retried; everything else escalates",
		server.port(), closedPort, led, false)

	fmt.Println()
	fmt.Println(strings.Repeat("-", 78))
	fmt.Printf("  duplicate charges    phase 1: %-6d phase 2: %d\n", naive.duplicates, fixed.duplicates)
	fmt.Printf("  unresolved ambiguity phase 1: %-6d phase 2: %d\n", naive.unresolved, fixed.unresolved)
	fmt.Println()
	fmt.Println("  Context cancellation is a local event. When the deadline fires, Go tears")
	fmt.Println("  down this side of the connection and returns. The handler on the other")
	fmt.Println("  side has already read the request and finishes its work regardless --")
	fmt.Println("  which is why the ledger row count above keeps rising for faults the")
	fmt.Println("  client recorded as failures. Making that retry safe is Topic 2.")
}
