// Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.
//
// WHAT THIS DEMONSTRATES: Java is the canonical version of this hazard and the
// one Kleppmann drew -- a stop-the-world GC pause that exceeds a lease TTL. It is
// also the one where you cannot point at a line in your own code: the pause
// belongs to the collector.
//
// This program does NOT assert that a pause happened. It MEASURES the longest
// gap between renewals under an allocation storm and reports it against the TTL,
// on whatever collector and heap you gave it. Whether a 2g heap on this machine
// can produce a ten-second stop-the-world is a question about your JVM, not
// something a program should tell you it knows in advance. Run it on several
// collectors and compare; that comparison is the exercise.
//
// Three runs:
//   1. allocation storm, most objects dying young    (cheap for a generational GC)
//   2. allocation storm with a LARGE LIVE SET        (expensive: survivors must
//                                                     be traced and copied)
//   3. run 2 plus forced full collections            (System.gc(), the worst case)
//
// And one Java-specific mechanism worth knowing alongside GC: a thread can only
// be paused at a SAFEPOINT. A long counted loop with no safepoint poll DELAYS the
// stop-the-world the JVM is trying to take, so the actual stop is longer than the
// collector's own accounting suggests. Run 4 measures time-to-safepoint directly.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the longest gap per run, and the instruction at
// the bottom for re-running with -Xlog:gc so you can put a collector pause next
// to each renewal gap rather than inferring it.
//
//   javac java/PauseAudit.java -d /tmp/javabuild && java -Xmx2g -cp /tmp/javabuild PauseAudit
//   java -Xmx2g -XX:+UseSerialGC -Xlog:gc -cp /tmp/javabuild PauseAudit
//   java -Xmx2g -XX:+UseZGC       -Xlog:gc -cp /tmp/javabuild PauseAudit

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public final class PauseAudit {

    private static final long LEASE_TTL_MS = 10_000;
    private static final long RENEW_INTERVAL_MS = 1_000;
    private static final long HAZARD_MS = 12_000;

    /** Renewal gaps on System.nanoTime(): monotonic, and valid within this JVM,
     *  which is exactly the scope of the question. currentTimeMillis() here would
     *  let an NTP step invent a pause that never happened -- see Topic 3. */
    static final class Renewals {
        private final List<Long> gapsNanos = new ArrayList<>();
        private long last = System.nanoTime();

        synchronized void tick() {
            long now = System.nanoTime();
            gapsNanos.add(now - last);
            last = now;
        }

        synchronized double longestSeconds() {
            return gapsNanos.stream().mapToLong(Long::longValue).max().orElse(0L) / 1e9;
        }
    }

    private static Thread startKeepalive(Renewals r, AtomicBoolean stop) {
        Thread t = new Thread(() -> {
            while (!stop.get()) {
                try {
                    Thread.sleep(RENEW_INTERVAL_MS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                r.tick();
            }
        }, "lease-keepalive");
        // A platform thread, deliberately. A virtual thread would be unmounted
        // during Thread.sleep and remounted after -- and a stop-the-world pause
        // stops the carrier too, so it would change nothing here. Worth knowing:
        // virtual threads fix Layer 1's blocking-call problem and do NOT fix
        // this one, because the collector stops every thread there is.
        t.setDaemon(true);
        t.start();
        return t;
    }

    private static long gcCount() {
        long n = 0;
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
            long c = b.getCollectionCount();
            if (c > 0) n += c;
        }
        return n;
    }

    /** Bytes allocated by THIS thread, from the JVM's own accounting.
     *
     *  Counting `new byte[n]` in the loop and summing n is the obvious thing and
     *  it lies: escape analysis can scalar-replace an array that never escapes,
     *  so the loop runs far more iterations than it allocates and you report a
     *  throughput no memory bus could deliver. The first draft of this file
     *  printed 140,000 GiB in twelve seconds, which is exactly the kind of
     *  unmeasured number this repo exists to stop shipping. Ask the JVM instead.
     *
     *  Returns -1 where the com.sun extension is unavailable, and the caller
     *  prints NaN rather than a plausible-looking zero.
     */
    private static long allocatedBytes() {
        java.lang.management.ThreadMXBean bean = ManagementFactory.getThreadMXBean();
        if (bean instanceof com.sun.management.ThreadMXBean sun
                && sun.isThreadAllocatedMemorySupported()) {
            return sun.getCurrentThreadAllocatedBytes();
        }
        return -1;
    }

    private static long gcMillis() {
        long ms = 0;
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
            long t = b.getCollectionTime();
            if (t > 0) ms += t;
        }
        return ms;
    }

    /** The hazard. Churn plus, optionally, a large live set the collector has to
     *  keep tracing and copying -- which is what turns a young-generation
     *  collection into an expensive one. */
    private static int allocationStorm(long millis, int liveSetMb, boolean forceFull) {
        List<byte[]> live = new ArrayList<>();
        for (int i = 0; i < liveSetMb; i++) {
            live.add(new byte[1024 * 1024]);
        }
        long deadline = System.nanoTime() + millis * 1_000_000L;
        int sink = 0;
        while (System.nanoTime() < deadline) {
            for (int i = 0; i < 2_000; i++) {
                byte[] garbage = new byte[8 * 1024];
                garbage[0] = (byte) i;
                sink += garbage[0];
            }
            if (forceFull) {
                // System.gc() is a request, not a command, and on some
                // collectors it is a no-op. It is here because the worst case is
                // worth measuring and this is the only portable way to ask.
                System.gc();
            }
            // Keep the live set alive AND churning, so survivors move.
            if (!live.isEmpty()) {
                live.set(sink < 0 ? 0 : Math.floorMod(sink, live.size()),
                        new byte[1024 * 1024]);
            }
        }
        return sink;
    }

    private static boolean run(String label, int liveSetMb, boolean forceFull) {
        Renewals r = new Renewals();
        AtomicBoolean stop = new AtomicBoolean(false);
        Thread keepalive = startKeepalive(r, stop);
        sleepQuietly(2 * RENEW_INTERVAL_MS);

        long gc0 = gcCount();
        long gcMs0 = gcMillis();
        long bytes0 = allocatedBytes();
        long t0 = System.nanoTime();
        int sink = allocationStorm(HAZARD_MS, liveSetMb, forceFull);
        long allocated = allocatedBytes() - bytes0;
        double took = (System.nanoTime() - t0) / 1e9;
        long collections = gcCount() - gc0;
        long collectionMs = gcMillis() - gcMs0;

        sleepQuietly(2 * RENEW_INTERVAL_MS);
        stop.set(true);
        keepalive.interrupt();

        double longest = r.longestSeconds();
        boolean lost = longest * 1000 > LEASE_TTL_MS;
        System.out.printf("  %-34s%8.2fs    %8.2fs    %-16s%6d colls %7d ms  %5.1f GiB%n",
                label, longest, took, lost ? "LOST THE LEASE" : "held",
                collections, collectionMs, allocated < 0 ? Double.NaN
                        : allocated / (1024.0 * 1024 * 1024));
        if (sink == Integer.MIN_VALUE) {
            System.out.print("");   // keep the loop from being optimised away entirely
        }
        return lost;
    }

    /** Time-to-safepoint. A counted int loop is not safepoint-polled by C2, so a
     *  thread inside one cannot be stopped until it finishes -- the JVM asks, and
     *  waits. That delay is added to every stop-the-world pause, and it does not
     *  appear in the collector's own timing. */
    private static void safepointNote() {
        Renewals r = new Renewals();
        AtomicBoolean stop = new AtomicBoolean(false);
        Thread keepalive = startKeepalive(r, stop);
        sleepQuietly(2 * RENEW_INTERVAL_MS);

        long t0 = System.nanoTime();
        long acc = 0;
        for (int i = 0; i < Integer.MAX_VALUE; i++) {   // counted int loop: no poll
            acc += i ^ (acc >>> 3);
        }
        double took = (System.nanoTime() - t0) / 1e9;
        sleepQuietly(2 * RENEW_INTERVAL_MS);
        stop.set(true);
        keepalive.interrupt();
        System.out.printf("  %-34s%8.2fs    %8.2fs    %-16s%n",
                "counted loop (no safepoint poll)", r.longestSeconds(), took,
                r.longestSeconds() * 1000 > LEASE_TTL_MS ? "LOST THE LEASE" : "held");
        if (acc == 42) {
            System.out.print("");   // keep the loop
        }
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    public static void main(String[] args) {
        long maxHeapMb = Runtime.getRuntime().maxMemory() / (1024 * 1024);
        System.out.println("==============================================================================");
        System.out.println("Layer 4 Topic 7 -- Java pause audit");
        System.out.println("==============================================================================");
        System.out.printf("  Java %s (%s) on %s %s%n", System.getProperty("java.version"),
                System.getProperty("java.vm.name"), System.getProperty("os.name"),
                System.getProperty("os.arch"));
        System.out.printf("  max heap %d MiB, collectors:", maxHeapMb);
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
            System.out.printf(" %s", b.getName());
        }
        System.out.printf("%n  lease TTL %ds, renewal every %ds, hazard %ds%n",
                LEASE_TTL_MS / 1000, RENEW_INTERVAL_MS / 1000, HAZARD_MS / 1000);
        System.out.println("  clock : System.nanoTime(), valid within this JVM, which is the scope");
        System.out.println("  thread: a PLATFORM thread -- a virtual one would not help, because a");
        System.out.println("          stop-the-world pause stops its carrier too");
        System.out.println();
        System.out.printf("  %-34s%9s    %9s    %-16s%n", "run", "longest gap", "hazard took", "verdict");

        int liveSetMb = (int) Math.min(600, Math.max(64, maxHeapMb / 3));
        boolean lost = false;
        lost |= run("churn, objects die young", 0, false);
        lost |= run("churn + " + liveSetMb + " MiB live set", liveSetMb, false);
        lost |= run("+ forced full collections", liveSetMb, true);
        safepointNote();

        System.out.println();
        System.out.println("  These numbers describe THIS collector on THIS heap. They are not a");
        System.out.println("  claim about Java. Re-run with the flags below and put a collector");
        System.out.println("  pause next to each renewal gap rather than inferring one:");
        System.out.println();
        System.out.println("    java -Xmx2g -XX:+UseSerialGC   -Xlog:gc -cp /tmp/javabuild PauseAudit");
        System.out.println("    java -Xmx2g -XX:+UseParallelGC -Xlog:gc -cp /tmp/javabuild PauseAudit");
        System.out.println("    java -Xmx8g -XX:+UseG1GC       -Xlog:gc -cp /tmp/javabuild PauseAudit");
        System.out.println("    java -Xmx8g -XX:+UseZGC        -Xlog:gc -cp /tmp/javabuild PauseAudit");
        System.out.println();
        System.out.println("  The comparison IS the exercise. A modern low-pause collector on a");
        System.out.println("  small heap may never come near a 10s TTL, and that is a real answer --");
        System.out.println("  but it is an answer about a configuration, and the configuration is");
        System.out.println("  one JVM flag and one heap-size change away from a different one.");
        System.out.println();
        if (lost) {
            System.out.println("  A run exceeded the TTL. Note that no line in this program caused it:");
            System.out.println("  the pause belongs to the collector, which is what makes this hazard");
            System.out.println("  different from Python's and Node's. There is nothing to un-block.");
        } else {
            System.out.println("  No run exceeded the TTL on this configuration. Do not read that as");
            System.out.println("  'Java is safe here'. Raise the heap, switch to a throughput collector,");
            System.out.println("  and add a real live set from your own service -- Kleppmann's canonical");
            System.out.println("  example is a large heap under a collector that stops the world, and");
            System.out.println("  the gap between that and this default is entirely configuration.");
        }
        System.out.println();
        System.out.println("  Fencing is what makes this survivable either way. A stale holder that");
        System.out.println("  resumes must be REJECTED BY THE RESOURCE -- `AND fence < $epoch` in the");
        System.out.println("  UPDATE -- because you cannot tune away a pause you did not schedule.");
        System.exit(lost ? 1 : 0);
    }
}
