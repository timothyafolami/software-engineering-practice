// Layer 1 - Why `counter++` is not atomic, C++ version.
// Unsynchronized concurrent access to a plain (non-atomic) variable is
// undefined behavior under the C++ memory model, exactly like Rust's rule
// for non-atomic shared data -- and for the same reason Rust's bare
// increment race didn't reproduce cleanly in this lab (see that topic's
// README), the optimizer is allowed to assume it doesn't happen and
// transform the loop accordingly. C++ hands you the footgun without even
// requiring an `unsafe` keyword to pull the trigger, which is precisely
// the contrast worth noticing against Rust.
#include <atomic>
#include <cstdio>
#include <mutex>
#include <thread>
#include <vector>

constexpr int THREADS = 8;
constexpr long INCREMENTS = 300'000;

long run_unsafe() {
    long counter = 0;
    std::vector<std::thread> threads;
    for (int i = 0; i < THREADS; i++) {
        threads.emplace_back([&] {
            for (long j = 0; j < INCREMENTS; j++) {
                counter++; // racy: not synchronized, and legally UB
            }
        });
    }
    for (auto& t : threads) t.join();
    return counter;
}

long run_mutex() {
    long counter = 0;
    std::mutex m;
    std::vector<std::thread> threads;
    for (int i = 0; i < THREADS; i++) {
        threads.emplace_back([&] {
            for (long j = 0; j < INCREMENTS; j++) {
                std::lock_guard<std::mutex> lk(m);
                counter++;
            }
        });
    }
    for (auto& t : threads) t.join();
    return counter;
}

long run_atomic() {
    std::atomic<long> counter{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < THREADS; i++) {
        threads.emplace_back([&] {
            for (long j = 0; j < INCREMENTS; j++) {
                counter.fetch_add(1, std::memory_order_relaxed);
            }
        });
    }
    for (auto& t : threads) t.join();
    return counter.load();
}

int main() {
    long expected = static_cast<long>(THREADS) * INCREMENTS;
    long unsafe_result = run_unsafe();
    long mutex_result = run_mutex();
    long atomic_result = run_atomic();
    std::printf("expected:               %ld\n", expected);
    std::printf("unsafe (counter++):     %ld  (lost %ld)\n", unsafe_result, expected - unsafe_result);
    std::printf("safe (std::mutex):      %ld\n", mutex_result);
    std::printf("safe (std::atomic):     %ld\n", atomic_result);
    return 0;
}
