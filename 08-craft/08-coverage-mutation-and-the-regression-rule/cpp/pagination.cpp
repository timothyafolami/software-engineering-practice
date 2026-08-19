// The mutation target. Everything mull is pointed at lives in this one
// translation unit; `mull.yml` excludes weak_test.cpp so the measuring
// instrument is never itself mutated.
//
// WHAT TO LOOK FOR: how few lines it takes. Four comparisons, one subtraction,
// one bound. Mutation testing is expensive per line, which is why this topic
// says to run it on your two or three most important pure modules rather than
// on everything -- and a pure module the size of this file is exactly the shape
// that pays for itself.

#include "pagination.hpp"

#include <algorithm>
#include <stdexcept>
#include <string>

namespace craft {

Page page(const std::vector<Row>& rows, std::optional<std::int64_t> cursor, int limit) {
    if (limit < 1) {
        throw std::invalid_argument("limit must be >= 1");
    }

    std::vector<Row> filtered;
    filtered.reserve(rows.size());
    for (const Row& r : rows) {
        // Strict `<`, on a column that is not unique. This is the shipped bug
        // topic 5 found; here it is one of the things a mutant can flip.
        if (!cursor.has_value() || r.created_at < *cursor) {
            filtered.push_back(r);
        }
    }

    const std::size_t take = std::min(filtered.size(), static_cast<std::size_t>(limit));
    Page out;
    out.rows.assign(filtered.begin(), filtered.begin() + static_cast<std::ptrdiff_t>(take));
    if (out.rows.size() == static_cast<std::size_t>(limit)) {
        out.next_cursor = out.rows[out.rows.size() - 1].created_at;
    } else {
        out.next_cursor = std::nullopt;
    }
    return out;
}

std::vector<Row> walk_pages(const std::vector<Row>& rows, int limit, int max_pages) {
    std::vector<Row> seen;
    std::optional<std::int64_t> cursor = std::nullopt;
    for (int i = 0; i < max_pages; ++i) {
        Page p = page(rows, cursor, limit);
        seen.insert(seen.end(), p.rows.begin(), p.rows.end());
        if (!p.next_cursor.has_value()) {
            return seen;
        }
        cursor = p.next_cursor;
    }
    throw std::runtime_error("walk_pages did not terminate within " + std::to_string(max_pages) +
                             " pages (" + std::to_string(seen.size()) + " rows emitted from " +
                             std::to_string(rows.size()) + ") -- this is the duplicate-forever "
                             "failure, not a harness timeout");
}

std::vector<Row> sorted_desc(std::vector<Row> rows) {
    std::stable_sort(rows.begin(), rows.end(),
                     [](const Row& a, const Row& b) { return a.created_at > b.created_at; });
    return rows;
}

}  // namespace craft
