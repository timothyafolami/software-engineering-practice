// Layer 2 · Topic 4 - Go: no idle timeout at all, by default, and that is the
// SAFE answer to this bug and the dangerous answer to a different one.
//
// http.Server.IdleTimeout, when zero, falls back to ReadTimeout; if that is
// zero too, idle connections are never closed by the server. So a default Go
// server can never be the side that closes first, the load balancer always is,
// and the ordering rule this topic is about is satisfied -- by accident rather
// than by design.
//
// Sit with that for a moment, because it is the same trade Topic 2 found at
// the database pool. "No limit" is not the absence of a decision. It is a
// decision about WHICH component fails: here it protects you from 502s and
// exposes you to unbounded idle connections, file descriptors and memory held
// by peers that went away without saying so.
//
// Four servers, one identical client that holds a pooled connection and reuses
// it after an idle gap:
//
//	default          IdleTimeout 0, ReadTimeout 0   -> never closes. No 502s.
//	read-timeout     IdleTimeout 0, ReadTimeout 300ms -> the FALLBACK, which
//	                 people set for a completely unrelated reason and thereby
//	                 acquire this bug
//	short-idle       IdleTimeout 300ms              -> the race, deliberately
//	ordered          IdleTimeout 3s                 -> longer than the pool's
//	                 idle gap. Correct.
//
// What to look for in the output:
//   - the "server closed first" column, and which configurations have it
//   - read-timeout behaving exactly like short-idle, which is the surprise:
//     ReadTimeout is documented as the header+body read deadline, and it
//     doubles as your idle timeout when IdleTimeout is unset
//   - handshake counts: every failure costs a reconnect, so this bug shows up
//     in your connection rate before it shows up in your error rate
//
// Run: go run no_idle_timeout_by_default.go
package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

const (
	poolIdleGap = 1 * time.Second // stands in for the LB's 60 s idle timeout
	requests    = 4
)

type config struct {
	name        string
	idleTimeout time.Duration
	readTimeout time.Duration
	note        string
}

// pooledConn is the load balancer: one connection, held, reused. Raw TCP
// rather than http.Client on purpose -- a Go client would notice the peer's
// FIN and silently dial again, which hides exactly the event we are here to
// watch.
type pooledConn struct {
	addr       string
	conn       net.Conn
	reader     *bufio.Reader
	handshakes int
}

func (p *pooledConn) dial() error {
	c, err := net.Dial("tcp", p.addr)
	if err != nil {
		return err
	}
	p.conn, p.reader, p.handshakes = c, bufio.NewReader(c), p.handshakes+1
	return nil
}

func (p *pooledConn) do(path string) (bool, string) {
	if p.conn == nil {
		if err := p.dial(); err != nil {
			return false, err.Error()
		}
	}
	if _, err := io.WriteString(p.conn, "GET "+path+" HTTP/1.1\r\nHost: lab\r\n\r\n"); err != nil {
		p.discard()
		return false, errString(err)
	}
	p.conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	line, err := p.reader.ReadString('\n')
	if err != nil {
		p.discard()
		// io.EOF here is the whole topic: the peer closed while we were not
		// looking, and we found out by writing a request into the corpse.
		return false, errString(err)
	}
	// Drain headers AND body, so the connection is genuinely usable again.
	// Leaving a body in the buffer is Topic 1's "response not read to EOF"
	// bug, and it would corrupt every reading after this one.
	contentLength := 0
	for {
		l, err := p.reader.ReadString('\n')
		if err != nil {
			break
		}
		if strings.TrimSpace(l) == "" {
			break
		}
		if n, ok := parseContentLength(l); ok {
			contentLength = n
		}
	}
	if contentLength > 0 {
		io.CopyN(io.Discard, p.reader, int64(contentLength))
	}
	return true, strings.TrimSpace(line)
}

func (p *pooledConn) discard() {
	if p.conn != nil {
		p.conn.Close()
		p.conn = nil
	}
}

func parseContentLength(header string) (int, bool) {
	name, value, found := strings.Cut(header, ":")
	if !found || !strings.EqualFold(strings.TrimSpace(name), "Content-Length") {
		return 0, false
	}
	n := 0
	if _, err := fmt.Sscanf(strings.TrimSpace(value), "%d", &n); err != nil {
		return 0, false
	}
	return n, true
}

func errString(err error) string {
	if err == io.EOF {
		return "EOF (peer had already closed -- this is the 502)"
	}
	return err.Error()
}

func run(cfg config) (int, int) {
	var accepted, served int64

	mux := http.NewServeMux()
	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&served, 1)
		io.WriteString(w, "ok")
	})

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	srv := &http.Server{
		Handler:     mux,
		IdleTimeout: cfg.idleTimeout,
		ReadTimeout: cfg.readTimeout,
		ConnState: func(_ net.Conn, s http.ConnState) {
			if s == http.StateNew {
				atomic.AddInt64(&accepted, 1)
			}
		},
	}
	go srv.Serve(ln)
	defer srv.Close()

	pool := &pooledConn{addr: ln.Addr().String()}
	failures := 0
	var log []string
	for i := 0; i < requests; i++ {
		ok, detail := pool.do("/work")
		if !ok {
			failures++
		}
		mark := "ok "
		if !ok {
			mark = "502"
		}
		log = append(log, fmt.Sprintf("      request %d: %s  %s", i, mark, detail))
		time.Sleep(poolIdleGap) // the idle gap the bug needs
	}
	pool.discard()

	fmt.Printf("  %s\n", cfg.name)
	fmt.Printf("    IdleTimeout    %v\n", cfg.idleTimeout)
	fmt.Printf("    ReadTimeout    %v\n", cfg.readTimeout)
	fmt.Printf("    effective idle %s\n", effectiveIdle(cfg))
	fmt.Printf("    pool idle gap  %v\n", poolIdleGap)
	fmt.Printf("    %s\n", cfg.note)
	for _, l := range log {
		fmt.Println(l)
	}
	fmt.Printf("    failures %d/%d   connections accepted %d   requests served %d   handshakes %d\n\n",
		failures, requests, atomic.LoadInt64(&accepted), atomic.LoadInt64(&served), pool.handshakes)
	return failures, pool.handshakes
}

// The documented fallback chain, written as code so it cannot drift from the
// prose: IdleTimeout, else ReadTimeout, else nothing at all.
func effectiveIdle(cfg config) string {
	if cfg.idleTimeout != 0 {
		return fmt.Sprintf("%v  (IdleTimeout)", cfg.idleTimeout)
	}
	if cfg.readTimeout != 0 {
		return fmt.Sprintf("%v  (falls back to ReadTimeout)", cfg.readTimeout)
	}
	return "none -- the server never closes an idle connection"
}

func main() {
	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Go: the server that never closes, and the one line that changes that")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Printf("  %s\n", runtime.Version())
	fmt.Println("  The pool below holds ONE connection and reuses it after an idle gap,")
	fmt.Println("  which is what nginx, an ALB and every HTTP client you own all do.")
	fmt.Println()

	fails := map[string]int{}
	shakes := map[string]int{}
	for _, cfg := range []config{
		{"default            -- IdleTimeout 0, ReadTimeout 0", 0, 0,
			"ordering: the server never closes  <-- correct, by accident"},
		{"read-timeout       -- IdleTimeout 0, ReadTimeout 300ms", 0, 300 * time.Millisecond,
			"ordering: server closes first      <-- the bug, acquired sideways"},
		{"short-idle         -- IdleTimeout 300ms", 300 * time.Millisecond, 0,
			"ordering: server closes first      <-- the bug, on purpose"},
		{"ordered            -- IdleTimeout 3s", 3 * time.Second, 0,
			"ordering: pool closes first        <-- correct, by design"},
	} {
		f, h := run(cfg)
		key := strings.Fields(cfg.name)[0]
		fails[key], shakes[key] = f, h
	}

	fmt.Println("  Read the read-timeout row again.")
	fmt.Println("    Nobody sets ReadTimeout to get an idle timeout. They set it because a")
	fmt.Println("    slow-loris check told them to, or because a linter did. IdleTimeout")
	fmt.Println("    then quietly inherits it, the server starts closing idle connections")
	fmt.Println("    the load balancer is holding, and a change made for security reasons")
	fmt.Printf("    produced %d intermittent 502s here with no mention of keep-alive anywhere.\n", fails["read-timeout"])
	fmt.Println()
	fmt.Println("  And read the default row against Topic 2's lesson.")
	fmt.Println("    'No timeout' is not the absence of a decision. It protects you from")
	fmt.Println("    this bug and hands you a different one: connections that are never")
	fmt.Println("    reaped, held by peers that vanished without a FIN, accumulating file")
	fmt.Println("    descriptors and memory until something else breaks. The answer is not")
	fmt.Println("    to leave it at zero. It is to set IdleTimeout ABOVE your load")
	fmt.Println("    balancer's idle timeout -- which needs you to know both numbers, and")
	fmt.Println("    the second one is not in this program or in Go's documentation.")
	fmt.Println()
	fmt.Printf("  handshake counts: default %d, read-timeout %d, short-idle %d, ordered %d\n",
		shakes["default"], shakes["read-timeout"], shakes["short-idle"], shakes["ordered"])
	fmt.Println("    Every failure costs a reconnect, so this defect raises your connection")
	fmt.Println("    rate before it raises your error rate. Topic 7 counts those SYNs.")
}
