// Layer 4 · Topic 1 — the third outcome, in Java.
//
// WHAT THIS DEMONSTRATES
//   The same five faults and the same ledger as the other five programs, driven
//   through java.net.http.HttpClient (JDK 11+, no dependencies).
//
//   Java's contribution is a trap built into the JDK's own type hierarchy:
//
//       HttpConnectTimeoutException  extends  HttpTimeoutException
//
//   One of those means the TCP handshake never completed, so the request
//   provably never landed and a retry is free. The other means the request was
//   sent and the answer never came. The safe case is a *subclass* of the unsafe
//   one, so the completely natural
//
//       catch (HttpTimeoutException e) { retry(); }
//
//   catches both, and the natural
//
//       catch (HttpTimeoutException e) { giveUp(); }
//
//   gives up on requests that never happened. There is no ordering of catch
//   blocks that is wrong here -- there is only knowing the hierarchy. The
//   program prints it at runtime rather than asserting it, so it is a fact you
//   watched rather than a claim you read.
//
//   The second Java-specific finding is phase 0. HttpClient retries requests
//   with an idempotent method (GET, PUT, DELETE, HEAD) once, by itself, when the
//   connection dies before a response arrives -- and it does not tell you. So a
//   client that has classified its errors perfectly can still double-charge,
//   because the retry happened a layer below the classification. Phase 0 counts
//   the requests the *server* saw, which is the only way to notice.
//
// WHAT TO LOOK FOR
//   1. The printed superclass line, and what it implies about every
//      `catch (HttpTimeoutException)` you have ever written.
//   2. Phase 0's server-side request counts for GET vs POST.
//   3. Phase 1's duplicate charges against phase 2's, and the unresolved
//      ambiguity that survives both.
//
// Build & run:
//   javac java/AmbiguousResult.java -d /tmp/javabuild && java -cp /tmp/javabuild AmbiguousResult

import java.io.IOException;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class AmbiguousResult {

    static final Duration CLIENT_TIMEOUT = Duration.ofMillis(300);
    static final long SLOW_RESPONSE_MS = 1000;
    static final int REQUESTS_PER_MODE = 4;
    static final int MAX_ATTEMPTS = 3;

    static final String[] MODES = {
        "ok", "slow", "hang", "reset", "crash_after_commit", "refused"
    };

    // --- server-side truth --------------------------------------------------

    static final List<String> LEDGER = Collections.synchronizedList(new ArrayList<>());
    static final List<Socket> HELD = Collections.synchronizedList(new ArrayList<>());

    static void commit(String chargeId) {
        LEDGER.add(chargeId);
    }

    /** Minimal HTTP/1.1 over raw sockets: two of the faults are below what an
     *  HttpHandler can express. */
    static ServerSocket startLedgerServer() throws IOException {
        ServerSocket server = new ServerSocket(0, 128,
            java.net.InetAddress.getLoopbackAddress());
        Thread acceptor = new Thread(() -> {
            while (!server.isClosed()) {
                try {
                    Socket socket = server.accept();
                    Thread.ofVirtual().start(() -> serve(socket));
                } catch (IOException e) {
                    return;
                }
            }
        });
        acceptor.setDaemon(true);
        acceptor.start();
        return server;
    }

    static void serve(Socket socket) {
        try {
            socket.setSoTimeout(5000);
            byte[] buf = new byte[4096];
            int n = socket.getInputStream().read(buf);
            if (n <= 0) { socket.close(); return; }
            String request = new String(buf, 0, n, StandardCharsets.ISO_8859_1);
            String path = request.split(" ", 3)[1];       // /charge/<mode>/<id>
            String[] parts = path.split("/", 4);
            if (parts.length < 4) { socket.close(); return; }
            String mode = parts[2], chargeId = parts[3];

            switch (mode) {
                case "ok" -> { commit(chargeId); reply(socket, chargeId); socket.close(); }
                case "slow" -> {
                    commit(chargeId);
                    Thread.sleep(SLOW_RESPONSE_MS);
                    reply(socket, chargeId);
                    socket.close();
                }
                case "hang" -> {
                    // Accepted, committed, never answered.
                    commit(chargeId);
                    HELD.add(socket);
                }
                case "reset" -> {
                    commit(chargeId);
                    // SO_LINGER 0 turns close() into an RST rather than a FIN.
                    socket.setSoLinger(true, 0);
                    socket.close();
                }
                case "crash_after_commit" -> {
                    // The case no timeout tuning can fix: durable work, dead
                    // reporter.
                    commit(chargeId);
                    socket.close();
                }
                default -> socket.close();
            }
        } catch (Exception ignored) {
            // RSTs we caused ourselves, and reads on sockets we just closed.
        }
    }

    static void reply(Socket socket, String chargeId) throws IOException {
        byte[] body = ("{\"charge_id\":\"" + chargeId + "\"}").getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + "Content-Length: " + body.length + "\r\nConnection: close\r\n\r\n";
        OutputStream out = socket.getOutputStream();
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    // --- classification -----------------------------------------------------

    enum Kind { SUCCESS, SAFE, AMBIGUOUS }

    record Outcome(Kind kind, String label) {}

    /**
     * The order of these checks is the entire lesson.
     *
     * HttpConnectTimeoutException must be tested BEFORE HttpTimeoutException,
     * because it is a subclass. Reverse them and every provably-safe connect
     * timeout is misfiled as ambiguous -- which is the conservative direction,
     * and merely loses you availability. Catch only the parent and *retry*, and
     * you get the other direction: duplicate charges.
     */
    static Outcome classify(Exception e) {
        if (e instanceof HttpConnectTimeoutException) {
            return new Outcome(Kind.SAFE, "SAFE(HttpConnectTimeoutException [connect])");
        }
        if (e instanceof HttpTimeoutException) {
            return new Outcome(Kind.AMBIGUOUS, "AMBIGUOUS(HttpTimeoutException [response])");
        }
        if (e instanceof ConnectException) {
            // ECONNREFUSED / EHOSTUNREACH: never left this machine.
            return new Outcome(Kind.SAFE, "SAFE(ConnectException [connect])");
        }
        String msg = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        if (msg.length() > 26) msg = msg.substring(0, 26);
        return new Outcome(Kind.AMBIGUOUS, "AMBIGUOUS(" + msg + " [response])");
    }

    static Outcome attempt(HttpClient client, String url) {
        // POST, not GET, and not only because a charge is a POST in real life:
        // HttpClient silently retries idempotent methods for us (phase 0), and
        // that would make this program measure the JDK's retry policy instead of
        // the one written below.
        HttpRequest request = HttpRequest.newBuilder(URI.create(url))
            .timeout(CLIENT_TIMEOUT)     // response deadline
            .POST(HttpRequest.BodyPublishers.ofString("{}"))
            .build();
        try {
            HttpResponse<String> response =
                client.send(request, HttpResponse.BodyHandlers.ofString());
            return new Outcome(Kind.SUCCESS, "SUCCESS(" + response.statusCode() + ")");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return new Outcome(Kind.AMBIGUOUS, "AMBIGUOUS(interrupted [response])");
        } catch (Exception e) {
            return classify(e);
        }
    }

    // --- phases -------------------------------------------------------------

    record PhaseResult(int duplicates, int unresolved) {}

    static PhaseResult runPhase(String tag, String name, String note,
                                int port, int closedPort, boolean retryAmbiguous) {
        int before = LEDGER.size();
        int unresolved = 0;
        HttpClient client = HttpClient.newBuilder()
            .connectTimeout(CLIENT_TIMEOUT)          // handshake deadline
            .version(HttpClient.Version.HTTP_1_1)
            .build();

        System.out.println();
        System.out.println("  " + name);
        System.out.println("  " + note);
        System.out.printf("  %-20s %-44s %9s %12s%n",
            "fault", "client verdict", "attempts", "ledger rows");

        for (String mode : MODES) {
            int modeBefore = LEDGER.size();
            int attempts = 0;
            Map<String, Integer> counts = new LinkedHashMap<>();
            int target = mode.equals("refused") ? closedPort : port;

            for (int i = 0; i < REQUESTS_PER_MODE; i++) {
                String chargeId = tag + "-" + mode + "-" + i;
                String url = "http://127.0.0.1:" + target + "/charge/" + mode + "/" + chargeId;
                Outcome outcome = new Outcome(Kind.AMBIGUOUS, "AMBIGUOUS(not attempted)");
                for (int a = 0; a < MAX_ATTEMPTS; a++) {
                    attempts++;
                    outcome = attempt(client, url);
                    if (outcome.kind() == Kind.SUCCESS) break;
                    if (outcome.kind() == Kind.SAFE) continue;   // provably safe
                    if (retryAmbiguous) continue;                // the bug
                    break;                                       // correct
                }
                if (outcome.kind() == Kind.AMBIGUOUS) unresolved++;
                counts.merge(outcome.label(), 1, Integer::sum);
            }

            StringBuilder summary = new StringBuilder();
            for (Map.Entry<String, Integer> e : counts.entrySet()) {
                if (summary.length() > 0) summary.append(", ");
                summary.append(e.getValue()).append("x ").append(e.getKey());
            }
            System.out.printf("  %-20s %-44s %9d %12d%n",
                mode, summary, attempts, LEDGER.size() - modeBefore);
        }

        Map<String, Integer> seen = new LinkedHashMap<>();
        synchronized (LEDGER) {
            for (int i = before; i < LEDGER.size(); i++) seen.merge(LEDGER.get(i), 1, Integer::sum);
        }
        int duplicates = 0, rows = 0;
        for (int n : seen.values()) { rows += n; if (n > 1) duplicates += n - 1; }

        System.out.println("  ledger rows written this phase : " + rows);
        System.out.println("  DUPLICATE CHARGES              : " + duplicates
            + "   <- created by this client's retries");
        System.out.println("  unresolved ambiguous outcomes  : " + unresolved
            + "   <- caller cannot tell whether these happened");
        return new PhaseResult(duplicates, unresolved);
    }

    /**
     * Count how many requests the server actually receives for one client call.
     *
     * The server closes every connection without answering, so the client sees
     * one IOException either way. The difference is entirely in what HttpClient
     * did before giving up, and the only place that is visible is the server.
     */
    static void phaseZero(int port) {
        HttpClient client = HttpClient.newBuilder()
            .connectTimeout(CLIENT_TIMEOUT)
            .version(HttpClient.Version.HTTP_1_1)
            .build();

        System.out.println();
        System.out.println("  phase 0 — retries you did not write");
        System.out.printf("  %-8s %-34s %s%n", "method", "client saw", "server received");
        for (String method : new String[] {"GET", "POST"}) {
            int before = LEDGER.size();
            String url = "http://127.0.0.1:" + port + "/charge/crash_after_commit/p0-"
                + method.toLowerCase();
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url))
                .timeout(CLIENT_TIMEOUT);
            HttpRequest request = method.equals("GET")
                ? builder.GET().build()
                : builder.POST(HttpRequest.BodyPublishers.ofString("{}")).build();
            String seen;
            try {
                client.send(request, HttpResponse.BodyHandlers.ofString());
                seen = "unexpected success";
            } catch (Exception e) {
                seen = "1x " + e.getClass().getSimpleName();
            }
            System.out.printf("  %-8s %-34s %d%n", method, seen, LEDGER.size() - before);
        }
        System.out.println("  One call, one exception, two charges. HttpClient retries methods");
        System.out.println("  RFC 9110 calls idempotent when a connection dies before a response.");
        System.out.println("  Your charge endpoint is idempotent only once Topic 2 makes it so, and");
        System.out.println("  the JDK has no way to know that. Phases 1 and 2 below use POST so the");
        System.out.println("  only retries being measured are the ones written in this file.");
    }

    static int findClosedPort() throws IOException {
        try (ServerSocket probe = new ServerSocket(0, 1,
                java.net.InetAddress.getLoopbackAddress())) {
            return probe.getLocalPort();
        }
    }

    public static void main(String[] args) throws Exception {
        ServerSocket server = startLedgerServer();
        int port = server.getLocalPort();
        int closedPort = findClosedPort();

        System.out.println("=".repeat(78));
        System.out.println("Layer 4 · Topic 1 — partial failure and the ambiguous result (Java)");
        System.out.println("=".repeat(78));
        System.out.printf("  ledger        : 127.0.0.1:%d  (in-process, holds server-side truth)%n", port);
        System.out.printf("  closed port   : 127.0.0.1:%d  (for the connect-refused case)%n", closedPort);
        System.out.printf("  client timeout: %s   slow response: %dms   max attempts: %d%n",
            CLIENT_TIMEOUT, SLOW_RESPONSE_MS, MAX_ATTEMPTS);

        // Printed, not asserted: this is the fact the whole Java version turns on.
        System.out.println();
        System.out.println("  the JDK's own hierarchy, read at runtime:");
        System.out.println("    HttpConnectTimeoutException  extends  "
            + HttpConnectTimeoutException.class.getSuperclass().getSimpleName());
        System.out.println("    HttpTimeoutException         extends  "
            + HttpTimeoutException.class.getSuperclass().getSimpleName());
        System.out.println("  So `catch (HttpTimeoutException)` catches the provably-safe case");
        System.out.println("  and the unknowable one in the same block. classify() below tests");
        System.out.println("  the subclass first, which is the only ordering that works.");

        phaseZero(port);

        PhaseResult naive = runPhase("p1",
            "phase 1 — retry on any exception",
            "`catch (Exception e) { retry(); }`, or equally `catch (HttpTimeoutException e)`",
            port, closedPort, true);
        PhaseResult fixed = runPhase("p2",
            "phase 2 — retry only provably-safe exceptions",
            "HttpConnectTimeoutException and ConnectException are retried; nothing else",
            port, closedPort, false);

        System.out.println();
        System.out.println("-".repeat(78));
        System.out.printf("  duplicate charges    phase 1: %-6d phase 2: %d%n",
            naive.duplicates(), fixed.duplicates());
        System.out.printf("  unresolved ambiguity phase 1: %-6d phase 2: %d%n",
            naive.unresolved(), fixed.unresolved());
        System.out.println();
        System.out.println("  The duplicates were the client's doing and the fix removes them.");
        System.out.println("  The ambiguity is the network's and nothing here can remove it.");
        System.out.println("  Making a retry of an ambiguous outcome safe is Topic 2.");

        synchronized (HELD) { for (Socket s : HELD) s.close(); }
        server.close();
    }
}
