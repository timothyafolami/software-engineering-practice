// Layer 2 · Topic 2 - Java: virtual threads remove the thread limit and
// leave the pool limit exactly where it was.
//
// Java is in this topic because Java 21 is where a lot of teams are right
// now, and virtual threads are being sold -- correctly -- as the answer to
// thread-per-request scaling. What they are NOT is an answer to a bounded
// connection pool, and the difference is worth seeing once with your own
// numbers, because "we moved to virtual threads and the p99 got worse" is
// a real and confusing outcome.
//
// Three runs, same offered load, same 10-connection pool:
//
//   1. platform threads, fixed pool of 50   - two limits stacked: 50
//      threads and 10 connections. The thread pool queue absorbs the
//      overflow, and the task queue is bounded by memory.
//   2. virtual threads                       - the thread limit is gone.
//      Every request gets a thread instantly. Throughput does not move,
//      because the connection pool never moved. What moved is WHERE the
//      queue is: thousands of virtual threads parked on the semaphore.
//   3. virtual threads + a bounded semaphore that REJECTS - the fix. The
//      queue is bounded on purpose and the failure is visible.
//
// What to look for in the output:
//   - completed requests per second: essentially identical in runs 1 and 2
//   - peak waiters: small in run 1, enormous in run 2
//   - p99: run 2 is worse than run 1 despite "more concurrency"
//
// Compile & run:
//   javac -d /tmp/javabuild PoolCeilingVsThreads.java && java -cp /tmp/javabuild PoolCeilingVsThreads

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

public class PoolCeilingVsThreads {

    static final int POOL_SIZE = 10;             // "connections"
    static final long QUERY_MS = 50;             // time each connection is held
    static final int ARRIVAL_RATE = 400;         // requests per second, open model
    static final long DURATION_MS = 5_000;
    static final int PLATFORM_THREADS = 50;

    public static void main(String[] args) throws Exception {
        System.out.println("=".repeat(78));
        System.out.println("Java: the pool ceiling does not care how cheap your threads are");
        System.out.println("=".repeat(78));
        System.out.printf("  java %s%n", System.getProperty("java.version"));
        System.out.printf("  pool %d connections, %d ms each, offered %d rps for %d ms%n",
                POOL_SIZE, QUERY_MS, ARRIVAL_RATE, DURATION_MS);
        System.out.printf("  Little's Law ceiling: %d / %.3fs = %.0f rps%n%n",
                POOL_SIZE, QUERY_MS / 1000.0, POOL_SIZE / (QUERY_MS / 1000.0));

        try (ExecutorService platform = Executors.newFixedThreadPool(PLATFORM_THREADS)) {
            run("1. PLATFORM THREADS - fixed pool of " + PLATFORM_THREADS, platform, null);
        }
        System.out.println();
        try (ExecutorService virtual = Executors.newVirtualThreadPerTaskExecutor()) {
            run("2. VIRTUAL THREADS - no thread limit at all", virtual, null);
        }
        System.out.println();
        try (ExecutorService virtual = Executors.newVirtualThreadPerTaskExecutor()) {
            run("3. VIRTUAL THREADS + bounded wait (reject after 150 ms)", virtual, Duration.ofMillis(150));
        }

        System.out.println();
        System.out.println("  What runs 1 and 2 are actually telling you:");
        System.out.println("    Throughput is set by the pool, not by the threads. Making threads");
        System.out.println("    free does not make connections free. What virtual threads change");
        System.out.println("    is the SHAPE of the overload: a fixed platform pool queues tasks");
        System.out.println("    in one place you can size and instrument, while");
        System.out.println("    newVirtualThreadPerTaskExecutor happily creates a thread for every");
        System.out.println("    arriving request and parks it on the semaphore. Nothing pushes");
        System.out.println("    back until memory does.");
        System.out.println();
        System.out.println("    This is why 'just switch to virtual threads' can raise p99. You");
        System.out.println("    removed the accidental backpressure that a fixed pool was");
        System.out.println("    providing, and did not replace it with deliberate backpressure.");
        System.out.println("    Run 3 is the replacement: tryAcquire with a timeout, so the wait");
        System.out.println("    is bounded and overload arrives as an error rate instead of an");
        System.out.println("    unbounded latency tail.");
    }

    static void run(String label, ExecutorService executor, Duration maxWait) throws Exception {
        Semaphore pool = new Semaphore(POOL_SIZE);
        AtomicInteger waiters = new AtomicInteger();
        AtomicInteger peakWaiters = new AtomicInteger();
        AtomicInteger completed = new AtomicInteger();
        AtomicInteger rejected = new AtomicInteger();
        AtomicLong issued = new AtomicLong();
        List<Double> latencies = Collections.synchronizedList(new ArrayList<>());

        long start = System.nanoTime();
        long index = 0;
        // Open model: submissions happen on a schedule derived from the start
        // time, never from when previous work finished.
        while (true) {
            long dueNanos = start + (index * 1_000_000_000L) / ARRIVAL_RATE;
            if ((dueNanos - start) / 1_000_000 > DURATION_MS) break;
            long waitNanos = dueNanos - System.nanoTime();
            if (waitNanos > 0) {
                TimeUnit.NANOSECONDS.sleep(waitNanos);
            }
            index++;
            issued.incrementAndGet();
            executor.submit(() -> {
                long began = System.nanoTime();
                int now = waiters.incrementAndGet();
                peakWaiters.accumulateAndGet(now, Math::max);
                boolean acquired = false;
                try {
                    if (maxWait == null) {
                        pool.acquire();
                        acquired = true;
                    } else {
                        acquired = pool.tryAcquire(maxWait.toMillis(), TimeUnit.MILLISECONDS);
                    }
                    waiters.decrementAndGet();
                    if (!acquired) {
                        rejected.incrementAndGet();
                        latencies.add((System.nanoTime() - began) / 1e6);
                        return;
                    }
                    Thread.sleep(QUERY_MS);       // the query
                    completed.incrementAndGet();
                    latencies.add((System.nanoTime() - began) / 1e6);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    if (acquired) pool.release();
                }
            });
        }
        long issueWindowMs = (System.nanoTime() - start) / 1_000_000;

        executor.shutdown();
        boolean drained = executor.awaitTermination(60, TimeUnit.SECONDS);
        long totalMs = (System.nanoTime() - start) / 1_000_000;

        List<Double> sorted = new ArrayList<>(latencies);
        Collections.sort(sorted);

        System.out.println("  " + label);
        System.out.printf("    offered              %.0f rps (%d issued over %d ms)%n",
                issued.get() * 1000.0 / issueWindowMs, issued.get(), issueWindowMs);
        System.out.printf("    completed            %.0f rps (%d over the full %d ms)%n",
                completed.get() * 1000.0 / totalMs, completed.get(), totalMs);
        System.out.printf("    rejected             %d%n", rejected.get());
        System.out.printf("    peak waiters on pool %d   <-- this is the queue%n", peakWaiters.get());
        System.out.printf("    latency p50 %.0f ms   p95 %.0f ms   p99 %.0f ms   max %.0f ms%n",
                at(sorted, 0.50), at(sorted, 0.95), at(sorted, 0.99), at(sorted, 1.0));
        if (totalMs > issueWindowMs * 1.2) {
            System.out.printf("    BACKLOG              %d ms of draining after the load stopped%n",
                    totalMs - issueWindowMs);
        }
        if (!drained) {
            System.out.println("    NOTE: did not drain within 60 s. That is the finding.");
        }
    }

    static double at(List<Double> sorted, double fraction) {
        if (sorted.isEmpty()) return Double.NaN;
        int index = Math.min(sorted.size() - 1, (int) (sorted.size() * fraction));
        return sorted.get(index);
    }
}
