// Layer 2 · Topic 2 - The pool nobody wrote for you.
//
// C++ is here because it has no pool, no queue, and no timeout unless you
// wrote one. Hand-rolling it takes a condition variable, a bounded container
// and a wait_for with a deadline -- and it forces you to make, explicitly,
// every decision the other five runtimes made for you and did not tell you
// about:
//
//     bounded or unbounded?      requests: unbounded (fails open)
//     block or fail?             httpx: block then raise; Go's sql.DB: block
//     wait forever or time out?  pg.Pool default connectionTimeoutMillis: 0
//                                = forever. SQLAlchemy: 30 s, which is
//                                indistinguishable from forever to a caller.
//     LIFO or FIFO handout?      HikariCP hands back the most recently used
//                                connection on purpose; a FIFO pool keeps
//                                every connection warm and keeps every one of
//                                them alive across an idle period.
//
// Four policies, one identical overload: CLIENTS threads each want a
// connection from a pool of POOL_SIZE, and each holds it for HOLD_MS. By
// Little's Law the work cannot finish faster than
// (CLIENTS / POOL_SIZE) * HOLD_MS regardless of policy -- so what the policy
// changes is not the throughput, it is WHO WAITS, HOW LONG, and WHETHER
// ANYONE IS TOLD.
//
// What to look for in the output:
//   - three things, and only three, can happen when demand exceeds capacity:
//     callers WAIT, the pool EXPANDS, or callers are REFUSED. Every row below
//     is one of those three, and every real client library is too
//   - unbounded: served == CLIENTS, and connections created far above
//     POOL_SIZE. Nobody waits and nobody errors -- the pressure went
//     somewhere else (the database, the fd table), which is exactly what
//     Go's SetMaxOpenConns default of unlimited does
//   - block_forever: served == CLIENTS, max wait ~= the Little's Law floor,
//     zero errors. This is the incident reported as "the service is slow"
//   - timeout_2s vs timeout_300ms: SAME policy, one number apart. The 2 s
//     limit sits above the backlog's natural drain time, so it never fires
//     and the row is identical to block_forever -- a pool timeout longer than
//     your queue takes to drain is decoration. SQLAlchemy ships 30 s
//   - shed_when_full: refusals are immediate and the accepted work has a
//     BOUNDED wait. Only this row changes the shape of the failure
//
// Portability: pure C++17 plus <thread>/<mutex>/<condition_variable>. No
// epoll, no /proc, no cgroups -- builds and runs unchanged on Darwin arm64
// and on Linux.
//
// Build & run:
//   c++ -O2 -std=c++17 -pthread -o /tmp/handpool hand_rolled_pool.cpp && /tmp/handpool

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using clock_type = std::chrono::steady_clock;
using ms = std::chrono::milliseconds;

static constexpr int POOL_SIZE = 4;
static constexpr int CLIENTS   = 40;
static constexpr int HOLD_MS   = 100;

enum class Policy { Unbounded, BlockForever, Timeout, Shed };

// A "connection" is just an id here. Everything interesting about a pool is
// the bookkeeping around handing one out, not what is being handed out.
struct Conn { int id; };

class Pool {
public:
    Pool(Policy policy, int size, ms wait_limit)
        : policy_(policy), size_(size), wait_limit_(wait_limit) {
        for (int i = 0; i < size; ++i) {
            idle_.push_back(Conn{next_id_++});
            ++created_;
        }
    }

    // Returns false when the caller was refused. `waited` is filled in either
    // way, because the wait a refused caller suffered is part of the cost.
    bool acquire(Conn& out, ms& waited) {
        auto t0 = clock_type::now();
        std::unique_lock<std::mutex> lock(mu_);

        auto elapsed = [&] {
            return std::chrono::duration_cast<ms>(clock_type::now() - t0);
        };

        if (idle_.empty()) {
            switch (policy_) {
            case Policy::Unbounded:
                // No limit is itself a configuration choice, and it chooses
                // which component dies: not this one, the one downstream.
                out = Conn{next_id_++};
                ++created_;
                ++overflow_;
                ++served_;
                waited = elapsed();
                return true;

            case Policy::Shed:
                // Refuse now, while there is still time to act on the signal.
                ++refused_;
                waited = elapsed();
                return false;

            case Policy::BlockForever:
                cv_.wait(lock, [&] { return !idle_.empty(); });
                break;

            case Policy::Timeout:
                if (!cv_.wait_for(lock, wait_limit_, [&] { return !idle_.empty(); })) {
                    ++timed_out_;
                    waited = elapsed();
                    return false;   // the typed error httpx raises as PoolTimeout
                }
                break;
            }
        }

        // LIFO handout (back of the deque): the connection most recently
        // returned is the one most likely to still be alive at the other end.
        // Swap to pop_front() and the pool keeps every connection warm
        // instead -- a decision with real consequences once an idle timer at
        // the far end enters the picture (Topic 4).
        out = idle_.back();
        idle_.pop_back();
        ++served_;
        waited = elapsed();
        if (waited > max_wait_) max_wait_ = waited;
        return true;
    }

    void release(Conn c) {
        {
            std::lock_guard<std::mutex> lock(mu_);
            idle_.push_back(c);
        }
        cv_.notify_one();
    }

    int served()   const { return served_; }
    int created()  const { return created_; }
    int overflow() const { return overflow_; }
    int refused()  const { return refused_; }
    int timedout() const { return timed_out_; }
    ms  max_wait() const { return max_wait_; }

private:
    Policy policy_;
    int size_;
    ms wait_limit_;

    std::mutex mu_;
    std::condition_variable cv_;
    std::deque<Conn> idle_;

    int next_id_   = 1;
    int created_   = 0;
    int served_    = 0;
    int overflow_  = 0;
    int refused_   = 0;
    int timed_out_ = 0;
    ms  max_wait_{0};
};

struct Result {
    std::string name;
    int served, refused, timed_out, created, overflow;
    long long max_wait_ms, wall_ms;
};

static Result run(const std::string& name, Policy policy, ms wait_limit) {
    Pool pool(policy, POOL_SIZE, wait_limit);
    std::atomic<int> completed{0};
    std::vector<std::thread> threads;
    threads.reserve(CLIENTS);

    auto t0 = clock_type::now();
    for (int i = 0; i < CLIENTS; ++i) {
        threads.emplace_back([&pool, &completed] {
            Conn c{};
            ms waited{0};
            if (!pool.acquire(c, waited)) return;   // refused or timed out
            std::this_thread::sleep_for(ms(HOLD_MS));
            pool.release(c);
            completed.fetch_add(1);
        });
    }
    for (auto& t : threads) t.join();
    auto wall = std::chrono::duration_cast<ms>(clock_type::now() - t0);

    return Result{name, pool.served(), pool.refused(), pool.timedout(),
                  pool.created(), pool.overflow(),
                  pool.max_wait().count(), wall.count()};
}

int main() {
    const double floor_ms =
        static_cast<double>(CLIENTS) / POOL_SIZE * HOLD_MS;

    std::printf("==============================================================================\n");
    std::printf("One pool, one overload, four policies -- and the policy is not the ceiling\n");
    std::printf("==============================================================================\n");
    std::printf("  pool size %d, %d concurrent clients, each holding a connection %d ms\n",
                POOL_SIZE, CLIENTS, HOLD_MS);
    std::printf("  Little's Law floor for %d clients: %d / %d x %d ms = %.0f ms\n\n",
                CLIENTS, CLIENTS, POOL_SIZE, HOLD_MS, floor_ms);

    std::vector<Result> rs;
    rs.push_back(run("unbounded",      Policy::Unbounded,    ms(0)));
    rs.push_back(run("block_forever",  Policy::BlockForever, ms(0)));
    // Two timeouts on purpose. One is above the queue's natural drain time
    // and one is below it, and the difference between those two rows is the
    // whole reason "we set a pool timeout" is not the same statement as "we
    // bounded our wait".
    rs.push_back(run("timeout_2s",     Policy::Timeout,      ms(2000)));
    rs.push_back(run("timeout_300ms",  Policy::Timeout,      ms(300)));
    rs.push_back(run("shed_when_full", Policy::Shed,         ms(0)));

    std::printf("  %-15s %7s %8s %9s %9s %10s %9s\n",
                "policy", "served", "refused", "timedout", "created", "max wait", "wall");
    for (const auto& r : rs) {
        std::printf("  %-15s %7d %8d %9d %9d %8lld ms %6lld ms\n",
                    r.name.c_str(), r.served, r.refused, r.timed_out,
                    r.created, r.max_wait_ms, r.wall_ms);
    }

    std::printf("\n  Read the table this way:\n");
    std::printf("    The 'created' column is the one that leaves your process. Under\n");
    std::printf("    'unbounded' it is far above the pool size, and every one of those\n");
    std::printf("    extra connections is a real socket arriving at a database whose own\n");
    std::printf("    max_connections you do not control. That is Go's database/sql\n");
    std::printf("    default -- SetMaxOpenConns unlimited -- and it does not remove the\n");
    std::printf("    limit, it relocates the failure into a component that cannot shed.\n\n");
    std::printf("    The 'max wait' column is the one your users feel and your dashboards\n");
    std::printf("    do not show. Under 'block_forever' it approaches the Little's Law\n");
    std::printf("    floor and there are zero errors: a perfectly healthy service, hanging.\n\n");
    std::printf("    Compare the two timeout rows before anything else. Same policy, one\n");
    std::printf("    number apart: 2 s sits above the time this backlog takes to drain, so\n");
    std::printf("    it never fires and that row is block_forever wearing a config option.\n");
    std::printf("    SQLAlchemy's default is 30 s. Set a pool timeout against the queue you\n");
    std::printf("    actually have, or you have set nothing.\n\n");
    std::printf("    Only 'shed_when_full' produces a number in the 'refused' column while\n");
    std::printf("    keeping 'max wait' small. The error rate is the feature. It is the\n");
    std::printf("    only signal that says 'we are at capacity' while there is still time\n");
    std::printf("    to act on it.\n\n");
    std::printf("    And read the wall-time column against the Little's Law floor above,\n");
    std::printf("    not against the other rows. Capacity here is %d connections / %d ms\n", POOL_SIZE, HOLD_MS);
    std::printf("    of hold time, and nothing in this file changes that number. The two\n");
    std::printf("    rows that beat the floor did not beat the ceiling: 'unbounded' ignored\n");
    std::printf("    it by opening sockets outside the pool, and 'shed_when_full' met it by\n");
    std::printf("    refusing most of the work. Wait, expand, or refuse -- there is no\n");
    std::printf("    fourth option, and every client library in this topic has silently\n");
    std::printf("    picked one on your behalf.\n");
    return 0;
}
