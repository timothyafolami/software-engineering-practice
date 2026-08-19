// Layer 8 Topic 3 - C++: four error mechanisms in one program, and why that is
// the lesson rather than a complaint.
//
// WHAT THIS DEMONSTRATES: exceptions, error codes, errno and (since C++23)
// std::expected<T, E> all coexist in one codebase. Exceptions can be disabled
// entirely by a compiler flag, which means a library that throws is unusable in
// half the embedded world, which is why so much C++ returns codes. And
// `noexcept` is a genuine contract: violate it and the program calls
// std::terminate rather than propagating -- the loudest category-3 behaviour in
// this lab.
//
// THE TRANSFERABLE LESSON, and it is aimed at Python: when your language offers
// four mechanisms, the taxonomy has to be WRITTEN DOWN, because the language
// will not imply one. Python offers exactly one mechanism and therefore implies
// even less.
//
// WHAT TO LOOK FOR: `std::expected` is the only one of the four where the
// SIGNATURE tells the caller what can go wrong. Compare the four declarations
// at the top of main().
//
//   g++ -std=c++23 -O2 -o /tmp/t3_cpp cpp/four_mechanisms.cpp && /tmp/t3_cpp
//   (Apple clang 21 supports <expected>; if yours does not, the file falls back
//    to a minimal stand-in so the other three mechanisms still run.)

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <exception>
#include <stdexcept>
#include <string>

#if __has_include(<expected>) && __cplusplus >= 202302L
#include <expected>
#define HAS_EXPECTED 1
#else
#define HAS_EXPECTED 0
#endif

// --- the taxonomy, written down, because the language will not imply one -----

enum class OrderErrc {
    ok = 0,
    not_found = 1,       // category 1: caller returns 404
    insufficient = 2,    // category 1: caller shows a message
    unavailable = 3,     // category 2: caller retries with a budget
};

const char* describe(OrderErrc e) {
    switch (e) {
        case OrderErrc::ok:           return "ok";
        case OrderErrc::not_found:    return "not found (404)";
        case OrderErrc::insufficient: return "insufficient funds (422)";
        case OrderErrc::unavailable:  return "dependency unavailable (503)";
    }
    return "unknown";
}

// --- mechanism 1: exceptions -------------------------------------------------
// The signature says NOTHING about what can be thrown. C++ removed dynamic
// exception specifications for exactly the reason Java's checked exceptions
// struggled: the granularity was wrong and the escape hatch was easier.

struct OrderNotFound : std::runtime_error {
    explicit OrderNotFound(unsigned long long id)
        : std::runtime_error("order " + std::to_string(id) + " not found") {}
};

unsigned long long load_order_throwing(unsigned long long id) {
    if (id == 0) throw OrderNotFound(id);
    return id * 100;
}

// --- mechanism 2: an error code out-parameter --------------------------------
// Works with exceptions disabled. The caller CAN ignore the code, and the
// compiler will not stop them -- which is the whole problem with this shape.

unsigned long long load_order_code(unsigned long long id, OrderErrc& ec) {
    if (id == 0) { ec = OrderErrc::not_found; return 0; }
    ec = OrderErrc::ok;
    return id * 100;
}

// --- mechanism 3: errno ------------------------------------------------------
// Global, thread-local, and set by things you did not call. It is only
// meaningful IMMEDIATELY after a call that documents it, which is a contract
// nobody writes down.

unsigned long long load_order_errno(unsigned long long id) {
    if (id == 0) { errno = ENOENT; return 0; }
    return id * 100;
}

// --- mechanism 4: std::expected (C++23) --------------------------------------
// The signature states the success type AND the error type, and the caller
// cannot read the value without acknowledging the error. This is Rust's
// Result<T, E>, arriving in C++ twenty years later and for the same reasons.

#if HAS_EXPECTED
std::expected<unsigned long long, OrderErrc> load_order_expected(unsigned long long id) {
    if (id == 0) return std::unexpected(OrderErrc::not_found);
    if (id == 1) return std::unexpected(OrderErrc::insufficient);
    return id * 100;
}
#endif

// --- noexcept: the loudest category-3 behaviour in this lab ------------------

// NOTE: this function compiles with a warning, deliberately:
//   warning: 'invariant_check' has a non-throwing exception specification but
//            can still throw [-Wexceptions]
// That warning IS the demonstration. The compiler can see the contradiction and
// says so; what it cannot do is stop you, and at run time the resolution is
// std::terminate rather than propagation. Leave the warning in place.
void invariant_check(bool holds) noexcept {
    if (!holds) {
        // Throwing out of a `noexcept` function does not propagate. It calls
        // std::terminate. That is correct for a violated invariant: nothing the
        // caller can do, so do not offer them a branch.
        throw std::logic_error("invariant violated");
    }
}

int main() {
    std::printf("=== four mechanisms, four signatures, one taxonomy ===\n");
    std::printf("  1 exceptions   unsigned long long load_order_throwing(id)"
                "                  <- says nothing\n");
    std::printf("  2 error code   unsigned long long load_order_code(id, OrderErrc&)"
                "        <- ignorable\n");
    std::printf("  3 errno        unsigned long long load_order_errno(id)"
                "                     <- global, implicit\n");
#if HAS_EXPECTED
    std::printf("  4 expected     std::expected<unsigned long long, OrderErrc> load_order_expected(id)\n");
    std::printf("                 ^ the only one whose TYPE is the contract\n");
#else
    std::printf("  4 expected     <expected> unavailable in this toolchain\n");
#endif

    std::printf("\n=== 1. exceptions ===\n");
    try {
        load_order_throwing(0);
    } catch (const OrderNotFound& e) {
        std::printf("  caught: %s\n", e.what());
    }
    std::printf("  -> works, and a caller who never reads the docs never learns this\n"
                "     function can throw at all.\n");

    std::printf("\n=== 2. error codes: the ignorable version ===\n");
    OrderErrc ec = OrderErrc::ok;
    auto total = load_order_code(0, ec);
    std::printf("  checked   : total=%llu, ec=%s\n", total, describe(ec));
    OrderErrc ignored = OrderErrc::ok;
    std::printf("  IGNORED   : total=%llu   <- the caller used a zero as an answer,\n",
                load_order_code(0, ignored));
    std::printf("              and nothing in the language objected.\n");

    std::printf("\n=== 3. errno: valid only immediately after the call ===\n");
    errno = 0;
    auto t3 = load_order_errno(0);
    std::printf("  total=%llu errno=%d (%s)\n", t3, errno, std::strerror(errno));
    std::printf("  -> insert ANY other library call between those two lines and this\n"
                "     reading becomes meaningless. That is a contract nobody wrote down.\n");

#if HAS_EXPECTED
    std::printf("\n=== 4. std::expected: the error is in the type ===\n");
    for (unsigned long long id : {0ULL, 1ULL, 7ULL}) {
        auto r = load_order_expected(id);
        if (r) std::printf("  id=%llu -> 200, total %llu\n", id, *r);
        else   std::printf("  id=%llu -> %s\n", id, describe(r.error()));
    }
    std::printf("  -> the caller cannot reach the value without acknowledging the\n"
                "     error, and the set of errors is enumerated in the signature.\n");
#endif

    std::printf("\n=== noexcept: category 3, at maximum volume ===\n");
    invariant_check(true);
    std::printf("  invariant_check(true)  -> returned normally\n");
    std::printf("  invariant_check(false) -> would call std::terminate, NOT propagate.\n");
    std::printf("     `noexcept` is a promise to the compiler AND to the reader; breaking\n"
                "     it aborts the process rather than offering anyone a branch. That is\n"
                "     the right shape for a violated invariant, and it is the loudest\n"
                "     category-3 mechanism in this lab.\n");
    return 0;
}
