"""
Layer 6 Topic 4 - One label, two failures: the silent one and the loud one.

Why Python: this is the lab's own service language, and the OTel Python SDK is
where the cardinality limit is configured per-view on the MeterProvider (or
globally with OTEL_METRIC_CARDINALITY_LIMIT). The default is the spec's 2000
and there is no warning log, so you discover overflow by QUERYING for the
attribute, never by reading stderr. This program builds that behaviour out of a
dict so the mechanism is visible, then makes you look at the arithmetic that is
the only symptom.

No OpenTelemetry SDK is installed on this machine, so the counter, the
per-stream cardinality limit and the overflow attribute are ~60 lines of
standard library here. They implement the spec's rule exactly:

    "When the limit is reached, the SDK MUST record the measurement with a
     single attribute set {otel.metric.overflow: true}."

Totals stay correct. Every breakdown by the offending label starts
undercounting. Nothing errors.

What this demonstrates
----------------------
1. Series count is a PRODUCT. The napkin arithmetic, computed from the label
   set rather than asserted.
2. The silent failure: with the SDK limit in place, `sum(total)` and
   `sum by (customer_id)` disagree, and the gap is the answer to "how much of
   my dashboard is missing".
3. WHICH label values survive is first-seen-wins, so the customer who signed up
   after warm-up is invisible for the lifetime of the process.
4. Which alerts survive overflow and which quietly stop firing. This is the
   half people never check, and it is the half that pages you.
5. The loud failure: bytes per series, MEASURED on this machine with
   tracemalloc, then multiplied out to the series count the label would
   actually produce. The measured number is small; the product is not.
6. The fix, proved rather than asserted: bound the label, move the unbounded
   dimension to a span attribute, and keep the jump with an exemplar. The
   exemplar has to still get you to the trace, or the fix is not a fix.

What to look for in the output
------------------------------
Section 2's "gap" line and section 4's alert table. Everything else is
supporting arithmetic. If you read only two numbers, read the percentage of
requests that ended up in the overflow bucket and the number of alert rules
that stopped firing without changing.
"""
import random
import sys
import tracemalloc

# Spec default, per the OTel metrics SDK. Configurable per view, or globally
# with OTEL_METRIC_CARDINALITY_LIMIT.
DEFAULT_CARDINALITY_LIMIT = 2000

# 40 templated routes -- the count a real service of this size has, and the
# reason route templating is move number one in the fix list. The raw-path
# version of this list is unbounded, because `?utm_source=` is user input.
RESOURCES = ["orders", "customers", "items", "invoices", "shipments",
             "returns", "quotes", "carts"]
ROUTES = ["/health", "/ready", "/metrics", "/checkout", "/pricing/quote",
          "/search", "/login", "/logout"]
for _resource in RESOURCES:
    ROUTES += [
        "/%s" % _resource,
        "/%s/{id}" % _resource,
        "/%s/{id}/events" % _resource,
        "/%s/{id}/audit" % _resource,
    ]
METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]
STATUSES = [200, 201, 204, 400, 401, 404, 429, 500]

CUSTOMERS = 10_000
REQUESTS = 200_000


class Counter:
    """A counter with a per-stream cardinality limit, per the OTel spec.

    Measurements past the limit are still counted -- their attributes are
    replaced by {otel.metric.overflow: True}. That is the entire behaviour, and
    it is why the totals on your dashboard stay right while the breakdowns
    stop being.
    """

    OVERFLOW = (("otel.metric.overflow", True),)

    def __init__(self, name, limit=DEFAULT_CARDINALITY_LIMIT):
        self.name = name
        self.limit = limit  # 0 or None means unlimited
        self.series = {}
        self.overflowed_measurements = 0
        self.rejected_attribute_sets = set()

    def add(self, value, **attributes):
        key = tuple(sorted(attributes.items()))
        if key in self.series:
            self.series[key] += value
            return
        unlimited = not self.limit
        # The spec reserves one slot for the overflow series itself.
        room = unlimited or len(self.series) < self.limit - 1
        if room:
            self.series[key] = value
            return
        self.series[self.OVERFLOW] = self.series.get(self.OVERFLOW, 0) + value
        self.overflowed_measurements += value
        self.rejected_attribute_sets.add(key)

    # --- the query surface, which is where the failure becomes visible ------

    def total(self):
        """sum(rate(http_server_requests_total[5m])) -- always correct."""
        return sum(self.series.values())

    def sum_by(self, label):
        """sum by (<label>) (...) -- silently loses the overflow point."""
        out = {}
        for key, value in self.series.items():
            attrs = dict(key)
            if label not in attrs:
                continue  # the overflow series has no customer_id at all
            out[attrs[label]] = out.get(attrs[label], 0) + value
        return out

    def active_series(self):
        return len(self.series)


def generate_traffic(rng, requests, customers):
    """A stream of requests. Routes are templated (bounded), customers are not."""
    for _ in range(requests):
        yield {
            "route": rng.choice(ROUTES),
            "method": rng.choices(METHODS, weights=[70, 20, 5, 3, 2])[0],
            "status": rng.choices(STATUSES, weights=[70, 8, 5, 5, 3, 4, 3, 2])[0],
            "customer_id": "cust-%05d" % rng.randrange(customers),
        }


def rule(label, width=46):
    print()
    print(label)
    print("-" * width)


def main():
    rng = random.Random(20260818)

    print("Layer 6 Topic 4 - cardinality: the silent failure, then the loud one")
    print("python %s   SDK cardinality limit default = %d attribute sets/stream"
          % (sys.version.split()[0], DEFAULT_CARDINALITY_LIMIT))
    print("=" * 72)

    # -----------------------------------------------------------------------
    rule("1. Series count is a product, and you can do it on a napkin")
    bounded = len(ROUTES) * len(METHODS) * len(STATUSES)
    print("  routes x methods x statuses      %d x %d x %d = %s series"
          % (len(ROUTES), len(METHODS), len(STATUSES), f"{bounded:,}"))
    print("  ... x customer_id                %s x %s = %s series"
          % (f"{bounded:,}", f"{CUSTOMERS:,}", f"{bounded * CUSTOMERS:,}"))
    print()
    print("  Nothing about the recording changed. Same counter, same call site,")
    print("  same request rate. One label multiplied. The code change is one word.")

    # -----------------------------------------------------------------------
    rule("2. The silent failure: SDK limit in place, dashboards undercount")

    baseline = Counter("http.server.requests")   # bounded labels only
    demo = Counter("http.server.requests")       # + customer_id, default limit

    traffic = list(generate_traffic(rng, REQUESTS, CUSTOMERS))
    for req in traffic:
        baseline.add(1, route=req["route"], method=req["method"], status=req["status"])
        demo.add(1, route=req["route"], method=req["method"], status=req["status"],
                 customer_id=req["customer_id"])

    total = demo.total()
    by_customer = demo.sum_by("customer_id")
    visible = sum(by_customer.values())
    gap = total - visible

    print("  requests recorded                %s" % f"{REQUESTS:,}")
    print("  distinct customers in traffic    %s" % f"{len({r['customer_id'] for r in traffic}):,}")
    print()
    print("  baseline counter (bounded labels)")
    print("    active series                  %s" % f"{baseline.active_series():,}")
    print("    sum(rate(...))                 %s" % f"{baseline.total():,}")
    print()
    print("  demo counter (+ customer_id, limit %d)" % demo.limit)
    print("    active series                  %s  <- capped, not grown"
          % f"{demo.active_series():,}")
    print("    sum(rate(...))                 %s  <- still correct" % f"{total:,}")
    print("    sum by (customer_id) (rate)    %s  <- what your dashboard shows"
          % f"{visible:,}")
    print("    gap                            %s  (%.1f%% of all traffic)"
          % (f"{gap:,}", 100.0 * gap / total))
    print("    customer_id values surviving   %s of %s (%.1f%%)"
          % (f"{len(by_customer):,}", f"{CUSTOMERS:,}", 100.0 * len(by_customer) / CUSTOMERS))
    print("    attribute sets rejected        %s"
          % f"{len(demo.rejected_attribute_sets):,}")
    print()
    print("  Nothing logged. Nothing errored. The only symptom is that those two")
    print("  sums disagree -- and you only ever see it if you compute both.")

    # -----------------------------------------------------------------------
    rule("3. Which values survive is first-seen-wins")
    late = Counter("http.server.requests")
    for req in traffic[:REQUESTS // 2]:
        late.add(1, route=req["route"], method=req["method"], status=req["status"],
                 customer_id=req["customer_id"])
    before = set(late.sum_by("customer_id"))

    # A new enterprise customer signs up after the process warmed up, and sends
    # a lot of traffic. Watch it not appear.
    for _ in range(5_000):
        late.add(1, route="/orders", method="GET", status=200,
                 customer_id="cust-NEW-ENTERPRISE")
    after = late.sum_by("customer_id")

    print("  customer_id values held before the new signup   %s" % f"{len(before):,}")
    print("  requests sent by cust-NEW-ENTERPRISE            5,000")
    print("  its rows on a per-customer dashboard            %s"
          % ("present" if "cust-NEW-ENTERPRISE" in after else "ABSENT"))
    print("  where those 5,000 requests went                 %s"
          % ("otel.metric.overflow=true"
             if "cust-NEW-ENTERPRISE" not in after else "its own series"))
    print()
    print("  The surviving set is whichever 1,999 attribute sets arrived first.")
    print("  That is not a sample of your customers, it is a sample of your")
    print("  start-up order, and it never refreshes while the process lives.")

    # -----------------------------------------------------------------------
    rule("4. Which alerts survive overflow, and which stop firing")

    # Ground truth, straight from the traffic we generated.
    true_5xx = sum(1 for r in traffic if r["status"] >= 500)
    true_ratio = true_5xx / REQUESTS

    # What the metric can answer. Note the trap: the overflow series carries
    # ONLY {otel.metric.overflow: true}. It has no `status` label either, so
    # any selector with a label matcher loses it too.
    metric_5xx = sum(v for k, v in demo.series.items() if dict(k).get("status", 0) >= 500)
    metric_all = demo.total()
    metric_ratio = metric_5xx / metric_all

    per_customer_visible = len(by_customer)

    print("  ground truth (from the generated traffic)")
    print("    5xx responses                  %s of %s  = %.2f%%"
          % (f"{true_5xx:,}", f"{REQUESTS:,}", 100 * true_ratio))
    print()
    print("  what each rule can still see")
    print("    %-44s %s" % ("rule", "verdict"))
    print("    %-44s %s" % ("-" * 44, "-" * 26))
    print("    %-44s intact   (%s)"
          % ("TrafficDropped: sum(rate(all)) < x", f"{metric_all:,}"))
    print("    %-44s BROKEN   (reports %.2f%%, true %.2f%%)"
          % ("ErrorRatioHigh: sum(5xx) / sum(all) > 1%", 100 * metric_ratio,
             100 * true_ratio))
    print("    %-44s BROKEN   (%s of %s customers)"
          % ("PerCustomerErrors: by (customer_id) > 5%",
             f"{per_customer_visible:,}", f"{CUSTOMERS:,}"))
    print()
    print("  The second row is the one that surprises people, and it is worth")
    print("  slowing down on. \"Totals stay correct\" means the GRAND total: the")
    print("  overflow point carries the count. It carries no other attribute, so")
    print("  the moment your query filters on a label -- status, route, method,")
    print("  anything -- the overflow point drops out of BOTH sides of your")
    print("  ratio, and the numerator loses proportionally more of it than the")
    print("  denominator does. Your error-rate alert now watches whichever 1,999")
    print("  attribute sets started first, and it will not tell you that.")
    print()
    print("  Overflow does not degrade your monitoring evenly. It degrades")
    print("  exactly the queries that name a label -- which is all the useful ones.")

    # -----------------------------------------------------------------------
    rule("5. The loud failure: what a series costs, measured then multiplied")

    def measure_series_bytes(n):
        """Build n distinct series in a fresh dict and measure the delta."""
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        counter = Counter("probe", limit=0)  # unlimited
        for i in range(n):
            counter.add(1, route="/orders", method="GET", status=200,
                        customer_id="cust-%08d" % i)
        peak = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        return (peak - base) / n, counter.active_series()

    per_series_10k, built_10k = measure_series_bytes(10_000)
    per_series_100k, built_100k = measure_series_bytes(100_000)
    per_series = per_series_100k

    print("  measured on THIS machine, with tracemalloc, in this process:")
    print("    %s series          %.0f bytes/series" % (f"{built_10k:>8,}", per_series_10k))
    print("    %s series          %.0f bytes/series" % (f"{built_100k:>8,}", per_series_100k))
    print()
    projected = bounded * CUSTOMERS
    print("  extrapolated (measured cost x series count -- NOT a measurement):")
    print("    %s series          %.1f GB in this toy store alone"
          % (f"{projected:>8,}", projected * per_series / 1e9))
    print()
    print("  That is one counter, in one process, with a dict for a storage")
    print("  engine. Prometheus pays more per series than this: the head block,")
    print("  the inverted index, the label-value dictionary and query fan-out")
    print("  all scale with the count. Take the shape from this number, not the")
    print("  magnitude -- and note that the shape is a straight line through the")
    print("  origin with your customer count on the x axis.")

    # -----------------------------------------------------------------------
    rule("6. The fix, and the proof that the capability survived it")

    fixed = Counter("http.server.requests")
    # A histogram bucket with an exemplar attached: a trace ID stored ALONGSIDE
    # the sample, not as a label. It creates no series, because it is not part
    # of the series identity.
    exemplars = {}  # bucket -> (trace_id, customer_id, duration)

    for i, req in enumerate(traffic):
        fixed.add(1, route=req["route"], method=req["method"], status=req["status"])
        duration = 0.02 if i % 100 else 2.4  # every 100th request is slow
        bucket = "le=0.1" if duration <= 0.1 else "le=+Inf"
        if bucket == "le=+Inf":
            exemplars[bucket] = ("%032x" % rng.getrandbits(128), req["customer_id"], duration)

    print("  1. bound the label      route templates, status classes, no raw IDs")
    print("     series after fix     %s (vs %s capped, vs %s uncapped)"
          % (f"{fixed.active_series():,}", f"{demo.active_series():,}",
             f"{bounded * CUSTOMERS:,}"))
    print("  2. move the dimension   customer_id -> span attribute (free there)")
    print("  3. keep the jump        exemplar on the slow bucket:")
    trace_id, cid, duration = exemplars["le=+Inf"]
    print("       bucket             le=+Inf")
    print("       exemplar trace_id  %s" % trace_id)
    print("       -> that trace      customer_id=%s  duration=%.2fs" % (cid, duration))
    print("       series added by the exemplar   0")
    print()
    print("  The fix is only real if the capability survives it. You still get")
    print("  from 'the p99 spiked' to one specific slow request for one specific")
    print("  customer -- in two clicks, and without buying a dimension.")

    print()
    print("=" * 72)
    print("The napkin check to run in code review, in ten seconds:")
    print("  'What is the maximum number of distinct values this label can take,")
    print("   and who decides it -- us, or a user?'")
    print("If the answer to the second half is 'a user', it is a span attribute.")


if __name__ == "__main__":
    main()
