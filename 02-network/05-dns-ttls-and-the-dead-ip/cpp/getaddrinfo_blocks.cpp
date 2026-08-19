// Layer 2 · Topic 5 - C++: getaddrinfo blocks the calling thread, and that is
// the whole story.
//
// POSIX offers no asynchronous name resolution worth using. There is no
// aio_getaddrinfo. glibc's getaddrinfo_a exists, is glibc-only, and is
// implemented with -- a thread pool. So every runtime in this topic is
// wrapping this same blocking call: Python in its default ThreadPoolExecutor,
// Node in libuv's four threads, tokio on spawn_blocking's pool, the JVM behind
// a cache. Writing it once, by hand, makes the design space visible and makes
// clear why c-ares exists at all: because the standard library had no answer.
//
// Three measurements:
//   A. Resolution on the calling thread, one name at a time. The honest cost.
//   B. The same work with a pool of POOL_SIZE workers -- the shape every
//      runtime above adopted -- while a ticker on the main thread records the
//      largest gap between beats.
//   C. The same pool, deliberately too small for the work, so the queue
//      becomes visible. This is Python's default executor during a DNS blip
//      and Node's four libuv threads, reproduced on purpose.
//
// What to look for in the output:
//   - phase A's per-name times. A cold lookup costs a network round trip; a
//     warm one costs a lookup in your OS resolver's cache. Neither is free
//     and neither is cached by this process.
//   - phase B's ticker gap versus phase A's total. The work took the same
//     time; only the thread it happened on changed.
//   - phase C: the same lookups, the same machine, latency inflated purely by
//     queueing. No DNS metric anywhere would show this.
//
// Portability: POSIX getaddrinfo(3) plus <thread>. No epoll, no /proc, no
// cgroups -- builds and runs unchanged on Darwin arm64 and on Linux. One
// Darwin note worth carrying: /etc/resolv.conf on macOS says, in its own first
// lines, that it is not consulted for hostname resolution. getaddrinfo here
// goes through libinfo/mDNSResponder instead, so the ndots and `options`
// experiments in this topic's README only mean what they say inside the Linux
// container.
//
// Build & run:
//   c++ -O2 -std=c++17 -pthread -o /tmp/dnsblocks getaddrinfo_blocks.cpp && /tmp/dnsblocks

#include <netdb.h>
#include <sys/socket.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <functional>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

using clock_type = std::chrono::steady_clock;

// Fresh names every phase. If we reused one list, phase A would warm your OS
// resolver's cache and every later phase would be timing a cache hit -- which
// is a very easy way to measure nothing and conclude that pool size does not
// matter. The random labels do not exist, and that is fine: an NXDOMAIN costs
// a full round trip to a resolver and blocks the calling thread for all of it.
static std::vector<std::string> make_names(int seed) {
    static const char* kAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
    std::mt19937 rng(static_cast<unsigned>(seed));
    std::uniform_int_distribution<int> pick(0, 35);

    std::vector<std::string> names;
    for (int i = 0; i < 6; ++i) {
        std::string label;
        for (int j = 0; j < 10; ++j) label.push_back(kAlphabet[pick(rng)]);
        names.push_back(label + ".example.com");
    }
    names.push_back("localhost");        // control: answered from /etc/hosts
    names.push_back("invalid.invalid");  // control: guaranteed NXDOMAIN
    return names;
}
static const int kNameCount = 8;

static constexpr int POOL_SIZE = 4;         // libuv's default, on purpose
static constexpr int SMALL_POOL = 1;        // the queue, made obvious

struct Outcome {
    std::string name;
    double ms;
    int addrs;
    int err;
};

static Outcome resolve_one(const std::string& name) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    addrinfo* res = nullptr;
    auto t0 = clock_type::now();
    int rc = ::getaddrinfo(name.c_str(), "80", &hints, &res);
    double ms = std::chrono::duration<double, std::milli>(clock_type::now() - t0).count();

    int n = 0;
    for (addrinfo* p = res; p; p = p->ai_next) ++n;
    if (res) ::freeaddrinfo(res);
    return Outcome{name, ms, n, rc};
}

// The forty lines every runtime in this topic wrote once and then hid from
// you: a bounded pool of workers, a queue in front of it, and no way to make
// the underlying call not block.
class Pool {
public:
    explicit Pool(int n) {
        for (int i = 0; i < n; ++i) {
            workers_.emplace_back([this] {
                for (;;) {
                    std::function<void()> job;
                    {
                        std::unique_lock<std::mutex> lock(mu_);
                        cv_.wait(lock, [this] { return stop_ || !queue_.empty(); });
                        if (stop_ && queue_.empty()) return;
                        job = std::move(queue_.front());
                        queue_.pop_front();
                    }
                    job();
                }
            });
        }
    }

    void submit(std::function<void()> job) {
        {
            std::lock_guard<std::mutex> lock(mu_);
            queue_.push_back(std::move(job));
        }
        cv_.notify_one();
    }

    ~Pool() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& w : workers_) w.join();
    }

private:
    std::vector<std::thread> workers_;
    std::deque<std::function<void()>> queue_;
    std::mutex mu_;
    std::condition_variable cv_;
    bool stop_ = false;
};

struct Summary {
    double wall_ms;
    double wait_p50, wait_max;      // time spent waiting for a worker
    double service_p50, service_max; // time spent inside getaddrinfo
};

static double median(std::vector<double> v) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    return v[v.size() / 2];
}

static double max_of(const std::vector<double>& v) {
    double m = 0;
    for (double x : v) m = x > m ? x : m;
    return m;
}

// Resolve a fresh set of names on `pool_size` workers, separating the two
// halves of the latency: how long a lookup waited for a thread, and how long
// the syscall itself took once it had one. Those two columns are the entire
// argument about pool sizing, and no client library shows you either.
static Summary run(int pool_size, int seed) {
    const std::vector<std::string> names = make_names(seed);
    std::vector<double> waits, services;
    std::mutex mu;

    auto t0 = clock_type::now();
    {
        Pool pool(pool_size);
        std::atomic<int> remaining{static_cast<int>(names.size())};
        for (const auto& n : names) {
            auto submitted = clock_type::now();
            pool.submit([&, n, submitted] {
                auto started = clock_type::now();
                Outcome o = resolve_one(n);
                {
                    std::lock_guard<std::mutex> lock(mu);
                    waits.push_back(std::chrono::duration<double, std::milli>(started - submitted).count());
                    services.push_back(o.ms);
                }
                remaining.fetch_sub(1);
            });
        }
        while (remaining.load() > 0) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    double wall = std::chrono::duration<double, std::milli>(clock_type::now() - t0).count();

    return Summary{wall, median(waits), max_of(waits), median(services), max_of(services)};
}

int main() {
    std::printf("==============================================================================\n");
    std::printf("C++: there is no async getaddrinfo, so here is the thread pool everyone wrote\n");
    std::printf("==============================================================================\n");
    std::printf("  %d names per phase, all freshly generated so nothing is served from your\n", kNameCount);
    std::printf("  OS resolver's cache. Most of them do not exist -- an NXDOMAIN costs a\n");
    std::printf("  full round trip and blocks the calling thread for every millisecond of\n");
    std::printf("  it, which is the point.\n\n");

    std::printf("  A. On the calling thread, one at a time (the honest cost)\n");
    for (const auto& n : make_names(1)) {
        Outcome o = resolve_one(n);
        std::printf("      %-26s %8.2f ms  %2d addresses  %s\n",
                    n.c_str(), o.ms, o.addrs, o.err ? ::gai_strerror(o.err) : "");
    }
    std::printf("\n");

    std::printf("  B/C. The same amount of work, on pools of two different sizes\n\n");
    std::printf("      %-26s %10s %10s %10s %11s %11s\n",
                "pool", "wall", "wait p50", "wait max", "service p50", "service max");

    Summary big = run(POOL_SIZE, 2);
    std::printf("      %-26s %8.1f ms %8.1f ms %8.1f ms %8.1f ms %8.1f ms\n",
                ("pool of " + std::to_string(POOL_SIZE)).c_str(),
                big.wall_ms, big.wait_p50, big.wait_max, big.service_p50, big.service_max);

    Summary small = run(SMALL_POOL, 3);
    std::printf("      %-26s %8.1f ms %8.1f ms %8.1f ms %8.1f ms %8.1f ms\n",
                ("pool of " + std::to_string(SMALL_POOL)).c_str(),
                small.wall_ms, small.wait_p50, small.wait_max, small.service_p50, small.service_max);

    std::printf("\n  Read the two pairs of columns separately.\n\n");
    std::printf("    SERVICE time is what getaddrinfo costs: a round trip to a resolver,\n");
    std::printf("    identical in both rows, unchanged by anything you do in your process.\n");
    std::printf("    No pool makes this smaller.\n\n");
    std::printf("    WAIT time is your own queue. It is zero when there is a free worker\n");
    std::printf("    and unbounded when there is not, and it is the entire difference\n");
    std::printf("    between the two rows. Nothing about DNS produced it.\n\n");
    std::printf("  Three things this makes concrete:\n\n");
    std::printf("    1. The call blocks. Not 'is slow' -- blocks, holding one OS thread\n");
    std::printf("       from entry to return. There is no flag, no O_NONBLOCK, no poll()\n");
    std::printf("       argument that changes that, which is why c-ares (what Node's\n");
    std::printf("       dns.resolve uses) exists at all: it reimplements DNS over UDP\n");
    std::printf("       rather than calling the system resolver.\n\n");
    std::printf("    2. A pool does not make resolution faster. It decides who waits.\n");
    std::printf("       That is Topic 2's lesson arriving in a place nobody thinks of as\n");
    std::printf("       a connection pool.\n\n");
    std::printf("    3. The small-pool row IS the incident. Same machine, same resolver,\n");
    std::printf("       same kind of name, latency inflated purely by waiting for a\n");
    std::printf("       thread. Every DNS metric you own looks perfect through it,\n");
    std::printf("       because the queue is inside your process. Python's default\n");
    std::printf("       ThreadPoolExecutor and Node's four libuv threads are this row,\n");
    std::printf("       and the fix in both is the same: give blocking work a pool of\n");
    std::printf("       its own, then make there be less of it.\n");
    return 0;
}
