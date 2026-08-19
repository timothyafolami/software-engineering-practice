// Layer 2 · Topic 3 - Java: cancellation is a request, and the target may
// decline it.
//
// HttpClient.Builder.connectTimeout covers the connect phase;
// HttpRequest.Builder.timeout covers the whole exchange. There is no separate
// read timeout, which surprises everyone arriving from HttpURLConnection's
// setReadTimeout -- and that absence is a feature, because a per-read timeout
// does not bound an operation (see the C++ file in this directory for the
// syscall-level version of the same point).
//
// The mechanism underneath is INTERRUPTION, and that is what makes Java
// different from Python and Rust. Those two cancel by declining to resume a
// coroutine or by dropping a future: the cancelled work simply stops
// existing. Java sets a flag on a thread and hopes the code running there
// checks it. Code that does not check -- a tight loop, a synchronized block,
// a native call -- is genuinely uninterruptible, and your timeout becomes a
// suggestion.
//
// Four phases:
//   A. A deadline budget spent down three sequential hops.
//   B. What a fired HttpRequest timeout does to the request in flight at the
//      server, and whether the connection is reused afterwards.
//   C. Interruption honoured: a thread parked in Thread.sleep stops early.
//   D. Interruption declined: a thread in a CPU loop that never checks the
//      flag runs to completion regardless. Same API, same call, no effect.
//
// Java 21 note: virtual threads make "one thread parked per in-flight
// request" cheap enough to stop being the implicit limiter your service was
// accidentally relying on. That removes a queue you did not know you had and
// moves all the pressure onto the timeout you set -- which is why this topic
// matters more on Java 21 than it did on Java 11, not less.
//
// What to look for in the output:
//   - phase A: hop 3 is never started
//   - phase B: the server's FINISHED count rises for the abandoned request
//   - phase C vs D: identical interrupt() call, opposite outcomes. That gap
//     is every "the timeout did not work" bug report on the JVM
//
// Compile & run:
//   javac TimeoutIsAnInterrupt.java -d /tmp/javabuild && \
//     java -cp /tmp/javabuild TimeoutIsAnInterrupt

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

public class TimeoutIsAnInterrupt {

    static final int SLOW_MS = 400;            // how long the server holds /slow
    static final int OUTER_BUDGET_MS = 900;    // what we promised our caller
    static final int RESERVE_MS = 100;         // held back for our own response
    static final int PER_HOP_CAP_MS = 500;     // the flat "library default"

    static final AtomicInteger accepted = new AtomicInteger();
    static final AtomicInteger started = new AtomicInteger();
    static final AtomicInteger finished = new AtomicInteger();

    // ------------------------------------------------------------ server --
    // A raw ServerSocket rather than com.sun.net.httpserver, so that
    // "connections accepted" is counted where connections are actually
    // accepted and not inferred from something else.
    static int startServer() throws IOException {
        ServerSocket server = new ServerSocket(0, 128, InetAddress.getLoopbackAddress());
        Thread.ofPlatform().daemon().start(() -> {
            while (!server.isClosed()) {
                try {
                    Socket s = server.accept();
                    accepted.incrementAndGet();
                    Thread.ofVirtual().start(() -> handle(s));
                } catch (IOException e) {
                    return;
                }
            }
        });
        return server.getLocalPort();
    }

    static void handle(Socket s) {
        try (s; InputStream in = s.getInputStream(); OutputStream out = s.getOutputStream()) {
            StringBuilder buf = new StringBuilder();
            byte[] chunk = new byte[1024];
            while (true) {
                int idx = buf.indexOf("\r\n\r\n");
                if (idx < 0) {
                    int n = in.read(chunk);
                    if (n <= 0) return;
                    buf.append(new String(chunk, 0, n, StandardCharsets.ISO_8859_1));
                    continue;
                }
                String head = buf.substring(0, idx);
                buf.delete(0, idx + 4);

                String path = "/";
                String[] parts = head.split("\\s+");
                if (parts.length > 1) path = parts[1];

                if (path.startsWith("/slow")) {
                    started.incrementAndGet();
                    // No check for the client having gone away. Same as yours.
                    try {
                        Thread.sleep(SLOW_MS);
                    } catch (InterruptedException ignored) {
                        Thread.currentThread().interrupt();
                    }
                    finished.incrementAndGet();
                }

                byte[] body = ("path=" + path).getBytes(StandardCharsets.UTF_8);
                String header = "HTTP/1.1 200 OK\r\nContent-Length: " + body.length
                        + "\r\nConnection: keep-alive\r\n\r\n";
                out.write(header.getBytes(StandardCharsets.US_ASCII));
                out.write(body);
                out.flush();
            }
        } catch (IOException ignored) {
            // client hung up; that is phase B happening
        }
    }

    // ---------------------------------------------------------- deadline --
    // The pattern: an absolute instant, a reserve never spent upstream, a cap.
    record Deadline(long expiresAtNanos, long reserveMillis) {
        static Deadline of(long totalMillis, long reserveMillis) {
            return new Deadline(System.nanoTime() + totalMillis * 1_000_000L, reserveMillis);
        }
        long remainingMillis() {
            return (expiresAtNanos - System.nanoTime()) / 1_000_000L;
        }
        long forCall(long capMillis) {
            return Math.min(remainingMillis() - reserveMillis, capMillis);
        }
    }

    // ------------------------------------------------------------ phases --
    static void phaseA(HttpClient client, String base) {
        System.out.println("A. A budget, spent down three sequential hops");
        System.out.printf("    promised to our caller     %5d ms%n", OUTER_BUDGET_MS);
        System.out.printf("    reserved for our own work  %5d ms%n", RESERVE_MS);
        System.out.printf("    each hop's flat default    %5d ms  <- what a flat config would use%n%n", PER_HOP_CAP_MS);

        Deadline dl = Deadline.of(OUTER_BUDGET_MS, RESERVE_MS);
        long t0 = System.nanoTime();

        for (int hop = 1; hop <= 3; hop++) {
            long slice = dl.forCall(PER_HOP_CAP_MS);
            if (slice <= 0) {
                System.out.printf("    hop %d  slice %6d ms  -> NOT STARTED: its answer would arrive after%n", hop, slice);
                System.out.println("                              our caller has stopped waiting. Failing now is");
                System.out.println("                              correct, and it is the line people skip.");
                break;
            }
            String outcome;
            try {
                client.send(HttpRequest.newBuilder(URI.create(base + "/slow"))
                        .timeout(Duration.ofMillis(slice))
                        .build(), HttpResponse.BodyHandlers.ofString());
                outcome = "ok";
            } catch (java.net.http.HttpTimeoutException e) {
                outcome = "HttpTimeoutException";
            } catch (IOException | InterruptedException e) {
                outcome = e.getClass().getSimpleName();
            }
            System.out.printf("    hop %d  slice %6d ms  -> %-22s (%d ms elapsed, %d ms left)%n",
                    hop, slice, outcome, (System.nanoTime() - t0) / 1_000_000L, dl.remainingMillis());
        }
        System.out.printf("%n    A flat %d ms per hop would have spent %d ms on three hops against a%n",
                PER_HOP_CAP_MS, PER_HOP_CAP_MS * 3);
        System.out.printf("    %d ms promise. A timeout not derived from the promise does not%n", OUTER_BUDGET_MS);
        System.out.println("    protect the promise.");
    }

    static void phaseB(HttpClient client, String base) throws Exception {
        System.out.println("\nB. What a fired timeout does to the request already in flight");
        Thread.sleep(SLOW_MS + 100);          // let phase A's abandoned hop land
        int before = finished.get();
        int connsBefore = accepted.get();

        long t0 = System.nanoTime();
        String result;
        try {
            client.send(HttpRequest.newBuilder(URI.create(base + "/slow"))
                    .timeout(Duration.ofMillis(100))
                    .build(), HttpResponse.BodyHandlers.ofString());
            result = "completed (the timeout did not fire)";
        } catch (java.net.http.HttpTimeoutException e) {
            result = "HttpTimeoutException";
        }
        System.out.printf("    client gave up after   %d ms (%s)%n", (System.nanoTime() - t0) / 1_000_000L, result);

        Thread.sleep(SLOW_MS + 200);
        System.out.printf("    server FINISHED this request anyway: %d -> %d%n", before, finished.get());
        System.out.println("    The timeout completed your CompletableFuture exceptionally. It did not");
        System.out.println("    reach the server, which never learned that anybody stopped caring.");

        // Now: does the pool reuse that connection?
        client.send(HttpRequest.newBuilder(URI.create(base + "/fast")).build(),
                HttpResponse.BodyHandlers.ofString());
        int connsAfter = accepted.get();
        System.out.printf("    connections accepted: %d before the timeout, %d after the next request%n",
                connsBefore, connsAfter);
        System.out.println(connsAfter > connsBefore
                ? "    The timed-out connection was NOT returned to the pool. The pool is\n"
                + "    private to this HttpClient instance and you cannot inspect it -- which\n"
                + "    is why building a client per request destroys a cache you cannot see."
                : "    No new connection was needed in this run. Do not read that as the\n"
                + "    cancelled exchange being safely reusable; re-run and watch the count.");
    }

    static void phaseC() throws Exception {
        System.out.println("\nC. Interruption HONOURED: a thread parked in a blocking call");
        Thread t = Thread.ofPlatform().start(() -> {
            try {
                Thread.sleep(5000);
                System.out.println("    slept the whole 5000 ms  <- interruption was ignored");
            } catch (InterruptedException e) {
                System.out.println("    InterruptedException  <- the blocking call agreed to stop");
            }
        });
        Thread.sleep(150);
        long t0 = System.nanoTime();
        t.interrupt();
        t.join();
        System.out.printf("    stopped %d ms after interrupt(), out of a 5000 ms sleep%n",
                (System.nanoTime() - t0) / 1_000_000L);
    }

    static void phaseD() throws Exception {
        System.out.println("\nD. Interruption DECLINED: a thread that never checks the flag");
        final long[] spins = {0};
        Thread t = Thread.ofPlatform().start(() -> {
            long end = System.nanoTime() + 1_500_000_000L;   // 1.5 s of pure CPU
            long x = 0;
            while (System.nanoTime() < end) x += 1;           // no sleep, no IO, no check
            spins[0] = x;
        });
        Thread.sleep(150);
        long t0 = System.nanoTime();
        t.interrupt();
        t.join();
        long elapsed = (System.nanoTime() - t0) / 1_000_000L;
        System.out.printf("    ran %d ms AFTER interrupt() (%d iterations), out of a planned 1500 ms%n",
                elapsed, spins[0]);
        System.out.println("    interrupt() set a flag. Nothing read it. There is no preemption on the");
        System.out.println("    JVM and no way to stop this thread short of Thread.stop, which was");
        System.out.println("    removed for being unsafe. A synchronized block, a native/JNI call and a");
        System.out.println("    tight loop all behave this way -- which is the honest answer to 'why did");
        System.out.println("    my timeout not fire': it did fire, and the work declined to notice.");
    }

    public static void main(String[] args) throws Exception {
        int port = startServer();
        String base = "http://127.0.0.1:" + port;

        // connectTimeout covers only the connect phase. Per-request .timeout()
        // covers the whole exchange, and there is no read timeout at all.
        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(2))
                .build();

        System.out.println("=".repeat(78));
        System.out.println("Java: a timeout is an interrupt, and an interrupt is a request");
        System.out.println("=".repeat(78));
        System.out.printf("  server on %s, holds /slow for %d ms%n", base, SLOW_MS);
        System.out.printf("  running on JDK %s%n%n", System.getProperty("java.version"));

        phaseA(client, base);
        phaseB(client, base);
        phaseC();
        phaseD();

        System.out.println("\n  For this topic's table:");
        System.out.println("    what a fired timeout does to the in-flight request:");
        System.out.println("      completes your future exceptionally and abandons the exchange; the");
        System.out.println("      server runs to completion, and any thread that does not check its");
        System.out.println("      interrupt flag keeps running too.");
        System.out.println("    connection reused after?");
        System.out.println("      no -- and you cannot inspect the pool to confirm it, only count");
        System.out.println("      accepted connections from the other end, as this program does.");
        System.out.printf("%n  connections accepted during this run: %d%n", accepted.get());
        System.exit(0);
    }
}
