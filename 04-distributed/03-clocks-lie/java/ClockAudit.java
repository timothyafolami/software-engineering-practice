// Layer 4 Topic 3 (Part A) -- Java's clocks, audited rather than assumed.
//
// WHAT THIS DEMONSTRATES: four things, in order.
//   1. the clock inventory. System.currentTimeMillis() is wall time;
//      System.nanoTime() is monotonic and comparable ONLY within one JVM;
//      Instant.now() reads the wall clock, so Duration.between(a, b) on two
//      Instants is wall-clock arithmetic wearing a type.
//   2. one span timed twice -- through the application's own Clock, which reads
//      wall time, and through nanoTime() -- with an NTP-style step applied
//      inside two of the spans. Java's genuinely nice answer shows up here: the
//      step is a java.time.Clock implementation passed in, so this experiment
//      needs no LD_PRELOAD, no container and no privileges.
//   3. the footgun specific to this runtime: nanoTime()'s origin is arbitrary
//      and per-JVM. Treat one as epoch nanoseconds -- which is what happens the
//      moment it crosses a process boundary into a trace or a log -- and you get
//      a date, with no error, that is wrong by decades.
//   4. the summary line for the README's record table.
//
// WHAT TO LOOK FOR IN THE OUTPUT: section 3's "nanoTime read as an Instant"
// line. It is a valid, parseable, well-formatted timestamp, and it is nonsense.
// Nothing in the type system objected, because both sides are just longs.
//
//   javac java/ClockAudit.java -d /tmp/javabuild && java -cp /tmp/javabuild ClockAudit

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.function.LongSupplier;

public final class ClockAudit {

    private static final long STEP_BACK_NANOS = -40L * 1_000_000_000L;
    private static final int SPANS = 400;
    private static final long SPAN_WORK_NANOS = 200_000L;

    // --------------------------------------------------------- 1. inventory

    /** Smallest non-zero delta this clock reports. Measured, not documented --
     *  a clock can advertise nanoseconds and tick in milliseconds, and one of
     *  the three below does exactly that. */
    private static long measureResolutionNanos(LongSupplier read, int trials) {
        long smallest = Long.MAX_VALUE;
        for (int i = 0; i < trials; i++) {
            long a = read.getAsLong();
            while (true) {
                long b = read.getAsLong();
                if (b != a) {
                    smallest = Math.min(smallest, Math.abs(b - a));
                    break;
                }
            }
        }
        return smallest;
    }

    private static void inventory() {
        System.out.println("------------------------------------------------------------------------------");
        System.out.println("1. the clocks Java offers, and the scope each one is valid in");
        System.out.println("------------------------------------------------------------------------------");
        System.out.printf("  %-38s%-12s%-22s%s%n", "expression", "kind", "valid across",
                "measured resolution");

        record Row(String name, String kind, String scope, LongSupplier read, long toNanos) {}
        List<Row> rows = List.of(
                new Row("System.currentTimeMillis()", "realtime", "machines",
                        System::currentTimeMillis, 1_000_000L),
                new Row("System.nanoTime()", "monotonic", "THIS JVM ONLY",
                        System::nanoTime, 1L),
                new Row("Instant.now().toEpochMilli()", "realtime", "machines",
                        () -> Instant.now().toEpochMilli(), 1_000_000L),
                new Row("Clock.systemUTC().instant()", "realtime", "machines",
                        () -> Clock.systemUTC().instant().toEpochMilli(), 1_000_000L));
        for (Row r : rows) {
            long res = measureResolutionNanos(r.read(), 20) * r.toNanos();
            System.out.printf("  %-38s%-12s%-22s%12d ns%n", r.name(), r.kind(), r.scope(), res);
        }

        System.out.println();
        System.out.printf("  Clock.systemUTC()          -> %s%n", Clock.systemUTC());
        System.out.printf("  Clock.systemDefaultZone()  -> %s%n", Clock.systemDefaultZone());
        System.out.println("  ^ java.time.Clock is an INTERFACE with a zone and an instant(). That is");
        System.out.println("    the design that makes section 2 possible without touching the OS: you");
        System.out.println("    inject a Clock, and a test injects Clock.fixed(...) or one that steps.");
    }

    // ------------------------------------------ 2. one span, two clocks

    /**
     * The application's own Clock. Every service has one; most read wall time.
     * The offset stands in for an NTP step -- we never touch the system clock,
     * and lab/README.md explains why per-container skew is not possible on this
     * machine anyway. Note that this is a real java.time.Clock, so anything in
     * the codebase that takes a Clock can be tested against a stepping one
     * without a single line of native code.
     */
    static final class SteppableClock extends Clock {
        private final Clock delegate;
        private long offsetNanos;

        SteppableClock(Clock delegate) {
            this.delegate = delegate;
        }

        void step(long nanos) {
            offsetNanos += nanos;
        }

        @Override
        public ZoneId getZone() {
            return delegate.getZone();
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return new SteppableClock(delegate.withZone(zone));
        }

        @Override
        public Instant instant() {
            return delegate.instant().plusNanos(offsetNanos);
        }
    }

    private static void burn(long nanos) {
        long end = System.nanoTime() + nanos;
        while (System.nanoTime() < end) {
            Thread.onSpinWait();
        }
    }

    private static double pct(double[] values, double q) {
        double[] s = values.clone();
        Arrays.sort(s);
        int i = (int) Math.round(q * s.length + 0.5) - 1;
        return s[Math.min(s.length - 1, Math.max(0, i))];
    }

    private static int spanReport(double[] wall, double[] mono) {
        System.out.println();
        System.out.println("------------------------------------------------------------------------------");
        System.out.printf("2. %d identical spans, timed twice, with a %ds step and a +%ds step%n",
                SPANS, STEP_BACK_NANOS / 1_000_000_000L, -STEP_BACK_NANOS / 1_000_000_000L);
        System.out.println("   landing INSIDE two of them, injected through java.time.Clock");
        System.out.println("------------------------------------------------------------------------------");
        System.out.printf("  %-30s%10s%12s%14s%14s%10s%n", "clock", "p50", "p99", "max", "min",
                "negative");

        int negatives = 0;
        String[] names = {"wall (injected Clock)", "monotonic (nanoTime)"};
        double[][] series = {wall, mono};
        for (int k = 0; k < names.length; k++) {
            int neg = 0;
            for (double x : series[k]) {
                if (x < 0) neg++;
            }
            if (k == 0) negatives = neg;
            double lo = Arrays.stream(series[k]).min().orElse(0);
            double hi = Arrays.stream(series[k]).max().orElse(0);
            System.out.printf("  %-30s%10.3f%12.3f%14.1f%14.1f%10d%n", names[k],
                    pct(series[k], 0.50), pct(series[k], 0.99), hi, lo, neg);
        }
        System.out.println("  (milliseconds; 'negative' counts spans that finished before they started)");

        int hot = 0;
        for (int i = 0; i < wall.length; i++) {
            if (wall[i] > wall[hot]) hot = i;
        }
        int lo = Math.max(0, hot - 19);
        int hi = Math.min(wall.length, hot + 21);
        double[] window = Arrays.copyOfRange(wall, lo, hi);
        double[] windowMono = Arrays.copyOfRange(mono, lo, hi);
        System.out.println();
        System.out.printf("  Two samples out of %d were touched: %.0f ms and %.0f ms, against a p50%n",
                SPANS, Arrays.stream(wall).min().orElse(0), Arrays.stream(wall).max().orElse(0));
        System.out.printf("  of %.3f ms. Over all %d spans that is only the max -- one sample in %d%n",
                pct(wall, 0.50), SPANS, SPANS);
        System.out.println("  cannot move a p99 by rank. But dashboards aggregate windows, not runs:");
        System.out.printf("  over the %d spans around the step the wall-clock p99 is %.1f ms against%n",
                window.length, pct(window, 0.99));
        System.out.printf("  a monotonic p99 of %.3f ms. Only the clock differed.%n",
                pct(windowMono, 0.99));
        return negatives;
    }

    // ------------------------------------------------- 3. the Java footgun

    private static boolean footguns() {
        System.out.println();
        System.out.println("------------------------------------------------------------------------------");
        System.out.println("3. the footgun specific to this runtime: nanoTime has no shared origin");
        System.out.println("------------------------------------------------------------------------------");

        long nanos = System.nanoTime();
        long millis = System.currentTimeMillis();
        System.out.printf("  System.nanoTime()            %d%n", nanos);
        System.out.printf("  System.currentTimeMillis()   %d%n", millis);
        System.out.printf("  difference in years          %.1f%n",
                (millis * 1e6 - nanos) / 1e9 / 60 / 60 / 24 / 365.25);

        // The bug: both are longs, so nothing objects when a nanoTime value ends
        // up somewhere that expects epoch nanos -- a span exporter, a JSON field,
        // a database column. The result parses, formats and sorts. It is wrong by
        // decades and there is no error anywhere.
        Instant honest = Instant.now();
        Instant nonsense = Instant.ofEpochSecond(nanos / 1_000_000_000L,
                nanos % 1_000_000_000L);
        System.out.printf("%n  Instant.now()                     %s%n", honest);
        System.out.printf("  nanoTime read as an Instant       %s   <-- WRONG, and silent%n",
                nonsense.atOffset(ZoneOffset.UTC));
        System.out.println("  Both are longs. Nothing in the type system distinguishes 'nanoseconds");
        System.out.println("  since the epoch' from 'nanoseconds since an unspecified origin', and");
        System.out.println("  the second of those is not even the same origin in the next JVM.");

        // And the quieter half: Duration.between on two Instants is wall-clock
        // arithmetic. It looks like the type-safe option and it is not.
        SteppableClock c = new SteppableClock(Clock.systemUTC());
        Instant a = c.instant();
        burn(2_000_000L);
        c.step(STEP_BACK_NANOS);
        Instant b = c.instant();
        Duration wall = Duration.between(a, b);
        long m0 = System.nanoTime();
        burn(2_000_000L);
        long monoNanos = System.nanoTime() - m0;
        System.out.println();
        System.out.printf("  Duration.between(a, b) across a %ds step   %s%n",
                STEP_BACK_NANOS / 1_000_000_000L, wall);
        System.out.printf("  the same span via nanoTime                   PT%.6fS%n",
                monoNanos / 1e9);
        System.out.println("  Duration is signed, so Java hands you a negative one without a word.");
        System.out.println("  Compare rust/clock_audit, where the same subtraction returns an Err.");

        System.out.println();
        System.out.printf("  JVM %s (%s), %s %s%n", System.getProperty("java.version"),
                System.getProperty("java.vm.name"), System.getProperty("os.name"),
                System.getProperty("os.arch"));
        System.out.println("  nanoTime()'s origin is fixed per JVM. Restart this process and the");
        System.out.println("  number above changes discontinuously -- which is exactly what happens");
        System.out.println("  to a trace that correlates spans from two services by nanoTime.");
        return wall.isNegative();
    }

    public static void main(String[] args) {
        System.out.println("==============================================================================");
        System.out.println("Layer 4 Topic 3 -- Java clock audit");
        System.out.println("==============================================================================");
        System.out.printf("  Java %s on %s %s%n%n", System.getProperty("java.version"),
                System.getProperty("os.name"), System.getProperty("os.arch"));

        inventory();

        SteppableClock clock = new SteppableClock(Clock.systemUTC());
        List<Double> wall = new ArrayList<>(SPANS);
        List<Double> mono = new ArrayList<>(SPANS);
        // Fixed indices rather than a scheduled task: a timer racing an 80ms loop
        // is how you get a run where the step lands between spans and the
        // experiment silently proves nothing -- the README calls that a broken
        // experiment rather than a wrong prediction.
        int stepBackAt = SPANS / 3;
        int stepFwdAt = 2 * SPANS / 3;
        for (int i = 0; i < SPANS; i++) {
            Instant w0 = clock.instant();
            long m0 = System.nanoTime();
            burn(SPAN_WORK_NANOS);
            if (i == stepBackAt) {
                clock.step(STEP_BACK_NANOS);
            } else if (i == stepFwdAt) {
                clock.step(-STEP_BACK_NANOS);
            }
            wall.add(Duration.between(w0, clock.instant()).toNanos() / 1e6);
            mono.add((System.nanoTime() - m0) / 1e6);
        }
        int negatives = spanReport(wall.stream().mapToDouble(Double::doubleValue).toArray(),
                mono.stream().mapToDouble(Double::doubleValue).toArray());

        boolean reproduced = footguns();

        System.out.println();
        System.out.println("------------------------------------------------------------------------------");
        System.out.println("4. one line for the record table in the README");
        System.out.println("------------------------------------------------------------------------------");
        long res = measureResolutionNanos(System::nanoTime, 20);
        System.out.printf("  | Java | System.nanoTime() | %d ns | %s (%d negative wall-clock span%s) |%n",
                res, reproduced ? "yes" : "NO -- investigate", negatives,
                negatives == 1 ? "" : "s");
        System.out.println();
        System.out.println("  The table in the README stays blank until you fill it in. This line is");
        System.out.println("  the measurement, not the answer -- copy it across yourself.");
    }
}
