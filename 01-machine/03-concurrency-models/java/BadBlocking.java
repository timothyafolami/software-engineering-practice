// Layer 1 - Java's traditional concurrency model: a fixed-size platform
// thread pool. A single-threaded ExecutorService has the exact same
// failure mode as C++'s one-worker pool, Python's asyncio, or naive Node:
// submit a blocking call to the same pool the ticker uses, and the ticker
// cannot run until it finishes. This is the model every Java service used
// before virtual threads existed (see GoodOffloaded.java for the modern
// answer), and it's still what you get from Executors.newFixedThreadPool
// today unless you specifically reach for virtual threads.
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public class BadBlocking {
    static final long TICK_INTERVAL_MS = 100;
    static final long BLOCK_FOR_MS = 1000;
    static final long LEAD_IN_MS = 200;
    static final long LEAD_OUT_MS = 200;

    public static void main(String[] args) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(1); // one worker -- the failure mode, on purpose
        List<Long> timestamps = Collections.synchronizedList(new ArrayList<>());
        long start = System.nanoTime();

        Thread timer = new Thread(() -> {
            try {
                while (!Thread.currentThread().isInterrupted()) {
                    Thread.sleep(TICK_INTERVAL_MS);
                    pool.submit(() -> timestamps.add(System.nanoTime() - start));
                }
            } catch (InterruptedException ignored) {
            }
        });
        timer.start();

        Thread.sleep(LEAD_IN_MS);

        // BAD: blocking call submitted to the SAME single-threaded pool as
        // the ticker's tasks.
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
        System.out.printf("[bad] ticks counted: %d  over %.2fs  (expected ~%.0f if never blocked)  max gap between ticks: %.2fs%n",
                timestamps.size(), elapsed, elapsed / (TICK_INTERVAL_MS / 1000.0), maxGap);
    }
}
