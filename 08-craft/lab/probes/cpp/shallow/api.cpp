// The single translation unit that includes the whole four-layer stack.
//
//   g++ -std=c++20 -E shallow/api.cpp | wc -l      # preprocessed TU lines
//   g++ -std=c++20 -O2 -o /tmp/t1_shallow shallow/api.cpp && /tmp/t1_shallow
#include "service.hpp"

#include <cstdio>

namespace shallow::dao {

std::vector<OrderRow> select_orders(const Store& store, std::uint64_t customer_id, std::size_t limit) {
    std::vector<OrderRow> rows;
    for (const auto& o : store.orders)
        if (o.customer_id == customer_id) rows.push_back(o);
    std::sort(rows.begin(), rows.end(), [](const OrderRow& a, const OrderRow& b) {
        if (a.created_at != b.created_at) return a.created_at > b.created_at;
        return a.id > b.id;
    });
    if (rows.size() > limit) rows.resize(limit);
    return rows;
}

std::size_t select_order_count(const Store& store, std::uint64_t customer_id) {
    std::size_t n = 0;
    for (const auto& o : store.orders)
        if (o.customer_id == customer_id) ++n;
    return n;
}

}  // namespace shallow::dao

namespace shallow::repository {

std::vector<OrderRecord> fetch_orders(const Store& store, std::uint64_t customer_id, std::size_t limit) {
    std::vector<OrderRecord> out;
    for (auto& r : dao::select_orders(store, customer_id, limit))
        out.push_back(OrderRecord{r.id, r.customer_id, r.status, r.total_cents, r.created_at});
    return out;  // a copy per layer, which is the runtime half of the same bill
}

std::size_t count_orders(const Store& store, std::uint64_t customer_id) {
    return dao::select_order_count(store, customer_id);
}

}  // namespace shallow::repository

namespace shallow::service {

std::optional<OrderPage> list_customer_orders(const Store& store, std::uint64_t customer_id, std::size_t limit) {
    if (std::find(store.customers.begin(), store.customers.end(), customer_id) == store.customers.end())
        return std::nullopt;
    OrderPage page;
    for (auto& r : repository::fetch_orders(store, customer_id, limit))
        page.items.push_back(OrderListing{r.id, r.status, r.total_cents, r.created_at});
    page.total = repository::count_orders(store, customer_id);
    return page;
}

}  // namespace shallow::service

int main() {
    shallow::Store store{
        {7, 8},
        {{1, 7, "paid", 500, 30}, {2, 7, "cancelled", 100, 20}, {3, 7, "paid", 900, 10}, {4, 8, "paid", 100, 40}},
    };
    auto page = shallow::service::list_customer_orders(store, 7, 2);
    std::printf("[shallow] customer 7: %zu items of %zu total; ids:",
                page->items.size(), page->total);
    for (const auto& i : page->items) std::printf(" %llu", (unsigned long long)i.id);
    std::printf("\n[shallow] unknown customer -> %s\n",
                shallow::service::list_customer_orders(store, 99, 2).has_value() ? "value" : "nullopt");
    return 0;
}
