// Layer 2 · Topic 2 - Go's pools fail OPEN, and that is not a kindness.
//
// Go is in this topic because its defaults are the mirror image of
// Python's. Where httpx bounds connections and raises, Go's two most
// important pools are unbounded out of the box:
//
//	http.Transport.MaxConnsPerHost = 0        // 0 means unlimited
//	sql.DB                                    // no limit until you call
//	                                          // SetMaxOpenConns
//
// Unlimited does not remove the limit. It moves it out of your process and
// into somebody else's -- the upstream's accept queue, the database's
// max_connections, your own file-descriptor ceiling. All three fail in
// ways that are harder to attribute than a PoolTimeout in your own logs.
//
// This program drives one slow upstream with a burst of concurrent
// requests and counts, from the SERVER side, how many connections each
// transport actually opened. Then it does the same with a bounded
// transport, so you can see the wait appear where the sockets used to be.
//
// What to look for in the output: connections opened by the default
// transport versus the concurrency you asked for. They are the same
// number. Nothing pushed back.
//
// Run: go run fails_open_by_default.go
package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
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
	concurrency = 60                     // simultaneous requests, i.e. a burst
	hold        = 300 * time.Millisecond // how long the upstream holds each one
)

func main() {
	var opened, concurrent, peakConcurrent int64

	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		now := atomic.AddInt64(&concurrent, 1)
		for {
			peak := atomic.LoadInt64(&peakConcurrent)
			if now <= peak || atomic.CompareAndSwapInt64(&peakConcurrent, peak, now) {
				break
			}
		}
		time.Sleep(hold)
		atomic.AddInt64(&concurrent, -1)
		io.WriteString(w, `{"ok":true}`)
	}))
	server.Config.ConnState = func(c net.Conn, state http.ConnState) {
		if state == http.StateNew {
			atomic.AddInt64(&opened, 1)
		}
	}
	server.Start()
	defer server.Close()

	fmt.Println("==============================================================================")
	fmt.Println("Go: unlimited by default, at both the HTTP and the database layer")
	fmt.Println("==============================================================================")
	fmt.Printf("  upstream %s holds each request for %s\n", server.URL, hold)
	fmt.Printf("  %d requests fired at once\n\n", concurrency)

	unbounded := http.DefaultTransport.(*http.Transport).Clone()
	fmt.Printf("  http.Transport defaults: MaxConnsPerHost=%d (0 = unlimited), "+
		"MaxIdleConnsPerHost=%d\n\n", unbounded.MaxConnsPerHost, http.DefaultMaxIdleConnsPerHost)

	burst("UNBOUNDED - MaxConnsPerHost = 0, the default",
		&http.Client{Transport: unbounded}, server.URL, &opened, &peakConcurrent)

	fmt.Println()
	bounded := http.DefaultTransport.(*http.Transport).Clone()
	bounded.MaxConnsPerHost = 8
	bounded.MaxIdleConnsPerHost = 8
	burst("BOUNDED   - MaxConnsPerHost = 8",
		&http.Client{Transport: bounded}, server.URL, &opened, &peakConcurrent)

	fmt.Println()
	fmt.Println("  Two things to notice about the bounded run:")
	fmt.Println("    - The connection count collapsed to the limit. That is the point.")
	fmt.Println("    - The wall clock went UP, and no request failed. Go's transport")
	fmt.Println("      makes waiters block on a channel with no deadline of its own;")
	fmt.Println("      the only thing that ends the wait is the request's Context.")
	fmt.Println("      MaxConnsPerHost WITHOUT a context deadline converts a socket")
	fmt.Println("      explosion into an invisible queue -- which is Topic 3's point")
	fmt.Println("      arriving early. Bound the pool AND propagate a deadline.")

	fmt.Println()
	describeSQLDefaults()
}

func burst(label string, client *http.Client, url string, opened, peak *int64) {
	beforeOpened := atomic.LoadInt64(opened)
	atomic.StoreInt64(peak, 0)

	var mu sync.Mutex
	latencies := make([]float64, 0, concurrency)
	started := time.Now()

	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			t := time.Now()
			// A real handler would carry the caller's context. This one has a
			// generous deadline purely so a hang shows up as an error rather
			// than as the program never finishing.
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			resp, err := client.Do(req)
			if err != nil {
				fmt.Printf("    request error: %v\n", err)
				return
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			mu.Lock()
			latencies = append(latencies, float64(time.Since(t).Microseconds())/1000)
			mu.Unlock()
		}()
	}
	wg.Wait()
	elapsed := time.Since(started)

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
	fmt.Printf("    requests                %d\n", len(latencies))
	fmt.Printf("    TCP connections opened  %d\n", atomic.LoadInt64(opened)-beforeOpened)
	fmt.Printf("    peak concurrent at the upstream %d\n", atomic.LoadInt64(peak))
	fmt.Printf("    wall clock              %s\n", elapsed.Round(time.Millisecond))
	fmt.Printf("    latency p50 %.1f ms   p99 %.1f ms\n", at(0.50), at(0.99))
	fmt.Printf("    errors                  %d\n", concurrency-len(latencies))
}

// describeSQLDefaults prints database/sql's pool defaults straight out of a
// live sql.DB. No driver is registered here, so this never opens a
// connection -- the point is the CONFIGURATION, and sql.OpenDB lets us read
// it without a database. (Layer 3 does the Postgres half properly.)
func describeSQLDefaults() {
	db := sql.OpenDB(nilConnector{})
	defer db.Close()
	stats := db.Stats()
	fmt.Println("  database/sql, straight out of the box:")
	fmt.Printf("    MaxOpenConnections  %d   <-- 0 means UNLIMITED\n", stats.MaxOpenConnections)
	fmt.Println("    MaxIdleConns        2   (the documented default; not exposed on DBStats)")
	fmt.Println("    ConnMaxLifetime     0   (0 means connections live forever -- Topic 5)")
	fmt.Println("    ConnMaxIdleTime     0   (0 means idle connections are never reaped)")
	fmt.Println()
	fmt.Println("    Compare with SQLAlchemy: pool_size=5, max_overflow=10, and a hard")
	fmt.Println("    QueuePool error when you exceed them. Go will happily open a")
	fmt.Println("    connection per concurrent query until Postgres answers")
	fmt.Println("    'FATAL: sorry, too many clients already' -- at which point the")
	fmt.Println("    error surfaces in a request handler that did nothing wrong, and")
	fmt.Println("    the service that caused it may not even be this one.")
	fmt.Println()
	fmt.Println("    The three lines every Go service should have and most do not:")
	fmt.Println("      db.SetMaxOpenConns(n)      // n <= your share of Postgres max_connections")
	fmt.Println("      db.SetMaxIdleConns(n)      // matching, or idle conns churn")
	fmt.Println("      db.SetConnMaxLifetime(5 * time.Minute)  // so failover is noticed")
}

// nilConnector satisfies driver.Connector without ever connecting. Connect
// would fail if called; db.Stats() does not call it, which is what lets this
// read the pool's real defaults with no database present.
type nilConnector struct{}

func (nilConnector) Connect(context.Context) (driver.Conn, error) {
	return nil, errors.New("no driver: this connector exists only to read pool defaults")
}

func (nilConnector) Driver() driver.Driver { return nil }
