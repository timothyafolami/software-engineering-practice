//! Layer 8 Topic 3 - Rust: the only language here where the SIGNATURE states
//! which errors exist and the compiler refuses to let you ignore one.
//!
//! WHAT THIS DEMONSTRATES: `Result<T, E>` is a value; `?` propagates with a
//! `From` conversion; `#[non_exhaustive]` lets you add variants without breaking
//! callers' matches. Category 3 has its own mechanism -- `panic!`, `unwrap`,
//! `expect` -- and the split between `Result` and `panic` IS this topic's
//! distinction, enforced by a type rather than by review.
//!
//! And the failure mode is real too: collapsing everything into one opaque
//! boxed error puts you back in Python's situation. Fine at the application
//! edge, wrong in a library, and the two halves below show why.
//!
//! WHAT TO LOOK FOR: the COLLAPSED half prints an error the caller can only
//! log. The TYPED half prints one the caller can branch on, with the retry
//! budget carried as a number rather than as words inside a string.
//!
//!     cd rust/taxonomy && cargo run

use std::error::Error;
use std::fmt;

// --- the taxonomy, as a type ------------------------------------------------

/// Every error this module can produce, enumerated. A caller that matches on
/// this gets a compiler warning when a new variant arrives -- unless we say
/// otherwise with `#[non_exhaustive]`, which is a deliberate promise that we
/// WILL add variants and callers must have a fallback.
#[derive(Debug)]
#[non_exhaustive]
enum OrderError {
    /// Category 1: the caller does something specific -- 404.
    NotFound { id: u64 },
    /// Category 1: the caller shows a message.
    InsufficientFunds { short_by_cents: i64 },
    /// Category 2: the same call, unchanged, might succeed later.
    Unavailable { dep: &'static str, retry_after_s: u64, source: DbError },
}

impl fmt::Display for OrderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OrderError::NotFound { id } => write!(f, "order {id} not found"),
            OrderError::InsufficientFunds { short_by_cents } => {
                write!(f, "insufficient funds, short by {short_by_cents} cents")
            }
            OrderError::Unavailable { dep, retry_after_s, .. } => {
                write!(f, "{dep} unavailable, retry after {retry_after_s}s")
            }
        }
    }
}

impl Error for OrderError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            OrderError::Unavailable { source, .. } => Some(source),
            _ => None,
        }
    }
}

#[derive(Debug)]
struct DbError(&'static str);

impl fmt::Display for DbError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl Error for DbError {}

/// The `From` impl is what makes `?` a translation rather than a leak: the
/// driver's error type never appears in this module's public signature.
impl From<DbError> for OrderError {
    fn from(e: DbError) -> Self {
        OrderError::Unavailable { dep: "postgres", retry_after_s: 2, source: e }
    }
}

fn query_db() -> Result<u64, DbError> {
    Err(DbError("dial tcp 10.0.0.7:5432: connect: connection refused"))
}

/// TYPED: the signature says exactly which errors this can produce.
fn load_order_typed(id: u64) -> Result<u64, OrderError> {
    if id == 0 {
        return Err(OrderError::NotFound { id });
    }
    if id == 1 {
        return Err(OrderError::InsufficientFunds { short_by_cents: 250 });
    }
    let total = query_db()?; // `?` converts DbError -> OrderError via From
    Ok(total)
}

/// COLLAPSED: `Box<dyn Error>` everywhere. Compiles, propagates, and destroys
/// the taxonomy -- the caller is back to string matching, which is exactly
/// where Python starts.
fn load_order_collapsed(id: u64) -> Result<u64, Box<dyn Error>> {
    if id == 0 {
        return Err(format!("order {id} not found").into());
    }
    let total = query_db()?;
    Ok(total)
}

fn main() {
    println!("=== COLLAPSED: Box<dyn Error>, the taxonomy erased ===");
    match load_order_collapsed(7) {
        Ok(t) => println!("  ok {t}"),
        Err(e) => {
            println!("  {e}");
            println!("  -> is this a 404, a 503 or a bug? The caller cannot tell without");
            println!("     matching on the message text, and the message is not a contract.");
        }
    }

    println!("\n=== TYPED: the compiler makes the caller decide ===");
    for id in [0u64, 1, 7] {
        report(id);
    }

    println!("\n=== category 3: panic is a DIFFERENT mechanism, on purpose ===");
    println!("  (the panic message below is SUPPOSED to be loud -- that is the point)");
    let outcome = std::panic::catch_unwind(|| {
        let v: Vec<u8> = Vec::new();
        v[3] // a bug. Not a Result, because there is nothing for a caller to do.
    });
    println!("  index out of bounds -> {}", if outcome.is_err() { "panicked" } else { "returned" });
    println!("  -> `Result` and `panic!` are the two categories, separated by the");
    println!("     language rather than by a code review. `unwrap()` in library code is");
    println!("     an assertion that this cannot fail; when it can, it is a category");
    println!("     error, not a style one.");
}

/// One caller, every variant. Note there is NO `_` arm: inside the defining
/// crate `#[non_exhaustive]` still gives exhaustiveness checking, so adding a
/// variant breaks this function at COMPILE time. Outside the crate a `_` arm is
/// required instead, which is the promise `#[non_exhaustive]` makes: we reserve
/// the right to add variants, and your fallback is how you survive that.
fn report(id: u64) {
    match load_order_typed(id) {
        Ok(t) => println!("  id={id} -> 200, total {t}"),
        Err(OrderError::NotFound { id }) => println!("  id={id} -> 404 for order {id}"),
        Err(OrderError::InsufficientFunds { short_by_cents }) => {
            println!("  id={id} -> 422, short by {short_by_cents} cents")
        }
        Err(e @ OrderError::Unavailable { retry_after_s, .. }) => {
            println!("  id={id} -> 503, Retry-After: {retry_after_s}");
            let mut src: Option<&dyn Error> = e.source();
            while let Some(s) = src {
                println!("     caused by: {s}");
                src = s.source();
            }
            println!("  -> the retry budget is an integer in the type, not a phrase in a");
            println!("     string, and the cause chain survived the `?` conversion.");
        }
    }
}
