// Layer 8 Topic 3 - Java: the language that put the categories in the type system,
// and the ecosystem that then largely rejected it.
//
// WHAT THIS DEMONSTRATES: checked exceptions ARE category 1 made mandatory. They
// failed in practice for two reasons this file shows rather than asserts -- the
// granularity was wrong (`throws IOException` on everything), and the escape
// hatch (wrap in a RuntimeException) was easier than the correct handling.
//
// Then the hazard that is Java's alone and bites hardest in 2026 code:
// `catch (Exception e)` also catches InterruptedException, and swallowing that
// one breaks cancellation for EVERYTHING above you -- including virtual threads,
// structured concurrency and every executor shutdown.
//
// WHAT TO LOOK FOR: the BROKEN worker keeps running for the full duration after
// being interrupted. The FIXED one stops within a few milliseconds. Same
// interrupt, same code, one `catch` block different.
//
//   cd java && javac ErrorTaxonomy.java -d /tmp/t3java && java -cp /tmp/t3java ErrorTaxonomy

import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

public class ErrorTaxonomy {

    // --- the taxonomy, unchecked, which is where modern Java landed ---------

    /** Category 1: the caller does something specific. */
    static class NotFoundException extends RuntimeException {
        final long id;
        NotFoundException(long id) { super("order " + id + " not found"); this.id = id; }
    }

    /** Category 2: the same call, unchanged, might succeed later. */
    static class UnavailableException extends RuntimeException {
        final Duration retryAfter;
        UnavailableException(String dep, Duration retryAfter, Throwable cause) {
            super(dep + " unavailable", cause);   // cause preserved: Java's `raise ... from`
            this.retryAfter = retryAfter;
        }
    }

    // --- the checked-exception story, in eight lines ------------------------

    /** Checked: the compiler forces every caller to acknowledge this. */
    static long loadOrderChecked(long id) throws java.io.IOException {
        throw new java.io.IOException("dial tcp 10.0.0.7:5432: connection refused");
    }

    /** THE ESCAPE HATCH, and the reason checked exceptions lost.
     *  It compiles, it is shorter than handling the error, and it converts a
     *  contract the compiler was enforcing into one nobody is. Note that it also
     *  DESTROYS the category: the caller now sees RuntimeException and cannot
     *  tell a transient failure from a bug. */
    static long loadOrderWrapped(long id) {
        try {
            return loadOrderChecked(id);
        } catch (java.io.IOException e) {
            throw new RuntimeException(e);   // <-- easier than the correct handling
        }
    }

    /** The correct version: translate INTO the taxonomy, keeping the cause and
     *  adding the thing the caller actually needs (how long to wait). */
    static long loadOrderTranslated(long id) {
        try {
            return loadOrderChecked(id);
        } catch (java.io.IOException e) {
            throw new UnavailableException("postgres", Duration.ofSeconds(2), e);
        }
    }

    // --- the InterruptedException hazard ------------------------------------

    static final Duration WORK_UNIT = Duration.ofMillis(20);
    static final int WORK_UNITS = 50;   // ~1s of work if never cancelled

    /** BROKEN: `catch (Exception e)` swallows the interrupt and clears the flag,
     *  so the loop's own cancellation check never fires again. */
    static int workerBROKEN(AtomicInteger done) {
        for (int i = 0; i < WORK_UNITS; i++) {
            if (Thread.currentThread().isInterrupted()) return i;
            try {
                Thread.sleep(WORK_UNIT);
            } catch (Exception e) {
                // Catching InterruptedException CLEARS the interrupt flag. Doing
                // nothing about it means the cancellation signal is now gone --
                // not deferred, gone. Nothing above this frame will ever see it.
            }
            done.incrementAndGet();
        }
        return WORK_UNITS;
    }

    /** FIXED: restore the flag (or rethrow). Two lines. */
    static int workerFIXED(AtomicInteger done) {
        for (int i = 0; i < WORK_UNITS; i++) {
            if (Thread.currentThread().isInterrupted()) return i;
            try {
                Thread.sleep(WORK_UNIT);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();   // restore, so callers can see it
                return i;                             // and stop, because we were asked to
            }
            done.incrementAndGet();
        }
        return WORK_UNITS;
    }

    static void chain(Throwable t) {
        for (Throwable e = t; e != null; e = e.getCause()) {
            System.out.printf("     %s: %s%n", e.getClass().getSimpleName(), e.getMessage());
        }
    }

    public static void main(String[] args) throws Exception {
        System.out.println("=== checked exceptions, and the escape hatch that beat them ===");
        try {
            loadOrderWrapped(7);
        } catch (RuntimeException e) {
            System.out.println("  wrapped    -> " + e.getClass().getSimpleName());
            System.out.println("     the caller sees RuntimeException. Is this retryable or a bug?");
            System.out.println("     The category was destroyed by the wrap, not by the failure.");
        }
        try {
            loadOrderTranslated(7);
        } catch (UnavailableException e) {
            System.out.println("  translated -> 503, Retry-After: " + e.retryAfter.toSeconds() + "s");
            chain(e);
            System.out.println("     the category is in the type and the cause survived.");
        }

        System.out.println();
        System.out.println("=== InterruptedException: one catch block, two cancellation stories ===");
        System.out.printf("  (each worker does %d units of %dms; both are interrupted after ~100ms)%n",
                WORK_UNITS, WORK_UNIT.toMillis());

        // Virtual threads (Java 21+), because this is exactly where the hazard
        // bites hardest: cheap threads make fan-out easy, and a swallowed
        // interrupt makes shutting that fan-out down impossible.
        for (String which : new String[] { "BROKEN", "FIXED" }) {
            AtomicInteger done = new AtomicInteger();
            long start = System.nanoTime();
            Thread t = Thread.ofVirtual().start(() -> {
                if (which.equals("BROKEN")) workerBROKEN(done); else workerFIXED(done);
            });
            Thread.sleep(100);
            t.interrupt();
            t.join();
            long ms = (System.nanoTime() - start) / 1_000_000;
            System.out.printf("  %-7s stopped after %4d ms, %2d units done%s%n",
                    which, ms, done.get(),
                    which.equals("BROKEN") ? "   <- it ignored the interrupt entirely" : "   <- it stopped when asked");
        }
        System.out.println("  -> `catch (Exception e)` is not a wider net, it is a broken one.");
        System.out.println("     Restore the flag or rethrow; there is no third correct option.");
    }
}
