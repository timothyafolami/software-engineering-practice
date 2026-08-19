//! The precondition for the whole measurement: the two shapes behave identically.
//!
//! Topic 1's fourth broken-experiment note: "the integration tests fail on one
//! shape -- stop and fix that first. Any measurement taken while the two shapes
//! behave differently is measuring the difference in behaviour, and you will
//! attribute it to design."
//!
//!     cargo test

fn fixtures() -> (shallow::Store, deep::Store) {
    let rows = vec![
        (1u64, 7u64, "paid", 500i64, 30i64),
        (2, 7, "cancelled", 100, 20),
        (3, 7, "paid", 900, 10),
        (4, 8, "paid", 100, 40),
    ];
    let s = shallow::Store {
        customers: vec![7, 8],
        orders: rows
            .iter()
            .map(|&(id, customer_id, status, total_cents, created_at)| shallow::StoredOrder {
                id,
                customer_id,
                status: status.to_string(),
                total_cents,
                created_at,
            })
            .collect(),
    };
    let d = deep::Store::new(vec![7, 8], rows);
    (s, d)
}

#[test]
fn both_shapes_return_the_same_page() {
    let (s, d) = fixtures();
    let a = shallow::list_customer_orders(&s, 7, 2).unwrap();
    let b = d.customer_order_page(7, 2).unwrap();

    assert_eq!(a.total, b.total);
    let a_ids: Vec<u64> = a.items.iter().map(|i| i.id).collect();
    let b_ids: Vec<u64> = b.items.iter().map(|i| i.id).collect();
    assert_eq!(a_ids, b_ids, "the two shapes must be indistinguishable to a caller");
}

#[test]
fn both_shapes_reject_an_unknown_customer() {
    let (s, d) = fixtures();
    assert_eq!(
        shallow::list_customer_orders(&s, 99, 2).unwrap_err(),
        shallow::OrderError::CustomerNotFound(99)
    );
    assert_eq!(
        d.customer_order_page(99, 2).unwrap_err(),
        deep::OrderError::CustomerNotFound(99)
    );
}

#[test]
fn both_shapes_reject_a_zero_limit() {
    let (s, d) = fixtures();
    assert!(shallow::list_customer_orders(&s, 7, 0).is_err());
    assert!(d.customer_order_page(7, 0).is_err());
}
