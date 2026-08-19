// Layer 1 - The fix, C++ version: give the blocking call its own thread
// instead of submitting it to the same pool the ticker uses. This is the
// most primitive possible version of "offload blocking work" -- no
// executor abstraction, just: don't make it compete with other work for a
// scarce shared resource (the pool's one worker thread).
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <functional>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;

class ThreadPool {
public:
    explicit ThreadPool(size_t n) {
        for (size_t i = 0; i < n; i++) {
            workers_.emplace_back([this] { workerLoop(); });
        }
    }
    ~ThreadPool() {
        {
            std::lock_guard<std::mutex> lk(m_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& t : workers_) t.join();
    }
    void submit(std::function<void()> task) {
        {
            std::lock_guard<std::mutex> lk(m_);
            tasks_.push(std::move(task));
        }
        cv_.notify_one();
    }

private:
    void workerLoop() {
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lk(m_);
                cv_.wait(lk, [this] { return stop_ || !tasks_.empty(); });
                if (stop_ && tasks_.empty()) return;
                task = std::move(tasks_.front());
                tasks_.pop();
            }
            task();
        }
    }
    std::vector<std::thread> workers_;
    std::queue<std::function<void()>> tasks_;
    std::mutex m_;
    std::condition_variable cv_;
    bool stop_ = false;
};

constexpr auto TICK_INTERVAL = std::chrono::milliseconds(100);
constexpr auto BLOCK_FOR = std::chrono::milliseconds(1000);
constexpr auto LEAD_IN = std::chrono::milliseconds(200);
constexpr auto LEAD_OUT = std::chrono::milliseconds(200);

int main() {
    ThreadPool pool(1); // still just one worker thread for the ticker
    std::mutex ts_mutex;
    std::vector<double> timestamps;
    std::atomic<bool> running{true};
    auto start = Clock::now();

    std::thread timer([&] {
        while (running.load()) {
            std::this_thread::sleep_for(TICK_INTERVAL);
            if (!running.load()) return;
            pool.submit([&] {
                std::lock_guard<std::mutex> lk(ts_mutex);
                timestamps.push_back(std::chrono::duration<double>(Clock::now() - start).count());
            });
        }
    });

    std::this_thread::sleep_for(LEAD_IN);

    // GOOD: the only change from bad_blocking.cpp -- run the blocking call
    // on its own dedicated std::thread instead of the pool. The pool's one
    // worker thread stays free to keep running tick tasks the whole time.
    std::thread blocking_work([&] { std::this_thread::sleep_for(BLOCK_FOR); });
    blocking_work.join();

    std::this_thread::sleep_for(LEAD_OUT);
    running = false;
    timer.join();

    double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
    double max_gap = timestamps.empty() ? elapsed : timestamps[0];
    for (size_t i = 1; i < timestamps.size(); i++) {
        max_gap = std::max(max_gap, timestamps[i] - timestamps[i - 1]);
    }
    std::printf("[good] ticks counted: %zu  over %.2fs  (expected ~%.0f if never blocked)  max gap between ticks: %.2fs\n",
                timestamps.size(), elapsed, elapsed / (TICK_INTERVAL.count() / 1000.0), max_gap);
    return 0;
}
