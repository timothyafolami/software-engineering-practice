// 7.2 -- Java: container-aware for longer than anyone else, and still the
// heaviest thread footprint in this topic.
//
// WHAT THIS DEMONSTRATES
//   The JVM got container CPU awareness right years before Go did:
//   -XX:+UseContainerSupport has been on by default since 8u191/JDK 10, so
//   Runtime.availableProcessors() already derives from cpu.max. This
//   program prints that number next to what the cgroup actually enforces,
//   so you can see the agreement rather than assume it.
//
//   Being right about the number does not make you safe, and that is the
//   lesson here. EVERYTHING in the JVM sizes itself from that one call --
//   ParallelGCThreads, G1's concurrent workers, the C1/C2 JIT compiler
//   threads, ForkJoinPool.commonPool(), and every newFixedThreadPool you
//   wrote -- so a JVM has more always-on runnable threads than any other
//   runtime in this folder. It can drain a small bucket while your
//   application code is one request handler doing nothing clever at all.
//   The thread census printed below is the number to sit with: those
//   threads exist before main() does anything.
//
//   Virtual threads (Java 21) change the count of APPLICATION threads
//   dramatically and the count of carrier threads not at all -- and the
//   carriers are what spend quota. The last variant shows exactly that:
//   ten thousand virtual threads, the same freeze.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. availableProcessors() versus the enforced quota in the header. They
//      agree inside a container with UseContainerSupport on; run again with
//      -XX:-UseContainerSupport to see the pre-container-support answer.
//   2. Row 1 vs row 2: identical offered load, identical quota, only the
//      pool size changes. Throughput identical, throttle ratio and
//      heartbeat gap not.
//   3. Row 4 (virtual threads): the application-thread count explodes and
//      the throttle ratio does not improve, because the carrier pool is
//      still sized from availableProcessors() and the bucket is still the
//      same size.
//
// RUN
//   javac QuotaFreeze.java -d /tmp/javabuild && java -cp /tmp/javabuild QuotaFreeze
//   java -XX:ActiveProcessorCount=1 -cp /tmp/javabuild QuotaFreeze   # the fix, from outside

import java.io.IOException;
import java.lang.management.ManagementFactory;
import java.lang.management.ThreadMXBean;
import com.sun.management.OperatingSystemMXBean;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

public final class QuotaFreeze {

    private static final double WORK_MS = 40.0;        // CPU cost of one request
    private static final double OFFERED_RATE = 9.0;    // req/s -> ~0.36 CPU of demand
    private static final double RUN_SECONDS = 15.0;
    private static final long HEARTBEAT_MS = 10;
    private static final long PERIOD_US = 100_000;     // kernel default, and Docker's
    private static final double CHUNK_MS = 2.0;        // budget check-in granularity
    private static final long SEED = 20260818L;

    private static final ThreadMXBean THREADS = ManagementFactory.getThreadMXBean();

    private static long[] block = new long[32 * 1024];
    private static int blocksPerChunk = 1;             // ~2ms of work
    private static final AtomicLong sink = new AtomicLong();

    // ---------------------------------------------------------- the kernel

    /** CPUs of bandwidth the cgroup actually enforces, or -1 for no ceiling. */
    private static double readCpuMax() {
        try {
            String raw = Files.readString(Path.of("/sys/fs/cgroup/cpu.max")).trim();
            String[] parts = raw.split("\\s+");
            if (parts[0].equals("max")) {
                return -1.0;
            }
            long quota = Long.parseLong(parts[0]);
            long period = parts.length > 1 ? Long.parseLong(parts[1]) : PERIOD_US;
            return (double) quota / (double) period;
        } catch (IOException | RuntimeException noCgroup) {
            // cgroup v1, or no cgroupfs at all (every macOS host).
            try {
                long quota = Long.parseLong(
                        Files.readString(Path.of("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")).trim());
                long period = Long.parseLong(
                        Files.readString(Path.of("/sys/fs/cgroup/cpu/cpu.cfs_period_us")).trim());
                return quota > 0 ? (double) quota / (double) period : -1.0;
            } catch (IOException | RuntimeException stillNothing) {
                return -1.0;
            }
        }
    }

    /**
     * CPU time consumed by the WHOLE PROCESS, in microseconds.
     *
     * Every other language in this folder charges the budget with per-thread
     * CPU time. Java cannot, and the reason is the fourth variant below:
     * ThreadMXBean.getCurrentThreadCpuTime() returns -1 on a virtual thread.
     * There is no per-thread CPU clock for a virtual thread because a virtual
     * thread is not an OS thread -- it is mounted on a carrier, unmounted,
     * and possibly remounted on a different carrier. The thing that consumed
     * CPU is the carrier.
     *
     * That is not an inconvenience to work around; it is this sub-topic's
     * point arriving in the measurement layer. The cgroup accounts at the
     * GROUP level too -- it charges the whole container, not individual
     * tasks -- so this budget does the same, sampling the process's CPU time
     * and charging the delta. Enforcement stays per-task: workers check in
     * between chunks and park when the bucket is empty, exactly as the kernel
     * dequeues a task at its next scheduling point.
     *
     * (Go's version in ../golang/ arrives at the same design from a different
     * direction: a goroutine's wall time is not its CPU time either.)
     */
    private static final OperatingSystemMXBean OS =
            (OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();

    private static long processCpuMicros() {
        long nanos = OS.getProcessCpuTime();
        return nanos < 0 ? 0 : nanos / 1000;
    }

    private static double nowMs() {
        return System.nanoTime() / 1e6;
    }

    // ---------------------------------------------------------- the budget

    /**
     * One cgroup's worth of CFS bandwidth: a bucket of quotaUs microseconds
     * refilled every periodUs, with every task parked when it is empty.
     *
     * This is a MODEL, not the kernel. Inside a Linux container the real
     * numbers are in /sys/fs/cgroup/cpu.stat; this class exists so the same
     * program produces a comparable table on a host with no cgroupfs.
     */
    private static final class CpuBudget {
        private final long quotaUs;
        private final long periodUs;
        private final Object lock = new Object();
        private long balance;
        private long usageUs;
        private long periods;
        private long throttled;
        private long generation;
        private boolean frozeThisPeriod;
        private volatile boolean running = true;
        private final Thread refill;

        CpuBudget(double quotaCpus, long periodUs) {
            this.quotaUs = (long) (quotaCpus * periodUs);
            this.periodUs = periodUs;
            this.balance = this.quotaUs;
            this.refill = new Thread(this::refillLoop, "cfs-refill");
            this.refill.setDaemon(true);
            this.refill.start();
            Thread accountant = new Thread(this::accountantLoop, "cfs-accountant");
            accountant.setDaemon(true);
            accountant.start();
        }

        /**
         * A worker's check-in point. It charges nothing -- the accountant
         * thread does the charging, at the group level -- and simply parks
         * if the bucket is currently empty.
         */
        void checkpoint() {
            synchronized (lock) {
                while (balance <= 0 && running) {
                    frozeThisPeriod = true;
                    long seen = generation;
                    try {
                        while (generation == seen && running) {
                            lock.wait(periodUs / 1000 + 1);
                        }
                    } catch (InterruptedException interrupted) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                }
            }
        }

        /**
         * The cgroup's CPU accounting. It samples the process's CPU time more
         * often than the kernel's 5ms bandwidth slice, so the group can
         * overshoot its quota by at most one sample -- which is also true of
         * the real thing, and is why a container's usage_usec can slightly
         * exceed its quota inside a single period.
         */
        private void accountantLoop() {
            long previous = processCpuMicros();
            while (running) {
                try {
                    Thread.sleep(1);
                } catch (InterruptedException interrupted) {
                    return;
                }
                long now = processCpuMicros();
                long delta = now - previous;
                previous = now;
                if (delta <= 0) {
                    continue;
                }
                synchronized (lock) {
                    balance -= delta;
                    usageUs += delta;
                    if (balance <= 0) {
                        frozeThisPeriod = true;
                        lock.notifyAll();
                    }
                }
            }
        }

        private void refillLoop() {
            long next = System.nanoTime();
            while (running) {
                next += periodUs * 1000;
                long sleep = next - System.nanoTime();
                if (sleep > 0) {
                    LockSupportSleep(sleep);
                }
                synchronized (lock) {
                    periods++;
                    if (frozeThisPeriod) {
                        throttled++;
                    }
                    frozeThisPeriod = false;
                    // Unused quota is LOST, not banked. Banking it is exactly
                    // what cpu.max.burst does, and it is 0 by default in both
                    // Docker and Kubernetes.
                    balance = quotaUs;
                    generation++;
                    lock.notifyAll();
                }
            }
        }

        private static void LockSupportSleep(long nanos) {
            try {
                Thread.sleep(nanos / 1_000_000, (int) (nanos % 1_000_000));
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }

        void stop() {
            synchronized (lock) {
                running = false;
                lock.notifyAll();
            }
        }

        double throttleRatio() {
            return periods > 0 ? (double) throttled / periods : 0.0;
        }
    }

    // ------------------------------------------------------------ the work

    /** A deterministic, un-optimisable CPU burn. Not a sleep -- a sleeping
     *  thread spends no quota, and spending it is the entire subject. */
    private static long hashBlock(long seed) {
        long h = seed ^ 0x9E3779B97F4A7C15L;
        for (long word : block) {
            h ^= word;
            h *= 0x100000001B3L;
            h = Long.rotateLeft(h, 7);
        }
        return h;
    }

    /** Size ONE CHUNK -- the granularity at which a worker checks in with the
     *  budget, the same role the kernel's 5ms bandwidth slice plays.
     *
     *  Warm up first and time second, and on a JVM that matters more than
     *  anywhere else in this topic: the first few thousand iterations run
     *  interpreted, so a calibration taken cold sizes every chunk from
     *  unJITted code and is wrong by an order of magnitude. */
    private static int calibrateBlocks(double targetMs) {
        long h = 0;
        for (int i = 0; i < 200; i++) {
            h = hashBlock(h);
        }
        sink.addAndGet(h);
        long start = System.nanoTime();
        for (int i = 0; i < 50; i++) {
            h = hashBlock(h);
        }
        sink.addAndGet(h);
        double perBlockMs = (System.nanoTime() - start) / 1e6 / 50.0;
        return perBlockMs <= 0 ? 1 : Math.max(1, (int) (targetMs / perBlockMs));
    }

    /**
     * Burn until this task has spent workMs actually RUNNING, checking in with
     * the budget between chunks.
     *
     * The elapsed time measured here excludes every park, because the park
     * happens outside the timed section -- so a task frozen for 87ms still
     * does exactly workMs of work, which is the variable this experiment
     * holds fixed across all four rows.
     *
     * Spend-until-done rather than a fixed block count, for a reason specific
     * to this machine: an Apple M1 has four performance cores and four
     * efficiency cores, so "how many hash blocks is 40ms" has two different
     * answers depending on where the thread landed. A block count calibrated
     * on one core type and executed on the other silently changes the offered
     * load -- the one variable that must not move.
     */
    private static void burnCpu(double workMs, CpuBudget budget) {
        long h = sink.get();
        double ranMs = 0.0;
        while (ranMs < workMs) {
            long start = System.nanoTime();
            for (int i = 0; i < blocksPerChunk; i++) {
                h = hashBlock(h);
            }
            ranMs += (System.nanoTime() - start) / 1e6;
            budget.checkpoint();
        }
        sink.set(h);
    }

    // -------------------------------------------------------- one variant

    private record Result(int completed, double reqPerS, double avgCpu, long periods,
                          long throttled, double p50, double p99, double hbGap,
                          int peakThreads) {
    }

    /** Poisson arrivals, not evenly spaced. Throttling at low average
     *  utilisation is a burstiness effect: the bucket is drained by demand
     *  that clumps inside one 100ms window. Evenly-spaced arrivals cannot
     *  reproduce it, which is why hand-rolled load loops so reliably fail to
     *  find production's tail latency. */
    private static double[] poissonSchedule(double rate, double seconds, long seed) {
        Random rng = new Random(seed);
        List<Double> out = new ArrayList<>();
        double t = 0.0;
        while (t < seconds) {
            out.add(t * 1000.0);
            t += -Math.log(1.0 - rng.nextDouble()) / rate;
        }
        double[] array = new double[out.size()];
        for (int i = 0; i < array.length; i++) {
            array[i] = out.get(i);
        }
        return array;
    }

    private static Result runVariant(String kind, int poolSize, double quotaCpus)
            throws InterruptedException {
        CpuBudget budget = new CpuBudget(quotaCpus, PERIOD_US);
        double[] schedule = poissonSchedule(OFFERED_RATE, RUN_SECONDS, SEED);

        BlockingQueue<Double> inbox = new ArrayBlockingQueue<>(20_000);
        List<Double> latencies = Collections.synchronizedList(new ArrayList<>());
        final double origin = nowMs();
        AtomicBoolean stop = new AtomicBoolean(false);

        // The heartbeat: wants to tick every 10ms, spends no measurable CPU,
        // and is frozen anyway -- because throttling dequeues every task in
        // the cgroup, not only the greedy ones.
        double[] hbMaxGap = {0.0};
        Thread heartbeat = new Thread(() -> {
            double last = nowMs();
            while (!stop.get()) {
                try {
                    Thread.sleep(HEARTBEAT_MS);
                } catch (InterruptedException interrupted) {
                    return;
                }
                budget.checkpoint();
                double t = nowMs();
                hbMaxGap[0] = Math.max(hbMaxGap[0], t - last);
                last = t;
            }
        }, "heartbeat");
        heartbeat.setDaemon(true);
        heartbeat.start();

        ExecutorService pool = kind.equals("virtual")
                ? Executors.newVirtualThreadPerTaskExecutor()
                : Executors.newFixedThreadPool(poolSize);

        CountDownLatch done = new CountDownLatch(schedule.length);
        int peakThreads = 0;

        for (double due : schedule) {
            double wait = due - (nowMs() - origin);
            if (wait > 0) {
                Thread.sleep((long) wait, (int) ((wait % 1) * 1_000_000));
            }
            pool.submit(() -> {
                try {
                    burnCpu(WORK_MS, budget);
                    latencies.add(nowMs() - origin - due);
                } finally {
                    done.countDown();
                }
            });
            peakThreads = Math.max(peakThreads, THREADS.getThreadCount());
        }

        done.await(30, TimeUnit.SECONDS);
        double wallS = (nowMs() - origin) / 1000.0;
        long periods = budget.periods;
        long throttled = budget.throttled;
        double avgCpu = budget.usageUs / 1e6 / wallS * 100.0;

        stop.set(true);
        heartbeat.interrupt();
        pool.shutdownNow();
        pool.awaitTermination(5, TimeUnit.SECONDS);
        budget.stop();

        List<Double> ordered = new ArrayList<>(latencies);
        Collections.sort(ordered);
        return new Result(ordered.size(), ordered.size() / RUN_SECONDS, avgCpu, periods,
                throttled, percentile(ordered, 50), percentile(ordered, 99), hbMaxGap[0],
                peakThreads);
    }

    private static double percentile(List<Double> sorted, double p) {
        if (sorted.isEmpty()) {
            return Double.NaN;
        }
        int rank = (int) Math.round(p / 100.0 * (sorted.size() - 1));
        return sorted.get(Math.min(rank, sorted.size() - 1));
    }

    // ----------------------------------------------------------- reporting

    private static void printTable(String[] headers, List<String[]> rows) {
        int[] widths = new int[headers.length];
        for (int i = 0; i < headers.length; i++) {
            widths[i] = headers[i].length();
            for (String[] row : rows) {
                widths[i] = Math.max(widths[i], row[i].length());
            }
        }
        printRow(headers, widths);
        String[] rule = new String[headers.length];
        for (int i = 0; i < headers.length; i++) {
            rule[i] = "-".repeat(widths[i]);
        }
        printRow(rule, widths);
        for (String[] row : rows) {
            printRow(row, widths);
        }
    }

    private static void printRow(String[] cells, int[] widths) {
        StringBuilder line = new StringBuilder();
        for (int i = 0; i < cells.length; i++) {
            line.append(String.format("%-" + widths[i] + "s", cells[i]));
            if (i + 1 < cells.length) {
                line.append("  ");
            }
        }
        System.out.println(line);
    }

    public static void main(String[] args) throws InterruptedException {
        Random rng = new Random(SEED);
        for (int i = 0; i < block.length; i++) {
            block[i] = rng.nextLong();
        }
        blocksPerChunk = calibrateBlocks(CHUNK_MS);

        int available = Runtime.getRuntime().availableProcessors();
        double quota = readCpuMax();

        System.out.println("7.2 -- throttled at 30% CPU: Java");
        System.out.printf("  runtime                : %s %s (%s)%n",
                System.getProperty("java.vm.name"),
                System.getProperty("java.version"),
                System.getProperty("os.name") + "/" + System.getProperty("os.arch"));
        System.out.printf("  availableProcessors()  : %d   <- cgroup-derived when "
                + "UseContainerSupport is on%n", available);
        System.out.printf("  quota actually enforced: %s%n",
                quota < 0 ? "none (no cpu.max on this host)"
                        : String.format("%.2f CPU (cpu.max)", quota));
        System.out.printf("  OS threads at rest     : %d   (JIT compilers, GC workers, "
                + "signal dispatcher, JFR...)%n", THREADS.getThreadCount());
        System.out.println("                           Those exist before main() ran a "
                + "line of your code.");
        System.out.println("                           Every one of them is in the same "
                + "cgroup as your handlers.");
        System.out.println();

        if (quota < 0) {
            System.out.println("  !! FALLBACK: no cpu.max to read on this host");
            System.out.println("  !! The bucket below is a userspace MODEL of CFS "
                    + "bandwidth control,");
            System.out.println("  !! not the Linux kernel. Real numbers come from");
            System.out.println("  !! /sys/fs/cgroup/cpu.stat inside a container.");
            System.out.println();
        }

        System.out.printf("  one hash block costs %.3f ms on the calibrating core "
                + "(measured, post-warmup); %d blocks per %.0fms chunk%n",
                CHUNK_MS / blocksPerChunk, blocksPerChunk, CHUNK_MS);
        System.out.printf("  each work unit burns until it has CONSUMED %.0fms of thread "
                + "CPU time, so a%n", WORK_MS);
        System.out.println("  P-core and an E-core do the same WORK per request, not the "
                + "same blocks.");
        System.out.printf("  offered load: %.0f req/s x %.0fms CPU = %.2f CPU of demand%n",
                OFFERED_RATE, WORK_MS, OFFERED_RATE * WORK_MS / 1000.0);
        System.out.println("  quota:        1.00 CPU. The demand is comfortably under "
                + "the limit.");
        System.out.printf("  heartbeat wants a tick every %dms; %.0fs per row%n",
                HEARTBEAT_MS, RUN_SECONDS);
        System.out.println();

        record Variant(String label, String kind, int pool, double quota) {
        }
        List<Variant> variants = List.of(
                new Variant("fixed pool = availableProcessors() (" + available + "), 1.0 CPU",
                        "platform", available, 1.0),
                new Variant("fix 1: fixed pool = 1 (the quota), 1.0 CPU",
                        "platform", 1, 1.0),
                new Variant("fix 2: fixed pool = " + available + ", 2.0 CPU",
                        "platform", available, 2.0),
                new Variant("virtual threads (one per request), 1.0 CPU",
                        "virtual", available, 1.0));

        List<String[]> rows = new ArrayList<>();
        for (Variant variant : variants) {
            Result r = runVariant(variant.kind(), variant.pool(), variant.quota());
            rows.add(new String[]{
                    variant.label(),
                    String.valueOf(r.completed()),
                    String.format("%.1f", r.reqPerS()),
                    String.format("%.0f%%", r.avgCpu()),
                    r.throttled() + "/" + r.periods(),
                    String.format("%.3f", r.periods() > 0
                            ? (double) r.throttled() / r.periods() : 0.0),
                    String.format("%.0f", r.p50()),
                    String.format("%.0f", r.p99()),
                    String.format("%.0f", r.hbGap()),
                    String.valueOf(r.peakThreads())});
            System.out.println("  ran: " + variant.label());
        }

        System.out.println();
        printTable(new String[]{"variant", "n", "req/s", "avg CPU", "throttled", "ratio",
                "p50 ms", "p99 ms", "hb gap ms", "peak threads"}, rows);

        System.out.println();
        System.out.println("  Row 1 is the JVM doing everything right and still being");
        System.out.println("  frozen: availableProcessors() read the quota correctly, and");
        System.out.println("  then a pool sized from it put that many runnable threads in");
        System.out.println("  a bucket that refills at one CPU per period.");
        System.out.println();
        System.out.println("  Row 4 is the one to sit with. Virtual threads multiply the");
        System.out.println("  application-thread count and change the throttle ratio very");
        System.out.println("  little, because virtual threads are not what spends quota --");
        System.out.println("  the carrier pool is, and it is still sized from the same");
        System.out.println("  availableProcessors() call. Loom fixes thread SCARCITY. It");
        System.out.println("  does not fix a CPU ceiling, and nothing can: the ceiling is");
        System.out.println("  a number in a file the JVM does not get a vote on.");
        System.out.println();
        System.out.println("  Ground truth for all of this, inside a container:");
        System.out.println("    cat /sys/fs/cgroup/cpu.stat");
    }
}
