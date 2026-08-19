// Layer 6 Topic 2 - Coordinated omission: why your load test says the p99 is fine.
//
// Why C++: it is the closest thing here to writing the load generator by hand
// against the kernel, and it shows the cost of the honest design with nothing
// hiding it. Every in-flight request in the open-loop phase is one std::thread,
// which is one pthread, which on macOS is a 512KB stack and a scheduler entry.
// The program prints how many it created. Put that number next to Go's (~0
// extra OS threads for the same hundred in flight) and Node's (none at all) and
// you have the entire practical history of why load testing tools defaulted to
// a small fixed pool of virtual users -- and therefore why an industry's worth
// of p99 numbers are measured with the generator looking away.
//
// The second thing C++ shows, by omission: the shared sample vector below is
// guarded by a std::mutex because it must be, not because anything made it so.
// Delete the lock_guard and this file still compiles, still runs, and produces
// numbers that are wrong in a way no output would reveal. The Rust version of
// this same file does not offer that option. That contrast is the point of
// having both.
//
// What this demonstrates
// ----------------------
//   * Service: single server, FIFO queue, 3ms per request -> ~333 req/s.
//   * Offered load: 200 req/s, a comfortable 60% of capacity.
//   * At T+2.5s exactly one request takes 500ms. One request.
//
//   * CLOSED-LOOP: 4 virtual users, send -> wait -> think 20ms -> repeat.
//     This is `k6 run --vus 4`, and almost every load test ever written.
//   * OPEN-LOOP: requests issued at a fixed 200/s regardless of what came back.
//     This is k6's constant-arrival-rate executor, or `vegeta -rate=200`.
//
// What to look for in the output
// ------------------------------
//   1. "requests started IN the stall window": ~4 closed-loop, ~100 open-loop.
//      That one line is the entire mechanism.
//   2. The p99 rows. Same service, same fault, two answers.
//   3. "OS threads spawned by the generator" against the Go and Node runs.
//
// Portability: standard C++17 threads only, no epoll, no /proc, nothing
// Linux-specific. Runs as written on macOS/arm64 and on Linux.
//
// Run:
//   clang++ -O2 -std=c++17 -pthread -o /tmp/coordinated_omission \
//     coordinated_omission.cpp && /tmp/coordinated_omission

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;
using TimePoint = Clock::time_point;

constexpr long SERVICE_MS = 3;        // -> ~333 req/s capacity
constexpr long STALL_AFTER_MS = 2500; // when the one slow request happens
constexpr long STALL_MS = 500;        // how long that one request takes
constexpr long RUN_MS = 5000;
constexpr long OPEN_RATE_PER_SEC = 200; // offered load, ~60% of capacity
constexpr long CLOSED_VUS = 4;
constexpr long CLOSED_THINK_MS = CLOSED_VUS * 1000 / OPEN_RATE_PER_SEC;

double ms_between(TimePoint a, TimePoint b) {
    return std::chrono::duration<double, std::milli>(b - a).count();
}

/// One completed request, all offsets in milliseconds since the run started.
struct Sample {
    double sent_offset_ms;
    double latency_from_sent_ms;
    double latency_from_arrival_ms;
};

/// One unit of work. The client waits on `done`.
struct Job {
    std::mutex mu;
    std::condition_variable cv;
    bool finished = false;
    TimePoint completed_at;

    void complete(TimePoint t) {
        {
            std::lock_guard<std::mutex> guard(mu);
            completed_at = t;
            finished = true;
        }
        cv.notify_one();
    }

    TimePoint wait() {
        std::unique_lock<std::mutex> lock(mu);
        cv.wait(lock, [this] { return finished; });
        return completed_at;
    }
};

/// A single server with a FIFO queue. The queue is where the latency a
/// closed-loop generator cannot see accumulates.
class Service {
public:
    explicit Service(TimePoint epoch) : epoch_(epoch) {
        worker_ = std::thread([this] { serve(); });
    }

    void submit(Job* job) {
        {
            std::lock_guard<std::mutex> guard(mu_);
            queue_.push_back(job);
        }
        cv_.notify_one();
    }

    void stop() {
        {
            std::lock_guard<std::mutex> guard(mu_);
            running_ = false;
        }
        cv_.notify_all();
        worker_.join();
    }

private:
    void serve() {
        bool stalled = false;
        for (;;) {
            Job* job = nullptr;
            {
                std::unique_lock<std::mutex> lock(mu_);
                cv_.wait(lock, [this] { return !queue_.empty() || !running_; });
                if (queue_.empty() && !running_) return;
                job = queue_.front();
                queue_.pop_front();
            }
            double elapsed_ms = ms_between(epoch_, Clock::now());
            if (!stalled && elapsed_ms >= static_cast<double>(STALL_AFTER_MS)) {
                stalled = true;
                std::this_thread::sleep_for(std::chrono::milliseconds(STALL_MS));
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(SERVICE_MS));
            }
            job->complete(Clock::now());
        }
    }

    TimePoint epoch_;
    std::mutex mu_;
    std::condition_variable cv_;
    std::deque<Job*> queue_;
    bool running_ = true;
    std::thread worker_;
};

struct RunResult {
    std::vector<Sample> samples;
    std::vector<double> iteration_ms;
    size_t peak_in_flight = 0;
    size_t threads_spawned = 0;
};

double percentile(std::vector<double> values, double q) {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    size_t idx = static_cast<size_t>(q * (values.size() - 1) + 0.5);
    return values[std::min(idx, values.size() - 1)];
}

size_t started_in_stall(const std::vector<Sample>& samples) {
    size_t n = 0;
    for (const auto& s : samples) {
        if (s.sent_offset_ms >= STALL_AFTER_MS &&
            s.sent_offset_ms < STALL_AFTER_MS + STALL_MS) {
            ++n;
        }
    }
    return n;
}

std::vector<double> column(const std::vector<Sample>& samples, bool from_arrival) {
    std::vector<double> out;
    out.reserve(samples.size());
    for (const auto& s : samples) {
        out.push_back(from_arrival ? s.latency_from_arrival_ms : s.latency_from_sent_ms);
    }
    return out;
}

RunResult run_closed_loop() {
    TimePoint epoch = Clock::now();
    Service service(epoch);
    std::mutex samples_mu;   // nothing in the language requires this. It is
    std::vector<Sample> samples;  // required anyway. That is the C++ deal.
    std::vector<double> iteration_ms;

    std::vector<std::thread> users;
    for (long i = 0; i < CLOSED_VUS; ++i) {
        users.emplace_back([&] {
            while (ms_between(epoch, Clock::now()) < RUN_MS) {
                TimePoint iter_start = Clock::now();
                Job job;
                TimePoint sent = Clock::now();
                service.submit(&job);
                TimePoint done = job.wait();  // <- this virtual user is now
                                              //    blocked, and offering no load
                double latency = ms_between(sent, done);
                {
                    std::lock_guard<std::mutex> guard(samples_mu);
                    samples.push_back({ms_between(epoch, sent), latency, latency});
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(CLOSED_THINK_MS));
                {
                    std::lock_guard<std::mutex> guard(samples_mu);
                    iteration_ms.push_back(ms_between(iter_start, Clock::now()));
                }
            }
        });
    }
    for (auto& t : users) t.join();
    service.stop();

    RunResult result;
    result.samples = std::move(samples);
    result.iteration_ms = std::move(iteration_ms);
    result.peak_in_flight = CLOSED_VUS;
    result.threads_spawned = CLOSED_VUS;
    return result;
}

RunResult run_open_loop() {
    TimePoint epoch = Clock::now();
    Service service(epoch);
    std::mutex samples_mu;
    std::vector<Sample> samples;
    std::atomic<size_t> in_flight{0};
    std::atomic<size_t> peak_in_flight{0};

    std::vector<std::thread> issuers;
    auto interval = std::chrono::nanoseconds(1000000000LL / OPEN_RATE_PER_SEC);
    for (long seq = 0; interval * seq < std::chrono::milliseconds(RUN_MS); ++seq) {
        TimePoint target = epoch + interval * seq;
        TimePoint now = Clock::now();
        if (target > now) std::this_thread::sleep_for(target - now);

        // One OS thread per in-flight request. This is the price of an honest
        // open-loop generator with nothing but the standard library.
        issuers.emplace_back([&, target] {
            size_t current = in_flight.fetch_add(1) + 1;
            size_t observed = peak_in_flight.load();
            while (current > observed && !peak_in_flight.compare_exchange_weak(observed, current)) {
            }
            Job job;
            TimePoint sent = Clock::now();
            service.submit(&job);
            TimePoint done = job.wait();
            {
                std::lock_guard<std::mutex> guard(samples_mu);
                samples.push_back({ms_between(epoch, sent), ms_between(sent, done),
                                   // measured from when the request was DUE
                                   ms_between(target, done)});
            }
            in_flight.fetch_sub(1);
        });
    }
    RunResult result;
    result.threads_spawned = issuers.size();
    for (auto& t : issuers) t.join();
    service.stop();

    result.samples = std::move(samples);
    result.peak_in_flight = peak_in_flight.load();
    return result;
}

}  // namespace

int main() {
    std::string bar(74, '=');
    std::printf("%s\n", bar.c_str());
    std::printf("COORDINATED OMISSION   (C++17 std::thread, single-server FIFO service)\n");
    std::printf("%s\n", bar.c_str());
    std::printf("service capacity ~%ld req/s (%ldms/request), offered load %ld req/s\n",
                1000 / SERVICE_MS, SERVICE_MS, OPEN_RATE_PER_SEC);
    std::printf("one request at T+%ldms takes %ldms instead of %ldms\n",
                STALL_AFTER_MS, STALL_MS, SERVICE_MS);
    std::printf("run length %ldms\n\n", RUN_MS);

    std::printf("running closed-loop (%ld virtual users, %ldms think time)...\n",
                CLOSED_VUS, CLOSED_THINK_MS);
    RunResult closed = run_closed_loop();
    std::printf("running open-loop (%ld req/s arrival rate)...\n\n", OPEN_RATE_PER_SEC);
    RunResult open = run_open_loop();

    std::vector<double> closed_latency = column(closed.samples, false);
    std::vector<double> open_latency = column(open.samples, true);

    std::printf("%-38s%14s%14s\n", "", "CLOSED-LOOP", "OPEN-LOOP");
    std::printf("%-38s%14zu%14zu\n", "requests completed", closed.samples.size(), open.samples.size());
    std::printf("%-38s%14zu%14zu\n", "requests started IN the stall window",
                started_in_stall(closed.samples), started_in_stall(open.samples));
    std::printf("%-38s%14zu%14zu\n", "peak requests in flight", closed.peak_in_flight, open.peak_in_flight);
    std::printf("%-38s%14zu%14zu\n", "OS threads spawned by the generator",
                closed.threads_spawned, open.threads_spawned);
    std::printf("\n");

    const char* labels[] = {"p50", "p75", "p95", "p99", "p99.9", "max"};
    const double qs[] = {0.50, 0.75, 0.95, 0.99, 0.999, 1.0};
    for (int i = 0; i < 6; ++i) {
        char label[40];
        std::snprintf(label, sizeof(label), "latency %s", labels[i]);
        std::printf("%-38s%12.1fms%12.1fms\n", label,
                    percentile(closed_latency, qs[i]), percentile(open_latency, qs[i]));
    }

    std::printf("\nThe closed-loop column measures request duration: send -> response.\n");
    std::printf("The open-loop column measures from the moment the request was DUE.\n");
    std::printf("Note the first row too: closed-loop completed %zu requests to open-loop's\n",
                closed.samples.size());
    std::printf("%zu. It did not go slower -- it asked for less, precisely while the\n",
                open.samples.size());
    std::printf("service was worst.\n");

    std::printf("\nThe tell, inside the closed-loop run alone:\n");
    std::printf("  request duration p99   : %8.1fms\n", percentile(closed_latency, 0.99));
    std::printf("  iteration duration p99 : %8.1fms\n", percentile(closed.iteration_ms, 0.99));
    std::printf("  If iteration_duration climbs while http_req_duration does not, your\n");
    std::printf("  generator stopped asking. That is k6's version of this same line.\n");

    double c99 = percentile(closed_latency, 0.99);
    double o99 = percentile(open_latency, 0.99);
    if (c99 > 0) {
        std::printf("\nVERDICT: open-loop p99 is %.1fx the closed-loop p99 for the identical\n", o99 / c99);
        std::printf("service and the identical fault.\n");
    }
    size_t hit = started_in_stall(closed.samples);
    std::printf("The closed-loop generator sampled the stall %zu times out of %zu requests\n",
                hit, closed.samples.size());
    std::printf("(%.2f%%), which is why it never reaches the 99th percentile.\n",
                100.0 * hit / std::max<size_t>(1, closed.samples.size()));
    std::printf("\nC++ footnote: the last row of the table cost one pthread and one 512KB\n");
    std::printf("stack per in-flight request. Nothing here is wrong -- it is simply the\n");
    std::printf("bill for measuring honestly without a runtime, and it is the bill that\n");
    std::printf("made closed-loop generators the industry default.\n");
    return 0;
}
