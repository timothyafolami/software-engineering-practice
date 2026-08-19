// Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one C++
// process.
//
// You cannot serve more than capacity. The only choice you have is whether
// the excess is rejected in one millisecond or times out after thirty seconds
// having consumed a connection, a thread and a query. This file runs the same
// ramp seven times and changes only the admission decision.
//
// C++ IS THE VERSION WHERE YOU SEE WHAT THE KERNEL WAS DOING ALL ALONG.
// `listen(fd, backlog)` is the first queue in every server anybody has ever
// written, and shedding there means the kernel refuses the connection or
// drops the SYN outright -- the cheapest possible rejection, and the only one
// that costs the server literally nothing. It is also invisible: nothing in
// your process ever hears about it. Every queue above it is one you wrote.
//
// So this file writes them, and because it wrote them it can do the thing no
// framework here will do for you: TIMESTAMP ON ENQUEUE AND REJECT ON MEASURED
// WAIT. That is CoDel, imported from network queue management, and it is the
// right signal for a reason worth stating precisely -- queue LENGTH tells you
// how many items are waiting and nothing about how long they take, so the
// same length is a healthy queue for a 1ms handler and a catastrophe for a
// 500ms one. Wait TIME is the thing your caller actually experiences.
//
// The whole CoDel half is about forty lines: `enqueued` on the item, a target
// wait, a check at admission, and a second check at dequeue for items that
// aged out while queued. `drop@deq` in the output counts the second one, and
// it is work the server would otherwise have done for callers who are gone.
//
// WHAT THIS DEMONSTRATES
//
//   A backend with 8 worker threads at 40ms each -- 200 requests/second of
//   capacity, measured the way topic 1 measures it -- behind six different
//   admission policies, at 80% and 130% of that capacity.
//
//     none rho=0.8      the healthy baseline. Everything looks fine.
//     none rho=1.3      an UNBOUNDED std::deque, which is the default shape
//                       of every hand-rolled thread pool ever written.
//     static rho=1.3    an in-flight limit of SHED_LIMIT plus a 50ms wait
//                       target, enforced at BOTH ends of the queue.
//     priority rho=1.3  the same limit, but /checkout (tier 0) may use all
//                       of it and /search (tier 3) may not.
//     adaptive rho=1.3  no configured number at all: a gradient controller
//                       infers the limit from latency. Service time triples
//                       half way through, on purpose.
//     bulkhead          one pool of 8 shared between checkout and a slow
//                       /report endpoint, then the SAME EIGHT split 6 + 2 --
//                       two pools, two queues, two sets of threads.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `p99_acc` and `goodput` in `none rho=1.3` against `static rho=1.3`.
//      Rejecting work should INCREASE the number of requests answered in
//      time. Check that rather than believe it.
//   2. `qlen` and `oldest` in scenario 2 -- the queue you did not bound, and
//      the age of the request at its head.
//   3. `tier0%` in the priority row.
//   4. `limit` in the adaptive row, before and after service time triples at
//      t=6s. Reason about Little's law before calling the controller broken:
//      the ideal in-flight limit for 8 servers is about 8 however long each
//      request takes. What must fall is the RATE, not the limit.
//   5. `reject_ms`, the cost of saying no.
//
// RUN
//   c++ -O2 -std=c++17 -pthread -o /tmp/shedder shedder.cpp && /tmp/shedder
//
// Roughly two and a half minutes: seven scenarios of twenty seconds.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;
using Ms = std::chrono::milliseconds;

static double ms_between(TimePoint a, TimePoint b) {
    return std::chrono::duration<double, std::milli>(b - a).count();
}

// ---------------------------------------------------------------- config
//
// Identical to python/shedder.py's constants: the six languages differ in how
// admission is expressed, not in what is being measured.

constexpr int WORKERS = 8;              // the real resource: 8 worker threads
constexpr auto SERVICE = Ms(40);        // 8 / 0.040 = 200 rps of capacity
constexpr double CAPACITY = WORKERS / 0.040;

constexpr double RHO_LOW = 0.8;
constexpr double RHO_HIGH = 1.3;

constexpr auto SLO = Ms(500);           // later than this is not goodput
// PERTURB_AT_S + MIN_RTT_RESET_S + room to watch the adaptive limit come
// back. At 12s the run ended during the dip and the return -- the half that
// shows the reset working -- was invisible.
constexpr double DURATION_S = 20.0;
constexpr double REPORT_EVERY = 2.0;

constexpr long SHED_LIMIT = 12;         // the knee's concurrency, measured
constexpr auto SHED_WAIT = Ms(50);      // CoDel's target wait
constexpr long TIER3_LIMIT = 10;        // tier 3 may not use the last two
constexpr double TIER0_SHARE = 0.20;

constexpr double ADAPT_MIN = 2.0;
constexpr double ADAPT_MAX = 64.0;
constexpr double ADAPT_START = 10.0;
constexpr auto ADAPT_WINDOW = Ms(250);
constexpr double ADAPT_SMOOTHING = 0.2;
constexpr auto MIN_RTT_RESET = std::chrono::seconds(5);
constexpr double PERTURB_AT = 6.0;
constexpr int PERTURB_FACTOR = 3;

constexpr double CHECKOUT_RPS = 120.0;
constexpr double REPORT_RPS = 6.0;
constexpr auto REPORT_SERVICE = Ms(800);  // 6 rps x 0.8s = 4.8 workers
constexpr int BULK_CHECKOUT = 6;          // the same 8, split. Nothing added.
constexpr int BULK_REPORT = 2;

// ---------------------------------------------------------------- metrics

struct Metrics {
    std::mutex mu;
    long offered = 0, accepted = 0, rejected = 0, goodput = 0;
    long tier0_offered = 0, tier0_goodput = 0;
    long dropped_at_dequeue = 0;
    std::vector<double> latencies, lat_tier0, reject_cost;
    long w_offered = 0, w_accepted = 0, w_rejected = 0, w_goodput = 0;
    std::vector<double> w_lat;
};

static double percentile(std::vector<double> v, double q) {
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    long idx = static_cast<long>(std::ceil(q * v.size())) - 1;
    if (idx < 0) idx = 0;
    if (idx >= static_cast<long>(v.size())) idx = v.size() - 1;
    return v[idx];
}

// ------------------------------------------------------ the gradient limit

// Netflix `concurrency-limits` in miniature, borrowed from TCP congestion
// control rather than from queueing theory: sample latency continuously,
// remember the minimum you have seen, raise the in-flight limit while current
// latency stays near that minimum, lower it when latency climbs. You never
// configure a number; the system discovers it, and rediscovers it when your
// code changes -- which matters because the hand-measured number from topic 1
// goes stale the day someone adds a join.
//
// The non-obvious parameter is the min-RTT RESET. Without it one fast sample
// from a quiet moment is remembered forever, so after a genuine permanent
// slowdown the gradient sticks near zero and the limit collapses to the floor
// and stays there. Vegas-style controllers all re-baseline.
class GradientLimit {
public:
    double limit() {
        std::lock_guard<std::mutex> lk(mu_);
        return limit_;
    }
    void observe(double rtt_ms) {
        std::lock_guard<std::mutex> lk(mu_);
        samples_.push_back(rtt_ms);
    }
    void update(TimePoint now) {
        std::lock_guard<std::mutex> lk(mu_);
        if (have_update_ && now - last_update_ < ADAPT_WINDOW) return;
        last_update_ = now;
        have_update_ = true;
        if (samples_.empty()) return;
        std::sort(samples_.begin(), samples_.end());
        double window_min = samples_.front();
        double median = samples_[samples_.size() / 2];
        samples_.clear();

        if (!have_reset_ || now - last_reset_ >= MIN_RTT_RESET) {
            min_rtt_ = window_min;
            last_reset_ = now;
            have_reset_ = true;
        } else {
            min_rtt_ = std::min(min_rtt_, window_min);
        }
        // gradient < 1 means "we are queueing"; the limit comes down in
        // proportion. The sqrt term is the queue you are willing to keep, and
        // is what stops the limit collapsing to 1 the moment one request is
        // slow.
        double gradient = std::max(0.5, std::min(1.0, min_rtt_ / std::max(median, 1e-6)));
        double target = limit_ * gradient + std::sqrt(limit_);
        limit_ = std::max(ADAPT_MIN, std::min(ADAPT_MAX,
                                              limit_ * (1 - ADAPT_SMOOTHING)
                                              + ADAPT_SMOOTHING * target));
    }

private:
    std::mutex mu_;
    double limit_ = ADAPT_START;
    double min_rtt_ = 1e9;
    std::vector<double> samples_;
    TimePoint last_update_{}, last_reset_{};
    bool have_update_ = false, have_reset_ = false;
};

// ------------------------------------------------------------- the request

struct Item {
    int tier;
    bool is_report;
    TimePoint arrived;   // when the client asked
    TimePoint enqueued;  // when it entered THIS queue. CoDel needs this.
    Ms service;
};

// ---------------------------------------------------------------- the pool

// A hand-rolled bounded pool: a queue you wrote, threads you started, and
// therefore a wait you can measure. `drop_expired` is the second half of
// CoDel -- an item that aged past the target while queued is thrown away at
// dequeue rather than served, because its caller is probably gone and
// serving it is strictly wasted work.
class Pool {
public:
    Pool(int workers, Metrics* m, std::atomic<long>* inflight, GradientLimit* limiter,
         bool drop_expired)
        : m_(m), inflight_(inflight), limiter_(limiter), drop_expired_(drop_expired) {
        for (int i = 0; i < workers; ++i) threads_.emplace_back([this] { worker(); });
    }

    ~Pool() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            stopping_ = true;
        }
        cv_.notify_all();
        for (auto& t : threads_) t.join();
    }

    void submit(const Item& it) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            q_.push_back(it);
        }
        cv_.notify_one();
    }

    void state(long& len, double& oldest_ms, long& busy) {
        std::lock_guard<std::mutex> lk(mu_);
        len = static_cast<long>(q_.size());
        oldest_ms = q_.empty() ? 0.0 : ms_between(q_.front().enqueued, Clock::now());
        busy = busy_;
    }

private:
    void worker() {
        for (;;) {
            Item it;
            {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait(lk, [this] { return stopping_ || !q_.empty(); });
                if (stopping_) return;
                it = q_.front();
                q_.pop_front();
                ++busy_;
            }
            double waited = ms_between(it.enqueued, Clock::now());
            if (drop_expired_ && waited > SHED_WAIT.count()) {
                // CoDel, second half. This item waited longer than the target
                // while sitting in the queue, so its caller has very likely
                // stopped waiting. Serving it now costs a full service time
                // and produces nothing.
                std::lock_guard<std::mutex> lk(m_->mu);
                {
                    std::lock_guard<std::mutex> lk2(mu_);
                    --busy_;
                }
                inflight_->fetch_sub(1);
                m_->rejected += 1;
                m_->w_rejected += 1;
                m_->dropped_at_dequeue += 1;
                m_->accepted -= 1;
                m_->w_accepted -= 1;
                m_->reject_cost.push_back(waited);
                continue;
            }
            std::this_thread::sleep_for(it.service);
            {
                std::lock_guard<std::mutex> lk(mu_);
                --busy_;
            }
            inflight_->fetch_sub(1);
            double latency = ms_between(it.arrived, Clock::now());
            if (limiter_) limiter_->observe(latency);
            std::lock_guard<std::mutex> lk(m_->mu);
            m_->latencies.push_back(latency);
            m_->w_lat.push_back(latency);
            if (it.tier == 0) m_->lat_tier0.push_back(latency);
            if (latency <= SLO.count()) {
                m_->goodput += 1;
                m_->w_goodput += 1;
                if (it.tier == 0) m_->tier0_goodput += 1;
            }
        }
    }

    std::mutex mu_;
    std::condition_variable cv_;
    std::deque<Item> q_;                 // THE queue. Bound it or do not.
    std::vector<std::thread> threads_;
    long busy_ = 0;
    bool stopping_ = false;
    Metrics* m_;
    std::atomic<long>* inflight_;
    GradientLimit* limiter_;
    bool drop_expired_;
};

// ------------------------------------------------------------- the server

struct Server {
    std::string mode;
    Metrics* m;
    std::atomic<long> inflight{0};
    std::unique_ptr<GradientLimit> limiter;
    std::unique_ptr<Pool> checkout;
    std::unique_ptr<Pool> report;   // non-null only when the pools are split
    std::atomic<long> service_ms{SERVICE.count()};

    Server(const std::string& mode_, Metrics* m_) : mode(mode_), m(m_) {
        if (mode == "adaptive") limiter = std::make_unique<GradientLimit>();
        bool drop = (mode == "static" || mode == "priority" || mode == "adaptive");
        int checkout_workers = (mode == "bulkhead_split") ? BULK_CHECKOUT : WORKERS;
        checkout = std::make_unique<Pool>(checkout_workers, m, &inflight, limiter.get(), drop);
        if (mode == "bulkhead_split") {
            // The bulkhead: its own queue, its own threads. /report is now
            // structurally incapable of touching checkout's workers -- not
            // because it is well behaved, but because it cannot reach them.
            report = std::make_unique<Pool>(BULK_REPORT, m, &inflight, limiter.get(), drop);
        }
    }

    // Admission. The interesting part is what happens when there is no room,
    // and there are exactly three honest answers: refuse now (priority's tier
    // 3, adaptive), refuse based on how long the queue has been making people
    // wait (static -- CoDel's first half), or accept everything and let the
    // queue grow (mode `none`, which is what you ship when you do not decide).
    bool admit(int tier, double& cost_ms) {
        auto t0 = Clock::now();
        cost_ms = 0.0;

        if (mode == "none" || mode.rfind("bulkhead", 0) == 0) {
            inflight.fetch_add(1);
            return true;
        }
        if (mode == "adaptive") {
            if (static_cast<double>(inflight.load()) >= limiter->limit()) {
                cost_ms = ms_between(t0, Clock::now());
                return false;
            }
            inflight.fetch_add(1);
            return true;
        }
        long limit = SHED_LIMIT;
        if (mode == "priority" && tier > 0) limit = TIER3_LIMIT;
        if (inflight.load() >= limit) {
            cost_ms = ms_between(t0, Clock::now());
            return false;
        }
        // CoDel's first half: if the queue is ALREADY making people wait
        // longer than target, refuse rather than join it. Note this is a
        // measured wait, not a length.
        long qlen = 0, busy = 0;
        double oldest = 0.0;
        checkout->state(qlen, oldest, busy);
        if (oldest > SHED_WAIT.count()) {
            cost_ms = ms_between(t0, Clock::now());
            return false;
        }
        inflight.fetch_add(1);
        cost_ms = ms_between(t0, Clock::now());
        return true;
    }

    void handle(int tier, bool is_report) {
        auto arrived = Clock::now();
        {
            std::lock_guard<std::mutex> lk(m->mu);
            m->offered += 1;
            m->w_offered += 1;
            if (tier == 0) m->tier0_offered += 1;
        }
        double cost = 0.0;
        if (!admit(tier, cost)) {
            std::lock_guard<std::mutex> lk(m->mu);
            m->rejected += 1;
            m->w_rejected += 1;
            m->reject_cost.push_back(cost);
            // A 503 with Retry-After, having touched nothing. That is the
            // entire product.
            return;
        }
        {
            std::lock_guard<std::mutex> lk(m->mu);
            m->accepted += 1;
            m->w_accepted += 1;
        }
        Item it;
        it.tier = tier;
        it.is_report = is_report;
        it.arrived = arrived;
        it.enqueued = Clock::now();
        it.service = is_report ? REPORT_SERVICE : Ms(service_ms.load());
        if (is_report && report) report->submit(it);
        else checkout->submit(it);
    }
};

// ------------------------------------------------------------- the harness

struct Row {
    double t, offered, accepted, reject, goodput, p99;
    long inflight;
    double limit;
    long busy, qlen;
    double oldest;
};

struct Scenario {
    std::string key, mode, label, note;
    double rate, tier0_share, report_rps;
};

struct Summary {
    std::string key, label;
    double offered, accepted, rejected, goodput, p99, p99_t0, tier0, reject_ms;
    long dropped;
};

static std::vector<Row> run_scenario(const Scenario& sc, Metrics& m) {
    Server server(sc.mode, &m);
    std::mt19937_64 rng(20250505);
    std::uniform_real_distribution<double> unit(0.0, 1.0);
    std::exponential_distribution<double> gap(sc.rate);
    std::exponential_distribution<double> report_gap(sc.report_rps > 0 ? sc.report_rps : 1.0);

    auto begin = Clock::now();
    auto last_report = begin;
    auto at = begin;
    auto next_report = begin;
    bool perturbed = false;
    std::vector<Row> rows;

    for (;;) {
        double t_planned = std::chrono::duration<double>(at - begin).count();
        if (t_planned > DURATION_S) break;
        at += std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(gap(rng)));
        std::this_thread::sleep_until(at);
        auto now = Clock::now();
        double t = std::chrono::duration<double>(now - begin).count();

        if (sc.mode == "adaptive" && !perturbed && t >= PERTURB_AT) {
            // "Then change service time by 3x at runtime and watch it
            // re-converge." Nobody redeployed. Nobody changed the limit.
            server.service_ms.store(SERVICE.count() * PERTURB_FACTOR);
            perturbed = true;
        }

        int tier = unit(rng) < sc.tier0_share ? 0 : 3;
        server.handle(tier, false);

        // The slow endpoint, offered as its own open-model stream rather than
        // as a fraction of checkout: reports do not arrive because checkouts
        // do.
        // Note `+=` and the `while`, not `= now +` and an `if`: this is an
        // ABSOLUTE schedule, exactly like `at` above. Rescheduling from `now`
        // throws away the lateness of every arrival, and since the check only
        // runs when a checkout arrives, the lateness is real and it grows with
        // load -- so the relative version quietly offers LESS /report the more
        // overloaded the server gets, which is backwards and hides the very
        // effect this scenario exists to show.
        while (sc.report_rps > 0 && now >= next_report) {
            next_report += std::chrono::duration_cast<Clock::duration>(
                               std::chrono::duration<double>(report_gap(rng)));
            server.handle(3, true);
        }

        if (server.limiter) server.limiter->update(now);

        if (std::chrono::duration<double>(now - last_report).count() >= REPORT_EVERY) {
            double span = std::chrono::duration<double>(now - last_report).count();
            long qlen = 0, busy = 0;
            double oldest = 0.0;
            server.checkout->state(qlen, oldest, busy);
            std::lock_guard<std::mutex> lk(m.mu);
            Row r;
            r.t = t;
            r.offered = sc.rate;
            r.accepted = m.w_accepted / span;
            r.reject = 100.0 * m.w_rejected / std::max(1L, m.w_offered);
            r.goodput = m.w_goodput / span;
            r.p99 = percentile(m.w_lat, 0.99);
            r.inflight = server.inflight.load();
            r.limit = server.limiter ? server.limiter->limit() : static_cast<double>(SHED_LIMIT);
            r.busy = busy;
            r.qlen = qlen;
            r.oldest = oldest;
            rows.push_back(r);
            m.w_offered = m.w_accepted = m.w_rejected = m.w_goodput = 0;
            m.w_lat.clear();
            last_report = now;
        }
    }

    // Let the tail drain: requests still in flight at the end of the window
    // are neither goodput nor rejections, and counting them either way would
    // be a lie about the run.
    std::this_thread::sleep_for(std::chrono::seconds(1));
    return rows;
}

// -------------------------------------------------------------- reporting

static const char* HEADER =
    "      t   offered  accepted  reject%   goodput  p99_acc  inflight  limit   busy   qlen  oldest";

static Summary render(const Scenario& sc, const std::vector<Row>& rows, Metrics& m) {
    std::printf("\n=== %s ===\n", sc.label.c_str());
    std::printf("    %s\n", sc.note.c_str());
    std::printf("%s\n", HEADER);
    std::printf("%s\n", std::string(std::strlen(HEADER), '-').c_str());
    for (const auto& r : rows) {
        const char* mark = (sc.mode == "adaptive" && std::fabs(r.t - PERTURB_AT) < REPORT_EVERY / 2)
                               ? "  <-- service time x3" : "";
        std::printf("  %5.1f %9.1f %9.1f %8.0f %9.1f %8.0f %9ld %6.1f %6ld %6ld %7.0f%s\n",
                    r.t, r.offered, r.accepted, r.reject, r.goodput, r.p99, r.inflight,
                    r.limit, r.busy, r.qlen, r.oldest, mark);
    }
    std::lock_guard<std::mutex> lk(m.mu);
    double reject_ms = 0.0;
    for (double c : m.reject_cost) reject_ms += c;
    if (!m.reject_cost.empty()) reject_ms /= m.reject_cost.size();
    Summary s;
    s.key = sc.key;
    s.label = sc.label;
    s.offered = m.offered / DURATION_S;
    s.accepted = m.accepted / DURATION_S;
    s.rejected = 100.0 * m.rejected / std::max(1L, m.offered);
    s.goodput = m.goodput / DURATION_S;
    s.p99 = percentile(m.latencies, 0.99);
    s.p99_t0 = percentile(m.lat_tier0, 0.99);
    s.tier0 = 100.0 * m.tier0_goodput / std::max(1L, m.tier0_offered);
    s.reject_ms = reject_ms;
    s.dropped = m.dropped_at_dequeue;
    std::printf("mode=%s  offered=%.0f  accepted=%.0f  rejected=%.0f%%  goodput=%.0f  "
                "p99_accepted=%.0fms  tier0_success=%.0f%%  p99_tier0=%.0fms  reject_ms=%.1f  "
                "drop@deq=%ld\n",
                s.key.c_str(), s.offered, s.accepted, s.rejected, s.goodput, s.p99, s.tier0,
                s.p99_t0, s.reject_ms, s.dropped);
    return s;
}

int main() {
    std::printf("Load shedding, backpressure and bulkheads: the same ramp, seven admission policies.\n");
    std::printf("Backend capacity is %d/%.3f = %.0f rps, measured the way topic 1 measures it. "
                "Anything above that is not servable by anybody.\n",
                WORKERS, SERVICE.count() / 1000.0, CAPACITY);
    std::printf("Offered load is %.1fx and %.1fx that number. Goodput counts responses inside a "
                "%lldms SLO; p99_acc is the p99 of ACCEPTED requests, p99_tier0 the p99 of tier-0 "
                "(/checkout) requests alone.\n",
                RHO_LOW, RHO_HIGH, static_cast<long long>(SLO.count()));
    std::printf("The static limit is %ld in flight with a %lldms CoDel wait target, checked at "
                "admission AND at dequeue. The adaptive one is not configured at all.\n",
                SHED_LIMIT, static_cast<long long>(SHED_WAIT.count()));

    char note3[192], note4[192], note5[192], note6[192];
    std::snprintf(note3, sizeof note3,
                  "In-flight limit %ld; anything that waits past %lldms is refused, at either end "
                  "of the queue.", SHED_LIMIT, static_cast<long long>(SHED_WAIT.count()));
    std::snprintf(note4, sizeof note4,
                  "/checkout is tier 0 (%.0f%% of traffic) and may use all %ld; /search is tier 3 "
                  "and may use %ld.", TIER0_SHARE * 100, SHED_LIMIT, TIER3_LIMIT);
    std::snprintf(note5, sizeof note5,
                  "No configured limit. Service time triples at t=%.0fs with nobody redeploying "
                  "anything.", PERTURB_AT);
    std::snprintf(note6, sizeof note6,
                  "%.0f rps of checkout plus %.0f rps of %lldms /report, all %d workers and ONE "
                  "queue.", CHECKOUT_RPS, REPORT_RPS, static_cast<long long>(REPORT_SERVICE.count()),
                  WORKERS);

    char label7[96];
    std::snprintf(label7, sizeof label7, "7 bulkhead: the same 8, split %d + %d",
                  BULK_CHECKOUT, BULK_REPORT);

    std::vector<Scenario> scenarios = {
        {"none_0.8", "none", "1 none, rho=0.8",
         "The healthy baseline. Nothing is rejected because nothing needs to be.",
         RHO_LOW * CAPACITY, TIER0_SHARE, 0},
        {"none_1.3", "none", "2 none, rho=1.3",
         "An unbounded deque at 130% of capacity. Watch qlen and oldest while reject% stays at zero.",
         RHO_HIGH * CAPACITY, TIER0_SHARE, 0},
        {"static_1.3", "static", "3 static shedding, rho=1.3", note3, RHO_HIGH * CAPACITY, TIER0_SHARE, 0},
        {"priority_1.3", "priority", "4 priority shedding, rho=1.3", note4, RHO_HIGH * CAPACITY, TIER0_SHARE, 0},
        {"adaptive_1.3", "adaptive", "5 adaptive shedding, rho=1.3", note5, RHO_HIGH * CAPACITY, TIER0_SHARE, 0},
        {"bulk_shared", "bulkhead_shared", "6 bulkhead: one shared pool", note6, CHECKOUT_RPS, 1.0, REPORT_RPS},
        {"bulk_split", "bulkhead_split", label7,
         "Nothing is added. Two queues, two sets of threads, the same eight threads in total.",
         CHECKOUT_RPS, 1.0, REPORT_RPS},
    };

    std::vector<Summary> summaries;
    for (const auto& sc : scenarios) {
        Metrics m;
        auto rows = run_scenario(sc, m);
        summaries.push_back(render(sc, rows, m));
    }

    std::printf("\n%s\n", std::string(112, '=').c_str());
    std::printf("%-38s%8s%9s%8s%8s%8s%9s%10s%10s%8s\n", "mode", "offered", "accepted", "goodput",
                "p99_acc", "p99_t0", "reject%", "tier0_ok%", "reject_ms", "drop@dq");
    std::printf("%s\n", std::string(112, '-').c_str());
    for (const auto& s : summaries) {
        std::printf("%-38s%8.0f%9.0f%8.0f%8.0f%8.0f%9.0f%10.0f%10.1f%8ld\n", s.label.c_str(),
                    s.offered, s.accepted, s.goodput, s.p99, s.p99_t0, s.rejected, s.tier0,
                    s.reject_ms, s.dropped);
    }

    auto find = [&](const std::string& key) -> const Summary& {
        for (const auto& s : summaries)
            if (s.key == key) return s;
        return summaries.front();
    };
    const Summary& none13 = find("none_1.3");
    const Summary& static13 = find("static_1.3");
    const Summary& shared = find("bulk_shared");
    const Summary& split = find("bulk_split");

    std::printf("\nRead rows 2 and 3 as one comparison and everything else is commentary:\n");
    std::printf("  none     rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%\n",
                none13.goodput, none13.p99, none13.rejected);
    std::printf("  static   rho=1.3   goodput %6.0f rps   p99 %6.0f ms   rejected %.0f%%\n",
                static13.goodput, static13.p99, static13.rejected);
    std::printf("Same offered load, same backend, same 200 rps of capacity. The only\n");
    std::printf("difference is that one of them said no.\n");
    std::printf("\nThe bulkhead pair is the other comparison worth making, and it is the one\n");
    std::printf("that adds nothing at all:\n");
    std::printf("  shared pool   checkout goodput %6.0f rps   checkout p99 %6.0f ms\n",
                shared.goodput, shared.p99_t0);
    std::printf("  split %d + %d   checkout goodput %6.0f rps   checkout p99 %6.0f ms\n",
                BULK_CHECKOUT, BULK_REPORT, split.goodput, split.p99_t0);
    std::printf("The split pool has FEWER threads available to checkout, and the boundary is\n");
    std::printf("worth more than the two threads it costs -- because /report at %.0f rps x %lldms\n",
                REPORT_RPS, static_cast<long long>(REPORT_SERVICE.count()));
    std::printf("wants %.1f workers' worth of the shared pool and takes them from whoever asks\n",
                REPORT_RPS * REPORT_SERVICE.count() / 1000.0);
    std::printf("last. Note what it costs: /report itself can now only ever get %.1f rps through.\n",
                BULK_REPORT / (REPORT_SERVICE.count() / 1000.0));
    std::printf("That is the bargain, and you should be able to say it out loud before you make it.\n");
    std::printf("\nThree things to carry out of this file:\n");
    std::printf("  1. An unbounded queue does not smooth load. It converts an availability\n");
    std::printf("     problem into a latency problem and hides it until latency exceeds every\n");
    std::printf("     timeout in the system at once.\n");
    std::printf("  2. Shed on WAIT TIME, not on queue length -- and check it at BOTH ends. An\n");
    std::printf("     item can pass admission and still rot in the queue, which is what\n");
    std::printf("     drop@dq counts.\n");
    std::printf("  3. In C++ specifically: every queue here is one you wrote, including the one\n");
    std::printf("     listen(2) is holding for you below main(). Bounding them is not advanced\n");
    std::printf("     work -- it is forty lines and a timestamp per item.\n");
    return 0;
}
