// Layer 10 - Topic 4: the compiler changes your arithmetic. (C++)
//
// What this demonstrates
//     Two things at once, and both are about who decided the order of
//     operations:
//
//       1. naive softmax vs the max-subtracted (log-sum-exp) form, in
//          float and in __fp16, printed as exact hex bits so "the same"
//          means the same and not "close enough to print identically";
//       2. what -ffast-math does to the identical source. It licenses
//          reassociation and contraction into FMA, so the compiler is free
//          to sum your logits in a different order and round differently.
//
//     Build this file twice and diff the outputs. Any difference is the
//     compiler having exercised that licence -- not a bug in the program,
//     and not something a code review would have caught.
//
// What to look for
//     - The naive rows becoming inf/nan while the stable rows keep
//       returning a distribution.
//     - Whether the two builds' hex digits differ, and in which rows.
//       -ffast-math also turns off nan/inf handling, so the naive rows can
//       change character entirely rather than just in the last bits.
//     - `sum_fwd` against `sum_rev`: the same values added in the opposite
//        order. If those two ever print identical bits, the loop was
//        optimised away, not proven equal -- check before concluding
//        anything.
//
// Contrast with rust/strict_fp, which cannot do this: Rust's f32 is
// IEEE-strict, reassociation never happens, and FMA only appears if you
// call mul_add yourself. That is the reference to check both C++ builds
// against.
//
// Build and run BOTH ways (no arguments):
//     c++ -O2 -std=c++20 -o /tmp/sm      cpp/softmax.cpp && /tmp/sm
//     c++ -O2 -ffast-math -std=c++20 -o /tmp/sm_fast cpp/softmax.cpp && /tmp/sm_fast
//     diff <(/tmp/sm) <(/tmp/sm_fast)

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace {

constexpr int kVocab = 1024;
constexpr unsigned kSeed = 20260818;

// Print the exact bits, because printf("%f") hides the last 20 of them.
void print_bits(const char* label, float v) {
    unsigned bits;
    std::memcpy(&bits, &v, sizeof(bits));
    std::printf("%s = %.9g  [0x%08x]", label, static_cast<double>(v), bits);
}

std::vector<float> logits(float peak) {
    std::mt19937 rng(kSeed);
    std::normal_distribution<float> gauss(0.0f, 1.0f);
    std::vector<float> x(kVocab);
    for (auto& v : x) v = gauss(rng);
    x[0] = peak;
    return x;
}

struct SoftmaxResult {
    float sum;
    float max_p;
};

SoftmaxResult softmax_naive(const std::vector<float>& x) {
    float total = 0.0f;
    float max_p = 0.0f;
    std::vector<float> e(x.size());
    for (size_t i = 0; i < x.size(); ++i) {
        e[i] = std::exp(x[i]);
        total += e[i];
    }
    for (float v : e) max_p = std::max(max_p, v / total);
    return {total / total, max_p};
}

SoftmaxResult softmax_stable(const std::vector<float>& x) {
    const float m = *std::max_element(x.begin(), x.end());
    float total = 0.0f;
    std::vector<float> e(x.size());
    for (size_t i = 0; i < x.size(); ++i) {
        // Exact, not approximate: multiplying numerator and denominator by
        // exp(-max) is an identity, and it moves the largest exponent to
        // exp(0) = 1.
        e[i] = std::exp(x[i] - m);
        total += e[i];
    }
    float max_p = 0.0f;
    for (float v : e) max_p = std::max(max_p, v / total);
    return {total / total, max_p};
}

// Sum the same values in opposite orders. Mathematically identical,
// bitwise not, because floating-point addition is not associative.
float sum_forward(const std::vector<float>& v) {
    float s = 0.0f;
    for (size_t i = 0; i < v.size(); ++i) s += v[i];
    return s;
}

float sum_reverse(const std::vector<float>& v) {
    float s = 0.0f;
    for (size_t i = v.size(); i-- > 0;) s += v[i];
    return s;
}

}  // namespace

int main() {
    std::printf("C++ softmax stability and compiler reassociation\n");
#ifdef __FAST_MATH__
    std::printf("  build: -ffast-math IS ON (reassociation and FMA contraction "
                "permitted)\n");
#else
    std::printf("  build: -ffast-math is off (IEEE semantics requested)\n");
#endif
    std::printf("  vocabulary %d, logits ~N(0,1) with one peak\n\n", kVocab);

    std::printf("  %11s %14s %20s %14s %20s\n", "peak logit", "naive sum",
                "naive max p", "stable sum", "stable max p");
    for (float peak : {50.0f, 200.0f, 800.0f}) {
        const auto x = logits(peak);
        const auto naive = softmax_naive(x);
        const auto stable = softmax_stable(x);
        unsigned nb, sb;
        std::memcpy(&nb, &naive.max_p, sizeof(nb));
        std::memcpy(&sb, &stable.max_p, sizeof(sb));
        std::printf("  %11.0f %14.6g %12.6g[0x%08x] %14.6g %12.6g[0x%08x]\n", peak,
                    static_cast<double>(naive.sum), static_cast<double>(naive.max_p),
                    nb, static_cast<double>(stable.sum),
                    static_cast<double>(stable.max_p), sb);
    }

    std::printf("\n  __fp16 storage, float accumulation vs __fp16 accumulation\n");
    std::printf("  Storage precision and accumulation precision are separate\n");
    std::printf("  decisions, and conflating them is a classic attention bug.\n");
    {
        const auto x = logits(5.0f);
        std::vector<__fp16> half(x.size());
        for (size_t i = 0; i < x.size(); ++i) half[i] = static_cast<__fp16>(x[i]);

        float acc_f32 = 0.0f;
        for (auto v : half) acc_f32 += static_cast<float>(v);
        __fp16 acc_f16 = 0;
        for (auto v : half) acc_f16 = static_cast<__fp16>(acc_f16 + v);

        std::printf("    ");
        print_bits("accumulate in float", acc_f32);
        std::printf("\n    ");
        print_bits("accumulate in __fp16", static_cast<float>(acc_f16));
        std::printf("\n    relative difference = %.3e\n",
                    std::fabs(static_cast<float>(acc_f16) - acc_f32) /
                        std::fabs(acc_f32));
    }

    std::printf("\n  Same values, opposite summation order\n");
    {
        const auto x = logits(5.0f);
        const float fwd = sum_forward(x);
        const float rev = sum_reverse(x);
        std::printf("    ");
        print_bits("sum_fwd", fwd);
        std::printf("\n    ");
        print_bits("sum_rev", rev);
        std::printf("\n    identical bits: %s\n", fwd == rev ? "yes" : "NO");
        std::printf("    If these are identical, check the loops were not folded\n");
        std::printf("    into one constant before concluding order does not matter.\n");
    }

    std::printf("\n  Now diff this output against the other build:\n");
    std::printf("    diff <(/tmp/sm) <(/tmp/sm_fast)\n");
    std::printf("  Any difference is the compiler exercising a licence you gave\n");
    std::printf("  it on the command line, in a file that did not change.\n");
    return 0;
}
