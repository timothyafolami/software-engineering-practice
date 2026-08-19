// Layer 8 Topic 8 - C++: mull, which mutates LLVM IR and patches mutants in at
// RUNTIME, so one compilation serves many mutants.
//
// WHAT THIS DEMONSTRATES: the same `page()` as the Python, Node, Java and Rust
// arms, so five mutation tools chew on one algorithm and their denominators can
// be put side by side. C++'s place on this topic's axis is the *late* insertion
// point: mutmut edits source (recompile-free but interpreted, so a full suite
// run per mutant), cargo-mutants edits source (compile per mutant), PIT edits
// bytecode, and mull edits IR at the point where the compiler has already
// finished. That is why mull is the closest thing to PIT's speed outside the
// JVM.
//
// WHAT TO LOOK FOR: the C++-specific hazard named in this topic's README. A
// mutant can turn a well-defined program into an undefined-behaviour one --
// `out[out.size() - 1]` with `size() == 0`, for instance, which is exactly one
// operator away from the code below. When that happens the test binary crashes,
// and a crash is neither a kill nor a survival: mull will record a non-zero exit
// as "killed" because it cannot tell a failed assertion from a segfault. Decide
// what you record BEFORE you run it, and write the decision in your table.
//
// Build and run: ./run.sh

#pragma once

#include <cstdint>
#include <optional>
#include <vector>

namespace craft {

/// A row as the query returns it. `id` is unique; `created_at` is not.
struct Row {
    std::int64_t created_at;
    std::int64_t id;
};

/// One page, plus the cursor for the next one. An absent cursor ends the walk.
struct Page {
    std::vector<Row> rows;
    std::optional<std::int64_t> next_cursor;
};

/// One page of `rows`, plus the cursor for the next page.
///
/// PRECONDITION: `rows` is sorted by `created_at` descending, because in
/// production it arrived from `ORDER BY created_at DESC`.
///
/// Mirrors: WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit
/// Throws std::invalid_argument when `limit < 1`.
Page page(const std::vector<Row>& rows, std::optional<std::int64_t> cursor, int limit);

/// Walk every page until the implementation says there is no next cursor.
/// Throws std::runtime_error after `max_pages`, so a non-terminating mutant
/// becomes a readable failure rather than a mutation-run timeout.
std::vector<Row> walk_pages(const std::vector<Row>& rows, int limit, int max_pages = 1000);

/// Establish `page`'s precondition.
std::vector<Row> sorted_desc(std::vector<Row> rows);

}  // namespace craft
