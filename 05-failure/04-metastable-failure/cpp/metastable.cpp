// Layer 5 - Topic 4: metastable failure, in one C++ process.
//
// THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
// The claim is that the thing which TRIGGERS an outage and the thing which
// SUSTAINS it are different mechanisms, so removing the trigger does not end
// the outage. This file removes the trigger, keeps offered load exactly where
// it was, waits, and shows you nothing improving.
//
// C++ gives you every sustaining effect at once and no defence from any of
// them, so this version is deliberately NOT a translation of the other five.
// It is the same experiment with the runtime's real defaults:
//
//   - The queue is an unbounded std::deque behind a mutex, which is the
//     default shape of every hand-rolled thread pool ever written. Nothing
//     bounds it, and nothing in the language suggests it should be bounded.
//   - THERE IS NO CANCELLATION. When a caller's 500ms deadline passes, the
//     other five runtimes drop the future, cancel the task or close the
//     context, and the abandoned work stops holding a connection. Here the
//     item stays in the queue, gets picked up eventually, and runs to
//     completion for a caller that left. Nothing in the language knows the
//     caller existed.
//   - So the queue fills with DEAD WORK, and that is the sustaining effect
//     this file is built to make visible. `qlen` and `oldest` below are the
//     age instrumentation the other five cannot show you: watch the oldest
//     item's age climb past the client timeout and keep going. Every
//     millisecond past 500 on that column is a connection spent on an answer
//     nobody will read.
//
// Read `zombie` in the per-scenario footer next to it. That is completions
// that finished AFTER the caller had given up -- topic 2's zombie work,
// arriving here as topic 4's amplifier.
//
// This is also why escape (c) is spelled differently here than in the other
// five files, and the difference is the most useful thing in this program. An
// in-flight LIMIT alone does nothing in C++ once the queue is full of dead
// work: the gauge it reads is dominated by items nobody is waiting for, so it
// rejects every new arrival while the pool keeps grinding through corpses at
// 30 a second. The shedder needs a second half -- permission to DROP, at
// dequeue, anything whose deadline has already passed. That is CoDel's rule,
// reject on measured wait, and it is topic 5 arriving early. In Python, Go,
// Rust and Java cancellation quietly does this for you; here you write it,
// and `dropped at dequeue` in the footer counts how much of the queue turned
// out to be garbage.
//
// WHAT THIS DEMONSTRATES
//
//   A cache in front of a database, at a 90% hit rate, comfortably stable.
//   The trigger is one instantaneous, fully reversible command: FLUSHALL.
//   The cache is BACK the moment it starts refilling -- except that it never
//   starts, because refilling requires a query to finish before its caller
//   gives up, and no query does any more.
//
//   HotOS '25 vocabulary, which this file is built to make concrete:
//     trigger                 the cache flush, over in one millisecond
//     amplification mechanism naive retries (topic 3) plus the miss rate
//                             going from 10% to 100%
//     sustaining effect       a cache that cannot refill, because fills only
//                             happen on completions that beat the deadline --
//                             and, here, a queue of work whose callers are
//                             already gone
//
//   The threads are the shape of an ordinary C++ service, not a simulation:
//   one arrival thread, one client-timer thread holding the deadlines, six
//   "connection" threads which ARE the pool, and four fast-path threads for
//   cache hits so that a hit does not queue behind a miss.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `goodput` versus `thruput`. Throughput stays high while goodput goes
//      to zero: the process is busy, the pool is full, requests are flowing,
//      and almost none of them produce a response anybody receives.
//   2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
//      sustaining effect, and it is why scenario 0 never recovers.
//   3. `oldest` -- the age of the item at the head of the queue, in
//      milliseconds. Once it passes the 500ms client timeout, every single
//      thing the pool does is wasted, and the pool has no way to know.
//   4. Which escapes are SUFFICIENT rather than merely helpful. The verdict
//      lines at the end are computed from THIS run, not asserted here.
//
// RUN
//   c++ -O2 -std=c++17 -pthread -o /tmp/metastable metastable.cpp && /tmp/metastable
//
// Roughly four minutes: five scenarios, the four with an escape running
// longer because "did it recover" is a question about minutes, not seconds.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;
using Ms = std::chrono::milliseconds;

// ---------------------------------------------------------------- config
//
// Identical to python/metastable.py's constants, deliberately: the point of
// six languages here is that the same system-level dynamic appears in all of
// them, so the constants are not allowed to drift.

constexpr double OFFERED_RPS = 180.0;  // constant. It never changes.
constexpr int KEYS = 400;              // the cache keyspace
constexpr int EVICT_PER_SEC = 18;      // TTL churn -> 90% hit rate

constexpr auto DB_SERVICE = Ms(200);    // an uncached read
constexpr auto CACHE_SERVICE = Ms(1);   // a cached one
constexpr int POOL_SIZE = 6;            // 6 / 0.200 = 30 misses/s of capacity
constexpr int FAST_THREADS = 4;         // the cache-hit path

constexpr auto CLIENT_TIMEOUT = Ms(500);  // longer than normal service time,
constexpr int ATTEMPTS = 3;               // shorter than degraded. On purpose.

constexpr double TRIGGER_AT = 6.0;    // redis-cli FLUSHALL
constexpr double ESCAPE_AT = 16.0;    // ten seconds of watching nothing improve
constexpr double END_AT = 30.0;       // long enough to prove 0 does not recover
constexpr double ESCAPE_END_AT = 50.0;
constexpr double REPORT_EVERY = 2.0;

constexpr long SHED_LIMIT = 8;             // escape (c). Topic 5, early.
constexpr double BUDGET_RATIO = 0.10;      // escape (b). Topic 3's bucket.
constexpr double RAMP_BACK_SECONDS = 8.0;  // escape (a) lets load back SLOWLY.
constexpr double DROP_SECONDS = 5.0;

// ---------------------------------------------------------------- metrics

struct Metrics {
    std::atomic<long> goodput{0};
    std::atomic<long> thruput{0};
    std::atomic<long> retries{0};
    std::atomic<long> failed{0};
    std::atomic<long> shed{0};
    std::atomic<long> zombie{0};
    std::atomic<long> dropped{0};
};
static Metrics M;

// --------------------------------------------------------------- the cache

// Redis, modelled as the only thing about Redis that matters here: a set of
// keys that are present, and the fact that emptying it is instant and
// refilling it is not.
class Cache {
public:
    Cache() {
        for (int k = 0; k < KEYS; ++k) present_.insert(k);
    }
    bool get(int key) {
        std::lock_guard<std::mutex> lk(mu_);
        if (present_.count(key)) { ++hits_; return true; }
        ++misses_;
        return false;
    }
    void put(int key) {
        std::lock_guard<std::mutex> lk(mu_);
        present_.insert(key);
    }
    // One command. Instantaneous. Fully reversible. This is the entire
    // trigger, and ten seconds later it will be completely irrelevant to why
    // the system is down.
    void flushall() {
        std::lock_guard<std::mutex> lk(mu_);
        present_.clear();
    }
    // Ordinary TTL churn, which is what holds the hit rate at 90% instead of
    // letting it climb to 100% and make the experiment lie.
    void evict(int n) {
        std::lock_guard<std::mutex> lk(mu_);
        for (int i = 0; i < n && !present_.empty(); ++i)
            present_.erase(present_.begin());
    }
    void take_rates(long& h, long& m) {
        std::lock_guard<std::mutex> lk(mu_);
        h = hits_; m = misses_;
        hits_ = misses_ = 0;
    }

private:
    std::mutex mu_;
    std::unordered_set<int> present_;
    long hits_ = 0, misses_ = 0;
};
static Cache* g_cache = nullptr;

// ------------------------------------------------------------ retry budget

// Topic 3's token bucket, used here only as escape (b). Milli-tokens in an
// atomic; -1 means "no budget configured", which is the default everywhere.
static std::atomic<long> g_budget{-1};

static bool budget_withdraw() {
    long cur = g_budget.load();
    if (cur < 0) return true;  // no budget: every retry is permitted
    while (cur >= 1000) {
        if (g_budget.compare_exchange_weak(cur, cur - 1000)) return true;
    }
    return false;
}

static void budget_deposit() {
    long cur = g_budget.load();
    if (cur < 0) return;
    while (true) {
        long next = std::min<long>(cur + static_cast<long>(BUDGET_RATIO * 1000), 103000);
        if (g_budget.compare_exchange_weak(cur, next)) return;
        if (cur < 0) return;
    }
}

// ------------------------------------------------------------- the request

struct App;

// One logical client request, which survives across attempts and across an
// app restart -- because the client is not the thing being restarted.
struct ReqState {
    int key;
    int attempt = 0;
};

// One ATTEMPT: the thing that goes in the queue. `settled` is the race
// between the worker finishing it and the client giving up on it, and
// whoever wins that race decides whether the completion was goodput or a
// zombie.
struct Attempt {
    std::shared_ptr<ReqState> req;
    TimePoint deadline;
    TimePoint enqueued;
    std::atomic<bool> settled{false};
    std::shared_ptr<App> app;
    bool miss = false;
};

static void submit(std::shared_ptr<ReqState> req);
static void finish_attempt(std::shared_ptr<ReqState> req, bool ok);

// --------------------------------------------------------------- the timer

// The client side, and the only place in this file that knows a deadline
// exists. In the other five runtimes this is a language feature; here it is
// a thread holding a sorted map, which is what those language features are
// underneath.
class TimerWheel {
public:
    void start() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            // Both lines matter, and the missing first one is a bug worth
            // seeing: `stop()` leaves `stopping_` true, so a second `start()`
            // launched a thread that fell straight out of its own loop and
            // every scenario after the first ran with NO client deadline at
            // all -- infinitely patient callers, no retries, no zombie
            // completions. The tell was in the output: `thruput` at the pool
            // rate instead of the attempt rate.
            stopping_ = false;
            pending_.clear();
        }
        thread_ = std::thread([this] { run(); });
    }
    void stop() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            stopping_ = true;
        }
        cv_.notify_all();
        thread_.join();
    }
    void arm(const std::shared_ptr<Attempt>& a) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            pending_.emplace(a->deadline, a);
        }
        cv_.notify_one();
    }

private:
    void run() {
        std::unique_lock<std::mutex> lk(mu_);
        while (!stopping_) {
            if (pending_.empty()) {
                cv_.wait(lk);
                continue;
            }
            auto next = pending_.begin()->first;
            if (Clock::now() < next) {
                cv_.wait_until(lk, next);
                continue;
            }
            auto a = pending_.begin()->second;
            pending_.erase(pending_.begin());
            lk.unlock();
            // The caller has stopped waiting. Note what does NOT happen here:
            // the item is not removed from the work queue, the connection is
            // not handed back, and the thread that will eventually run it is
            // not told. There is nowhere to put that instruction.
            if (!a->settled.exchange(true)) finish_attempt(a->req, false);
            lk.lock();
        }
    }
    std::mutex mu_;
    std::condition_variable cv_;
    std::multimap<TimePoint, std::shared_ptr<Attempt>> pending_;
    bool stopping_ = false;
    std::thread thread_;
};
static TimerWheel g_timer;

// ----------------------------------------------------------------- the app

// The server process: its queues, its pool, and its threads. Restarting it
// (escape d) means replacing this whole object, which is what a container
// restart does and why the escape is modelled that way rather than by
// zeroing counters.
struct App : std::enable_shared_from_this<App> {
    std::mutex mu;
    std::condition_variable cv;
    std::deque<std::shared_ptr<Attempt>> dbq;    // THE unbounded queue
    std::deque<std::shared_ptr<Attempt>> fastq;  // cache hits
    bool stopping = false;
    std::atomic<long> inflight{0};
    std::atomic<long> in_use{0};
    std::atomic<long> shed_limit{0};
    // The second half of escape (c), and the half the other five runtimes get
    // for free: permission to notice, at dequeue, that an item's caller is
    // already gone and to throw it away instead of running it. This is CoDel's
    // rule -- reject on MEASURED WAIT -- and topic 5 spends a page on it.
    std::atomic<bool> drop_expired{false};
    std::vector<std::thread> threads;

    void start() {
        for (int i = 0; i < POOL_SIZE; ++i)
            threads.emplace_back([this] { worker(true); });
        for (int i = 0; i < FAST_THREADS; ++i)
            threads.emplace_back([this] { worker(false); });
    }

    void stop() {
        {
            std::lock_guard<std::mutex> lk(mu);
            stopping = true;
            dbq.clear();
            fastq.clear();
        }
        cv.notify_all();
        for (auto& t : threads) t.join();
        threads.clear();
    }

    void enqueue(const std::shared_ptr<Attempt>& a) {
        {
            std::lock_guard<std::mutex> lk(mu);
            if (stopping) return;
            (a->miss ? dbq : fastq).push_back(a);
        }
        cv.notify_one();
    }

    // Queue-age instrumentation: the head of the queue is the oldest thing
    // in the system, and its age is the wait every new arrival is about to
    // inherit. Topic 5 sheds on exactly this number.
    void queue_state(long& len, double& oldest_ms) {
        std::lock_guard<std::mutex> lk(mu);
        len = static_cast<long>(dbq.size());
        oldest_ms = dbq.empty() ? 0.0
                                : std::chrono::duration<double, std::milli>(
                                      Clock::now() - dbq.front()->enqueued).count();
    }

    void worker(bool is_db) {
        for (;;) {
            std::shared_ptr<Attempt> a;
            {
                std::unique_lock<std::mutex> lk(mu);
                auto& q = is_db ? dbq : fastq;
                cv.wait(lk, [&] { return stopping || !q.empty(); });
                if (stopping) return;
                a = q.front();
                q.pop_front();
            }
            if (drop_expired.load() && Clock::now() > a->deadline) {
                // Dead on arrival. Nobody is waiting for this. Dropping it
                // costs microseconds; running it would cost a connection for
                // 200ms, which is the entire reason the queue never drains.
                inflight.fetch_sub(1);
                M.dropped.fetch_add(1);
                if (!a->settled.exchange(true)) finish_attempt(a->req, false);
                continue;
            }
            if (is_db) {
                in_use.fetch_add(1);
                std::this_thread::sleep_for(DB_SERVICE);
                in_use.fetch_sub(1);
            } else {
                std::this_thread::sleep_for(CACHE_SERVICE);
            }
            complete(a);
        }
    }

    void complete(const std::shared_ptr<Attempt>& a) {
        inflight.fetch_sub(1);
        bool in_time = Clock::now() <= a->deadline;
        if (in_time && a->miss) {
            // THE SUSTAINING EFFECT, in one `if`. The fill happens after the
            // query returns -- and under overload the caller is already gone
            // by then, so the fill never happens. The cache cannot refill
            // precisely because the database is slow, and the database is
            // slow precisely because the cache is empty.
            g_cache->put(a->req->key);
        }
        if (!a->settled.exchange(true)) {
            finish_attempt(a->req, in_time);
        } else {
            // The client gave up on this a while ago. The pool held a
            // connection for it anyway, start to finish.
            M.zombie.fetch_add(1);
        }
    }
};

static std::mutex g_app_mu;
static std::shared_ptr<App> g_app;

static std::shared_ptr<App> current_app() {
    std::lock_guard<std::mutex> lk(g_app_mu);
    return g_app;
}

static std::shared_ptr<App> new_app() {
    auto app = std::make_shared<App>();
    app->start();
    std::lock_guard<std::mutex> lk(g_app_mu);
    g_app = app;
    return app;
}

// -------------------------------------------------------------- the client

// Topic 3's naive retry client: no jitter, no budget unless escape (b)
// turned one on, and a per-attempt timeout that is comfortable when the
// system is well and hopeless when it is not.
static void finish_attempt(std::shared_ptr<ReqState> req, bool ok) {
    M.thruput.fetch_add(1);
    if (ok) {
        // GOODPUT: a response delivered to a caller that was still waiting
        // for it. Not "requests handled". This is the only number in this
        // file worth alerting on.
        M.goodput.fetch_add(1);
        budget_deposit();
        return;
    }
    if (req->attempt + 1 < ATTEMPTS && budget_withdraw()) {
        req->attempt += 1;
        M.retries.fetch_add(1);
        submit(req);  // additive: a new request to a system already behind
        return;
    }
    M.failed.fetch_add(1);
}

static void submit(std::shared_ptr<ReqState> req) {
    auto app = current_app();
    if (!app) return;
    // Escape (c), and topic 5 in one line: refuse work you have no capacity
    // for, immediately, instead of accepting it and being late.
    long lim = app->shed_limit.load();
    if (lim > 0 && app->inflight.load() >= lim) {
        M.shed.fetch_add(1);
        finish_attempt(req, false);
        return;
    }
    auto a = std::make_shared<Attempt>();
    a->req = req;
    a->enqueued = Clock::now();
    a->deadline = a->enqueued + CLIENT_TIMEOUT;
    a->app = app;
    a->miss = !g_cache->get(req->key);
    app->inflight.fetch_add(1);
    app->enqueue(a);
    g_timer.arm(a);
}

// ------------------------------------------------------------- the harness

struct Row {
    double t, offered, thruput, goodput, hit;
    long pg, inflight, qlen;
    double oldest, retry;
};

// Offered load. Constant everywhere except escape (a), which is the only
// intervention in this file that touches the client side at all.
static double offered_rate(double t, const std::string& escape) {
    if (escape != "a" || t < ESCAPE_AT) return OFFERED_RPS;
    double since = t - ESCAPE_AT;
    if (since < DROP_SECONDS) return 0.0;                       // take it away
    double ramp = (since - DROP_SECONDS) / RAMP_BACK_SECONDS;   // ... let back
    return OFFERED_RPS * std::min(1.0, ramp);                   // SLOWLY
}

static std::vector<Row> run_scenario(const std::string& escape, double& end_out,
                                     long& zombies_out, long& dropped_out) {
    double end_at = escape.empty() ? END_AT : ESCAPE_END_AT;
    end_out = end_at;

    M.goodput = M.thruput = M.retries = M.failed = M.shed = M.zombie = 0;
    M.dropped = 0;
    g_budget = -1;
    Cache cache;
    g_cache = &cache;
    g_timer.start();
    auto app = new_app();

    std::mt19937_64 rng(20250504);
    std::uniform_int_distribution<int> key_dist(0, KEYS - 1);

    auto begin = Clock::now();
    auto last_report = begin;
    auto last_evict = begin;
    auto at = begin;
    long last_g = 0, last_th = 0, last_r = 0;
    bool triggered = false, escaped = false;
    std::vector<Row> rows;

    for (;;) {
        double t_planned = std::chrono::duration<double>(at - begin).count();
        if (t_planned > end_at) break;
        double rate = offered_rate(t_planned, escape);
        if (rate <= 0.0) {
            at += Ms(50);
        } else {
            std::exponential_distribution<double> gap(rate);
            at += std::chrono::duration_cast<Clock::duration>(
                std::chrono::duration<double>(gap(rng)));
        }
        std::this_thread::sleep_until(at);
        auto now = Clock::now();
        double t = std::chrono::duration<double>(now - begin).count();

        if (!triggered && t >= TRIGGER_AT) {
            cache.flushall();
            triggered = true;
        }
        if (!escaped && t >= ESCAPE_AT) {
            escaped = true;
            if (escape == "b") {
                g_budget = 3000;
            } else if (escape == "c") {
                app->shed_limit.store(SHED_LIMIT);
                app->drop_expired.store(true);
            } else if (escape == "d") {
                // "Restart the app containers." The queues, the in-flight
                // work and the pool all go. The cache is external and stays
                // exactly as cold as it was, and the clients never stopped
                // retrying -- their timers are in g_timer, which is client
                // state and survives, which is the entire point of the
                // escape.
                auto old = app;
                app = new_app();
                std::thread([old] { old->stop(); }).detach();
            }
        }

        if (now - last_evict >= std::chrono::seconds(1)) {
            cache.evict(EVICT_PER_SEC);
            last_evict = now;
        }

        if (rate > 0.0) {
            // No backpressure anywhere in that line. The queue accepts
            // whatever arrives, and it will keep accepting it long after
            // accepting it has stopped meaning anything.
            auto req = std::make_shared<ReqState>();
            req->key = key_dist(rng);
            submit(req);
        }

        if (std::chrono::duration<double>(now - last_report).count() >= REPORT_EVERY) {
            double span = std::chrono::duration<double>(now - last_report).count();
            long g = M.goodput.load(), th = M.thruput.load(), r = M.retries.load();
            long hits = 0, misses = 0;
            cache.take_rates(hits, misses);
            long qlen = 0;
            double oldest = 0.0;
            app->queue_state(qlen, oldest);
            Row row;
            row.t = t;
            row.offered = rate;
            row.thruput = (th - last_th) / span;
            row.goodput = (g - last_g) / span;
            row.hit = 100.0 * hits / std::max(1L, hits + misses);
            row.pg = app->in_use.load();
            row.inflight = app->inflight.load();
            row.qlen = qlen;
            row.oldest = oldest;
            row.retry = (r - last_r) / std::max(1.0, static_cast<double>(th - last_th));
            rows.push_back(row);
            last_g = g; last_th = th; last_r = r;
            last_report = now;
        }
    }

    app->stop();
    {
        std::lock_guard<std::mutex> lk(g_app_mu);
        g_app.reset();
    }
    g_timer.stop();
    zombies_out = M.zombie.load();
    dropped_out = M.dropped.load();
    g_cache = nullptr;
    return rows;
}

// -------------------------------------------------------------- reporting

static const char* HEADER =
    "      t   offered   thruput   goodput   hit%   pg  inflight   qlen  oldest_ms  retry/req"
    "   goodput as % of offered";

static void render(const std::string& title, const std::string& note,
                   const std::vector<Row>& rows, double end_at, long zombies,
                   long dropped, double& g_before_out, double& g_after_out) {
    std::printf("\n=== %s ===\n", title.c_str());
    std::printf("    %s\n", note.c_str());
    std::printf("%s\n", HEADER);
    std::printf("%s\n", std::string(std::strlen(HEADER), '-').c_str());
    for (const auto& r : rows) {
        double frac = r.goodput / OFFERED_RPS;
        std::string bar(static_cast<size_t>(std::max(0.0, std::round(24 * std::min(1.0, frac)))), '#');
        const char* mark = "";
        if (std::fabs(r.t - TRIGGER_AT) < REPORT_EVERY / 2) mark = "  <-- FLUSHALL";
        else if (std::fabs(r.t - ESCAPE_AT) < REPORT_EVERY / 2) mark = "  <-- escape applied";
        std::printf("  %5.1f %9.1f %9.1f %9.1f %6.1f %4ld %9ld %6ld %10.0f %10.2f   |%s%s\n",
                    r.t, r.offered, r.thruput, r.goodput, r.hit, r.pg, r.inflight,
                    r.qlen, r.oldest, r.retry, bar.c_str(), mark);
    }
    double gb = 0, ga = 0;
    int nb = 0, na = 0;
    for (const auto& r : rows) {
        if (r.t < TRIGGER_AT) { gb += r.goodput; ++nb; }
        if (r.t >= end_at - 6) { ga += r.goodput; ++na; }
    }
    if (nb) gb /= nb;
    if (na) ga /= na;
    std::printf("    goodput before the trigger %6.1f rps (%.0f%% of offered)   "
                "final 6 seconds %6.1f rps (%.0f%% of offered)   "
                "zombie completions %ld   dropped at dequeue %ld\n",
                gb, 100 * gb / OFFERED_RPS, ga, 100 * ga / OFFERED_RPS, zombies, dropped);
    g_before_out = gb;
    g_after_out = ga;
}

// COMPUTED from the run that just happened, never asserted here. Sufficient
// means "goodput came back", not "the intervention did something
// measurable" -- that distinction is the whole of step 5 in the README.
static std::string verdict(double before, double after) {
    char buf[128];
    if (before <= 1.0) return "baseline never established -- see README";
    double pct = 100 * after / before;
    if (pct >= 70) std::snprintf(buf, sizeof buf, "SUFFICIENT   (recovered to %.0f%% of pre-trigger goodput)", pct);
    else if (pct >= 20) std::snprintf(buf, sizeof buf, "partial      (only %.0f%% of pre-trigger goodput)", pct);
    else std::snprintf(buf, sizeof buf, "not sufficient (%.0f%% of pre-trigger goodput)", pct);
    return std::string(buf);
}

int main() {
    std::printf("Metastable failure: a cache flush that stops mattering long before the outage does.\n");
    std::printf("Offered load is constant at %.0f rps and is never raised. Cache hit rate %.0f%% when warm.\n",
                OFFERED_RPS, 100.0 - 100.0 * EVICT_PER_SEC / OFFERED_RPS);
    double capacity = POOL_SIZE / (DB_SERVICE.count() / 1000.0);
    std::printf("Database capacity is %d/%.3f = %.0f queries per second. Warm, the miss rate needs %d of them (%.0f%% utilised).\n",
                POOL_SIZE, DB_SERVICE.count() / 1000.0, capacity, EVICT_PER_SEC,
                100.0 * EVICT_PER_SEC / capacity);
    std::printf("Cold, it needs all %.0f -- %.0fx capacity, before a single retry. Client timeout %lldms, %d attempts, no jitter, no budget, no shedding.\n",
                OFFERED_RPS, OFFERED_RPS / capacity,
                static_cast<long long>(CLIENT_TIMEOUT.count()), ATTEMPTS);
    std::printf("FLUSHALL at t=%.0fs. Escapes, where a scenario has one, at t=%.0fs.\n",
                TRIGGER_AT, ESCAPE_AT);
    std::printf("Threads: 1 arrival, 1 client timer, %d pool, %d fast path. The queue in front of the pool is unbounded, and nothing here can cancel work.\n",
                POOL_SIZE, FAST_THREADS);

    struct Scenario { const char* title; std::string note; const char* escape; };
    char note_a[192], note_c[128];
    std::snprintf(note_a, sizeof note_a,
                  "The one nobody wants to authorise. %.0fs of zero, then %.0fs of ramp. Watch the ramp, not the drop.",
                  DROP_SECONDS, RAMP_BACK_SECONDS);
    std::snprintf(note_c, sizeof note_c,
                  "Admit at most %ld in flight AND drop queued work whose deadline has passed. "
                  "In C++ you need both.", SHED_LIMIT);
    std::vector<Scenario> scenarios = {
        {"0 no escape: remove the trigger and wait",
         "The trigger was over in a millisecond. Watch the next 24 seconds.", ""},
        {"a drop offered load to zero, then ramp it back slowly", note_a, "a"},
        {"b enable topic 3's 10% retry budget, load unchanged",
         "Removes the amplification. Does not remove the sustaining effect.", "b"},
        {"c enable topic 5's load shedder (both halves), load unchanged", note_c, "c"},
        {"d restart the app, load unchanged",
         "Clears the queues, the in-flight work and the pool. Not the cache.", "d"},
    };

    struct Result { std::string title; double before, after; };
    std::vector<Result> results;
    for (const auto& sc : scenarios) {
        double end_at = 0;
        long zombies = 0, dropped = 0;
        auto rows = run_scenario(sc.escape, end_at, zombies, dropped);
        double before = 0, after = 0;
        render(sc.title, sc.note, rows, end_at, zombies, dropped, before, after);
        results.push_back({sc.title, before, after});
    }

    std::printf("\n%s\n", std::string(78, '=').c_str());
    std::printf("%-52s%15s%11s\n", "scenario", "goodput before", "after");
    std::printf("%s\n", std::string(78, '-').c_str());
    for (const auto& r : results)
        std::printf("%-52s%14.1f%11.1f\n", r.title.c_str(), r.before, r.after);

    std::printf("\nScenario 0 is the whole topic. The trigger -- one FLUSHALL -- was over\n");
    std::printf("instantly and reversibly, offered load never changed by a single request,\n");
    std::printf("and goodput half a minute later is %.1f rps -- which is what THIS run\n",
                results[0].after);
    std::printf("measured, not a sentence written before it. If it is not near zero, read\n");
    std::printf("the README's 'what would mean the experiment is broken' before reading\n");
    std::printf("anything else. Nothing is broken. Nothing needs rolling back. The system\n");
    std::printf("has settled into a second stable state, where the cache cannot refill\n");
    std::printf("because the database is saturated and the database is saturated because\n");
    std::printf("the cache is empty.\n");
    std::printf("\nEscapes, judged against THIS run rather than against a story:\n");
    for (size_t i = 1; i < results.size(); ++i)
        std::printf("  %c  %s\n", results[i].title[0], verdict(results[i].before, results[i].after).c_str());
    std::printf("  (scenario 0 finished at %.1f rps of goodput, for comparison)\n", results[0].after);
    std::printf("\nWhat each escape actually touches, which is why they do not rank the way\n");
    std::printf("intuition ranks them:\n");
    std::printf("  (a) drop and ramp    removes load, not the loop. The drop always works;\n");
    std::printf("      the RAMP is the experiment. Full load returning to a cache that is\n");
    std::printf("      still empty walks straight back into the same state, so \"let it back\n");
    std::printf("      slowly\" is a QUANTITATIVE claim -- the ramp has to be slower than the\n");
    std::printf("      cache can refill, which here is %.0f keys per second against %d keys.\n",
                capacity, KEYS);
    std::printf("      Raise RAMP_BACK_SECONDS from %.0f and find the threshold yourself.\n",
                RAMP_BACK_SECONDS);
    std::printf("  (b) retry budget     removes topic 3's amplification and leaves the\n");
    std::printf("      sustaining effect untouched. \"We turned the retries off\" is a sentence\n");
    std::printf("      people say in incidents that are still ongoing twenty minutes later.\n");
    std::printf("  (c) load shedding    is the only one that breaks the FEEDBACK LOOP: it is\n");
    std::printf("      the only intervention that lets the ADMITTED requests finish inside\n");
    std::printf("      their deadline, which is the exact condition the cache needs to\n");
    std::printf("      refill. Watch hit%% climb while oldest_ms collapses -- that is the\n");
    std::printf("      loop running backwards, and in C++ you can see it in the queue.\n");
    std::printf("  (d) restart the app  throws away the queue, the pool and the threads --\n");
    std::printf("      every bit of state the process owns -- and keeps none of what the\n");
    std::printf("      clients own. The amplifier is in the clients. They did not restart.\n");
    std::printf("\nIn HotOS '25 vocabulary, worth writing down for your own system before\n");
    std::printf("you need it:\n");
    std::printf("  trigger                 a cache flush, over in one millisecond\n");
    std::printf("  amplification mechanism naive retries, plus the miss rate going from 10%%\n");
    std::printf("                          to 100%% on a database that was 60%% utilised\n");
    std::printf("  sustaining effect       fills only happen on completions that beat the\n");
    std::printf("                          caller's deadline, and under overload none do --\n");
    std::printf("                          and here the queue keeps the dead work, because\n");
    std::printf("                          C++ has nowhere to put a cancellation\n");
    return 0;
}
