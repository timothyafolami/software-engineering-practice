// Layer 8 Topic 4 - C++: the knob for how much unspecified behaviour a double
// tolerates, which is the thing Python's AsyncMock does not have at all.
//
// WHAT THIS DEMONSTRATES: gMock needs a seam -- a virtual function or a template
// parameter -- exactly like Rust's trait, so the design has to admit its
// dependency before a test can substitute it. Its own contribution is the
// STRICTNESS setting: an "uninteresting call" (a call to a mocked method with no
// EXPECT_CALL on it) is a warning under the default NaggyMock, silent under
// NiceMock, and a test failure under StrictMock.
//
// gMock is not installed on this machine and is not needed: `MockRows` below
// hand-implements the three policies, which is about forty lines, and having
// them in front of you is more useful than having them behind a macro. The
// semantics match gMock's documented behaviour for uninteresting calls and for
// unsatisfied expectations.
//
// The bug under test is the one strictness catches and correctness assertions do
// not: `recent_orders` makes an EXTRA round trip (`total_count`) that nobody
// asked for. Every output assertion still passes, so a Nice double is green, a
// Naggy double is green with a warning on stderr nobody reads in CI, and only a
// Strict double turns it into a failure.
//
// WHAT TO LOOK FOR: RUN 1 and RUN 2 are green on broken code. RUN 2 prints a
// warning line -- that line is the entire difference between the default and
// Nice, and it does not change the exit status. RUN 3 fails. RUN 4 shows the two
// ordering/limit bugs that NO strictness setting can see, because strictness is
// about calls, not about semantics.
//
//   g++ -std=c++20 -O2 -o /tmp/t4_cpp cpp/nice_naggy_strict.cpp && /tmp/t4_cpp

#include <algorithm>
#include <cstdio>
#include <set>
#include <string>
#include <vector>

// --- the domain -------------------------------------------------------------

struct Order {
    int id;
    int customer_id;
    std::string status;
    int created_at;  // minutes since an arbitrary epoch
};

struct Query {
    int customer_id;
    bool exclude_cancelled;
    bool order_by_created_desc;
    int limit;
};

// THE SEAM. gMock cannot mock this without `virtual`; that requirement is the
// same design pressure Rust applies with `trait`, applied with less enforcement.
struct Rows {
    virtual ~Rows() = default;
    virtual std::vector<Order> query(const Query& q) = 0;
    virtual int total_count(int customer_id) = 0;
};

// --- the unit under test ------------------------------------------------------

// Three bugs, deliberately of two different kinds:
//   BUG 1  no ordering is requested.
//   BUG 2  the cancelled filter runs here, after the store applied the limit.
//   BUG 3  an extra round trip whose result is discarded. Correct output, wrong
//          behaviour -- and the only one a strictness setting can catch.
std::vector<Order> recent_orders(Rows& rows, int customer_id, int limit) {
    Query q{customer_id, /*exclude_cancelled=*/false,
            /*order_by_created_desc=*/false, limit};
    std::vector<Order> fetched = rows.query(q);

    (void)rows.total_count(customer_id);  // BUG 3: nobody uses this

    std::vector<Order> out;
    for (const Order& o : fetched) {
        if (o.status != "cancelled") out.push_back(o);  // BUG 2: after the limit
    }
    return out;
}

std::vector<Order> recent_orders_fixed(Rows& rows, int customer_id, int limit) {
    Query q{customer_id, /*exclude_cancelled=*/true,
            /*order_by_created_desc=*/true, limit};
    return rows.query(q);
}

// --- the double, with gMock's three strictness policies -----------------------

enum class Strictness { Naggy, Nice, Strict };

class MockRows : public Rows {
public:
    explicit MockRows(Strictness s, std::vector<Order> scripted)
        : strictness_(s), scripted_(std::move(scripted)) {}

    // EXPECT_CALL(mock, method(...)) -- declares the call interesting.
    void expect_call(const std::string& method) { expected_.insert(method); }

    std::vector<Order> query(const Query& q) override {
        seen_ = q;
        note("query");
        return scripted_;  // THE LIE: `q` was recorded and then not consulted.
    }

    int total_count(int customer_id) override {
        note("total_count");
        return static_cast<int>(scripted_.size()) + customer_id * 0;
    }

    const Query& last_query() const { return seen_; }
    const std::vector<std::string>& violations() const { return violations_; }

private:
    void note(const std::string& method) {
        called_.insert(method);
        if (expected_.count(method)) return;  // an interesting call: fine

        switch (strictness_) {
            case Strictness::Nice:
                break;  // silence. The call happened and nobody will ever know.
            case Strictness::Naggy:
                // stdout is block-buffered when piped and stderr is not, so
                // flush first or the warning jumps to the top of the output.
                std::fflush(stdout);
                std::fprintf(stderr,
                             "GMOCK WARNING: Uninteresting mock function call - "
                             "returning default value.\n    Function call: %s(...)\n",
                             method.c_str());
                break;
            case Strictness::Strict:
                violations_.push_back("unexpected call to " + method + "()");
                break;
        }
    }

    Strictness strictness_;
    std::vector<Order> scripted_;
    std::set<std::string> expected_;
    std::set<std::string> called_;
    std::vector<std::string> violations_;
    Query seen_{0, false, false, 0};
};

// --- the honest fake: a table, with the clauses applied in a table's order -----

class HeapRows : public Rows {
public:
    explicit HeapRows(std::vector<Order> stored) : stored_(std::move(stored)) {}

    std::vector<Order> query(const Query& q) override {
        std::vector<Order> out;
        for (const Order& o : stored_) {           // storage order, not sorted
            if (o.customer_id == q.customer_id) out.push_back(o);
        }
        if (q.exclude_cancelled) {                 // WHERE, before LIMIT
            std::vector<Order> kept;
            for (const Order& o : out)
                if (o.status != "cancelled") kept.push_back(o);
            out.swap(kept);
        }
        if (q.order_by_created_desc) {
            std::stable_sort(out.begin(), out.end(), [](const Order& a, const Order& b) {
                if (a.created_at != b.created_at) return a.created_at > b.created_at;
                return a.id > b.id;                // a TOTAL order; ties happen
            });
        }
        if (static_cast<int>(out.size()) > q.limit) out.resize(q.limit);  // LIMIT last
        return out;
    }

    int total_count(int customer_id) override {
        int n = 0;
        for (const Order& o : stored_) n += (o.customer_id == customer_id);
        return n;
    }

private:
    std::vector<Order> stored_;
};

// --- fixtures -----------------------------------------------------------------

static Order mk(int id, int minutes, const char* status, int cust = 1) {
    return Order{id, cust, status, minutes};
}

// The answer, already ordered and already filtered, because that is what the
// author meant. This fixture is the bug's alibi.
static std::vector<Order> newest_first() {
    return {mk(5, 50, "paid"), mk(4, 40, "paid"), mk(3, 30, "paid"), mk(2, 20, "paid")};
}

// The same rows as a heap holds them: inserted shuffled, 5 and 4 UPDATEd after,
// which on a real heap moves them to the end of the table.
static std::vector<Order> storage_order() {
    return {mk(2, 20, "paid"),      mk(6, 60, "cancelled"), mk(3, 30, "paid"),
            mk(1, 10, "cancelled"), mk(5, 50, "paid"),      mk(4, 40, "paid"),
            mk(9, 90, "paid", 2)};
}

static std::string ids(const std::vector<Order>& v) {
    std::string s = "[";
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) s += ", ";
        s += std::to_string(v[i].id);
    }
    return s + "]";
}

// --- a two-line test harness ---------------------------------------------------

struct Suite {
    int passed = 0, failed = 0;
    void check(const char* what, bool ok, const std::string& detail) {
        (ok ? passed : failed)++;
        std::printf("  [%s] %-24s %s\n", ok ? "PASS" : "FAIL", what, detail.c_str());
    }
    void report() const { std::printf("  ----> %d/%d pass\n", passed, passed + failed); }
};

// The three output assertions, identical in every run. Only the double changes.
static Suite output_assertions(Rows& rows) {
    Suite s;
    const int limit = 4;
    std::vector<Order> got = recent_orders(rows, 1, limit);
    s.check("returns newest first", ids(got) == "[5, 4, 3, 2]",
            "got " + ids(got) + ", want [5, 4, 3, 2]");
    bool none_cancelled = std::none_of(got.begin(), got.end(),
                                       [](const Order& o) { return o.status == "cancelled"; });
    s.check("excludes cancelled", none_cancelled,
            std::to_string(got.size()) + " rows, none cancelled");
    s.check("respects the limit", static_cast<int>(got.size()) == limit,
            "asked for " + std::to_string(limit) + ", got " + std::to_string(got.size()));
    return s;
}

int main() {
    std::printf("Layer 8 topic 4 - C++: Nice, Naggy and Strict over the same broken code.\n");
    std::printf("`recent_orders` has three bugs. Watch which runs notice which.\n");

    std::printf("\nRUN 1 - NiceMock: uninteresting calls are silent\n");
    MockRows nice(Strictness::Nice, newest_first());
    nice.expect_call("query");
    Suite r1 = output_assertions(nice);
    r1.report();
    std::printf("  strictness violations: %zu\n", nice.violations().size());

    std::printf("\nRUN 2 - NaggyMock (gMock's DEFAULT): a warning on stderr, and nothing else\n");
    MockRows naggy(Strictness::Naggy, newest_first());
    naggy.expect_call("query");
    Suite r2 = output_assertions(naggy);
    r2.report();
    std::printf("  strictness violations: %zu   <- the warning above is not a failure,\n",
                naggy.violations().size());
    std::printf("  and CI keeps no record of it. This is the most common real outcome.\n");

    std::printf("\nRUN 3 - StrictMock: an unexpected call IS a failure\n");
    MockRows strict(Strictness::Strict, newest_first());
    strict.expect_call("query");
    Suite r3 = output_assertions(strict);
    for (const std::string& v : strict.violations())
        std::printf("  [FAIL] %-24s %s\n", "no unexpected calls", v.c_str());
    std::printf("  ----> %d/%d pass\n", r3.passed,
                r3.passed + r3.failed + static_cast<int>(strict.violations().size()));
    std::printf("  BUG 3 caught. Note what caught it: not an assertion about the\n");
    std::printf("  RESULT, which is identical in all three runs, but a policy about\n");
    std::printf("  what the double is willing to tolerate.\n");

    std::printf("\nRUN 4 - the same assertions against a fake that models a table\n");
    HeapRows heap(storage_order());
    Suite r4 = output_assertions(heap);
    r4.report();
    std::vector<Order> broken = recent_orders(heap, 1, 4);
    std::vector<Order> fixed = recent_orders_fixed(heap, 1, 4);
    std::printf("  recent_orders(limit=4)       -> %s  (%zu rows)\n", ids(broken).c_str(), broken.size());
    std::printf("  recent_orders_fixed(limit=4) -> %s  (%zu rows)\n", ids(fixed).c_str(), fixed.size());

    std::printf("\nWHERE THE ORDERING CAME FROM\n");
    const Query& q = nice.last_query();
    std::printf("  the double was handed order_by_created_desc=%s, exclude_cancelled=%s\n",
                q.order_by_created_desc ? "true" : "false",
                q.exclude_cancelled ? "true" : "false");
    std::printf("  and returned newest-first rows anyway, because `newest_first()` was\n");
    std::printf("  written by someone who already knew the answer.\n");

    std::printf("\nSUMMARY\n");
    std::printf("  Nice   %d/%d   +0 violations   -> green\n", r1.passed, r1.passed + r1.failed);
    std::printf("  Naggy  %d/%d   +0 violations   -> green, with a warning\n", r2.passed, r2.passed + r2.failed);
    std::printf("  Strict %d/%d   +%zu violation(s) -> RED, on bug 3\n", r3.passed,
                r3.passed + r3.failed, strict.violations().size());
    std::printf("  Heap   %d/%d                   -> RED, on bugs 1 and 2\n", r4.passed, r4.passed + r4.failed);
    std::printf("  Strictness and fidelity are different axes. Strict caught the call\n");
    std::printf("  nobody expected; only the fake caught the semantics nobody modelled.\n");
    std::printf("  Python's AsyncMock has NEITHER knob: every call is uninteresting and\n");
    std::printf("  every uninteresting call is fine.\n");
    return 0;
}
