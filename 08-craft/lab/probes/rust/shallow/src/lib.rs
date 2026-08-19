//! Topic 1, Rust probe: the four-layer shape, in a language that charges for it.
//!
//! WHAT THIS DEMONSTRATES: Rust makes shallowness visible IN THE SIGNATURE. A
//! wrapper that absorbs nothing has to repeat the callee's lifetimes, generic
//! parameters and error type, so the pass-through is character-for-character
//! almost identical to what it wraps. You can *see* the ratio rather than
//! reason about it -- read `repository::fetch_orders` next to `dao::select_orders`.
//!
//! WHAT TO LOOK FOR: run `cargo public-api -p shallow` and count the entries,
//! then do the same for `deep`. Every public item here is interface surface a
//! consumer can depend on, and every one of them is a thing you may not remove
//! without a major version bump.

pub mod dao;
pub mod repository;
pub mod service;

pub use service::{list_customer_orders, OrderListing, OrderPage};

/// The error type. Each layer re-exposes it verbatim, which is the point:
/// absorbing nothing means repeating everything.
#[derive(Debug, PartialEq, Eq)]
pub enum OrderError {
    CustomerNotFound(u64),
    InvalidLimit(usize),
}

impl std::fmt::Display for OrderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderError::CustomerNotFound(id) => write!(f, "customer {id} not found"),
            OrderError::InvalidLimit(n) => write!(f, "limit {n} must be >= 1"),
        }
    }
}

impl std::error::Error for OrderError {}

/// The storage the three layers agree to read from. Public because the dao's
/// public signature mentions it -- which is itself a leak the deep version does
/// not have.
#[derive(Debug, Clone, Default)]
pub struct Store {
    pub customers: Vec<u64>,
    pub orders: Vec<StoredOrder>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredOrder {
    pub id: u64,
    pub customer_id: u64,
    pub status: String,
    pub total_cents: i64,
    pub created_at: i64,
}
