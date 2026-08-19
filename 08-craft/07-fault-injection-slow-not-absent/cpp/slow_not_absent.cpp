// Layer 8 Topic 7 - C++: every bound in the fix kit is somebody's code.
//
// WHAT THIS DEMONSTRATES: the same latency ladder the lab runs through
// toxiproxy, in one process, with nothing provided by the language. The
// "database" is a real TCP server on loopback whose service time is a knob. The
// connection pool is a `std::vector<int>` behind a mutex and a condition
// variable, because C++ has no pool. The deadline is `SO_RCVTIMEO`, because C++
// has no deadline. Load is offered at a FIXED ARRIVAL RATE -- a thread per
// scheduled request -- because a closed loop would slow down with the server and
// hide the collapse entirely.
//
// Little's Law is the whole prediction: with a pool of P connections and a
// per-request service time S, throughput through that pool cannot exceed P / S,
// no matter how much load arrives. Phase B raises S by 20x without touching
// anything else, and the queue in front of the pool is unbounded, so the
// observable symptom is not errors -- it is latency with no ceiling.
//
// WHAT TO LOOK FOR: three things, in this order.
//   1. Phase B has an error rate of ZERO. Nothing failed. Everything is late.
//   2. Phase B's p99 is far larger than the injected latency, and the ratio is
//      the amplification. The injected number is printed next to it.
//   3. Phase C is not faster than B in throughput -- it is barely different --
//      but it converts unbounded latency into fast, honest rejection. Recognising
//      that as a win is most of this topic.
//   4. Phase D sets the read deadline BELOW the dependency's service time, so
//      every read breaches. Watch `connections destroyed`: a timed-out read
//      cannot return its connection to the pool, so a deadline that is too short
//      shreds connections, succeeds at nothing, and does not stop the dependency
//      from doing the work anyway.
//
// Every latency below is measured from the request's SCHEDULED arrival time, not
// from when a thread got round to it. Measuring from the start of the work is
// coordinated omission and it erases exactly the effect being measured.
//
//   g++ -std=c++20 -O2 -pthread -o /tmp/t7_cpp cpp/slow_not_absent.cpp && /tmp/t7_cpp

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

// macOS trap: htonl/htons/ntohs are MACROS in <sys/_endian.h>, not functions, so
// `::htonl(...)` does not compile here even though it does on glibc. Called
// unqualified below for that reason.
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

using clk = std::chrono::steady_clock;
using ms = std::chrono::duration<double, std::milli>;

// --- knobs, all in one place so the arithmetic on the page is checkable -------

static constexpr int POOL_SIZE = 5;      // P in Little's Law
static constexpr int ARRIVAL_RPS = 100;  // offered load, open model
static constexpr int WINDOW_MS = 2000;   // how long we offer it for
static constexpr int FAST_MS = 5;        // baseline dependency service time
static constexpr int SLOW_MS = 100;      // the injected fault: slow, not absent

// ============================================================================
// The dependency: a TCP server whose service time is a knob. Not a mock -- a
// real socket, real accept, real blocking read on the client side.
// ============================================================================

class SlowServer {
public:
    bool start() {
        listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        if (listen_fd_ < 0) return false;
        int yes = 1;
        ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = 0;  // let the kernel choose
        if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) return false;
        if (::listen(listen_fd_, 128) < 0) return false;

        socklen_t len = sizeof(addr);
        if (::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len) < 0) return false;
        port_ = ntohs(addr.sin_port);

        acceptor_ = std::thread([this] { accept_loop(); });
        return true;
    }

    void set_latency_ms(int v) { latency_ms_.store(v); }
    int port() const { return port_; }

    void stop() {
        running_.store(false);
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        if (acceptor_.joinable()) acceptor_.join();
        for (std::thread& t : conns_) if (t.joinable()) t.join();
    }

private:
    void accept_loop() {
        while (running_.load()) {
            int fd = ::accept(listen_fd_, nullptr, nullptr);
            if (fd < 0) break;
            conns_.emplace_back([this, fd] { serve(fd); });
        }
    }

    // One thread per connection: the SERVER is not the bottleneck in this
    // experiment, and it must not be, or the finding would be about the server.
    void serve(int fd) {
        char buf[64];
        while (running_.load()) {
            ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
            if (n <= 0) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(latency_ms_.load()));
            if (::send(fd, "ok\n", 3, 0) != 3) break;
        }
        ::close(fd);
    }

    int listen_fd_ = -1;
    int port_ = 0;
    std::atomic<int> latency_ms_{FAST_MS};
    std::atomic<bool> running_{true};
    std::thread acceptor_;
    std::vector<std::thread> conns_;
};

// ============================================================================
// The connection pool. C++ ships no such thing, so here it is: a vector, a
// mutex, a condition variable, and one design decision that is the entire
// topic -- `acquire` either waits forever or takes a timeout.
// ============================================================================

class Pool {
public:
    Pool(int port, int size) : port_(port), size_(size) {}

    // timeout_ms < 0 means WAIT FOREVER, which is SQLAlchemy's default
    // `pool_timeout=None`, HikariCP's absence of a connectionTimeout, and the
    // behaviour every one of these libraries ships with until someone changes it.
    std::optional<int> acquire(int timeout_ms) {
        std::unique_lock<std::mutex> lk(m_);
        auto pred = [this] { return !idle_.empty() || created_ < size_; };
        if (timeout_ms < 0) {
            cv_.wait(lk, pred);
        } else if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms), pred)) {
            return std::nullopt;  // fast, honest failure: the 503 you want
        }
        if (!idle_.empty()) {
            int fd = idle_.back();
            idle_.pop_back();
            return fd;
        }
        created_++;
        lk.unlock();
        int fd = connect_one();
        if (fd < 0) {
            std::lock_guard<std::mutex> g(m_);
            created_--;
            return std::nullopt;
        }
        return fd;
    }

    void release(int fd) {
        {
            std::lock_guard<std::mutex> g(m_);
            idle_.push_back(fd);
        }
        cv_.notify_one();
    }

    // A connection that timed out mid-response is NOT reusable: the reply is
    // still in flight and the next borrower would read it as its own. Throwing
    // it away is correct and is why a read timeout costs a connection.
    void discard(int fd) {
        ::close(fd);
        discarded_.fetch_add(1);
        {
            std::lock_guard<std::mutex> g(m_);
            created_--;
        }
        cv_.notify_one();
    }

    // How many connections this pool destroyed rather than reused. Phase D
    // exists so this number is measured here instead of asserted in a comment.
    int discards() const { return discarded_.load(); }

    void close_all() {
        std::lock_guard<std::mutex> g(m_);
        for (int fd : idle_) ::close(fd);
        idle_.clear();
        created_ = 0;
    }

private:
    int connect_one() const {
        int fd = ::socket(AF_INET, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        int yes = 1;
        ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = htons(static_cast<uint16_t>(port_));
        if (::connect(fd, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) < 0) {
            ::close(fd);
            return -1;
        }
        return fd;
    }

    int port_;
    int size_;
    int created_ = 0;
    std::atomic<int> discarded_{0};
    std::vector<int> idle_;
    std::mutex m_;
    std::condition_variable cv_;
};

// ============================================================================
// Measurement
// ============================================================================

struct Sample {
    double total_ms = 0;      // from SCHEDULED arrival, not from thread start
    double pool_wait_ms = 0;
    double service_ms = 0;    // time the connection was held
    bool ok = false;
    const char* failure = "";
};

static double pct(std::vector<double> v, double p) {
    if (v.empty()) return 0;
    std::sort(v.begin(), v.end());
    size_t i = static_cast<size_t>(p / 100.0 * (v.size() - 1) + 0.5);
    return v[i];
}

struct Phase {
    const char* name;
    int injected_ms;
    int pool_timeout_ms;  // <0 = forever
    int read_timeout_ms;  // 0 = none
};

struct Result {
    int offered = 0, ok = 0, pool_timeouts = 0, read_timeouts = 0;
    double wall_ms = 0, p50 = 0, p99 = 0, max = 0;
    double wait_p50 = 0, wait_p99 = 0, service_mean = 0;
    int discards = 0;   // connections destroyed rather than returned to the pool
    // Worst distance between a request's SCHEDULED arrival and the moment the
    // generator actually dispatched it. If this is large, the load generator
    // itself fell behind and every latency below it is coordinated omission --
    // the same thing k6's `dropped_iterations` tells you.
    double max_lag_ms = 0;
};

static Result run_phase(SlowServer& server, const Phase& ph) {
    server.set_latency_ms(ph.injected_ms);
    Pool pool(server.port(), POOL_SIZE);

    const int total = ARRIVAL_RPS * WINDOW_MS / 1000;
    std::vector<Sample> samples(static_cast<size_t>(total));
    std::vector<std::thread> workers;
    workers.reserve(static_cast<size_t>(total));

    const auto t_start = clk::now();
    const auto gap = std::chrono::microseconds(1000000 / ARRIVAL_RPS);
    double max_lag = 0;

    for (int i = 0; i < total; ++i) {
        // OPEN MODEL: request i is scheduled at t_start + i*gap and is dispatched
        // then, whether or not request i-1 has finished. This is the difference
        // between k6's constant-arrival-rate and constant-vus, and it is the
        // single most common way to get a null result from this experiment.
        const auto due = t_start + gap * i;
        std::this_thread::sleep_until(due);
        // Measured BEFORE the thread is spawned, so thread-creation cost lands
        // in the next iteration's lag rather than being hidden inside it.
        const double lag = ms(clk::now() - due).count();
        if (lag > max_lag) max_lag = lag;

        workers.emplace_back([&, i, due] {
            Sample& s = samples[static_cast<size_t>(i)];
            const auto acq_start = clk::now();
            std::optional<int> fd = pool.acquire(ph.pool_timeout_ms);
            s.pool_wait_ms = ms(clk::now() - acq_start).count();

            if (!fd) {
                s.failure = "pool timeout";
                s.total_ms = ms(clk::now() - due).count();
                return;
            }
            if (ph.read_timeout_ms > 0) {
                // THE DEADLINE, C++ style: a syscall argument you pass yourself.
                // There is no ambient context, no cancellation token, and nothing
                // will apply one for you.
                timeval tv{};
                tv.tv_sec = ph.read_timeout_ms / 1000;
                tv.tv_usec = (ph.read_timeout_ms % 1000) * 1000;
                ::setsockopt(*fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            }

            const auto svc_start = clk::now();
            char buf[64];
            bool ok = ::send(*fd, "GET\n", 4, 0) == 4;
            if (ok) {
                ssize_t n = ::recv(*fd, buf, sizeof(buf), 0);
                ok = n > 0;
                if (!ok) s.failure = "read timeout";
            }
            s.service_ms = ms(clk::now() - svc_start).count();
            s.ok = ok;
            if (ok) pool.release(*fd); else pool.discard(*fd);
            s.total_ms = ms(clk::now() - due).count();
        });
    }
    for (std::thread& t : workers) t.join();
    const double wall = ms(clk::now() - t_start).count();

    Result r;
    r.offered = total;
    r.wall_ms = wall;
    r.max_lag_ms = max_lag;
    r.discards = pool.discards();
    std::vector<double> lat, wait, svc;
    for (const Sample& s : samples) {
        lat.push_back(s.total_ms);
        wait.push_back(s.pool_wait_ms);
        if (s.ok) {
            r.ok++;
            svc.push_back(s.service_ms);
        } else if (std::strcmp(s.failure, "pool timeout") == 0) {
            r.pool_timeouts++;
        } else {
            r.read_timeouts++;
        }
    }
    r.p50 = pct(lat, 50);
    r.p99 = pct(lat, 99);
    r.max = pct(lat, 100);
    r.wait_p50 = pct(wait, 50);
    r.wait_p99 = pct(wait, 99);
    double sum = 0;
    for (double v : svc) sum += v;
    r.service_mean = svc.empty() ? 0 : sum / static_cast<double>(svc.size());

    pool.close_all();
    return r;
}

static void print_row(const char* label, const Phase& ph, const Result& r) {
    const double rps = r.ok / (r.wall_ms / 1000.0);
    std::printf("  %-26s %7d %7d %8.0f %8.0f %8.0f %9.0f %8.1f\n", label, r.offered, r.ok, rps,
                r.p50, r.p99, r.wait_p99, r.service_mean);
    std::printf("  %-26s injected %d ms | pool timeouts %d | read timeouts %d | "
                "errors %.1f%% | wall %.1f s | generator max lag %.0f ms | "
                "connections destroyed %d\n",
                "", ph.injected_ms, r.pool_timeouts, r.read_timeouts,
                100.0 * (r.offered - r.ok) / r.offered, r.wall_ms / 1000.0, r.max_lag_ms,
                r.discards);
}

int main() {
    SlowServer server;
    if (!server.start()) {
        std::fprintf(stderr, "could not start the loopback dependency\n");
        return 1;
    }
    std::printf("Layer 8 topic 7 - C++: nothing is provided. Every bound below is a "
                "data structure\nor a syscall argument in this file.\n\n");
    std::printf("  dependency: 127.0.0.1:%d, one thread per connection, service time is a knob\n",
                server.port());
    std::printf("  pool size P = %d      offered load = %d rps (open model, thread per arrival)\n",
                POOL_SIZE, ARRIVAL_RPS);
    std::printf("  offer window = %d ms  -> %d requests per phase\n\n", WINDOW_MS,
                ARRIVAL_RPS * WINDOW_MS / 1000);

    std::printf("  %-26s %7s %7s %8s %8s %8s %9s %8s\n", "phase", "offered", "ok", "rps", "p50 ms",
                "p99 ms", "wait p99", "svc mean");
    std::printf("  %s\n", std::string(96, '-').c_str());

    const Phase a{"A baseline", FAST_MS, -1, 0};
    const Result ra = run_phase(server, a);
    print_row("A baseline, fast dep", a, ra);

    const Phase b{"B slow, unbounded", SLOW_MS, -1, 0};
    const Result rb = run_phase(server, b);
    print_row("B slow dep, no bounds", b, rb);

    const Phase c{"C slow, bounded", SLOW_MS, 250, 500};
    const Result rc = run_phase(server, c);
    print_row("C slow dep + bounds", c, rc);

    // Phase D exists for one reason: in C the read deadline (500 ms) is longer
    // than the service time (~100 ms), so it never fires and `Pool::discard` is
    // never called. A claim the output does not exercise is a claim the reader
    // has to take on trust. Here the deadline is set BELOW the service time, so
    // every read breaches, and the price shows up as a measured number.
    const Phase d{"D deadline under S", SLOW_MS, 250, 60};
    const Result rd = run_phase(server, d);
    print_row("D read deadline < S", d, rd);

    server.stop();

    // ---- the arithmetic, derived here from the numbers above ----------------
    std::printf("\nLITTLE'S LAW, worked from the measurements above\n");
    const double sa = ra.service_mean / 1000.0, sb = rb.service_mean / 1000.0;
    std::printf("  phase A: P / S = %d / %.4f s = %.0f rps of pool capacity, against %d rps offered\n",
                POOL_SIZE, sa, sa > 0 ? POOL_SIZE / sa : 0.0, ARRIVAL_RPS);
    std::printf("  phase B: P / S = %d / %.4f s = %.0f rps of pool capacity, against %d rps offered\n",
                POOL_SIZE, sb, sb > 0 ? POOL_SIZE / sb : 0.0, ARRIVAL_RPS);
    std::printf("  Nothing else changed. The pool did not shrink and the load did not rise.\n");

    std::printf("\nWHAT ACTUALLY HAPPENED\n");
    std::printf("  A -> B  p99 went %.0f ms -> %.0f ms while the dependency got %d ms slower.\n",
                ra.p99, rb.p99, SLOW_MS - FAST_MS);
    std::printf("          amplification: %.1fx the injected delay, and it is not proportional --\n",
                rb.p99 / (SLOW_MS > 0 ? SLOW_MS : 1));
    std::printf("          it is the queue in front of a pool that has no ceiling.\n");
    std::printf("  A -> B  error rate: %.1f%% -> %.1f%%. Nothing failed. Everything is late,\n",
                100.0 * (ra.offered - ra.ok) / ra.offered,
                100.0 * (rb.offered - rb.ok) / rb.offered);
    std::printf("          which is why an errors-only dashboard and an errors-only circuit\n");
    std::printf("          breaker both sit this incident out.\n");
    std::printf("  B -> C  throughput %.0f rps -> %.0f rps. The bound bought NO throughput.\n",
                rb.ok / (rb.wall_ms / 1000.0), rc.ok / (rc.wall_ms / 1000.0));
    std::printf("          What it bought is p99 %.0f ms -> %.0f ms and %d fast rejections.\n",
                rb.p99, rc.p99, rc.pool_timeouts + rc.read_timeouts);
    std::printf("          Unbounded latency became honest failure. That is the win.\n");

    std::printf("\nTHE C++ POINT\n");
    std::printf("  `Pool::acquire(timeout_ms)` is thirty lines and one `wait_for`.\n");
    std::printf("  The deadline is `SO_RCVTIMEO`, a `timeval` handed to `setsockopt`.\n");
    std::printf("  Neither exists unless someone writes it. Your framework HAS a default\n");
    std::printf("  for both, which means somebody made a choice you have not read.\n");
    std::printf("  Note `Pool::discard`: a connection abandoned mid-response cannot go back\n");
    std::printf("  in the pool, because the reply is still in flight. That cost is phase D:\n");
    std::printf("    C  read deadline %d ms > service %.0f ms -> read timeouts %d, destroyed %d\n",
                c.read_timeout_ms, rc.service_mean, rc.read_timeouts, rc.discards);
    std::printf("    D  read deadline %d ms < service %.0f ms -> read timeouts %d, destroyed %d\n",
                d.read_timeout_ms, rb.service_mean, rd.read_timeouts, rd.discards);
    std::printf("  D bought nothing at all: %d of %d requests succeeded, and every failure\n",
                rd.ok, rd.offered);
    std::printf("  cost a TCP connection the pool then had to rebuild. A deadline shorter than\n");
    std::printf("  the dependency's service time is not a bound, it is a connection shredder --\n");
    std::printf("  and the dependency did the work anyway, because nothing in TCP cancels it.\n");
    return 0;
}
