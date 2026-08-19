//! Layer 1 of 3. Declares `OrderListing`. Forwards to the repository.

use crate::repository::{count_orders, fetch_orders};
use crate::{OrderError, Store};

/// DTO #3 of 3.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderListing {
    pub id: u64,
    pub status: String,
    pub total_cents: i64,
    pub created_at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderPage {
    pub items: Vec<OrderListing>,
    pub total: usize,
}

pub fn list_customer_orders(
    store: &Store,
    customer_id: u64,
    limit: usize,
) -> Result<OrderPage, OrderError> {
    if !store.customers.contains(&customer_id) {
        return Err(OrderError::CustomerNotFound(customer_id));
    }
    let records = fetch_orders(store, customer_id, limit)?;
    let total = count_orders(store, customer_id);
    Ok(OrderPage {
        items: records
            .into_iter()
            .map(|r| OrderListing {
                id: r.id,
                status: r.status,
                total_cents: r.total_cents,
                created_at: r.created_at,
            })
            .collect(),
        total,
    })
}
