// Layer 10 - Topic 3: the pool is the concurrency limit. (C++)
//
// What this demonstrates
//     Part 1  L = λW as a wall, against the pool every C++ service ends up
//             with: a std::condition_variable over a fixed-size free list.
//             c slots and mean service time W pin maximum throughput at
//             c/W. This is the best language to READ to understand what
//             the other five are doing, because the wait is right there in
//             the source instead of inside a framework.
//     Part 2  The C++-specific fact: there is no cancellation. A caller
//             that gave up at its deadline is not connected to the worker
//             thread in any way, so the slot stays held until the query
//             returns. The second row is the only fix available -- a
//             cooperative check, written by hand, at the one point where
//             the worker can still act on it.
//
// What to look for
//     - Part 1: `svc p50` flat across every row while `acq p99` explodes.
//     - Part 2: `slot-seconds wasted` -- slot time spent on work whose
//       caller has already given up. Nothing in the language will reclaim
//       it. Compare against rust/pool_queueing, where dropping the future
//       returns the permit with no code at all.
//     - The cooperative fix only works at a yield point you chose. Once
//       the worker is inside the blocking call, it is gone until the call
//       returns, which is the same shape as topic 2's C++ gateway.
//
// The Kingman variance arm lives in python/pool_queueing.py -- distributions
// are arithmetic, not a property of any runtime.
//
// Build and run (no arguments):
//     c++ -O2 -std=c++20 -pthread -o /tmp/pool_cpp cpp/pool_queueing.cpp \
//       && /tmp/pool_cpp

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;
constexpr uint64_t kSeed = 20260818;

double ms(Clock::duration d) {
    return std::chrono::duration<double, std::milli>(d).count();
}

// The pool every C++ service ends up with: a counter, a mutex and a
// condition variable. `c` is explicit and the wait is visible.
class Pool {
public:
    explicit Pool(int slots) : free_(slots) {}

    void acquire() {
        std::unique_lock<std::mutex> lock(mu_);
        cv_.wait(lock, [this] { return free_ > 0; });
        --free_;
    }

    void release() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            ++free_;
        }
        cv_.notify_one();
    }

private:
    std::mutex mu_;
    std::condition_variable cv_;
    int free_;
};

struct Samples {
    std::mutex mu;
    std::vector<double> acquire, service, total;
};

double pct(std::vector<double> v, double q) {
    if (v.empty()) return std::nan("");
    std::sort(v.begin(), v.end());
    size_t i = std::min(v.size() - 1, static_cast<size_t>(q * v.size()));
    return v[i];
}

struct WallResult {
    Samples samples;
    std::atomic<long> completed{0};
    // Completions that landed INSIDE the arrival window. Throughput has to be
    // counted over the same interval as `wall`: `completed` keeps rising while
    // the backlog drains after the last arrival, and dividing the post-drain
    // total by the arrival window reports a rate above c/W -- above the wall
    // this section says cannot be crossed.
    long completed_in_window = 0;
    double wall = 0;
};

// Part 1: open-loop Poisson arrivals, one thread per request. Threads are
// the wrong unit at this rate and that is fine -- the point is the pool
// wait, and a thread per request is exactly what a naive service does.
void wall_run(double lambda, int slots, std::chrono::milliseconds service,
              std::chrono::seconds duration, WallResult* out) {
    std::mt19937_64 rng(kSeed);
    std::exponential_distribution<double> interarrival(lambda);
    Pool pool(slots);

    std::vector<std::thread> threads;
    const auto start = Clock::now();
    auto next = start;
    while (Clock::now() - start < duration) {
        next += std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(interarrival(rng)));
        std::this_thread::sleep_until(next);
        threads.emplace_back([&pool, service, out] {
            const auto arrived = Clock::now();
            pool.acquire();
            const auto acquired = Clock::now();
            std::this_thread::sleep_for(service);
            const auto done = Clock::now();
            pool.release();
            out->completed.fetch_add(1, std::memory_order_relaxed);
            std::lock_guard<std::mutex> lock(out->samples.mu);
            out->samples.acquire.push_back(ms(acquired - arrived));
            out->samples.service.push_back(ms(done - acquired));
            out->samples.total.push_back(ms(done - arrived));
        });
    }
    out->wall = std::chrono::duration<double>(Clock::now() - start).count();
    out->completed_in_window = out->completed.load(std::memory_order_relaxed);
    for (auto& t : threads) t.join();
}

struct DeadlineResult {
    std::atomic<long> goodput{0};
    std::atomic<long> abandoned{0};
    std::atomic<long long> wasted_us{0};
};

// Part 2: the same load with a client deadline.
//
//   cooperative = false   nothing connects the caller's deadline to the
//                         worker. The slot is held for the full service
//                         time regardless.
//   cooperative = true    the worker checks the deadline immediately after
//                         acquiring, and gives the slot straight back if
//                         the caller is already gone. Written by hand,
//                         because nothing else is going to write it.
void deadline_run(double lambda, int slots, std::chrono::milliseconds service,
                  std::chrono::milliseconds deadline, std::chrono::seconds duration,
                  bool cooperative, DeadlineResult* out) {
    std::mt19937_64 rng(kSeed);
    std::exponential_distribution<double> interarrival(lambda);
    Pool pool(slots);

    std::vector<std::thread> threads;
    const auto start = Clock::now();
    auto next = start;
    while (Clock::now() - start < duration) {
        next += std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(interarrival(rng)));
        std::this_thread::sleep_until(next);
        threads.emplace_back([&pool, service, deadline, cooperative, out] {
            const auto arrived = Clock::now();
            pool.acquire();
            const auto held_from = Clock::now();

            if (cooperative && held_from - arrived >= deadline) {
                // The caller is already gone. Do not start the work.
                pool.release();
                out->abandoned.fetch_add(1, std::memory_order_relaxed);
                return;
            }

            std::this_thread::sleep_for(service);
            const auto done = Clock::now();
            pool.release();

            const auto elapsed = done - arrived;
            if (elapsed <= deadline) {
                out->goodput.fetch_add(1, std::memory_order_relaxed);
            } else {
                out->abandoned.fetch_add(1, std::memory_order_relaxed);
                // Slot time spent past the caller's deadline: pure waste.
                const auto overrun = std::min(elapsed - deadline, done - held_from);
                out->wasted_us.fetch_add(
                    std::chrono::duration_cast<std::chrono::microseconds>(overrun).count(),
                    std::memory_order_relaxed);
            }
        });
    }
    for (auto& t : threads) t.join();
}

}  // namespace

int main() {
    std::printf("C++ - pool queueing and Little's Law\n");
    std::printf("  arrivals: Poisson (c_a = 1), open loop, seed %llu\n",
                static_cast<unsigned long long>(kSeed));

    constexpr int kSlots = 20;
    constexpr auto kService = std::chrono::milliseconds(50);
    std::printf("\nPart 1 - L = λW. c = %d slots, W = %lldms, so λ_max = c/W = %.0f req/s\n",
                kSlots, static_cast<long long>(kService.count()),
                kSlots / (kService.count() / 1000.0));
    std::printf("%s\n", std::string(78, '-').c_str());
    std::printf("  %-10s %5s %9s %9s %9s %9s %9s\n", "run", "rho", "acq p50", "acq p99",
                "svc p50", "tot p99", "done/s");
    for (double lambda : {200.0, 360.0, 400.0, 440.0}) {
        WallResult r;
        wall_run(lambda, kSlots, kService, std::chrono::seconds(3), &r);
        const double rho = lambda * (kService.count() / 1000.0) / kSlots;
        std::printf("  %-10s %5.2f %9.1f %9.1f %9.1f %9.1f %9.0f\n",
                    ("lambda=" + std::to_string(static_cast<int>(lambda))).c_str(), rho,
                    pct(r.samples.acquire, 0.5), pct(r.samples.acquire, 0.99),
                    pct(r.samples.service, 0.5), pct(r.samples.total, 0.99),
                    r.completed_in_window / r.wall);
    }
    std::printf("\n  Service time is identical in every row. Everything that moved is\n");
    std::printf("  waiting for a slot, which is why acquire wait needs its own timer.\n");
    std::printf("  Read `svc p50` before comparing done/s against the header's\n");
    std::printf("  lambda_max. sleep_for(50ms) overshoots by a few milliseconds here,\n");
    std::printf("  so the REAL wall is c / measured W, a few percent below 400. The\n");
    std::printf("  arithmetic is right; the W you feed it has to be the measured one,\n");
    std::printf("  which is the same point Part 3 of python/pool_queueing.py makes\n");
    std::printf("  about c.\n");

    constexpr auto kDeadline = std::chrono::milliseconds(120);
    std::printf("\nPart 2 - a client deadline, in a language with no cancellation\n");
    std::printf("%s\n", std::string(78, '-').c_str());
    std::printf("  lambda = 420/s against c = %d, W = %lldms (rho > 1 on purpose), "
                "deadline %lldms\n",
                kSlots, static_cast<long long>(kService.count()),
                static_cast<long long>(kDeadline.count()));
    std::printf("\n  %-34s %9s %11s %22s\n", "policy", "goodput", "abandoned",
                "slot-seconds wasted");
    for (auto [label, cooperative] :
         {std::pair<const char*, bool>{"no cancellation (the default)", false},
          std::pair<const char*, bool>{"cooperative check after acquire", true}}) {
        DeadlineResult r;
        deadline_run(420.0, kSlots, kService, kDeadline, std::chrono::seconds(4),
                     cooperative, &r);
        std::printf("  %-34s %9ld %11ld %22.2f\n", label, r.goodput.load(),
                    r.abandoned.load(), r.wasted_us.load() / 1e6);
    }
    std::printf("\n  The second row is the whole of what C++ offers: a check you wrote,\n");
    std::printf("  at a point you chose, before the blocking call starts. Once the\n");
    std::printf("  worker is inside that call it is unreachable until it returns --\n");
    std::printf("  the same shape as this topic's C++ gateway in topic 2.\n");
    std::printf("\n  The Kingman variance arm is in python/pool_queueing.py.\n");
    return 0;
}
