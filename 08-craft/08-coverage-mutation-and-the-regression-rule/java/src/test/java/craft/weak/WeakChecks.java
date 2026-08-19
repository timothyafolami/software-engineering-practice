// The three checks that make up the deliberately weak suite, plus one mutant
// applied by hand. This file is the *subject* of the experiment, not
// scaffolding around it.
//
// WHAT THIS DEMONSTRATES: three checks that pass, execute every line of
// `Pagination.page`, and still leave a mutant alive -- because they check the
// *shape* of the answer (how many rows came back, that a zero limit is
// rejected, that the walk terminates) and never the *content* (which rows, and
// what cursor). A surviving mutant names a missing assertion, not a missing
// test: the test that should have killed it is already here and already calls
// the right method.
//
// WHY THE CHECKS LIVE HERE AND NOT IN THE JUnit CLASS: they have no
// dependencies at all, so this file compiles and runs with `javac` and `java`
// alone. That is the only part of this arm that can run on a machine with no
// Maven and no network, and PaginationWeakTest delegates to these same three
// methods -- so there is exactly one copy of the suite, and the JUnit class and
// the offline demo can never drift apart.
//
// WHAT TO LOOK FOR: `main` prints one mutant -- the next cursor read off the
// FIRST row of the page instead of the last -- passing the suite unchanged,
// then prints the single assertion that would have killed it. That is one
// mutant chosen by a human, not a mutation score.
//
// KEEP THIS SUITE WEAK. Every check you add here changes the number PIT prints,
// which is the measurement you are trying to take.

package craft.weak;

import craft.core.Pagination;
import craft.core.Pagination.Page;
import craft.core.Pagination.Row;

import java.util.ArrayList;
import java.util.List;

public final class WeakChecks {

    private WeakChecks() {}

    /** Any implementation of the paging step, so the hand mutant can be run through the same suite. */
    @FunctionalInterface
    public interface PageFn {
        Page apply(List<Row> rows, Long cursor, int limit);
    }

    /** Any implementation of the walk. */
    @FunctionalInterface
    public interface WalkFn {
        List<Row> apply(List<Row> rows, int limit, int maxPages);
    }

    /**
     * Three distinct timestamps: topic 5's tie bug is NOT what this file is
     * about, and a tie here would make the hand mutant look like that bug.
     */
    public static List<Row> fixture() {
        return Pagination.sortedDesc(List.of(new Row(30, 3), new Row(20, 2), new Row(10, 1)));
    }

    // --- the three checks. null means passed; a string names the failure. ----

    /** A page is no longer than the limit. Says nothing about WHICH rows, or about the cursor. */
    public static String checkLimitRespected(PageFn pageFn) {
        Page p = pageFn.apply(fixture(), null, 2);
        if (p.rows().size() != 2) {
            return "limit not respected: got " + p.rows().size() + " rows";
        }
        return null;  // note what is absent: p.nextCursor() is never looked at
    }

    /** A zero limit is rejected. */
    public static String checkZeroLimitRejected(PageFn pageFn) {
        try {
            pageFn.apply(fixture(), null, 0);
            return "limit 0 was accepted";
        } catch (IllegalArgumentException expected) {
            return null;
        }
    }

    /**
     * The walk terminates, at two limits. Says nothing about what it yielded --
     * and THAT is the missing assertion `main` goes on to name. Both limits are
     * here because a `<` to `<=` mutation only fails to terminate at limit 1;
     * at limit 2 it re-serves the boundary row and then finishes.
     */
    public static String checkWalkTerminates(WalkFn walkFn) {
        for (int limit : new int[] {1, 2}) {
            try {
                walkFn.apply(fixture(), limit, 1000);
            } catch (RuntimeException e) {
                return "walk at limit " + limit + " failed: " + e.getMessage();
            }
        }
        return null;
    }

    /** The whole suite. null means every check passed. */
    public static String weakSuite(PageFn pageFn, WalkFn walkFn) {
        String r = checkLimitRespected(pageFn);
        if (r != null) return r;
        r = checkZeroLimitRejected(pageFn);
        if (r != null) return r;
        return checkWalkTerminates(walkFn);
    }

    // ------------------------------------------------------------------------
    // One mutant, applied by hand. NOT mutation testing, and no score comes out
    // of it: it is the single mutant a reader can hold in their head, so that
    // PIT's bulk output is recognisable when Maven is available. It lives in the
    // test sources because pom.xml points PIT's `targetClasses` at `craft.core.*`
    // only -- a mutated measuring instrument measures nothing.
    // ------------------------------------------------------------------------

    /**
     * {@link Pagination#page} with exactly one change: the next cursor is read
     * off the FIRST row of the page instead of the last. Diff it against
     * Pagination.java and you will find one token. PIT's DEFAULTS mutator group
     * contains changes of this size.
     */
    public static Page pageHandMutant(List<Row> rows, Long cursor, int limit) {
        if (limit < 1) {
            throw new IllegalArgumentException("limit must be >= 1");
        }
        List<Row> filtered = new ArrayList<>();
        for (Row r : rows) {
            if (cursor == null || r.createdAt() < cursor) {
                filtered.add(r);
            }
        }
        int take = Math.min(filtered.size(), limit);
        List<Row> out = List.copyOf(filtered.subList(0, take));
        Long nextCursor = out.size() == limit
                ? out.get(0).createdAt()   // <-- the mutation, and all of it
                : null;
        return new Page(out, nextCursor);
    }

    /** {@link Pagination#walkPages} over the mutant. Identical body; it has to call it. */
    public static List<Row> walkHandMutant(List<Row> rows, int limit, int maxPages) {
        List<Row> seen = new ArrayList<>();
        Long cursor = null;
        for (int i = 0; i < maxPages; i++) {
            Page p = pageHandMutant(rows, cursor, limit);
            seen.addAll(p.rows());
            if (p.nextCursor() == null) {
                return seen;
            }
            cursor = p.nextCursor();
        }
        throw new IllegalStateException("hand mutant did not terminate");
    }

    private static String render(List<Row> rows) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < rows.size(); i++) {
            if (i > 0) sb.append(' ');
            sb.append(rows.get(i).createdAt()).append('/').append(rows.get(i).id());
        }
        return sb.append(']').toString();
    }

    private static String verdict(String failure) {
        return failure == null ? "PASS" : "FAIL (" + failure + ")";
    }

    /** The offline demo. No JUnit, no Maven, no network: `javac` and `java` and nothing else. */
    public static void main(String[] args) {
        int problems = 0;

        String realFailure = weakSuite(Pagination::page, Pagination::walkPages);
        System.out.println();
        System.out.println("the weak suite");
        System.out.printf("  weak suite vs %-18s : %s%n", "Pagination.page", verdict(realFailure));
        if (realFailure != null) {
            System.out.println();
            System.out.println("the real page failed its own suite -- fix that before measuring.");
            System.exit(1);
        }

        String mutantFailure = weakSuite(WeakChecks::pageHandMutant, WeakChecks::walkHandMutant);
        System.out.println();
        System.out.println("one mutant, applied by hand: next cursor read off the FIRST row of");
        System.out.println("the page instead of the last (Pagination.java, one token).");
        System.out.println();
        System.out.printf("  weak suite vs %-18s : %s%s%n", "pageHandMutant", verdict(mutantFailure),
                mutantFailure == null ? "   <-- SURVIVED" : "");
        if (mutantFailure != null) {
            System.out.println();
            System.out.println("the hand mutant no longer survives: the suite above got stronger.");
            System.out.println("record that, it is the exercise.");
            problems++;
        }

        // The assertion the suite never made. Evaluated against the MUTANT only:
        // asserting it against Pagination.page would strengthen the measured
        // suite and change the very number this arm exists to read.
        List<Row> rows = fixture();
        List<Row> walkedReal = Pagination.walkPages(rows, 2, 1000);
        List<Row> walkedMutant = walkHandMutant(rows, 2, 1000);

        System.out.println();
        System.out.println("  the assertion this suite never made, at limit 2:");
        System.out.println("      \"walking the pages yields every input row exactly once\"");
        System.out.printf("      input                     %d rows %s%n", rows.size(), render(rows));
        System.out.printf("      page()       walk yields  %d rows %s%n",
                walkedReal.size(), render(walkedReal));
        System.out.printf("      mutant       walk yields  %d rows %s   <-- this assertion kills it%n",
                walkedMutant.size(), render(walkedMutant));
        System.out.println();

        if (walkedMutant.size() == rows.size()) {
            System.out.println("the hand mutant is no longer observable by that assertion -- "
                    + "the demonstration is broken, not the tool.");
            problems++;
        }
        System.exit(problems == 0 ? 0 : 1);
    }
}
