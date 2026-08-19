// Layer 10 - Topic 1: STREAM-style bandwidth ceiling in C++.
//
// What this demonstrates
//     The reference number for the whole topic: how many bytes per second
//     this machine actually moves out of DRAM, with nothing between the
//     loop and the load/store units. Decode at batch 1 is bounded by this
//     figure divided by the model's weight bytes, so every prediction in
//     topic 1 starts here.
//
// What to look for
//     - 1 thread vs all threads, because you cannot know in advance which
//       one is the ceiling. If the many-thread row is higher, a single core
//       could not saturate the memory controller. If it is flat or lower,
//       one core already reaches the controller's limit and the extra
//       threads only add contention -- and on a big.LITTLE part, efficiency
//       cores dragging the slowest chunk. Run both; report the higher.
//     - copy / add / scale / triad should land within a few percent of
//       each other once you account for the byte factor printed in the
//       "bytes" column. If triad is much slower per byte, the compiler did
//       not vectorise it.
//     - This number and rust/stream's number should agree within ~10% at
//       the same thread count. A gap there is a benchmark bug.
//
// Byte accounting: copy and scale move 2N (one read, one write), add and
// triad move 3N. Classic STREAM convention, matched by the Python and Rust
// implementations so the three are comparable. On a write-allocate cache
// real DRAM traffic is one array higher; the convention is what matters,
// not its absolute truth, as long as all three use the same one.
//
// Build and run (no arguments):
//     c++ -O3 -std=c++20 -o /tmp/stream cpp/stream.cpp && /tmp/stream

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <functional>
#include <thread>
#include <vector>

namespace {

constexpr std::size_t kBytesPerArray = 512ull * 1024 * 1024;
constexpr std::size_t kElems = kBytesPerArray / sizeof(double);
constexpr int kReps = 7;
constexpr double kScalar = 3.0;

using Clock = std::chrono::steady_clock;

// Split [0, kElems) across `threads` workers and run `body(lo, hi)` on each.
void parallel_for(unsigned threads, const std::function<void(std::size_t, std::size_t)>& body) {
    if (threads <= 1) {
        body(0, kElems);
        return;
    }
    std::vector<std::thread> pool;
    pool.reserve(threads);
    const std::size_t chunk = (kElems + threads - 1) / threads;
    for (unsigned t = 0; t < threads; ++t) {
        const std::size_t lo = std::min(kElems, static_cast<std::size_t>(t) * chunk);
        const std::size_t hi = std::min(kElems, lo + chunk);
        if (lo >= hi) break;
        pool.emplace_back([&body, lo, hi] { body(lo, hi); });
    }
    for (auto& th : pool) th.join();
}

struct Result {
    double first_gbps;
    double best_gbps;
    double best_ms;
};

Result run(unsigned threads, int byte_factor,
           const std::function<void(std::size_t, std::size_t)>& body) {
    std::vector<double> times;
    times.reserve(kReps);
    for (int r = 0; r < kReps; ++r) {
        const auto t0 = Clock::now();
        parallel_for(threads, body);
        const auto t1 = Clock::now();
        times.push_back(std::chrono::duration<double>(t1 - t0).count());
    }
    const double bytes = static_cast<double>(byte_factor) * kBytesPerArray;
    const double best = *std::min_element(times.begin() + 1, times.end());
    return Result{bytes / times.front() / 1e9, bytes / best / 1e9, best * 1e3};
}

}  // namespace

int main() {
    const unsigned hw = std::max(1u, std::thread::hardware_concurrency());

    std::printf("elements per array : %zu doubles (%.0f MiB)\n", kElems,
                static_cast<double>(kBytesPerArray) / (1024 * 1024));
    std::printf("working set        : %.2f GiB across 3 arrays\n",
                3.0 * kBytesPerArray / (1024.0 * 1024 * 1024));
    std::printf("hardware threads   : %u\n", hw);
    std::printf("reps               : %d (rep 0 reported separately: first touch)\n\n", kReps);

    std::vector<double> a(kElems), b(kElems), c(kElems);
    // First touch on the main thread, once, so the timed reps below are not
    // measuring the page-fault handler.
    for (std::size_t i = 0; i < kElems; ++i) {
        a[i] = 1.0;
        b[i] = 2.0;
        c[i] = 0.0;
    }

    double* pa = a.data();
    double* pb = b.data();
    double* pc = c.data();

    struct Kernel {
        const char* label;
        int byte_factor;
        std::function<void(std::size_t, std::size_t)> body;
    };

    const std::vector<Kernel> kernels = {
        {"copy   b[i] = a[i]", 2,
         [pa, pb](std::size_t lo, std::size_t hi) {
             for (std::size_t i = lo; i < hi; ++i) pb[i] = pa[i];
         }},
        {"scale  b[i] = q*a[i]", 2,
         [pa, pb](std::size_t lo, std::size_t hi) {
             for (std::size_t i = lo; i < hi; ++i) pb[i] = kScalar * pa[i];
         }},
        {"add    c[i] = a[i]+b[i]", 3,
         [pa, pb, pc](std::size_t lo, std::size_t hi) {
             for (std::size_t i = lo; i < hi; ++i) pc[i] = pa[i] + pb[i];
         }},
        {"triad  c[i] = a[i]+q*b[i]", 3,
         [pa, pb, pc](std::size_t lo, std::size_t hi) {
             for (std::size_t i = lo; i < hi; ++i) pc[i] = pa[i] + kScalar * pb[i];
         }},
    };

    double ceiling = 0.0;
    unsigned ceiling_threads = 1;
    for (unsigned threads : {1u, hw}) {
        std::printf("--- %u thread%s ---\n", threads, threads == 1 ? "" : "s");
        std::printf("%-28s %6s %11s %11s %9s\n", "kernel", "bytes", "rep0 GB/s",
                    "best GB/s", "best ms");
        for (const auto& k : kernels) {
            const Result r = run(threads, k.byte_factor, k.body);
            std::printf("%-28s %5dN %11.1f %11.1f %9.1f\n", k.label, k.byte_factor,
                        r.first_gbps, r.best_gbps, r.best_ms);
            if (r.best_gbps > ceiling) {
                ceiling = r.best_gbps;
                ceiling_threads = threads;
            }
        }
        std::printf("\n");
        if (hw == 1) break;
    }

    // Consume the results so no optimiser can delete the loops above.
    volatile double sink = c[0] + c[kElems / 2] + c[kElems - 1] + b[kElems - 1];
    (void)sink;

    std::printf("best sustained     : %.1f GB/s at %u thread%s\n", ceiling,
                ceiling_threads, ceiling_threads == 1 ? "" : "s");
    std::printf("This is the ceiling batch-1 decode is divided out of.\n");
    std::printf("Compare against rust/stream (same thread count) and\n");
    std::printf("python/stream.py (single-threaded). Disagreement over ~10%%\n");
    std::printf("is a benchmark bug, not a language finding.\n");
    return 0;
}
