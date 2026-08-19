// Layer 6 Topic 5 - Utilization is not saturation: the connection pool, in Java.
//
// Why Java: HikariCP plus the Micrometer binder is the top rung of the ladder,
// and the ceiling the other three are approximating by hand. Out of the box,
// with no code of yours, you get:
//
//   hikaricp.connections.acquire   a TIMER -> count, total, max, percentiles
//   hikaricp.connections.usage     a timer -> how long connections are held
//   hikaricp.connections.active / .idle / .pending    gauges
//   hikaricp.connections.timeout   a counter
//
// The first line is the whole difference. Python ships nothing, Node ships an
// instantaneous gauge, Go ships two cumulative counters that can only ever
// produce a mean -- and Java ships the distribution, in the ecosystem with the
// oldest production pooling culture.
//
// This program models that pool and that meter registry: a FIFO wait queue, a
// timer that records every acquisition, and Micrometer's two distinct ways of
// producing percentiles, which behave differently and are worth telling apart:
//
//   publishPercentiles(...)          computed IN the process from a
//                                    high-resolution histogram, exact-ish, and
//                                    NOT aggregatable across instances.
//   publishPercentileHistogram()     publishes buckets; the percentile is
//                                    interpolated by the backend, aggregates
//                                    correctly across pods, and is only as
//                                    good as the bucket boundaries.
//
// Both are printed against the true percentile from every raw sample, so the
// interpolation error is measured here rather than described. That error is
// Topic 2's bucket lesson arriving in the metric you would actually alert on.
//
// No HikariCP and no Micrometer are installed on this machine, and neither is
// needed: the observable is the stats surface, not the JDBC protocol.
//
// What to look for in the output
// ------------------------------
//  1. The ramp: utilization pins at 100% and stops moving; pending and
//     acquire-time keep climbing.
//  2. Section 3: true p99 vs the two Micrometer percentiles. One is close, one
//     depends entirely on where the bucket edges fell.
//  3. Section 5: the same 120 in-flight requests held by platform threads and
//     by virtual threads, with the OS thread count for each. The measurement is
//     identical; the cost of taking it is not. That is Layer 1's material
//     arriving as an observability property.

import java.lang.management.ManagementFactory;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Deque;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public final class PoolSaturation {

    static final int POOL_SIZE = 5;             // HikariConfig.setMaximumPoolSize(5)
    static final long SERVICE_TIME_MS = 5;      // holding the connection
    static final long THINK_TIME_MS = 10;       // application work in between
    static final long STEP_MS = 1000;
    static final long SCRAPE_INTERVAL_MS = 250; // the dashboard's scrape
    static final int[] STEPS = {2, 5, 10, 25, 60, 120};

    // -----------------------------------------------------------------------
    // Micrometer's Timer, cut down to the two ways it can give you a percentile.
    // -----------------------------------------------------------------------

    static final class Timer {
        // publishPercentileHistogram(): fixed buckets, aggregatable, interpolated.
        // A coarse ladder in milliseconds, of the kind people configure by hand.
        // Micrometer's own publishPercentileHistogram ships far more buckets than
        // this; the coarse ladder is here so the interpolation error is visible
        // at this sample size instead of hiding in the third decimal.
        static final double[] BUCKETS_MS = {
            1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, Double.POSITIVE_INFINITY
        };

        private final long[] bucketCounts = new long[BUCKETS_MS.length];
        private final List<Double> raw = new ArrayList<>();  // publishPercentiles()
        private long count;
        private double totalMs;
        private double maxMs;

        synchronized void record(double ms) {
            count++;
            totalMs += ms;
            maxMs = Math.max(maxMs, ms);
            raw.add(ms);
            for (int i = 0; i < BUCKETS_MS.length; i++) {
                if (ms <= BUCKETS_MS[i]) {
                    bucketCounts[i]++;
                    break;
                }
            }
        }

        synchronized long count() { return count; }

        synchronized double totalTimeMs() { return totalMs; }

        synchronized double maxMs() { return maxMs; }

        synchronized double mean() { return count == 0 ? 0 : totalMs / count; }

        /** publishPercentiles(0.5, 0.95, 0.99): computed in-process, near exact. */
        synchronized double percentileInProcess(double q) {
            return percentile(new ArrayList<>(raw), q);
        }

        /**
         * publishPercentileHistogram(): what the BACKEND computes, by linear
         * interpolation inside the bucket the rank falls in. Exactly
         * histogram_quantile()'s arithmetic, and exactly its failure mode.
         */
        synchronized double percentileFromBuckets(double q) {
            long target = (long) Math.ceil(q * count);
            long cumulative = 0;
            double lower = 0;
            for (int i = 0; i < BUCKETS_MS.length; i++) {
                long inBucket = bucketCounts[i];
                if (cumulative + inBucket >= target) {
                    double upper = BUCKETS_MS[i];
                    if (Double.isInfinite(upper)) {
                        // Nothing to interpolate against: the documented
                        // behaviour is to return the highest finite bound.
                        return BUCKETS_MS[BUCKETS_MS.length - 2];
                    }
                    if (inBucket == 0) return upper;
                    double within = (double) (target - cumulative) / inBucket;
                    return lower + within * (upper - lower);
                }
                cumulative += inBucket;
                lower = BUCKETS_MS[i];
            }
            return maxMs;
        }

        synchronized List<Double> snapshot() { return new ArrayList<>(raw); }
    }

    static double percentile(List<Double> values, double q) {
        if (values.isEmpty()) return 0;
        values.sort(null);
        int index = (int) Math.ceil(q * values.size()) - 1;
        return values.get(Math.max(0, Math.min(values.size() - 1, index)));
    }

    // -----------------------------------------------------------------------
    // HikariCP's pool, cut down: fixed size, FIFO waiters, connectionTimeout.
    // -----------------------------------------------------------------------

    static final class HikariPool {
        private final int size;
        private int active;
        private final Deque<CountDownLatch> waiters = new ArrayDeque<>();

        // The Micrometer binder's meters. All of these are free in the real thing.
        final Timer acquire = new Timer();      // hikaricp.connections.acquire
        final Timer usage = new Timer();        // hikaricp.connections.usage
        final AtomicInteger pending = new AtomicInteger();   // .pending (gauge)
        final AtomicLong timeouts = new AtomicLong();        // .timeout (counter)
        final AtomicInteger maxPending = new AtomicInteger(); // ours, for ground truth

        HikariPool(int size) { this.size = size; }

        int getActiveConnections() { return active; }         // .active (gauge)
        int getIdleConnections() { return size - active; }    // .idle   (gauge)
        int getTotalConnections() { return size; }
        double utilization() { return (double) active / size; }

        /** getConnection(): returns the wait in ms, or -1 on connectionTimeout. */
        double getConnection(long timeoutMs) throws InterruptedException {
            long start = System.nanoTime();
            CountDownLatch latch;
            synchronized (this) {
                if (active < size && waiters.isEmpty()) {
                    active++;
                    acquire.record(0);
                    return 0;
                }
                latch = new CountDownLatch(1);
                waiters.addLast(latch);
                int p = pending.incrementAndGet();
                maxPending.accumulateAndGet(p, Math::max);
            }

            boolean granted = latch.await(timeoutMs, TimeUnit.MILLISECONDS);
            pending.decrementAndGet();
            double waitMs = (System.nanoTime() - start) / 1e6;

            synchronized (this) {
                if (!granted) {
                    waiters.remove(latch);
                    timeouts.incrementAndGet();
                    return -1;
                }
                active++;
            }
            acquire.record(waitMs);   // <- the line the other three runtimes lack
            return waitMs;
        }

        void close(double heldMs) {
            usage.record(heldMs);
            synchronized (this) {
                active--;
                CountDownLatch next = waiters.pollFirst();
                if (next != null) {
                    // Handed straight to the longest waiter, which re-increments
                    // `active` when it wakes. FIFO, exactly like HikariCP.
                    next.countDown();
                }
            }
        }
    }

    // -----------------------------------------------------------------------

    record StepResult(int inFlight, int requests, double polledUtil, int polledPending,
                      int truePending, int scrapes, double waitP50, double waitP99,
                      double reqP99, int osThreads) {}

    static StepResult runStep(HikariPool pool, int inFlight, boolean virtualThreads)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + STEP_MS;
        int firstSample = pool.acquire.snapshot().size();
        pool.maxPending.set(0);

        List<Double> latencies = java.util.Collections.synchronizedList(new ArrayList<>());
        double[] polledUtil = {0};
        int[] polledPending = {0};
        int[] scrapes = {0};

        Thread scraper = Thread.ofPlatform().daemon().start(() -> {
            // A Prometheus scrape reading the three HikariCP gauges. It sees
            // this instant and nothing between instants.
            while (System.currentTimeMillis() < deadline) {
                polledUtil[0] = Math.max(polledUtil[0], pool.utilization());
                polledPending[0] = Math.max(polledPending[0], pool.pending.get());
                scrapes[0]++;
                try {
                    Thread.sleep(SCRAPE_INTERVAL_MS);
                } catch (InterruptedException e) {
                    return;
                }
            }
        });

        ExecutorService workers = virtualThreads
                ? Executors.newVirtualThreadPerTaskExecutor()
                : Executors.newFixedThreadPool(inFlight);

        int peakThreads;
        try (workers) {
            for (int i = 0; i < inFlight; i++) {
                workers.submit(() -> {
                    while (System.currentTimeMillis() < deadline) {
                        long requestStart = System.nanoTime();
                        try {
                            double wait = pool.getConnection(30_000);
                            if (wait < 0) continue;
                            long held = System.nanoTime();
                            Thread.sleep(SERVICE_TIME_MS);      // the query
                            pool.close((System.nanoTime() - held) / 1e6);
                            latencies.add((System.nanoTime() - requestStart) / 1e6);
                            Thread.sleep(THINK_TIME_MS);        // app work
                        } catch (InterruptedException e) {
                            return;
                        }
                    }
                });
            }
            Thread.sleep(STEP_MS / 2);
            peakThreads = ManagementFactory.getThreadMXBean().getThreadCount();
        }
        scraper.join();

        List<Double> all = pool.acquire.snapshot();
        List<Double> waits = new ArrayList<>(all.subList(firstSample, all.size()));

        return new StepResult(inFlight, waits.size(), polledUtil[0], polledPending[0],
                pool.maxPending.get(), scrapes[0],
                percentile(new ArrayList<>(waits), 0.50),
                percentile(new ArrayList<>(waits), 0.99),
                percentile(new ArrayList<>(latencies), 0.99),
                peakThreads);
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Layer 6 Topic 5 - utilization vs saturation, on a Java connection pool");
        System.out.printf("java %s   maximumPoolSize=%d, service time %d ms, scrape every %d ms%n",
                System.getProperty("java.version"), POOL_SIZE, SERVICE_TIME_MS, SCRAPE_INTERVAL_MS);
        System.out.println("=".repeat(78));
        System.out.println();

        HikariPool pool = new HikariPool(POOL_SIZE);
        List<StepResult> rows = new ArrayList<>();
        for (int step : STEPS) {
            rows.add(runStep(pool, step, false));
        }

        System.out.println("--- The ramp: one pool, six concurrency levels, everything measured ---");
        System.out.println();
        System.out.println("              |  USE: utilization  |      USE: saturation      |   RED   ");
        System.out.println("  in flight   |  polled  in use    |  max pending  acq p50/p99 |  req p99");
        System.out.println("  ------------+--------------------+---------------------------+---------");
        for (StepResult r : rows) {
            System.out.printf("  %9d   |  %4.0f%%   %d of %d    |  %11d  %5.1f/%6.1f ms |  %6.1f ms%n",
                    r.inFlight(), 100 * r.polledUtil(),
                    Math.round(r.polledUtil() * POOL_SIZE), POOL_SIZE,
                    r.truePending(), r.waitP50(), r.waitP99(), r.reqP99());
        }
        System.out.println();
        System.out.println("  Utilization pins at 100% and then carries no information at all.");
        System.out.println("  `hikaricp.connections.pending` and the acquire timer are the two");
        System.out.println("  that keep moving, and they are the two nobody puts on a dashboard.");
        System.out.println();

        System.out.println("--- Section 2: what the Micrometer binder gives you, free ---");
        System.out.println();
        System.out.printf("  hikaricp.connections.active      %d%n", pool.getActiveConnections());
        System.out.printf("  hikaricp.connections.idle        %d%n", pool.getIdleConnections());
        System.out.printf("  hikaricp.connections.pending     %d%n", pool.pending.get());
        System.out.printf("  hikaricp.connections.timeout     %d%n", pool.timeouts.get());
        System.out.printf("  hikaricp.connections.acquire     count=%d  total=%.0f ms  max=%.1f ms%n",
                pool.acquire.count(), pool.acquire.totalTimeMs(), pool.acquire.maxMs());
        System.out.printf("  hikaricp.connections.usage       count=%d  mean=%.1f ms%n",
                pool.usage.count(), pool.usage.mean());
        System.out.println();
        System.out.println("  Note what the acquire timer is: count AND total AND max AND a");
        System.out.println("  distribution. Go's two counters give you the first two, which is");
        System.out.println("  a mean. `max` alone is already more than Go can answer -- 'did");
        System.out.println("  anyone wait more than a second' has a yes/no here and does not");
        System.out.println("  there.");
        System.out.println();

        System.out.println("--- Section 3: two ways to get a percentile, and what each costs ---");
        System.out.println();
        System.out.printf("  %-14s %14s %16s %18s%n", "quantile", "true", "publishPercentiles",
                "publishPercentileHistogram");
        System.out.printf("  %-14s %14s %16s %18s%n", "-".repeat(14), "-".repeat(14),
                "-".repeat(18), "-".repeat(26));
        double[] quantiles = {0.50, 0.95, 0.99, 0.999};
        String[] labels = {"p50", "p95", "p99", "p999"};
        for (int i = 0; i < quantiles.length; i++) {
            double truth = pool.acquire.percentileInProcess(quantiles[i]);
            double bucketed = pool.acquire.percentileFromBuckets(quantiles[i]);
            double error = truth == 0 ? 0 : 100 * (bucketed - truth) / truth;
            System.out.printf("  %-14s %11.1f ms %13.1f ms %15.1f ms   (%+.0f%%)%n",
                    labels[i], truth, truth, bucketed, error);
        }
        System.out.println();
        System.out.println("  Column 3 is exact because it is computed from every sample inside");
        System.out.println("  this process -- and for the same reason it cannot be summed with");
        System.out.println("  another pod's. Column 4 aggregates correctly across a fleet and is");
        System.out.println("  only as good as the bucket boundaries, which is Topic 2's lesson");
        System.out.println("  arriving in the one metric you would actually alert on.");
        System.out.printf("  Bucket edges in use (ms): %s%n",
                Arrays.toString(Arrays.copyOf(Timer.BUCKETS_MS, Timer.BUCKETS_MS.length - 1)));
        System.out.println();

        System.out.println("--- Section 4: what the scrape saw, against what happened ---");
        System.out.println();
        System.out.printf("  %-12s %9s %15s %18s%n", "in flight", "scrapes", "true max pending",
                "polled max pending");
        for (StepResult r : rows) {
            String note = r.polledPending() < r.truePending()
                    ? "   <- missed " + (r.truePending() - r.polledPending()) : "";
            System.out.printf("  %-12d %9d %15d %18d%s%n",
                    r.inFlight(), r.scrapes(), r.truePending(), r.polledPending(), note);
        }
        System.out.println();
        System.out.println("  The gauges miss the same way node-postgres's counts do. The timer");
        System.out.println("  misses nothing, because it records at the event rather than at the");
        System.out.println("  scrape. Gauge for utilization, histogram for saturation.");
        System.out.println();

        System.out.println("--- Section 5: 120 waiters, held two ways ---");
        System.out.println();
        HikariPool platformPool = new HikariPool(POOL_SIZE);
        StepResult platform = runStep(platformPool, 120, false);
        HikariPool virtualPool = new HikariPool(POOL_SIZE);
        StepResult virtual = runStep(virtualPool, 120, true);

        System.out.printf("  %-22s %12s %14s %14s%n", "how the waiters are held",
                "JVM threads", "acq p99", "req p99");
        System.out.printf("  %-22s %12s %14s %14s%n", "-".repeat(22), "-".repeat(12),
                "-".repeat(14), "-".repeat(14));
        System.out.printf("  %-22s %12d %11.1f ms %11.1f ms%n", "platform threads",
                platform.osThreads(), platform.waitP99(), platform.reqP99());
        System.out.printf("  %-22s %12d %11.1f ms %11.1f ms%n", "virtual threads",
                virtual.osThreads(), virtual.waitP99(), virtual.reqP99());
        System.out.println();
        System.out.println("  Same pool, same fault, same numbers -- and one of them cost 120 OS");
        System.out.println("  threads to observe while the other cost a handful. That is why");
        System.out.println("  load generators were written closed-loop for twenty years (Topic 2)");
        System.out.println("  and it is the same change as Layer 1's, arriving here as the price");
        System.out.println("  of holding enough requests in flight to see the queue at all.");
        System.out.println();

        System.out.println("--- The ladder, in one table ---");
        System.out.println();
        System.out.printf("  %-24s %-26s %-22s%n", "runtime", "free saturation metric",
                "wait percentile?");
        System.out.printf("  %-24s %-26s %-22s%n", "-".repeat(24), "-".repeat(26), "-".repeat(22));
        System.out.printf("  %-24s %-26s %-22s%n", "Python / SQLAlchemy", "none (a status string)",
                "only if you build it");
        System.out.printf("  %-24s %-26s %-22s%n", "Node.js / node-postgres", "waitingCount (gauge)",
                "only if you build it");
        System.out.printf("  %-24s %-26s %-22s%n", "Go / database/sql", "WaitCount, WaitDuration",
                "no - counters give a mean");
        System.out.printf("  %-24s %-26s %-22s%n", "Java / HikariCP", "acquire timer",
                "yes, out of the box");
        System.out.println();
        System.out.println("  Run the other three programs in this topic and read their last");
        System.out.println("  sections against this table. The rung you are on decides how much");
        System.out.println("  of an incident you can reconstruct afterwards.");
    }
}
