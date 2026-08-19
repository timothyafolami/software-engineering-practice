// Layer 6 Topic 4 - One label, two failures, and the three places you can fix it.
//
// Why Go: the mechanism is a spec-mandated SDK default, not a runtime property,
// so this file is not here to be different arithmetic. It is here because the
// CONFIGURATION SURFACE is different, and the difference matters when you are
// trying to fix this at 3am:
//
//	Python  a per-view setting on the MeterProvider, or the process-wide
//	        OTEL_METRIC_CARDINALITY_LIMIT env var.
//	Go      metric.NewView(...) -> a function from Instrument to Stream, so the
//	        limit, the attribute FILTER and the aggregation are all decided in
//	        one place, by code, per instrument.
//
// That second option -- Stream.AttributeFilter -- is the lever Python does not
// give you as directly, and it is the better fix: dropping the attribute keeps
// your ratios honest, where letting it overflow does not. Both are modelled
// below, side by side, against the same traffic.
//
// No OpenTelemetry SDK is installed on this machine, so the counter, the view,
// the cardinality limit and the overflow attribute are written out here in
// about eighty lines of standard library. They implement the spec's rule: past
// the limit, the measurement is still counted, but its attributes are replaced
// by {otel.metric.overflow: true}.
//
// What to look for in the output
// ------------------------------
//  1. The three views' series counts, side by side, against the same 200k
//     requests. Same data, same instrument, three configurations.
//  2. The `sum by (customer_id)` gap under the limit -- the silent failure.
//  3. Section 4's MEASURED bytes per series, from runtime.MemStats on this
//     machine, then multiplied out. The measurement is real; the multiplication
//     is labelled as a multiplication.
//  4. Section 5, the collector fix: when thirty services are emitting a bad
//     label you cannot redeploy thirty services, but you can edit one config.
//     The last block shows the same traffic passing through a drop rule and a
//     hash rule, and what each one costs you.
package main

import (
	"fmt"
	"math/rand"
	"runtime"
	"strings"
)

// The spec default. In the real Go SDK this is
// metric.NewView(metric.Instrument{Name: "..."},
//
//	metric.Stream{AggregationCardinalityLimit: 2000})
const defaultCardinalityLimit = 2000

// ---------------------------------------------------------------------------
// A counter with a view: a cardinality limit and an optional attribute filter.
// ---------------------------------------------------------------------------

type attrSet struct {
	route      string
	method     string
	status     int
	customerID string // "" means the attribute is not present
	overflow   bool
}

func (a attrSet) key() string {
	if a.overflow {
		return "otel.metric.overflow=true"
	}
	return fmt.Sprintf("route=%s,method=%s,status=%d,customer_id=%s",
		a.route, a.method, a.status, a.customerID)
}

// Stream is the Go SDK's view output, cut down to the two fields this topic is
// about. AttributeFilter runs BEFORE the cardinality limit, which is the whole
// reason it is the better fix.
type Stream struct {
	Name                        string
	AggregationCardinalityLimit int                   // 0 = unlimited
	AttributeFilter             func(attrSet) attrSet // nil = keep everything
}

type Counter struct {
	stream           Stream
	series           map[string]int64
	attrs            map[string]attrSet
	overflowedPoints int64
	rejectedAttrSets map[string]struct{}
}

func NewCounter(stream Stream) *Counter {
	return &Counter{
		stream:           stream,
		series:           make(map[string]int64),
		attrs:            make(map[string]attrSet),
		rejectedAttrSets: make(map[string]struct{}),
	}
}

func (c *Counter) Add(value int64, a attrSet) {
	if c.stream.AttributeFilter != nil {
		a = c.stream.AttributeFilter(a)
	}
	k := a.key()
	if _, ok := c.series[k]; ok {
		c.series[k] += value
		return
	}
	limit := c.stream.AggregationCardinalityLimit
	// The spec reserves one slot for the overflow series itself.
	if limit == 0 || len(c.series) < limit-1 {
		c.series[k] = value
		c.attrs[k] = a
		return
	}
	overflow := attrSet{overflow: true}
	ok := overflow.key()
	c.series[ok] += value
	c.attrs[ok] = overflow
	c.overflowedPoints += value
	c.rejectedAttrSets[k] = struct{}{}
}

// Total is sum(rate(...)) with no label matcher. Always correct.
func (c *Counter) Total() int64 {
	var total int64
	for _, v := range c.series {
		total += v
	}
	return total
}

// SumBy is sum by (label) (...). The overflow point has no such label, so it
// silently drops out.
func (c *Counter) SumBy(label string) map[string]int64 {
	out := make(map[string]int64)
	for k, v := range c.series {
		a := c.attrs[k]
		if a.overflow {
			continue
		}
		switch label {
		case "customer_id":
			if a.customerID == "" {
				continue
			}
			out[a.customerID] += v
		case "route":
			out[a.route] += v
		default:
			panic("unknown label " + label)
		}
	}
	return out
}

// SumWhere is any query with a label matcher, e.g. {status=~"5.."}. Same trap:
// the overflow point carries no status either.
func (c *Counter) SumWhere(pred func(attrSet) bool) int64 {
	var total int64
	for k, v := range c.series {
		a := c.attrs[k]
		if a.overflow {
			continue
		}
		if pred(a) {
			total += v
		}
	}
	return total
}

func (c *Counter) ActiveSeries() int { return len(c.series) }

// ---------------------------------------------------------------------------
// Traffic
// ---------------------------------------------------------------------------

var routes = buildRoutes()
var methods = []string{"GET", "POST", "PUT", "DELETE", "PATCH"}
var statuses = []int{200, 201, 204, 400, 401, 404, 429, 500}
var statusWeights = []int{70, 8, 5, 5, 3, 4, 3, 2}

func buildRoutes() []string {
	// 40 templated routes. The raw-path version of this list is unbounded,
	// because `?utm_source=` is user input.
	out := []string{"/health", "/ready", "/metrics", "/checkout",
		"/pricing/quote", "/search", "/login", "/logout"}
	for _, r := range []string{"orders", "customers", "items", "invoices",
		"shipments", "returns", "quotes", "carts"} {
		out = append(out,
			"/"+r,
			"/"+r+"/{id}",
			"/"+r+"/{id}/events",
			"/"+r+"/{id}/audit")
	}
	return out
}

const (
	customers = 10_000
	requests  = 200_000
)

func weightedPick(rng *rand.Rand, weights []int) int {
	total := 0
	for _, w := range weights {
		total += w
	}
	n := rng.Intn(total)
	for i, w := range weights {
		n -= w
		if n < 0 {
			return i
		}
	}
	return len(weights) - 1
}

func generateTraffic(rng *rand.Rand) []attrSet {
	out := make([]attrSet, 0, requests)
	for i := 0; i < requests; i++ {
		out = append(out, attrSet{
			route:      routes[rng.Intn(len(routes))],
			method:     methods[weightedPick(rng, []int{70, 20, 5, 3, 2})],
			status:     statuses[weightedPick(rng, statusWeights)],
			customerID: fmt.Sprintf("cust-%05d", rng.Intn(customers)),
		})
	}
	return out
}

func section(title string) {
	fmt.Printf("\n%s\n%s\n", title, strings.Repeat("-", 52))
}

func main() {
	rng := rand.New(rand.NewSource(20260818))

	fmt.Println("Layer 6 Topic 4 - cardinality in Go: three views over one instrument")
	fmt.Printf("%s   spec default AggregationCardinalityLimit = %d\n",
		runtime.Version(), defaultCardinalityLimit)
	fmt.Println(strings.Repeat("=", 72))

	traffic := generateTraffic(rng)

	// -----------------------------------------------------------------------
	section("1. Series count is a product, and you can do it on a napkin")
	bounded := len(routes) * len(methods) * len(statuses)
	fmt.Printf("  routes x methods x statuses   %d x %d x %d = %s series\n",
		len(routes), len(methods), len(statuses), comma(int64(bounded)))
	fmt.Printf("  ... x customer_id             %s x %s = %s series\n",
		comma(int64(bounded)), comma(customers), comma(int64(bounded)*customers))
	fmt.Println()
	fmt.Println("  Nothing about the recording changed. One label multiplied.")

	// -----------------------------------------------------------------------
	section("2. Three views, one instrument, the same 200,000 requests")

	// View A: the label is not there at all. This is what you shipped last week.
	viewBounded := NewCounter(Stream{
		Name:                        "http.server.requests",
		AggregationCardinalityLimit: defaultCardinalityLimit,
		AttributeFilter: func(a attrSet) attrSet {
			a.customerID = ""
			return a
		},
	})

	// View B: customer_id added, default limit left in place. The silent one.
	viewLimited := NewCounter(Stream{
		Name:                        "http.server.requests",
		AggregationCardinalityLimit: defaultCardinalityLimit,
	})

	// View C: customer_id added, limit removed. The loud one -- do not do this
	// in a process that shares a TSDB with anything you care about.
	viewUnlimited := NewCounter(Stream{
		Name:                        "http.server.requests",
		AggregationCardinalityLimit: 0,
	})

	for _, a := range traffic {
		viewBounded.Add(1, a)
		viewLimited.Add(1, a)
		viewUnlimited.Add(1, a)
	}

	fmt.Printf("  %-34s %12s %14s\n", "view", "series", "sum(rate(...))")
	fmt.Printf("  %-34s %12s %14s\n", strings.Repeat("-", 34),
		strings.Repeat("-", 12), strings.Repeat("-", 14))
	fmt.Printf("  %-34s %12s %14s\n", "A: AttributeFilter drops it",
		comma(int64(viewBounded.ActiveSeries())), comma(viewBounded.Total()))
	fmt.Printf("  %-34s %12s %14s\n", "B: limit 2000 (the default)",
		comma(int64(viewLimited.ActiveSeries())), comma(viewLimited.Total()))
	fmt.Printf("  %-34s %12s %14s\n", "C: limit 0 (unlimited)",
		comma(int64(viewUnlimited.ActiveSeries())), comma(viewUnlimited.Total()))
	fmt.Println()
	fmt.Println("  All three totals agree. That is the point: the total is the one")
	fmt.Println("  number that survives every version of this failure, which is")
	fmt.Println("  exactly why the total is not where you will notice it.")

	// -----------------------------------------------------------------------
	section("3. The silent failure, in the two queries that disagree")

	total := viewLimited.Total()
	byCustomer := viewLimited.SumBy("customer_id")
	var visible int64
	for _, v := range byCustomer {
		visible += v
	}
	gap := total - visible

	trueErrors := int64(0)
	for _, a := range traffic {
		if a.status >= 500 {
			trueErrors++
		}
	}
	metricErrors := viewLimited.SumWhere(func(a attrSet) bool { return a.status >= 500 })

	fmt.Printf("  sum(rate(...))                       %14s\n", comma(total))
	fmt.Printf("  sum by (customer_id) (rate(...))     %14s\n", comma(visible))
	fmt.Printf("  gap                                  %14s  (%.1f%% of traffic)\n",
		comma(gap), 100*float64(gap)/float64(total))
	fmt.Printf("  customer_id values surviving         %14s of %s\n",
		comma(int64(len(byCustomer))), comma(customers))
	fmt.Printf("  attribute sets rejected              %14s\n",
		comma(int64(len(viewLimited.rejectedAttrSets))))
	fmt.Println()
	fmt.Printf("  error ratio, ground truth            %13.2f%%\n",
		100*float64(trueErrors)/float64(requests))
	fmt.Printf("  error ratio, as the metric reports   %13.2f%%  <- your alert\n",
		100*float64(metricErrors)/float64(total))
	fmt.Println()
	fmt.Println("  The overflow point carries the count and NOTHING else -- no")
	fmt.Println("  status, no route, no method. So every query with a label matcher")
	fmt.Println("  loses it, and a ratio whose numerator filters and whose")
	fmt.Println("  denominator does not is wrong in the safe-looking direction.")

	// -----------------------------------------------------------------------
	section("4. The loud failure: bytes per series, measured then multiplied")

	perSeries10k := measureBytesPerSeries(10_000)
	perSeries100k := measureBytesPerSeries(100_000)

	fmt.Println("  measured on THIS machine with runtime.ReadMemStats:")
	fmt.Printf("    %8s series   %6.0f bytes/series\n", comma(10_000), perSeries10k)
	fmt.Printf("    %8s series   %6.0f bytes/series\n", comma(100_000), perSeries100k)
	fmt.Println()
	projected := int64(bounded) * customers
	fmt.Println("  extrapolated (measured cost x series count -- NOT a measurement):")
	fmt.Printf("    %8s series   %6.1f GB in this toy store alone\n",
		comma(projected), float64(projected)*perSeries100k/1e9)
	fmt.Println()
	fmt.Println("  Prometheus pays more per series than a Go map does: head block,")
	fmt.Println("  inverted index, label-value dictionary, and query fan-out all")
	fmt.Println("  scale with the count. Take the SHAPE from this number -- a")
	fmt.Println("  straight line with your customer count on the x axis -- and get")
	fmt.Println("  the magnitude from prometheus_tsdb_head_series on the real thing.")

	// -----------------------------------------------------------------------
	section("5. The collector fix: one config instead of thirty deploys")

	// A collector processor is a function from attributes to attributes,
	// applied to every service's telemetry at once. That is the entire reason
	// it is the only intervention available during an incident.
	drop := func(a attrSet) attrSet { a.customerID = ""; return a }
	hash := func(a attrSet) attrSet {
		// attributes/transform can hash instead of dropping: you keep a bounded
		// number of buckets and lose the ability to name one customer.
		h := uint32(2166136261)
		for i := 0; i < len(a.customerID); i++ {
			h = (h ^ uint32(a.customerID[i])) * 16777619
		}
		a.customerID = fmt.Sprintf("bucket-%02d", h%16)
		return a
	}

	dropped := NewCounter(Stream{Name: "http.server.requests",
		AggregationCardinalityLimit: defaultCardinalityLimit, AttributeFilter: drop})
	hashed := NewCounter(Stream{Name: "http.server.requests",
		AggregationCardinalityLimit: defaultCardinalityLimit, AttributeFilter: hash})
	// Same hash, with the limit raised deliberately to fit the series it
	// produces. Raising the limit is only safe when you have done the
	// multiplication first -- which is the whole habit this topic is teaching.
	hashedRaised := NewCounter(Stream{Name: "http.server.requests",
		AggregationCardinalityLimit: 50_000, AttributeFilter: hash})
	for _, a := range traffic {
		dropped.Add(1, a)
		hashed.Add(1, a)
		hashedRaised.Add(1, a)
	}

	trueRatio := 100 * float64(trueErrors) / float64(requests)
	errRatio := func(c *Counter) float64 {
		e := c.SumWhere(func(a attrSet) bool { return a.status >= 500 })
		return 100 * float64(e) / float64(c.Total())
	}

	fmt.Printf("  %-30s %9s %9s  %s\n", "collector rule", "series", "error %",
		"can you still name a customer?")
	fmt.Printf("  %-30s %9s %9s  %s\n", strings.Repeat("-", 30),
		strings.Repeat("-", 9), strings.Repeat("-", 9), strings.Repeat("-", 30))
	fmt.Printf("  %-30s %9s %8.2f%%  %s\n", "none (overflowing)",
		comma(int64(viewLimited.ActiveSeries())),
		100*float64(metricErrors)/float64(total), "no, and it does not say so")
	fmt.Printf("  %-30s %9s %8.2f%%  %s\n", "delete customer_id",
		comma(int64(dropped.ActiveSeries())), errRatio(dropped),
		"no, and you chose that")
	fmt.Printf("  %-30s %9s %8.2f%%  %s\n", "hash -> 16, limit 2000",
		comma(int64(hashed.ActiveSeries())), errRatio(hashed),
		"no -- still overflowing")
	fmt.Printf("  %-30s %9s %8.2f%%  %s\n", "hash -> 16, limit 50000",
		comma(int64(hashedRaised.ActiveSeries())), errRatio(hashedRaised),
		"by bucket, not by name")
	fmt.Println()
	fmt.Printf("  ground truth error ratio: %.2f%%\n", trueRatio)
	fmt.Println()
	fmt.Println("  Read the error-% column against ground truth, and read row 3")
	fmt.Println("  twice. Hashing customer_id into 16 buckets sounds like a fix and")
	fmt.Printf("  is not, because the base label set is already %s series: %s x 16\n",
		comma(int64(bounded)), comma(int64(bounded)))
	fmt.Printf("  = %s, which is %.0fx over the default limit, so it still\n",
		comma(int64(bounded)*16), float64(bounded*16)/float64(defaultCardinalityLimit))
	fmt.Println("  overflows and still lies. A BOUNDED label is not automatically a")
	fmt.Println("  SMALL one, and the default limit of 2000 is smaller than one")
	fmt.Println("  ordinary HTTP label set with anything at all added to it.")
	fmt.Println()
	fmt.Println("  Rows 2 and 4 are the two honest fixes: delete the dimension, or")
	fmt.Println("  keep a bounded version of it AND raise the limit to a number you")
	fmt.Println("  computed first. Both give the correct error ratio. Neither lets")
	fmt.Println("  you name one customer from a metric -- that capability lives in a")
	fmt.Println("  span attribute, with an exemplar as the link back. The Python")
	fmt.Println("  program in this topic covers that half.")

	fmt.Println()
	fmt.Println(strings.Repeat("=", 72))
	fmt.Println("The review question, askable in ten seconds:")
	fmt.Println("  'How many distinct values can this label take, and who decides")
	fmt.Println("   that number -- us, or a user?'")
	fmt.Println("If a user decides it, it is a span attribute, not a label.")
}

// measureBytesPerSeries builds n distinct series and measures the heap delta
// on this machine. It is a real measurement of a Go map of counters -- not of
// Prometheus, and the output says so.
func measureBytesPerSeries(n int) float64 {
	// Both readings are taken immediately after a GC, so what is compared is
	// LIVE heap and not allocation churn. Without the second GC this number
	// swings by 2x between runs, which would make it a number you should not
	// print.
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)

	c := NewCounter(Stream{Name: "probe", AggregationCardinalityLimit: 0})
	for i := 0; i < n; i++ {
		c.Add(1, attrSet{route: "/orders", method: "GET", status: 200,
			customerID: fmt.Sprintf("cust-%08d", i)})
	}

	runtime.GC() // c is still reachable, so its map stays on the live heap
	runtime.ReadMemStats(&after)
	bytes := float64(after.HeapAlloc - before.HeapAlloc)
	runtime.KeepAlive(c)
	return bytes / float64(n)
}

func comma(n int64) string {
	s := fmt.Sprintf("%d", n)
	if n < 0 {
		return "-" + comma(-n)
	}
	var parts []string
	for len(s) > 3 {
		parts = append([]string{s[len(s)-3:]}, parts...)
		s = s[:len(s)-3]
	}
	parts = append([]string{s}, parts...)
	return strings.Join(parts, ",")
}
