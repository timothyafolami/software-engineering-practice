// 7.6 -- Java: container-aware, the most misread of the six, and the only
// runtime here that can show you BOTH failures cleanly.
//
// WHAT THIS DEMONSTRATES
//   With UseContainerSupport (default since 8u191/JDK 10), MaxRAMPercentage
//   sizes the heap as a percentage of memory.max rather than of host RAM.
//   The HotSpot default is 25%, which surprises people twice:
//
//     once when they discover the JVM is using a quarter of the container,
//     and again when they set it to 100 and get OOM-killed anyway.
//
//   The reason for the second is the point of this file: the Java heap is
//   only PART of the JVM's RSS. Metaspace, the code cache, thread stacks,
//   direct ByteBuffers and GC bookkeeping all live outside -Xmx. This
//   program allocates in both regions so you can watch the two deaths:
//
//     java Oom --heap     fill the Java heap  -> OutOfMemoryError, CAUGHT,
//                                                with a stack trace, exit 1
//     java Oom --direct   fill direct buffers -> outside -Xmx entirely.
//                                                The cgroup kills you: 137,
//                                                silence, no OutOfMemoryError
//
//   Same container, same limit, two completely different failures. Being
//   able to tell them apart from a restart log is the practical skill, and
//   -XX:NativeMemoryTracking=summary plus `jcmd <pid> VM.native_memory` is
//   how you see the region that -Xmx does not cover.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. The header: Runtime.maxMemory() next to memory.max, and the
//      percentage between them. Under --memory=256m with the default
//      MaxRAMPercentage the heap is ~64 MiB, and the other ~190 MiB is not
//      yours to fill.
//   2. In --heap mode: an OutOfMemoryError that is CAUGHT, printed, and
//      survivable. Compare that with python/oom.py, which prints nothing.
//   3. In --direct mode: the last allocated line, then nothing. The catch
//      block for OutOfMemoryError does not fire, the shutdown hook does not
//      run, and the exit code is 137.
//
// RUN
//   javac Oom.java -d /tmp/javabuild
//   java -XX:MaxRAMPercentage=75 -cp /tmp/javabuild Oom --heap
//
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w eclipse-temurin:21 \
//     sh -c 'javac /w/Oom.java -d /tmp/b && java -XX:MaxRAMPercentage=75 -cp /tmp/b Oom --direct'
//   echo "exit code: $?"      # 137
//
// On macOS there is no cgroup memory controller, so nothing can OOM-kill
// this process. --heap still reaches the real -Xmx ceiling, because that
// ceiling exists everywhere; --direct imposes its own cap and says so.

import java.io.IOException;
import java.lang.management.BufferPoolMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public final class Oom {

    private static final int CHUNK_MB = 8;
    private static final MemoryMXBean MEMORY = ManagementFactory.getMemoryMXBean();

    // ------------------------------------------------------------ the kernel

    /** Bytes the cgroup will let this container charge, or -1 for no limit. */
    private static long memoryMax() {
        try {
            String raw = Files.readString(Path.of("/sys/fs/cgroup/memory.max")).trim();
            return raw.equals("max") ? -1 : Long.parseLong(raw);
        } catch (IOException | RuntimeException noV2) {
            try {
                long v1 = Long.parseLong(Files
                        .readString(Path.of("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
                        .trim());
                // v1 spells "unlimited" as a number near 2^63, not as a word.
                return v1 < (1L << 62) ? v1 : -1;
            } catch (IOException | RuntimeException noV1) {
                return -1;  // no cgroupfs at all: every macOS host
            }
        }
    }

    private static String readOr(String path, String fallback) {
        try {
            return Files.readString(Path.of(path)).trim().replace('\n', ' ');
        } catch (IOException absent) {
            return fallback;
        }
    }

    /** Current RSS in MiB, or -1 where there is no way to ask.
     *
     *  Linux only. Darwin offers a PEAK via getrusage, which is a different
     *  question, and printing one number under the name of another is how
     *  memory dashboards end up lying. -1 with a printed reason beats a
     *  plausible wrong number. */
    private static double rssMb() {
        try {
            for (String line : Files.readAllLines(Path.of("/proc/self/status"))) {
                if (line.startsWith("VmRSS:")) {
                    return Long.parseLong(line.replaceAll("\\D+", "")) / 1024.0;
                }
            }
        } catch (IOException | RuntimeException noProc) {
            // fall through
        }
        return -1;
    }

    private static String rssDisplay() {
        double rss = rssMb();
        return rss < 0 ? "n/a" : String.format("%.0f MiB", rss);
    }

    private static String vmFlag(String name) {
        try {
            var bean = ManagementFactory.getPlatformMXBean(
                    com.sun.management.HotSpotDiagnosticMXBean.class);
            return bean.getVMOption(name).getValue();
        } catch (RuntimeException unavailable) {
            return "n/a";
        }
    }

    /** Direct buffer bytes -- the region OUTSIDE the Java heap that -Xmx does
     *  not cover and that the cgroup charges you for anyway. */
    private static long directBufferBytes() {
        for (BufferPoolMXBean pool :
                ManagementFactory.getPlatformMXBeans(BufferPoolMXBean.class)) {
            if (pool.getName().equals("direct")) {
                return pool.getMemoryUsed();
            }
        }
        return 0;
    }

    private static String mib(long bytes) {
        return String.format("%.0f MiB", bytes / (double) (1 << 20));
    }

    public static void main(String[] args) {
        boolean direct = List.of(args).contains("--direct");
        int selfLimitMb = 512;
        for (int i = 0; i < args.length - 1; i++) {
            if (args[i].equals("--limit-mb")) {
                selfLimitMb = Integer.parseInt(args[i + 1]);
            }
        }

        // A shutdown hook. It runs on a normal exit, on System.exit, and on
        // SIGTERM. It does not run on SIGKILL.
        Runtime.getRuntime().addShutdownHook(new Thread(() ->
                System.out.println("  [shutdown hook] ran. RSS " + rssDisplay()
                        + ". So this was NOT a SIGKILL.")));

        long limit = memoryMax();
        long maxHeap = Runtime.getRuntime().maxMemory();

        System.out.println("7.6 -- memory: Java");
        System.out.printf("  runtime         : %s %s%n",
                System.getProperty("java.vm.name"), System.getProperty("java.version"));
        System.out.printf("  memory.max      : %s%n",
                limit < 0 ? "no limit / no cgroupfs" : mib(limit));
        System.out.printf("  memory.high     : %s   <- degrades instead of killing; "
                + "no Compose key%n", readOr("/sys/fs/cgroup/memory.high", "unset"));
        System.out.printf("  Runtime.maxMemory() : %s   <- the JAVA HEAP ceiling (-Xmx)%n",
                mib(maxHeap));
        System.out.printf("  MaxRAMPercentage    : %s%n", vmFlag("MaxRAMPercentage"));
        System.out.printf("  MaxDirectMemorySize : %s   <- 0 means \"same as -Xmx\"; "
                + "direct buffers are%n", vmFlag("MaxDirectMemorySize"));
        System.out.println("                        outside the HEAP but not outside the "
                + "JVM's accounting");
        System.out.printf("  UseContainerSupport : %s%n", vmFlag("UseContainerSupport"));
        System.out.printf("  starting RSS        : %s%n", rssDisplay());
        System.out.println();

        if (limit > 0) {
            long outside = limit - maxHeap;
            System.out.printf("  The heap is %.0f%% of the container limit. The other %s is%n",
                    maxHeap * 100.0 / limit, mib(outside));
            System.out.println("  metaspace, the code cache, thread stacks, direct ByteBuffers,");
            System.out.println("  GC bookkeeping and the JVM's own binary -- none of which -Xmx");
            System.out.println("  covers and all of which the cgroup charges you for.");
            System.out.println("  See it with: -XX:NativeMemoryTracking=summary, then");
            System.out.println("               jcmd <pid> VM.native_memory summary");
            System.out.println();
        } else {
            System.out.printf("  !! No cgroup memory limit on this host, so nothing can "
                    + "OOM-kill this%n");
            System.out.printf("  !! process. --heap still reaches the real -Xmx ceiling "
                    + "(it exists%n");
            System.out.printf("  !! everywhere); --direct will stop ITSELF at %d MiB and "
                    + "say so.%n", selfLimitMb);
            System.out.println("  !! For the kill:");
            System.out.println("  !!   docker run --rm --memory=256m -v \"$PWD:/w\" -w /w \\");
            System.out.println("  !!     eclipse-temurin:21 sh -c 'javac /w/Oom.java -d /tmp/b && \\");
            System.out.println("  !!       java -XX:MaxRAMPercentage=75 -cp /tmp/b Oom --direct'");
            System.out.println();
        }

        System.out.println("  installed, and about to be tested:");
        System.out.println("    * try/catch (OutOfMemoryError) around the allocation");
        System.out.println("    * a shutdown hook");
        System.out.println("    * a finally block");
        System.out.println("  One of the two modes below runs all three. The other runs none.");
        System.out.println();

        long ceilingBytes = direct
                ? (limit > 0 ? (long) (limit * 1.5) : (long) selfLimitMb << 20)
                : (long) (maxHeap * 1.5);

        System.out.printf("  mode: --%s%n", direct ? "direct" : "heap");
        if (direct) {
            System.out.println("    Allocating direct ByteBuffers. These live OUTSIDE the Java");
            System.out.println("    heap -- GC does not move them and -Xmx does not cover them --");
            System.out.println("    which is why a JVM can be OOM-killed with heap headroom left.");
            System.out.println();
            System.out.println("    But read the header again: MaxDirectMemorySize defaults to");
            System.out.println("    the SAME value as -Xmx, so out of the box the JVM still");
            System.out.println("    bounds this region and still raises OutOfMemoryError:");
            System.out.println("    Direct buffer memory. That default is the JVM protecting you");
            System.out.println("    from exactly the failure this mode is trying to show.");
            System.out.println();
            System.out.println("    To reach the CGROUP limit instead of the JVM's, raise it past");
            System.out.println("    memory.max and let the kernel be the first ceiling you hit:");
            System.out.println("      java -XX:MaxRAMPercentage=50 -XX:MaxDirectMemorySize=2g \\");
            System.out.println("           -cp /tmp/b Oom --direct");
            System.out.println("    Then the death is SIGKILL, exit 137, and silence.");
        } else {
            System.out.println("    Allocating byte[] on the Java heap. The JVM owns this region");
            System.out.println("    and WILL tell you, with a stack trace, when it fills.");
        }
        System.out.println();

        // A field-like local the catch block can clear as its FIRST action.
        // See the comment in the handler: everything you want to do after an
        // OutOfMemoryError -- including logging it -- allocates.
        List<Object> blocks = new ArrayList<>();
        long allocated = 0;
        try {
            while (allocated < ceilingBytes) {
                if (direct) {
                    // allocateDirect goes to the OS, not the Java heap, and
                    // put() touches the pages -- which is the write the cgroup
                    // charges for.
                    ByteBuffer buffer = ByteBuffer.allocateDirect(CHUNK_MB << 20);
                    for (int offset = 0; offset < buffer.capacity(); offset += 4096) {
                        buffer.put(offset, (byte) 1);
                    }
                    blocks.add(buffer);
                } else {
                    byte[] block = new byte[CHUNK_MB << 20];
                    // Java zero-fills a new array, so the pages are already
                    // touched. Writing again anyway makes the intent explicit
                    // and keeps the two branches symmetric.
                    for (int offset = 0; offset < block.length; offset += 4096) {
                        block[offset] = 1;
                    }
                    blocks.add(block);
                }
                allocated += (long) CHUNK_MB << 20;

                if ((allocated >> 20) % 32 == 0) {
                    System.out.printf("    allocated %5d MiB   heap used %6s   "
                                    + "direct %6s   RSS %8s   memory.events: %s%n",
                            allocated >> 20,
                            mib(MEMORY.getHeapMemoryUsage().getUsed()),
                            mib(directBufferBytes()),
                            rssDisplay(),
                            readOr("/sys/fs/cgroup/memory.events", "n/a"));
                    System.out.flush();
                }
            }
        } catch (OutOfMemoryError err) {
            // RELEASE MEMORY FIRST. Not stylistic -- structural.
            //
            // The first version of this file printed from the handler before
            // clearing `blocks`, and the handler itself threw a second
            // OutOfMemoryError building the message: on a full heap, string
            // concatenation, autoboxing and stack-trace formatting all
            // allocate. The second error propagated out of the catch, a third
            // came out of the finally block, and the program died reporting a
            // line number in code that had nothing to do with the failure.
            //
            // That is not a curiosity. It is why OutOfMemoryError handlers
            // that log before they free are unreliable exactly when you need
            // them, and it is most of why -XX:+ExitOnOutOfMemoryError and
            // -XX:+HeapDumpOnOutOfMemoryError exist: the JVM does that work
            // from outside your handler, where the heap is not the problem.
            blocks.clear();
            blocks = null;
            System.gc();

            // This runs in --heap mode and never in --direct mode under a
            // cgroup kill. Catching an OutOfMemoryError is generally a bad
            // idea in production -- the JVM's state afterwards is not
            // guaranteed -- but catching it HERE is the demonstration.
            boolean directBufferError = String.valueOf(err.getMessage()).toLowerCase().contains("direct buffer");
            System.out.println();
            System.out.println("  OutOfMemoryError CAUGHT: " + err.getMessage());
            if (directBufferError) {
                System.out.println("  -- and note WHICH limit that was. Not the heap, and not the");
                System.out.println("     cgroup: MaxDirectMemorySize, which defaults to -Xmx. The");
                System.out.println("     JVM bounded a region outside the heap and told you about");
                System.out.println("     it. Raise -XX:MaxDirectMemorySize past memory.max to see");
                System.out.println("     what happens when nothing in the JVM is bounding you.");
            }
            System.out.println("  With a stack trace, a message, and a live process to log it:");
            StackTraceElement[] trace = err.getStackTrace();
            for (int i = 0; i < Math.min(3, trace.length); i++) {
                System.out.println("      at " + trace[i]);
            }
            System.out.println();
            System.out.println("  This is the FIRST of Java's two deaths. You exceeded a limit");
            System.out.println("  the JVM enforces, so the JVM told you. Compare that with what");
            System.out.println("  --direct prints, and with what python/oom.py prints, which is");
            System.out.println("  nothing at all.");
            System.out.println();
            System.out.println("  Note what the handler had to do BEFORE it could print any");
            System.out.println("  of that: drop every reference and collect. Logging allocates.");
            System.out.println("  An OutOfMemoryError handler that logs first usually throws a");
            System.out.println("  second OutOfMemoryError out of the handler, and the stack");
            System.out.println("  trace you finally get points at your logging code.");
            System.exit(1);
        } finally {
            // Deliberately allocation-free: no printf, no autoboxing, no
            // string concatenation. A finally block that allocates on a full
            // heap throws from the finally and replaces the original error
            // with a useless one.
            System.out.write('\n');
            System.out.print("  [finally] reached");
            System.out.println();
        }

        System.out.println();
        System.out.printf("  Reached %d MiB without dying.%n", allocated >> 20);
        if (limit < 0) {
            System.out.println("  Expected: no cgroup here to kill anything.");
        } else {
            System.out.println("  NOT expected under a memory limit. The kernel reclaimed enough");
            System.out.println("  to keep up, or memory.high is set and doing its job.");
        }
        System.out.println();
        System.out.println("  Java's two deaths, and how to tell them apart in a restart log:");
        System.out.println("    OutOfMemoryError  a limit the JVM enforces. Stack trace, message,");
        System.out.println("                      catchable, and the process is still there to");
        System.out.println("                      write a log line. Fix: -Xmx / MaxRAMPercentage,");
        System.out.println("                      or a heap dump and less garbage.");
        System.out.println("    exit 137          a limit the KERNEL enforces. Nothing printed,");
        System.out.println("                      no hook, no trace, and .State.OOMKilled = true.");
        System.out.println("                      Fix: raise memory.max, or shrink what lives");
        System.out.println("                      outside -Xmx.");
        System.out.println();
        System.out.println("  Setting MaxRAMPercentage=100 converts the first into the second.");
        System.out.println("  That is not a fix; it is trading a diagnosable failure for an");
        System.out.println("  undiagnosable one.");
    }
}
