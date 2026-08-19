// 7.2 -- C++: the same hazard with nothing to protect you, and the only
// version in this topic that talks to the kernel directly.
//
// WHAT THIS DEMONSTRATES
//   Every other runtime in this folder has a layer between you and the
//   cgroup: a scheduler that might size itself from the quota, a standard
//   library call that might read cpu.max. C++ has neither. It has
//   std::thread::hardware_concurrency(), which is specified as a *hint*,
//   is allowed to return 0, reports host logical CPUs on every mainstream
//   implementation, and knows nothing about cgroups. So the obvious thread
//   pool -- one worker per hardware_concurrency() -- is maximally wrong
//   under a quota, and being a fast compiled language buys you exactly
//   nothing, because the bucket is drained in CPU-seconds, not
//   instructions.
//
//   The fix is the whole point: read sched_getaffinity(2) and
//   /sys/fs/cgroup/cpu.max yourself. Those twenty lines are all that Go
//   1.25, Rust's available_parallelism() and the JVM's UseContainerSupport
//   are doing on your behalf. Read them here once and the other five stop
//   being magic.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. The header block: hardware_concurrency() next to the affinity count
//      next to the enforced quota. Under `docker run --cpus=1.0` on a
//      4-CPU VM those read 4, 4 and 1.00. Two of the three are lies about
//      what you may consume.
//   2. Row 1 vs row 2: identical offered load, identical quota, only the
//      pool size changes. Throughput is the same -- the quota was always
//      the ceiling -- while the throttle ratio and the heartbeat's largest
//      gap are not.
//   3. The heartbeat. It burns no measurable CPU and is frozen anyway,
//      because throttling dequeues every task in the cgroup, not just the
//      greedy ones.
//
// PORTABILITY
//   Builds and runs on both Linux and macOS. sched_getaffinity(2) and
//   /sys/fs/cgroup exist only on Linux; the Darwin path uses sysctl for
//   the CPU count and mach's task_threads() for the thread census, and
//   prints a FALLBACK banner because there is no quota on this host to
//   enforce -- the bucket below is then a userspace model, not the kernel.
//
// RUN
//   g++ -O2 -std=c++17 -pthread -o /tmp/quota_freeze quota_freeze.cpp && /tmp/quota_freeze

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <sched.h>
#include <unistd.h>
#elif defined(__APPLE__)
#include <mach/mach.h>
#include <sys/sysctl.h>
#endif

#include <time.h>

namespace {

constexpr double kWorkMs = 40.0;       // CPU cost of one "request"
constexpr double kOfferedRate = 9.0;   // req/s -> ~0.36 CPU of demand
constexpr double kRunSeconds = 15.0;
constexpr int kHeartbeatMs = 10;
constexpr long kPeriodUs = 100000;     // the kernel default, and Docker's
constexpr double kChunkMs = 2.0;       // budget check-in granularity
constexpr unsigned kSeed = 20260818;

using Clock = std::chrono::steady_clock;

double now_ms() {
  return std::chrono::duration<double, std::milli>(Clock::now().time_since_epoch())
      .count();
}

// Per-thread CPU time, not wall time. A worker parked by the budget below
// is not consuming CPU, and charging it wall time would bill it for the
// freeze it is already being punished by -- which inverts the whole result.
double thread_cpu_us() {
  struct timespec ts;
  if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts) != 0) return 0.0;
  return ts.tv_sec * 1e6 + ts.tv_nsec / 1e3;
}

// ---------------------------------------------------------------- the kernel

// What the kernel is actually enforcing. Returns CPUs of bandwidth, or -1
// for "no ceiling / no cgroupfs". Twenty lines, no dependencies -- this is
// the function every container-aware runtime has its own copy of.
double read_cpu_max() {
#if defined(__linux__)
  std::ifstream v2("/sys/fs/cgroup/cpu.max");
  if (v2) {
    std::string quota, period;
    v2 >> quota >> period;
    if (quota != "max" && !quota.empty()) {
      long q = std::stol(quota);
      long p = period.empty() ? kPeriodUs : std::stol(period);
      if (p > 0) return static_cast<double>(q) / static_cast<double>(p);
    }
    return -1.0;  // "max <period>": a cgroup exists, with no ceiling
  }
  std::ifstream q1("/sys/fs/cgroup/cpu/cpu.cfs_quota_us");
  std::ifstream p1("/sys/fs/cgroup/cpu/cpu.cfs_period_us");
  long q = 0, p = 0;
  if (q1 >> q && p1 >> p && q > 0 && p > 0)
    return static_cast<double>(q) / static_cast<double>(p);
#endif
  return -1.0;
}

// Which CPUs we may run on. Moves under cpuset.cpus and under nothing else
// -- in particular it does NOT move under --cpus, which is exactly why
// affinity-based sizing is silently wrong in the common case.
int affinity_cpus() {
#if defined(__linux__)
  cpu_set_t set;
  CPU_ZERO(&set);
  if (sched_getaffinity(0, sizeof(set), &set) == 0) return CPU_COUNT(&set);
  return -1;
#else
  return -1;  // no such call on Darwin; the honest answer is "not available"
#endif
}

int host_cpus() {
#if defined(__APPLE__)
  int value = 0;
  size_t len = sizeof(value);
  if (sysctlbyname("hw.logicalcpu", &value, &len, nullptr, 0) == 0) return value;
  return -1;
#elif defined(__linux__)
  long n = sysconf(_SC_NPROCESSORS_ONLN);
  return n > 0 ? static_cast<int>(n) : -1;
#else
  return -1;
#endif
}

// OS threads this process has right now. /proc does not exist on Darwin,
// so the two branches use genuinely different mechanisms.
int thread_census() {
#if defined(__linux__)
  std::ifstream status("/proc/self/status");
  std::string line;
  while (std::getline(status, line)) {
    if (line.rfind("Threads:", 0) == 0) return std::stoi(line.substr(8));
  }
  return -1;
#elif defined(__APPLE__)
  thread_act_array_t threads;
  mach_msg_type_number_t count = 0;
  if (task_threads(mach_task_self(), &threads, &count) != KERN_SUCCESS) return -1;
  for (mach_msg_type_number_t i = 0; i < count; ++i)
    mach_port_deallocate(mach_task_self(), threads[i]);
  vm_deallocate(mach_task_self(), reinterpret_cast<vm_address_t>(threads),
                count * sizeof(thread_act_t));
  return static_cast<int>(count);
#else
  return -1;
#endif
}

// ---------------------------------------------------------------- the budget

// One cgroup's worth of CFS bandwidth. A bucket holding quota_us
// microseconds is refilled every period_us; tasks charge what they burned
// and park when it is empty; every parked task wakes at the next period
// boundary. That single rule is the whole of 7.1 and 7.2.
//
// This is a MODEL, not the kernel. On Linux inside a container the real
// numbers are in /sys/fs/cgroup/cpu.stat and this class is only here so the
// same program produces a comparable table on a host with no cgroupfs.
class CpuBudget {
 public:
  CpuBudget(double quota_cpus, long period_us)
      : quota_us_(static_cast<long>(quota_cpus * period_us)), period_us_(period_us) {
    balance_ = quota_us_;
    refill_ = std::thread([this] { RefillLoop(); });
  }

  ~CpuBudget() {
    {
      std::lock_guard<std::mutex> lock(mu_);
      running_ = false;
    }
    cv_.notify_all();
    if (refill_.joinable()) refill_.join();
  }

  // Charge CPU time already burned, then park if the bucket is empty. The
  // kernel also charges after the fact, in 5ms slices, which is why a real
  // container's usage_usec can slightly overshoot its quota within a period.
  void Spend(double micros) {
    std::unique_lock<std::mutex> lock(mu_);
    balance_ -= static_cast<long>(micros);
    usage_us_ += static_cast<long>(micros);
    while (balance_ <= 0 && running_) {
      froze_this_period_ = true;
      long generation = generation_;
      double frozen_at = now_ms();
      cv_.wait(lock, [&] { return generation_ != generation || !running_; });
      throttled_us_ += static_cast<long>((now_ms() - frozen_at) * 1000.0);
    }
  }

  // For tasks that consume no measurable CPU but are still in the cgroup.
  // The heartbeat calls this: it never spends anything, and is frozen
  // anyway. That asymmetry is what makes a health check fail on a container
  // whose own CPU graph looks calm.
  void ParkIfThrottled() {
    std::unique_lock<std::mutex> lock(mu_);
    while (balance_ <= 0 && running_) {
      long generation = generation_;
      cv_.wait(lock, [&] { return generation_ != generation || !running_; });
    }
  }

  long periods() const { return periods_; }
  long throttled() const { return throttled_; }
  long usage_us() const { return usage_us_; }
  double throttle_ratio() const {
    return periods_ > 0 ? static_cast<double>(throttled_) / periods_ : 0.0;
  }

 private:
  void RefillLoop() {
    auto next = Clock::now();
    while (true) {
      next += std::chrono::microseconds(period_us_);
      std::this_thread::sleep_until(next);
      {
        std::lock_guard<std::mutex> lock(mu_);
        if (!running_) return;
        ++periods_;
        if (froze_this_period_) ++throttled_;
        froze_this_period_ = false;
        balance_ = quota_us_;  // unused quota is LOST, not banked. That is
                               // what cpu.max.burst changes, and it is 0 by
                               // default in both Docker and Kubernetes.
        ++generation_;
      }
      cv_.notify_all();
    }
  }

  const long quota_us_;
  const long period_us_;
  std::mutex mu_;
  std::condition_variable cv_;
  long balance_ = 0;
  long usage_us_ = 0;
  long periods_ = 0;
  long throttled_ = 0;
  long throttled_us_ = 0;   // total time frozen, the throttled_usec column
  long generation_ = 0;
  bool froze_this_period_ = false;
  bool running_ = true;
  std::thread refill_;
};

// ------------------------------------------------------------------ the work

// A deterministic, un-optimisable CPU burn. Not a sleep: a sleeping thread
// spends no quota, and the entire failure mode here is about spending it.
std::vector<uint64_t> g_block(32 * 1024);
std::atomic<uint64_t> g_sink{0};

void init_block() {
  std::mt19937_64 rng(kSeed);
  for (auto& word : g_block) word = rng();
}

uint64_t hash_block(uint64_t seed) {
  uint64_t h = seed ^ 0x9E3779B97F4A7C15ULL;
  for (uint64_t word : g_block) {
    h ^= word;
    h *= 0x100000001B3ULL;
    h = (h << 7) | (h >> 57);
  }
  return h;
}

int g_blocks_per_chunk = 1;   // ~2ms of work: the check-in granularity

// Size ONE CHUNK, not the whole work unit. The chunk is the granularity at
// which a worker checks in with the budget, the same role the kernel's 5ms
// bandwidth slice plays: fine enough that a task cannot burn a whole period
// without noticing, coarse enough that the check-in is not the workload.
//
// Warm up first and time second. The first pass over a 256 KiB block comes
// from DRAM and every later pass from L2 -- Layer 1 Topic 1, arriving here
// as a measurement bug rather than as a lesson.
int calibrate_blocks(double target_ms) {
  uint64_t h = 0;
  for (int i = 0; i < 40; ++i) h = hash_block(h);   // warm-up, not timed
  double start = thread_cpu_us();
  for (int i = 0; i < 40; ++i) h = hash_block(h);
  double per_block_ms = (thread_cpu_us() - start) / 1000.0 / 40.0;
  g_sink.fetch_add(h, std::memory_order_relaxed);
  if (per_block_ms <= 0) return 1;
  return std::max(1, static_cast<int>(target_ms / per_block_ms));
}

// Burn until the thread has actually CONSUMED work_ms of CPU, charging the
// budget between chunks.
//
// Spend-until-consumed rather than a fixed block count, and the reason is
// this machine specifically: an Apple M1 has four performance cores and four
// efficiency cores, so "how many hash blocks is 40ms" has two different
// answers depending on which core the thread landed on. A block count
// calibrated on one core type and executed on the other silently changes the
// offered load, which is the one variable this whole experiment holds fixed.
// Charging real consumed CPU time is immune to that -- and it is also what
// the kernel does, which is the better reason.
void burn_cpu(double work_ms, CpuBudget& budget) {
  uint64_t h = g_sink.load(std::memory_order_relaxed);
  double charged_us = 0.0;
  const double target_us = work_ms * 1000.0;
  while (charged_us < target_us) {
    double start = thread_cpu_us();
    for (int i = 0; i < g_blocks_per_chunk; ++i) h = hash_block(h);
    double spent = thread_cpu_us() - start;
    charged_us += spent;
    budget.Spend(spent);
  }
  g_sink.store(h, std::memory_order_relaxed);
}

// --------------------------------------------------------------- one variant

struct Result {
  int completed = 0;
  double req_per_s = 0;
  double avg_cpu = 0;
  long periods = 0;
  long throttled = 0;
  double p50 = 0;
  double p99 = 0;
  double hb_gap = 0;
  int hb_ticks = 0;
};

double percentile(std::vector<double>& sorted, double p) {
  if (sorted.empty()) return 0.0;
  size_t rank = static_cast<size_t>(p / 100.0 * (sorted.size() - 1) + 0.5);
  return sorted[std::min(rank, sorted.size() - 1)];
}

// Poisson arrivals, not evenly spaced. Throttling at low average
// utilisation is a BURSTINESS effect: the bucket is drained by demand that
// clumps inside one 100ms window, not by demand averaged over a minute.
// Evenly-spaced arrivals cannot reproduce it, which is why hand-rolled load
// loops so reliably fail to find production's tail latency.
std::vector<double> poisson_schedule(double rate, double seconds, unsigned seed) {
  std::mt19937 rng(seed);
  std::exponential_distribution<double> gap(rate);
  std::vector<double> out;
  for (double t = 0.0; t < seconds; t += gap(rng)) out.push_back(t * 1000.0);
  return out;
}

Result run_variant(int pool_size, double quota_cpus) {
  CpuBudget budget(quota_cpus, kPeriodUs);
  std::vector<double> schedule = poisson_schedule(kOfferedRate, kRunSeconds, kSeed);

  std::mutex mu;
  std::condition_variable work_cv;
  std::vector<double> inbox;   // due-times, ms since start
  std::vector<double> latencies;
  bool closed = false;

  double origin = now_ms();

  // The heartbeat: wants to tick every 10ms, spends no measurable CPU, and
  // records the largest gap it actually saw.
  std::atomic<bool> hb_stop{false};
  double hb_max_gap = 0;
  int hb_ticks = 0;
  std::thread heartbeat([&] {
    double last = now_ms();
    while (!hb_stop.load(std::memory_order_relaxed)) {
      std::this_thread::sleep_for(std::chrono::milliseconds(kHeartbeatMs));
      budget.ParkIfThrottled();
      double t = now_ms();
      hb_max_gap = std::max(hb_max_gap, t - last);
      last = t;
      ++hb_ticks;
    }
  });

  std::vector<std::thread> pool;
  for (int i = 0; i < pool_size; ++i) {
    pool.emplace_back([&] {
      while (true) {
        double due;
        {
          std::unique_lock<std::mutex> lock(mu);
          work_cv.wait(lock, [&] { return !inbox.empty() || closed; });
          if (inbox.empty()) return;
          due = inbox.front();
          inbox.erase(inbox.begin());
        }
        burn_cpu(kWorkMs, budget);
        double latency = now_ms() - origin - due;
        std::lock_guard<std::mutex> lock(mu);
        latencies.push_back(latency);
      }
    });
  }

  for (double due : schedule) {
    double wait = due - (now_ms() - origin);
    if (wait > 0)
      std::this_thread::sleep_for(std::chrono::duration<double, std::milli>(wait));
    {
      std::lock_guard<std::mutex> lock(mu);
      inbox.push_back(due);
    }
    work_cv.notify_one();
  }

  // Drain, so the tail is not truncated away by the stopwatch.
  double deadline = now_ms() + 10000.0;
  while (now_ms() < deadline) {
    std::lock_guard<std::mutex> lock(mu);
    if (inbox.empty()) break;
    work_cv.notify_all();
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  std::this_thread::sleep_for(std::chrono::milliseconds(200));

  Result result;
  result.hb_gap = hb_max_gap;
  result.hb_ticks = hb_ticks;
  result.periods = budget.periods();
  result.throttled = budget.throttled();
  double wall_s = (now_ms() - origin) / 1000.0;
  result.avg_cpu = budget.usage_us() / 1e6 / wall_s * 100.0;

  hb_stop.store(true);
  heartbeat.join();
  {
    std::lock_guard<std::mutex> lock(mu);
    closed = true;
  }
  work_cv.notify_all();
  for (auto& thread : pool) thread.join();

  std::sort(latencies.begin(), latencies.end());
  result.completed = static_cast<int>(latencies.size());
  result.req_per_s = result.completed / kRunSeconds;
  result.p50 = percentile(latencies, 50);
  result.p99 = percentile(latencies, 99);
  return result;
}

// ------------------------------------------------------------------- output

void print_table(const std::vector<std::string>& headers,
                 const std::vector<std::vector<std::string>>& rows) {
  std::vector<size_t> widths(headers.size());
  for (size_t i = 0; i < headers.size(); ++i) widths[i] = headers[i].size();
  for (const auto& row : rows)
    for (size_t i = 0; i < row.size(); ++i) widths[i] = std::max(widths[i], row[i].size());

  auto emit = [&](const std::vector<std::string>& cells) {
    for (size_t i = 0; i < cells.size(); ++i)
      std::cout << std::left << std::setw(static_cast<int>(widths[i])) << cells[i]
                << (i + 1 < cells.size() ? "  " : "");
    std::cout << "\n";
  };
  emit(headers);
  std::vector<std::string> rule;
  for (size_t w : widths) rule.emplace_back(w, '-');
  emit(rule);
  for (const auto& row : rows) emit(row);
}

std::string fixed(double value, int places) {
  std::ostringstream out;
  out << std::fixed << std::setprecision(places) << value;
  return out.str();
}

}  // namespace

int main() {
  init_block();
  g_blocks_per_chunk = calibrate_blocks(kChunkMs);

  unsigned hardware = std::thread::hardware_concurrency();
  int affinity = affinity_cpus();
  double quota = read_cpu_max();

  std::cout << "7.2 -- throttled at 30% CPU: C++\n";
#if defined(__linux__)
  std::cout << "  platform               : Linux\n";
#elif defined(__APPLE__)
  std::cout << "  platform               : Darwin (macOS)\n";
#endif
  std::cout << "  hardware_concurrency() : " << hardware
            << "   <- HOST logical CPUs. A hint. May legally be 0.\n";
  std::cout << "  sched_getaffinity count: "
            << (affinity < 0 ? std::string("n/a (Linux-only call)")
                             : std::to_string(affinity))
            << "   <- moves under cpuset, never under --cpus\n";
  std::cout << "  sysconf/sysctl CPUs    : " << host_cpus() << "\n";
  std::cout << "  quota actually enforced: "
            << (quota < 0 ? std::string("none (no cpu.max on this host)")
                          : fixed(quota, 2) + " CPU")
            << "\n";
  std::cout << "  OS threads at rest     : " << thread_census()
            << "   (before this program starts any of its own)\n";
  std::cout << "\n";

  if (quota < 0) {
    std::cout << "  !! FALLBACK: no cpu.max to read on this host\n";
    std::cout << "  !! The bucket below is a userspace MODEL of CFS bandwidth control,\n";
    std::cout << "  !! not the Linux kernel. Real numbers come from\n";
    std::cout << "  !! /sys/fs/cgroup/cpu.stat inside a container.\n\n";
  }

  std::cout << "  one hash block costs " << fixed(kChunkMs / g_blocks_per_chunk, 3)
            << " ms on the calibrating core (measured); " << g_blocks_per_chunk
            << " blocks per " << fixed(kChunkMs, 0) << "ms chunk\n";
  std::cout << "  each work unit burns until it has CONSUMED " << fixed(kWorkMs, 0)
            << "ms of thread CPU time, so a P-core and an E-core do the same\n"
            << "  amount of WORK-per-request rather than the same number of blocks.\n";
  std::cout << "  offered load: " << fixed(kOfferedRate, 0) << " req/s x "
            << fixed(kWorkMs, 0) << "ms CPU = "
            << fixed(kOfferedRate * kWorkMs / 1000.0, 2) << " CPU of demand\n";
  std::cout << "  quota:        1.00 CPU. The demand is comfortably under the limit.\n";
  std::cout << "  heartbeat wants a tick every " << kHeartbeatMs << "ms; "
            << fixed(kRunSeconds, 0) << "s per row\n\n";

  int naive_pool = hardware > 0 ? static_cast<int>(hardware) : 4;

  struct Variant {
    std::string label;
    int pool;
    double quota;
  };
  std::vector<Variant> variants = {
      {"pool = hardware_concurrency() (" + std::to_string(naive_pool) + "), 1.0 CPU",
       naive_pool, 1.0},
      {"fix 1: pool = 1 (the quota), 1.0 CPU", 1, 1.0},
      {"fix 2: pool = " + std::to_string(naive_pool) + ", 2.0 CPU", naive_pool, 2.0},
  };

  std::vector<std::vector<std::string>> rows;
  for (const auto& variant : variants) {
    Result r = run_variant(variant.pool, variant.quota);
    rows.push_back({variant.label, std::to_string(r.completed), fixed(r.req_per_s, 1),
                    fixed(r.avg_cpu, 0) + "%",
                    std::to_string(r.throttled) + "/" + std::to_string(r.periods),
                    fixed(r.periods > 0 ? static_cast<double>(r.throttled) / r.periods : 0.0, 3),
                    fixed(r.p50, 0), fixed(r.p99, 0), fixed(r.hb_gap, 0)});
    std::cout << "  ran: " << variant.label << "\n";
  }

  std::cout << "\n";
  print_table({"variant", "n", "req/s", "avg CPU", "throttled", "ratio", "p50 ms",
               "p99 ms", "hb gap ms"},
              rows);

  std::cout << "\n";
  std::cout << "  Row 1 is what the obvious C++ thread pool does under a quota:\n";
  std::cout << "  hardware_concurrency() workers draining a bucket sized for one.\n";
  std::cout << "  Row 2 changes ONE number and keeps the same throughput, because\n";
  std::cout << "  the quota -- not the pool -- was always the throughput ceiling.\n";
  std::cout << "\n";
  std::cout << "  Nothing in the C++ standard library would have told you this.\n";
  std::cout << "  read_cpu_max() above is twenty lines and no dependencies, and it\n";
  std::cout << "  is exactly what Go 1.25, Rust's available_parallelism() and the\n";
  std::cout << "  JVM's UseContainerSupport do for you in the other five versions.\n";
  return 0;
}
