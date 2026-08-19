// Layer 5 - Topic 1: the latency knee in C++.
//
// WHAT THIS DEMONSTRATES
//   C++ gives you no runtime, no scheduler and no pool, so the queue is
//   visible as an object you can print. That is this file's contribution:
//   everywhere else in this topic the waiting room is inside somebody's
//   library, and here it is a std::deque you can watch grow.
//
//   The second contribution is the measurement that settles the argument
//   in a real incident. This program asks the kernel, via getrusage(2),
//   how much CPU it actually consumed, and prints it beside wall time. At
//   every row past the knee, latency is multiples of the service time
//   while CPU utilisation is a few percent. If latency climbs while CPU
//   sits low, you are queueing on a COUNT -- pool slots, threads, tokens,
//   workers -- and adding CPU cannot help you.
//
//   (On Linux you would probably reach for /proc/self/stat here. That path
//   does not exist on Darwin. getrusage is POSIX and works on both, which
//   is the general lesson: prefer the syscall to the pseudo-filesystem.)
//
// WHAT TO LOOK FOR IN THE OUTPUT
//  1. `achieved` plateaus at workers / service time: lambda_max = L / W.
//  2. p99 tracks the S/(1-rho) column until the queue stops draining.
//  3. `queue max` is the high-water mark of the waiting room. Nobody
//     bounded it; nothing would have stopped it reaching a million.
//  4. `cpu %` stays low in every row. The machine is not busy. It is full.
//  5. Doubling the worker count moves capacity and the knee proportionally.
//
// BUILD AND RUN
//	c++ -O2 -std=c++17 -pthread -o /tmp/latency_knee latency_knee.cpp && /tmp/latency_knee
#include <sys/resource.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

static constexpr auto kService = std::chrono::milliseconds(40);
static constexpr auto kStep = std::chrono::seconds(8);
static constexpr auto kGaugeEvery = std::chrono::milliseconds(20);
static const std::vector<int> kWorkerCounts = {5, 10};
static const std::vector<double> kRhos = {0.2, 0.5, 0.8, 0.9, 0.95, 1.1};

static double ms_since(TimePoint from, TimePoint to) {
    return std::chrono::duration<double, std::milli>(to - from).count();
}

// One request, carrying the time it was SCHEDULED to arrive. Every latency
// in this file is measured from that instant, not from the moment a thread
// got round to enqueueing it. A generator that starts the clock at dispatch
// forgives itself for being late; real users do not. Topic 6.
struct Request {
    TimePoint scheduled;
};

// A bounded number of workers in front of an UNBOUNDED waiting room. This
// is the default shape of almost every thread pool ever written, including
// the one in Layer 1 topic 3, and topic 5 of this layer is about why the
// second half of that sentence is a defect rather than a default.
class Server {
public:
    explicit Server(int workers) {
        for (int i = 0; i < workers; i++) {
            threads_.emplace_back([this] { work(); });
        }
    }

    ~Server() {
        {
            std::lock_guard<std::mutex> lk(m_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& t : threads_) t.join();
    }

    void submit(Request r) {
        {
            std::lock_guard<std::mutex> lk(m_);
            queue_.push_back(r);
            queue_max_ = std::max<size_t>(queue_max_, queue_.size());
        }
        inflight_.fetch_add(1);
        cv_.notify_one();
    }

    void reset_samples() {
        std::lock_guard<std::mutex> lk(m_);
        total_.clear();
        wait_.clear();
        completions_.clear();
        queue_max_ = 0;
    }

    struct Snapshot {
        std::vector<double> total;
        std::vector<double> wait;
        std::vector<TimePoint> completions;
        size_t queue_max;
    };

    Snapshot snapshot() {
        std::lock_guard<std::mutex> lk(m_);
        return Snapshot{total_, wait_, completions_, queue_max_};
    }

    int inflight() const { return inflight_.load(); }

    void drain() {
        std::unique_lock<std::mutex> lk(m_);
        idle_.wait(lk, [this] { return queue_.empty() && busy_ == 0; });
    }

private:
    void work() {
        while (true) {
            Request r;
            {
                std::unique_lock<std::mutex> lk(m_);
                cv_.wait(lk, [this] { return stop_ || !queue_.empty(); });
                if (stop_ && queue_.empty()) return;
                r = queue_.front();
                queue_.pop_front();
                busy_++;
            }
            // The instant a worker picks this up is the end of its queue
            // wait and the start of its service. Everything between
            // `scheduled` and here is pure waiting: no code ran for it.
            TimePoint started = Clock::now();
            std::this_thread::sleep_for(kService);
            TimePoint done = Clock::now();
            inflight_.fetch_sub(1);
            {
                std::lock_guard<std::mutex> lk(m_);
                wait_.push_back(std::max(0.0, ms_since(r.scheduled, started)));
                total_.push_back(ms_since(r.scheduled, done));
                completions_.push_back(done);
                busy_--;
                if (queue_.empty() && busy_ == 0) idle_.notify_all();
            }
        }
    }

    std::vector<std::thread> threads_;
    std::deque<Request> queue_;
    std::vector<double> total_, wait_;
    std::vector<TimePoint> completions_;
    size_t queue_max_ = 0;
    int busy_ = 0;
    std::atomic<int> inflight_{0};
    std::mutex m_;
    std::condition_variable cv_, idle_;
    bool stop_ = false;
};

static double percentile(std::vector<double> v, double p) {
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    size_t k = static_cast<size_t>(std::lround(p / 100.0 * (v.size() - 1)));
    return v[std::min(k, v.size() - 1)];
}

static double mean(const std::vector<double>& v) {
    if (v.empty()) return 0.0;
    double s = 0;
    for (double x : v) s += x;
    return s / v.size();
}

// Total CPU seconds this process has burned, user + system, straight from
// the kernel. Subtract two readings to get the CPU consumed by a step.
static double cpu_seconds() {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    return ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6 +
           ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6;
}

struct StepResult {
    double offered, achieved, p50, p99, wait_p50, mean_total, gauge_l, cpu_percent;
    size_t queue_max;
};

// Exponential gaps make a Poisson process: the standard model for
// independent users arriving. Evenly spaced arrivals would understate the
// queue, because bursts are what fill it.
static StepResult step(Server& server, double rate, std::chrono::seconds dur, unsigned seed) {
    std::mt19937 rng(seed);
    std::exponential_distribution<double> gap(rate);

    server.reset_samples();
    std::vector<double> gauge;
    std::atomic<bool> sampling{true};
    std::thread sampler([&] {
        while (sampling.load()) {
            std::this_thread::sleep_for(kGaugeEvery);
            gauge.push_back(server.inflight());
        }
    });

    TimePoint begin = Clock::now();
    TimePoint deadline = begin + dur;
    double cpu_before = cpu_seconds();

    size_t sent = 0;
    TimePoint at = begin;
    while (true) {
        at += std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(gap(rng)));
        if (at > deadline) break;
        std::this_thread::sleep_until(at);
        sent++;
        server.submit(Request{at});
    }

    // Drain. Past rho=1 this is where the backlog built up during the step
    // finally comes out, which is why those rows carry latencies larger
    // than the step itself.
    server.drain();
    double cpu_used = cpu_seconds() - cpu_before;
    sampling.store(false);
    sampler.join();

    auto snap = server.snapshot();
    double seconds = std::chrono::duration<double>(dur).count();
    size_t in_window = 0;
    for (auto& c : snap.completions) {
        if (c <= deadline) in_window++;
    }
    double wall = std::chrono::duration<double>(Clock::now() - begin).count();
    unsigned cores = std::max(1u, std::thread::hardware_concurrency());

    return StepResult{
        static_cast<double>(sent) / seconds,
        static_cast<double>(in_window) / seconds,
        percentile(snap.total, 50),
        percentile(snap.total, 99),
        percentile(snap.wait, 50),
        // Little's Law is a statement about MEANS. L = lambda * p50 is not
        // a law and stops holding exactly when the distribution skews,
        // which is exactly when you reach for it.
        mean(snap.total),
        mean(gauge),
        100.0 * cpu_used / (wall * cores),
        snap.queue_max,
    };
}

static const char* kHeader =
    "  rho   offered  achieved      p50      p99   wait p50   L (gauge)   lam*Wbar   S/(1-rho)   queue max   cpu %";

static void print_row(double rho, const StepResult& r, double service) {
    char predicted[16];
    if (rho < 1.0) {
        std::snprintf(predicted, sizeof(predicted), "%9.1f", service / (1.0 - rho) * 1000.0);
    } else {
        std::snprintf(predicted, sizeof(predicted), "%9s", "inf");
    }
    std::printf("%5.2f %9.1f %9.1f %8.1f %8.1f %10.1f %11.1f %10.1f %s %11zu %7.1f\n",
                rho, r.offered, r.achieved, r.p50, r.p99, r.wait_p50, r.gauge_l,
                r.achieved * r.mean_total / 1000.0, predicted, r.queue_max, r.cpu_percent);
}

static void chart(const std::vector<double>& p99s) {
    double top = 1.0;
    for (double v : p99s) top = std::max(top, v);
    std::printf("\n  p99 (ms) against rho\n");
    for (size_t i = 0; i < p99s.size(); i++) {
        int n = std::max(1, static_cast<int>(std::lround(56 * p99s[i] / top)));
        std::printf("  rho=%-6.2f|%s %.0f\n", kRhos[i], std::string(n, '#').c_str(), p99s[i]);
    }
    std::printf("  %10s+%s %.0f ms full scale\n", "", std::string(56, '-').c_str(), top);
}

static void sweep(int workers) {
    Server server(workers);

    // Measure S rather than assuming kService. A capacity computed from a
    // constant nobody measured is the commonest way this experiment lies.
    StepResult warm = step(server, 5.0, std::chrono::seconds(2), 12345);
    double service = warm.mean_total / 1000.0;
    double capacity = workers / service;

    std::printf("\n=== %d workers, measured service time S = %.1f ms ===\n", workers, service * 1000);
    std::printf("predicted capacity L/S = %.1f rps\n\n", capacity);
    std::printf("%s\n", kHeader);
    std::printf("%s\n", std::string(std::char_traits<char>::length(kHeader), '-').c_str());

    std::vector<double> p99s;
    for (size_t i = 0; i < kRhos.size(); i++) {
        StepResult r = step(server, capacity * kRhos[i], kStep, 777 + i * 31);
        print_row(kRhos[i], r, service);
        p99s.push_back(r.p99);
    }
    chart(p99s);
}

int main() {
    std::printf("Latency knee in C++: a fixed worker pool, an unbounded waiting room,\n");
    std::printf("and open-model Poisson arrivals. %u hardware threads available.\n",
                std::max(1u, std::thread::hardware_concurrency()));
    for (int w : kWorkerCounts) sweep(w);
    std::printf("\nThe two sweeps ran identical code and an identical ramp; the only\n");
    std::printf("difference is the worker count. Compare their capacity lines and their\n");
    std::printf("rho=0.9 rows: that is the whole topic in two numbers.\n");
    std::printf("\nThen read the cpu %% column top to bottom. Not one row is CPU-bound.\n");
    std::printf("Every millisecond of that latency was spent waiting for a count.\n");
    return 0;
}
