//! Topic 1, Rust probe: the same feature, one deep module.
//!
//! WHAT THIS DEMONSTRATES: the body is not smaller -- it is roughly the same
//! size. What is smaller is the SURFACE. Everything that was a public DTO, a
//! public layer function and a public storage type in `shallow` is `pub(crate)`
//! or private here, so `cargo public-api -p deep` prints a much shorter list.
//!
//! Note the internal structure: `order_query`, `require_customer` and
//! `to_view` are real seams. Depth is about the interface being small, not
//! about the body being one long function -- a 200-line function with no
//! internal structure is topic 1's second broken-experiment note, and it is the
//! easy way to get this "fix" wrong.
//!
//! WHAT TO LOOK FOR: `Store` is not public. A caller builds one through
//! `Store::from_orders`, which means the internal representation can change
//! without a major version bump. In `shallow` it is a public struct with public
//! fields, so it cannot.

use std::fmt;

/// One page of a customer's orders, plus the total that matches the filter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderPage {
    pub items: Vec<OrderView>,
    pub total: usize,
}

/// What a caller sees. One shape, not three.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderView {
    pub id: u64,
    pub status: String,
    pub total_cents: i64,
    pub created_at: i64,
}

#[derive(Debug, PartialEq, Eq)]
#[non_exhaustive] // new variants may be added without breaking callers' matches
pub enum OrderError {
    CustomerNotFound(u64),
    InvalidLimit(usize),
}

impl fmt::Display for OrderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OrderError::CustomerNotFound(id) => write!(f, "customer {id} not found"),
            OrderError::InvalidLimit(n) => write!(f, "limit {n} must be >= 1"),
        }
    }
}

impl std::error::Error for OrderError {}

/// The store. Opaque: its fields are private, so its representation is not
/// part of the contract.
#[derive(Debug, Clone, Default)]
pub struct Store {
    customers: Vec<u64>,
    orders: Vec<Order>,
}

impl Store {
    pub fn new(customers: Vec<u64>, orders: Vec<(u64, u64, &str, i64, i64)>) -> Self {
        Store {
            customers,
            orders: orders
                .into_iter()
                .map(|(id, customer_id, status, total_cents, created_at)| Order {
                    id,
                    customer_id,
                    status: status.to_string(),
                    total_cents,
                    created_at,
                })
                .collect(),
        }
    }

    /// Return one page of `customer_id`'s orders, newest first, with the total.
    ///
    /// Returns `OrderError::CustomerNotFound` when the customer does not exist.
    /// An empty page for a customer that DOES exist is `Ok` with no items --
    /// the two cases are genuinely different and the caller can tell them apart.
    pub fn customer_order_page(
        &self,
        customer_id: u64,
        limit: usize,
    ) -> Result<OrderPage, OrderError> {
        self.require_customer(customer_id)?;
        if limit == 0 {
            return Err(OrderError::InvalidLimit(limit));
        }
        let matching = self.order_query(customer_id);
        let total = matching.len();
        let mut items: Vec<&Order> = matching;
        items.sort_by(|a, b| (b.created_at, b.id).cmp(&(a.created_at, a.id)));
        items.truncate(limit);
        Ok(OrderPage {
            items: items.into_iter().map(to_view).collect(),
            total,
        })
    }

    // --- private. Seams, not surface. ---

    /// One place where the filter is expressed, so the page and the count
    /// cannot drift apart. This is why topic 1's requirement -- "the total must
    /// reflect the filter" -- is a one-line change here.
    fn order_query(&self, customer_id: u64) -> Vec<&Order> {
        self.orders.iter().filter(|o| o.customer_id == customer_id).collect()
    }

    fn require_customer(&self, customer_id: u64) -> Result<(), OrderError> {
        if self.customers.contains(&customer_id) {
            Ok(())
        } else {
            Err(OrderError::CustomerNotFound(customer_id))
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Order {
    id: u64,
    customer_id: u64,
    status: String,
    total_cents: i64,
    created_at: i64,
}

fn to_view(o: &Order) -> OrderView {
    OrderView {
        id: o.id,
        status: o.status.clone(),
        total_cents: o.total_cents,
        created_at: o.created_at,
    }
}
