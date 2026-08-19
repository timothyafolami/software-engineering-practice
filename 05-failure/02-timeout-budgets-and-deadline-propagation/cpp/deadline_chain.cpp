// Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
// C++ process.
//
// C++ has no cancellation story whatsoever, which makes it the honest
// baseline for this topic. A deadline is an absolute
// std::chrono::steady_clock::time_point that you pass by value; enforcement
// is SO_RCVTIMEO/SO_SNDTIMEO or a poll() with a computed remaining-millis
// argument; and a thread that has entered a blocking call stays there until
// the kernel returns, no matter who has stopped caring about the answer.
//
// Everything the other runtimes in this topic hand you -- cancellation
// trees, ambient contexts, drop semantics, AbortSignal -- is code you write
// here. Writing it once is the fastest way to understand what those runtimes
// are actually doing, and it makes one thing unmissable that the others hide:
// the gateway returning 504 at 500ms and the work stopping are two completely
// separate events, and nothing in any language makes the second follow from
// the first. The other runtimes let you cancel a WAIT. None of them cancel
// the work.
//
// So the chain here runs inline on one thread per request, which is the
// ordinary shape of a thread-per-connection C++ server, and the gateway's
// verdict is computed from when the answer actually arrived. A request whose
// answer arrives at 3s got a 504 at 500ms; the thread was busy for all 3s
// regardless. That gap is the topic.
//
// WHAT THIS DEMONSTRATES
//
//   gateway -> service_b -> service_c, C holds a pooled connection for a
//   controlled service time, gateway budget 500ms.
//
//     1 healthy            everything succeeds; the bug is invisible
//     2 naive              each hop uses the same 500ms constant, so B and
//                          C never learn what is left of the budget
//     3 deadline by value  the absolute time_point is an argument on every
//                          signature; B and C refuse work that cannot
//                          finish and hand a connection straight back when
//                          the request behind it is already dead
//     4 + bounded wait     the blocking wait itself takes a computed
//                          remaining-millis, the way poll() does
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `zombie/s` -- work C finished after the gateway had already given up.
//   2. `C pool in use` pinned at the pool size in row 2: topic 1's L, spent
//      entirely on requests nobody is waiting for.
//   3. `threads peak` is the column the other languages cannot show you.
//      Every one of those threads is a stack, a scheduler entry, and a
//      request that has not finished. Row 2 needs several times as many as
//      row 4 to serve the same offered load, and none of the extra ones are
//      doing anything useful.
//   4. Row 4 is where the pool comes back down, because it is the only row
//      where the WORK is bounded rather than the waiting.
//
// The load generator is OPEN MODEL: Poisson arrivals, dispatched on time
// regardless of how the system under test is coping.
//
// RUN
//   c++ -O2 -std=c++17 -pthread -o /tmp/deadline_chain deadline_chain.cpp && /tmp/deadline_chain

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;
using Ms = std::chrono::milliseconds;

// ------------------------------------------------------------------ config

constexpr auto GATEWAY_BUDGET = Ms(500);
constexpr auto SLACK = Ms(20);
constexpr auto HOP_OVERHEAD = Ms(5);
constexpr auto C_SERVICE_FAST = Ms(40);
constexpr auto C_SERVICE_SLOW = Ms(800);
constexpr double SLOW_FRACTION = 0.25;
constexpr int C_POOL_SIZE = 8;
constexpr double RATE = 50.0;
constexpr auto DURATION = std::chrono::seconds(12);
constexpr auto GAUGE_EVERY = Ms(20);

// A deadline that may not exist. There is no std::optional<time_point> in
// C++14 and no ambient carrier in any version, so this is what "the caller
// told us how long it has" looks like when you write it out by hand.
struct Deadline {
    bool present = false;
    TimePoint at{};

    Ms remaining() const {
        return std::chrono::duration_cast<Ms>(at - Clock::now());
    }
    bool expiredWithin(Ms margin) const {
        return present && remaining() < margin;
    }
};

// ----------------------------------------------------------------- metrics

struct Metrics {
    std::atomic<long> ok{0}, failed{0}, zombie{0}, killed{0}, abandoned{0};
    std::atomic<long> inUse{0}, live{0}, peakLive{0};
    std::mutex mu;
    std::vector<double> cLatency;
    std::vector<double> gauge;

    void observeC(double ms, bool isZombie) {
        {
            std::lock_guard<std::mutex> lk(mu);
            cLatency.push_back(ms);
        }
        if (isZombie) zombie++;
    }
    void enter() {
        long n = ++live;
        long seen = peakLive.load();
        while (n > seen && !peakLive.compare_exchange_weak(seen, n)) {
        }
    }
    void leave() { --live; }
};

// --------------------------------------------------------------- the pool

// A counting semaphore built by hand, because that is what a connection pool
// is, and because C++17 has no std::counting_semaphore (that is C++20). The
// database on the other side of it does not know your deadline exists.
class Pool {
public:
    Pool(int size, Metrics& m) : available_(size), m_(m) {}

    void acquire() {
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait(lk, [this] { return available_ > 0; });
        available_--;
    }
    void release() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            available_++;
        }
        cv_.notify_one();
    }

    // Returns true if the query ran to completion.
    bool query(Ms duration, const Deadline& dl, bool boundedWait) {
        acquire();

        // Checked out. If the request that queued for this connection died
        // while it was queueing, give the connection straight back rather
        // than spend a whole service time on a corpse. Under overload this
        // is where most of the recovered capacity comes from.
        if (dl.expiredWithin(SLACK)) {
            m_.abandoned++;
            release();
            return false;
        }

        m_.inUse++;
        bool completed = true;
        if (boundedWait && dl.present) {
            // The C++ answer to statement_timeout: compute the remaining
            // milliseconds and hand THAT to the blocking call, instead of
            // handing it the duration you hope the work will take. This is
            // literally the third argument to poll(), and the value in
            // SO_RCVTIMEO. It is not cancellation -- nobody interrupts
            // anybody -- it is refusing to wait longer than you have.
            Ms budget = dl.remaining() - SLACK;
            Ms wait = std::min(duration, std::max(Ms(0), budget));
            std::this_thread::sleep_for(wait);
            if (wait < duration) {
                m_.killed++;
                completed = false;
            }
        } else {
            // The blocking call. Nothing on earth shortens this: no signal,
            // no destructor, no drop. The thread is in the kernel until the
            // kernel is done with it.
            std::this_thread::sleep_for(duration);
        }
        m_.inUse--;
        release();
        return completed;
    }

private:
    std::mutex mu_;
    std::condition_variable cv_;
    int available_;
    Metrics& m_;
};

// --------------------------------------------------------------- the hops

// Note that `dl` is an argument, and would simply be absent from any
// function where nobody typed it. That is the whole ergonomic story: no
// ambient context means nothing is implicit, and nothing is accidentally
// correct either.
bool serviceC(Pool& pool, Metrics& m, bool slow, Deadline dl,
              TimePoint gatewayDeadline, bool boundedWait) {
    if (dl.expiredWithin(SLACK)) {
        // Refuse to START work that cannot finish. A request rejected here
        // costs no pool slot, no queue position, nothing at all.
        return false;
    }
    std::this_thread::sleep_for(HOP_OVERHEAD);

    Ms duration = slow ? C_SERVICE_SLOW : C_SERVICE_FAST;
    TimePoint started = Clock::now();
    bool completed = pool.query(duration, dl, boundedWait);
    TimePoint finished = Clock::now();

    m.observeC(std::chrono::duration<double, std::milli>(finished - started).count(),
               completed && finished > gatewayDeadline);
    return completed;
}

bool serviceB(Pool& pool, Metrics& m, bool slow, Deadline dl,
              TimePoint gatewayDeadline, bool boundedWait) {
    if (dl.expiredWithin(SLACK)) return false;
    std::this_thread::sleep_for(HOP_OVERHEAD);

    // budget_out = budget_in - elapsed_here - slack. In the naive variant
    // there is nothing to subtract from, because nobody said.
    Deadline out;
    if (dl.present) {
        out.present = true;
        out.at = dl.at - SLACK;
    }
    return serviceC(pool, m, slow, out, gatewayDeadline, boundedWait);
}

void gatewayRequest(Pool& pool, Metrics& m, bool slow, bool propagate, bool boundedWait) {
    m.enter();
    TimePoint gatewayDeadline = Clock::now() + GATEWAY_BUDGET;
    Deadline dl;
    if (propagate) {
        dl.present = true;
        dl.at = gatewayDeadline;
    }

    bool completed = serviceB(pool, m, slow, dl, gatewayDeadline, boundedWait);

    // The gateway's 504 went out at gatewayDeadline; this thread found out
    // now. Success means the answer beat the deadline, which is the only
    // definition the client would recognise.
    if (completed && Clock::now() <= gatewayDeadline) {
        m.ok++;
    } else {
        m.failed++;
    }
    m.leave();
}

// -------------------------------------------------------------- the driver

struct Row {
    double success, zombiePerSec, poolInUse, cP99, killedPerSec, gaveBackPerSec;
    long peakThreads;
};

Row runVariant(double slowFraction, bool propagate, bool boundedWait) {
    Metrics m;
    Pool pool(C_POOL_SIZE, m);

    // Identical arrivals and an identical set of slow requests in every
    // variant, so what differs between the rows is policy and only policy.
    std::mt19937 rng(20250502);
    std::exponential_distribution<double> gap(RATE);
    std::uniform_real_distribution<double> unit(0.0, 1.0);

    std::atomic<bool> sampling{true};
    std::thread sampler([&] {
        while (sampling.load()) {
            std::this_thread::sleep_for(GAUGE_EVERY);
            std::lock_guard<std::mutex> lk(m.mu);
            m.gauge.push_back(static_cast<double>(m.inUse.load()));
        }
    });

    TimePoint begin = Clock::now();
    TimePoint end = begin + DURATION;
    TimePoint at = begin;
    std::vector<std::thread> requests;
    for (;;) {
        at += std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(gap(rng)));
        if (at > end) break;
        auto wait = at - Clock::now();
        if (wait > Clock::duration::zero()) std::this_thread::sleep_for(wait);
        bool slow = unit(rng) < slowFraction;
        // One thread per request: the ordinary shape of a thread-per-
        // connection server, and the reason `threads peak` is a column.
        requests.emplace_back(gatewayRequest, std::ref(pool), std::ref(m), slow,
                              propagate, boundedWait);
    }
    for (auto& t : requests) t.join();
    // Drain. Zombies are by definition still running after everyone gave up.
    std::this_thread::sleep_for(C_SERVICE_SLOW + Ms(300));
    sampling = false;
    sampler.join();

    double seconds = std::chrono::duration<double>(DURATION).count();
    long total = m.ok.load() + m.failed.load();
    std::lock_guard<std::mutex> lk(m.mu);
    std::sort(m.cLatency.begin(), m.cLatency.end());
    double p99 = 0;
    if (!m.cLatency.empty()) {
        size_t k = static_cast<size_t>(std::llround(0.99 * (m.cLatency.size() - 1)));
        p99 = m.cLatency[std::min(k, m.cLatency.size() - 1)];
    }
    double gaugeMean = 0;
    if (!m.gauge.empty()) {
        for (double v : m.gauge) gaugeMean += v;
        gaugeMean /= static_cast<double>(m.gauge.size());
    }
    return Row{
        total ? 100.0 * static_cast<double>(m.ok.load()) / static_cast<double>(total) : 0.0,
        static_cast<double>(m.zombie.load()) / seconds,
        gaugeMean,
        p99,
        static_cast<double>(m.killed.load()) / seconds,
        static_cast<double>(m.abandoned.load()) / seconds,
        m.peakLive.load(),
    };
}

const char* HEADER =
    "variant                      gw success  zombie/s  C pool in use  C p99 ms  killed/s  gaveback/s  threads peak";

void printRow(const char* label, const Row& r) {
    char pool[32];
    std::snprintf(pool, sizeof(pool), "%.1f/%d", r.poolInUse, C_POOL_SIZE);
    std::printf("%-28s %9.1f%% %9.1f %13s %9.0f %9.1f %11.1f %13ld\n",
                label, r.success, r.zombiePerSec, pool, r.cP99,
                r.killedPerSec, r.gaveBackPerSec, r.peakThreads);
}

int main() {
    double fastDemand = RATE * (1 - SLOW_FRACTION) * C_SERVICE_FAST.count() / 1000.0;
    double slowDemand = RATE * SLOW_FRACTION * C_SERVICE_SLOW.count() / 1000.0;

    std::printf("Deadline propagation through gateway -> serviceB -> serviceC, in C++.\n");
    std::printf("Gateway budget %lldms, slack %lldms per hop, C pool %d, offered %.0f rps for %llds.\n",
                (long long)GATEWAY_BUDGET.count(), (long long)SLACK.count(), C_POOL_SIZE,
                RATE, (long long)std::chrono::duration_cast<std::chrono::seconds>(DURATION).count());
    std::printf("When C is unwell, %.0f%% of queries take %lldms and the rest take %lldms.\n",
                SLOW_FRACTION * 100, (long long)C_SERVICE_SLOW.count(),
                (long long)C_SERVICE_FAST.count());
    std::printf("Demand on the pool is then %.1f + %.1f = %.1f connection-seconds per second\n",
                slowDemand, fastDemand, slowDemand + fastDemand);
    std::printf("against %d available, i.e. rho = %.2f. None of the slow queries can beat the budget.\n\n",
                C_POOL_SIZE, (slowDemand + fastDemand) / C_POOL_SIZE);
    std::printf("%s\n", HEADER);
    std::printf("%s\n", std::string(std::char_traits<char>::length(HEADER), '-').c_str());

    printRow("1 healthy", runVariant(0.0, false, false));
    printRow("2 naive", runVariant(SLOW_FRACTION, false, false));
    printRow("3 deadline by value", runVariant(SLOW_FRACTION, true, false));
    printRow("4 + bounded wait", runVariant(SLOW_FRACTION, true, true));

    std::printf("\n");
    std::printf("Rows 2 and 3: the deadline is an argument. Nothing carries it for you\n");
    std::printf("and nothing loses it for you either, which is the trade C++ makes with\n");
    std::printf("you on every topic in this repository.\n\n");
    std::printf("Rows 3 and 4 are the one that matters, and it is why this file is in\n");
    std::printf("C++. Row 3 knows the deadline and still waits the full service time,\n");
    std::printf("because knowing when you should stop is not the same as being able to.\n");
    std::printf("Row 4 hands the remaining milliseconds to the blocking call itself --\n");
    std::printf("poll()'s third argument, SO_RCVTIMEO, statement_timeout -- and only\n");
    std::printf("then does the connection come back early.\n\n");
    std::printf("Every other runtime in this topic gives you something that looks like\n");
    std::printf("cancellation. None of them cancel the work either; they cancel your\n");
    std::printf("WAIT for it. C++ is just the one that does not let you confuse the two.\n");
    return 0;
}
