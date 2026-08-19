// The deliberately weak suite. This file is the *subject* of the experiment,
// not scaffolding around it.
//
// WHAT THIS DEMONSTRATES: three tests that pass, cover every line of `page()`,
// and still leave a mutant alive -- because they check the *shape* of the answer
// (how many rows came back, that a zero limit is rejected, that the walk
// terminates) and never once check the *content* (which rows, and what cursor).
// That is the topic's one sentence made runnable: a surviving mutant names a
// missing assertion, not a missing test. The test that should have killed it is
// already here and already calls the right function.
//
// WHAT TO LOOK FOR when you run `./run.sh`: the third test prints one mutant
// applied by hand -- the next cursor read off the FIRST row of the page instead
// of the last -- passing this suite unchanged, and then prints the one assertion
// that would have killed it. Nothing in that demonstration is a mutation-testing
// *score*; it is one mutant, chosen by a human, to show the shape of the thing
// `cargo mutants` reports in bulk.
//
// KEEP THIS SUITE WEAK. Every assertion you add here changes the number
// cargo-mutants prints, which is the measurement you are trying to take. When
// you get to step 3 of the experiment and start adding the missing assertions,
// copy this file first and record the before/after as two rows of your table.

use page_mutants::{page, sorted_desc, walk_pages, PageError, Row};

type PageFn = fn(&[Row], Option<i64>, usize) -> Result<(Vec<Row>, Option<i64>), PageError>;
type WalkFn = fn(&[Row], usize, usize) -> Result<Vec<Row>, PageError>;

struct Impl {
    name: &'static str,
    page: PageFn,
    walk: WalkFn,
}

fn fixture() -> Vec<Row> {
    // Three distinct timestamps: topic 5's tie bug is NOT what this file is
    // about, and a tie here would make the mutant below look like that bug.
    sorted_desc(&[
        Row { created_at: 30, id: 3 },
        Row { created_at: 20, id: 2 },
        Row { created_at: 10, id: 1 },
    ])
}

/// The whole of what this suite checks. Returns the name of the first failing
/// check so it can be reported for an implementation that is not `page`.
fn weak_suite(imp: &Impl) -> Result<(), String> {
    let rows = fixture();

    // 1. A page is no longer than the limit. Says nothing about WHICH rows.
    let (out, _next_cursor) = (imp.page)(&rows, None, 2).map_err(|e| format!("{e:?}"))?;
    if out.len() != 2 {
        return Err(format!("limit not respected: got {} rows", out.len()));
    }

    // 2. A zero limit is rejected.
    if (imp.page)(&rows, None, 0).is_ok() {
        return Err("limit 0 was accepted".to_string());
    }

    // 3. The walk terminates, at two limits. Says nothing about what it
    //    yielded -- and THAT is the missing assertion the third test below goes
    //    on to name. Both limits are here because a `<` -> `<=` mutation only
    //    fails to terminate at limit 1: at limit 2 it re-serves the boundary
    //    row and then finishes, so a suite that walked only at limit 2 would
    //    let topic 5's own bug-fix-shaped mutant through as well.
    for limit in [1usize, 2usize] {
        (imp.walk)(&rows, limit, 1000)
            .map_err(|e| format!("walk at limit {limit} failed: {e:?}"))?;
    }

    Ok(())
}

#[test]
fn weak_suite_passes_on_the_real_page() {
    let real = Impl { name: "page", page, walk: walk_pages };
    assert_eq!(weak_suite(&real), Ok(()));
}

#[test]
fn a_page_is_never_longer_than_the_limit() {
    let rows = fixture();
    for limit in 1..=5usize {
        let (out, _) = page(&rows, None, limit).expect("limit >= 1");
        assert!(out.len() <= limit);
    }
}

// ---------------------------------------------------------------------------
// One mutant, applied by hand. This is NOT mutation testing and does not
// produce a score; it is the single mutant a reader can hold in their head,
// so that `cargo mutants`' output is recognisable when the tool is installed.
//
// It lives in the test crate on purpose: `mutants.toml` excludes `tests/**`,
// so this copy is never itself mutated and never changes the denominator.
// ---------------------------------------------------------------------------

/// `page`, with exactly one change: the next cursor is read off the FIRST row
/// of the page instead of the last. Diff against `src/lib.rs` and you will find
/// one token. cargo-mutants' operator set contains changes of this size.
fn page_hand_mutant(
    rows: &[Row],
    cursor: Option<i64>,
    limit: usize,
) -> Result<(Vec<Row>, Option<i64>), PageError> {
    if limit < 1 {
        return Err(PageError::LimitTooSmall);
    }
    let filtered: Vec<Row> = match cursor {
        Some(c) => rows.iter().copied().filter(|r| r.created_at < c).collect(),
        None => rows.to_vec(),
    };
    let out: Vec<Row> = filtered.into_iter().take(limit).collect();
    let next_cursor = if out.len() == limit {
        Some(out[0].created_at) //          <-- the mutation, and the whole of it
    } else {
        None
    };
    Ok((out, next_cursor))
}

/// `walk_pages` over the mutant. Identical body; it has to call the mutant.
fn walk_hand_mutant(rows: &[Row], limit: usize, max_pages: usize) -> Result<Vec<Row>, PageError> {
    let mut seen: Vec<Row> = Vec::new();
    let mut cursor: Option<i64> = None;
    for _ in 0..max_pages {
        let (out, next) = page_hand_mutant(rows, cursor, limit)?;
        seen.extend_from_slice(&out);
        match next {
            None => return Ok(seen),
            Some(c) => cursor = Some(c),
        }
    }
    Err(PageError::DidNotTerminate { pages: max_pages, emitted: seen.len() })
}

fn render(rows: &[Row]) -> String {
    let parts: Vec<String> = rows.iter().map(|r| format!("{}/{}", r.created_at, r.id)).collect();
    format!("[{}]", parts.join(" "))
}

#[test]
fn one_hand_applied_mutant_survives_this_suite() {
    let real = Impl { name: "page", page, walk: walk_pages };
    let mutant = Impl { name: "page_hand_mutant", page: page_hand_mutant, walk: walk_hand_mutant };
    let rows = fixture();

    println!();
    println!("one mutant, applied by hand: next cursor read off the FIRST row of");
    println!("the page instead of the last (src/lib.rs, one token).");
    println!();
    println!("  weak suite vs {:<18} : {:?}", real.name, weak_suite(&real));
    println!("  weak suite vs {:<18} : {:?}   <-- SURVIVED", mutant.name, weak_suite(&mutant));
    println!();

    assert_eq!(weak_suite(&real), Ok(()), "the real page must pass its own suite");
    assert_eq!(
        weak_suite(&mutant),
        Ok(()),
        "if this ever fails, the suite got stronger -- record that, it is the exercise"
    );

    // The assertion the suite never made. Run against the MUTANT only: asserting
    // it against `page` would strengthen the measured suite and change the very
    // number the experiment is trying to read.
    let walked_real = walk_pages(&rows, 2, 1000).expect("terminates");
    let walked_mutant = walk_hand_mutant(&rows, 2, 1000).expect("terminates");

    println!("  the assertion this suite never made, at limit 2:");
    println!("      \"walking the pages yields every input row exactly once\"");
    println!("      input                      {} rows {}", rows.len(), render(&rows));
    println!("      page()        walk yields  {} rows {}", walked_real.len(), render(&walked_real));
    println!(
        "      mutant        walk yields  {} rows {}   <-- this assertion kills it",
        walked_mutant.len(),
        render(&walked_mutant)
    );
    println!();

    assert_ne!(
        walked_mutant.len(),
        rows.len(),
        "the hand mutant is supposed to be observable by the missing assertion"
    );
}
