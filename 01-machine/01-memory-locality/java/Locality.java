// Layer 1 - Memory & cache locality, Java version.
// Same pointer-chasing benchmark. Java adds a variable the other languages
// don't have to think about: the JIT. HotSpot interprets bytecode at
// first, then compiles hot methods to native code after enough
// invocations -- so a naive "time it once" benchmark partly measures
// warm-up, not steady-state performance. We run a small warm-up pass
// before the timed one specifically to let the JIT kick in first, the way
// a real benchmarking library (JMH) would insist on.
import java.util.Random;

public class Locality {
    static final int N = 2_000_000_000;
    static final int LAPS = 5;

    static int[] values;
    static int[] next;

    static void build(boolean shuffled) {
        values = new int[N];
        next = new int[N];
        for (int i = 0; i < N; i++) values[i] = i;

        if (!shuffled) {
            for (int i = 0; i < N; i++) next[i] = (i + 1) % N;
            return;
        }
        int[] perm = new int[N];
        for (int i = 0; i < N; i++) perm[i] = i;
        Random rng = new Random(42);
        for (int i = N - 1; i > 0; i--) {
            int j = rng.nextInt(i + 1);
            int tmp = perm[i];
            perm[i] = perm[j];
            perm[j] = tmp;
        }
        for (int i = 0; i < N; i++) {
            next[perm[i]] = perm[(i + 1) % N];
        }
    }

    static long traverse(long laps) {
        long total = 0;
        int idx = 0;
        long steps = (long) N * laps;
        for (long s = 0; s < steps; s++) {
            total += values[idx];
            idx = next[idx];
        }
        return total;
    }

    static void bench(String label, boolean shuffled, boolean warmup) {
        build(shuffled);
        if (warmup) {
            traverse(1); // let the JIT see this loop before we time it
        }
        long start = System.nanoTime();
        long total = traverse(LAPS);
        long elapsed = System.nanoTime() - start;
        double elapsedS = elapsed / 1e9;
        double nsPerStep = (double) elapsed / (N * LAPS);
        System.out.printf("%-10s  total=%15d  time=%6.3fs  %6.1f ns/step%n", label, total, elapsedS, nsPerStep);
    }

    public static void main(String[] args) {
        System.out.printf("N=%d laps=%d (Java %s, after JIT warm-up)%n", N, LAPS, System.getProperty("java.version"));
        bench("sequential", false, true);
        bench("shuffled", true, true);
    }
}
