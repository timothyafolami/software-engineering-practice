// Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.
//
// Why Java: it is the only runtime here that can run the *same* open-loop
// generator two ways in one process -- one platform thread per in-flight
// request, and one virtual thread per in-flight request -- and show that the
// measurement is identical while the cost of taking it is not. That comparison
// is the practical answer to "why was every load generator written closed-loop
// for twenty years": because holding a thousand in-flight requests used to mean
// a thousand OS threads, and the profession quietly redefined the measurement
// to fit the tool. Java 21 removes the excuse.
//
// This file therefore runs three phases:
//   1. closed-loop, 4 virtual users (the dishonest-but-cheap default)
//   2. open-loop with PLATFORM threads (honest, and historically expensive)
//   3. open-loop with VIRTUAL threads (honest and cheap)
// and prints how many OS threads the JVM actually started for each.
//
// What this demonstrates
// ----------------------
//   * Service: single server, FIFO queue, 3ms per request -> ~333 req/s.
//   * Offered load: 200 req/s, a comfortable 60% of capacity.
//   * At T+2.5s exactly one request takes 500ms. One request.
//
// What to look for in the output
// ------------------------------
//   1. "requests started IN the stall window": ~4 closed-loop, ~100 open-loop.
//      That one line is the entire mechanism.
//   2. p99 for phases 2 and 3. They should agree -- virtual threads change what
//      the measurement costs, not what it says.
//   3. "OS threads started". Phase 2 against phase 3.
//
// Takes about 15 seconds: three 5-second load phases, run in sequence so they
// cannot interfere with each other.
//
// Run:
//   javac CoordinatedOmission.java -d /tmp/javabuild && \
//     java -cp /tmp/javabuild CoordinatedOmission

import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public class CoordinatedOmission {

    static final long SERVICE_MS = 3;         // -> ~333 req/s capacity
    static final long STALL_AFTER_MS = 2500;  // when the one slow request happens
    static final long STALL_MS = 500;         // how long that one request takes
    static final long RUN_MS = 5000;
    static final int OPEN_RATE_PER_SEC = 200; // offered load, ~60% of capacity
    static final int CLOSED_VUS = 4;
    static final long CLOSED_THINK_MS = (long) (CLOSED_VUS / (double) OPEN_RATE_PER_SEC * 1000);

    static final class Request {
        final int seq;
        final long arrivalNs;   // when it *should* have been sent
        volatile long sentNs;   // when it actually was sent
        volatile long doneNs;
        final CountDownLatch settled = new CountDownLatch(1);

        Request(int seq, long arrivalNs) {
            this.seq = seq;
            this.arrivalNs = arrivalNs;
        }
    }

    /** A single server with a FIFO queue. The queue is where the latency a
     *  closed-loop generator cannot see accumulates. */
    static final class Service {
        private final BlockingQueue<Request> inbox = new ArrayBlockingQueue<>(8192);
        private final long epochNs;
        private final Thread worker;
        private final Request poison = new Request(-1, 0);
        private boolean stalled = false;

        Service(long epochNs) {
            this.epochNs = epochNs;
            this.worker = new Thread(this::serve, "service");
            this.worker.start();
        }

        void submit(Request r) {
            r.sentNs = System.nanoTime();
            try {
                inbox.put(r);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        private void serve() {
            try {
                while (true) {
                    Request r = inbox.take();
                    if (r == poison) return;
                    long elapsedMs = (System.nanoTime() - epochNs) / 1_000_000;
                    if (!stalled && elapsedMs >= STALL_AFTER_MS) {
                        stalled = true;
                        Thread.sleep(STALL_MS);   // the one bad request
                    } else {
                        Thread.sleep(SERVICE_MS);
                    }
                    r.doneNs = System.nanoTime();
                    r.settled.countDown();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        void stop() throws InterruptedException {
            inbox.put(poison);
            worker.join();
        }
    }

    record Phase(String name, int requests, List<Double> latencyMs, List<Double> iterationMs,
                 int startedInStall, int peakInFlight, long osThreadsStarted) {}

    static double percentile(List<Double> values, double q) {
        if (values.isEmpty()) return 0;
        List<Double> ordered = new ArrayList<>(values);
        Collections.sort(ordered);
        int idx = (int) Math.round(q * (ordered.size() - 1));
        return ordered.get(Math.min(idx, ordered.size() - 1));
    }

    static long osThreadsStarted() {
        return ManagementFactory.getThreadMXBean().getTotalStartedThreadCount();
    }

    static int startedInStall(List<Request> requests, long epochNs) {
        int n = 0;
        for (Request r : requests) {
            long offsetMs = (r.sentNs - epochNs) / 1_000_000;
            if (offsetMs >= STALL_AFTER_MS && offsetMs < STALL_AFTER_MS + STALL_MS) n++;
        }
        return n;
    }

    static Phase runClosedLoop() throws Exception {
        long epochNs = System.nanoTime();
        Service service = new Service(epochNs);
        List<Request> requests = Collections.synchronizedList(new ArrayList<>());
        List<Double> iterationMs = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger seq = new AtomicInteger();
        long threadsBefore = osThreadsStarted();

        List<Thread> users = new ArrayList<>();
        for (int i = 0; i < CLOSED_VUS; i++) {
            Thread t = new Thread(() -> {
                try {
                    while ((System.nanoTime() - epochNs) / 1_000_000 < RUN_MS) {
                        long iterStart = System.nanoTime();
                        Request r = new Request(seq.incrementAndGet(), System.nanoTime());
                        service.submit(r);
                        r.settled.await();      // <- this virtual user is now blocked
                        requests.add(r);
                        Thread.sleep(CLOSED_THINK_MS);
                        iterationMs.add((System.nanoTime() - iterStart) / 1e6);
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });
            users.add(t);
            t.start();
        }
        for (Thread t : users) t.join();
        service.stop();

        List<Double> latency = new ArrayList<>();
        for (Request r : requests) latency.add((r.doneNs - r.sentNs) / 1e6);
        return new Phase("closed-loop, " + CLOSED_VUS + " VUs", requests.size(), latency, iterationMs,
                startedInStall(requests, epochNs), CLOSED_VUS, osThreadsStarted() - threadsBefore);
    }

    static Phase runOpenLoop(String name, ExecutorService executor) throws Exception {
        long epochNs = System.nanoTime();
        Service service = new Service(epochNs);
        List<Request> requests = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger inFlight = new AtomicInteger();
        AtomicInteger peakInFlight = new AtomicInteger();
        long threadsBefore = osThreadsStarted();

        long intervalNs = 1_000_000_000L / OPEN_RATE_PER_SEC;
        int seq = 0;
        while (seq * intervalNs / 1_000_000 < RUN_MS) {
            long targetNs = epochNs + seq * intervalNs;
            long waitNs = targetNs - System.nanoTime();
            if (waitNs > 0) TimeUnit.NANOSECONDS.sleep(waitNs);
            seq++;
            Request r = new Request(seq, targetNs);
            int now = inFlight.incrementAndGet();
            peakInFlight.accumulateAndGet(now, Math::max);
            // One thread per in-flight request -- platform or virtual depending
            // on which executor was handed in. Everything else is identical.
            executor.submit(() -> {
                try {
                    service.submit(r);
                    r.settled.await();
                    inFlight.decrementAndGet();
                    requests.add(r);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });
        }
        executor.shutdown();
        executor.awaitTermination(30, TimeUnit.SECONDS);
        service.stop();

        // Latency from INTENDED arrival, not from when the generator got round
        // to sending. In a working open-loop generator these agree.
        List<Double> latency = new ArrayList<>();
        for (Request r : requests) latency.add((r.doneNs - r.arrivalNs) / 1e6);
        return new Phase(name, requests.size(), latency, List.of(),
                startedInStall(requests, epochNs), peakInFlight.get(),
                osThreadsStarted() - threadsBefore);
    }

    public static void main(String[] args) throws Exception {
        String bar = "=".repeat(74);
        System.out.println(bar);
        System.out.println("COORDINATED OMISSION   (Java " + System.getProperty("java.version")
                + ", single-server FIFO service)");
        System.out.println(bar);
        System.out.printf("service capacity ~%d req/s (%dms/request), offered load %d req/s%n",
                1000 / SERVICE_MS, SERVICE_MS, OPEN_RATE_PER_SEC);
        System.out.printf("one request at T+%dms takes %dms instead of %dms%n",
                STALL_AFTER_MS, STALL_MS, SERVICE_MS);
        System.out.printf("run length %dms per phase, three phases%n%n", RUN_MS);

        System.out.printf("phase 1: closed-loop (%d virtual users, %dms think time)...%n",
                CLOSED_VUS, CLOSED_THINK_MS);
        Phase closed = runClosedLoop();
        System.out.println("phase 2: open-loop, platform threads...");
        Phase openPlatform = runOpenLoop("open-loop, platform threads",
                Executors.newCachedThreadPool(Thread.ofPlatform().factory()));
        System.out.println("phase 3: open-loop, virtual threads...");
        Phase openVirtual = runOpenLoop("open-loop, virtual threads",
                Executors.newVirtualThreadPerTaskExecutor());
        System.out.println();

        System.out.printf("%-36s %12s %12s %12s%n", "",
                "CLOSED", "OPEN/platform", "OPEN/virtual");
        System.out.printf("%-36s %12d %12d %12d%n", "requests completed",
                closed.requests(), openPlatform.requests(), openVirtual.requests());
        System.out.printf("%-36s %12d %12d %12d%n", "started IN the stall window",
                closed.startedInStall(), openPlatform.startedInStall(), openVirtual.startedInStall());
        System.out.printf("%-36s %12d %12d %12d%n", "peak requests in flight",
                closed.peakInFlight(), openPlatform.peakInFlight(), openVirtual.peakInFlight());
        System.out.printf("%-36s %12d %12d %12d%n", "OS threads started by the JVM",
                closed.osThreadsStarted(), openPlatform.osThreadsStarted(),
                openVirtual.osThreadsStarted());
        System.out.println();
        double[] qs = {0.50, 0.75, 0.95, 0.99, 0.999, 1.0};
        String[] labels = {"p50", "p75", "p95", "p99", "p99.9", "max"};
        for (int i = 0; i < qs.length; i++) {
            System.out.printf("%-36s %10.1fms %10.1fms %10.1fms%n", "latency " + labels[i],
                    percentile(closed.latencyMs(), qs[i]),
                    percentile(openPlatform.latencyMs(), qs[i]),
                    percentile(openVirtual.latencyMs(), qs[i]));
        }

        System.out.println("\nColumns 2 and 3 are the same experiment run two ways. If their p99s");
        System.out.println("agree, virtual threads changed what the honest measurement COSTS and");
        System.out.println("not what it SAYS -- which is the only claim worth making for them here.");

        System.out.println("\nThe tell, inside the closed-loop run alone:");
        System.out.printf("  request duration p99   : %8.1fms%n", percentile(closed.latencyMs(), 0.99));
        System.out.printf("  iteration duration p99 : %8.1fms%n", percentile(closed.iterationMs(), 0.99));
        System.out.println("  If iteration_duration climbs while http_req_duration does not, your");
        System.out.println("  generator stopped asking. That is k6's version of this same line.");

        double c99 = percentile(closed.latencyMs(), 0.99);
        double o99 = percentile(openVirtual.latencyMs(), 0.99);
        if (c99 > 0) {
            System.out.printf("%nVERDICT: open-loop p99 is %.1fx the closed-loop p99 for the identical%n", o99 / c99);
            System.out.println("service and the identical fault.");
        }
        System.out.printf("The closed-loop generator sampled the stall %d times out of %d requests%n",
                closed.startedInStall(), closed.requests());
        System.out.printf("(%.2f%%), which is why it never reaches the 99th percentile.%n",
                100.0 * closed.startedInStall() / Math.max(1, closed.requests()));
    }
}
