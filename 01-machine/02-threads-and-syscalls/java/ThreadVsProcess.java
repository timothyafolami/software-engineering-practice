// Layer 1 - Thread vs process creation cost, Java version.
// Thread (traditionally a real 1:1 OS thread -- see this topic's README
// for the Java 21 virtual-thread caveat, which belongs more to Topic 3)
// vs ProcessBuilder, which forks+execs a brand new JVM-less OS process
// (here, /bin/true) -- the heaviest tier in this whole lab, since it's not
// just a new address space, it's a whole new process image loaded from
// disk.
import java.io.IOException;

public class ThreadVsProcess {
    static final int N = 200;

    static double benchThreads() throws InterruptedException {
        long start = System.nanoTime();
        for (int i = 0; i < N; i++) {
            Thread t = new Thread(() -> {});
            t.start();
            t.join();
        }
        return (System.nanoTime() - start) / 1e9;
    }

    static double benchProcesses() throws IOException, InterruptedException {
        long start = System.nanoTime();
        for (int i = 0; i < N; i++) {
            Process p = new ProcessBuilder("true").start();
            p.waitFor();
        }
        return (System.nanoTime() - start) / 1e9;
    }

    public static void main(String[] args) throws Exception {
        double tThread = benchThreads();
        double tProc = benchProcesses();
        System.out.printf("N=%d%n", N);
        System.out.printf("Thread spawn+join:        %6.3fs  (%7.1f us/thread)%n", tThread, tThread / N * 1e6);
        System.out.printf("ProcessBuilder spawn+wait: %6.3fs  (%7.1f us/process)%n", tProc, tProc / N * 1e6);
        System.out.printf("process is %.1fx the cost of a thread%n", tProc / tThread);
    }
}
