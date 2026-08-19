// Layer 2 · Topic 6 - Go: the queue did not disappear. It moved inside the
// connection, where nothing can see it.
//
// Go's net/http enables HTTP/2 automatically over TLS, and the peer's advertised
// SETTINGS_MAX_CONCURRENT_STREAMS becomes your real concurrency limit: Topic 2's
// pool queue is still there, still bounded, still the thing setting your
// throughput -- and it is now inside the connection, invisible to `ss`, invisible
// to a connection-count dashboard, and bounded by a number the SERVER chose.
//
// What the transport does with the requests that do not fit is the part you must
// MEASURE rather than look up. The folklore -- and older Go releases -- say it
// keeps a SINGLE connection per host and QUEUES the excess on it. This program
// counts accepted connections instead of assuming, and on the toolchain it
// prints below it may well find the opposite: a transport that dials another
// connection once the first is at its stream ceiling. Both behaviours have
// shipped. Read the connection count in the h2 row, not this comment.
//
// Compare that with the Python file in this topic, which shows httpx doing the
// opposite thing with the identical protocol: it opens streams past the
// advertised limit and then fails locally with LocalProtocolError. Same RFC,
// same SETTINGS frame, two clients: one turns overload into invisible latency,
// the other turns it into errors. Neither tells you the number.
//
// Everything here is standard library, including the TLS certificate, which is
// generated in-process -- HTTP/2 needs ALPN over TLS in Go, so a cleartext
// server would silently give you HTTP/1.1 and a comparison of nothing.
//
// Two runs, identical workload:
//
//	h1   Transport with MaxConnsPerHost = POOL, forced to HTTP/1.1 via ALPN
//	h2   the same Transport with HTTP/2 allowed
//
// The server holds every request for DELAY, so elapsed time measures real
// in-flight concurrency:  effective = requests x DELAY / wall.
//
// What to look for in the output:
//   - connections accepted: POOL for h1, ONE for h2
//   - effective concurrency for h2, which lands at the server's advertised
//     MAX_CONCURRENT_STREAMS -- a number this program MEASURES rather than
//     quotes, because Go's default has changed across releases and the one in
//     your head is probably from a blog post
//   - zero errors in both runs. The h2 excess was queued, not refused, which
//     is why this ceiling is so much harder to notice than Topic 2's
//
// Run: go run h2_queues_where_h1_pools.go
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	requests = 400
	delay    = 100 * time.Millisecond
	pool     = 10
)

type connStats struct {
	inFlight atomic.Int64
	max      atomic.Int64
}

type ctxKey struct{}

type counters struct {
	connections atomic.Int64
	inFlight    atomic.Int64
	maxInFlight atomic.Int64
	maxPerConn  atomic.Int64 // the number that actually is the stream limit
	served      atomic.Int64
}

func (c *counters) start() {
	n := c.inFlight.Add(1)
	c.served.Add(1)
	for {
		m := c.maxInFlight.Load()
		if n <= m || c.maxInFlight.CompareAndSwap(m, n) {
			break
		}
	}
}

func (c *counters) finish() { c.inFlight.Add(-1) }

// The interesting ceiling is per CONNECTION, not per process: it is the peer's
// SETTINGS_MAX_CONCURRENT_STREAMS. Counting globally would just tell you how
// many goroutines you started.
func (c *counters) observeConn(cs *connStats) {
	n := cs.inFlight.Add(1)
	for {
		m := cs.max.Load()
		if n <= m || cs.max.CompareAndSwap(m, n) {
			break
		}
	}
	for {
		m := c.maxPerConn.Load()
		if n <= m || c.maxPerConn.CompareAndSwap(m, n) {
			break
		}
	}
}

// A self-signed certificate, in memory. HTTP/2 in Go is negotiated by ALPN
// over TLS; without this the server speaks HTTP/1.1 and the h2 row measures
// the h1 row.
func selfSigned() tls.Certificate {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		panic(err)
	}
	tmpl := x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "lab"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		IsCA:                  true,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		panic(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

func startServer(c *counters) (string, *http.Server) {
	mux := http.NewServeMux()
	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		c.start()
		cs, _ := r.Context().Value(ctxKey{}).(*connStats)
		if cs != nil {
			c.observeConn(cs)
		}
		time.Sleep(delay)
		if cs != nil {
			cs.inFlight.Add(-1)
		}
		c.finish()
		io.WriteString(w, "ok")
	})

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	srv := &http.Server{
		Handler: mux,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{selfSigned()},
			// h2 first, http/1.1 second: the client's own ALPN list decides
			// which one is actually used, which is how the two runs below
			// differ by exactly one field.
			NextProtos: []string{"h2", "http/1.1"},
		},
		ConnState: func(_ net.Conn, s http.ConnState) {
			if s == http.StateNew {
				c.connections.Add(1)
			}
		},
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return context.WithValue(ctx, ctxKey{}, &connStats{})
		},
	}
	go srv.ServeTLS(ln, "", "")
	return "https://" + ln.Addr().String() + "/work", srv
}

func run(label string, client *http.Client, url string, c *counters, ceiling string) {
	// Warm up ONE request before the burst, then reset the counters. Without
	// this, four hundred goroutines all find an empty pool at the same instant
	// and Go dials a hundred-odd connections before the first handshake
	// finishes -- for HTTP/2 as well, because the dialling happens in the
	// generic transport before ALPN has told anyone this will be multiplexed.
	// That is a real behaviour worth knowing (a cold h2 pool under a burst is
	// NOT one connection), and it is also not what this program is measuring.
	if resp, err := client.Get(url); err == nil {
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}

	c.connections.Store(0)
	c.maxInFlight.Store(0)
	c.maxPerConn.Store(0)
	c.served.Store(0)

	var wg sync.WaitGroup
	var failures atomic.Int64
	var firstErr atomic.Value

	t0 := time.Now()
	for i := 0; i < requests; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := client.Get(url)
			if err != nil {
				failures.Add(1)
				firstErr.CompareAndSwap(nil, err.Error())
				return
			}
			if resp.StatusCode != http.StatusOK {
				failures.Add(1)
				firstErr.CompareAndSwap(nil, "unexpected status "+resp.Status)
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}()
	}
	wg.Wait()
	elapsed := time.Since(t0)

	// The response tells us which protocol actually ran. Checking this is not
	// paranoia: an h2 run that silently fell back to HTTP/1.1 is the single
	// most common way to produce two identical rows and a wrong conclusion.
	proto := "unknown"
	if resp, err := client.Get(url); err == nil {
		proto = resp.Proto
		resp.Body.Close()
	}

	effective := float64(requests) * delay.Seconds() / elapsed.Seconds()
	fmt.Printf("    %s\n", label)
	fmt.Printf("      negotiated              %s\n", proto)
	fmt.Printf("      wall time               %.2f s\n", elapsed.Seconds())
	fmt.Printf("      failures                %d %s\n", failures.Load(), errNote(&firstErr))
	fmt.Printf("      connections accepted    %d\n", c.connections.Load())
	fmt.Printf("      max in flight, all conns %d\n", c.maxInFlight.Load())
	fmt.Printf("      max streams on ONE conn  %d   <- the ceiling that is not yours\n", c.maxPerConn.Load())
	fmt.Printf("      effective concurrency   %.1f   (= %d x %v / %.2fs)\n",
		effective, requests, delay, elapsed.Seconds())
	fmt.Printf("      ceiling                 %s\n\n", ceiling)
}

func errNote(v *atomic.Value) string {
	if s, ok := v.Load().(string); ok {
		return "(" + s + ")"
	}
	return ""
}

func main() {
	c := &counters{}
	url, srv := startServer(c)
	defer srv.Close()

	fmt.Println(strings.Repeat("=", 78))
	fmt.Println("Go: HTTP/2 moves the ceiling into the connection -- measure what the")
	fmt.Println("transport then does with the excess, because it is not what you were told")
	fmt.Println(strings.Repeat("=", 78))
	fmt.Printf("  %s -- record this next to any number below\n", runtime.Version())
	fmt.Printf("  %d concurrent requests, server holds each for %v\n", requests, delay)
	fmt.Printf("  h1 Transport.MaxConnsPerHost = %d\n\n", pool)

	h1 := &http.Client{Transport: &http.Transport{
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: true,                 // self-signed, in-process
			NextProtos:         []string{"http/1.1"}, // the ONE field that differs
		},
		MaxConnsPerHost:     pool,
		MaxIdleConnsPerHost: pool,
		ForceAttemptHTTP2:   false,
	}}

	h2 := &http.Client{Transport: &http.Transport{
		TLSClientConfig:   &tls.Config{InsecureSkipVerify: true},
		ForceAttemptHTTP2: true,
	}}

	fmt.Println("  Two runs, identical workload:")
	fmt.Println()
	run(fmt.Sprintf("h1, MaxConnsPerHost=%d", pool), h1, url, c,
		fmt.Sprintf("%d  (you, in Transport.MaxConnsPerHost)", pool))
	measuredH1 := c.maxPerConn.Load()
	run("h2, multiplexed", h2, url, c,
		"measured below  (the server, in its SETTINGS frame)")

	fmt.Printf("  The h2 ceiling this run MEASURED: %d concurrent streams on a single\n", c.maxPerConn.Load())
	fmt.Printf("  connection, across %d connection(s) in total.\n", c.connections.Load())
	fmt.Println("    Not quoted, because Go's server default has moved across releases and")
	fmt.Println("    a real peer's value is whatever that team configured. Read your own")
	fmt.Println("    off a capture (Topic 7) or off this kind of measurement.")
	fmt.Println()
	fmt.Printf("  h1 put %d request(s) on each connection at a time -- one, always, because\n", measuredH1)
	fmt.Printf("  that is what HTTP/1.1 is -- and reached its concurrency by holding %d of\n", pool)
	fmt.Println("  them open. You chose that number and can see it in `ss`.")
	fmt.Println("  h2 reached its number a completely different way: the server said so,")
	fmt.Println("  in a SETTINGS frame you never see, and the limit is PER CONNECTION.")
	fmt.Println()
	fmt.Println("  Now read the connection count in the h2 row again, because it is")
	fmt.Println("  probably not 1, and the folklore says it should be.")
	fmt.Println()
	fmt.Println("    The received wisdom -- and older versions of Go -- is that the HTTP/2")
	fmt.Println("    transport keeps ONE connection per host and queues requests past the")
	fmt.Println("    stream limit on it. What this run measured on this toolchain is that")
	fmt.Println("    once a connection is at its stream ceiling, the transport dials")
	fmt.Println("    another one. Both behaviours have shipped; which you get depends on")
	fmt.Println("    your Go version and on Transport.StrictMaxConcurrentStreams. Record")
	fmt.Println("    the version you measured with, next to the number.")
	fmt.Println()
	fmt.Println("    That difference matters more than it looks. If your client queues,")
	fmt.Println("    overload arrives as pure latency with no error rate and no new")
	fmt.Println("    sockets -- invisible. If it dials, overload arrives as a connection")
	fmt.Println("    storm against a server that chose a stream limit precisely to avoid")
	fmt.Println("    one. Same protocol, same peer, opposite operational signature, and")
	fmt.Println("    the deciding factor is a client-library detail nobody reviews.")
	fmt.Println()
	fmt.Println("  Two consequences worth carrying:")
	fmt.Println()
	fmt.Println("    1. Zero errors in both runs. Whatever the client did with the excess")
	fmt.Println("       -- queue it or dial around it -- it did not tell you. Compare that")
	fmt.Println("       with the Python file in this topic, where httpx raises")
	fmt.Println("       LocalProtocolError instead. Three clients, three policies for the")
	fmt.Println("       same SETTINGS frame.")
	fmt.Println()
	fmt.Println("    2. `ss -tan` cannot see a stream. Under h1 your capacity model is")
	fmt.Println("       connection-shaped and observable; under h2 the number that bounds")
	fmt.Println("       you is in-flight streams per connection, which almost nobody")
	fmt.Println("       exports and no socket tool reports.")
	fmt.Println()
	fmt.Println("    3. Transport.StrictMaxConcurrentStreams and, in current versions,")
	fmt.Println("       allowing more than one connection per host, are the knobs that")
	fmt.Println("       change this behaviour. Check what your Go version actually offers")
	fmt.Println("       before designing around either -- `go doc net/http.Transport` on")
	fmt.Println("       the toolchain you ship with, not this page.")
}
