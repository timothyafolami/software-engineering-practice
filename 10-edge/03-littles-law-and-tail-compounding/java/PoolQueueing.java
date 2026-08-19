// Layer 10 - Topic 3: the pool is the concurrency limit. (Java)
//
// What this demonstrates
//     Part 1  L = λW as a wall, against a Semaphore -- which is what
//             HikariCP's maximumPoolSize is underneath, and HikariCP's own
//             documentation argues the Little's Law case better than most
//             textbooks. c permits and mean service time W pin maximum
//             throughput at c/W.
//     Part 2  The claim this lab keeps making, tested: virtual threads
//             MOVE the queue rather than removing it. Two configurations
//             with the same c = 20:
//
//               platform  a fixed pool of 20 platform threads. The thread
//                         pool IS the limit; the queue is the executor's
//                         task queue, and "thread count" is the metric
//                         people watch.
//               virtual   one virtual thread per request, all blocking on
//                         a Semaphore(20). Threads are no longer scarce,
//                         so thread count tells you nothing at all -- and
//                         the wait reappears, in full, as permit wait.
//
// What to look for
//     - Total time in system is the same to within noise. It has to be:
//       L = λW with the same λ and the same c does not care which object
//       the waiting happens in front of.
//     - `threads created` differs by orders of magnitude. That is the
//       metric that stopped meaning anything.
//     - `wait p99` is COMPARABLE in the two rows, not near zero in the
//       platform row. That is deliberate: this program times the wait from
//       the moment the request was SUBMITTED, so it can see the executor's
//       task queue. A real service instruments inside the handler, after a
//       thread has picked the work up, and that timer cannot see the
//       executor queue at all -- it would report near zero while the same
//       requests sat waiting. So the danger of the old shape is not that
//       the queue is absent, it is that the usual in-handler timer is
//       blind to it, which is why queue depth is the metric to alert on.
//       The program prints this caveat next to the table rather than
//       leaving you to infer it from a suspiciously flattering number.
//
// The Kingman variance arm lives in python/pool_queueing.py -- distributions
// are arithmetic, not a property of any runtime.
//
// No dependencies. Requires Java 21+ for virtual threads. Runs with no
// arguments:
//     cd java && javac PoolQueueing.java -d /tmp/javabuild \
//       && java -cp /tmp/javabuild PoolQueueing

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

public class PoolQueueing {
    static final long SEED = 20260818L;
    static final int SLOTS = 20;
    static final long SERVICE_MS = 50;

    record Sample(double acquire, double service, double total) {}

    static double pct(List<Double> values, double q) {
        if (values.isEmpty()) return Double.NaN;
        List<Double> v = new ArrayList<>(values);
        Collections.sort(v);
        return v.get(Math.min(v.size() - 1, (int) (q * v.size())));
    }

    /** Open-loop Poisson arrivals against a Semaphore of `slots` permits. */
    static Result drive(double lambda, int slots, long serviceMs, double durationSec,
                        boolean virtualThreads, boolean useSemaphore) throws Exception {
        Random rng = new Random(SEED);
        Semaphore pool = new Semaphore(slots);
        AtomicLong threadsCreated = new AtomicLong();
        List<Sample> samples = Collections.synchronizedList(new ArrayList<>());
        AtomicLong completed = new AtomicLong();

        ThreadFactory counting = r -> {
            threadsCreated.incrementAndGet();
            return virtualThreads ? Thread.ofVirtual().unstarted(r)
                                  : Thread.ofPlatform().unstarted(r);
        };
        ExecutorService exec = virtualThreads
                ? Executors.newThreadPerTaskExecutor(counting)
                // The old shape: the thread pool itself is the concurrency
                // limit, and work waits in the executor's queue.
                : Executors.newFixedThreadPool(slots, counting);

        long start = System.nanoTime();
        double nextNanos = start;
        long durationNanos = (long) (durationSec * 1e9);
        while (System.nanoTime() - start < durationNanos) {
            nextNanos += -Math.log(1 - rng.nextDouble()) / lambda * 1e9;
            long delay = (long) nextNanos - System.nanoTime();
            if (delay > 0) TimeUnit.NANOSECONDS.sleep(delay);
            long arrived = System.nanoTime();
            exec.submit(() -> {
                try {
                    if (useSemaphore) pool.acquire();
                    long acquired = System.nanoTime();
                    Thread.sleep(serviceMs);
                    long done = System.nanoTime();
                    if (useSemaphore) pool.release();
                    completed.incrementAndGet();
                    samples.add(new Sample((acquired - arrived) / 1e6,
                            (done - acquired) / 1e6, (done - arrived) / 1e6));
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
            });
        }
        double wall = (System.nanoTime() - start) / 1e9;
        // Completions that landed INSIDE the arrival window. Throughput has to
        // be counted over the same interval as `wall`: `completed` keeps rising
        // during the drain below, and dividing the post-drain total by the
        // arrival window reports a rate above c/W -- above the wall itself.
        long completedInWindow = completed.get();

        exec.shutdown();
        // Bounded drain: past the wall the queue never drains, and that is
        // the result rather than a bug.
        if (!exec.awaitTermination((long) durationSec, TimeUnit.SECONDS)) {
            exec.shutdownNow();
        }
        return new Result(samples, completedInWindow, wall, threadsCreated.get());
    }

    record Result(List<Sample> samples, long completed, double wall, long threads) {
        List<Double> acquire() { return samples.stream().map(Sample::acquire).toList(); }
        List<Double> service() { return samples.stream().map(Sample::service).toList(); }
        List<Double> total() { return samples.stream().map(Sample::total).toList(); }
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Java " + Runtime.version().feature()
                + " - pool queueing and Little's Law");
        System.out.println("  arrivals: Poisson (c_a = 1), open loop, seed " + SEED);

        System.out.printf("%nPart 1 - L = λW. c = %d permits, W = %dms, "
                + "so λ_max = c/W = %.0f req/s%n", SLOTS, SERVICE_MS,
                SLOTS / (SERVICE_MS / 1000.0));
        System.out.println("-".repeat(78));
        System.out.printf("  %-10s %5s %9s %9s %9s %9s %9s%n",
                "run", "ρ", "acq p50", "acq p99", "svc p50", "tot p99", "done/s");
        for (double lambda : new double[] {200, 360, 400, 440}) {
            Result r = drive(lambda, SLOTS, SERVICE_MS, 3.0, true, true);
            double rho = lambda * (SERVICE_MS / 1000.0) / SLOTS;
            System.out.printf("  %-10s %5.2f %9.1f %9.1f %9.1f %9.1f %9.0f%n",
                    "λ=" + (int) lambda, rho,
                    pct(r.acquire(), 0.5), pct(r.acquire(), 0.99),
                    pct(r.service(), 0.5), pct(r.total(), 0.99),
                    r.completed() / r.wall());
        }
        System.out.println();
        System.out.println("  Service time is identical in every row. Everything that moved");
        System.out.println("  is waiting for a permit, which is why it needs its own timer.");

        System.out.println();
        System.out.println("Part 2 - virtual threads move the queue, they do not remove it");
        System.out.println("-".repeat(78));
        System.out.printf("  λ = 360/s, c = %d either way. Only the object you wait in "
                + "front of changes.%n%n", SLOTS);
        System.out.printf("  %-38s %9s %11s %11s %10s%n",
                "configuration", "threads", "wait p99", "tot p99", "done/s");
        Object[][] configs = {
            {"fixed platform pool of " + SLOTS, false, false},
            {"virtual threads + Semaphore(" + SLOTS + ")", true, true},
        };
        for (Object[] cfg : configs) {
            Result r = drive(360, SLOTS, SERVICE_MS, 4.0, (boolean) cfg[1], (boolean) cfg[2]);
            System.out.printf("  %-38s %9d %10.1f %10.1f %10.0f%n",
                    cfg[0], r.threads(), pct(r.acquire(), 0.99),
                    pct(r.total(), 0.99), r.completed() / r.wall());
        }
        System.out.println();
        System.out.println("  Throughput, wait and total latency all match, because c and λ");
        System.out.println("  match and Little's Law does not care what kind of object the");
        System.out.println("  waiting happens in front of. The queue moved from the executor");
        System.out.println("  to the semaphore and kept its exact size.");
        System.out.println();
        System.out.println("  Read the `threads` column: 20 against roughly a thousand. That is");
        System.out.println("  the number that stopped meaning anything. On the platform row,");
        System.out.println("  thread count IS the concurrency limit, so watching it tells you");
        System.out.println("  when you are full. On the virtual row it tells you how much");
        System.out.println("  traffic arrived, and nothing whatsoever about capacity -- so the");
        System.out.println("  alert has to be on permit wait or queue depth instead.");
        System.out.println();
        System.out.println("  One measurement caveat worth naming, because it flatters the");
        System.out.println("  platform row here: `wait p99` above is timed from the moment the");
        System.out.println("  request was submitted, so it captures the executor queue. A real");
        System.out.println("  service usually instruments INSIDE the handler, after a thread has");
        System.out.println("  picked the work up, and that timer cannot see the executor queue");
        System.out.println("  at all. Same queue, invisible.");
        System.out.println();
        System.out.println("  So: which term of Little's Law changed? None. Concurrency limits");
        System.out.println("  are conserved. The metric you must alert on is what changed.");
        System.out.println();
        System.out.println("  The Kingman variance arm is in python/pool_queueing.py.");
        System.exit(0);
    }
}
