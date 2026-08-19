// Layer 8 Topic 8 - Java: PIT, which mutates BYTECODE. No recompilation per
// mutant, no invalid-mutant problem, and incremental analysis on top.
//
// WHAT THIS DEMONSTRATES: the same `page()` as the Python, Node, Rust and C++
// arms of this topic, so five mutation tools chew on one algorithm and their
// denominators can be put side by side. Java's place on this topic's axis is the
// reason it is worth knowing even if you never write Java: PIT inserts mutants
// after javac has finished, so a mutant costs one classloader trick rather than
// a compile, and every mutant is by construction valid bytecode. That is why
// per-PR mutation testing is practical here and awkward everywhere else -- the
// expense is an implementation detail of the other toolchains, not a law about
// mutation testing.
//
// WHAT TO LOOK FOR, once Maven is installed: `target/pit-reports/index.html`,
// and specifically the line-by-line view. PIT tells you which mutant survived
// AND which tests covered the line it was on -- a covered line with a surviving
// mutant is precisely "a test ran this and checked nothing", which is this
// topic's whole thesis rendered as a table.
//
// Run: ./run.sh

package craft.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** Keyset pagination over a column that is not unique. */
public final class Pagination {

    private Pagination() {}

    /** A row as the query returns it. {@code id} is unique; {@code createdAt} is not. */
    public record Row(long createdAt, long id) {}

    /**
     * One page, plus the cursor for the next one.
     *
     * <p>{@code nextCursor} is a nullable {@link Long} rather than an
     * {@code OptionalLong} on purpose: the null is the end of the walk, and a
     * mutant that returns null early is one of the more instructive survivors
     * this module produces.
     */
    public record Page(List<Row> rows, Long nextCursor) {}

    /**
     * One page of {@code rows}, plus the cursor for the next page.
     *
     * <p>PRECONDITION: {@code rows} is sorted by {@code createdAt} descending,
     * because in production it arrived from {@code ORDER BY created_at DESC}.
     *
     * <p>Mirrors: {@code WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit}
     *
     * @throws IllegalArgumentException if {@code limit < 1}
     */
    public static Page page(List<Row> rows, Long cursor, int limit) {
        if (limit < 1) {
            throw new IllegalArgumentException("limit must be >= 1");
        }

        List<Row> filtered = new ArrayList<>();
        for (Row r : rows) {
            // Strict `<`, on a column that is not unique. This is the shipped
            // bug topic 5 found; here it is one of the things a mutant flips.
            if (cursor == null || r.createdAt() < cursor) {
                filtered.add(r);
            }
        }

        int take = Math.min(filtered.size(), limit);
        List<Row> out = List.copyOf(filtered.subList(0, take));
        Long nextCursor = out.size() == limit ? out.get(out.size() - 1).createdAt() : null;
        return new Page(out, nextCursor);
    }

    /**
     * Walk every page until the implementation says there is no next cursor.
     *
     * <p>{@code maxPages} exists because an implementation can genuinely fail to
     * terminate; throwing here turns "the test hangs" -- which a mutation run
     * records as a timeout rather than a kill -- into a readable failure.
     */
    public static List<Row> walkPages(List<Row> rows, int limit, int maxPages) {
        List<Row> seen = new ArrayList<>();
        Long cursor = null;
        for (int i = 0; i < maxPages; i++) {
            Page p = page(rows, cursor, limit);
            seen.addAll(p.rows());
            if (p.nextCursor() == null) {
                return seen;
            }
            cursor = p.nextCursor();
        }
        throw new IllegalStateException(
                "walkPages did not terminate within " + maxPages + " pages (" + seen.size()
                        + " rows emitted from " + rows.size() + ") -- this is the "
                        + "duplicate-forever failure, not a harness timeout");
    }

    /** Establish {@link #page}'s precondition. */
    public static List<Row> sortedDesc(List<Row> rows) {
        List<Row> copy = new ArrayList<>(rows);
        copy.sort(Comparator.comparingLong(Row::createdAt).reversed());
        return copy;
    }
}
