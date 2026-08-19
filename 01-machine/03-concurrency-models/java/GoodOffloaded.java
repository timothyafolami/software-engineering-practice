// Layer 1 - The fix, Java version -- and the closest thing in this whole
// lab to Go's story. Since Java 21, virtual threads (JEP 444, Project
// Loom) give you cheap, JVM-scheduled threads: potentially millions of
// them, multiplexed onto a small pool of real "carrier" OS threads. The
// key mechanism that matters here: when a virtual thread calls a blocking
// operation the JDK knows about (Thread.sleep, blocking IO, most
// java.util.concurrent primitives), the JVM automatically UNMOUNTS it from
// its carrier thread, freeing that carrier to run a different virtual
// thread, then remounts it (possibly on a different carrier) when the
// blocking call is ready to proceed. That's a different mechanism than
// Go's netpoller, but the same idea: "blocking" code doesn't have to mean
// "blocking a scarce shared thread." No code changes are needed here
// beyond which executor factory method you call.
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class GoodOffloaded {
    static final long TICK_INTERVAL_MS = 100;
    static final long BLOCK_FOR_MS = 1000;
    static final long LEAD_IN_MS = 200;
    static final long LEAD_OUT_MS = 200;

    public static void main(String[] args) throws Exception {
        // The only change from BadBlocking.java: a virtual-thread-per-task
        // executor instead of a fixed pool of one platform thread.
        ExecutorService pool = Executors.newVirtualThreadPerTaskExecutor();
        List<Long> timestamps = Collections.synchronizedList(new ArrayList<>());
        long start = System.nanoTime();

        Thread timer = Thread.ofVirtual().start(() -> {
            try {
                while (!Thread.currentThread().isInterrupted()) {
                    Thread.sleep(TICK_INTERVAL_MS);
                    pool.submit(() -> timestamps.add(System.nanoTime() - start));
                }
            } catch (InterruptedException ignored) {
            }
        });

        Thread.sleep(LEAD_IN_MS);

        pool.submit(() -> {
            try {
                Thread.sleep(BLOCK_FOR_MS);
            } catch (InterruptedException ignored) {
            }
        }).get();

        Thread.sleep(LEAD_OUT_MS);
        timer.interrupt();
        pool.shutdownNow();

        report(timestamps, start);
    }

    static void report(List<Long> timestamps, long start) {
        double elapsed = (System.nanoTime() - start) / 1e9;
        double maxGap = timestamps.isEmpty() ? elapsed : timestamps.get(0) / 1e9;
        for (int i = 1; i < timestamps.size(); i++) {
            double gap = (timestamps.get(i) - timestamps.get(i - 1)) / 1e9;
            if (gap > maxGap) maxGap = gap;
        }
        System.out.printf("[good] ticks counted: %d  over %.2fs  (expected ~%.0f if never blocked)  max gap between ticks: %.2fs%n",
                timestamps.size(), elapsed, elapsed / (TICK_INTERVAL_MS / 1000.0), maxGap);
    }
}
