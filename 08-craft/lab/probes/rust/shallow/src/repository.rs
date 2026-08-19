//! Layer 2 of 3. Declares `OrderRecord`. Forwards to the dao.
//!
//! Read the two signatures side by side. `fetch_orders` repeats `&Store`,
//! `u64`, `usize` and `Result<_, OrderError>` exactly -- the compiler made the
//! pass-through impossible to disguise, which is the property topic 1 is
//! borrowing this language for.

use crate::dao::{select_order_count, select_orders, OrderRow};
use crate::{OrderError, Store};

/// DTO #2 of 3. Structurally identical to `OrderRow`. Declared anyway.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderRecord {
    pub id: u64,
    pub customer_id: u64,
    pub status: String,
    pub total_cents: i64,
    pub created_at: i64,
}

impl From<OrderRow> for OrderRecord {
    fn from(r: OrderRow) -> Self {
        OrderRecord {
            id: r.id,
            customer_id: r.customer_id,
            status: r.status,
            total_cents: r.total_cents,
            created_at: r.created_at,
        }
    }
}

pub fn fetch_orders(
    store: &Store,
    customer_id: u64,
    limit: usize,
) -> Result<Vec<OrderRecord>, OrderError> {
    Ok(select_orders(store, customer_id, limit)?
        .into_iter()
        .map(OrderRecord::from)
        .collect())
}

pub fn count_orders(store: &Store, customer_id: u64) -> usize {
    select_order_count(store, customer_id)
}
