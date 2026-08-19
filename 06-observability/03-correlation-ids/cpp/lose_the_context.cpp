// Layer 6 Topic 3 - Losing trace context at a C++ concurrency boundary.
//
// What this demonstrates
// ----------------------
// C++ has no ambient context of any kind. There is no contextvar, no
// AsyncLocalStorage, no ThreadLocal-backed Context object shipped with the
// language. You have exactly two options, and this file runs both:
//
//   RUN 1  Thread a context struct through every call.
//          Verbose, mechanical, and correct -- the compiler will not let you
//          forget a parameter. This is Go's answer arrived at by necessity.
//
//   RUN 2  Put the context in a `thread_local`.
//          Ergonomic, and exactly wrong the moment a thread pool reuses a
//          thread for a different request. You do not lose the context: you
//          inherit the PREVIOUS request's, which is worse than a truncated
//          trace, because it produces a complete-looking trace that is false.
//          Nothing errors. Nothing is empty. The ID is simply someone else's.
//
// This is why the shared output shape has a third verdict that the other five
// languages do not use: `WRONG (inherited from previous request)`. A truncated
// trace makes you look for the break. A wrong one makes you look at the wrong
// request, and you will not know you are doing it.
//
// The pool below has TWO worker threads, each with its own queue, and requests
// are dispatched by index -- so which thread serves which request is decided by
// this file, not by the OS scheduler. That makes run 2 reproducible instead of
// a race you might not hit. In a real pool it is a race, which is the reason
// this bug survives testing.
//
// macOS/arm64 note: this uses only <thread>, <mutex> and <condition_variable>,
// which are portable. Build with -pthread; Apple clang accepts it.
//
// What to look for in the output
// ------------------------------
// The shared shape:
//
//   caller trace_id   <id>
//   callee trace_id   <id or "none">   naive
//   callee trace_id   <id>             propagated
//   verdict           lost | preserved | WRONG (inherited from previous request)
//
// Then run 2's per-request table. Request C never set the thread-local (its
// handler was a queue drain that assumed context was "already there"), landed
// on the thread that served request A, and reported A's trace ID. Compare the
// `observed` and `truth` columns: they disagree, and no line of that program
// could have noticed.

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

// ---------------------------------------------------------------------------
// A minimal span and the W3C traceparent codec.
// ---------------------------------------------------------------------------

struct Span {
    std::string name;
    std::string trace_id;
    std::string span_id;
    bool sampled = true;
};

static uint64_t xorshift_state = 0x2026081800000001ULL;

static std::string rand_hex(int bytes) {
    // Fixed-seed xorshift. Not secure and does not need to be: nothing here
    // leaves the process.
    std::ostringstream out;
    for (int i = 0; i < bytes; ++i) {
        xorshift_state ^= xorshift_state << 13;
        xorshift_state ^= xorshift_state >> 7;
        xorshift_state ^= xorshift_state << 17;
        char buf[3];
        std::snprintf(buf, sizeof(buf), "%02x",
                      static_cast<unsigned>((xorshift_state >> 24) & 0xff));
        out << buf;
    }
    return out.str();
}

static Span make_span(const std::string& name) {
    Span s;
    s.name = name;
    s.trace_id = rand_hex(16);
    s.span_id = rand_hex(8);
    return s;
}

static std::string traceparent_of(const Span& s) {
    return "00-" + s.trace_id + "-" + s.span_id + "-" + (s.sampled ? "01" : "00");
}

static std::optional<Span> span_from_traceparent(const std::string& header,
                                                 const std::string& name) {
    // version-traceid-spanid-flags
    if (header.size() != 55 || header.substr(0, 3) != "00-") return std::nullopt;
    Span s;
    s.name = name;
    s.trace_id = header.substr(3, 32);
    s.span_id = header.substr(36, 16);
    s.sampled = header.substr(53, 2) != "00";
    if (header[35] != '-' || header[52] != '-') return std::nullopt;
    return s;
}

// ---------------------------------------------------------------------------
// A fixed thread pool where placement is decided by the caller. Each worker
// owns a queue, so `submit(worker_index, task)` is deterministic.
// ---------------------------------------------------------------------------

class PinnedPool {
public:
    explicit PinnedPool(int workers) : queues_(workers), mutexes_(workers), cvs_(workers) {
        for (int i = 0; i < workers; ++i) {
            threads_.emplace_back([this, i] { this->run(i); });
        }
    }

    ~PinnedPool() { shutdown(); }

    void submit(int worker, std::function<void()> task) {
        {
            std::lock_guard<std::mutex> lock(mutexes_[worker]);
            queues_[worker].push(std::move(task));
        }
        cvs_[worker].notify_one();
    }

    void shutdown() {
        if (stopping_.exchange(true)) return;
        for (size_t i = 0; i < cvs_.size(); ++i) cvs_[i].notify_all();
        for (auto& t : threads_) {
            if (t.joinable()) t.join();
        }
    }

private:
    void run(int index) {
        for (;;) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lock(mutexes_[index]);
                cvs_[index].wait(lock, [&] { return stopping_ || !queues_[index].empty(); });
                if (queues_[index].empty()) {
                    if (stopping_) return;
                    continue;
                }
                task = std::move(queues_[index].front());
                queues_[index].pop();
            }
            task();
        }
    }

    std::vector<std::queue<std::function<void()>>> queues_;
    std::vector<std::mutex> mutexes_;
    std::vector<std::condition_variable> cvs_;
    std::vector<std::thread> threads_;
    std::atomic<bool> stopping_{false};
};

// Run a task on a chosen worker and wait for it. Keeps the demos readable
// without hiding which thread did the work.
static std::string run_on(PinnedPool& pool, int worker, std::function<std::string()> task) {
    std::mutex m;
    std::condition_variable cv;
    bool done = false;
    std::string result;
    pool.submit(worker, [&] {
        std::string r = task();
        {
            std::lock_guard<std::mutex> lock(m);
            result = std::move(r);
            done = true;
        }
        cv.notify_one();
    });
    std::unique_lock<std::mutex> lock(m);
    cv.wait(lock, [&] { return done; });
    return result;
}

// ---------------------------------------------------------------------------
// The two options.
// ---------------------------------------------------------------------------

// Option A: an explicit context, passed as a parameter. The only place the
// compiler can help you.
struct RequestContext {
    Span span;
};

static std::string trace_id_of(const RequestContext* ctx) {
    return ctx ? ctx->span.trace_id : "none";
}

// Option B: a thread_local. Never cleared, because clearing it is the line
// everybody forgets -- that omission IS the bug being demonstrated.
static thread_local std::optional<Span> tls_current;

static std::string tls_trace_id() {
    return tls_current ? tls_current->trace_id : "none";
}

// ---------------------------------------------------------------------------
// Structured logging.
// ---------------------------------------------------------------------------

struct LogRecord {
    std::string msg;
    std::string trace_id;
};

static std::mutex log_mutex;
static std::vector<LogRecord> logs;

static void log_info(const std::string& msg, const std::string& trace_id) {
    std::lock_guard<std::mutex> lock(log_mutex);
    logs.push_back({msg, trace_id == "none" ? "" : trace_id});
}

static const char* report(const std::string& boundary, const std::string& caller,
                          const std::string& naive, const std::string& propagated,
                          const std::string& note) {
    const char* verdict = (naive == caller) ? "preserved" : "lost";
    std::printf("boundary          %s\n", boundary.c_str());
    std::printf("caller trace_id   %s\n", caller.c_str());
    std::printf("callee trace_id   %-32s naive\n", naive.c_str());
    std::printf("callee trace_id   %-32s propagated\n", propagated.c_str());
    if (note.empty()) {
        std::printf("verdict           %s\n\n", verdict);
    } else {
        std::printf("verdict           %s   (%s)\n\n", verdict, note.c_str());
    }
    return verdict;
}

// ---------------------------------------------------------------------------
// RUN 1: the explicit context. Naive = the handler calls the worker without
// passing ctx (it compiles, because the parameter is a pointer with a default).
// ---------------------------------------------------------------------------

static std::string price_lookup(const RequestContext* ctx, const char* label) {
    log_info(std::string("pricing call (") + label + ")", trace_id_of(ctx));
    return trace_id_of(ctx);
}

static const char* run_explicit_context(PinnedPool& pool) {
    Span span = make_span("GET /orders");
    RequestContext ctx{span};

    // Naive: the lambda captures nothing but the label. There is no context on
    // that thread and no context in the call, so there is no context.
    std::string naive = run_on(pool, 0, [] { return price_lookup(nullptr, "naive"); });

    // Propagated: capture the context by value. A copy per task is the price,
    // and for a struct this size it is not a price.
    RequestContext copy = ctx;
    std::string propagated =
        run_on(pool, 0, [copy]() mutable { return price_lookup(&copy, "ctx passed"); });

    return report("thread pool, explicit context", span.trace_id, naive, propagated,
                  "the compiler cannot warn you: nullptr is a valid argument");
}

// ---------------------------------------------------------------------------
// RUN 2: the thread_local, on a reused thread. The interesting one.
//
// Three requests, two worker threads, dispatched deterministically:
//
//   request A -> worker 0   handler sets tls_current  (normal path)
//   request B -> worker 1   handler sets tls_current  (normal path)
//   request C -> worker 0   handler does NOT set it   (queue-drain path that
//                           assumes "context is already there")
//
// C reports A's trace ID. Not empty. Not an error. A's.
// ---------------------------------------------------------------------------

struct Observation {
    std::string request;
    int worker;
    std::string observed;
    std::string truth;
    bool set_tls;
};

static const char* run_thread_local(PinnedPool& pool) {
    std::vector<Observation> observed;

    auto serve = [&](const char* name, int worker, bool set_tls) {
        Span span = make_span(name);
        std::string truth = span.trace_id;
        std::string seen = run_on(pool, worker, [span, set_tls]() -> std::string {
            if (set_tls) {
                tls_current = span;  // and nobody ever clears it
            }
            log_info(std::string("handling ") + span.name, tls_trace_id());
            return tls_trace_id();
        });
        observed.push_back({name, worker, seen, truth, set_tls});
    };

    serve("req-A", 0, true);
    serve("req-B", 1, true);
    serve("req-C", 0, false);  // lands on A's thread, sets nothing

    const Observation& a = observed[0];
    const Observation& c = observed[2];

    std::printf("boundary          thread_local on a reused pool thread\n");
    std::printf("caller trace_id   %s   (req-C's real trace)\n", c.truth.c_str());
    std::printf("callee trace_id   %-32s naive\n", c.observed.c_str());
    std::printf("callee trace_id   %-32s propagated (explicit ctx, run 1)\n",
                c.truth.c_str());
    const char* verdict = "preserved";
    if (c.observed == c.truth) {
        verdict = "preserved";
    } else if (c.observed == "none") {
        verdict = "lost";
    } else {
        verdict = "WRONG (inherited from previous request)";
    }
    std::printf("verdict           %s\n\n", verdict);

    std::printf("  request  worker  set tls?  observed trace_id                 real trace_id\n");
    for (const auto& o : observed) {
        std::printf("  %-8s %-7d %-9s %-33s %s%s\n", o.request.c_str(), o.worker,
                    o.set_tls ? "yes" : "no", o.observed.c_str(), o.truth.c_str(),
                    (o.observed == o.truth) ? "" : "   <-- MISMATCH");
    }
    std::printf("\n");
    if (c.observed == a.truth) {
        std::printf("  req-C reported req-A's trace ID. Both requests were real, both\n");
        std::printf("  traces exist, and every span of req-C is now filed under req-A.\n");
        std::printf("  A truncated trace makes you hunt for the break. This one does not\n");
        std::printf("  look broken at all -- which is why it is the worse failure.\n\n");
    }
    return verdict;
}

// ---------------------------------------------------------------------------
// RUN 3: a queue -- the message body is the only thing that crosses.
// ---------------------------------------------------------------------------

static const char* run_queue() {
    Span span = make_span("POST /orders");

    struct Message {
        std::string id;
        std::string traceparent;
    };

    auto consume = [](const Message& m) -> std::string {
        // A separate process. Starts from nothing.
        std::optional<Span> restored;
        if (!m.traceparent.empty()) restored = span_from_traceparent(m.traceparent, "job");
        std::string id = restored ? restored->trace_id : "none";
        log_info("processing job " + m.id, id);
        return id;
    };

    std::string naive = consume({"naive", ""});
    std::string propagated = consume({"propagated", traceparent_of(span)});

    return report("Postgres-backed queue", span.trace_id, naive, propagated,
                  "the transport carries no headers; put traceparent in the body");
}

// ---------------------------------------------------------------------------
// RUN 4: the outbound HTTP call -- the easy half, made concrete.
// ---------------------------------------------------------------------------

static const char* run_http() {
    Span span = make_span("GET /orders");
    std::string header = traceparent_of(span);
    auto downstream = span_from_traceparent(header, "GET /price");
    std::printf("boundary          HTTP request to pricing\n");
    std::printf("caller trace_id   %s\n", span.trace_id.c_str());
    std::printf("traceparent sent  %s\n", header.c_str());
    std::printf("callee trace_id   %-32s parsed from the header\n",
                downstream ? downstream->trace_id.c_str() : "PARSE FAILED");
    std::printf("verdict           preserved   (this is what being a W3C standard buys)\n\n");
    return "preserved";
}

int main() {
    std::printf("Layer 6 Topic 3 - losing trace context in C++ (no ambient context at all)\n");
    std::printf("hardware_concurrency=%u   pool: 2 workers, caller-pinned\n",
                std::thread::hardware_concurrency());
    for (int i = 0; i < 72; ++i) std::printf("=");
    std::printf("\n\n");

    PinnedPool pool(2);

    struct Row {
        const char* name;
        const char* verdict;
        const char* who;
    };
    std::vector<Row> rows;
    rows.push_back({"explicit ctx parameter", run_explicit_context(pool), "YOU carry it - by parameter"});
    rows.push_back({"thread_local + reuse", run_thread_local(pool), "YOU carry it - and it lies"});
    rows.push_back({"Postgres queue", run_queue(), "YOU carry it - in the message body"});
    rows.push_back({"http traceparent", run_http(), "the wire format carries it"});

    pool.shutdown();

    std::printf("--- Summary: C++ has no runtime to blame ---\n");
    for (const auto& r : rows) {
        std::printf("  %-26s %-40s %s\n", r.name, r.verdict, r.who);
    }
    std::printf("\n");
    std::printf("  The choice is between a parameter you might forget to pass (loud:\n");
    std::printf("  it is visible at the call site) and a thread_local you might forget\n");
    std::printf("  to set (silent: it reports the last request that used the thread).\n");
    std::printf("  Every other language in this topic makes the same choice; C++ is\n");
    std::printf("  just the one that does not hide either option behind a keyword.\n\n");

    std::printf("--- The one-query test, on the log lines this run emitted ---\n");
    size_t with_id = 0;
    for (const auto& r : logs) {
        if (!r.trace_id.empty()) ++with_id;
    }
    std::printf("  log lines emitted            %zu\n", logs.size());
    std::printf("  lines carrying a trace_id    %zu\n", with_id);
    std::printf("  lines carrying nothing       %zu   <- unqueryable by request\n",
                logs.size() - with_id);
    for (const auto& r : logs) {
        std::printf("    %-28s trace_id=%s\n", r.msg.c_str(),
                    r.trace_id.empty() ? "(empty)" : r.trace_id.c_str());
    }
    std::printf("\n");
    std::printf("  Count the lines that carry an id but the WRONG id: the log pipeline\n");
    std::printf("  cannot tell you which those are, and neither can the trace store.\n");
    return 0;
}
