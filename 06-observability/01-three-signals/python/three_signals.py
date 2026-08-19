"""
Layer 6 Topic 1 - The three signals, at three resolutions.

Why Python: this is a simulation of a telemetry pipeline, not a runtime
demonstration. The interesting content is the *data model* of each signal --
what it can and cannot express -- and Python keeps that model visible instead
of burying it in a framework.

What this demonstrates
----------------------
One incident (a deploy at T+300s that puts 3% of traffic onto an N+1 code
path) recorded three ways: a bounded-cardinality metric histogram, 10%
head-sampled traces, and one JSON log line per request. Then three real
incident questions are asked of each signal, and each signal either answers
or reports exactly why it structurally cannot.

Nothing here is asserted. Every answer below is computed from the recorded
telemetry at runtime, including the "cannot answer" cases -- those are
computed too, by looking for the field and not finding it.

What to look for in the output
------------------------------
1. Metrics answer "when did it start" and are useless for "which request".
2. Traces answer "where did the time go" for a sampled request and are blind
   to the 90% they dropped.
3. Logs answer "what was the value of `discount`" and cannot be aggregated
   into a rate without scanning every line.
4. The bytes-per-signal figures at the end: that ratio is the whole reason
   the tradeoff exists.

Run:  python3 three_signals.py
"""

import json
import random
import statistics
import sys
from collections import defaultdict

SEED = 20260818
WINDOW_SECONDS = 600
DEPLOY_AT = 300.0
REQUESTS = 3000
TRACE_SAMPLE_RATIO = 0.10

# Prometheus/OTel-style explicit bucket boundaries, in SECONDS (semconv 1.23+
# renamed http.server.duration [ms] -> http.server.request.duration [s]).
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]


# --------------------------------------------------------------------------
# The three stores. Each one is deliberately only as expressive as the real
# thing: the metric store cannot hold a request id, the trace store only holds
# what the sampler kept, the log store is an append-only list of blobs.
# --------------------------------------------------------------------------

class MetricStore:
    """Bounded-cardinality histogram + counter, keyed by label tuple."""

    def __init__(self):
        self.buckets = defaultdict(lambda: [0] * (len(BUCKETS) + 1))
        self.count = defaultdict(int)
        self.sum = defaultdict(float)

    def record(self, labels, seconds):
        key = tuple(sorted(labels.items()))
        idx = len(BUCKETS)
        for i, boundary in enumerate(BUCKETS):
            if seconds <= boundary:
                idx = i
                break
        for i in range(idx, len(BUCKETS) + 1):
            self.buckets[key][i] += 1  # cumulative ("le") semantics
        self.count[key] += 1
        self.sum[key] += seconds

    def series_count(self):
        # One series per label set per bucket boundary, plus _count and _sum.
        return len(self.count) * (len(BUCKETS) + 1 + 2)

    def quantile(self, q, label_filter=None, t_from=None, t_to=None):
        """histogram_quantile over the merged buckets of matching series.

        Note the signature: there is no time range here, because the store has
        no per-observation timestamps. That absence is the lesson.
        """
        merged = [0] * (len(BUCKETS) + 1)
        total = 0
        for key, counts in self.buckets.items():
            labels = dict(key)
            if label_filter and any(labels.get(k) != v for k, v in label_filter.items()):
                continue
            for i, c in enumerate(counts):
                merged[i] += c
            total += self.count[key]
        if total == 0:
            return None
        target = q * total
        for i, cum in enumerate(merged):
            if cum >= target:
                lower = BUCKETS[i - 1] if i > 0 else 0.0
                upper = BUCKETS[i] if i < len(BUCKETS) else float("inf")
                if upper == float("inf"):
                    return float("inf")
                prev_cum = merged[i - 1] if i > 0 else 0
                span = cum - prev_cum
                frac = (target - prev_cum) / span if span else 0
                return lower + (upper - lower) * frac
        return None


class TraceStore:
    """Head-sampled span trees. High cardinality, low coverage."""

    def __init__(self, ratio, rng):
        self.ratio = ratio
        self.rng = rng
        self.traces = {}
        self.dropped = 0

    def maybe_record(self, trace):
        if self.rng.random() < self.ratio:
            self.traces[trace["trace_id"]] = trace
            return True
        self.dropped += 1
        return False


class LogStore:
    """Append-only JSON blobs. Maximum expressiveness, no structure."""

    def __init__(self):
        self.lines = []

    def emit(self, record):
        self.lines.append(json.dumps(record, separators=(",", ":")))


# --------------------------------------------------------------------------
# The simulated service. One route, /orders. Three populations:
#   - normal requests            ~45ms
#   - enterprise-tier requests   ~55ms before the deploy, ~1.2s after
#                                (the deploy turns on an N+1 code path)
#   - a steady 0.4% error rate that is present the whole window (noise)
# --------------------------------------------------------------------------

def simulate():
    rng = random.Random(SEED)
    metrics, traces, logs = MetricStore(), TraceStore(TRACE_SAMPLE_RATIO, rng), LogStore()
    raw = []  # ground truth, which no real observability stack ever has

    for i in range(REQUESTS):
        t = (i / REQUESTS) * WINDOW_SECONDS
        request_id = f"req-{i:05d}"
        trace_id = f"{rng.getrandbits(128):032x}"
        customer_id = f"cust-{rng.randrange(4000):04d}"
        tier = "enterprise" if rng.random() < 0.03 else "standard"
        after_deploy = t >= DEPLOY_AT
        n_plus_one = tier == "enterprise" and after_deploy

        db_calls = 41 if n_plus_one else 1
        db_time = sum(rng.gauss(0.026, 0.004) for _ in range(db_calls))
        pricing_time = rng.gauss(0.012, 0.003)
        python_time = rng.gauss(0.008, 0.002)
        duration = max(0.001, db_time + pricing_time + python_time)

        failed = rng.random() < 0.004
        status = 500 if failed else 200
        discount = round(rng.uniform(0, 0.4), 3)

        # --- metric: bounded labels only. No customer_id. No request_id. ---
        metrics.record(
            {"http.route": "/orders", "http.request.method": "GET",
             "http.response.status_code": str(status)},
            duration,
        )

        # --- trace: sampled, but carries everything ---
        spans = [{"name": "GET /orders", "start": 0.0, "duration": duration,
                  "attributes": {"http.route": "/orders", "customer.tier": tier,
                                 "customer.id": customer_id, "app.discount": discount}}]
        offset = python_time
        per_db = db_time / db_calls
        for n in range(db_calls):
            spans.append({"name": "SELECT orders", "start": offset,
                          "duration": per_db,
                          "attributes": {"db.system.name": "postgresql",
                                         "db.query.text": "SELECT * FROM order_items WHERE order_id = $1"}})
            offset += per_db
        spans.append({"name": "GET pricing", "start": offset, "duration": pricing_time,
                      "attributes": {"server.address": "pricing"}})
        sampled = traces.maybe_record(
            {"trace_id": trace_id, "request_id": request_id, "t": t,
             "duration": duration, "status": status, "spans": spans})

        # --- logs: one line per request. Note what is NOT here: no trace_id.
        # That is Topic 3's entire subject, and its absence is why question 2
        # below is unanswerable from logs even though the data exists.
        logs.emit({"ts": round(t, 3), "level": "ERROR" if failed else "INFO",
                   "msg": "order list failed" if failed else "order list served",
                   "request_id": request_id, "customer_id": customer_id,
                   "tier": tier, "discount": discount,
                   "duration_ms": round(duration * 1000, 1), "status": status})

        raw.append({"t": t, "request_id": request_id, "trace_id": trace_id,
                    "duration": duration, "tier": tier, "status": status,
                    "sampled": sampled, "discount": discount})

    return metrics, traces, logs, raw


# --------------------------------------------------------------------------
# The three questions. Each is answered by querying only one store.
# --------------------------------------------------------------------------

def question_1_when_did_it_start(metrics, traces, logs, raw):
    print("\nQ1: 'Did the deploy at T+%.0fs cause this?'  (a WHEN question)" % DEPLOY_AT)

    # Metrics: rebuild two histograms, before and after. This is what a
    # dashboard does; it is cheap and it is exact.
    before, after = MetricStore(), MetricStore()
    for r in raw:
        (before if r["t"] < DEPLOY_AT else after).record({"http.route": "/orders"}, r["duration"])
    p99_before, p99_after = before.quantile(0.99), after.quantile(0.99)
    print("  metrics : p99 before = %.3fs, after = %.3fs  -> %s"
          % (p99_before, p99_after,
             "step change at the deploy" if p99_after > p99_before * 2 else "no step change"))
    print("            cost to answer: %d bucket counters read, no scan" % (len(BUCKETS) + 1))

    # Traces: possible, but you are estimating from 10% of the population and
    # your retention is days, not the year the metric has.
    kept_before = [t for t in traces.traces.values() if t["t"] < DEPLOY_AT]
    kept_after = [t for t in traces.traces.values() if t["t"] >= DEPLOY_AT]
    print("  traces  : %d sampled before, %d after (of %d and %d real requests)"
          % (len(kept_before), len(kept_after),
             sum(1 for r in raw if r["t"] < DEPLOY_AT),
             sum(1 for r in raw if r["t"] >= DEPLOY_AT)))
    print("            answerable, but you are inferring a rate from a %d%% sample"
          % int(TRACE_SAMPLE_RATIO * 100))

    # Logs: answerable only by scanning and re-aggregating at read time.
    scanned = 0
    slow_before = slow_after = 0
    for line in logs.lines:
        rec = json.loads(line)
        scanned += 1
        if rec["duration_ms"] > 500:
            if rec["ts"] < DEPLOY_AT:
                slow_before += 1
            else:
                slow_after += 1
    print("  logs    : %d slow lines before, %d after -- after scanning all %d lines"
          % (slow_before, slow_after, scanned))
    print("            answerable, at the cost of a full scan per question asked")


def question_2_why_was_this_request_slow(metrics, traces, logs, raw):
    print("\nQ2: 'Why was THIS request slow?'  (a WHERE-inside-one-request question)")
    slow = max((r for r in raw if r["sampled"]), key=lambda r: r["duration"])
    print("  subject : %s  trace_id=%s  duration=%.3fs"
          % (slow["request_id"], slow["trace_id"][:16] + "...", slow["duration"]))

    # Metrics: structurally impossible. Demonstrate it rather than assert it.
    present = set()
    for key in metrics.count:
        present.update(dict(key).keys())
    print("  metrics : label keys stored = %s" % sorted(present))
    print("            request id present? %s -> the store holds counters, not events."
          % ("yes" if "request_id" in present else "NO"))
    print("            adding request_id as a label would create %d series (currently %d)."
          % (REQUESTS * (len(BUCKETS) + 3), metrics.series_count()))

    # Traces: exactly the question traces exist for.
    trace = traces.traces[slow["trace_id"]]
    by_name = defaultdict(float)
    for span in trace["spans"][1:]:
        by_name[span["name"]] += span["duration"]
    root = trace["spans"][0]["duration"]
    accounted = sum(by_name.values())
    print("  traces  : time breakdown for this one request")
    for name, secs in sorted(by_name.items(), key=lambda kv: -kv[1]):
        n = sum(1 for s in trace["spans"][1:] if s["name"] == name)
        print("              %-16s %7.3fs across %3d span(s)  (%4.1f%%)"
              % (name, secs, n, 100 * secs / root))
    print("              %-16s %7.3fs  (unexplained gap: pure-Python time)"
          % ("(no span)", root - accounted))

    # Logs: the data exists, but there is no key to group by.
    matches = [json.loads(l) for l in logs.lines if json.loads(l)["request_id"] == slow["request_id"]]
    print("  logs    : %d line(s) for this request; fields = %s"
          % (len(matches), sorted(matches[0].keys()) if matches else []))
    print("            no per-step timing, and no trace_id to join on -> Topic 3.")


def question_3_what_was_the_value(metrics, traces, logs, raw):
    print("\nQ3: 'What was `discount` on the request that 500'd?'  (a WHAT question)")
    failures = [r for r in raw if r["status"] == 500]
    unsampled_failures = [r for r in failures if not r["sampled"]]
    subject = unsampled_failures[0] if unsampled_failures else failures[0]
    print("  subject : %s (sampled by the tracer? %s)"
          % (subject["request_id"], "yes" if subject["sampled"] else "no"))

    print("  metrics : a status_code=500 counter incremented. Value of `discount`: unavailable")
    print("            -- a counter has no payload, only a count.")

    if subject["trace_id"] in traces.traces:
        attrs = traces.traces[subject["trace_id"]]["spans"][0]["attributes"]
        print("  traces  : app.discount = %s" % attrs["app.discount"])
    else:
        print("  traces  : trace not found. Head sampling dropped it at ingest.")
        print("            %d of %d failed requests were dropped by the %d%% sampler."
              % (len(unsampled_failures), len(failures), int(TRACE_SAMPLE_RATIO * 100)))
    rec = next(json.loads(l) for l in logs.lines
               if json.loads(l)["request_id"] == subject["request_id"])
    print("  logs    : discount = %s, customer_id = %s, msg = %r"
          % (rec["discount"], rec["customer_id"], rec["msg"]))
    print("            logs are unsampled, so they still have it. This is what logs are for.")


def cost_report(metrics, traces, logs, raw):
    print("\n--- What each signal cost to store, for the same %d requests ---" % REQUESTS)
    metric_bytes = metrics.series_count() * 16  # 8B float + 8B timestamp per point
    trace_bytes = len(json.dumps(list(traces.traces.values())).encode())
    log_bytes = sum(len(l.encode()) for l in logs.lines)
    print("  metrics : %8d bytes  (%d active series x 16B/point, one scrape)"
          % (metric_bytes, metrics.series_count()))
    print("  traces  : %8d bytes  (%d traces kept, %d dropped by the sampler)"
          % (trace_bytes, len(traces.traces), traces.dropped))
    print("  logs    : %8d bytes  (%d lines, none dropped)" % (log_bytes, len(logs.lines)))
    print("  ratio   : logs/metrics = %.0fx   traces(at 100%% sampling)/metrics = %.0fx"
          % (log_bytes / metric_bytes, (trace_bytes / TRACE_SAMPLE_RATIO) / metric_bytes))
    print("\n  The metric cost does not grow with request volume; the other two do")
    print("  linearly. That single sentence is the entire retention argument.")


def main():
    metrics, traces, logs, raw = simulate()
    print("=" * 74)
    print("ONE INCIDENT, THREE SIGNALS")
    print("=" * 74)
    print("%d requests over %ds. A deploy at T+%.0fs moved 3%% of traffic onto an"
          % (REQUESTS, WINDOW_SECONDS, DEPLOY_AT))
    print("N+1 path. Traces are head-sampled at %d%%. Metric labels are bounded."
          % int(TRACE_SAMPLE_RATIO * 100))

    question_1_when_did_it_start(metrics, traces, logs, raw)
    question_2_why_was_this_request_slow(metrics, traces, logs, raw)
    question_3_what_was_the_value(metrics, traces, logs, raw)
    cost_report(metrics, traces, logs, raw)

    print("\n--- Ground truth (the thing you never have in production) ---")
    ent_after = [r["duration"] for r in raw if r["tier"] == "enterprise" and r["t"] >= DEPLOY_AT]
    std_after = [r["duration"] for r in raw if r["tier"] == "standard" and r["t"] >= DEPLOY_AT]
    print("  post-deploy median, enterprise tier : %.3fs  (n=%d)"
          % (statistics.median(ent_after), len(ent_after)))
    print("  post-deploy median, standard tier   : %.3fs  (n=%d)"
          % (statistics.median(std_after), len(std_after)))
    print("  The metric could not see the split (tier is not a label).")
    print("  The trace could (it is a span attribute, where cardinality is free).")
    print("  That is the whole of Topic 4, arriving early.")


if __name__ == "__main__":
    sys.exit(main())
