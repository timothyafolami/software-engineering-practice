// Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
// JVM.
//
// Java sits between Go and Rust on this topic. There is no ambient context
// in the platform -- gRPC-Java ships its own Context/Deadline, and Deadline
// is absolute-time based, which is the shape modelled below.
// HttpClient.newBuilder().connectTimeout() plus
// HttpRequest.newBuilder().timeout() cover the transport;
// CompletableFuture.orTimeout covers composition; and JDBC's
// Statement.setQueryTimeout is the equivalent of statement_timeout -- with
// the same caveat, that it ASKS THE DRIVER to cancel, and what the server
// does about that is the server's business.
//
// Java 21's ScopedValue plus StructuredTaskScope is the first thing in the
// platform that looks like Go's context tree. Both are still preview APIs in
// 21, so this file uses a ThreadLocal to carry the deadline instead -- which
// is exactly what gRPC-Java's Context does underneath, and which works
// unchanged on virtual threads. If you have virtual threads and a JDK where
// ScopedValue is final, that is the shape to build; the mechanism this file
// demonstrates does not change.
//
// WHAT THIS DEMONSTRATES
//
//   gateway -> serviceB -> serviceC, C holds a pooled connection for a
//   controlled service time, gateway budget 500ms.
//
//     1 healthy               everything succeeds; the bug is invisible
//     2 naive                 every hop uses the same 500ms constant, so B
//                             and C never learn what is left of the budget
//     3 deadline propagated   the absolute deadline rides a ThreadLocal;
//                             B and C refuse work that cannot finish, and
//                             hand a connection straight back when the
//                             request behind it is already dead
//     4 + setQueryTimeout     the query itself is bounded, not just the
//                             CompletableFuture waiting for it
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `zombie/s` -- completions C finished AFTER the gateway had already
//      returned 504. One pool slot and one service time each.
//   2. `C pool in use` pinned at the pool size in row 2. That is topic 1's
//      L, spent entirely on work nobody is waiting for.
//   3. Row 3 helps and does not fix it. orTimeout completes YOUR future
//      exceptionally; it does not reach into the database. This is the same
//      finding as Python's shield and Rust's spawn_blocking, in a third
//      spelling.
//   4. Row 4 bounds the work at the resource, and the pool comes back down.
//      Watch gateway success and `C pool in use` move together -- they are
//      the same fact stated twice.
//
// The load generator is OPEN MODEL: Poisson arrivals, and it does not wait
// for a response before sending the next request.
//
// RUN
//   javac DeadlineChain.java -d /tmp/javabuild && java -cp /tmp/javabuild DeadlineChain

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

public class DeadlineChain {

    // -------------------------------------------------------------- config

    static final long GATEWAY_BUDGET_MS = 500;
    static final long SLACK_MS = 20;
    static final long HOP_OVERHEAD_MS = 5;
    static final long C_SERVICE_FAST_MS = 40;
    static final long C_SERVICE_SLOW_MS = 800;
    static final double SLOW_FRACTION = 0.25;
    static final int C_POOL_SIZE = 8;
    static final double RATE = 50.0;
    static final long DURATION_MS = 12_000;
    static final long WARMUP_MS = 3_000;   // JIT only; never printed
    static final long GAUGE_EVERY_MS = 20;

    // ------------------------------------------------------ the deadline

    /**
     * An ABSOLUTE deadline, which is what gRPC-Java's Deadline is too. The
     * absoluteness is the load-bearing part: a relative per-call timeout
     * cannot compose, because the third call in a handler has no idea what
     * the first two already spent.
     */
    record Deadline(long atNanos) {
        long remainingMs() {
            return TimeUnit.NANOSECONDS.toMillis(atNanos - System.nanoTime());
        }
        boolean expiredWithin(long marginMs) {
            return remainingMs() < marginMs;
        }
    }

    /**
     * The carrier. gRPC-Java's Context is a ThreadLocal underneath, and a
     * ThreadLocal is inherited by nothing -- so every place the deadline
     * needs to cross a thread boundary is a place somebody has to write code.
     * That is the whole ergonomic gap between this and Go's ctx parameter.
     */
    static final ThreadLocal<Deadline> DEADLINE = new ThreadLocal<>();

    static Deadline deadline() {
        return DEADLINE.get();
    }

    // ------------------------------------------------------------ metrics

    static final class Metrics {
        final AtomicLong ok = new AtomicLong();
        final AtomicLong failed = new AtomicLong();
        final AtomicLong zombie = new AtomicLong();
        final AtomicLong killed = new AtomicLong();
        final AtomicLong abandoned = new AtomicLong();
        final AtomicLong inUse = new AtomicLong();
        final List<Double> cLatency = Collections.synchronizedList(new ArrayList<>());
        final List<Double> gauge = Collections.synchronizedList(new ArrayList<>());
    }

    // --------------------------------------------------------- the pool

    /**
     * HikariCP, and the database behind it. A permit is a connection.
     * Nothing the caller does shortens a query that is already running --
     * only the query timeout does, and only when one was set.
     */
    static final class Pool {
        private final Semaphore permits;
        private final Metrics m;

        Pool(int size, Metrics m) {
            this.permits = new Semaphore(size, true);
            this.m = m;
        }

        boolean query(long durationMs, Deadline dl, boolean useQueryTimeout)
                throws InterruptedException {
            permits.acquire();
            try {
                // Checked out. If the request that queued for this connection
                // died while it was queueing, give the connection straight
                // back rather than spend a whole service time on a corpse.
                // Under overload this is where the recovered capacity is.
                if (dl != null && dl.expiredWithin(SLACK_MS)) {
                    m.abandoned.incrementAndGet();
                    return false;
                }

                m.inUse.incrementAndGet();
                try {
                    if (dl != null && useQueryTimeout) {
                        // Statement.setQueryTimeout(seconds), derived from the
                        // SAME number as the application budget. Two
                        // independently chosen timeouts is how you end up
                        // shedding load in the app while the database stays
                        // pinned -- the errors AND the load.
                        long budget = Math.max(0, dl.remainingMs() - SLACK_MS);
                        if (budget < durationMs) {
                            Thread.sleep(budget);
                            m.killed.incrementAndGet();
                            return false;
                        }
                    }
                    Thread.sleep(durationMs);
                    return true;
                } finally {
                    m.inUse.decrementAndGet();
                }
            } finally {
                permits.release();
            }
        }
    }

    static final class Expired extends RuntimeException {
        Expired(String s) {
            super(s, null, false, false);
        }
    }

    // ---------------------------------------------------------- the hops

    static void serviceC(Pool pool, Metrics m, boolean slow, long gatewayDeadlineNanos,
                         boolean useQueryTimeout) throws InterruptedException {
        Deadline dl = deadline();
        if (dl != null && dl.expiredWithin(SLACK_MS)) {
            // Refuse to START work that cannot finish. A request rejected
            // here costs no pool slot, no queue position, nothing at all.
            throw new Expired("no budget left at C");
        }
        Thread.sleep(HOP_OVERHEAD_MS);

        long duration = slow ? C_SERVICE_SLOW_MS : C_SERVICE_FAST_MS;
        long started = System.nanoTime();

        // The query runs on its own virtual thread -- a database session,
        // not a coroutine of yours. Completing our CompletableFuture
        // exceptionally below does not touch it.
        CompletableFuture<Boolean> query = new CompletableFuture<>();
        Thread.ofVirtual().start(() -> {
            boolean completed;
            try {
                completed = pool.query(duration, dl, useQueryTimeout);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                completed = false;
            }
            long finished = System.nanoTime();
            m.cLatency.add((finished - started) / 1e6);
            if (completed && finished > gatewayDeadlineNanos) {
                m.zombie.incrementAndGet();
            }
            query.complete(completed);
        });

        long localBudget = dl != null
                ? Math.max(0, dl.remainingMs())
                : GATEWAY_BUDGET_MS;   // the naive constant, copied down the chain

        try {
            // orTimeout completes THIS stage exceptionally. The virtual
            // thread above never finds out. That sentence is the topic.
            query.orTimeout(localBudget, TimeUnit.MILLISECONDS).join();
        } catch (Exception e) {
            throw new Expired("timed out waiting on C");
        }
    }

    static void serviceB(Pool pool, Metrics m, boolean slow, long gatewayDeadlineNanos,
                         boolean useQueryTimeout) throws InterruptedException {
        Deadline dl = deadline();
        if (dl != null && dl.expiredWithin(SLACK_MS)) {
            throw new Expired("no budget left at B");
        }
        Thread.sleep(HOP_OVERHEAD_MS);

        if (dl != null) {
            // budget_out = budget_in - elapsed_here - slack. With an absolute
            // deadline the subtraction is the only arithmetic there is, and
            // it can only ever tighten.
            DEADLINE.set(new Deadline(dl.atNanos() - TimeUnit.MILLISECONDS.toNanos(SLACK_MS)));
        }
        serviceC(pool, m, slow, gatewayDeadlineNanos, useQueryTimeout);
    }

    static void gateway(Pool pool, Metrics m, boolean slow, boolean propagate,
                        boolean useQueryTimeout) {
        long gatewayDeadlineNanos =
                System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(GATEWAY_BUDGET_MS);
        if (propagate) {
            DEADLINE.set(new Deadline(gatewayDeadlineNanos));
        } else {
            DEADLINE.remove();
        }
        try {
            serviceB(pool, m, slow, gatewayDeadlineNanos, useQueryTimeout);
            // The gateway's own budget, enforced locally as well as passed
            // on: a caller that only trusts the callee to be timely has no
            // timeout at all.
            if (System.nanoTime() <= gatewayDeadlineNanos) {
                m.ok.incrementAndGet();
            } else {
                m.failed.incrementAndGet();
            }
        } catch (Exception e) {
            m.failed.incrementAndGet();
        } finally {
            DEADLINE.remove();
        }
    }

    // -------------------------------------------------------- the driver

    static Metrics runVariant(double slowFraction, boolean propagate, boolean useQueryTimeout)
            throws Exception {
        return runVariant(slowFraction, propagate, useQueryTimeout, DURATION_MS);
    }

    static Metrics runVariant(double slowFraction, boolean propagate, boolean useQueryTimeout,
                              long durationMs)
            throws InterruptedException {
        Metrics m = new Metrics();
        Pool pool = new Pool(C_POOL_SIZE, m);
        // Identical arrivals and an identical set of slow requests in every
        // variant, so what differs between rows is policy and only policy.
        Random rng = new Random(20250502);

        Thread sampler = Thread.ofPlatform().daemon().start(() -> {
            try {
                while (!Thread.currentThread().isInterrupted()) {
                    Thread.sleep(GAUGE_EVERY_MS);
                    m.gauge.add((double) m.inUse.get());
                }
            } catch (InterruptedException ignored) {
            }
        });

        long begin = System.nanoTime();
        long end = begin + TimeUnit.MILLISECONDS.toNanos(durationMs);
        long at = begin;
        List<Thread> requests = new ArrayList<>();
        while (true) {
            at += (long) (-Math.log(1 - rng.nextDouble()) / RATE * 1e9);
            if (at > end) break;
            long wait = at - System.nanoTime();
            if (wait > 0) Thread.sleep(wait / 1_000_000L, (int) (wait % 1_000_000L));
            boolean slow = rng.nextDouble() < slowFraction;
            // One virtual thread per request. On platform threads this
            // program would be a study in thread exhaustion instead; see the
            // C++ version's `threads peak` column for what that looks like.
            requests.add(Thread.ofVirtual().start(
                    () -> gateway(pool, m, slow, propagate, useQueryTimeout)));
        }
        for (Thread t : requests) t.join();
        // Drain. Zombies are by definition still running after everyone gave
        // up, so a report taken at the end of the load would undercount them.
        Thread.sleep(C_SERVICE_SLOW_MS + 300);
        sampler.interrupt();
        return m;
    }

    // ------------------------------------------------------- reporting

    static final String HEADER =
            "variant                      gw success  zombie/s  C pool in use  C p99 ms  killed/s  gaveback/s";

    static void printRow(String label, Metrics m) {
        double seconds = DURATION_MS / 1000.0;
        long total = m.ok.get() + m.failed.get();
        double success = total > 0 ? 100.0 * m.ok.get() / total : 0;
        List<Double> lat = new ArrayList<>(m.cLatency);
        Collections.sort(lat);
        System.out.printf("%-28s %9.1f%% %9.1f %13s %9.0f %9.1f %11.1f%n",
                label, success, m.zombie.get() / seconds,
                String.format("%.1f/%d", mean(m.gauge), C_POOL_SIZE),
                percentile(lat, 99), m.killed.get() / seconds, m.abandoned.get() / seconds);
    }

    static double percentile(List<Double> sorted, double p) {
        if (sorted.isEmpty()) return 0;
        int k = (int) Math.round(p / 100 * (sorted.size() - 1));
        return sorted.get(Math.min(k, sorted.size() - 1));
    }

    static double mean(List<Double> v) {
        List<Double> copy = new ArrayList<>(v);
        if (copy.isEmpty()) return 0;
        double s = 0;
        for (double x : copy) s += x;
        return s / copy.size();
    }

    public static void main(String[] args) throws Exception {
        double fastDemand = RATE * (1 - SLOW_FRACTION) * C_SERVICE_FAST_MS / 1000.0;
        double slowDemand = RATE * SLOW_FRACTION * C_SERVICE_SLOW_MS / 1000.0;

        System.out.printf("Deadline propagation through gateway -> serviceB -> serviceC (%s).%n",
                Runtime.version());
        System.out.printf("Gateway budget %dms, slack %dms per hop, C pool %d, offered %.0f rps for %ds.%n",
                GATEWAY_BUDGET_MS, SLACK_MS, C_POOL_SIZE, RATE, DURATION_MS / 1000);
        System.out.printf("When C is unwell, %.0f%% of queries take %dms and the rest take %dms.%n",
                SLOW_FRACTION * 100, C_SERVICE_SLOW_MS, C_SERVICE_FAST_MS);
        System.out.printf("Demand on the pool is then %.1f + %.1f = %.1f connection-seconds per second%n",
                slowDemand, fastDemand, slowDemand + fastDemand);
        System.out.printf("against %d available, i.e. rho = %.2f. None of the slow queries can beat the budget.%n%n",
                C_POOL_SIZE, (slowDemand + fastDemand) / C_POOL_SIZE);
        System.out.println(HEADER);
        System.out.println("-".repeat(HEADER.length()));

        // Discarded. The first variant to run pays for class loading and for
        // the interpreter running every hop until C2 catches up, and on a
        // 500ms budget that alone fails ~7% of requests and drags C's p99 to
        // ~450ms. Left in, it would make the healthy BASELINE look like a
        // symptom of the thing this program is about. Warm it, then measure.
        runVariant(0.0, false, false, WARMUP_MS);

        printRow("1 healthy", runVariant(0.0, false, false));
        printRow("2 naive", runVariant(SLOW_FRACTION, false, false));
        printRow("3 deadline propagated", runVariant(SLOW_FRACTION, true, false));
        printRow("4 + setQueryTimeout", runVariant(SLOW_FRACTION, true, true));

        System.out.println();
        System.out.println("Rows 2 and 3: an absolute deadline on a ThreadLocal, subtracted once");
        System.out.println("per hop, is the whole discipline. B and C stop queueing work whose");
        System.out.println("caller has already gone, and hand connections back the moment they");
        System.out.println("find one checked out for a dead request ('gaveback/s').");
        System.out.println();
        System.out.println("Rows 3 and 4 are the half the platform will not do for you.");
        System.out.println("CompletableFuture.orTimeout completes YOUR stage exceptionally. The");
        System.out.println("virtual thread holding the connection never hears about it, and");
        System.out.println("neither does the database. setQueryTimeout is the piece that bounds");
        System.out.println("the work rather than the waiting -- and even it only asks the driver");
        System.out.println("to cancel. What the server does about that request is the server's");
        System.out.println("business, which is why Postgres has statement_timeout as well.");
    }
}
