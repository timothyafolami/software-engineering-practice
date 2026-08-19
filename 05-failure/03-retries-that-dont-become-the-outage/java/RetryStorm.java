// Layer 5 - Topic 3: retry amplification, in one JVM.
//
// Java has the most mature policy libraries of any language in this lab --
// Resilience4j's Retry with IntervalFunction.ofExponentialRandomBackoff,
// Spring Retry, and gRPC-Java's retryThrottling from the same service-config
// spec Go's gRPC uses. Backoff, jitter and even budgets are all available
// off the shelf.
//
// Which is exactly why the Java-specific hazard is not the policy but the
// LAYERING. A Feign client with retries, inside a Resilience4j decorator
// with retries, over an HttpClient whose connection pool retries idempotent
// requests, is three multipliers most teams do not know they have -- and
// they multiply, they do not max. Counting the retry layers in an existing
// Java service is a genuinely useful exercise and the answer is rarely one.
// Variant E below is that mistake, measured.
//
// WHAT THIS DEMONSTRATES
//
//   gateway -> serviceB -> serviceC -> database, each hop retrying up to 3
//   times. The database refuses connections for a window in the middle of
//   the run. The leaf counter counts DATABASE CALLS, so the theoretical
//   worst case is 3 hops x 3 attempts = 27x the offered rate.
//
//     A naive        exponential backoff, no jitter, no budget
//     B + jitter     full jitter: sleep = random(0, min(cap, base * 2**n))
//     C + budget     a 10% token bucket at every hop, Envoy/gRPC style
//     D edge only    only the hop adjacent to the database retries, and it
//                    marks the error non-retryable on the way up
//     E two layers   variant A, plus one extra retry decorator per hop that
//                    nobody remembered was there. 3 attempts becomes 9 per
//                    hop and the worst case becomes 9^3 = 729.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `amp` during the fault, and what it does AFTER the fault clears.
//      Once retries have built a queue, the queue causes the next round of
//      retries; whether that loop sustains itself is a property of the
//      runtime and the headroom rather than of the policy.
//   2. Variant C's retry traffic falling to zero by itself as failures
//      climb. Nobody decides that; the bucket runs dry.
//   3. Variant E against variant A. The policies are identical. The only
//      difference is that somebody wrapped a retrying client in a retrying
//      decorator, which is a code review nobody failed.
//
// RUN
//   javac RetryStorm.java -d /tmp/javabuild && java -cp /tmp/javabuild RetryStorm

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.Callable;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

public class RetryStorm {

    // -------------------------------------------------------------- config

    static final double OFFERED_RPS = 150.0;
    static final long DURATION_MS = 24_000;
    static final long FAULT_ON_MS = 5_000;
    static final long FAULT_OFF_MS = 12_000;
    static final long BUCKET_MS = 2_000;

    static final int ATTEMPTS = 3;
    static final long BASE_BACKOFF_MS = 50;
    static final long BACKOFF_CAP_MS = 400;
    static final long ATTEMPT_TIMEOUT_MS = 300;
    static final long REQUEST_BUDGET_MS = 1_500;

    static final int LEAF_POOL = 8;
    static final long LEAF_SERVICE_MS = 40;

    static final double BUDGET_RATIO = 0.10;   // Envoy's budget_percent
    static final double BUDGET_FLOOR = 3.0;    // Envoy's min_retry_concurrency

    // -------------------------------------------------------- retry budget

    /**
     * A token bucket that permits retries only while retries stay under some
     * fraction of successes. Envoy calls it budget_percent, gRPC-Java calls
     * it retryThrottling in its service config, Yandex reported settling on
     * 10%. Resilience4j does not ship one.
     *
     * The property is qualitative rather than numeric: at low failure rates
     * this behaves exactly like an ordinary retrying client, and as failures
     * climb its retry traffic goes to ZERO by itself. Backoff delays
     * amplification; only this bounds it.
     */
    static final class RetryBudget {
        private final AtomicLong milliTokens = new AtomicLong((long) (BUDGET_FLOOR * 1000));
        private final long ceiling = (long) ((BUDGET_FLOOR + 100) * 1000);

        /** Refills on SUCCESSES, never on wall-clock: an idle service must
         *  not accumulate retries it never earned, and a service in total
         *  outage must not receive a steady drip of amplification forever. */
        void deposit() {
            milliTokens.updateAndGet(t -> Math.min(t + (long) (BUDGET_RATIO * 1000), ceiling));
        }

        boolean withdraw() {
            return milliTokens.getAndUpdate(t -> t >= 1000 ? t - 1000 : t) >= 1000;
        }
    }

    // ------------------------------------------------------------- errors

    static final class Unavailable extends RuntimeException {
        Unavailable(String s) { super(s, null, false, false); }
    }

    /** Variant D's entire mechanism: "I already spent the attempts here,
     *  do not spend yours." */
    static final class NonRetryable extends RuntimeException {
        NonRetryable(String s) { super(s, null, false, false); }
    }

    /** (1) Only transient failures are worth retrying. A 400, 401, 403, 404
     *  or 422 will fail identically forever, and retrying it is pure waste. */
    static boolean retryable(Throwable t) {
        return t instanceof Unavailable;
    }

    // --------------------------------------------------------------- leaf

    static final class Metrics {
        final AtomicLong leafReceived = new AtomicLong();
        final AtomicLong ok = new AtomicLong();
        final AtomicLong failed = new AtomicLong();
        final AtomicLong retries = new AtomicLong();
        final AtomicLong budgetDenied = new AtomicLong();
        final List<double[]> samples = Collections.synchronizedList(new ArrayList<>());
    }

    static final class Leaf {
        final Semaphore pool = new Semaphore(LEAF_POOL, true);
        final AtomicBoolean faulty = new AtomicBoolean();
        final Metrics m;

        Leaf(Metrics m) { this.m = m; }

        Void call() throws InterruptedException {
            // THE COUNTER THAT MATTERS. Requests RECEIVED, not requests
            // succeeded. Divided by the client's offered rate it is the live
            // amplification factor, and the one number here worth a dashboard.
            m.leafReceived.incrementAndGet();

            if (faulty.get()) {
                // Connection refused: fast, cheap, and therefore the worst
                // kind of failure for a retrying client, because the retry
                // arrives almost immediately.
                throw new Unavailable("connection refused");
            }
            pool.acquire();
            try {
                Thread.sleep(LEAF_SERVICE_MS);
            } finally {
                pool.release();
            }
            return null;
        }
    }

    // ------------------------------------------------------------- policy

    /**
     * One retry decorator. Resilience4j's Retry.decorateCallable is this,
     * with more configuration and the same semantics -- and, critically, the
     * same property that wrapping one in another MULTIPLIES the attempts.
     */
    static final class RetryLayer {
        final boolean jitter;
        final RetryBudget budget;   // null when the variant has no budget
        final Metrics m;
        final Random rng;

        RetryLayer(boolean jitter, RetryBudget budget, Metrics m, Random rng) {
            this.jitter = jitter;
            this.budget = budget;
            this.m = m;
            this.rng = rng;
        }

        <T> T call(Callable<T> body, long deadline) throws Exception {
            long delay = BASE_BACKOFF_MS;
            RuntimeException last = new Unavailable("never attempted");

            for (int attempt = 0; attempt < ATTEMPTS; attempt++) {
                if (attempt > 0) {
                    // (4) The budget, checked BEFORE the sleep, so a denied
                    // retry costs nothing at all -- not even the wait.
                    if (budget != null && !budget.withdraw()) {
                        m.budgetDenied.incrementAndGet();
                        throw last;
                    }
                    m.retries.incrementAndGet();

                    long bounded = Math.min(BACKOFF_CAP_MS, delay);
                    // Full jitter, the AWS Builders' Library recommendation
                    // and Resilience4j's ofExponentialRandomBackoff: spread a
                    // synchronised cohort across the WHOLE interval rather
                    // than around a common centre.
                    long wait;
                    synchronized (rng) {
                        wait = jitter ? (long) (rng.nextDouble() * bounded) : bounded;
                    }
                    delay *= 2;

                    // (3) A hard cap that fits inside the caller's budget. A
                    // retry policy allowed to outlive its caller's deadline
                    // is generating topic 2's zombie work on purpose.
                    if (System.currentTimeMillis() + wait > deadline) throw last;
                    Thread.sleep(wait);
                }

                if (System.currentTimeMillis() >= deadline) throw last;
                try {
                    return body.call();
                } catch (NonRetryable e) {
                    throw e;
                } catch (Exception e) {
                    if (!retryable(e)) throw e;
                    last = (RuntimeException) e;
                }
            }
            throw last;
        }
    }

    // -------------------------------------------------------------- chain

    static final class Chain {
        final Leaf leaf;
        final Metrics m;
        final boolean edgeOnly;
        final int layersPerHop;
        final RetryLayer[][] layers;   // [hop][layer]

        Chain(Leaf leaf, Metrics m, boolean jitter, boolean budgeted,
              boolean edgeOnly, int layersPerHop) {
            this.leaf = leaf;
            this.m = m;
            this.edgeOnly = edgeOnly;
            this.layersPerHop = layersPerHop;
            Random rng = new Random(777);
            this.layers = new RetryLayer[3][layersPerHop];
            for (int hop = 0; hop < 3; hop++) {
                // One bucket per hop, shared across every request that hop
                // handles. Per-request state would defeat the whole idea:
                // the budget exists to make one client's retries visible to
                // the next client's.
                RetryBudget b = budgeted ? new RetryBudget() : null;
                for (int layer = 0; layer < layersPerHop; layer++) {
                    layers[hop][layer] = new RetryLayer(jitter, b, m, rng);
                }
            }
        }

        /** Wrap `body` in every retry layer this hop has. With one layer this
         *  is an ordinary retrying client. With two it is a Feign client
         *  inside a Resilience4j decorator, and the attempts multiply. */
        <T> T through(int hop, Callable<T> body, long deadline) throws Exception {
            Callable<T> wrapped = body;
            for (int layer = 0; layer < layersPerHop; layer++) {
                final Callable<T> inner = wrapped;
                final RetryLayer rl = layers[hop][layer];
                wrapped = () -> rl.call(inner, deadline);
            }
            return wrapped.call();
        }

        Void serviceC(long deadline) throws Exception {
            try {
                return through(2, () -> withTimeout(leaf::call, deadline), deadline);
            } catch (Exception e) {
                if (edgeOnly && !(e instanceof NonRetryable)) {
                    // THE STRUCTURAL FIX. The hop next to the failure has
                    // already spent its attempts; saying so upward turns the
                    // worst case from 3^3 back into 3, composes cleanly with
                    // topic 2, and is far easier to reason about than tuning.
                    throw new NonRetryable("exhausted at the edge");
                }
                throw e;
            }
        }

        Void serviceB(long deadline) throws Exception {
            return through(1, () -> serviceC(deadline), deadline);
        }

        void gateway() {
            long deadline = System.currentTimeMillis() + REQUEST_BUDGET_MS;
            try {
                through(0, () -> serviceB(deadline), deadline);
                m.ok.incrementAndGet();
            } catch (Exception e) {
                m.failed.incrementAndGet();
            }
        }
    }

    /** The per-attempt patience, run on its own virtual thread so that
     *  "we stopped waiting" and "the work stopped" stay visibly separate --
     *  which is topic 2's whole point, and is exactly the gap a retry loop
     *  turns into amplification. */
    static Void withTimeout(Callable<Void> body, long deadline) throws Exception {
        long budget = Math.min(ATTEMPT_TIMEOUT_MS, deadline - System.currentTimeMillis());
        if (budget <= 0) throw new Unavailable("no budget for an attempt");

        final Throwable[] outcome = new Throwable[1];
        Thread runner = Thread.ofVirtual().start(() -> {
            try {
                body.call();
            } catch (Throwable e) {
                outcome[0] = e;
            }
        });
        runner.join(budget);
        if (runner.isAlive()) {
            // We give up waiting. The virtual thread carries on holding
            // whatever it holds, and the retry we are about to issue is
            // therefore additive rather than a replacement.
            runner.interrupt();
            throw new Unavailable("attempt timed out");
        }
        if (outcome[0] instanceof NonRetryable nr) throw nr;
        if (outcome[0] != null) throw new Unavailable(String.valueOf(outcome[0].getMessage()));
        return null;
    }

    // ------------------------------------------------------------ driver

    static Metrics runVariant(boolean jitter, boolean budgeted, boolean edgeOnly,
                              int layersPerHop) throws InterruptedException {
        Metrics m = new Metrics();
        Leaf leaf = new Leaf(m);
        Chain chain = new Chain(leaf, m, jitter, budgeted, edgeOnly, layersPerHop);
        Random arrivals = new Random(20250503);

        long begin = System.currentTimeMillis();
        long end = begin + DURATION_MS;
        double at = begin;
        long lastBucket = begin, lastReceived = 0, lastOk = 0, lastTotal = 0;
        List<Thread> inFlight = new ArrayList<>();

        while (true) {
            at += -Math.log(1 - arrivals.nextDouble()) / OFFERED_RPS * 1000;
            if (at > end) break;
            long wait = (long) at - System.currentTimeMillis();
            if (wait > 0) Thread.sleep(wait);

            long t = System.currentTimeMillis() - begin;
            leaf.faulty.set(t >= FAULT_ON_MS && t < FAULT_OFF_MS);
            inFlight.add(Thread.ofVirtual().start(chain::gateway));

            if (System.currentTimeMillis() - lastBucket >= BUCKET_MS) {
                double span = (System.currentTimeMillis() - lastBucket) / 1000.0;
                double received = (m.leafReceived.get() - lastReceived) / span;
                long total = m.ok.get() + m.failed.get();
                long done = total - lastTotal;
                long ok = m.ok.get() - lastOk;
                m.samples.add(new double[]{
                        t / 1000.0, received, received / OFFERED_RPS,
                        done > 0 ? 100.0 * ok / done : 0.0});
                lastBucket = System.currentTimeMillis();
                lastReceived = m.leafReceived.get();
                lastOk = m.ok.get();
                lastTotal = total;
            }
        }
        for (Thread t : inFlight) t.join();
        return m;
    }

    // ---------------------------------------------------------- reporting

    record Summary(String label, double peak, double tail, double tailSuccess) {}

    static Summary render(String label, Metrics m) {
        System.out.printf("%n=== %s ===%n", label);
        System.out.println("     t   leaf rps      amp   success                 amplification");
        List<double[]> samples = new ArrayList<>(m.samples);
        double peak = samples.stream().mapToDouble(s -> s[2]).max().orElse(0);
        double scale = Math.max(peak, 1);
        for (double[] s : samples) {
            int n = (int) Math.round(34 * s[2] / scale);
            String fault = (s[0] * 1000 >= FAULT_ON_MS && s[0] * 1000 < FAULT_OFF_MS)
                    ? " FAULT" : "      ";
            System.out.printf("  %5.1f %10.1f %8.2f %8.1f%%%s |%s%n",
                    s[0], s[1], s[2], s[3], fault, "#".repeat(Math.max(0, n)));
        }
        List<double[]> after = samples.stream()
                .filter(s -> s[0] >= FAULT_OFF_MS / 1000.0 + 4).toList();
        double tail = after.stream().mapToDouble(s -> s[2]).average().orElse(0);
        double tailSuccess = after.stream().mapToDouble(s -> s[3]).average().orElse(0);
        System.out.printf("  peak amp %.2fx   mean amp from %.0fs onward %.2fx   success after %.1f%%   retries %d   budget-denied %d%n",
                peak, FAULT_OFF_MS / 1000.0 + 4, tail, tailSuccess,
                m.retries.get(), m.budgetDenied.get());
        return new Summary(label, peak, tail, tailSuccess);
    }

    /**
     * Why the table above makes jitter look useless, and why it is not.
     *
     * In the sweep, arrivals are a Poisson process: every client fails at a
     * different moment already, so their retries were never going to
     * collide. Jitter has nothing to decorrelate, and full jitter's shorter
     * average wait actually lets MORE attempts fit inside the budget.
     *
     * Production is not that. Production is a thousand clients that were all
     * talking to the same dependency when it fell over at the same instant.
     */
    static void synchronisedCohort() {
        Random rng = new Random(20250503);
        int clients = 1000;
        long delay = Math.min(BACKOFF_CAP_MS, BASE_BACKOFF_MS * 2);

        System.out.println("\n" + "=".repeat(78));
        System.out.println("Why the table above makes jitter look pointless: 1000 clients, one");
        System.out.println("simultaneous failure, arrival times of their first retry.");
        histogram("no jitter -- sleep = min(cap, base * 2**n)", clients, () -> (double) delay);
        histogram("full jitter -- sleep = random(0, min(cap, base * 2**n))", clients,
                () -> rng.nextDouble() * delay);
        System.out.println("\n  Same number of retries either way. Jitter does not reduce the");
        System.out.println("  area, it reduces the PEAK, and the peak is what a service trying");
        System.out.println("  to recover actually has to survive. The benefit is about");
        System.out.println("  correlation, not about randomness, which is exactly why it is");
        System.out.println("  invisible in a single-process test with independent arrivals.");
    }

    static void histogram(String title, int clients, java.util.function.DoubleSupplier draw) {
        int n = 10;
        double width = BACKOFF_CAP_MS / (double) n;
        int[] buckets = new int[n];
        for (int i = 0; i < clients; i++) {
            buckets[Math.min((int) (draw.getAsDouble() / width), n - 1)]++;
        }
        System.out.printf("%n  %s%n", title);
        int peak = 0;
        for (int i = 0; i < n; i++) {
            peak = Math.max(peak, buckets[i]);
            System.out.printf("   %5.0f-%-5.0fms |%s %d%n", i * width, (i + 1) * width,
                    "#".repeat((int) Math.round(48.0 * buckets[i] / clients)), buckets[i]);
        }
        System.out.printf("   peak instantaneous retry rate: %.0f rps from %d clients%n",
                peak / (width / 1000.0), clients);
    }

    public static void main(String[] args) throws Exception {
        System.out.printf("Retry amplification through gateway -> serviceB -> serviceC -> database (%s).%n",
                Runtime.version());
        System.out.printf("Offered %.0f rps for %ds, database refuses connections from t=%ds to t=%ds.%n",
                OFFERED_RPS, DURATION_MS / 1000, FAULT_ON_MS / 1000, FAULT_OFF_MS / 1000);
        System.out.printf("%d attempts per hop over 3 hops = %dx worst case at the leaf; the leaf's real capacity is %d/%.3f = %.0f rps.%n",
                ATTEMPTS, ATTEMPTS * ATTEMPTS * ATTEMPTS, LEAF_POOL, LEAF_SERVICE_MS / 1000.0,
                LEAF_POOL / (LEAF_SERVICE_MS / 1000.0));
        System.out.println("amp = database calls per second / offered rps. Watch what it does AFTER the fault clears.");

        List<Summary> rows = new ArrayList<>();
        rows.add(render("A naive: exponential backoff, no jitter", runVariant(false, false, false, 1)));
        rows.add(render("B + full jitter", runVariant(true, false, false, 1)));
        rows.add(render("C + 10% retry budget at every hop", runVariant(true, true, false, 1)));
        rows.add(render("D retry at the edge only", runVariant(true, false, true, 1)));
        rows.add(render("E variant A + one forgotten layer", runVariant(false, false, false, 2)));

        System.out.println("\n" + "=".repeat(78));
        System.out.printf("%-44s%10s%11s%14s%n", "variant", "peak amp", "amp after", "success after");
        System.out.println("-".repeat(78));
        for (Summary r : rows) {
            System.out.printf("%-44s%9.2fx%10.2fx%13.1f%%%n", r.label(), r.peak(), r.tail(), r.tailSuccess());
        }

        System.out.println();
        System.out.println("The 27x worst case does not appear, and why it does not is the useful");
        System.out.println("part: the per-attempt timeout and the request budget expire before the");
        System.out.println("deepest retries can be attempted. Timeouts cap amplification by accident.");
        System.out.println("Do not rely on an accident.");
        System.out.println();
        System.out.println("Row E is the Java-specific one, and it is why this file is in Java. It");
        System.out.println("runs variant A's policy with one extra retry decorator per hop -- a");
        System.out.println("Feign client inside a Resilience4j decorator, say, which is a pull");
        System.out.println("request nobody would reject. On paper the attempts per hop go from 3 to");
        System.out.println("9 and the worst case from 27x to 729x.");
        System.out.println();
        System.out.println("Compare E's peak against A's, and its `retries` count against A's, and");
        System.out.println("read the gap between those two comparisons carefully. The extra layer");
        System.out.println("issues materially more retries and barely moves the peak, because the");
        System.out.println("request budget expires long before nine attempts per hop can be spent.");
        System.out.println("Two ways to say the same thing: your deadline is doing the work your");
        System.out.println("retry configuration is supposed to do, and it is the only thing between");
        System.out.println("you and 729x. Lengthen one timeout -- a change that looks like patience");
        System.out.println("and reads as harmless -- and the layers you forgot about get to finish");
        System.out.println("what they started. Go and count the layers in a service you own.");
        System.out.println();
        System.out.println("C is the only variant whose retry traffic falls as failures climb, and");
        System.out.println("the only one that is a bound rather than a delay. D gets most of the");
        System.out.println("same benefit structurally, by making the answer to 'which layer owns");
        System.out.println("retries' a single layer -- which is also the answer to row E.");

        synchronisedCohort();
    }
}
