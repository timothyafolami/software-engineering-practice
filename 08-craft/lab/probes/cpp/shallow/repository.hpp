#pragma once
#include "dao.hpp"

namespace shallow::repository {
// The pass-through, in a language where the declaration has to be written out
// in full a second time and then a third in the .cpp.
std::vector<OrderRecord> fetch_orders(const Store& store, std::uint64_t customer_id, std::size_t limit);
std::size_t count_orders(const Store& store, std::uint64_t customer_id);
}  // namespace shallow::repository
