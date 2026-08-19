// Layer 2 · Topic 1 - Java: the pool lives inside the HttpClient instance,
// and so does a thread.
//
// Java is here because it makes one consequence of "build a client per
// request" visible that no other language in this topic does. A
// java.net.http.HttpClient owns a connection pool AND a selector thread
// plus an executor. Before JDK 21 it had no close() at all, so a discarded
// client leaked those threads until GC got round to it -- a per-request
// client was a per-request thread leak, on top of a per-request handshake.
// JDK 21 made HttpClient AutoCloseable, which is the version this runs on.
//
// Three variants against one raw ServerSocket server that counts accept()
// calls. The third deliberately does NOT close its per-request clients, so
// you can watch the JVM's live thread count move.
//
// What to look for in the output:
//   - connections opened: per-request client vs shared client
//   - the live thread count after variant 3 versus before it
//
// Compile & run:
//   javac -d /tmp/javabuild ConnectionReuse.java && java -cp /tmp/javabuild ConnectionReuse

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public class ConnectionReuse {

    static final int REQUESTS = 200;
    static final int CONCURRENCY = 10;
    static final String BODY = "{\"ok\":true}";
    static final AtomicInteger accepted = new AtomicInteger();

    public static void main(String[] args) throws Exception {
        // Backlog 512. The default is 50, and 200 simultaneous cold connects
        // against a backlog of 50 measures the accept queue, not the client.
        ServerSocket listener = new ServerSocket(0, 512, java.net.InetAddress.getLoopbackAddress());
        Thread acceptLoop = new Thread(() -> {
            while (!listener.isClosed()) {
                try {
                    Socket socket = listener.accept();
                    accepted.incrementAndGet();   // one per accept(2)
                    Thread.ofVirtual().start(() -> serve(socket));
                } catch (IOException e) {
                    return;
                }
            }
        });
        acceptLoop.setDaemon(true);
        acceptLoop.start();

        URI url = URI.create("http://127.0.0.1:" + listener.getLocalPort() + "/thing");
        System.out.println("=".repeat(78));
        System.out.println("Java: the connection pool is a field on the HttpClient you kept");
        System.out.println("=".repeat(78));
        System.out.printf("  java %s   server %s   %d requests, %d in flight%n%n",
                System.getProperty("java.version"), url, REQUESTS, CONCURRENCY);
        System.out.printf("  jdk.httpclient.keepalive.timeout = %s (seconds; JDK default 30)%n",
                System.getProperty("jdk.httpclient.keepalive.timeout", "unset -> 30"));
        System.out.printf("  jdk.httpclient.connectionPoolSize = %s (0 means unbounded)%n%n",
                System.getProperty("jdk.httpclient.connectionPoolSize", "unset -> 0"));

        // Warm the JIT first. Without this the FIRST variant measured carries
        // every class load and interpreter-to-C2 transition in the whole
        // program, and comes out slower than the variant that follows it --
        // which would invert the result and look like evidence for the wrong
        // conclusion. This is the JVM's version of "the experiment is broken
        // rather than the prediction wrong".
        try (HttpClient warmup = newClient()) {
            drive(url, ignored -> warmup, false);
        }

        int before = accepted.get();
        try (HttpClient shared = newClient()) {
            List<Double> warm = drive(url, ignored -> shared, false);
            report("WARM - one HttpClient, created once, reused", warm, accepted.get() - before);
        }

        System.out.println();
        before = accepted.get();
        List<Double> cold = drive(url, ignored -> newClient(), true);
        report("COLD - a new HttpClient per request, closed properly", cold, accepted.get() - before);

        System.out.println();
        long threadsBefore = Thread.getAllStackTraces().size();
        before = accepted.get();
        List<Double> leaky = drive(url, ignored -> newClient(), false);
        long threadsAfter = Thread.getAllStackTraces().size();
        report("COLD, LEAKY - a new HttpClient per request, never closed", leaky, accepted.get() - before);
        System.out.printf("    live JVM threads before this variant %d, after %d  (delta %+d)%n",
                threadsBefore, threadsAfter, threadsAfter - threadsBefore);
        System.out.println("    Each HttpClient owns a selector thread and an executor. Before");
        System.out.println("    JDK 21 there was no close() to call, so this delta was the");
        System.out.println("    shape of a real production leak: a service that built a client");
        System.out.println("    per request grew threads until it fell over, and the handshake");
        System.out.println("    cost was the SMALLER of its two problems.");

        System.out.println();
        System.out.println("  The Java-specific thing to take away:");
        System.out.println("    HttpClient is immutable and thread-safe by design -- one instance");
        System.out.println("    per downstream service, held in a static field or injected as a");
        System.out.println("    singleton, is the intended usage. If you find yourself writing");
        System.out.println("    HttpClient.newHttpClient() inside a method that handles a request,");
        System.out.println("    that is the same bug as httpx.AsyncClient() inside a FastAPI");
        System.out.println("    handler, with a thread leak stapled on.");

        listener.close();
        System.exit(0);
    }

    static HttpClient newClient() {
        return HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)  // pin it: HTTP_2 changes what "a connection" means (Topic 6)
                .connectTimeout(Duration.ofSeconds(2))
                .build();
    }

    interface ClientSource {
        HttpClient get(int i);
    }

    static List<Double> drive(URI url, ClientSource source, boolean closeEach) throws Exception {
        List<Double> latencies = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger issued = new AtomicInteger();
        List<Thread> workers = new ArrayList<>();
        for (int w = 0; w < CONCURRENCY; w++) {
            workers.add(Thread.ofVirtual().start(() -> {
                int i;
                while ((i = issued.getAndIncrement()) < REQUESTS) {
                    HttpClient client = source.get(i);
                    long started = System.nanoTime();
                    try {
                        client.send(HttpRequest.newBuilder(url).GET().build(),
                                HttpResponse.BodyHandlers.discarding());
                        latencies.add((System.nanoTime() - started) / 1e6);
                    } catch (Exception e) {
                        System.out.println("    request error: " + e);
                    } finally {
                        if (closeEach) {
                            client.close();  // JDK 21+. Before 21 this method did not exist.
                        }
                    }
                }
            }));
        }
        for (Thread t : workers) {
            t.join();
        }
        return latencies;
    }

    static void report(String label, List<Double> latencies, int connections) {
        List<Double> sorted = new ArrayList<>(latencies);
        Collections.sort(sorted);
        System.out.println("  " + label);
        System.out.printf("    requests issued        %d%n", sorted.size());
        System.out.printf("    TCP connections opened %d%n", connections);
        System.out.printf("    requests per connection %.1f%n",
                (double) sorted.size() / Math.max(connections, 1));
        System.out.printf("    latency p50 %.3f ms   p95 %.3f ms   p99 %.3f ms%n",
                at(sorted, 0.50), at(sorted, 0.95), at(sorted, 0.99));
    }

    static double at(List<Double> sorted, double fraction) {
        if (sorted.isEmpty()) return 0;
        int index = Math.min(sorted.size() - 1, (int) (sorted.size() * fraction));
        return sorted.get(index);
    }

    static void serve(Socket socket) {
        try (socket;
             BufferedReader in = new BufferedReader(
                     new InputStreamReader(socket.getInputStream(), StandardCharsets.ISO_8859_1))) {
            OutputStream out = socket.getOutputStream();
            while (true) {
                boolean closeRequested = false;
                boolean sawRequestLine = false;
                String line;
                while ((line = in.readLine()) != null) {
                    if (!line.isEmpty()) {
                        sawRequestLine = true;
                        if (line.toLowerCase().startsWith("connection: close")) {
                            closeRequested = true;
                        }
                    } else {
                        break;   // blank line ends the headers
                    }
                }
                if (line == null || !sawRequestLine) return;

                String response = "HTTP/1.1 200 OK\r\n"
                        + "Content-Type: application/json\r\n"
                        + "Content-Length: " + BODY.length() + "\r\n"
                        + (closeRequested ? "Connection: close\r\n" : "")
                        + "\r\n" + BODY;
                out.write(response.getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
                if (closeRequested) return;
            }
        } catch (IOException ignored) {
            // peer went away; nothing useful to say about it here
        }
    }
}
