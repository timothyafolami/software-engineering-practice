// Topic 1, C++ probe: the widely-included header that makes a layer PHYSICAL.
//
// WHAT THIS DEMONSTRATES: in C++ the cost of a shallow layer is paid in the
// BUILD, and it is the only language in this lab where you can watch that
// happen. This header is included by every layer, and it drags <string>,
// <vector>, <optional>, <chrono> and <algorithm> into every translation unit
// that touches an order -- whether that TU needs them or not.
//
// WHAT TO LOOK FOR: `g++ -E shallow/api.cpp | wc -l` versus the same for
// `deep/api.cpp`. Preprocessed translation-unit size is a proxy for interface
// surface that no amount of arguing can talk down, and the second number --
// rebuild time after touching one header -- is the one that shows up on a CI
// dashboard.
#pragma once

#include <algorithm>
#include <chrono>
#include <optional>
#include <string>
#include <vector>

namespace shallow {

// DTO #1 of 3. Every layer below declares its own, and every one of them lands
// in every consumer's preprocessed output.
struct OrderRow {
    std::uint64_t id;
    std::uint64_t customer_id;
    std::string status;
    std::int64_t total_cents;
    std::int64_t created_at;
};

// DTO #2 of 3.
struct OrderRecord {
    std::uint64_t id;
    std::uint64_t customer_id;
    std::string status;
    std::int64_t total_cents;
    std::int64_t created_at;
};

// DTO #3 of 3.
struct OrderListing {
    std::uint64_t id;
    std::string status;
    std::int64_t total_cents;
    std::int64_t created_at;
};

struct OrderPage {
    std::vector<OrderListing> items;
    std::size_t total;
};

struct Store {
    std::vector<std::uint64_t> customers;
    std::vector<OrderRow> orders;
};

}  // namespace shallow
