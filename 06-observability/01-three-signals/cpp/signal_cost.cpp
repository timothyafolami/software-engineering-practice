// Layer 6 Topic 1 - What one unit of telemetry costs the process emitting it.
//
// Why C++: it is the only language here with no logging story at all in the
// standard library, which makes the design pressure visible instead of
// inherited. Every widely used C++ logger -- spdlog, glog, Abseil's LOG,
// Boost.Log -- is a macro, and this file shows why in one measurement: a
// logging *function* evaluates its argument before the level check, a logging
// *macro* does not, and in C++ the argument is usually an ostringstream, which
// is the single most expensive thing in this file.
//
// It is also the language with nothing between you and the mistake. The
// disabled-debug row here is not a style issue: a std::ostringstream built and
// discarded per request is a malloc, a locale lookup, and a virtual call chain,
// for output that is never written.
//
// What this demonstrates
// ----------------------
//   1. counter add       - unordered_map lookup on a bounded label key
//   2. span record       - struct construction, timestamps, six attributes
//   3. log line (INFO)   - ostringstream JSON into a counting sink
//   4. debug, DISABLED, function call, ostringstream built eagerly  <- the bug
//   5. debug, DISABLED, macro, argument never evaluated             <- the fix
//   6. debug, DISABLED, macro with a compile-time level             <- free
//
// Rows 4, 5 and 6 emit nothing.
//
// Reading zeros honestly
// ----------------------
// This is compiled at -O2. A row that reads 0 ns/op has been deleted by the
// optimizer. That is the correct outcome for row 6 and a broken measurement
// anywhere else, so every measured value feeds a volatile sink that is printed
// at the end. Layer 1 of this lab published an optimizer artefact as a finding;
// do not repeat it. If rows 1-4 read zero, the experiment failed.
//
// Run:
//   clang++ -O2 -std=c++17 -o /tmp/signal_cost signal_cost.cpp && /tmp/signal_cost

#include <chrono>
#include <cstdio>
#include <iomanip>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

constexpr long ITERATIONS = 200000;
constexpr long WARMUP = 20000;

// Printed at the end. volatile so the compiler must actually perform every
// store into it, which keeps the benchmarked work alive.
volatile unsigned long long sink = 0;

enum class Level : int { Debug = 20, Info = 30 };

// The compile-time floor. Anything below this is removed at compile time --
// this is what spdlog's SPDLOG_ACTIVE_LEVEL and glog's stripping do.
constexpr Level kCompileTimeLevel = Level::Info;

struct CountingSink {
    unsigned long long bytes = 0;
    unsigned long long lines = 0;
    void write(const std::string& line) {
        bytes += line.size();
        lines += 1;
    }
};

struct Logger {
    CountingSink sink;
    Level level;
    bool enabled(Level l) const { return static_cast<int>(l) >= static_cast<int>(level); }
    void log(Level l, const std::string& line) {
        if (enabled(l)) sink.write(line);
    }
};

// The macro form. `expr` is only evaluated if the guard passes, which is the
// entire reason C++ logging APIs are macros and not functions.
#define DEBUG_LAZY(logger, expr)                 \
    do {                                         \
        if ((logger).enabled(Level::Debug)) {    \
            (logger).log(Level::Debug, (expr));  \
        }                                        \
    } while (0)

// The same, gated on a constexpr. The branch folds at compile time and the
// body -- including the ostringstream -- is not emitted at all.
#define DEBUG_STATIC(logger, expr)                                               \
    do {                                                                         \
        if (static_cast<int>(Level::Debug) >= static_cast<int>(kCompileTimeLevel)) { \
            (logger).log(Level::Debug, (expr));                                  \
        }                                                                        \
    } while (0)

struct Span {
    const char* name;
    const char* trace_id;
    const char* span_id;
    std::vector<std::pair<const char*, std::string>> attributes;
    long long start_ns;
    long long end_ns;
};

struct Row {
    const char* label;
    double ns_per_op;
};

long long now_ns() {
    using namespace std::chrono;
    return duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count();
}

template <typename F>
Row bench(const char* label, F&& f) {
    for (long i = 0; i < WARMUP; ++i) f();
    auto start = std::chrono::steady_clock::now();
    for (long i = 0; i < ITERATIONS; ++i) f();
    auto elapsed = std::chrono::steady_clock::now() - start;
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count();
    return Row{label, ns / static_cast<double>(ITERATIONS)};
}

// Stands in for the serialisation a real debug line performs on its way into
// the logger. Kept out of line so it cannot be inlined into nothing.
__attribute__((noinline)) std::string expensive_argument(const char* order_id,
                                                         const char* customer_id,
                                                         double discount) {
    std::ostringstream os;
    os << "pricing payload={\"order_id\":\"" << order_id
       << "\",\"customer_id\":\"" << customer_id
       << "\",\"discount\":" << discount
       << ",\"items\":[{\"sku\":\"SKU-1\",\"qty\":2},{\"sku\":\"SKU-7\",\"qty\":1}]}";
    return os.str();
}

}  // namespace

int main(int argc, char** argv) {
    std::unordered_map<std::string, unsigned long long> counter;
    Logger logger;
    // Read from argv rather than written as a literal, so the optimizer cannot
    // prove the Debug branch in DEBUG_LAZY is dead and delete it. That deletion
    // would make row 5 read 0 and hide the difference between a runtime guard
    // and a compile-time one. No arguments means Info, so the program still
    // runs with one command and no arguments.
    logger.level = (argc > 1 && std::string(argv[1]) == "--debug") ? Level::Debug : Level::Info;

    const std::string label_key = "GET|/orders/{id}|200";
    std::vector<Row> rows;

    rows.push_back(bench("counter.add (3 bounded labels)", [&] {
        counter[label_key] += 1;
        sink += counter[label_key] & 1ULL;
    }));

    rows.push_back(bench("span create + end (6 attrs)", [&] {
        Span span{"GET /orders/{id}",
                  "4bf92f3577b34da6a3ce929d0e0e4736",
                  "00f067aa0ba902b7",
                  {{"http.request.method", "GET"},
                   {"http.route", "/orders/{id}"},
                   {"http.response.status_code", "200"},
                   {"db.system.name", "postgresql"},
                   {"customer.id", "cus_00194"},
                   {"order.id", "ord_8f31c2"}},
                  now_ns(),
                  0};
        span.end_ns = now_ns();
        sink += static_cast<unsigned long long>(span.end_ns - span.start_ns) +
                span.attributes.size();
    }));

    rows.push_back(bench("log INFO, one JSON line", [&] {
        std::ostringstream os;
        os << "{\"level\":\"info\",\"msg\":\"order priced\",\"order_id\":\"ord_8f31c2\","
              "\"customer_id\":\"cus_00194\",\"duration_ms\":12.4}";
        std::string line = os.str();
        sink += line.size();
        logger.log(Level::Info, line);
    }));

    rows.push_back(bench("log DEBUG (disabled), function, eager arg", [&] {
        // THE BUG. logger.level is Info, nothing is written -- but the
        // ostringstream is constructed, filled, converted to a std::string and
        // destroyed on every call, because C++ evaluates arguments before the
        // call and the level check lives inside the call.
        std::string line = expensive_argument("ord_8f31c2", "cus_00194", 0.15);
        sink += line.size();
        logger.log(Level::Debug, line);
    }));

    rows.push_back(bench("log DEBUG (disabled), macro, runtime level", [&] {
        // THE FIX. One comparison. The ostringstream is never constructed.
        DEBUG_LAZY(logger, expensive_argument("ord_8f31c2", "cus_00194", 0.15));
    }));

    rows.push_back(bench("log DEBUG (disabled), macro, compile-time level", [&] {
        DEBUG_STATIC(logger, expensive_argument("ord_8f31c2", "cus_00194", 0.15));
    }));

    std::string bar(74, '=');
    std::printf("%s\n", bar.c_str());
    std::printf("COST OF EMITTING ONE UNIT OF TELEMETRY   (clang -O2, n=%ld)\n", ITERATIONS);
    std::printf("%s\n", bar.c_str());
    std::printf("%-46s%12s\n", "operation", "ns/op");
    for (const auto& r : rows) std::printf("%-46s%12.1f\n", r.label, r.ns_per_op);

    double eager = rows[3].ns_per_op;
    double lazy = rows[4].ns_per_op;
    double stat = rows[5].ns_per_op;
    std::printf("\nRows 4, 5 and 6 all emit nothing at all.\n");
    std::printf("  function, eager ostringstream : %8.1f ns\n", eager);
    std::printf("  macro, runtime level          : %8.1f ns\n", lazy);
    std::printf("  macro, compile-time level     : %8.1f ns\n", stat);
    std::printf("The %.1f ns gap between rows 4 and 5 is the cost of building a string\n",
                eager - lazy);
    std::printf("that is immediately thrown away. At 8 disabled debug calls per request\n");
    std::printf("and 1000 req/s that is %.1f ms/s of CPU producing nothing.\n",
                8 * 1000 * (eager - lazy) / 1e6);
    std::printf("\nRow 6 is the only row here allowed to read 0.0 -- the branch is\n");
    std::printf("constexpr-false, so no code was emitted. If rows 1-4 also read 0.0 the\n");
    std::printf("optimizer ate the benchmark and the numbers mean nothing.\n");

    std::printf("\nBytes written by the INFO logs: %llu over %llu lines (%.0f B/line).\n",
                logger.sink.bytes, logger.sink.lines,
                logger.sink.lines ? static_cast<double>(logger.sink.bytes) / logger.sink.lines : 0.0);
    std::printf("(sink=%llu, printed so nothing above can be optimised away)\n", sink);
    return 0;
}
