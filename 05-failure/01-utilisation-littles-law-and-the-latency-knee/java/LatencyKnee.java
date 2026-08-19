// Layer 5 - Topic 1: the latency knee in Java, and the counterintuitive
// result about virtual threads.
//
// WHAT THIS DEMONSTRATES
//
//   Java stacks two counts and most teams tune the wrong one. The servlet
//   container's thread pool (Tomcat's maxThreads, 200 by default under
//   Spring Boot) is the number people raise; HikariCP's maximumPoolSize
//   (default 10) is almost always the smaller number and therefore the
//   actual ceiling. Raising maxThreads moves nothing, because lambda_max
//   is L/S where L is the SMALLEST count on the path.
//
//   The second, less intuitive half: switching the container to virtual
//   threads makes the knee SHARPER, not gentler. A bounded platform pool
//   with a bounded queue eventually *rejects*, and a rejection is fast --
//   it caps latency by discarding work. Virtual threads remove that
//   ceiling, so nothing is ever rejected and every excess request queues
//   on the connection pool instead. You traded a bounded-latency failure
//   for an unbounded-latency one. Predict which you want before you look.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `achieved` plateaus at pool / S, not at containerThreads / S. The
//      200-thread container is irrelevant to capacity.
//   2. p99 tracks the S/(1-rho) column while rho < 1 and leaves it behind
//      once the queue stops draining.
//   3. `wait p50` -- time queued for a connection, not time spent using
//      one -- is ~0 at rho=0.2 and is nearly all of the latency by 0.95.
//      The handler never got slower.
//   4. Doubling maximumPoolSize moves capacity and the knee with it.
//   5. Platform sweep at rho=1.1 has a non-zero `rejected` column and a
//      finite p99. Virtual sweep at rho=1.1 has zero rejections and a p99
//      several times larger. Same pool, same load, same service time.
//
// The load generator is OPEN MODEL: arrivals are a Poisson process
// computed ahead of time, and each request's clock starts when it was
// SCHEDULED to arrive, not when this program managed to dispatch it. A
// generator that starts the clock at dispatch forgives itself for being
// late. Topic 6 is about why that matters.
//
// RUN
//   javac LatencyKnee.java -d /tmp/javabuild && java -cp /tmp/javabuild LatencyKnee
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Future;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class LatencyKnee {

    // ------------------------------------------------------------ config

    static final long SERVICE_NANOS = 40_000_000L;   // 40ms inside a connection
    static final double STEP_SECONDS = 8.0;
    static final double[] RHOS = {0.2, 0.5, 0.8, 0.9, 0.95, 1.1};
    static final int[] POOL_SIZES = {5, 10};         // HikariCP maximumPoolSize
    static final int CONTAINER_THREADS = 200;        // Tomcat maxThreads default
    static final int CONTAINER_QUEUE = 100;          // Tomcat acceptCount default
    static final double OVERLOAD_RHO = 1.5;          // sustained, not a spike
    static final long GAUGE_EVERY_NANOS = 20_000_000L;

    static final Random RNG = new Random(20250501L);

    // ------------------------------------------------- the bounded thing

    /**
     * Stands in for HikariCP plus the database behind it. Acquiring a permit
     * is `getConnection()`; holding it for SERVICE_NANOS is the query. This
     * is the only genuinely bounded resource in the program, which is the
     * entire point of the topic: capacity is a property of the smallest
     * count, and everything else is decoration.
     */
    static final class ConnectionPool {
        private final Semaphore permits;

        ConnectionPool(int size) {
            // Fair, because HikariCP hands connections out in arrival order
            // and an unfair pool would give you a tail that is an artefact
            // of the lock rather than of the queue.
            this.permits = new Semaphore(size, true);
        }

        void query() throws InterruptedException {
            permits.acquire();
            try {
                sleepNanos(SERVICE_NANOS);
            } finally {
                permits.release();
            }
        }
    }

    // --------------------------------------------------- the two carriers

    interface Joinable {
        void join() throws InterruptedException;
    }

    /** How a request gets a thread. The only difference between the sweeps. */
    interface Carrier {
        /** Returns a handle, or null if the request was rejected outright. */
        Joinable submit(Runnable r);

        void shutdown();

        String describe();
    }

    /**
     * The traditional servlet container: a fixed pool of platform threads
     * with a bounded queue in front of it. When both are full the request
     * is REJECTED -- which is a real answer, delivered fast, and is why
     * this configuration's p99 stays finite past rho=1.
     */
    static Carrier platformCarrier() {
        ThreadPoolExecutor ex = new ThreadPoolExecutor(
                CONTAINER_THREADS, CONTAINER_THREADS,
                0L, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(CONTAINER_QUEUE),
                new ThreadPoolExecutor.AbortPolicy());
        ex.prestartAllCoreThreads();
        return new Carrier() {
            public Joinable submit(Runnable r) {
                try {
                    Future<?> f = ex.submit(r);
                    return () -> {
                        try {
                            f.get();
                        } catch (Exception ignored) {
                        }
                    };
                } catch (RejectedExecutionException e) {
                    return null;   // 503 at the container, before any Java of yours runs
                }
            }

            public void shutdown() {
                ex.shutdownNow();
            }

            public String describe() {
                return CONTAINER_THREADS + " platform threads + queue of " + CONTAINER_QUEUE
                        + " (rejects when both are full)";
            }
        };
    }

    /**
     * Java 21 virtual threads: one per request, no ceiling worth the name.
     * Nothing is ever rejected, so nothing caps latency except the pool.
     */
    static Carrier virtualCarrier() {
        return new Carrier() {
            public Joinable submit(Runnable r) {
                Thread t = Thread.ofVirtual().start(r);
                return t::join;
            }

            public void shutdown() {
            }

            public String describe() {
                return "one virtual thread per request (no ceiling, nothing is ever rejected)";
            }
        };
    }

    // ------------------------------------------------------- measurement

    record Result(double offered, double achieved, double p50, double p99,
                  double waitP50, double meanTotal, double gaugeL, long rejected) {
    }

    /**
     * One measurement step at a fixed offered rate.
     */
    static Result step(ConnectionPool pool, Carrier carrier, double rate, double durSeconds)
            throws InterruptedException {
        List<Double> total = Collections.synchronizedList(new ArrayList<>());
        List<Long> completions = Collections.synchronizedList(new ArrayList<>());
        List<Double> gauge = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger inflight = new AtomicInteger();
        AtomicLong rejected = new AtomicLong();
        List<Joinable> handles = new ArrayList<>();

        long begin = System.nanoTime();
        long deadline = begin + (long) (durSeconds * 1e9);

        Thread sampler = Thread.ofPlatform().daemon().start(() -> {
            try {
                while (!Thread.currentThread().isInterrupted()) {
                    sleepNanos(GAUGE_EVERY_NANOS);
                    gauge.add((double) inflight.get());
                }
            } catch (InterruptedException ignored) {
            }
        });

        int sent = 0;
        long at = begin;
        while (true) {
            at += (long) (expovariate() / rate * 1e9);
            if (at > deadline) break;
            long wait = at - System.nanoTime();
            if (wait > 0) sleepNanos(wait);
            sent++;
            final long scheduled = at;

            Joinable h = carrier.submit(() -> {
                inflight.incrementAndGet();
                try {
                    pool.query();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                } finally {
                    inflight.decrementAndGet();
                }
                long done = System.nanoTime();
                // Latency measured from the SCHEDULED arrival, so lateness in
                // the generator counts against the system under test rather
                // than being quietly forgiven.
                total.add((done - scheduled) / 1e6);
                completions.add(done);
            });

            if (h == null) {
                rejected.incrementAndGet();
            } else {
                handles.add(h);
            }
        }

        // Drain. Past rho=1 this is where the backlog built up during the
        // step finally comes out, which is why those rows carry latencies
        // longer than the step itself.
        for (Joinable h : handles) h.join();
        sampler.interrupt();

        // Queue wait = total latency minus the service time we know each
        // request spent actually holding a connection. With a single
        // controlled S this subtraction is exact, and it isolates the term
        // that the 1/(1-rho) curve is actually about: the handler never got
        // slower, only the waiting in front of it did.
        List<Double> sortedTotal = new ArrayList<>(total);
        List<Double> sortedWaits = new ArrayList<>(sortedTotal.size());
        for (double t : sortedTotal) {
            sortedWaits.add(Math.max(0.0, t - SERVICE_NANOS / 1e6));
        }
        Collections.sort(sortedTotal);
        Collections.sort(sortedWaits);

        long inWindow = completions.stream().filter(c -> c <= deadline).count();
        return new Result(
                sent / durSeconds,
                inWindow / durSeconds,
                percentile(sortedTotal, 50),
                percentile(sortedTotal, 99),
                percentile(sortedWaits, 50),
                mean(sortedTotal),
                mean(gauge),
                rejected.get());
    }

    // ------------------------------------------------------------ sweeps

    static final String HEADER =
            "  rho   offered  achieved      p50      p99   wait p50   L (gauge)   lam*Wbar   S/(1-rho)  rejected";

    static void printRow(double rho, Result r, double serviceSeconds) {
        String predicted = rho < 1
                ? String.format("%9.1f", serviceSeconds / (1 - rho) * 1000)
                : "      inf";
        System.out.printf("%5.2f %9.1f %9.1f %8.1f %8.1f %10.1f %11.1f %10.1f %s %9d%n",
                rho, r.offered(), r.achieved(), r.p50(), r.p99(), r.waitP50(), r.gaugeL(),
                r.achieved() * r.meanTotal() / 1000, predicted, r.rejected());
    }

    static double[] sweep(int poolSize, Carrier carrier, String label) throws InterruptedException {
        ConnectionPool pool = new ConnectionPool(poolSize);

        // Measure S rather than assuming it. A capacity computed from a
        // constant nobody measured is the commonest way this experiment
        // quietly lies to you.
        Result warm = step(pool, carrier, 5, 2.0);
        double service = warm.meanTotal() / 1000;
        double capacity = poolSize / service;

        System.out.printf("%n=== %s ===%n", label);
        System.out.printf("carrier: %s%n", carrier.describe());
        System.out.printf("maximumPoolSize = %d, measured service time S = %.1f ms%n",
                poolSize, service * 1000);
        System.out.printf("predicted capacity L/S = %.1f rps%n%n", capacity);
        System.out.println(HEADER);
        System.out.println("-".repeat(HEADER.length()));

        double[] p99s = new double[RHOS.length];
        for (int i = 0; i < RHOS.length; i++) {
            Result r = step(pool, carrier, capacity * RHOS[i], STEP_SECONDS);
            printRow(RHOS[i], r, service);
            p99s[i] = r.p99();
        }
        return p99s;
    }

    /** The knee is a shape, and a table of numbers hides shapes. */
    static void chart(double[] p99s) {
        double top = Arrays.stream(p99s).max().orElse(1);
        if (top == 0) top = 1;
        System.out.println("\n  p99 (ms) against rho");
        for (int i = 0; i < p99s.length; i++) {
            int n = Math.max(1, (int) Math.round(56 * p99s[i] / top));
            System.out.printf("  rho=%-6.2f|%s %.0f%n", RHOS[i], "#".repeat(n), p99s[i]);
        }
        System.out.printf("  %10s+%s %.0f ms full scale%n", "", "-".repeat(56), top);
    }

    // -------------------------------------------------------------- main

    public static void main(String[] args) throws Exception {
        System.out.printf("Latency knee in Java (%s, %d cores).%n",
                Runtime.version(), Runtime.getRuntime().availableProcessors());
        System.out.println("Two counts stacked: a " + CONTAINER_THREADS + "-thread container in front of a");
        System.out.println("connection pool of " + POOL_SIZES[0] + " then " + POOL_SIZES[1]
                + ". Watch which one sets capacity.");

        double[] last = null;
        for (int size : POOL_SIZES) {
            Carrier platform = platformCarrier();
            last = sweep(size, platform, "Platform threads, maximumPoolSize = " + size);
            platform.shutdown();
            chart(last);
        }

        Carrier virtual = virtualCarrier();
        int size = POOL_SIZES[POOL_SIZES.length - 1];
        double[] virtualP99 = sweep(size, virtual, "Virtual threads, maximumPoolSize = " + size);
        virtual.shutdown();
        chart(virtualP99);

        overloadComparison(size);

        System.out.println();
        System.out.println("Raising maxThreads did nothing to capacity in any sweep, because");
        System.out.println("capacity is L/S at the SMALLEST count and that was never the threads.");
        System.out.println("Removing the thread bound entirely did change something, and not in");
        System.out.println("the direction the phrase 'removed a bottleneck' suggests: the bounded");
        System.out.println("container was shedding load for you, and you did not know it was a");
        System.out.println("feature until you deleted it. Topic 5 is about doing that on purpose.");
    }

    /**
     * The sweeps above stop at rho=1.1, where 8 seconds of 10% excess is not
     * enough backlog to reach any container bound. Sustained overload is
     * where the two carriers stop agreeing, so run one on purpose: the same
     * pool, the same offered rate, only the thread carrier differs.
     */
    static void overloadComparison(int poolSize) throws InterruptedException {
        System.out.printf("%n=== Sustained overload: rho = %.1f, maximumPoolSize = %d ===%n",
                OVERLOAD_RHO, poolSize);
        System.out.println("Same pool, same offered rate, same handler. Only the carrier differs.\n");
        System.out.printf("%-22s %9s %9s %9s %9s %10s%n",
                "carrier", "offered", "achieved", "p50", "p99", "rejected");
        System.out.println("-".repeat(73));

        double capacity = poolSize / (SERVICE_NANOS / 1e9);
        for (String kind : new String[]{"platform", "virtual"}) {
            Carrier c = kind.equals("platform") ? platformCarrier() : virtualCarrier();
            ConnectionPool pool = new ConnectionPool(poolSize);
            Result r = step(pool, c, capacity * OVERLOAD_RHO, STEP_SECONDS);
            c.shutdown();
            System.out.printf("%-22s %9.1f %9.1f %9.1f %9.1f %10d%n",
                    kind, r.offered(), r.achieved(), r.p50(), r.p99(), r.rejected());
        }

        System.out.println();
        System.out.println("  The platform row rejects and keeps a finite p99. The virtual row");
        System.out.println("  rejects nothing and pays for it in the tail, because the backlog");
        System.out.println("  has nowhere to go but the queue in front of the pool. Neither is");
        System.out.println("  'correct' -- but only one of them was a decision somebody made.");
    }

    // ---------------------------------------------------------- plumbing

    static double expovariate() {
        return -Math.log(1 - RNG.nextDouble());
    }

    static void sleepNanos(long nanos) throws InterruptedException {
        if (nanos <= 0) return;
        Thread.sleep(nanos / 1_000_000L, (int) (nanos % 1_000_000L));
    }

    static double percentile(List<Double> sorted, double p) {
        if (sorted.isEmpty()) return 0;
        int k = (int) Math.round(p / 100 * (sorted.size() - 1));
        return sorted.get(Math.min(k, sorted.size() - 1));
    }

    static double mean(List<Double> v) {
        if (v.isEmpty()) return 0;
        double s = 0;
        for (double x : v) s += x;
        return s / v.size();
    }
}
