// Layer 6 lab - `pricing`: the downstream that lies.
//
// A tiny Go service with a deliberate latency tail: 99 requests return in 8ms
// and the 100th takes 900ms. That shape is the point. A mean hides it entirely,
// a p50 hides it entirely, and it is transmitted into `api`'s latency through
// whatever concurrency mistake `api` is making -- which is Topic 2's exercise.
//
// Why Go: no runtime trap of its own (Layer 1 Topic 3 -- the scheduler protects
// you), so the tail this service produces is unambiguously a DEPENDENCY
// property rather than a runtime one. When you find it, you have found a fact
// about `pricing`, not about the language it happens to be written in.
//
// Standard library only, on purpose. Trace context is handled by parsing and
// emitting the W3C `traceparent` header by hand -- 30 lines, no OTel SDK, no
// modules to resolve at build time. That also makes Topic 3's third break
// (`BREAK=pricing_fresh_ctx`) something you can read rather than configure:
// see continueTrace below.
//
// The span EXPORT is hand-rolled too, for the same reason: OTLP over HTTP with
// the JSON encoding is one POST of one JSON document, so `pricing` can put
// itself in Tempo without an SDK. See exportSpan. This is not decoration --
// without it `pricing` emits no spans at all, and then Topic 3's third break
// ("two complete traces, each looking healthy") produces exactly one trace,
// and its fourth break (the collector dropping `pricing`'s spans) has nothing
// to drop. The two breaks that are supposed to be visible only at the
// collector are the two that need this.
//
// Endpoints:
//
//	GET  /price/{id}   the price lookup, with the tail
//	POST /_tail        {"on": true|false} -- Topic 7 scenario X's fault
//	GET  /health       liveness
//	GET  /stats        requests served, tails fired, for checking the ratio
//
// VERIFICATION STATUS: built and run inside the compose stack on 2026-08-19.
// Serves /price/{id} with the tail, exports its own spans to the collector,
// and joins or orphans the caller's trace depending on BREAK.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type config struct {
	tailEnabled bool
	tailEvery   int64
	tailMS      int64
	fastMS      int64
	breakMode   string
	serviceName string
}

func envInt(key string, fallback int64) int64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			return n
		}
	}
	return fallback
}

func loadConfig() config {
	return config{
		tailEnabled: os.Getenv("PRICING_TAIL") != "off",
		tailEvery:   envInt("PRICING_TAIL_EVERY", 100),
		tailMS:      envInt("PRICING_TAIL_MS", 900),
		fastMS:      envInt("PRICING_FAST_MS", 8),
		breakMode:   os.Getenv("BREAK"),
		serviceName: os.Getenv("OTEL_SERVICE_NAME"),
	}
}

// --- W3C trace context, by hand --------------------------------------------

type spanContext struct {
	traceID string
	spanID  string
	sampled bool
}

func parseTraceparent(header string) (spanContext, bool) {
	// version-traceid-spanid-flags
	parts := strings.Split(header, "-")
	if len(parts) != 4 || parts[0] != "00" || len(parts[1]) != 32 || len(parts[2]) != 16 {
		return spanContext{}, false
	}
	flags, err := strconv.ParseUint(parts[3], 16, 8)
	if err != nil {
		return spanContext{}, false
	}
	return spanContext{traceID: parts[1], spanID: parts[2], sampled: flags&1 == 1}, true
}

var spanCounter atomic.Uint64

func newSpanID() string {
	// Deterministic and unique within a process; good enough for a lab, and
	// nothing here is a security boundary.
	return fmt.Sprintf("%016x", spanCounter.Add(1))
}

// continueTrace decides whether this request joins the caller's trace.
//
// BREAK=pricing_fresh_ctx is Topic 3's third break: the service builds its
// tracer from a fresh context and ignores the incoming header. The result is
// the nastiest of the four shapes -- not a truncated trace but two complete
// ones, each looking perfectly healthy on its own.
func continueTrace(cfg config, r *http.Request) (span spanContext, parentSpanID string, joined bool) {
	if cfg.breakMode == "pricing_fresh_ctx" {
		// Note there is no parent even though a perfectly good traceparent
		// arrived on the wire: that is the break. The exported span roots a
		// second trace that is complete and internally consistent.
		return spanContext{traceID: fmt.Sprintf("%032x", spanCounter.Add(1)),
			spanID: newSpanID(), sampled: true}, "", false
	}
	if parent, ok := parseTraceparent(r.Header.Get("traceparent")); ok {
		return spanContext{traceID: parent.traceID, spanID: newSpanID(),
			sampled: parent.sampled}, parent.spanID, true
	}
	return spanContext{traceID: fmt.Sprintf("%032x", spanCounter.Add(1)),
		spanID: newSpanID(), sampled: true}, "", false
}

// --- OTLP/HTTP export, by hand ----------------------------------------------
//
// OTLP has three encodings; the JSON one over HTTP is the only one you can
// produce from the standard library, and it is a single POST of a single
// document to <endpoint>/v1/traces. The one thing that catches everybody: in
// OTLP/JSON `traceId` and `spanId` are LOWERCASE HEX STRINGS, not the base64
// that proto3's default JSON mapping would give you for a bytes field. That
// exception is written into opentelemetry-proto and nowhere else, and getting
// it wrong produces a 200 from the collector with the spans silently dropped
// as malformed -- which is the same symptom as not exporting at all.

var (
	otlpTracesURL string
	otlpQueue     chan []byte
	otlpClient    = &http.Client{Timeout: 3 * time.Second}
	otlpDropped   atomic.Int64
	otlpSent      atomic.Int64
)

func initExporter(cfg config) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		return
	}
	otlpTracesURL = strings.TrimRight(endpoint, "/") + "/v1/traces"
	// Buffered and lossy on purpose. An exporter that blocks the handler has
	// turned your telemetry into a dependency of your request path, which is
	// the failure Topic 4 is about. Dropping is counted, not hidden.
	otlpQueue = make(chan []byte, 2048)
	go func() {
		for body := range otlpQueue {
			req, err := http.NewRequest("POST", otlpTracesURL, strings.NewReader(string(body)))
			if err != nil {
				otlpDropped.Add(1)
				continue
			}
			req.Header.Set("Content-Type", "application/json")
			resp, err := otlpClient.Do(req)
			if err != nil {
				otlpDropped.Add(1)
				continue
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode >= 300 {
				otlpDropped.Add(1)
				continue
			}
			otlpSent.Add(1)
		}
	}()
}

func attrValue(v any) map[string]any {
	switch t := v.(type) {
	case string:
		return map[string]any{"stringValue": t}
	case bool:
		return map[string]any{"boolValue": t}
	case int:
		return map[string]any{"intValue": strconv.Itoa(t)}
	case int64:
		return map[string]any{"intValue": strconv.FormatInt(t, 10)}
	default:
		return map[string]any{"stringValue": fmt.Sprint(t)}
	}
}

func exportSpan(span spanContext, parentSpanID, name string, start, end time.Time, attrs map[string]any) {
	if otlpQueue == nil {
		return
	}
	otlpAttrs := make([]map[string]any, 0, len(attrs))
	for k, v := range attrs {
		otlpAttrs = append(otlpAttrs, map[string]any{"key": k, "value": attrValue(v)})
	}
	serviceName := os.Getenv("OTEL_SERVICE_NAME")
	if serviceName == "" {
		serviceName = "pricing"
	}
	s := map[string]any{
		"traceId":           span.traceID,
		"spanId":            span.spanID,
		"name":              name,
		"kind":              2, // SPAN_KIND_SERVER
		"startTimeUnixNano": strconv.FormatInt(start.UnixNano(), 10),
		"endTimeUnixNano":   strconv.FormatInt(end.UnixNano(), 10),
		"attributes":        otlpAttrs,
		"status":            map[string]any{},
	}
	if parentSpanID != "" {
		s["parentSpanId"] = parentSpanID
	}
	body, err := json.Marshal(map[string]any{
		"resourceSpans": []map[string]any{{
			"resource": map[string]any{"attributes": []map[string]any{
				{"key": "service.name", "value": map[string]any{"stringValue": serviceName}},
			}},
			"scopeSpans": []map[string]any{{
				"scope": map[string]any{"name": "lab.pricing"},
				"spans": []map[string]any{s},
			}},
		}},
	})
	if err != nil {
		otlpDropped.Add(1)
		return
	}
	select {
	case otlpQueue <- body:
	default:
		otlpDropped.Add(1)
	}
}

// --- The service ------------------------------------------------------------

type server struct {
	mu       sync.RWMutex
	cfg      config
	served   atomic.Int64
	tails    atomic.Int64
	joined   atomic.Int64
	orphaned atomic.Int64
}

func (s *server) tailFires() bool {
	s.mu.RLock()
	cfg := s.cfg
	s.mu.RUnlock()
	if !cfg.tailEnabled || cfg.tailEvery <= 0 {
		return false
	}
	return s.served.Load()%cfg.tailEvery == 0
}

func (s *server) handlePrice(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	cfg := s.cfg
	s.mu.RUnlock()

	started := time.Now()
	span, parentSpanID, joined := continueTrace(cfg, r)
	if joined {
		s.joined.Add(1)
	} else {
		s.orphaned.Add(1)
	}

	n := s.served.Add(1)
	slow := s.tailFires()
	delay := time.Duration(cfg.fastMS) * time.Millisecond
	if slow {
		delay = time.Duration(cfg.tailMS) * time.Millisecond
		s.tails.Add(1)
	}
	time.Sleep(delay)

	idPart := strings.TrimPrefix(r.URL.Path, "/price/")
	orderID, _ := strconv.ParseInt(idPart, 10, 64)

	// One JSON log line per request, with the trace id, so the one-query test
	// covers this service too.
	logLine(map[string]any{
		"level": "INFO", "service": "pricing", "msg": "price served",
		"trace_id": span.traceID, "span_id": span.spanID,
		"order_id": orderID, "duration_ms": delay.Milliseconds(),
		"tail": slow, "joined_caller_trace": joined, "n": n,
	})

	exportSpan(span, parentSpanID, "GET /price/{id}", started, time.Now(), map[string]any{
		"http.request.method":       "GET",
		"http.route":                "/price/{id}",
		"http.response.status_code": 200,
		"order.id":                  orderID,
		"pricing.tail":              slow,
	})

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("traceparent", fmt.Sprintf("00-%s-%s-01", span.traceID, span.spanID))
	json.NewEncoder(w).Encode(map[string]any{
		"order_id":    orderID,
		"cents":       1000 + orderID%9000,
		"currency":    "USD",
		"duration_ms": delay.Milliseconds(),
		"tail":        slow,
	})
}

func (s *server) handleTail(w http.ResponseWriter, r *http.Request) {
	var body struct {
		On    *bool  `json:"on"`
		Every *int64 `json:"every"`
		MS    *int64 `json:"ms"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	if body.On != nil {
		s.cfg.tailEnabled = *body.On
	}
	if body.Every != nil {
		s.cfg.tailEvery = *body.Every
	}
	if body.MS != nil {
		s.cfg.tailMS = *body.MS
	}
	cfg := s.cfg
	s.mu.Unlock()

	logLine(map[string]any{
		"level": "INFO", "service": "pricing", "msg": "tail reconfigured",
		"enabled": cfg.tailEnabled, "every": cfg.tailEvery, "ms": cfg.tailMS,
	})
	json.NewEncoder(w).Encode(map[string]any{
		"tail": cfg.tailEnabled, "every": cfg.tailEvery, "ms": cfg.tailMS,
	})
}

func (s *server) handleStats(w http.ResponseWriter, _ *http.Request) {
	json.NewEncoder(w).Encode(map[string]any{
		"served":           s.served.Load(),
		"tails":            s.tails.Load(),
		"joined_traces":    s.joined.Load(),
		"orphaned_traces":  s.orphaned.Load(),
		"spans_exported":   otlpSent.Load(),
		"spans_dropped":    otlpDropped.Load(),
		"observed_tail_pc": ratio(s.tails.Load(), s.served.Load()),
	})
}

func ratio(a, b int64) float64 {
	if b == 0 {
		return 0
	}
	return 100 * float64(a) / float64(b)
}

func logLine(fields map[string]any) {
	fields["ts"] = float64(time.Now().UnixNano()) / 1e9
	encoded, err := json.Marshal(fields)
	if err != nil {
		log.Printf("could not encode log line: %v", err)
		return
	}
	fmt.Println(string(encoded))
}

func main() {
	cfg := loadConfig()
	initExporter(cfg)
	s := &server{cfg: cfg}

	mux := http.NewServeMux()
	mux.HandleFunc("/price/", s.handlePrice)
	mux.HandleFunc("/_tail", s.handleTail)
	mux.HandleFunc("/stats", s.handleStats)
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, `{"status":"ok"}`)
	})

	logLine(map[string]any{
		"level": "INFO", "service": "pricing", "msg": "pricing starting",
		"tail_enabled": cfg.tailEnabled, "tail_every": cfg.tailEvery,
		"tail_ms": cfg.tailMS, "fast_ms": cfg.fastMS,
		"break": cfg.breakMode,
	})

	server := &http.Server{
		Addr:              ":8081",
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Fatal(server.ListenAndServe())
}
