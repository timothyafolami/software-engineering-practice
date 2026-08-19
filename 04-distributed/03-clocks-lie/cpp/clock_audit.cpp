// Layer 4 Topic 3 (Part A) -- C++'s clocks, audited rather than assumed.
//
// WHAT THIS DEMONSTRATES: four things, in order.
//   1. the clock inventory. std::chrono::system_clock is wall time;
//      steady_clock is monotonic and says so at compile time via is_steady;
//      high_resolution_clock is IMPLEMENTATION-DEFINED and is a typedef for one
//      of the other two. This program prints which one, on your toolchain, with
//      std::is_same_v -- so the clock most people reach for when they want
//      precision may be the one that can step backwards, and you find out rather
//      than assume.
//   2. one span timed twice -- through the application's own now(), which reads
//      the wall clock, and through steady_clock -- with an NTP-style step
//      applied inside two of the spans.
//   3. the raw clock_gettime layer, which is where the Darwin/Linux difference
//      stops being trivia: CLOCK_BOOTTIME does not exist on Darwin, and the
//      nearest equivalents are CLOCK_MONOTONIC_RAW (unslewed) and
//      CLOCK_UPTIME_RAW (excludes sleep). Both paths are compiled behind
//      #ifdef so this file builds on either platform, and it prints which
//      branch it took.
//   4. the summary line for the README's record table.
//
// WHAT TO LOOK FOR IN THE OUTPUT: the high_resolution_clock line in section 1,
// and the CLOCK_BOOTTIME line in section 3. Neither is a number you can look up
// once and reuse -- both change with the standard library and the kernel.
//
//   g++ -O2 -std=c++17 -Wall -Wextra -o /tmp/l4t3_cpp cpp/clock_audit.cpp && /tmp/l4t3_cpp

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <string>
#include <type_traits>
#include <vector>

namespace ch = std::chrono;

static constexpr int64_t kStepBackNs = -40LL * 1000 * 1000 * 1000;  // an NTP step
static constexpr int kSpans = 400;
static constexpr int64_t kSpanWorkNs = 200 * 1000;

// ------------------------------------------------------------- 1. inventory

// Smallest non-zero delta this clock reports. Measured, not documented: a clock
// can advertise nanoseconds and tick in microseconds, and on Darwin several do.
template <typename Read>
static int64_t measure_resolution_ns(Read read, int trials = 20) {
    int64_t smallest = INT64_MAX;
    for (int i = 0; i < trials; ++i) {
        const int64_t a = read();
        for (;;) {
            const int64_t b = read();
            if (b != a) {
                const int64_t d = b > a ? b - a : a - b;
                smallest = std::min(smallest, d);
                break;
            }
        }
    }
    return smallest;
}

template <typename Clock>
static int64_t now_ns() {
    return ch::duration_cast<ch::nanoseconds>(Clock::now().time_since_epoch()).count();
}

static void inventory() {
    std::printf("------------------------------------------------------------------------------\n");
    std::printf("1. the three std::chrono clocks, and which one high_resolution_clock IS\n");
    std::printf("------------------------------------------------------------------------------\n");
    std::printf("  %-36s%-12s%-12s%s\n", "clock", "is_steady", "period", "measured resolution");

    struct Row {
        const char* name;
        bool steady;
        double period_ns;
        int64_t (*read)();
    };
    const Row rows[] = {
        {"std::chrono::system_clock", ch::system_clock::is_steady,
         static_cast<double>(ch::system_clock::period::num) * 1e9 / ch::system_clock::period::den,
         &now_ns<ch::system_clock>},
        {"std::chrono::steady_clock", ch::steady_clock::is_steady,
         static_cast<double>(ch::steady_clock::period::num) * 1e9 / ch::steady_clock::period::den,
         &now_ns<ch::steady_clock>},
        {"std::chrono::high_resolution_clock", ch::high_resolution_clock::is_steady,
         static_cast<double>(ch::high_resolution_clock::period::num) * 1e9 /
             ch::high_resolution_clock::period::den,
         &now_ns<ch::high_resolution_clock>},
    };
    for (const Row& r : rows) {
        std::printf("  %-36s%-12s%9.3f ns%12lld ns\n", r.name, r.steady ? "true" : "FALSE",
                    r.period_ns,
                    static_cast<long long>(measure_resolution_ns(r.read)));
    }

    // The line this section exists for. is_steady == false on the clock whose
    // name promises precision is the whole trap: "high resolution" says nothing
    // about "monotonic", and on several standard libraries these are the same
    // type. Checked here rather than looked up, because it varies by toolchain.
    std::printf("\n");
    std::printf("  is_same_v<high_resolution_clock, system_clock>  ->  %s\n",
                std::is_same_v<ch::high_resolution_clock, ch::system_clock> ? "TRUE" : "false");
    std::printf("  is_same_v<high_resolution_clock, steady_clock>  ->  %s\n",
                std::is_same_v<ch::high_resolution_clock, ch::steady_clock> ? "TRUE" : "false");
    if (std::is_same_v<ch::high_resolution_clock, ch::system_clock>) {
        std::printf("  ^ on THIS toolchain the precise-sounding clock is the settable one.\n");
        std::printf("    Every duration measured with it can go backwards on an NTP step.\n");
    } else {
        std::printf("  ^ on THIS toolchain you got away with it. That is a property of the\n");
        std::printf("    standard library you compiled against, not of the language, and it\n");
        std::printf("    is not portable. Name steady_clock explicitly and stop guessing.\n");
    }
}

// ------------------------------------------------- 2. one span, two clocks

// The application's own now(). Every service has one; most read the wall clock.
// The offset stands in for an NTP step -- we never touch the system clock, and
// lab/README.md explains why per-container skew is not possible here anyway.
struct AppClock {
    int64_t offset_ns = 0;
    int64_t now_ns() const { return ::now_ns<ch::system_clock>() + offset_ns; }
    void step(int64_t ns) { offset_ns += ns; }
};

static void burn(int64_t ns) {
    const auto end = ch::steady_clock::now() + ch::nanoseconds(ns);
    while (ch::steady_clock::now() < end) {
        // busy: we are timing, not sleeping
    }
}

static double pct(std::vector<double> v, double q) {
    std::sort(v.begin(), v.end());
    long i = static_cast<long>(q * static_cast<double>(v.size()) + 0.5) - 1;
    if (i < 0) i = 0;
    if (i >= static_cast<long>(v.size())) i = static_cast<long>(v.size()) - 1;
    return v[static_cast<size_t>(i)];
}

static int span_report(const std::vector<double>& wall, const std::vector<double>& mono) {
    std::printf("\n------------------------------------------------------------------------------\n");
    std::printf("2. %d identical spans, timed twice, with a %.0fs step and a %.0fs step\n", kSpans,
                static_cast<double>(kStepBackNs) / 1e9, -static_cast<double>(kStepBackNs) / 1e9);
    std::printf("   landing INSIDE two of them\n");
    std::printf("------------------------------------------------------------------------------\n");
    std::printf("  %-30s%10s%12s%14s%14s%10s\n", "clock", "p50", "p99", "max", "min", "negative");

    int negatives = 0;
    struct Row { const char* name; const std::vector<double>* v; };
    const Row rows[] = {{"wall (app now())", &wall}, {"monotonic (steady_clock)", &mono}};
    for (const Row& r : rows) {
        int neg = 0;
        for (double x : *r.v) if (x < 0) ++neg;
        if (r.name[0] == 'w') negatives = neg;
        const auto mm = std::minmax_element(r.v->begin(), r.v->end());
        std::printf("  %-30s%10.3f%12.3f%14.1f%14.1f%10d\n", r.name, pct(*r.v, 0.50),
                    pct(*r.v, 0.99), *mm.second, *mm.first, neg);
    }
    std::printf("  (milliseconds; 'negative' counts spans that finished before they started)\n");

    const size_t hot = static_cast<size_t>(
        std::max_element(wall.begin(), wall.end()) - wall.begin());
    const size_t lo = hot > 19 ? hot - 19 : 0;
    const size_t hi = std::min(hot + 21, wall.size());
    const auto mm = std::minmax_element(wall.begin(), wall.end());
    std::printf("\n  Two samples out of %d were touched: %.0f ms and %.0f ms, against a p50\n",
                kSpans, *mm.first, *mm.second);
    std::printf("  of %.3f ms. Over all %d spans that is only the max -- one sample in %d\n",
                pct(wall, 0.50), kSpans, kSpans);
    std::printf("  cannot move a p99 by rank. But dashboards aggregate windows, not runs:\n");
    std::printf("  over the %zu spans around the step the wall-clock p99 is %.1f ms against\n",
                hi - lo, pct(std::vector<double>(wall.begin() + static_cast<long>(lo),
                                                 wall.begin() + static_cast<long>(hi)), 0.99));
    std::printf("  a monotonic p99 of %.3f ms. Only the clock differed.\n",
                pct(std::vector<double>(mono.begin() + static_cast<long>(lo),
                                        mono.begin() + static_cast<long>(hi)), 0.99));
    return negatives;
}

// --------------------------------------------- 3. the raw POSIX clock layer

static bool read_posix_clock(clockid_t id, double* out_seconds) {
    struct timespec ts {};
    if (clock_gettime(id, &ts) != 0) return false;
    *out_seconds = static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
    return true;
}

static void posix_clocks() {
    std::printf("\n------------------------------------------------------------------------------\n");
    std::printf("3. clock_gettime, where the Darwin/Linux difference stops being trivia\n");
    std::printf("------------------------------------------------------------------------------\n");

#if defined(__APPLE__)
    std::printf("  compiled branch: __APPLE__ (Darwin)\n");
    std::printf("  CLOCK_BOOTTIME does not exist here. It is not a macro you can test for\n");
    std::printf("  and fall back from at runtime -- referencing it does not compile, which\n");
    std::printf("  is why this file is #ifdef'd rather than branching on uname().\n\n");
    const struct { const char* name; clockid_t id; const char* note; } clocks[] = {
        {"CLOCK_REALTIME", CLOCK_REALTIME, "wall clock, SETTABLE, can step backwards"},
        {"CLOCK_MONOTONIC", CLOCK_MONOTONIC, "monotonic, slewed by NTP, stops in sleep"},
        {"CLOCK_MONOTONIC_RAW", CLOCK_MONOTONIC_RAW, "monotonic, NOT slewed"},
        {"CLOCK_UPTIME_RAW", CLOCK_UPTIME_RAW, "monotonic, excludes time asleep"},
        {"CLOCK_PROCESS_CPUTIME_ID", CLOCK_PROCESS_CPUTIME_ID, "CPU time, not wall time at all"},
    };
#elif defined(__linux__)
    std::printf("  compiled branch: __linux__\n");
    std::printf("  CLOCK_BOOTTIME exists here and is the one that keeps counting while the\n");
    std::printf("  machine is suspended -- which is the clock a lease timer wants and the\n");
    std::printf("  one Darwin cannot give you. Topic 7 is where that matters.\n\n");
    const struct { const char* name; clockid_t id; const char* note; } clocks[] = {
        {"CLOCK_REALTIME", CLOCK_REALTIME, "wall clock, SETTABLE, can step backwards"},
        {"CLOCK_MONOTONIC", CLOCK_MONOTONIC, "monotonic, slewed by NTP, stops in suspend"},
        {"CLOCK_MONOTONIC_RAW", CLOCK_MONOTONIC_RAW, "monotonic, NOT slewed"},
        {"CLOCK_BOOTTIME", CLOCK_BOOTTIME, "monotonic, INCLUDES time suspended"},
        {"CLOCK_PROCESS_CPUTIME_ID", CLOCK_PROCESS_CPUTIME_ID, "CPU time, not wall time at all"},
    };
#else
    std::printf("  compiled branch: neither __APPLE__ nor __linux__.\n");
    std::printf("  This program does not know which clock ids your platform offers, and\n");
    std::printf("  guessing is how you get a lease timer that silently reads CPU time.\n");
    const struct { const char* name; clockid_t id; const char* note; } clocks[] = {
        {"CLOCK_REALTIME", CLOCK_REALTIME, "wall clock, SETTABLE"},
        {"CLOCK_MONOTONIC", CLOCK_MONOTONIC, "monotonic"},
    };
#endif

    std::printf("  %-28s%-16s%s\n", "clock id", "reads", "what it is");
    for (const auto& c : clocks) {
        double seconds = 0;
        if (read_posix_clock(c.id, &seconds)) {
            std::printf("  %-28s%13.3f s  %s\n", c.name, seconds, c.note);
        } else {
            std::printf("  %-28s%13s    %s\n", c.name, "FAILED", c.note);
        }
    }

#if defined(__APPLE__)
    double mono = 0, raw = 0;
    if (read_posix_clock(CLOCK_MONOTONIC, &mono) && read_posix_clock(CLOCK_MONOTONIC_RAW, &raw)) {
        std::printf("\n  MONOTONIC - MONOTONIC_RAW = %+.6f s\n", mono - raw);
        std::printf("  ^ the slew NTP has applied since boot. Not an error -- it is the\n");
        std::printf("    correction working, and it is why RAW and non-RAW are two clocks.\n");
    }
#endif
}

int main() {
    std::printf("==============================================================================\n");
    std::printf("Layer 4 Topic 3 -- C++ clock audit\n");
    std::printf("==============================================================================\n");
#if defined(__clang__)
    std::printf("  clang %d.%d.%d", __clang_major__, __clang_minor__, __clang_patchlevel__);
#elif defined(__GNUC__)
    std::printf("  gcc %d.%d.%d", __GNUC__, __GNUC_MINOR__, __GNUC_PATCHLEVEL__);
#else
    std::printf("  unknown compiler");
#endif
    std::printf("   __cplusplus=%ldL\n\n", static_cast<long>(__cplusplus));

    inventory();

    AppClock clock;
    std::vector<double> wall, mono;
    wall.reserve(kSpans);
    mono.reserve(kSpans);
    // Fixed indices rather than a timer thread: a timer racing an 80ms loop is
    // how you get a run where the step lands between spans and the experiment
    // silently proves nothing -- the README lists that as a broken experiment.
    const int step_back_at = kSpans / 3;
    const int step_fwd_at = 2 * kSpans / 3;
    for (int i = 0; i < kSpans; ++i) {
        const int64_t w0 = clock.now_ns();
        const auto m0 = ch::steady_clock::now();
        burn(kSpanWorkNs);
        if (i == step_back_at) clock.step(kStepBackNs);
        else if (i == step_fwd_at) clock.step(-kStepBackNs);
        wall.push_back(static_cast<double>(clock.now_ns() - w0) / 1e6);
        mono.push_back(
            static_cast<double>(ch::duration_cast<ch::nanoseconds>(
                                    ch::steady_clock::now() - m0).count()) / 1e6);
    }
    const int negatives = span_report(wall, mono);

    posix_clocks();

    std::printf("\n------------------------------------------------------------------------------\n");
    std::printf("4. one line for the record table in the README\n");
    std::printf("------------------------------------------------------------------------------\n");
    const int64_t res = measure_resolution_ns(&now_ns<ch::steady_clock>);
    std::printf("  | C++ | std::chrono::steady_clock | %lld ns | yes (%d negative wall-clock span%s) |\n",
                static_cast<long long>(res), negatives, negatives == 1 ? "" : "s");
    std::printf("\n  The table in the README stays blank until you fill it in. This line is\n");
    std::printf("  the measurement, not the answer -- copy it across yourself.\n");
    return 0;
}
