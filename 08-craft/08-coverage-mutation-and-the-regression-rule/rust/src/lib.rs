// Layer 8 Topic 8 - Rust: cargo-mutants, which mutates SOURCE and then lets the
// compiler throw out the nonsense before a single test runs.
//
// WHAT THIS DEMONSTRATES: the same `page()` the Python, Node, Java and C++ arms
// of this topic implement, line for line, so that five mutation tools chew on
// one algorithm and you can put their denominators side by side. Rust's place on
// this topic's axis is the *compile per mutant*: cargo-mutants edits the source,
// so every mutant costs a rebuild -- expensive -- but a mutant that does not
// typecheck is discarded as `unviable` before it ever runs your suite, which
// removes a whole category of noise that mutmut cannot remove.
//
// WHAT TO LOOK FOR, once `cargo mutants` is installed: the split of the run into
// caught / missed / unviable / timeout, printed at the end and written to
// `mutants.out/`. The `unviable` count is the number this arm exists to show --
// it is the noise Python's tooling has to leave in. Then read `mutants.out/
// missed.txt` and, for each survivor, name the missing *assertion* in
// `tests/weak_suite.rs`, not the missing test.
//
// One Rust-specific note worth a line in your table: `limit` is a `usize` here,
// so the "limit must be >= 1" guard cannot be handed a negative number and the
// whole family of negative-limit mutants does not exist. A narrower type is a
// smaller denominator. That is a real difference between this arm and the
// Python one, and it is not a difference in test quality.
//
// Run: ./run.sh   (or `cargo test` for the suite alone)

/// A row as the query returns it. `id` is unique; `created_at` is not -- which
/// is the whole of topic 5's bug and the reason this function is worth mutating.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Row {
    pub created_at: i64,
    pub id: i64,
}

/// The one error this module can produce. A named type rather than a `&str` so
/// the tests can match on it without matching on prose.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PageError {
    /// `limit` was zero. There is no sensible zero-row page.
    LimitTooSmall,
    /// `walk_pages` gave up. Only reachable from an implementation that
    /// re-serves the boundary row forever -- see topic 5's `page_inclusive`.
    DidNotTerminate { pages: usize, emitted: usize },
}

/// One page of `rows`, plus the cursor for the next page.
///
/// PRECONDITION: `rows` is sorted by `created_at` descending, because in
/// production it arrived from `ORDER BY created_at DESC`.
///
/// Mirrors: `WHERE created_at < :cursor ORDER BY created_at DESC LIMIT :limit`
pub fn page(
    rows: &[Row],
    cursor: Option<i64>,
    limit: usize,
) -> Result<(Vec<Row>, Option<i64>), PageError> {
    if limit < 1 {
        return Err(PageError::LimitTooSmall);
    }
    let filtered: Vec<Row> = match cursor {
        // Strict `<`, on a column that is not unique. This is the shipped bug.
        Some(c) => rows.iter().copied().filter(|r| r.created_at < c).collect(),
        None => rows.to_vec(),
    };
    let out: Vec<Row> = filtered.into_iter().take(limit).collect();
    let next_cursor = if out.len() == limit {
        Some(out[out.len() - 1].created_at)
    } else {
        None
    };
    Ok((out, next_cursor))
}

/// Walk every page until the implementation says there is no next cursor.
///
/// `max_pages` exists because an implementation can genuinely fail to
/// terminate; raising here turns "the test hangs" -- which a mutation run
/// records as a timeout rather than a kill -- into a readable error.
pub fn walk_pages(rows: &[Row], limit: usize, max_pages: usize) -> Result<Vec<Row>, PageError> {
    let mut seen: Vec<Row> = Vec::new();
    let mut cursor: Option<i64> = None;
    for _ in 0..max_pages {
        let (out, next) = page(rows, cursor, limit)?;
        seen.extend_from_slice(&out);
        match next {
            None => return Ok(seen),
            Some(c) => cursor = Some(c),
        }
    }
    Err(PageError::DidNotTerminate {
        pages: max_pages,
        emitted: seen.len(),
    })
}

/// Establish `page`'s precondition.
pub fn sorted_desc(rows: &[Row]) -> Vec<Row> {
    let mut v = rows.to_vec();
    v.sort_by_key(|r| std::cmp::Reverse(r.created_at));
    v
}
