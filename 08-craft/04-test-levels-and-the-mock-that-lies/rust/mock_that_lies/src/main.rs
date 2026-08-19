// Layer 8 Topic 4 - Rust: the mock you cannot retrofit, and the one thing it
// still cannot check.
//
// WHAT THIS DEMONSTRATES: Rust has no monkey-patching. `PgRows` below is a
// concrete struct, and there is no `patch("...PgRows.query")` in this language,
// at any privilege level. To substitute it you must have written `trait Rows`
// FIRST -- so the seam is a design decision visible in the production signature,
// not something a test retrofits. That is the good news, and it is real.
//
// The bad news is the topic's point and it survives all of that: once the seam
// exists, a scripted double is exactly as blind as Python's `AsyncMock`. SUITE A
// below is three green assertions against a function with two bugs, and the
// double supplies the ordering the missing clause removed.
//
// WHAT TO LOOK FOR: SUITE A is 3/3 PASS. SUITE B runs the SAME three assertions
// against a fake that models a table -- storage order, WHERE then ORDER BY then
// LIMIT -- and is 1/3. Nothing about the code under test changed between them.
//
//   cd rust/mock_that_lies && cargo run
//
// `cargo test` also passes, and passing is the finding: the #[test] block at the
// bottom is the scripted suite, written the way it would be written for real.

use std::cell::RefCell;

// --- the domain ------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
struct Order {
    id: i64,
    customer_id: i64,
    status: &'static str,
    created_at: i64, // minutes since an arbitrary epoch; ties are possible
}

/// The query, as a value rather than a string, so a double has no excuse for
/// not reading it. It reads it anyway only if someone wrote the code to.
#[derive(Clone, Debug, PartialEq)]
struct Query {
    customer_id: i64,
    exclude_cancelled: bool,
    order_by_created_desc: bool,
    limit: usize,
}

/// THE SEAM. This trait exists for one reason: without it, nothing in this file
/// could be tested at all without a database. In Python the equivalent trait is
/// implicit, undeclared and free, which is why nobody notices they have taken on
/// a dependency. Here you had to type it, and it is public API.
trait Rows {
    fn query(&self, q: &Query) -> Vec<Order>;
}

// --- the unit under test, with the same two planted bugs as the Python arm ---

/// BUG 1: `order_by_created_desc: false` -- no ordering is requested at all.
/// BUG 2: `exclude_cancelled: false`, and the filter runs below, in Rust, after
/// the store has already applied the limit.
fn recent_orders<R: Rows>(rows: &R, customer_id: i64, limit: usize) -> Vec<Order> {
    let q = Query {
        customer_id,
        exclude_cancelled: false,
        order_by_created_desc: false,
        limit,
    };
    rows.query(&q)
        .into_iter()
        .filter(|o| o.status != "cancelled")
        .collect()
}

/// Filter, order and limit in one place, in that order -- and a TOTAL order,
/// because `created_at` alone is not one when timestamps tie.
fn recent_orders_fixed<R: Rows>(rows: &R, customer_id: i64, limit: usize) -> Vec<Order> {
    rows.query(&Query {
        customer_id,
        exclude_cancelled: true,
        order_by_created_desc: true,
        limit,
    })
}

// --- double 1: what mockall generates --------------------------------------

/// `#[automock]` on `trait Rows` emits a struct with per-method expectations and
/// a `returning` closure. This is that struct, written out. Two properties
/// matter and both are visible here:
///
///   * it is STRICTER than `AsyncMock`: an unexpected call panics rather than
///     inventing an attribute, and mockall makes you say what you expect;
///   * and that strictness buys you nothing on this bug, because the expectation
///     you wrote is `returning(|_| the_rows_i_meant)` -- the `_` is the query.
struct MockRows {
    returns: Vec<Order>,
    seen: RefCell<Vec<Query>>,
}

impl MockRows {
    fn expect_query_returning(rows: Vec<Order>) -> Self {
        MockRows { returns: rows, seen: RefCell::new(Vec::new()) }
    }
}

impl Rows for MockRows {
    fn query(&self, q: &Query) -> Vec<Order> {
        self.seen.borrow_mut().push(q.clone()); // recorded, then ignored
        self.returns.clone()
    }
}

// --- double 2: a fake that models a table ----------------------------------

/// Rows in STORAGE order, and the three clauses applied in the order a database
/// applies them. Every line here is a semantics you had to know before you could
/// write it, which is the argument for writing it.
struct HeapRows {
    stored: Vec<Order>,
}

impl Rows for HeapRows {
    fn query(&self, q: &Query) -> Vec<Order> {
        let mut out: Vec<Order> = self
            .stored
            .iter()
            .filter(|o| o.customer_id == q.customer_id)
            .cloned()
            .collect(); // storage order: not insertion order, and not sorted

        if q.exclude_cancelled {
            out.retain(|o| o.status != "cancelled"); // WHERE, before LIMIT
        }
        if q.order_by_created_desc {
            out.sort_by(|a, b| b.created_at.cmp(&a.created_at).then(b.id.cmp(&a.id)));
        }
        out.truncate(q.limit); // LIMIT last, counting rows the STORE kept
        out
    }
}

/// The production implementation. There is no seam into this one: no attribute
/// on it can be replaced at runtime, and no test in any Rust crate can reach
/// inside it. It is here to be looked at, not run.
#[allow(dead_code)]
struct PgRows {
    dsn: String,
}

#[allow(dead_code)]
impl PgRows {
    fn query(&self, _q: &Query) -> Vec<Order> {
        unimplemented!("needs a database -- and needs `impl Rows for PgRows` to be testable at all")
    }
}

// --- fixtures ---------------------------------------------------------------

fn o(id: i64, minutes: i64, status: &'static str) -> Order {
    Order { id, customer_id: 1, status, created_at: minutes }
}

/// What a person writes when asked for "the recent orders": the answer, already
/// ordered, already filtered. Plausible, and it is the bug's alibi.
fn newest_first() -> Vec<Order> {
    vec![o(5, 50, "paid"), o(4, 40, "paid"), o(3, 30, "paid"), o(2, 20, "paid")]
}

/// The same customer's rows as a heap holds them: inserted shuffled, and 5 and 4
/// were UPDATEd afterwards, which on a real heap moves them to the end.
fn storage_order() -> Vec<Order> {
    vec![
        o(2, 20, "paid"),
        o(6, 60, "cancelled"),
        o(3, 30, "paid"),
        o(1, 10, "cancelled"),
        o(5, 50, "paid"),
        o(4, 40, "paid"),
        Order { id: 9, customer_id: 2, status: "paid", created_at: 90 },
    ]
}

fn ids(orders: &[Order]) -> Vec<i64> {
    orders.iter().map(|o| o.id).collect()
}

// --- the two suites, run as one program so the contrast is one output -------

struct Suite {
    passed: usize,
    failed: usize,
}

impl Suite {
    fn new(name: &'static str) -> Self {
        println!("\n{name}");
        Suite { passed: 0, failed: 0 }
    }
    fn check(&mut self, what: &str, ok: bool, detail: String) {
        if ok {
            self.passed += 1;
            println!("  [PASS] {what:<26} {detail}");
        } else {
            self.failed += 1;
            println!("  [FAIL] {what:<26} {detail}");
        }
    }
    fn report(&self) {
        println!("  ----> {}/{} pass", self.passed, self.passed + self.failed);
    }
}

/// The three assertions, verbatim, so that nothing differs between the suites
/// except which double they run against.
fn run_suite<R: Rows>(name: &'static str, rows: &R) -> Suite {
    let mut s = Suite::new(name);
    const LIMIT: usize = 4;

    let got = recent_orders(rows, 1, LIMIT);
    s.check(
        "returns newest first",
        ids(&got) == vec![5, 4, 3, 2],
        format!("got {:?}, want [5, 4, 3, 2]", ids(&got)),
    );
    s.check(
        "excludes cancelled",
        got.iter().all(|o| o.status != "cancelled"),
        format!("{} rows, none cancelled", got.len()),
    );
    s.check(
        "respects the limit",
        got.len() == LIMIT,
        format!("asked for {LIMIT}, got {}", got.len()),
    );
    s.report();
    s
}

fn main() {
    println!("Layer 8 topic 4 - Rust: the seam is mandatory, the blindness is not cured.");
    println!("The code under test is IDENTICAL in both suites. Only the double changes.");

    let mock = MockRows::expect_query_returning(newest_first());
    let a = run_suite("SUITE A - scripted double (what mockall's #[automock] emits)", &mock);

    let heap = HeapRows { stored: storage_order() };
    let b = run_suite("SUITE B - hand-written fake that models a table", &heap);

    println!("\nWHERE THE ORDERING CAME FROM");
    let seen = mock.seen.borrow();
    println!("  the double was handed this query: {:?}", seen[0]);
    println!("  order_by_created_desc = {} -- no ordering was ever requested,",
        seen[0].order_by_created_desc);
    println!("  and the double returned rows sorted newest-first regardless, because");
    println!("  the fixture `newest_first()` was written by someone who knew the answer.");

    println!("\nEVIDENCE THE SYSTEM IS BROKEN");
    let heap_broken = recent_orders(&heap, 1, 4);
    let heap_fixed = recent_orders_fixed(&heap, 1, 4);
    println!("  recent_orders(limit=4)       -> {:?}  ({} rows)", ids(&heap_broken), heap_broken.len());
    println!("  recent_orders_fixed(limit=4) -> {:?}  ({} rows)", ids(&heap_fixed), heap_fixed.len());
    println!("  bug 1: storage order, not newest-first.");
    println!("  bug 2: the store spent 2 of the 4 limit slots on cancelled rows");
    println!("         before Rust ever saw them, so the caller is short.");

    println!("\nSUMMARY  suite A {}/{}   suite B {}/{}",
        a.passed, a.passed + a.failed, b.passed, b.passed + b.failed);
    println!("The Rust-specific part is upstream of all of this: `trait Rows` had to");
    println!("exist before either suite could be written. `PgRows` has no seam and no");
    println!("test can reach into it -- in Rust, \"hard to test\" is a compile-time");
    println!("statement about coupling, not a complaint about tooling.");
}

// --- and the same suite as real #[test]s, because `cargo test` passing is the
//     finding, not a formality ------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn mocked() -> MockRows {
        MockRows::expect_query_returning(newest_first())
    }

    #[test]
    fn returns_newest_first() {
        // Green. `recent_orders` requests no ordering at all.
        assert_eq!(ids(&recent_orders(&mocked(), 1, 4)), vec![5, 4, 3, 2]);
    }

    #[test]
    fn excludes_cancelled() {
        // Green. The filter runs -- after the limit, which the double cannot show.
        assert!(recent_orders(&mocked(), 1, 4).iter().all(|o| o.status != "cancelled"));
    }

    #[test]
    fn respects_the_limit() {
        // Green, and the most misleading of the three: the fixture holds exactly
        // four rows because its author applied the limit by hand.
        assert!(recent_orders(&mocked(), 1, 4).len() <= 4);
    }

    #[test]
    fn the_hand_written_fake_disagrees_with_all_of_that() {
        let heap = HeapRows { stored: storage_order() };
        let broken = recent_orders(&heap, 1, 4);
        assert_ne!(ids(&broken), vec![5, 4, 3, 2], "bug 1 should be visible here");
        assert!(broken.len() < 4, "bug 2 should be visible here");
        assert_eq!(ids(&recent_orders_fixed(&heap, 1, 4)), vec![5, 4, 3, 2]);
    }
}
