// Layer 1 - C++'s concurrency model: there isn't one built into the
// language. C++ gives you std::thread and synchronization primitives and
// leaves the scheduling policy entirely up to you (or a library you pull
// in -- Boost.Asio, libuv, etc.). The most common real-world shape is a
// fixed-size thread pool, and a fixed-size pool has EXACTLY the same
// failure mode as Python's single-threaded asyncio if you shrink it to one
// worker: submit a blocking task to the same pool your "ticker" runs on,
// and the ticker cannot run until the blocking task finishes, no matter
// how many other tasks are queued behind it.
//
// This also reuses a lesson from Topic 3's tokio experiment: counting
// ticks alone can lie. A queue-based pool "catches up" queued ticks the
// instant a worker frees up, so the total count can look almost normal
// even though every one of those ticks fired late, back-to-back, instead
// of on schedule. We track timestamps and report the max gap for exactly
// that reason.
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <functional>
#include <future>
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
    ThreadPool pool(1); // single worker thread -- the failure mode, on purpose
    std::mutex ts_mutex;
    std::vector<double> timestamps;
    std::atomic<bool> running{true};
    auto start = Clock::now();

    // A dedicated timer thread submits a tick task to the pool every
    // 100ms. It's not itself part of the pool, so it keeps firing on
    // schedule regardless of what the pool is doing -- exactly like a real
    // OS timer would.
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

    // BAD: the blocking call is submitted to the SAME pool as the ticker.
    // With only one worker thread, this owns it completely for a full
    // second; every tick task queued during that time has to wait.
    std::promise<void> done;
    auto fut = done.get_future();
    pool.submit([&] {
        std::this_thread::sleep_for(BLOCK_FOR);
        done.set_value();
    });
    fut.wait();

    std::this_thread::sleep_for(LEAD_OUT);
    running = false;
    timer.join();

    double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
    double max_gap = timestamps.empty() ? elapsed : timestamps[0];
    for (size_t i = 1; i < timestamps.size(); i++) {
        max_gap = std::max(max_gap, timestamps[i] - timestamps[i - 1]);
    }
    std::printf("[bad] ticks counted: %zu  over %.2fs  (expected ~%.0f if never blocked)  max gap between ticks: %.2fs\n",
                timestamps.size(), elapsed, elapsed / (TICK_INTERVAL.count() / 1000.0), max_gap);
    return 0;
}
