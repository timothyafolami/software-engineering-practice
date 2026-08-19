// Layer 10 - Topic 4: the batch-invariance finding, on a CPU. (Java)
//
// What this demonstrates
//     The same mechanism as golang/parallel_sum.go, through Java's own
//     parallel reduction. Ten million values, summed three ways:
//
//       sequential            DoubleStream.sum(), one thread
//       parallel              DoubleStream.parallel().sum(), where the
//                             result depends on how the SPLITERATOR chose
//                             to divide the work -- a decision made by the
//                             framework, based on the common ForkJoinPool
//                             and the input size
//       explicit W-way        the same partitioning written by hand, for
//                             W = 1..64, so you can see the answer move
//                             with W rather than with the weather
//
//     Note that DoubleStream.sum() is not a naive loop: it carries a
//     compensation term (Kahan summation), which is why its sequential
//     answer is closer to exact than a hand-written loop. That is worth
//     knowing before you conclude anything from a comparison against one.
//
// Worth knowing the history, because it is precisely this topic's journey:
//     `strictfp` used to be a keyword you needed for reproducible results,
//     because an x87 FPU could keep intermediates at 80 bits and the answer
//     depended on the host. JEP 306 (Java 17) made all floating-point
//     strict by default. So Java moved from "your result depends on the
//     hardware" to "your result depends only on your partitioning" -- which
//     is the exact problem this topic is about, and the reason the
//     remaining nondeterminism is now entirely yours.
//
// What to look for
//     - `distinct` across the explicit partitionings. More than one means
//       a pure function of a fixed input gave different answers depending
//       on how it was divided.
//     - Double.toHexString, not %f. Two sums that print identically in
//       decimal can differ in the bits that matter.
//     - The float32 section. Repeat the same experiment at float precision
//       and the spread grows by orders of magnitude -- which is the regime
//       an inference kernel actually runs in.
//
// No dependencies. Runs with no arguments:
//     cd java && javac ParallelSum.java -d /tmp/javabuild \
//       && java -cp /tmp/javabuild ParallelSum

import java.util.Arrays;
import java.util.HashSet;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.ForkJoinPool;
import java.util.stream.DoubleStream;

public class ParallelSum {
    static final int N = 10_000_000;
    static final long SEED = 20260818L;
    static final int[] WORKERS = {1, 2, 4, 8, 16, 32, 64};

    /** Sum in `w` chunks, each chunk summed in order, partials added in order. */
    static double sumPartitioned(double[] data, int w) {
        if (w <= 1) {
            double s = 0;
            for (double v : data) s += v;
            return s;
        }
        int chunk = (data.length + w - 1) / w;
        double[] partials = new double[w];
        ForkJoinPool.commonPool().submit(() ->
            java.util.stream.IntStream.range(0, w).parallel().forEach(i -> {
                int lo = i * chunk;
                int hi = Math.min(data.length, lo + chunk);
                double s = 0;
                for (int j = lo; j < hi; j++) s += data[j];
                partials[i] = s;
            })).join();
        double total = 0;
        for (double p : partials) total += p;   // in index order: deterministic per w
        return total;
    }

    static float sumPartitionedFloat(float[] data, int w) {
        if (w <= 1) {
            float s = 0;
            for (float v : data) s += v;
            return s;
        }
        int chunk = (data.length + w - 1) / w;
        float[] partials = new float[w];
        ForkJoinPool.commonPool().submit(() ->
            java.util.stream.IntStream.range(0, w).parallel().forEach(i -> {
                int lo = i * chunk;
                int hi = Math.min(data.length, lo + chunk);
                float s = 0;
                for (int j = lo; j < hi; j++) s += data[j];
                partials[i] = s;
            })).join();
        float total = 0;
        for (float p : partials) total += p;
        return total;
    }

    public static void main(String[] args) {
        Random rng = new Random(SEED);
        double[] data = new double[N];
        float[] dataF = new float[N];
        for (int i = 0; i < N; i++) {
            double v = rng.nextDouble() + 0.5;   // ~U(0.5, 1.5)
            data[i] = v;
            dataF[i] = (float) v;
        }

        System.out.println("Partition-order nondeterminism -- Java "
                + Runtime.version().feature() + ", no GPU involved");
        System.out.printf("  %d values ~U(0.5, 1.5), seed %d%n", N, SEED);
        System.out.printf("  common ForkJoinPool parallelism: %d%n%n",
                ForkJoinPool.getCommonPoolParallelism());

        double seq = DoubleStream.of(data).sum();
        double par = DoubleStream.of(data).parallel().sum();
        System.out.println("  DoubleStream (Kahan-compensated inside the JDK):");
        System.out.printf("    sequential          %s  %.9f%n", Double.toHexString(seq), seq);
        System.out.printf("    parallel()          %s  %.9f%n", Double.toHexString(par), par);
        System.out.printf("    identical bits: %s%n", seq == par ? "yes" : "NO");
        System.out.println("    (the spliterator decided the split; you did not, and the");
        System.out.println("     decision depends on input size and pool parallelism)");

        System.out.println("\n  Explicit W-way partitioning, double:");
        System.out.printf("    %8s %26s %20s %14s%n", "workers", "bits", "sum", "rel err");
        Set<Double> seen = new HashSet<>();
        double[] values = new double[WORKERS.length];
        for (int i = 0; i < WORKERS.length; i++) {
            double s = sumPartitioned(data, WORKERS[i]);
            seen.add(s);
            values[i] = s;
            System.out.printf("    %8d %26s %20.6f %14.3e%n", WORKERS[i],
                    Double.toHexString(s), s, Math.abs(s - seq) / seq);
        }
        Arrays.sort(values);
        System.out.printf("    distinct: %d of %d    relative spread: %.3e   "
                + "(double ulp near 1: %.3e)%n", seen.size(), WORKERS.length,
                (values[values.length - 1] - values[0]) / seq, Math.ulp(1.0));

        System.out.println("\n  Same experiment at float precision -- the regime a kernel");
        System.out.println("  actually runs in:");
        System.out.printf("    %8s %20s %20s %14s%n", "workers", "bits", "sum", "rel err");
        Set<Float> seenF = new HashSet<>();
        float[] valuesF = new float[WORKERS.length];
        for (int i = 0; i < WORKERS.length; i++) {
            float s = sumPartitionedFloat(dataF, WORKERS[i]);
            seenF.add(s);
            valuesF[i] = s;
            System.out.printf("    %8d %20s %20.6f %14.3e%n", WORKERS[i],
                    Float.toHexString(s), s, Math.abs(s - seq) / seq);
        }
        Arrays.sort(valuesF);
        System.out.printf("    distinct: %d of %d    relative spread: %.3e   "
                + "(float ulp near 1: %.3e)%n", seenF.size(), WORKERS.length,
                (valuesF[valuesF.length - 1] - valuesF[0]) / seq, Math.ulp(1.0f));

        System.out.println("\n  same W, repeated -- this is not a race:");
        for (int w : new int[] {4, 8}) {
            float a = sumPartitionedFloat(dataF, w);
            float b = sumPartitionedFloat(dataF, w);
            System.out.printf("    W=%-3d %s  %s   identical: %s%n", w,
                    Float.toHexString(a), Float.toHexString(b), a == b);
        }

        System.out.println("\n  Each partitioning is perfectly reproducible on its own. What");
        System.out.println("  is not reproducible is WHICH partitioning you get -- on an");
        System.out.println("  inference server that is decided by the batch shape, and the");
        System.out.println("  batch shape is decided by other people's traffic.");
        System.exit(0);
    }
}
