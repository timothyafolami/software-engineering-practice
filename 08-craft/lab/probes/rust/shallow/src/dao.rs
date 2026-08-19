//! Layer 3 of 3. Owns the "query". Declares `OrderRow`.

use crate::{OrderError, StoredOrder, Store};

/// DTO #1 of 3.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderRow {
    pub id: u64,
    pub customer_id: u64,
    pub status: String,
    pub total_cents: i64,
    pub created_at: i64,
}

impl From<&StoredOrder> for OrderRow {
    fn from(o: &StoredOrder) -> Self {
        OrderRow {
            id: o.id,
            customer_id: o.customer_id,
            status: o.status.clone(),
            total_cents: o.total_cents,
            created_at: o.created_at,
        }
    }
}

pub fn select_orders(
    store: &Store,
    customer_id: u64,
    limit: usize,
) -> Result<Vec<OrderRow>, OrderError> {
    if limit == 0 {
        return Err(OrderError::InvalidLimit(limit));
    }
    let mut rows: Vec<OrderRow> = store
        .orders
        .iter()
        .filter(|o| o.customer_id == customer_id)
        .map(OrderRow::from)
        .collect();
    rows.sort_by(|a, b| (b.created_at, b.id).cmp(&(a.created_at, a.id)));
    rows.truncate(limit);
    Ok(rows)
}

pub fn select_order_count(store: &Store, customer_id: u64) -> usize {
    store.orders.iter().filter(|o| o.customer_id == customer_id).count()
}
