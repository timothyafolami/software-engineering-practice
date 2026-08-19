// Layer 2 · Topic 4 - Java: "the backend's idle timeout" is not one number
// until you have read both layers that hold one.
//
// On the JVM this bug is usually a THREE-timer problem, not a two-timer one.
// A servlet container has its own idle timer (Tomcat's `keepAliveTimeout`,
// which falls back to `connectionTimeout` when unset) and its own reuse bound
// (`maxKeepAliveRequests`, the direct analogue of nginx's
// `keepalive_requests`). In front of it there is usually a second thing
// holding an idle timer too -- an embedded reverse proxy, a service mesh
// sidecar, an API gateway -- and in front of THAT is the load balancer this
// topic is named after. The ordering rule has to hold at every adjacent pair,
// and it is the smallest number in the chain that decides your behaviour.
//
// This program does not pretend to be Tomcat. It builds the chain out of raw
// sockets, with every idle timer named and set explicitly, because the point
// is the ordering and not any one container's defaults. Those defaults are
// deliberately NOT quoted here: they move between releases, and a number you
// did not read from the version you are running is exactly the class of
// fabrication this lab exists to avoid. The knobs to go and read are printed
// at the end.
//
// The chain, smallest scale first:
//
//     client  ->  [ sidecar: pool + idle timer ]  ->  [ container: idle timer ]
//
// Three configurations:
//   mismatched  container 300 ms, sidecar pool reuses after 1000 ms  -> 502s
//   ordered     container 3000 ms                                    -> clean
//   bounded     ordered, plus maxKeepAliveRequests = 2               -> clean,
//               and connections rotate on purpose so the pool keeps
//               rediscovering where the backend is (Topic 5)
//
// What to look for in the output:
//   - the mismatched run failing on exactly the requests that follow an idle
//     gap, with nothing logged on the container side
//   - the container's "closed by idle timer" count matching those failures
//   - the handshake count in the bounded run: reuse bounded on purpose costs
//     handshakes, and that is the price of a pool that stays current
//
// Compile & run:
//   javac IdleTimersOnBothSides.java -d /tmp/javabuild && \
//     java -cp /tmp/javabuild IdleTimersOnBothSides

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

public class IdleTimersOnBothSides {

    static final int POOL_IDLE_GAP_MS = 1000;   // the sidecar/LB idle gap
    static final int REQUESTS = 5;

    /** The servlet container: keep-alive HTTP/1.1 with an idle timer and an
     *  optional bound on requests per connection. Both knobs exist in Tomcat
     *  under different names; both exist in nginx too. */
    static final class Container implements AutoCloseable {
        final ServerSocket socket;
        final int idleTimeoutMs;
        final int maxKeepAliveRequests;      // <= 0 means unbounded
        final AtomicInteger accepted = new AtomicInteger();
        final AtomicInteger served = new AtomicInteger();
        final AtomicInteger closedByIdleTimer = new AtomicInteger();
        final AtomicInteger closedByRequestBound = new AtomicInteger();

        Container(int idleTimeoutMs, int maxKeepAliveRequests) throws IOException {
            this.socket = new ServerSocket(0, 128, InetAddress.getLoopbackAddress());
            this.idleTimeoutMs = idleTimeoutMs;
            this.maxKeepAliveRequests = maxKeepAliveRequests;
            Thread.ofPlatform().daemon().start(this::acceptLoop);
        }

        int port() { return socket.getLocalPort(); }

        void acceptLoop() {
            while (!socket.isClosed()) {
                try {
                    Socket s = socket.accept();
                    accepted.incrementAndGet();
                    Thread.ofVirtual().start(() -> handle(s));
                } catch (IOException e) {
                    return;
                }
            }
        }

        void handle(Socket s) {
            int onThisConnection = 0;
            try (s; InputStream in = s.getInputStream(); OutputStream out = s.getOutputStream()) {
                while (true) {
                    // THE IDLE TIMER. setSoTimeout is what a container's
                    // keepAliveTimeout ultimately becomes: a bound on how long
                    // a read may wait for the next request on this connection.
                    s.setSoTimeout(idleTimeoutMs);
                    byte[] buf = new byte[2048];
                    int n;
                    try {
                        n = in.read(buf);
                    } catch (SocketTimeoutException e) {
                        // We close. Correctly. As configured. Silently.
                        closedByIdleTimer.incrementAndGet();
                        return;
                    }
                    if (n <= 0) return;

                    served.incrementAndGet();
                    onThisConnection++;
                    boolean last = maxKeepAliveRequests > 0 && onThisConnection >= maxKeepAliveRequests;

                    byte[] body = "ok".getBytes(StandardCharsets.UTF_8);
                    String head = "HTTP/1.1 200 OK\r\nContent-Length: " + body.length
                            + "\r\nConnection: " + (last ? "close" : "keep-alive") + "\r\n\r\n";
                    out.write(head.getBytes(StandardCharsets.US_ASCII));
                    out.write(body);
                    out.flush();

                    if (last) {
                        // maxKeepAliveRequests / keepalive_requests: retire the
                        // connection on purpose, having ANNOUNCED it first with
                        // Connection: close. That announcement is the whole
                        // difference between this and the bug -- the peer is
                        // told before the socket goes away.
                        closedByRequestBound.incrementAndGet();
                        return;
                    }
                }
            } catch (IOException ignored) {
                // the peer went away mid-exchange
            }
        }

        @Override public void close() throws IOException { socket.close(); }
    }

    /** The sidecar or load balancer: one pooled connection, held and reused. */
    static final class PooledConnection {
        final int port;
        Socket socket;
        int handshakes;

        PooledConnection(int port) { this.port = port; }

        String request() {
            try {
                if (socket == null || socket.isClosed()) {
                    socket = new Socket(InetAddress.getLoopbackAddress(), port);
                    socket.setSoTimeout(2000);
                    handshakes++;
                }
                OutputStream out = socket.getOutputStream();
                out.write("GET /work HTTP/1.1\r\nHost: lab\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
                out.flush();

                InputStream in = socket.getInputStream();
                byte[] buf = new byte[2048];
                int n = in.read(buf);
                if (n <= 0) {
                    // The read came back empty: the peer had already sent its
                    // FIN. This is the 502, and nothing on the far side logged
                    // anything at all.
                    discard();
                    return "FAIL: empty read (peer had already closed)";
                }
                String resp = new String(buf, 0, n, StandardCharsets.ISO_8859_1);
                if (resp.toLowerCase().contains("connection: close")) {
                    // Announced. We retire the connection ourselves, in order,
                    // and nobody gets a surprise.
                    discard();
                    return "200 (peer announced Connection: close)";
                }
                return "200";
            } catch (IOException e) {
                discard();
                return "FAIL: " + e.getClass().getSimpleName() + " " + e.getMessage();
            }
        }

        void discard() {
            try { if (socket != null) socket.close(); } catch (IOException ignored) { }
            socket = null;
        }
    }

    static int run(String name, int containerIdleMs, int maxKeepAliveRequests, String note) throws Exception {
        try (Container container = new Container(containerIdleMs, maxKeepAliveRequests)) {
            PooledConnection pool = new PooledConnection(container.port());
            int failures = 0;
            List<String> log = new ArrayList<>();

            for (int i = 0; i < REQUESTS; i++) {
                String r = pool.request();
                boolean ok = !r.startsWith("FAIL");
                if (!ok) failures++;
                log.add(String.format("      request %d: %s  %s", i, ok ? "ok " : "502", r));
                Thread.sleep(POOL_IDLE_GAP_MS);   // the idle gap the bug needs
            }
            pool.discard();

            System.out.println("  " + name);
            System.out.printf("    container idle timeout    %d ms%n", containerIdleMs);
            System.out.printf("    maxKeepAliveRequests      %s%n",
                    maxKeepAliveRequests > 0 ? String.valueOf(maxKeepAliveRequests) : "unbounded");
            System.out.printf("    pool idle gap             %d ms%n", POOL_IDLE_GAP_MS);
            System.out.printf("    ordering                  %s%n", note);
            log.forEach(System.out::println);
            System.out.printf("    failures %d/%d   accepted %d   served %d   "
                            + "closed by idle timer %d   closed by request bound %d   handshakes %d%n%n",
                    failures, REQUESTS, container.accepted.get(), container.served.get(),
                    container.closedByIdleTimer.get(), container.closedByRequestBound.get(),
                    pool.handshakes);
            return failures;
        }
    }

    public static void main(String[] args) throws Exception {
        System.out.println("=".repeat(78));
        System.out.println("Java: two idle timers on the backend side before the LB even appears");
        System.out.println("=".repeat(78));
        System.out.printf("  JDK %s%n%n", System.getProperty("java.version"));

        int mismatched = run("mismatched -- container 300 ms, pool reuses after 1000 ms",
                300, 0, "container closes first  <-- the bug");
        int ordered = run("ordered -- container 3000 ms, pool reuses after 1000 ms",
                3000, 0, "pool closes first       <-- correct");
        int bounded = run("ordered_bounded -- ordered, plus maxKeepAliveRequests = 2",
                3000, 2, "pool closes first, and connections rotate on purpose");

        System.out.println("  Summary");
        System.out.printf("    mismatched      %d failures out of %d%n", mismatched, REQUESTS);
        System.out.printf("    ordered         %d failures out of %d%n", ordered, REQUESTS);
        System.out.printf("    ordered_bounded %d failures out of %d%n%n", bounded, REQUESTS);

        System.out.println("    Compare the two ways a connection ended in this run. The idle timer");
        System.out.println("    closes WITHOUT telling anyone -- the peer discovers it by writing a");
        System.out.println("    request into a socket that is already gone. maxKeepAliveRequests closes");
        System.out.println("    having sent `Connection: close` FIRST, so the peer retires the");
        System.out.println("    connection in order and no request is ever lost. Same outcome, one");
        System.out.println("    announced and one not, and the announcement is the entire difference");
        System.out.println("    between a rotation strategy and an intermittent 502.");
        System.out.println();
        System.out.println("    Numbers to go and read on YOUR stack, because they move between");
        System.out.println("    releases and none of them are quoted in this file on purpose:");
        System.out.println("      Tomcat        keepAliveTimeout (falls back to connectionTimeout),");
        System.out.println("                    maxKeepAliveRequests   -- server.xml / Spring Boot's");
        System.out.println("                    server.tomcat.* properties");
        System.out.println("      Jetty         idleTimeout on the connector");
        System.out.println("      Undertow      NO_REQUEST_TIMEOUT, REQUEST_PARSE_TIMEOUT");
        System.out.println("      your sidecar  Envoy's idle_timeout, and it is per-listener AND");
        System.out.println("                    per-cluster, so there are two of those as well");
        System.out.println("      the LB        ALB idle timeout, or nginx keepalive_timeout in the");
        System.out.println("                    upstream block");
        System.out.println();
        System.out.println("    The rule is one line and it has to hold at every adjacent pair:");
        System.out.println("    the side further from the client -- the one NOT holding a pool it is");
        System.out.println("    about to reuse -- must have the longer idle timeout.");
        System.exit(0);
    }
}
