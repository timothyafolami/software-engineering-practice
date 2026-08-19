// 7.3 -- Java: container-aware since long before it was fashionable, and the
// one with the widest blast radius if you override it wrongly.
//
// WHAT THIS DEMONSTRATES
//   Runtime.getRuntime().availableProcessors() derives from the cgroup CPU
//   limit when UseContainerSupport is on, which it has been by default since
//   8u191 / JDK 10. So the JVM answered question (3) years before Go did, and
//   without a version flag or a library.
//
//   The number is not the interesting part. The BLAST RADIUS is. Everything
//   in the JVM sizes itself from that single call:
//
//     ParallelGCThreads, G1's concurrent workers, the C1/C2 JIT compiler
//     threads, ForkJoinPool.commonPool()'s parallelism, every
//     newFixedThreadPool you wrote, and the carrier pool behind virtual
//     threads.
//
//   This probe prints those actual values, read from the live VM rather than
//   quoted from documentation, so you can see one number fan out into six.
//   Get it wrong -- with -XX:ActiveProcessorCount, or by turning container
//   support off -- and it is wrong everywhere at once.
//
//   Memory has the parallel story: Runtime.maxMemory() under MaxRAMPercentage
//   reads memory.max. That is 7.6; the numbers are printed here for the
//   comparison.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. availableProcessors() next to the enforced quota. Inside a container
//      under `--cpus=1.5` they agree -- and note how the JVM rounded the .5.
//   2. Re-run with -XX:-UseContainerSupport to get the pre-8u191 answer, the
//      way Go's GODEBUG produces the pre-1.25 one. The flag's value is
//      printed rather than inferred.
//   3. The "one number, six consumers" block. That is why this call has the
//      widest blast radius of any row in the matrix.
//
// RUN
//   javac CpuInfo.java -d /tmp/javabuild && java -cp /tmp/javabuild CpuInfo
//   java -XX:-UseContainerSupport -cp /tmp/javabuild CpuInfo   # the "before"
//   java -XX:ActiveProcessorCount=2 -cp /tmp/javabuild CpuInfo # the override
//
//   Inside a Linux container, which is where the columns separate:
//     docker run --rm --cpus=1.5 -v "$PWD:/w" -w /w eclipse-temurin:21 \
//       sh -c 'javac /w/CpuInfo.java -d /tmp/b && java -cp /tmp/b CpuInfo'

import java.io.IOException;
import java.lang.management.ManagementFactory;
import java.lang.management.RuntimeMXBean;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ForkJoinPool;

public final class CpuInfo {

    // ------------------------------------------------------------ the kernel

    /** CPUs of bandwidth the cgroup actually enforces, or -1 for no ceiling.
     *
     *  Printed for comparison, not because you need it: with
     *  UseContainerSupport on, availableProcessors() has already read this
     *  file. Reading the enforced number yourself before trusting any runtime
     *  that claims to have read it for you is the habit this topic installs. */
    private static double readCpuMax() {
        try {
            String[] parts = Files.readString(Path.of("/sys/fs/cgroup/cpu.max"))
                    .trim().split("\\s+");
            if (parts[0].equals("max")) {
                return -1.0;
            }
            long period = parts.length > 1 ? Long.parseLong(parts[1]) : 100_000L;
            return Long.parseLong(parts[0]) / (double) period;
        } catch (IOException | RuntimeException noV2) {
            try {
                long quota = Long.parseLong(Files
                        .readString(Path.of("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")).trim());
                long period = Long.parseLong(Files
                        .readString(Path.of("/sys/fs/cgroup/cpu/cpu.cfs_period_us")).trim());
                return quota > 0 ? quota / (double) period : -1.0;
            } catch (IOException | RuntimeException noV1) {
                return -1.0;  // no cgroupfs at all: every macOS host
            }
        }
    }

    private static String readOrNa(String path) {
        try {
            return Files.readString(Path.of(path)).trim();
        } catch (IOException absent) {
            return "n/a";
        }
    }

    /** Read a VM flag's live value rather than inferring it from behaviour.
     *  "Print the flag, do not infer it" is the whole reason this method
     *  exists: two identical-looking rows usually mean the flag was already
     *  set the way you were about to set it. */
    private static String vmFlag(String name) {
        try {
            var bean = ManagementFactory.getPlatformMXBean(
                    com.sun.management.HotSpotDiagnosticMXBean.class);
            var flag = bean.getVMOption(name);
            return flag.getValue() + "  (" + flag.getOrigin() + ")";
        } catch (RuntimeException notHotSpot) {
            return "n/a (not a HotSpot VM, or flag unavailable)";
        }
    }

    // -------------------------------------------------------------- output

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

    public static void main(String[] args) {
        int available = Runtime.getRuntime().availableProcessors();
        double quota = readCpuMax();
        RuntimeMXBean runtimeBean = ManagementFactory.getRuntimeMXBean();

        System.out.println("7.3 -- how big is this machine? Java's answer");
        System.out.printf("  runtime     : %s %s on %s/%s%n",
                System.getProperty("java.vm.name"),
                System.getProperty("java.version"),
                System.getProperty("os.name"),
                System.getProperty("os.arch"));
        System.out.printf("  VM args     : %s%n", runtimeBean.getInputArguments());
        System.out.println();

        List<String[]> rows = new ArrayList<>();
        rows.add(new String[]{"availableProcessors()",
                "Runtime.getRuntime().availableProcessors()",
                String.valueOf(available),
                "(3) when UseContainerSupport is on",
                "cgroup CPU limit, else host+affinity"});
        rows.add(new String[]{"/sys/fs/cgroup/cpu.max", "Files.readString(...)",
                quota < 0 ? "n/a" : String.format("%.2f", quota),
                "(3) how much CPU TIME may I consume",
                "cpu.max -- THE ENFORCED NUMBER"});
        printTable(new String[]{"what people call", "the call", "answer here",
                "which question it answers", "what it tracks"}, rows);
        System.out.println();

        System.out.println("  the two flags that change the answer (values read, not inferred):");
        System.out.printf("    UseContainerSupport   %s%n", vmFlag("UseContainerSupport"));
        System.out.printf("    ActiveProcessorCount  %s%n", vmFlag("ActiveProcessorCount"));
        System.out.println("      ActiveProcessorCount = -1 means 'not overridden'.");
        System.out.println("      If your two runs give identical numbers, print these before");
        System.out.println("      concluding anything: the flag was probably already set that way.");
        System.out.println();

        System.out.println("  ground truth on this host:");
        System.out.printf("    cpu.max               %s%n", readOrNa("/sys/fs/cgroup/cpu.max"));
        System.out.printf("    cpuset.cpus.effective %s%n",
                readOrNa("/sys/fs/cgroup/cpuset.cpus.effective"));
        System.out.printf("    memory.max            %s%n", readOrNa("/sys/fs/cgroup/memory.max"));
        System.out.println();

        if (quota < 0) {
            System.out.println("  NOTE: no CPU quota is enforced here, so availableProcessors()");
            System.out.println("        falls back to the host CPU count and the matrix has one");
            System.out.println("        column. That is the correct result on this host -- run it");
            System.out.println("        under --cpus=1.5 in a container and the rows separate.");
            System.out.println();
        } else {
            System.out.printf("  The JVM read %.2f CPU of quota and reported %d processor(s).%n",
                    quota, available);
            System.out.println("  Note which way it rounded. Go 1.25 rounds fractional limits UP");
            System.out.println("  (more threads than the quota can keep busy, still throttleable);");
            System.out.println("  whichever direction a runtime picks, it picked it for you.");
            System.out.println();
        }

        // ---- one number, six consumers ------------------------------------
        System.out.println("  ONE number, and everything downstream of it:");
        System.out.printf("    availableProcessors()          %d%n", available);
        System.out.printf("    ForkJoinPool.commonPool()      %d parallelism%n",
                ForkJoinPool.commonPool().getParallelism());
        System.out.printf("    ParallelGCThreads              %s%n", vmFlag("ParallelGCThreads"));
        System.out.printf("    ConcGCThreads                  %s%n", vmFlag("ConcGCThreads"));
        System.out.printf("    CICompilerCount (JIT threads)  %s%n", vmFlag("CICompilerCount"));
        System.out.printf("    live thread count right now    %d%n",
                ManagementFactory.getThreadMXBean().getThreadCount());
        System.out.println("    ...plus the carrier pool behind virtual threads, and every");
        System.out.println("    newFixedThreadPool(availableProcessors()) in your own code.");
        System.out.println();
        System.out.println("  That fan-out is why this is simultaneously the most correct");
        System.out.println("  answer in the matrix and the one with the widest blast radius.");
        System.out.println("  Override it wrongly and you have not mis-sized one pool -- you");
        System.out.println("  have mis-sized the garbage collector, the JIT, and every pool in");
        System.out.println("  the process at the same time.");
        System.out.println();

        // ---- the memory half, for the 7.6 comparison ----------------------
        long maxHeap = Runtime.getRuntime().maxMemory();
        System.out.println("  And the memory axis, which Java also reads (7.6):");
        System.out.printf("    Runtime.maxMemory()   %.0f MiB   <- MaxRAMPercentage of memory.max%n",
                maxHeap / (double) (1 << 20));
        System.out.printf("    MaxRAMPercentage      %s%n", vmFlag("MaxRAMPercentage"));
        System.out.println("      The HotSpot default is 25%, which surprises people twice: once");
        System.out.println("      when they find the JVM using a quarter of the container, and");
        System.out.println("      again when they set it to 100 and get OOM-killed anyway --");
        System.out.println("      because metaspace, the code cache, thread stacks and direct");
        System.out.println("      ByteBuffers all live outside -Xmx. That is 7.6.");
    }
}
