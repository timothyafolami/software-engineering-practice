// Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one JVM.
//
// You cannot serve more than capacity. The only choice you have is whether
// the excess is rejected in one millisecond or times out after thirty seconds
// having consumed a connection, a thread and a query. This file runs the same
// ramp seven times and changes only the admission decision.
//
// JAVA HAS THE MATURE TOOLKIT and one genuinely important twist.
//
// The toolkit: Resilience4j's `Bulkhead` (semaphore) and `ThreadPoolBulkhead`,
// and -- with no dependencies at all -- a `ThreadPoolExecutor` with a bounded
// `ArrayBlockingQueue` plus an explicit `RejectedExecutionHandler`, which is
// load shedding spelled out in the standard library:
//
//     new ThreadPoolExecutor(8, 8, 0L, MILLISECONDS,
//                            new ArrayBlockingQueue<>(4),
//                            new ThreadPoolExecutor.AbortPolicy());
//
// Eight servers, four queue slots, and a documented exception when both are
// full. That is modes 1-3 of this file expressed as a pool rather than as a
// semaphore, and the reason this file uses a `Semaphore` instead is that a
// semaphore lets a request WAIT a bounded time before being refused, which
// `AbortPolicy` cannot express.
//
// THE TWIST, and it is the one to carry away: switching to
// `Executors.newVirtualThreadPerTaskExecutor()` removes the thread pool that
// was ACCIDENTALLY your admission controller. A service that used to reject
// when its 200 threads were busy now accepts everything and queues it against
// the database instead. Nothing in the diff says "removed load shedding"; the
// diff says "modernised the executor". This file runs on virtual threads
// throughout, so mode `none` is exactly that service -- and modes 3 to 5 are
// what you have to write once nobody is limiting you by accident.
//
// Virtual threads make you write the limit you were getting for free. That is
// the same lesson Go teaches, arrived at from the opposite direction.
//
// WHAT THIS DEMONSTRATES
//
//   A backend with 8 concurrent servers at 40ms each -- 200 requests/second
//   of capacity, measured the way topic 1 measures it -- behind six different
//   admission policies, at 80% and 130% of that capacity.
//
//     none rho=0.8      the healthy baseline. Everything looks fine.
//     none rho=1.3      an UNBOUNDED queue of virtual threads parked on a
//                       semaphore. Nothing rejects. p99 leaves the building.
//     static rho=1.3    Semaphore.tryAcquire(50ms) -> 503 Retry-After.
//     priority rho=1.3  the same limit, but /checkout (tier 0) may use all
//                       of it and /search (tier 3) may not.
//     adaptive rho=1.3  no configured number at all: a gradient controller
//                       infers the limit from latency. Service time triples
//                       half way through, on purpose.
//     bulkhead          one pool of 8 shared between checkout and a slow
//                       /report endpoint, then the SAME EIGHT split 6 + 2.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `p99_acc` and `goodput` in `none rho=1.3` against `static rho=1.3`.
//      Rejecting work should INCREASE the number of requests answered in
//      time. Check that rather than believe it.
//   2. `vthreads` in scenario 2: the requests nobody refused.
//   3. `tier0%` in the priority row.
//   4. `limit` in the adaptive row, before and after service time triples at
//      t=6s. Reason about Little's law before calling the controller broken:
//      the ideal in-flight limit for 8 servers is about 8 however long each
//      request takes. What must fall is the RATE, not the limit.
//   5. `reject_ms`, the cost of saying no. Note that tryAcquire WITH a
//      timeout makes a rejection cost the whole timeout -- which is a real
//      trade, not a bug, and is why the wait deadline is 50ms and not 500.
//
// RUN
//   javac Shedder.java -Xlint:all -d /tmp/javabuild && java -cp /tmp/javabuild Shedder
//
// Roughly two and a half minutes: seven scenarios of twenty seconds.

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

public final class Shedder {

    // ------------------------------------------------------------ config
    //
    // Identical to python/shedder.py's constants: the six languages differ in
    // how admission is expressed, not in what is being measured.

    static final int WORKERS = 8;            // the real resource
    static final long SERVICE_MS = 40;       // 8 / 0.040 = 200 rps
    static final double CAPACITY = WORKERS / (SERVICE_MS / 1000.0);

    static final double RHO_LOW = 0.8;
    static final double RHO_HIGH = 1.3;

    static final long SLO_MS = 500;          // later than this is not goodput
    // PERTURB_AT_S + MIN_RTT_RESET_S + room to watch the adaptive limit come
    // back. At 12s the run ended during the dip and the return -- the half
    // that shows the reset working -- was invisible.
    static final double DURATION_S = 20.0;
    static final double REPORT_EVERY = 2.0;

    static final int SHED_LIMIT = 12;        // the knee's concurrency, measured
    static final long SHED_WAIT_MS = 50;     // queue-wait deadline before a 503
    static final long TIER3_LIMIT = 10;      // tier 3 may not use the last two
    static final double TIER0_SHARE = 0.20;

    static final double ADAPT_MIN = 2.0;
    static final double ADAPT_MAX = 64.0;
    static final double ADAPT_START = 10.0;
    static final long ADAPT_WINDOW_MS = 250;
    static final double ADAPT_SMOOTHING = 0.2;
    static final long MIN_RTT_RESET_MS = 5000;
    static final double PERTURB_AT = 6.0;
    static final int PERTURB_FACTOR = 3;

    static final double CHECKOUT_RPS = 120.0;
    static final double REPORT_RPS = 6.0;
    static final long REPORT_SERVICE_MS = 800;  // 6 rps x 0.8s = 4.8 servers
    static final int BULK_CHECKOUT = 6;         // the same 8, split
    static final int BULK_REPORT = 2;

    // -------------------------------------------------------- the backend

    /**
     * The resource being protected. A `Semaphore` is a real bounded resource
     * and blocking on it is correct backpressure -- but nothing bounds the
     * number of BLOCKED THREADS, and on virtual threads that number can reach
     * six figures without anything complaining. That is mode `none`.
     */
    static final class Backend {
        private final Semaphore sem;
        private final AtomicLong inUse = new AtomicLong();

        Backend(int workers) {
            this.sem = new Semaphore(workers);
        }

        void call(long serviceMs) throws InterruptedException {
            sem.acquire();
            inUse.incrementAndGet();
            try {
                Thread.sleep(serviceMs);
            } finally {
                inUse.decrementAndGet();
                sem.release();
            }
        }

        long inUse() {
            return inUse.get();
        }
    }

    // ------------------------------------------------- the gradient limit

    /**
     * Netflix `concurrency-limits` in miniature, borrowed from TCP congestion
     * control rather than from queueing theory: sample latency continuously,
     * remember the minimum you have seen, raise the in-flight limit while
     * current latency stays near that minimum, lower it when latency climbs.
     * You never configure a number; the system discovers it, and rediscovers
     * it when your code changes -- which matters because the hand-measured
     * number from topic 1 goes stale the day someone adds a join.
     *
     * The non-obvious parameter is the min-RTT RESET. Without it one fast
     * sample from a quiet moment is remembered forever, so after a genuine
     * permanent slowdown the gradient sticks near zero and the limit collapses
     * to the floor and stays there. Vegas-style controllers all re-baseline.
     */
    static final class GradientLimit {
        private double limit = ADAPT_START;
        private double minRtt = Double.MAX_VALUE;
        private final List<Double> samples = new ArrayList<>();
        private long lastUpdate;
        private long lastReset;

        synchronized double limit() {
            return limit;
        }

        synchronized void observe(double rttMs) {
            samples.add(rttMs);
        }

        synchronized void update(long nowMs) {
            if (lastUpdate != 0 && nowMs - lastUpdate < ADAPT_WINDOW_MS) {
                return;
            }
            lastUpdate = nowMs;
            if (samples.isEmpty()) {
                return;
            }
            Collections.sort(samples);
            double windowMin = samples.get(0);
            double median = samples.get(samples.size() / 2);
            samples.clear();

            if (lastReset == 0 || nowMs - lastReset >= MIN_RTT_RESET_MS) {
                minRtt = windowMin;
                lastReset = nowMs;
            } else {
                minRtt = Math.min(minRtt, windowMin);
            }
            // gradient < 1 means "we are queueing"; the limit comes down in
            // proportion. The sqrt term is the queue you are willing to keep,
            // and is what stops the limit collapsing to 1 the moment one
            // request is slow.
            double gradient = Math.max(0.5, Math.min(1.0, minRtt / Math.max(median, 1e-6)));
            double target = limit * gradient + Math.sqrt(limit);
            limit = Math.max(ADAPT_MIN, Math.min(ADAPT_MAX,
                    limit * (1 - ADAPT_SMOOTHING) + ADAPT_SMOOTHING * target));
        }
    }

    // ------------------------------------------------------ the admission

    /**
     * The fifty lines. Everything above the backend and below the router.
     *
     * The interesting part is what happens when you cannot have a permit
     * immediately, and Java spells all three answers with the same method:
     * `tryAcquire()` refuses now, `tryAcquire(t, unit)` waits a BOUNDED time,
     * and `acquire()` waits forever -- which is mode `none`, and which is what
     * you ship when you do not decide.
     */
    static final class Admission {
        final String mode;
        private final Semaphore sem = new Semaphore(SHED_LIMIT);
        private final AtomicLong inflight = new AtomicLong();
        final GradientLimit limiter;

        Admission(String mode) {
            this.mode = mode;
            this.limiter = mode.equals("adaptive") ? new GradientLimit() : null;
        }

        long inflight() {
            return inflight.get();
        }

        double limit() {
            return limiter != null ? limiter.limit() : SHED_LIMIT;
        }

        boolean usesPermit(int tier) {
            return !mode.equals("none") && !mode.equals("adaptive");
        }

        /**
         * Returns the cost of the decision in milliseconds if the request was
         * REJECTED, or -1 if it was admitted. The cost belongs on a dashboard:
         * a shedder that takes 50ms to say no has spent 10% of a 500ms budget
         * on nothing.
         */
        double admit(int tier) throws InterruptedException {
            long t0 = System.nanoTime();
            if (mode.equals("none")) {
                // No admission control at all. Every request is accepted and
                // waits for the backend for as long as that takes, and the
                // queue has no bound because nobody gave it one.
                inflight.incrementAndGet();
                return -1;
            }
            if (mode.equals("adaptive")) {
                // Limit-based, no queueing: the controller's whole job is to
                // hold the limit where waiting is unnecessary.
                if (inflight.get() >= limiter.limit()) {
                    return (System.nanoTime() - t0) / 1e6;
                }
                inflight.incrementAndGet();
                return -1;
            }
            if (mode.equals("priority") && tier > 0) {
                // Tier 3 is shed against a LOWER limit -- the last two permits
                // are reserved for tier 0 -- and gets the no-argument
                // tryAcquire, which does not queue at all.
                if (inflight.get() >= TIER3_LIMIT || !sem.tryAcquire()) {
                    return (System.nanoTime() - t0) / 1e6;
                }
                inflight.incrementAndGet();
                return -1;
            }
            // static, and priority's tier 0: a BOUNDED wait, which is the one
            // shape a ThreadPoolExecutor's RejectedExecutionHandler cannot
            // give you.
            if (!sem.tryAcquire(SHED_WAIT_MS, TimeUnit.MILLISECONDS)) {
                return (System.nanoTime() - t0) / 1e6;
            }
            inflight.incrementAndGet();
            return -1;
        }

        void release(boolean usedPermit) {
            inflight.decrementAndGet();
            if (usedPermit) {
                sem.release();
            }
        }
    }

    // -------------------------------------------------------- the metrics

    static final class Metrics {
        long offered;
        long accepted;
        long rejected;
        long goodput;
        long tier0Offered;
        long tier0Goodput;
        final List<Double> latencies = new ArrayList<>();
        final List<Double> latTier0 = new ArrayList<>();
        final List<Double> rejectCost = new ArrayList<>();
        long wOffered;
        long wAccepted;
        long wRejected;
        long wGoodput;
        final List<Double> wLat = new ArrayList<>();
        final List<Row> rows = new ArrayList<>();
    }

    record Row(double t, double offered, double accepted, double reject, double goodput,
               double p99, long inflight, double limit, long busy, long vthreads) { }

    static double percentile(List<Double> values, double q) {
        if (values.isEmpty()) {
            return 0;
        }
        List<Double> ordered = new ArrayList<>(values);
        Collections.sort(ordered);
        int idx = Math.min(ordered.size() - 1,
                Math.max(0, (int) Math.ceil(q * ordered.size()) - 1));
        return ordered.get(idx);
    }

    // --------------------------------------------------------- the server

    static final class Server {
        final String mode;
        final Metrics m;
        final Admission admission;
        final Backend checkoutBackend;
        final Backend reportBackend;
        volatile long serviceMs = SERVICE_MS;
        final AtomicLong vthreads = new AtomicLong();

        Server(String mode, Metrics m) {
            this.mode = mode;
            this.m = m;
            this.admission = new Admission(mode.startsWith("bulkhead") ? "none" : mode);
            this.checkoutBackend = new Backend(
                    mode.equals("bulkhead_split") ? BULK_CHECKOUT : WORKERS);
            // The bulkhead: /report gets its own, smaller pool and is
            // structurally incapable of touching checkout's servers.
            this.reportBackend = mode.equals("bulkhead_split")
                    ? new Backend(BULK_REPORT)
                    : this.checkoutBackend;
        }

        void handle(int tier, boolean isReport) {
            long t0 = System.nanoTime();
            synchronized (m) {
                m.offered++;
                m.wOffered++;
                if (tier == 0) {
                    m.tier0Offered++;
                }
            }
            boolean usedPermit = admission.usesPermit(tier);
            double cost;
            try {
                cost = admission.admit(tier);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
            if (cost >= 0) {
                synchronized (m) {
                    m.rejected++;
                    m.wRejected++;
                    m.rejectCost.add(cost);
                }
                // A 503 with Retry-After, having touched nothing. That is the
                // entire product.
                return;
            }
            synchronized (m) {
                m.accepted++;
                m.wAccepted++;
            }
            try {
                Backend backend = isReport ? reportBackend : checkoutBackend;
                backend.call(isReport ? REPORT_SERVICE_MS : serviceMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            } finally {
                admission.release(usedPermit);
            }
            double latency = (System.nanoTime() - t0) / 1e6;
            if (admission.limiter != null) {
                admission.limiter.observe(latency);
            }
            synchronized (m) {
                m.latencies.add(latency);
                m.wLat.add(latency);
                if (tier == 0) {
                    m.latTier0.add(latency);
                }
                if (latency <= SLO_MS) {
                    m.goodput++;
                    m.wGoodput++;
                    if (tier == 0) {
                        m.tier0Goodput++;
                    }
                }
            }
        }
    }

    // -------------------------------------------------------- the harness

    record Scenario(String key, String mode, String label, String note, double rate,
                    double tier0Share, double reportRps) { }

    static Metrics runScenario(Scenario sc) throws Exception {
        Metrics m = new Metrics();
        Server server = new Server(sc.mode(), m);
        Random rng = new Random(20250505);
        // Every arrival gets a thread, and nothing here refuses to make one.
        // On a fixed platform pool this executor WAS your admission control.
        ExecutorService exec = Executors.newVirtualThreadPerTaskExecutor();

        long begin = System.nanoTime();
        long lastReport = begin;
        long at = begin;
        long nextReport = begin;
        boolean perturbed = false;

        while (true) {
            if ((at - begin) / 1e9 > DURATION_S) {
                break;
            }
            at += (long) (-Math.log(1 - rng.nextDouble()) / sc.rate() * 1e9);
            long sleepNanos = at - System.nanoTime();
            if (sleepNanos > 0) {
                Thread.sleep(sleepNanos / 1_000_000L, (int) (sleepNanos % 1_000_000L));
            }
            long now = System.nanoTime();
            double t = (now - begin) / 1e9;

            if (sc.mode().equals("adaptive") && !perturbed && t >= PERTURB_AT) {
                // "Then change service time by 3x at runtime and watch it
                // re-converge." Nobody redeployed. Nobody changed the limit.
                server.serviceMs = SERVICE_MS * PERTURB_FACTOR;
                perturbed = true;
            }

            int tier = rng.nextDouble() < sc.tier0Share() ? 0 : 3;
            server.vthreads.incrementAndGet();
            exec.submit(() -> {
                try {
                    server.handle(tier, false);
                } finally {
                    server.vthreads.decrementAndGet();
                }
            });

            // The slow endpoint, offered as its own open-model stream rather
            // than as a fraction of checkout: reports do not arrive because
            // checkouts do.
            // Note `+=` and the `while`, not `= now +` and an `if`: this is an
            // ABSOLUTE schedule, exactly like `at` above. Rescheduling from
            // `now` throws away the lateness of every arrival, and since the
            // check only runs when a checkout arrives, the lateness is real
            // and it grows with load -- so the relative version quietly offers
            // LESS /report the more overloaded the server gets, which is
            // backwards and hides the very effect this scenario exists to
            // show.
            while (sc.reportRps() > 0 && now >= nextReport) {
                nextReport += (long) (-Math.log(1 - rng.nextDouble()) / sc.reportRps() * 1e9);
                server.vthreads.incrementAndGet();
                exec.submit(() -> {
                    try {
                        server.handle(3, true);
                    } finally {
                        server.vthreads.decrementAndGet();
                    }
                });
            }

            if (server.admission.limiter != null) {
                server.admission.limiter.update(now / 1_000_000L);
            }

            if ((now - lastReport) / 1e9 >= REPORT_EVERY) {
                double span = (now - lastReport) / 1e9;
                synchronized (m) {
                    m.rows.add(new Row(t, sc.rate(),
                            m.wAccepted / span,
                            100.0 * m.wRejected / Math.max(1, m.wOffered),
                            m.wGoodput / span,
                            percentile(m.wLat, 0.99),
                            server.admission.inflight(),
                            server.admission.limit(),
                            server.checkoutBackend.inUse(),
                            server.vthreads.get()));
                    m.wOffered = 0;
                    m.wAccepted = 0;
                    m.wRejected = 0;
                    m.wGoodput = 0;
                    m.wLat.clear();
                }
                lastReport = now;
            }
        }

        // Let the tail drain: requests still in flight at the end of the
        // window are neither goodput nor rejections, and counting them either
        // way would be a lie about the run.
        Thread.sleep(1000);
        exec.shutdownNow();
        exec.awaitTermination(2, TimeUnit.SECONDS);
        return m;
    }

    // ------------------------------------------------------- reporting

    static final String HEADER =
            "      t   offered  accepted  reject%   goodput  p99_acc  inflight  limit   busy  vthreads";

    record Summary(String key, String label, double offered, double accepted, double rejected,
                   double goodput, double p99, double p99t0, double tier0, double rejectMs) { }

    static Summary render(Scenario sc, Metrics m) {
        System.out.printf("%n=== %s ===%n", sc.label());
        System.out.printf("    %s%n", sc.note());
        System.out.println(HEADER);
        System.out.println("-".repeat(HEADER.length()));
        for (Row r : m.rows) {
            String mark = sc.mode().equals("adaptive")
                    && Math.abs(r.t() - PERTURB_AT) < REPORT_EVERY / 2
                    ? "  <-- service time x3" : "";
            System.out.printf("  %5.1f %9.1f %9.1f %8.0f %9.1f %8.0f %9d %6.1f %6d %9d%s%n",
                    r.t(), r.offered(), r.accepted(), r.reject(), r.goodput(), r.p99(),
                    r.inflight(), r.limit(), r.busy(), r.vthreads(), mark);
        }
        double rejectMs = m.rejectCost.isEmpty() ? 0
                : m.rejectCost.stream().mapToDouble(Double::doubleValue).average().orElse(0);
        Summary s = new Summary(sc.key(), sc.label(),
                m.offered / DURATION_S,
                m.accepted / DURATION_S,
                100.0 * m.rejected / Math.max(1, m.offered),
                m.goodput / DURATION_S,
                percentile(m.latencies, 0.99),
                percentile(m.latTier0, 0.99),
                100.0 * m.tier0Goodput / Math.max(1, m.tier0Offered),
                rejectMs);
        System.out.printf("mode=%s  offered=%.0f  accepted=%.0f  rejected=%.0f%%  goodput=%.0f  "
                        + "p99_accepted=%.0fms  tier0_success=%.0f%%  p99_tier0=%.0fms  "
                        + "reject_ms=%.1f%n",
                s.key(), s.offered(), s.accepted(), s.rejected(), s.goodput(), s.p99(),
                s.tier0(), s.p99t0(), s.rejectMs());
        return s;
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Load shedding, backpressure and bulkheads: the same ramp, seven "
                + "admission policies.");
        System.out.printf("Backend capacity is %d/%.3f = %.0f rps, measured the way topic 1 "
                        + "measures it. Anything above that is not servable by anybody.%n",
                WORKERS, SERVICE_MS / 1000.0, CAPACITY);
        System.out.printf("Offered load is %.1fx and %.1fx that number. Goodput counts responses "
                        + "inside a %dms SLO; p99_acc is the p99 of ACCEPTED requests, p99_tier0 "
                        + "the p99 of tier-0 (/checkout) requests alone.%n",
                RHO_LOW, RHO_HIGH, SLO_MS);
        System.out.printf("The static limit is %d in flight with a %dms queue-wait deadline. The "
                + "adaptive one is not configured at all.%n", SHED_LIMIT, SHED_WAIT_MS);

        List<Scenario> scenarios = List.of(
                new Scenario("none_0.8", "none", "1 none, rho=0.8",
                        "The healthy baseline. Nothing is rejected because nothing needs to be.",
                        RHO_LOW * CAPACITY, TIER0_SHARE, 0),
                new Scenario("none_1.3", "none", "2 none, rho=1.3",
                        "An unbounded queue at 130% of capacity. Watch p99_acc climb and vthreads "
                        + "with it, while reject% stays at zero.",
                        RHO_HIGH * CAPACITY, TIER0_SHARE, 0),
                new Scenario("static_1.3", "static", "3 static shedding, rho=1.3",
                        String.format("Semaphore(%d).tryAcquire(%dms) -> 503 Retry-After.",
                                SHED_LIMIT, SHED_WAIT_MS),
                        RHO_HIGH * CAPACITY, TIER0_SHARE, 0),
                new Scenario("priority_1.3", "priority", "4 priority shedding, rho=1.3",
                        String.format("/checkout is tier 0 (%.0f%% of traffic) and may use all %d; "
                                + "/search is tier 3 and may use %d.",
                                TIER0_SHARE * 100, SHED_LIMIT, TIER3_LIMIT),
                        RHO_HIGH * CAPACITY, TIER0_SHARE, 0),
                new Scenario("adaptive_1.3", "adaptive", "5 adaptive shedding, rho=1.3",
                        String.format("No configured limit. Service time triples at t=%.0fs with "
                                + "nobody redeploying anything.", PERTURB_AT),
                        RHO_HIGH * CAPACITY, TIER0_SHARE, 0),
                new Scenario("bulk_shared", "bulkhead_shared", "6 bulkhead: one shared pool",
                        String.format("%.0f rps of checkout plus %.0f rps of %dms /report, all %d "
                                + "servers shared.", CHECKOUT_RPS, REPORT_RPS, REPORT_SERVICE_MS,
                                WORKERS),
                        CHECKOUT_RPS, 1.0, REPORT_RPS),
                new Scenario("bulk_split", "bulkhead_split",
                        String.format("7 bulkhead: the same 8, split %d + %d", BULK_CHECKOUT,
                                BULK_REPORT),
                        "Nothing is added. /report is now structurally incapable of touching "
                        + "checkout's servers.",
                        CHECKOUT_RPS, 1.0, REPORT_RPS));

        Map<String, Summary> byKey = new LinkedHashMap<>();
        for (Scenario sc : scenarios) {
            Metrics m = runScenario(sc);
            Summary s = render(sc, m);
            byKey.put(s.key(), s);
        }

        System.out.println();
        System.out.println("=".repeat(104));
        System.out.printf("%-38s%8s%9s%8s%8s%8s%9s%10s%10s%n", "mode", "offered", "accepted",
                "goodput", "p99_acc", "p99_t0", "reject%", "tier0_ok%", "reject_ms");
        System.out.println("-".repeat(104));
        for (Summary s : byKey.values()) {
            System.out.printf("%-38s%8.0f%9.0f%8.0f%8.0f%8.0f%9.0f%10.0f%10.1f%n", s.label(),
                    s.offered(), s.accepted(), s.goodput(), s.p99(), s.p99t0(), s.rejected(),
                    s.tier0(), s.rejectMs());
        }

        Summary none13 = byKey.get("none_1.3");
        Summary static13 = byKey.get("static_1.3");
        Summary shared = byKey.get("bulk_shared");
        Summary split = byKey.get("bulk_split");

        System.out.println();
        System.out.println("Read rows 2 and 3 as one comparison and everything else is commentary:");
        System.out.printf("  none     rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%%n",
                none13.goodput(), none13.p99(), none13.rejected());
        System.out.printf("  static   rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%%n",
                static13.goodput(), static13.p99(), static13.rejected());
        System.out.println("Same offered load, same backend, same 200 rps of capacity. The only");
        System.out.println("difference is that one of them said no.");
        System.out.println();
        System.out.println("The bulkhead pair is the other comparison worth making, and it is the one");
        System.out.println("that adds nothing at all:");
        System.out.printf("  shared pool   checkout goodput %6.0f rps   checkout p99 %6.0f ms%n",
                shared.goodput(), shared.p99t0());
        System.out.printf("  split %d + %d   checkout goodput %6.0f rps   checkout p99 %6.0f ms%n",
                BULK_CHECKOUT, BULK_REPORT, split.goodput(), split.p99t0());
        System.out.println("The split pool has FEWER servers available to checkout, and the boundary is");
        System.out.printf("worth more than the two servers it costs -- because /report at %.0f rps x "
                + "%dms wants%n", REPORT_RPS, REPORT_SERVICE_MS);
        System.out.printf("%.1f servers' worth of the shared pool and takes them from whoever asks "
                + "last. Note%n", REPORT_RPS * REPORT_SERVICE_MS / 1000.0);
        System.out.printf("what it costs: /report itself can now only ever get %.1f rps through. "
                + "That is the%n", BULK_REPORT / (REPORT_SERVICE_MS / 1000.0));
        System.out.println("bargain, and you should be able to say it out loud before you make it.");
        System.out.println();
        System.out.println("Three things to carry out of this file:");
        System.out.println("  1. An unbounded queue does not smooth load. It converts an availability");
        System.out.println("     problem into a latency problem and hides it until latency exceeds every");
        System.out.println("     timeout in the system at once.");
        System.out.println("  2. Shed on WAIT TIME, not on queue length. Length is meaningless without a");
        System.out.println("     service time attached: the same length is a healthy queue for a 1ms");
        System.out.println("     handler and a catastrophe for a 500ms one.");
        System.out.println("  3. In Java specifically: moving to virtual threads deletes an admission");
        System.out.println("     controller you did not know you had. If your service used to reject when");
        System.out.println("     its pool was full, write that limit down explicitly BEFORE you migrate,");
        System.out.println("     because afterwards the queue moves somewhere you are not measuring.");
    }

    private Shedder() { }
}
