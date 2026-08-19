// Layer 1 - What a syscall actually costs, Java version.
// FileInputStream.read() on /dev/zero eventually calls read(2), but there
// are several layers between your Java call and the kernel: a JNI native
// method call, a transition out of the JVM's managed execution, argument
// marshalling -- more machinery than C++'s direct call, which is part of
// why this measures higher overhead than C++ or Go despite hitting the
// exact same kernel function.
import java.io.FileInputStream;
import java.io.IOException;

public class SyscallCost {
    static final long N = 500_000;

    static double benchSyscall() throws IOException {
        FileInputStream in = new FileInputStream("/dev/zero");
        byte[] buf = new byte[1];
        long start = System.nanoTime();
        for (long i = 0; i < N; i++) {
            in.read(buf);
        }
        long elapsed = System.nanoTime() - start;
        in.close();
        return elapsed / 1e9;
    }

    static double benchPureJava() {
        long total = 0;
        long start = System.nanoTime();
        for (long i = 0; i < N; i++) {
            total += i & 0xFF;
        }
        long elapsed = System.nanoTime() - start;
        if (total == -1) System.out.println(""); // keep the loop from being optimized away
        return elapsed / 1e9;
    }

    public static void main(String[] args) throws IOException {
        double tSys = benchSyscall();
        double tPure = benchPureJava();
        System.out.printf("N=%d%n", N);
        System.out.printf("read(/dev/zero) x%d:  %6.3fs  (%6.1f ns/call)%n", N, tSys, tSys / N * 1e9);
        System.out.printf("pure Java loop:       %6.3fs  (%6.1f ns/iter)%n", tPure, tPure / N * 1e9);
        System.out.printf("syscall is %.1fx the cost of an equivalent pure-Java step%n", tSys / tPure);
    }
}
