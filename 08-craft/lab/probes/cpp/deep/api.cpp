//   g++ -std=c++20 -E deep/api.cpp | wc -l
//   g++ -std=c++20 -O2 -o /tmp/t1_deep deep/api.cpp && /tmp/t1_deep
#include "orders.hpp"

#include <algorithm>
#include <cstdio>
#include <tuple>

namespace deep {

namespace {
struct Order {
    std::uint64_t id;
    std::uint64_t customer_id;
    std::string status;
    std::int64_t total_cents;
    std::int64_t created_at;
};
}  // namespace

struct Store::Impl {
    std::vector<std::uint64_t> customers;
    std::vector<Order> orders;

    // One place where the filter is expressed, so the page and the count cannot
    // drift apart. Same argument as the Python and Rust versions.
    std::vector<const Order*> order_query(std::uint64_t customer_id) const {
        std::vector<const Order*> out;
        for (const auto& o : orders)
            if (o.customer_id == customer_id) out.push_back(&o);
        return out;
    }
};

Store::Store(std::vector<std::uint64_t> customers,
             std::vector<std::tuple<std::uint64_t, std::uint64_t, std::string, std::int64_t, std::int64_t>> orders)
    : impl_(std::make_unique<Impl>()) {
    impl_->customers = std::move(customers);
    for (auto& t : orders)
        impl_->orders.push_back(Order{std::get<0>(t), std::get<1>(t), std::get<2>(t),
                                      std::get<3>(t), std::get<4>(t)});
}

Store::~Store() = default;
Store::Store(Store&&) noexcept = default;
Store& Store::operator=(Store&&) noexcept = default;

OrderError Store::customer_order_page(std::uint64_t customer_id, std::size_t limit, OrderPage& out) const {
    if (std::find(impl_->customers.begin(), impl_->customers.end(), customer_id) == impl_->customers.end())
        return OrderError::customer_not_found;
    if (limit == 0) return OrderError::invalid_limit;

    auto matching = impl_->order_query(customer_id);
    out.total = matching.size();
    std::sort(matching.begin(), matching.end(), [](const Order* a, const Order* b) {
        if (a->created_at != b->created_at) return a->created_at > b->created_at;
        return a->id > b->id;
    });
    if (matching.size() > limit) matching.resize(limit);
    out.items.clear();
    for (const Order* o : matching)
        out.items.push_back(OrderView{o->id, o->status, o->total_cents, o->created_at});
    return OrderError::none;
}

}  // namespace deep

int main() {
    deep::Store store({7, 8}, {{1, 7, "paid", 500, 30},
                               {2, 7, "cancelled", 100, 20},
                               {3, 7, "paid", 900, 10},
                               {4, 8, "paid", 100, 40}});
    deep::OrderPage page;
    auto err = store.customer_order_page(7, 2, page);
    std::printf("[deep] customer 7: %zu items of %zu total; ids:", page.items.size(), page.total);
    for (const auto& i : page.items) std::printf(" %llu", (unsigned long long)i.id);
    std::printf("\n[deep] unknown customer -> %s\n",
                store.customer_order_page(99, 2, page) == deep::OrderError::customer_not_found
                    ? "customer_not_found" : "value");
    (void)err;
    return 0;
}
