// Layer 10 - Topic 2: does hanging up actually free the KV blocks? (Java)
//
// What this demonstrates
//     The same experiment as the other five, on Java 21+ virtual threads --
//     the workload Loom was designed for: many concurrent requests, each
//     mostly waiting. A stub model server streams 40 tokens at 100ms each
//     while watching for its caller to leave; a gateway sits in front with
//     two handlers; a client hangs up after 500ms against each.
//
//       /naive       one virtual thread per request, blocking style, reads
//                    the upstream to EOF and then writes it downstream.
//                    Nothing is watching the client socket, so the upstream
//                    generation finishes for a response that is discarded.
//       /cancelling  a second virtual thread blocks on a read of the CLIENT
//                    socket. A read returning -1 is the hang-up, and it
//                    calls interrupt() on the worker.
//
// The Java-specific finding this file exists to check
//     Thread.interrupt() has historically been useless against a blocking
//     socket read: java.net.Socket streams are not interruptible channels,
//     so the interrupt sets a flag nobody looks at until the read returns
//     on its own. Under Loom, socket I/O on a VIRTUAL thread is implemented
//     on top of NIO and the platform carrier is never actually blocked --
//     so interrupt() unparks the virtual thread and the read throws. The
//     table below is the check, not the claim: run it and see which column
//     the /cancelling row lands in.
//
// What to look for
//     - /naive: upstream decodes all 40 tokens, ~3.5s of it after the
//       client stopped listening.
//     - /cancelling: how quickly the upstream sees EOF, and whether it sees
//       it at all. If it does not, interrupt() did not reach the read and
//       you would need to close the upstream socket from the watcher --
//       the C++ answer, in Java.
//
// No dependencies. Binds 127.0.0.1 only. Runs with no arguments:
//     cd java && javac CancelPropagation.java -d /tmp/javabuild \
//       && java -cp /tmp/javabuild CancelPropagation

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public class CancelPropagation {
    static final int TOKENS = 40;
    static final long TOKEN_INTERVAL_MS = 100;        // 4.0s of "decode"
    static final long CLIENT_HANGS_UP_AFTER_MS = 500;

    record Observation(boolean aborted, int tokens, double seconds) {}

    static final List<Observation> LEDGER = new ArrayList<>();

    static void record(Observation o) {
        synchronized (LEDGER) {
            LEDGER.add(o);
        }
    }

    // ---------------------------------------------------------------------
    // The stub model server. A read returning -1 on its own socket is the
    // only signal it gets that the consumer of this generation has gone.
    // ---------------------------------------------------------------------
    static void upstreamConnection(Socket sock) {
        long start = System.nanoTime();
        int sent = 0;
        boolean aborted = false;
        try (sock) {
            InputStream in = sock.getInputStream();
            OutputStream out = sock.getOutputStream();
            readRequestHead(in);
            out.write("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                    .getBytes(StandardCharsets.US_ASCII));
            out.flush();

            // A virtual thread parked on read() costs nothing, which is what
            // makes "one watcher per in-flight request" a reasonable design
            // rather than a thread-count disaster.
            Thread peerWatcher = Thread.ofVirtual().start(() -> {
                try {
                    if (in.read() == -1) sock.close();
                } catch (IOException ignored) {
                    // socket closed under us: same conclusion
                }
            });

            for (int i = 0; i < TOKENS; i++) {
                if (sock.isClosed()) { aborted = true; break; }
                try {
                    out.write(("data: token " + i + "\n\n").getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                } catch (IOException e) {
                    aborted = true;
                    break;
                }
                sent = i + 1;
                Thread.sleep(TOKEN_INTERVAL_MS);
            }
            peerWatcher.interrupt();
        } catch (IOException | InterruptedException e) {
            aborted = true;
        } finally {
            record(new Observation(aborted, sent, (System.nanoTime() - start) / 1e9));
        }
    }

    // ---------------------------------------------------------------------
    // The gateway.
    // ---------------------------------------------------------------------
    static void gatewayConnection(Socket client, int upstreamPort) {
        try (client) {
            String head = readRequestHead(client.getInputStream());
            boolean cancelling = head.contains("/cancelling");

            Socket upstream = new Socket(InetAddress.getLoopbackAddress(), upstreamPort);
            Thread worker = Thread.currentThread();
            Thread watcher = null;

            if (cancelling) {
                InputStream fromClient = client.getInputStream();
                watcher = Thread.ofVirtual().start(() -> {
                    try {
                        if (fromClient.read() == -1) {
                            // The whole fix. On a virtual thread the worker
                            // is parked on NIO, not on a carrier, so this
                            // interrupt reaches the blocked read.
                            worker.interrupt();
                        }
                    } catch (IOException ignored) {
                        worker.interrupt();
                    }
                });
            }

            try (upstream) {
                OutputStream up = upstream.getOutputStream();
                up.write(("POST /completions HTTP/1.1\r\nHost: localhost\r\n"
                        + "Content-Length: 0\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
                up.flush();

                // Buffer the upstream response, then reply. Deliberate: a
                // streaming forward would fail its first downstream write
                // and tear the upstream down that way, which is a second
                // safety net that would hide whether interrupt() worked.
                InputStream from = upstream.getInputStream();
                byte[] buf = new byte[4096];
                var body = new java.io.ByteArrayOutputStream();
                int n;
                while ((n = from.read(buf)) > 0) body.write(buf, 0, n);
                client.getOutputStream().write(body.toByteArray());
            } catch (IOException e) {
                // Interrupting a virtual thread blocked in socket I/O closes
                // the channel and surfaces here.
            } finally {
                if (watcher != null) watcher.interrupt();
                Thread.interrupted();  // clear the flag before the thread is reused
            }
        } catch (IOException ignored) {
        }
    }

    static String readRequestHead(InputStream in) throws IOException {
        byte[] buf = new byte[2048];
        int n = in.read(buf);
        return n <= 0 ? "" : new String(buf, 0, n, StandardCharsets.US_ASCII);
    }

    // A raw socket, on purpose: an HTTP client's timeout returns control to
    // your code without necessarily closing the TCP connection, so the
    // server would see nothing and the experiment would measure something
    // else entirely.
    static void hangUpOn(int gatewayPort, String path) throws Exception {
        Socket sock = new Socket(InetAddress.getLoopbackAddress(), gatewayPort);
        sock.getOutputStream().write(("POST " + path + " HTTP/1.1\r\nHost: localhost\r\n"
                + "Content-Length: 0\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        sock.getOutputStream().flush();
        sock.setSoTimeout((int) CLIENT_HANGS_UP_AFTER_MS);
        try {
            byte[] buf = new byte[4096];
            while (sock.getInputStream().read(buf) > 0) { /* read until we give up */ }
        } catch (IOException expected) {
            // the read timeout: this is the moment the user closes the tab
        }
        sock.close();  // the hang-up
    }

    public static void main(String[] args) throws Exception {
        ExecutorService vthreads = Executors.newVirtualThreadPerTaskExecutor();

        ServerSocket upstreamLn = new ServerSocket(0, 16, InetAddress.getLoopbackAddress());
        vthreads.submit(() -> {
            while (!upstreamLn.isClosed()) {
                Socket s = upstreamLn.accept();
                vthreads.submit(() -> upstreamConnection(s));
            }
            return null;
        });

        ServerSocket gatewayLn = new ServerSocket(0, 16, InetAddress.getLoopbackAddress());
        vthreads.submit(() -> {
            while (!gatewayLn.isClosed()) {
                Socket s = gatewayLn.accept();
                vthreads.submit(() -> gatewayConnection(s, upstreamLn.getLocalPort()));
            }
            return null;
        });

        System.out.println("Java " + Runtime.version().feature()
                + " / virtual threads - cancellation on client disconnect");
        System.out.printf("  upstream streams %d tokens x %dms = %.1fs of decode%n",
                TOKENS, TOKEN_INTERVAL_MS, TOKENS * TOKEN_INTERVAL_MS / 1000.0);
        System.out.printf("  client hangs up after %.1fs%n%n",
                CLIENT_HANGS_UP_AFTER_MS / 1000.0);
        System.out.printf("  %-14s %-16s %14s %13s %8s%n",
                "handler", "upstream saw", "tokens decoded", "upstream ran", "wasted");
        System.out.println("  " + "-".repeat(70));

        for (String path : new String[] {"/naive", "/cancelling"}) {
            synchronized (LEDGER) { LEDGER.clear(); }
            hangUpOn(gatewayLn.getLocalPort(), path);

            long deadline = System.nanoTime()
                    + TimeUnit.MILLISECONDS.toNanos(TOKENS * TOKEN_INTERVAL_MS + 1000);
            Observation obs = new Observation(false, 0, Double.NaN);
            while (System.nanoTime() < deadline) {
                synchronized (LEDGER) {
                    if (!LEDGER.isEmpty()) { obs = LEDGER.get(0); break; }
                }
                Thread.sleep(50);
            }

            double wasted = Math.max(0.0, obs.seconds() - CLIENT_HANGS_UP_AFTER_MS / 1000.0);
            System.out.printf("  %-14s %-16s %14d %12.2fs %7.2fs%n", path,
                    obs.aborted() ? "cancelled" : "nothing", obs.tokens(),
                    obs.seconds(), wasted);
        }

        System.out.println();
        System.out.println("  'wasted' is decode time spent on a response nobody read. On a");
        System.out.println("  loaded server those KV blocks stayed allocated the whole time,");
        System.out.println("  so the scheduler could not admit somebody who was still waiting.");
        System.out.println();
        System.out.println("  Watch the /cancelling row: it is a direct test of whether");
        System.out.println("  Thread.interrupt() reaches a socket read on a virtual thread.");

        upstreamLn.close();
        gatewayLn.close();
        vthreads.shutdownNow();
        System.exit(0);
    }
}
