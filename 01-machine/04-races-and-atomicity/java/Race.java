// Layer 1 - Why `counter++` is not atomic, Java version.
// A plain `long` field has no atomicity or visibility guarantees across
// threads under the Java Memory Model unless it's `volatile` (visibility
// only, not atomicity for read-modify-write) or you use
// java.util.concurrent.atomic / synchronized (both). Java's real threads
// and its JIT don't get to quietly optimize this race away the way Rust's
// and (likely) C++'s LLVM-based toolchains might -- see this experiment's
// result against those two.
import java.util.concurrent.atomic.AtomicLong;

public class Race {
    static final int THREADS = 8;
    static final long INCREMENTS = 300_000;

    static long counter = 0;

    static long runUnsafe() throws InterruptedException {
        counter = 0;
        Thread[] threads = new Thread[THREADS];
        for (int i = 0; i < THREADS; i++) {
            threads[i] = new Thread(() -> {
                for (long j = 0; j < INCREMENTS; j++) {
                    counter++; // racy: not synchronized
                }
            });
            threads[i].start();
        }
        for (Thread t : threads) t.join();
        return counter;
    }

    static long runSynchronized() throws InterruptedException {
        counter = 0;
        Object lock = new Object();
        Thread[] threads = new Thread[THREADS];
        for (int i = 0; i < THREADS; i++) {
            threads[i] = new Thread(() -> {
                for (long j = 0; j < INCREMENTS; j++) {
                    synchronized (lock) {
                        counter++;
                    }
                }
            });
            threads[i].start();
        }
        for (Thread t : threads) t.join();
        return counter;
    }

    static long runAtomic() throws InterruptedException {
        AtomicLong atomicCounter = new AtomicLong(0);
        Thread[] threads = new Thread[THREADS];
        for (int i = 0; i < THREADS; i++) {
            threads[i] = new Thread(() -> {
                for (long j = 0; j < INCREMENTS; j++) {
                    atomicCounter.incrementAndGet();
                }
            });
            threads[i].start();
        }
        for (Thread t : threads) t.join();
        return atomicCounter.get();
    }

    public static void main(String[] args) throws InterruptedException {
        long expected = (long) THREADS * INCREMENTS;
        long unsafeResult = runUnsafe();
        long syncResult = runSynchronized();
        long atomicResult = runAtomic();
        System.out.printf("expected:                %d%n", expected);
        System.out.printf("unsafe (counter++):      %d  (lost %d)%n", unsafeResult, expected - unsafeResult);
        System.out.printf("safe (synchronized):     %d%n", syncResult);
        System.out.printf("safe (AtomicLong):       %d%n", atomicResult);
    }
}
