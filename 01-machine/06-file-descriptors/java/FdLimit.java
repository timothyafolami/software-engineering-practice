// Layer 1 - File descriptors, Java version.
// Same story as the Node/Rust versions: no getrlimit binding in the
// standard library, so this reads /proc/self/limits directly. Note what
// throws here isn't a raw errno -- it's java.io.FileNotFoundException with
// a message mentioning "Too many open files", because FileInputStream
// wraps the open(2) failure in Java's general-purpose IO exception
// hierarchy rather than surfacing EMFILE as a distinct, checkable code the
// way Python's OSError.errno or Go's syscall.EMFILE do. That's worth
// noticing on its own: catching this specific failure in Java means
// string-matching the exception message, which is less robust than the
// other languages' approach.
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

public class FdLimit {
    static String readSoftLimit() throws IOException {
        return Files.lines(Paths.get("/proc/self/limits"))
                .filter(l -> l.startsWith("Max open files"))
                .findFirst()
                .orElse("(unknown -- not on Linux?)");
    }

    public static void main(String[] args) throws IOException {
        System.out.println(readSoftLimit());

        List<FileInputStream> fds = new ArrayList<>();
        try {
            while (true) {
                fds.add(new FileInputStream("/dev/null"));
            }
        } catch (IOException e) {
            System.out.printf("hit %s after opening %d fds%n", e.getClass().getSimpleName() + ": " + e.getMessage(), fds.size());
        } finally {
            for (FileInputStream f : fds) f.close();
            System.out.printf("closed all %d fds; process is healthy again%n", fds.size());
        }
    }
}
