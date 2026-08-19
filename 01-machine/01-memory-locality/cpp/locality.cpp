// Layer 1 - Memory & cache locality, C++ version.
// Same pointer-chasing benchmark as the other languages: two physical
// layouts of the same logical traversal. Compiled with optimizations on,
// this is the closest thing in the lab to "what the hardware actually
// does" with none of a managed runtime's overhead in the way -- which is
// exactly why the shuffled/sequential ratio here should be the widest of
// any language in this lab (see the Python version's README note on how
// interpreter overhead dilutes this same signal).
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <random>
#include <vector>

constexpr int64_t N = 2'000'000;
constexpr int64_t LAPS = 5;

void build(bool shuffled, std::vector<int32_t>& values, std::vector<int32_t>& next) {
    values.resize(N);
    next.resize(N);
    for (int64_t i = 0; i < N; i++) values[i] = static_cast<int32_t>(i);

    if (!shuffled) {
        for (int64_t i = 0; i < N; i++) next[i] = static_cast<int32_t>((i + 1) % N);
        return;
    }
    std::vector<int64_t> perm(N);
    for (int64_t i = 0; i < N; i++) perm[i] = i;
    std::mt19937_64 rng(42);
    for (int64_t i = N - 1; i > 0; i--) {
        std::uniform_int_distribution<int64_t> dist(0, i);
        int64_t j = dist(rng);
        std::swap(perm[i], perm[j]);
    }
    for (int64_t i = 0; i < N; i++) {
        next[perm[i]] = static_cast<int32_t>(perm[(i + 1) % N]);
    }
}

int64_t traverse(const std::vector<int32_t>& values, const std::vector<int32_t>& next, int64_t laps) {
    int64_t total = 0;
    int32_t idx = 0;
    int64_t steps = N * laps;
    for (int64_t s = 0; s < steps; s++) {
        total += values[idx];
        idx = next[idx];
    }
    return total;
}

void bench(const char* label, bool shuffled) {
    std::vector<int32_t> values, next;
    build(shuffled, values, next);
    auto start = std::chrono::high_resolution_clock::now();
    int64_t total = traverse(values, next, LAPS);
    auto end = std::chrono::high_resolution_clock::now();
    double elapsed = std::chrono::duration<double>(end - start).count();
    double ns_per_step = elapsed / (N * LAPS) * 1e9;
    std::printf("%-10s  total=%15lld  time=%6.3fs  %6.1f ns/step\n", label, (long long)total, elapsed, ns_per_step);
}

int main() {
    std::printf("N=%lld laps=%lld (g++/clang++, -O2)\n", (long long)N, (long long)LAPS);
    bench("sequential", false);
    bench("shuffled", true);
    return 0;
}
