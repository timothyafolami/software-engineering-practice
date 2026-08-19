// Layer 6 Topic 3 - Losing trace context at a Java concurrency boundary.
//
// What this demonstrates
// ----------------------
// OpenTelemetry's Java `Context` is ThreadLocal-backed. That means it survives
// every ordinary method call for free -- and dies at every `ExecutorService`
// handoff, because the task runs on a pool thread that was created before your
// request existed.
//
// The library's own fix is `Context.taskWrapping(executor)`, which returns an
// executor that captures the current context at submit time and re-scopes it
// inside the task. This file implements that wrapper in about fifteen lines so
// that what it does is on the page. No OpenTelemetry SDK is installed on this
// machine and none is needed: the failure is a property of ThreadLocal plus a
// thread pool.
//
// Java is also the only runtime in this topic where the fix and the Layer 1
// concurrency material are literally the same change. Virtual threads alter the
// ECONOMICS, not the semantics:
//
//   * Semantics unchanged: submitting to a virtual-thread executor still loses
//     the context, for exactly the same reason. Run 3 shows that.
//   * Economics changed: one virtual thread per request means the thread-local
//     is per-request again, and no thread is ever reused by a second request --
//     which removes the STALE-context failure that a platform pool can produce.
//     Run 4 shows a platform pool producing C++'s "WRONG, inherited from the
//     previous request" verdict, and the same code on virtual threads not
//     producing it.
//
// Java 21's `ScopedValue` is the structured answer to the same problem: an
// immutable binding with a defined lifetime instead of a mutable slot that
// somebody has to remember to clear.
//
// What to look for in the output
// ------------------------------
// The shared shape:
//
//   caller trace_id   <id>
//   callee trace_id   <id or "none">   naive
//   callee trace_id   <id>             propagated
//   verdict           lost | preserved | WRONG (inherited from previous request)
//
// Then run 4's table, which is the one worth staring at: the platform pool
// reports a real, complete, wrong trace ID; the virtual-thread version reports
// "none". "None" is the better bug.

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;

public final class LoseTheContext {

    // -----------------------------------------------------------------------
    // A minimal span and the W3C traceparent codec.
    // -----------------------------------------------------------------------

    record Span(String name, String traceId, String spanId, boolean sampled) {
        static Span create(String name) {
            return new Span(name, randomHex(16), randomHex(8), true);
        }

        String traceparent() {
            return "00-" + traceId + "-" + spanId + "-" + (sampled ? "01" : "00");
        }

        static Optional<Span> fromTraceparent(String header, String name) {
            String[] parts = header.split("-");
            if (parts.length != 4 || !parts[0].equals("00")
                    || parts[1].length() != 32 || parts[2].length() != 16) {
                return Optional.empty();
            }
            int flags = Integer.parseInt(parts[3], 16);
            return Optional.of(new Span(name, parts[1], parts[2], (flags & 1) == 1));
        }
    }

    private static String randomHex(int bytes) {
        StringBuilder sb = new StringBuilder(bytes * 2);
        for (int i = 0; i < bytes; i++) {
            sb.append(String.format("%02x", ThreadLocalRandom.current().nextInt(256)));
        }
        return sb.toString();
    }

    // -----------------------------------------------------------------------
    // The context: a ThreadLocal, which is what io.opentelemetry.context.Context
    // is underneath. `makeCurrent()` returns a Scope you are obliged to close;
    // forgetting to close it is how a pool thread ends up holding a stale span.
    // -----------------------------------------------------------------------

    static final class Context {
        private static final ThreadLocal<Span> CURRENT = new ThreadLocal<>();

        static Span current() {
            return CURRENT.get();
        }

        static String currentTraceId() {
            Span s = CURRENT.get();
            return s == null ? "none" : s.traceId();
        }

        /** Installs `span` and returns the previous value, restoring on close. */
        static Scope makeCurrent(Span span) {
            Span previous = CURRENT.get();
            CURRENT.set(span);
            return () -> {
                if (previous == null) {
                    CURRENT.remove();
                } else {
                    CURRENT.set(previous);
                }
            };
        }

        /** Set without a scope: the shape that leaves a pool thread dirty. */
        static void setForever(Span span) {
            CURRENT.set(span);
        }

        /**
         * This is Context.taskWrapping: capture at SUBMIT time on the calling
         * thread, re-scope at RUN time on the pool thread.
         */
        static Runnable wrap(Runnable task) {
            Span captured = CURRENT.get();
            return () -> {
                try (Scope ignored = makeCurrent(captured)) {
                    task.run();
                }
            };
        }

        static <T> Callable<T> wrap(Callable<T> task) {
            Span captured = CURRENT.get();
            return () -> {
                try (Scope ignored = makeCurrent(captured)) {
                    return task.call();
                }
            };
        }
    }

    interface Scope extends AutoCloseable {
        @Override
        void close();
    }

    // -----------------------------------------------------------------------
    // Structured logging: read the ThreadLocal per record.
    // -----------------------------------------------------------------------

    record LogRecord(String msg, String traceId) {}

    static final List<LogRecord> LOGS = java.util.Collections.synchronizedList(new ArrayList<>());

    static void logInfo(String msg) {
        String id = Context.currentTraceId();
        LOGS.add(new LogRecord(msg, id.equals("none") ? "" : id));
    }

    static String report(String boundary, String caller, String naive, String propagated,
                         String note) {
        String verdict = naive.equals(caller) ? "preserved" : "lost";
        System.out.printf("boundary          %s%n", boundary);
        System.out.printf("caller trace_id   %s%n", caller);
        System.out.printf("callee trace_id   %-32s naive%n", naive);
        System.out.printf("callee trace_id   %-32s propagated%n", propagated);
        System.out.printf("verdict           %s%s%n%n", verdict,
                note.isEmpty() ? "" : "   (" + note + ")");
        return verdict;
    }

    // -----------------------------------------------------------------------
    // Run 1: ExecutorService.submit on a platform-thread pool.
    // -----------------------------------------------------------------------

    static String runPlatformExecutor(ExecutorService pool)
            throws ExecutionException, InterruptedException {
        Span span = Span.create("GET /orders");
        try (Scope ignored = Context.makeCurrent(span)) {

            // Naive: the pool thread has its own (empty) ThreadLocal.
            Future<String> naive = pool.submit(() -> {
                logInfo("pricing call (submitted, naive)");
                return Context.currentTraceId();
            });

            // Propagated: capture at submit, re-scope at run.
            Future<String> fixed = pool.submit(Context.wrap(() -> {
                logInfo("pricing call (submitted, wrapped)");
                return Context.currentTraceId();
            }));

            return report("ExecutorService.submit (platform threads)", span.traceId(),
                    naive.get(), fixed.get(),
                    "fix = Context.taskWrapping(executor), or wrap the task");
        }
    }

    // -----------------------------------------------------------------------
    // Run 2: a queue. Only the message body crosses.
    // -----------------------------------------------------------------------

    record Message(String id, String traceparent) {}

    static String runQueue() {
        Span span = Span.create("POST /orders");

        java.util.function.Function<Message, String> consume = m -> {
            // `worker`, a separate process. Starts from nothing.
            Span restored = m.traceparent() == null ? null
                    : Span.fromTraceparent(m.traceparent(), "job").orElse(null);
            try (Scope ignored = Context.makeCurrent(restored)) {
                logInfo("processing job " + m.id());
                return Context.currentTraceId();
            }
        };

        String naive = consume.apply(new Message("naive", null));
        String propagated = consume.apply(new Message("propagated", span.traceparent()));

        return report("Postgres-backed queue", span.traceId(), naive, propagated,
                "the transport carries no headers; put traceparent in the body");
    }

    // -----------------------------------------------------------------------
    // Run 3: virtual threads. Same semantics, different economics.
    // -----------------------------------------------------------------------

    static String runVirtualExecutor() throws ExecutionException, InterruptedException {
        Span span = Span.create("GET /orders");
        try (ExecutorService vpool = Executors.newVirtualThreadPerTaskExecutor();
             Scope ignored = Context.makeCurrent(span)) {

            Future<String> naive = vpool.submit(Context::currentTraceId);
            Future<String> fixed = vpool.submit(Context.wrap(Context::currentTraceId));

            return report("virtual thread per task, submit", span.traceId(),
                    naive.get(), fixed.get(),
                    "virtual threads change the cost of a thread, not the semantics of a ThreadLocal");
        }
    }

    // -----------------------------------------------------------------------
    // Run 4: the stale-context failure -- Java's version of the C++ result.
    //
    // Three requests, one pool thread. A and B set the context without a scope
    // (the `try`-less form, which is what a hand-rolled filter usually looks
    // like). C's handler assumes context is already there and sets nothing. On
    // a platform pool, C sees B's. On virtual threads, C sees nothing, because
    // its thread never served anyone else.
    // -----------------------------------------------------------------------

    record Observation(String request, String observed, String truth, boolean setContext) {}

    static String runStaleContext() throws Exception {
        List<Observation> platform = serveThree(Executors.newSingleThreadExecutor());
        List<Observation> virtual = serveThree(Executors.newVirtualThreadPerTaskExecutor());

        Observation c = platform.get(2);
        String verdict;
        if (c.observed().equals(c.truth())) {
            verdict = "preserved";
        } else if (c.observed().equals("none")) {
            verdict = "lost";
        } else {
            verdict = "WRONG (inherited from previous request)";
        }

        System.out.printf("boundary          single pool thread, context set without a Scope%n");
        System.out.printf("caller trace_id   %s   (req-C's real trace)%n", c.truth());
        System.out.printf("callee trace_id   %-32s naive (platform pool)%n", c.observed());
        System.out.printf("callee trace_id   %-32s naive (virtual threads)%n",
                virtual.get(2).observed());
        System.out.printf("verdict           %s%n%n", verdict);

        System.out.printf("  %-8s %-9s %-34s %-34s%n",
                "request", "sets ctx", "observed (platform pool)", "observed (virtual threads)");
        for (int i = 0; i < 3; i++) {
            Observation p = platform.get(i);
            Observation v = virtual.get(i);
            String flag = p.observed().equals(p.truth()) ? "" : "   <-- MISMATCH";
            System.out.printf("  %-8s %-9s %-34s %-34s%s%n", p.request(),
                    p.setContext() ? "yes" : "no", p.observed(), v.observed(), flag);
        }
        System.out.println();
        System.out.println("  The platform pool hands req-C a complete, plausible, wrong trace ID.");
        System.out.println("  The virtual-thread run hands it nothing, which is a bug you can see.");
        System.out.println("  Reuse is what makes a ThreadLocal lie; one thread per request is");
        System.out.println("  what makes it stop. That is Layer 1's change, arriving as a");
        System.out.println("  correctness property rather than a throughput one.");
        System.out.println();
        return verdict;
    }

    private static List<Observation> serveThree(ExecutorService pool) throws Exception {
        List<Observation> out = new ArrayList<>();
        try (pool) {
            List<Map.Entry<String, Boolean>> requests = List.of(
                    Map.entry("req-A", true),
                    Map.entry("req-B", true),
                    Map.entry("req-C", false));
            for (Map.Entry<String, Boolean> req : requests) {
                Span span = Span.create(req.getKey());
                boolean setContext = req.getValue();
                String observed = pool.submit(() -> {
                    if (setContext) {
                        Context.setForever(span); // no Scope, so nothing restores it
                    }
                    logInfo("handling " + span.name());
                    return Context.currentTraceId();
                }).get();
                out.add(new Observation(req.getKey(), observed, span.traceId(), setContext));
            }
        }
        return out;
    }

    // -----------------------------------------------------------------------
    // Run 5: the outbound HTTP call -- the easy half, made concrete.
    // -----------------------------------------------------------------------

    static String runHttp() {
        Span span = Span.create("GET /orders");
        String header = span.traceparent();
        Span downstream = Span.fromTraceparent(header, "GET /price").orElseThrow();
        System.out.printf("boundary          HTTP request to pricing%n");
        System.out.printf("caller trace_id   %s%n", span.traceId());
        System.out.printf("traceparent sent  %s%n", header);
        System.out.printf("callee trace_id   %-32s parsed from the header%n", downstream.traceId());
        System.out.printf("verdict           preserved   (this is what being a W3C standard buys)%n%n");
        return "preserved";
    }

    public static void main(String[] args) throws Exception {
        System.out.println("Layer 6 Topic 3 - losing trace context in Java (ThreadLocal-backed Context)");
        System.out.printf("java %s   %s/%s%n", System.getProperty("java.version"),
                System.getProperty("os.name"), System.getProperty("os.arch"));
        System.out.println("=".repeat(72));
        System.out.println();

        ExecutorService pool = Executors.newFixedThreadPool(2);
        // Force the pool threads to exist before any request does. That single
        // fact is the whole of run 1.
        pool.submit(() -> null).get();

        List<String[]> rows = new ArrayList<>();
        rows.add(new String[]{"ExecutorService.submit", runPlatformExecutor(pool),
                "YOU carry it - wrap the executor"});
        rows.add(new String[]{"virtual thread submit", runVirtualExecutor(),
                "YOU carry it - identical semantics"});
        rows.add(new String[]{"stale ThreadLocal", runStaleContext(),
                "YOU carry it - and it lies"});
        rows.add(new String[]{"Postgres queue", runQueue(),
                "YOU carry it - in the message body"});
        rows.add(new String[]{"http traceparent", runHttp(),
                "the wire format carries it"});

        pool.shutdown();
        pool.awaitTermination(5, TimeUnit.SECONDS);

        System.out.println("--- Summary: what the JVM carries across a handoff (nothing) ---");
        for (String[] r : rows) {
            System.out.printf("  %-26s %-40s %s%n", r[0], r[1], r[2]);
        }
        System.out.println();
        System.out.println("  A ThreadLocal is per-thread, and a pool's threads are per-pool.");
        System.out.println("  Every row above follows from those two sentences. Java 21's");
        System.out.println("  ScopedValue is the structured fix: a binding with a lifetime,");
        System.out.println("  rather than a slot somebody has to remember to clear.");
        System.out.println();

        System.out.println("--- The one-query test, on the log lines this run emitted ---");
        long withId = LOGS.stream().filter(r -> !r.traceId().isEmpty()).count();
        System.out.printf("  log lines emitted            %d%n", LOGS.size());
        System.out.printf("  lines carrying a trace_id    %d%n", withId);
        System.out.printf("  lines carrying nothing       %d   <- unqueryable by request%n",
                LOGS.size() - withId);
        for (LogRecord r : LOGS) {
            System.out.printf("    %-28s trace_id=%s%n", r.msg(),
                    r.traceId().isEmpty() ? "(empty)" : r.traceId());
        }
        System.out.println();
        System.out.println("  Some of those ids are correct and some belong to the previous");
        System.out.println("  request on the same thread. Nothing in the log pipeline can");
        System.out.println("  tell them apart, which is why run 4 is the important one.");
    }
}
