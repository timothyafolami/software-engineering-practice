// The deliberately weak suite, and mull's test binary. This file is the
// *subject* of the experiment, not scaffolding around it.
//
// WHAT THIS DEMONSTRATES: three checks that pass, execute every line of
// `page()`, and still leave a mutant alive -- because they check the *shape* of
// the answer (how many rows came back, that a zero limit is rejected, that the
// walk terminates) and never the *content* (which rows, and what cursor). A
// surviving mutant names a missing assertion, not a missing test.
//
// There is no test framework here on purpose. mull-runner decides "killed"
// purely from the process exit status: zero means the mutant survived, non-zero
// means something noticed. `main` returns the number of failed checks, so a
// plain binary is a complete mutation-testing harness -- and one fewer
// dependency between you and a result.
//
// WHAT TO LOOK FOR: the section after the suite applies ONE mutant by hand --
// the next cursor read off the first row of the page instead of the last -- and
// shows it passing the suite unchanged, then prints the single assertion that
// would have killed it. That is one mutant chosen by a human, not a score.
//
// KEEP THIS SUITE WEAK. Every check you add here changes the number mull
// prints, which is the measurement you are trying to take.

#include <algorithm>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <optional>
#include <string>
#include <vector>

#include "pagination.hpp"

using craft::Page;
using craft::Row;

using PageFn = Page (*)(const std::vector<Row>&, std::optional<std::int64_t>, int);
using WalkFn = std::vector<Row> (*)(const std::vector<Row>&, int, int);

namespace {

std::vector<Row> fixture() {
    // Three distinct timestamps: topic 5's tie bug is NOT what this file is
    // about, and a tie here would make the hand mutant look like that bug.
    return craft::sorted_desc({Row{30, 3}, Row{20, 2}, Row{10, 1}});
}

std::string render(const std::vector<Row>& rows) {
    std::string s = "[";
    for (std::size_t i = 0; i < rows.size(); ++i) {
        if (i != 0) s += " ";
        s += std::to_string(rows[i].created_at) + "/" + std::to_string(rows[i].id);
    }
    return s + "]";
}

/// The whole of what this suite checks. Empty string means every check passed.
std::string weak_suite(PageFn page_fn, WalkFn walk_fn) {
    const std::vector<Row> rows = fixture();

    // 1. A page is no longer than the limit. Says nothing about WHICH rows.
    try {
        const Page p = page_fn(rows, std::nullopt, 2);
        if (p.rows.size() != 2) {
            return "limit not respected: got " + std::to_string(p.rows.size()) + " rows";
        }
        // NOTE what is not here: p.next_cursor is never looked at.
    } catch (const std::exception& e) {
        return std::string("page(limit=2) threw: ") + e.what();
    }

    // 2. A zero limit is rejected.
    try {
        page_fn(rows, std::nullopt, 0);
        return "limit 0 was accepted";
    } catch (const std::invalid_argument&) {
        // expected
    }

    // 3. The walk terminates, at two limits. Says nothing about what it
    //    yielded -- and THAT is the missing assertion named below. Both limits
    //    are here because a `<` -> `<=` mutation only fails to terminate at
    //    limit 1; at limit 2 it re-serves the boundary row and then finishes.
    for (int limit : {1, 2}) {
        try {
            walk_fn(rows, limit, 1000);
        } catch (const std::exception& e) {
            return "walk at limit " + std::to_string(limit) + " failed: " + e.what();
        }
    }

    return "";
}

// -------------------------------------------------------------------------
// One mutant, applied by hand. NOT mutation testing, and no score comes out of
// it: it is the single mutant a reader can hold in their head, so mull's bulk
// output is recognisable when the tool is installed. It lives in this file
// because `mull.yml` excludes this file from mutation -- a mutated measuring
// instrument measures nothing.
// -------------------------------------------------------------------------

/// `craft::page`, with exactly one change: the next cursor is read off the
/// FIRST row of the page instead of the last. Diff it against pagination.cpp
/// and you will find one token. mull's operator set contains changes this size.
Page page_hand_mutant(const std::vector<Row>& rows, std::optional<std::int64_t> cursor,
                      int limit) {
    if (limit < 1) {
        throw std::invalid_argument("limit must be >= 1");
    }
    std::vector<Row> filtered;
    filtered.reserve(rows.size());
    for (const Row& r : rows) {
        if (!cursor.has_value() || r.created_at < *cursor) {
            filtered.push_back(r);
        }
    }
    const std::size_t take = std::min(filtered.size(), static_cast<std::size_t>(limit));
    Page out;
    out.rows.assign(filtered.begin(), filtered.begin() + static_cast<std::ptrdiff_t>(take));
    if (out.rows.size() == static_cast<std::size_t>(limit)) {
        out.next_cursor = out.rows[0].created_at;  // <-- the mutation, and all of it
    } else {
        out.next_cursor = std::nullopt;
    }
    return out;
}

/// `craft::walk_pages` over the mutant. Identical body; it has to call it.
std::vector<Row> walk_hand_mutant(const std::vector<Row>& rows, int limit, int max_pages) {
    std::vector<Row> seen;
    std::optional<std::int64_t> cursor = std::nullopt;
    for (int i = 0; i < max_pages; ++i) {
        Page p = page_hand_mutant(rows, cursor, limit);
        seen.insert(seen.end(), p.rows.begin(), p.rows.end());
        if (!p.next_cursor.has_value()) {
            return seen;
        }
        cursor = p.next_cursor;
    }
    throw std::runtime_error("hand mutant did not terminate");
}

int report(const std::string& label, const std::string& failure) {
    std::cout << "  weak suite vs " << std::left << std::setw(18) << label << " : "
              << (failure.empty() ? std::string("PASS") : "FAIL (" + failure + ")");
    return failure.empty() ? 0 : 1;
}

}  // namespace

int main() {
    int failures = 0;

    std::cout << "\nthe weak suite\n";
    const std::string real_failure = weak_suite(&craft::page, &craft::walk_pages);
    failures += report("craft::page", real_failure);
    std::cout << "\n";
    if (!real_failure.empty()) {
        std::cout << "\nthe real page failed its own suite -- fix that before measuring.\n";
        return failures;  // non-zero: mull would read this as a kill
    }

    std::cout << "\none mutant, applied by hand: next cursor read off the FIRST row of\n"
              << "the page instead of the last (pagination.cpp, one token).\n\n";
    const std::string mutant_failure = weak_suite(&page_hand_mutant, &walk_hand_mutant);
    report("page_hand_mutant", mutant_failure);
    std::cout << (mutant_failure.empty() ? "   <-- SURVIVED" : "") << "\n";
    if (!mutant_failure.empty()) {
        // Not an error in the mutant -- an error in the demonstration.
        std::cout << "\nthe hand mutant no longer survives: the suite above got stronger.\n"
                  << "record that, it is the exercise.\n";
        ++failures;
    }

    // The assertion the suite never made. Evaluated against the MUTANT only:
    // asserting it against craft::page would strengthen the measured suite and
    // change the very number this arm exists to read.
    const std::vector<Row> rows = fixture();
    const std::vector<Row> walked_real = craft::walk_pages(rows, 2, 1000);
    const std::vector<Row> walked_mutant = walk_hand_mutant(rows, 2, 1000);

    std::cout << "\n  the assertion this suite never made, at limit 2:\n"
              << "      \"walking the pages yields every input row exactly once\"\n"
              << "      input                     " << rows.size() << " rows " << render(rows)
              << "\n"
              << "      page()       walk yields  " << walked_real.size() << " rows "
              << render(walked_real) << "\n"
              << "      mutant       walk yields  " << walked_mutant.size() << " rows "
              << render(walked_mutant) << "   <-- this assertion kills it\n\n";

    if (walked_mutant.size() == rows.size()) {
        std::cout << "the hand mutant is no longer observable by that assertion -- "
                     "the demonstration is broken, not the tool.\n";
        ++failures;
    }

    return failures;
}
