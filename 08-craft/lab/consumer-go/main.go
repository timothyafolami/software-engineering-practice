// A Go consumer of the craft-lab API, and topic 7's Go client in ladder F.
//
// WHAT THIS DEMONSTRATES: where a Go client queues, and what it reports while
// waiting. `context.Context` deadlines propagate by convention through the whole
// ecosystem, and the deadline is a mandatory first parameter -- so forgetting to
// propagate it is a VISIBLE OMISSION at every call site rather than an invisible
// default. That is the structural property worth internalising, not envying.
//
// WHAT TO LOOK FOR: the `X-Client-Queue-Ms` header. It reports how long this
// process spent waiting for a connection from its OWN transport pool, separately
// from how long the API took. From outside, those two waits look identical; that
// is why ladder F exists.
//
//	go build ./...          # topic 6: the contract check
//	go run .                # topic 7: serve on :8080
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/http/httptrace"
	"os"
	"strconv"
	"time"
)

const (
	// MaxIdleConnsPerHost is the invisible second queue. Go's default is 2,
	// which means a client offering 100 concurrent requests spends most of its
	// time queueing HERE and never reaches the API's database pool at all.
	// Setting it explicitly is the difference between measuring the server and
	// measuring your own transport.
	maxConnsPerHost = 100
	clientTimeout   = 30 * time.Second
)

type client struct {
	base string
	http *http.Client
}

func newClient(base string) *client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConnsPerHost = maxConnsPerHost
	t.MaxConnsPerHost = maxConnsPerHost
	return &client{base: base, http: &http.Client{Transport: t, Timeout: clientTimeout}}
}

// listOrders fetches one customer's orders. The context is the FIRST parameter,
// which is the convention that makes a missing deadline visible in review.
func (c *client) listOrders(ctx context.Context, customerID int) (*CustomerOrderListOut, time.Duration, error) {
	url := fmt.Sprintf("%s/customers/%d/orders?limit=50", c.base, customerID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}

	// httptrace tells us how much of the wall clock was spent waiting for a
	// connection rather than waiting for the server. Without this the two are
	// indistinguishable from the caller's side, and every incident review turns
	// into an argument about whose queue it was.
	var gotConnAt, startedAt time.Time
	startedAt = time.Now()
	trace := &httptrace.ClientTrace{GetConn: func(string) { startedAt = time.Now() },
		GotConn: func(httptrace.GotConnInfo) { gotConnAt = time.Now() }}
	req = req.WithContext(httptrace.WithClientTrace(req.Context(), trace))

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	queue := time.Duration(0)
	if !gotConnAt.IsZero() {
		queue = gotConnAt.Sub(startedAt)
	}

	if resp.StatusCode >= 400 {
		var e ApiError
		_ = json.NewDecoder(resp.Body).Decode(&e)
		return nil, queue, fmt.Errorf("api %d: %s %s", resp.StatusCode, e.Error, e.Message)
	}

	var out CustomerOrderListOut
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		// A decode error IS the contract break surfacing at runtime, for any
		// break the compiler could not see. Distinguish it from a transport
		// error in the log, because they have completely different fixes.
		return nil, queue, fmt.Errorf("response did not match the generated contract: %w", err)
	}
	return &out, queue, nil
}

func main() {
	api := os.Getenv("API")
	if api == "" {
		api = "http://api:8000"
	}
	c := newClient(api)

	mux := http.NewServeMux()
	mux.HandleFunc("/orders", func(w http.ResponseWriter, r *http.Request) {
		customer, _ := strconv.Atoi(r.URL.Query().Get("customer"))
		if customer == 0 {
			customer = 1
		}
		// One budget for this request, propagated. Nothing in Go applies one
		// for you either -- the difference is that the parameter is right there
		// in every signature, so omitting it is an act rather than an oversight.
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()

		out, queue, err := c.listOrders(ctx, customer)
		w.Header().Set("X-Client-Queue-Ms", strconv.FormatInt(queue.Milliseconds(), 10))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"ok":true}`)
	})

	log.Printf("consumer-go listening on :8080, target %s (MaxConnsPerHost=%d)", api, maxConnsPerHost)
	log.Fatal(http.ListenAndServe(":8080", mux))
}
