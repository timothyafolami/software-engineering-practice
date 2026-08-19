#pragma once
#include "repository.hpp"

namespace shallow::service {
// Returns std::nullopt when the customer does not exist. Note that to learn
// this you must open service.hpp; to learn what happens with limit == 0 you
// must open dao.hpp. Two files for one question is the cognitive-load row.
std::optional<OrderPage> list_customer_orders(const Store& store, std::uint64_t customer_id, std::size_t limit);
}  // namespace shallow::service
