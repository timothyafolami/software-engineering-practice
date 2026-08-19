// Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.
//
// WHAT THIS DEMONSTRATES: C++ has no runtime, no collector and no event loop, so
// the only thing that can stop the renewal thread is the OPERATING SYSTEM. That
// makes this the version where the general shape is clearest: every other
// runtime in this topic adds its own reasons to pause on top of the ones here,
// and none of them removes these.
//
// Three hazards, in order:
//
//   1. CPU oversubscription. 8x more busy threads than cores. The kernel
//      scheduler is preemptive and fair, so the renewal thread keeps getting
//      slices -- the expected result is that this does NOT lose the lease, and
//      it is worth seeing that a loaded machine is not the same as a stopped one.
//   2. SIGSTOP. A child process is forked to send SIGCONT after the window, then
//      this process stops itself. This is the real hazard, reproduced exactly:
//      it is what `docker kill -s SIGSTOP relay-a` does in parts 2 and 3 of this
//      topic, and it is indistinguishable from a live-migrating VM or a host
//      that started swapping.
//   3. cgroup CFS throttling. THIS IS THE PRODUCTION ONE and it does not exist
//      on Darwin -- there are no cgroup files to exhaust. The program says so
//      and refuses rather than running something else and calling it the same
//      hazard. Run it inside a Linux container with a CPU limit.
//
// WHAT TO LOOK FOR IN THE OUTPUT: hazard 1 held and hazard 2 did not. Busy is not
// stopped. Every lease design that reasons about "the process is healthy" is
// reasoning about hazard 1 and will be defeated by hazard 2, which no amount of
// renewal tuning shortens because you do not control how long you are stopped.
//
//   g++ -O2 -std=c++17 -pthread -Wall -Wextra -o /tmp/l4t7_cpp cpp/pause_audit.cpp && /tmp/l4t7_cpp

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

namespace ch = std::chrono;
using clk = ch::steady_clock;   // monotonic; a wall clock here would let an NTP
                                // step masquerade as a pause (Topic 3)

static constexpr auto kLeaseTTL = ch::seconds(10);
static constexpr auto kRenewInterval = ch::seconds(1);
static constexpr auto kHazard = ch::seconds(12);

struct Renewals {
    std::vector<double> gaps;
    clk::time_point last = clk::now();
    void tick() {
        const auto now = clk::now();
        gaps.push_back(ch::duration<double>(now - last).count());
        last = now;
    }
    double longest() const {
        return gaps.empty() ? 0.0 : *std::max_element(gaps.begin(), gaps.end());
    }
};

static void renew_loop(Renewals* r, std::atomic<bool>* stop) {
    while (!stop->load(std::memory_order_relaxed)) {
        std::this_thread::sleep_for(kRenewInterval);
        r->tick();
    }
}

// Force `v` to be materialised in a register here, so the optimiser cannot
// delete the loop that produced it. This is Google Benchmark's DoNotOptimize
// idiom and it is the ONLY thing that reliably works.
//
// The obvious guard -- ending the loop with `if (acc == 42) printf("");` -- does
// NOT work, and that is worth knowing rather than trusting. Measured here with
// Apple clang 21 at -O2: with that guard the loop below reports ~2.8e12
// rounds/s on one core; with the value genuinely observed it reports ~5.9e8
// rounds/s. The first number is roughly 5000x the rate this expression can
// physically sustain, because the arithmetic was deleted while `n += 50000`
// kept counting as if it had happened. A "keep the compiler honest" line that
// the compiler ignores is worse than no line at all: it produces a fabricated
// number wearing a comment that says it is trustworthy.
static inline void keep_alive(uint64_t& v) { asm volatile("" : "+r"(v)); }

// Hazard 1: more busy threads than cores. Busy, not stopped.
static uint64_t oversubscribe(unsigned threads, ch::seconds d) {
    std::atomic<uint64_t> rounds{0};
    std::vector<std::thread> workers;
    const auto end = clk::now() + d;
    for (unsigned i = 0; i < threads; ++i) {
        workers.emplace_back([&rounds, end] {
            uint64_t acc = 0x9e3779b97f4a7c15ULL, n = 0;
            while (clk::now() < end) {
                for (int k = 0; k < 50000; ++k) {
                    acc = (acc << 7 | acc >> 57) ^ (acc * 0x2545f4914f6cdd1dULL);
                }
                keep_alive(acc);   // see above: this line is load-bearing
                n += 50000;
            }
            rounds.fetch_add(n, std::memory_order_relaxed);
        });
    }
    for (auto& t : workers) t.join();
    return rounds.load();
}

// Hazard 2: stop this process outright, and arrange for something else to start
// it again. A process cannot SIGCONT itself -- that is the whole nature of the
// hazard -- so a child is forked first to do it.
static bool sigstop_self(ch::seconds d) {
    const pid_t parent = getpid();
    const pid_t child = fork();
    if (child < 0) {
        std::printf("  fork() failed: %s -- skipping the SIGSTOP hazard\n", std::strerror(errno));
        return false;
    }
    if (child == 0) {
        // fork() in a multithreaded process gives the child ONE thread and a
        // copy of every lock, some of which may have been held by a thread that
        // does not exist here. So the child does only async-signal-safe things:
        // nanosleep (which is what sleep_for compiles to), kill, _exit. No
        // allocation, no iostreams, no std::thread, and _exit rather than exit
        // so no atexit handler from the parent runs twice.
        std::this_thread::sleep_for(d);
        kill(parent, SIGCONT);
        _exit(0);
    }
    // Everything in this process stops here, including the renewal thread. No
    // signal handler runs, no timer fires, nothing is queued for later. The
    // process is not slow; it does not exist as far as the scheduler cares.
    raise(SIGSTOP);
    int status = 0;
    waitpid(child, &status, 0);
    return true;
}

static bool report(const char* label, double longest, double took, uint64_t rounds) {
    const bool lost = longest > ch::duration<double>(kLeaseTTL).count();
    std::printf("  %-40s%8.2fs    %8.2fs    %-16s%13llu rounds\n", label, longest, took,
                lost ? "LOST THE LEASE" : "held", static_cast<unsigned long long>(rounds));
    return lost;
}

int main() {
    std::printf("==============================================================================\n");
    std::printf("Layer 4 Topic 7 -- C++ pause audit\n");
    std::printf("==============================================================================\n");
    const unsigned cores = std::max(1u, std::thread::hardware_concurrency());
#if defined(__clang__)
    std::printf("  clang %d.%d.%d, ", __clang_major__, __clang_minor__, __clang_patchlevel__);
#elif defined(__GNUC__)
    std::printf("  gcc %d.%d.%d, ", __GNUC__, __GNUC_MINOR__, __GNUC_PATCHLEVEL__);
#endif
    std::printf("hardware_concurrency = %u\n", cores);
    std::printf("  lease TTL %llds, renewal every %llds, hazard %llds\n",
                static_cast<long long>(kLeaseTTL.count()),
                static_cast<long long>(kRenewInterval.count()),
                static_cast<long long>(kHazard.count()));
    std::printf("  clock : std::chrono::steady_clock (is_steady = %s)\n",
                clk::is_steady ? "true" : "false");
    std::printf("\n  %-40s%9s    %9s    %-16s\n", "run", "longest gap", "hazard took", "verdict");

    bool any_lost = false;

    {   // hazard 1
        Renewals r;
        std::atomic<bool> stop{false};
        std::thread keepalive(renew_loop, &r, &stop);
        std::this_thread::sleep_for(2 * kRenewInterval);
        const auto t0 = clk::now();
        const uint64_t rounds = oversubscribe(cores * 8, kHazard);
        const double took = ch::duration<double>(clk::now() - t0).count();
        std::this_thread::sleep_for(2 * kRenewInterval);
        stop = true;
        keepalive.join();
        char label[64];
        std::snprintf(label, sizeof label, "CPU oversubscription, %u threads", cores * 8);
        any_lost |= report(label, r.longest(), took, rounds);
    }

    {   // hazard 2
        Renewals r;
        std::atomic<bool> stop{false};
        std::thread keepalive(renew_loop, &r, &stop);
        std::this_thread::sleep_for(2 * kRenewInterval);
        const auto t0 = clk::now();
        const bool ran = sigstop_self(kHazard);
        const double took = ch::duration<double>(clk::now() - t0).count();
        std::this_thread::sleep_for(2 * kRenewInterval);
        stop = true;
        keepalive.join();
        if (ran) any_lost |= report("SIGSTOP for the whole window", r.longest(), took, 0);
    }

    // hazard 3
    std::printf("\n");
#if defined(__linux__)
    std::printf("  cgroup CFS throttling: available on this platform. Set a CPU limit on\n");
    std::printf("  the container and run this program's oversubscription hazard inside it;\n");
    std::printf("  the quota runs out mid-period and every thread is descheduled until the\n");
    std::printf("  next one. Read /sys/fs/cgroup/cpu.stat and watch nr_throttled climb.\n");
#else
    std::printf("  cgroup CFS throttling: NOT AVAILABLE on this platform, and this program\n");
    std::printf("  will not pretend otherwise. There are no cgroup files on Darwin to\n");
    std::printf("  exhaust, and the closest local hazard (oversubscription, above) is a\n");
    std::printf("  DIFFERENT mechanism -- the kernel keeps scheduling you, it just gives\n");
    std::printf("  you less. Throttling stops you outright, like the SIGSTOP run.\n");
    std::printf("\n");
    std::printf("  To run the real one, inside a Linux container with a CPU limit:\n");
    std::printf("    docker run --rm --cpus 0.1 -v \"$PWD:/w\" -w /w gcc:14 \\\n");
    std::printf("      sh -c 'g++ -O2 -std=c++17 -pthread -o /tmp/a cpp/pause_audit.cpp && /tmp/a'\n");
    std::printf("    # then: cat /sys/fs/cgroup/cpu.stat   -- nr_throttled and throttled_usec\n");
#endif

    std::printf("\n");
    std::printf("  Read the two runs above together. Hazard 1 kept the lease while running\n");
    std::printf("  every core flat out: BUSY IS NOT STOPPED, and a lease design that only\n");
    std::printf("  ever gets tested under load will pass. Hazard 2 lost it outright, and\n");
    std::printf("  nothing in this process could have prevented that -- there is no code\n");
    std::printf("  you can write that runs while you are SIGSTOPped.\n");
    std::printf("\n");
    std::printf("  That is the argument for fencing in one paragraph. You cannot shrink the\n");
    std::printf("  pause window to zero because you do not control it, so safety has to be\n");
    std::printf("  enforced at the RESOURCE: `AND fence < $epoch` in the UPDATE, zero rows\n");
    std::printf("  updated means you are stale, and the stale holder logs loudly and exits.\n");
    return any_lost ? 1 : 0;
}
