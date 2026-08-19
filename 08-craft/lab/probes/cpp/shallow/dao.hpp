#pragma once
#include "order_types.hpp"

namespace shallow::dao {
std::vector<OrderRow> select_orders(const Store& store, std::uint64_t customer_id, std::size_t limit);
std::size_t select_order_count(const Store& store, std::uint64_t customer_id);
}  // namespace shallow::dao
