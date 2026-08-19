// Topic 1, C++ probe: the same feature behind an interface that does not leak.
//
// WHAT THIS DEMONSTRATES: the pimpl idiom exists for exactly this reason -- it
// buys an interface that does not drag the implementation into every consumer's
// compile. This header includes <cstdint>, <memory>, <string>, <vector> and
// nothing else; the algorithm headers, the DTO zoo and the storage layout all
// live in the .cpp where no consumer pays for them.
//
// WHAT TO LOOK FOR: touch this header and only its own TU rebuilds. Touch
// shallow/order_types.hpp and every TU that mentioned an order rebuilds. That
// is the rebuild-seconds row of topic 1's probe table.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace deep {

struct OrderView {
    std::uint64_t id;
    std::string status;
    std::int64_t total_cents;
    std::int64_t created_at;
};

struct OrderPage {
    std::vector<OrderView> items;
    std::size_t total;
};

enum class OrderError { none, customer_not_found, invalid_limit };

// The whole interface: construct a store, ask it one question.
class Store {
public:
    Store(std::vector<std::uint64_t> customers,
          std::vector<std::tuple<std::uint64_t, std::uint64_t, std::string, std::int64_t, std::int64_t>> orders);
    ~Store();
    Store(Store&&) noexcept;
    Store& operator=(Store&&) noexcept;

    // Returns OrderError::customer_not_found when the customer does not exist;
    // an existing customer with no orders yields an empty page and
    // OrderError::none. The two cases are distinguishable at the call site.
    OrderError customer_order_page(std::uint64_t customer_id, std::size_t limit, OrderPage& out) const;

private:
    struct Impl;                        // <-- the implementation is not in this header,
    std::unique_ptr<Impl> impl_;        //     so it is not in any consumer's TU either
};

}  // namespace deep
