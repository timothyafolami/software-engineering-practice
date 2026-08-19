// Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.
//
// Why Java: two things only the JVM shows you.
//
// First, the SLF4J placeholder myth. Every Java codebase has been told to write
// log.debug("payload={}", payload) instead of string concatenation, and that
// advice is real -- it defers *formatting*. It does not defer *evaluation*. If
// the argument is a method call, that method runs whether or not the level is
// enabled, and the varargs call allocates an Object[] to hold it either way.
// Row 5 below is that exact line, disabled, and it is not free.
//
// Second, the JIT. The same bytecode has two different costs depending on how
// long the process has been up: interpreted at first, then C1, then C2 once a
// method is hot. This file measures the INFO log line cold and warm and prints
// both. It matters beyond benchmarking -- it is why the first few thousand
// requests after a deploy are slower than the steady state, and why a p99
// computed over a rolling window that includes a rollout is measuring the JIT
// rather than your code.
//
// What this demonstrates
// ----------------------
//   1. counter add       - HashMap lookup on a bounded label key
//   2. span record       - object allocation, timestamps, six attributes
//   3. log line (INFO)   - StringBuilder JSON into a counting sink
//   4. debug, DISABLED, string concatenation                    <- the bug
//   5. debug, DISABLED, {} placeholder with an evaluated arg    <- still the bug
//   6. debug, DISABLED, isDebugEnabled guard                    <- the fix
//
// Rows 4, 5 and 6 emit nothing at all.
//
// What to look for in the output
// ------------------------------
//   - row 5 against row 6. The placeholder did not save you.
//   - the cold/warm block. If you benchmark JVM telemetry without warm-up you
//     will report the interpreter's number and be wrong by a large factor.
//
// Run:
//   javac SignalCost.java -d /tmp/javabuild && java -cp /tmp/javabuild SignalCost

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class SignalCost {

    static final int ITERATIONS = 200_000;
    static final int WARMUP = 50_000;   // enough for C2 to compile these methods

    // Printed at the end so the JIT cannot decide the work below is dead. The
    // JVM does eliminate provably-unused allocation (scalar replacement after
    // escape analysis), so this is not decoration.
    static long sink = 0;

    enum Level {
        DEBUG(20), INFO(30);
        final int value;
        Level(int v) { this.value = v; }
    }

    static final class CountingSink {
        long bytes = 0;
        long lines = 0;
        void write(String line) {
            bytes += line.length();
            lines += 1;
        }
    }

    /** The shape of every Java logging facade: a level, and methods that check it. */
    static final class Logger {
        final CountingSink sink = new CountingSink();
        final Level level;
        Logger(Level level) { this.level = level; }

        boolean isDebugEnabled() { return Level.DEBUG.value >= level.value; }

        void info(String line) {
            if (Level.INFO.value >= level.value) sink.write(line);
        }

        /** Plain form: the caller has already built the String. */
        void debug(String line) {
            if (Level.DEBUG.value >= level.value) sink.write(line);
        }

        /**
         * SLF4J's form. The formatting is deferred to here -- but `args` was
         * already evaluated at the call site, and the varargs call already
         * allocated an Object[] to carry it. Deferring the format string does
         * not defer the arguments. This is the misunderstanding.
         */
        void debug(String pattern, Object... args) {
            if (Level.DEBUG.value < level.value) return;
            StringBuilder sb = new StringBuilder(pattern.length() + 64);
            int argIndex = 0, i = 0;
            while (i < pattern.length()) {
                if (i + 1 < pattern.length() && pattern.charAt(i) == '{' && pattern.charAt(i + 1) == '}'
                        && argIndex < args.length) {
                    sb.append(args[argIndex++]);
                    i += 2;
                } else {
                    sb.append(pattern.charAt(i++));
                }
            }
            sink.write(sb.toString());
        }
    }

    static final class Span {
        final String name;
        final String traceId;
        final String spanId;
        final Map<String, Object> attributes;
        final long startNs;
        long endNs;
        Span(String name, String traceId, String spanId, Map<String, Object> attributes) {
            this.name = name;
            this.traceId = traceId;
            this.spanId = spanId;
            this.attributes = attributes;
            this.startNs = System.nanoTime();
        }
        void end() { this.endNs = System.nanoTime(); }
    }

    record Row(String label, double nsPerOp) {}

    static double timeOnly(int iterations, Runnable body) {
        long start = System.nanoTime();
        for (int i = 0; i < iterations; i++) body.run();
        return (System.nanoTime() - start) / (double) iterations;
    }

    static Row bench(String label, Runnable body) {
        for (int i = 0; i < WARMUP; i++) body.run();
        return new Row(label, timeOnly(ITERATIONS, body));
    }

    /** Stands in for the toString/serialisation a real debug line performs. */
    static String expensiveArgument(String orderId, String customerId, double discount) {
        return new StringBuilder(160)
                .append("pricing payload={\"order_id\":\"").append(orderId)
                .append("\",\"customer_id\":\"").append(customerId)
                .append("\",\"discount\":").append(discount)
                .append(",\"items\":[{\"sku\":\"SKU-1\",\"qty\":2},{\"sku\":\"SKU-7\",\"qty\":1}]}")
                .toString();
    }

    public static void main(String[] args) {
        Map<String, Long> counter = new HashMap<>();
        // INFO, so every debug call below is disabled -- the production config.
        Logger logger = new Logger(Level.INFO);

        String labelKey = "GET|/orders/{id}|200";
        Map<String, Object> attributes = new HashMap<>();
        attributes.put("http.request.method", "GET");
        attributes.put("http.route", "/orders/{id}");
        attributes.put("http.response.status_code", 200);
        attributes.put("db.system.name", "postgresql");
        attributes.put("customer.id", "cus_00194");
        attributes.put("order.id", "ord_8f31c2");

        Runnable infoLog = () -> logger.info(new StringBuilder(128)
                .append("{\"level\":\"info\",\"msg\":\"order priced\",\"order_id\":\"ord_8f31c2\",")
                .append("\"customer_id\":\"cus_00194\",\"duration_ms\":12.4}")
                .toString());

        // Cold: no warm-up at all, straight from the interpreter. Measured
        // before anything else has had a chance to make this method hot.
        double coldInfo = timeOnly(2_000, infoLog);

        List<Row> rows = new ArrayList<>();

        rows.add(bench("counter.add (3 bounded labels)", () -> {
            counter.merge(labelKey, 1L, Long::sum);
            sink += counter.get(labelKey) & 1L;
        }));

        rows.add(bench("span create + end (6 attrs)", () -> {
            Span span = new Span("GET /orders/{id}", "4bf92f3577b34da6a3ce929d0e0e4736",
                    "00f067aa0ba902b7", attributes);
            span.end();
            sink += (span.endNs - span.startNs) + span.attributes.size();
        }));

        rows.add(bench("log INFO, one JSON line", infoLog));

        rows.add(bench("log DEBUG (disabled), string concatenation", () -> {
            // THE BUG in its most obvious form. The concatenation happens
            // before debug() is entered.
            logger.debug("pricing payload=" + expensiveArgument("ord_8f31c2", "cus_00194", 0.15));
        }));

        rows.add(bench("log DEBUG (disabled), {} placeholder", () -> {
            // THE BUG in the form people believe is the fix. The {} defers
            // formatting. It does not defer expensiveArgument(), and the
            // varargs call allocates an Object[1] to carry the result.
            logger.debug("pricing payload={}", expensiveArgument("ord_8f31c2", "cus_00194", 0.15));
        }));

        rows.add(bench("log DEBUG (disabled), isDebugEnabled guard", () -> {
            // THE FIX. One field read and a comparison.
            if (logger.isDebugEnabled()) {
                logger.debug("pricing payload={}", expensiveArgument("ord_8f31c2", "cus_00194", 0.15));
            }
        }));

        String bar = "=".repeat(74);
        System.out.println(bar);
        System.out.printf("COST OF EMITTING ONE UNIT OF TELEMETRY   (JDK %s, n=%d)%n",
                System.getProperty("java.version"), ITERATIONS);
        System.out.println(bar);
        System.out.printf("%-46s%12s%n", "operation", "ns/op");
        for (Row r : rows) System.out.printf("%-46s%12.1f%n", r.label(), r.nsPerOp());

        double concat = rows.get(3).nsPerOp();
        double placeholder = rows.get(4).nsPerOp();
        double guarded = rows.get(5).nsPerOp();
        System.out.printf("%nRows 4, 5 and 6 all emit nothing at all.%n");
        System.out.printf("  string concatenation  : %8.1f ns%n", concat);
        System.out.printf("  {} placeholder        : %8.1f ns%n", placeholder);
        System.out.printf("  isDebugEnabled guard  : %8.1f ns%n", guarded);
        System.out.printf("The placeholder saved %.1f ns of the %.1f ns. The guard saved %.1f.%n",
                concat - placeholder, concat - guarded, concat - guarded);
        System.out.printf("Deferring the format string is not deferring the argument. That is%n");
        System.out.printf("the whole lesson of this file, and it is a one-line PR.%n");

        double warmInfo = rows.get(2).nsPerOp();
        System.out.printf("%nJIT WARM-UP, same INFO log line, same bytecode:%n");
        System.out.printf("  cold (first 2,000 calls, interpreted) : %8.1f ns/op%n", coldInfo);
        System.out.printf("  warm (after %,d calls, C2 compiled)  : %8.1f ns/op%n", WARMUP, warmInfo);
        System.out.printf("  ratio                                 : %8.1fx%n", coldInfo / warmInfo);
        System.out.printf("A JVM benchmark without warm-up reports the first number and calls%n");
        System.out.printf("it the cost of logging. It is the cost of the interpreter.%n");

        System.out.printf("%nBytes written by the INFO logs: %d over %d lines (%.0f B/line).%n",
                logger.sink.bytes, logger.sink.lines,
                logger.sink.lines == 0 ? 0.0 : (double) logger.sink.bytes / logger.sink.lines);
        System.out.printf("(sink=%d, printed so nothing above can be optimised away)%n", sink);
    }
}
