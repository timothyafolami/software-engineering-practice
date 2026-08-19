// 7.3 -- C++: no answer at all, and the standard permits it to say so.
//
// WHAT THIS DEMONSTRATES
//   std::thread::hardware_concurrency() is specified as a HINT. The standard
//   allows it to return 0. On every mainstream implementation it reports
//   host logical CPUs, and nothing in the standard library knows what a
//   cgroup is. OpenMP's default team size has the same problem.
//
//   So C++ is reliably wrong here -- and, for exactly that reason, it is the
//   version to read for the mechanism. The fix is to call sched_getaffinity(2)
//   and open("/sys/fs/cgroup/cpu.max") yourself, with no runtime in the way,
//   and then notice that this is ALL any of the other five are doing. Go
//   1.25's container-aware GOMAXPROCS, Rust's available_parallelism(), the
//   JVM's UseContainerSupport, libuv's uv_available_parallelism: every one of
//   them is the forty lines below, wrapped in a nicer name.
//
//   Read those forty lines once and the other five stop being magic.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. Three numbers that answer three different questions, then the
//      enforced one underneath. Inside a container under `--cpus=1.5`,
//      hardware_concurrency() and the affinity count agree with each other
//      and disagree with the kernel.
//   2. Under `--cpuset-cpus=0,1` instead, the affinity count moves and
//      hardware_concurrency() does not -- that is the SECOND column of the
//      README's matrix, and the reason it is a different column.
//   3. The `container_aware_concurrency()` line at the bottom: what a
//      correct C++ program would have used, computed the same way the other
//      runtimes compute it.
//
// PORTABILITY
//   Builds and runs on Linux and macOS. sched_getaffinity(2) and
//   /sys/fs/cgroup are Linux-only; the Darwin branch prints n/a and says why
//   rather than substituting a plausible-looking wrong number, which is the
//   entire failure mode this file is about.
//
// RUN
//   g++ -O2 -std=c++17 -pthread -o /tmp/cpuinfo cpuinfo.cpp && /tmp/cpuinfo
//
//   Inside a Linux container, which is where the columns separate:
//     docker run --rm --cpus=1.5 -v "$PWD:/w" -w /w gcc:14 \
//       sh -c 'g++ -O2 -std=c++17 -pthread -o /tmp/c /w/cpuinfo.cpp && /tmp/c'

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <sched.h>
#include <unistd.h>
#elif defined(__APPLE__)
#include <sys/sysctl.h>
#include <unistd.h>
#endif

namespace {

// ---------------------------------------------------------------- the kernel

// Question (3): how much CPU TIME may I consume per period.
// Returns CPUs of bandwidth, or -1 for "no ceiling / no cgroupfs".
//
// THIS is the function every container-aware runtime has its own copy of. It
// is twenty lines and needs no library.
double read_cpu_max() {
#if defined(__linux__)
  std::ifstream v2("/sys/fs/cgroup/cpu.max");
  if (v2) {
    std::string quota, period;
    v2 >> quota >> period;
    if (quota != "max" && !quota.empty()) {
      long q = std::stol(quota);
      long p = period.empty() ? 100000L : std::stol(period);
      if (p > 0) return static_cast<double>(q) / static_cast<double>(p);
    }
    return -1.0;  // "max <period>": a cgroup exists, with no ceiling set
  }
  // cgroup v1: two files, and -1 spells "unlimited".
  std::ifstream q1("/sys/fs/cgroup/cpu/cpu.cfs_quota_us");
  std::ifstream p1("/sys/fs/cgroup/cpu/cpu.cfs_period_us");
  long q = 0, p = 0;
  if (q1 >> q && p1 >> p && q > 0 && p > 0)
    return static_cast<double>(q) / static_cast<double>(p);
#endif
  return -1.0;
}

// Question (2): which CPUs am I allowed to run on. Moves under cpuset.cpus
// and under nothing else -- in particular NOT under --cpus, which is exactly
// why affinity-based sizing is silently wrong in the common case.
int affinity_cpus() {
#if defined(__linux__)
  cpu_set_t set;
  CPU_ZERO(&set);
  if (sched_getaffinity(0, sizeof(set), &set) == 0) return CPU_COUNT(&set);
#endif
  return -1;
}

// Question (1): how many logical CPUs does the machine have. Not namespaced,
// so inside a container this is the HOST's answer, always.
int host_cpus() {
#if defined(__APPLE__)
  int value = 0;
  size_t len = sizeof(value);
  if (sysctlbyname("hw.logicalcpu", &value, &len, nullptr, 0) == 0) return value;
#elif defined(__linux__)
  long n = sysconf(_SC_NPROCESSORS_ONLN);
  if (n > 0) return static_cast<int>(n);
#endif
  return -1;
}

std::string read_or_na(const char* path) {
#if defined(__linux__)
  std::ifstream file(path);
  if (file) {
    std::string line;
    std::getline(file, line);
    if (!line.empty()) return line;
  }
#else
  (void)path;
#endif
  return "n/a";
}

// The forty lines that make a C++ thread pool correct under a container.
// min(host, affinity, ceil(quota)), floored at 1. Which way to round a
// fractional quota is a real decision -- Go rounds up, and 7.2's table is
// where you find out what that costs.
int container_aware_concurrency() {
  int best = host_cpus();
  if (best <= 0) best = static_cast<int>(std::thread::hardware_concurrency());
  if (best <= 0) best = 1;  // hardware_concurrency() may legally return 0

  int affinity = affinity_cpus();
  if (affinity > 0) best = std::min(best, affinity);

  double quota = read_cpu_max();
  if (quota > 0) best = std::min(best, std::max(1, static_cast<int>(std::ceil(quota))));

  return std::max(1, best);
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

std::string num_or_na(int value) {
  return value < 0 ? "n/a" : std::to_string(value);
}

}  // namespace

int main() {
  unsigned hardware = std::thread::hardware_concurrency();
  int affinity = affinity_cpus();
  int host = host_cpus();
  double quota = read_cpu_max();

  std::cout << "7.3 -- how big is this machine? C++'s (non-)answer\n";
#if defined(__linux__)
  std::cout << "  platform    : Linux\n";
#elif defined(__APPLE__)
  std::cout << "  platform    : Darwin (macOS) -- no sched_getaffinity, no cgroupfs\n";
#endif
  std::cout << "  compiler    : "
#if defined(__clang__)
            << "clang " << __clang_major__ << "." << __clang_minor__
#elif defined(__GNUC__)
            << "gcc " << __GNUC__ << "." << __GNUC_MINOR__
#else
            << "unknown"
#endif
            << ", C++" << (__cplusplus / 100 - 2000) << "\n\n";

  std::ostringstream quota_cell;
  if (quota < 0) {
    quota_cell << "n/a";
  } else {
    quota_cell << std::fixed << std::setprecision(2) << quota;
  }

  print_table(
      {"what people call", "the call", "answer here", "which question it answers",
       "what it tracks"},
      {
          {"hardware_concurrency()", "std::thread::hardware_concurrency()",
           hardware == 0 ? "0 (legal!)" : std::to_string(hardware),
           "(1) how big is the machine", "nothing -- a HINT, host CPUs"},
          {"sched_getaffinity count", "sched_getaffinity(2) + CPU_COUNT",
           num_or_na(affinity), "(2) which CPUs may I use", "cpuset.cpus"},
          {"sysconf/sysctl CPUs", "sysconf(_SC_NPROCESSORS_ONLN)", num_or_na(host),
           "(1) how big is the machine", "the host, always"},
          {"/sys/fs/cgroup/cpu.max", "open() + parse", quota_cell.str(),
           "(3) how much CPU TIME may I consume", "cpu.max -- THE ENFORCED NUMBER"},
      });
  std::cout << "\n";

  std::cout << "  ground truth on this host:\n";
  std::cout << "    cpu.max               " << read_or_na("/sys/fs/cgroup/cpu.max") << "\n";
  std::cout << "    cpuset.cpus.effective "
            << read_or_na("/sys/fs/cgroup/cpuset.cpus.effective") << "\n";
  std::cout << "    memory.max            " << read_or_na("/sys/fs/cgroup/memory.max")
            << "\n\n";

  if (quota < 0) {
    std::cout << "  NOTE: no CPU quota is enforced here, so every row above that has a\n";
    std::cout << "        number agrees and the matrix has one column. That is the\n";
    std::cout << "        correct result on this host. Run it under --cpus=1.5 inside a\n";
    std::cout << "        Linux container and the rows separate.\n\n";
  }

  std::cout << "  What a correct C++ program would use instead:\n";
  std::cout << "    container_aware_concurrency() = " << container_aware_concurrency()
            << "\n";
  std::cout << "      min(host, affinity, ceil(quota)), floored at 1 -- forty lines,\n";
  std::cout << "      no dependencies, defined above. Go 1.25, Rust's\n";
  std::cout << "      available_parallelism(), libuv's uv_available_parallelism() and\n";
  std::cout << "      the JVM's UseContainerSupport are all THIS, wrapped in a name.\n\n";

  std::cout << "  Three things the C++ answer costs you that the others do not:\n";
  std::cout << "    * hardware_concurrency() is permitted to return 0, and code that\n";
  std::cout << "      divides by it is a latent crash on an implementation that does.\n";
  std::cout << "    * OpenMP's default team size is host CPUs too. A parallel-for in a\n";
  std::cout << "      1.0-CPU container starts a team the bucket cannot feed.\n";
  std::cout << "    * being a fast compiled language buys you nothing here. The bucket\n";
  std::cout << "      is drained in CPU-SECONDS, not instructions -- so the faster your\n";
  std::cout << "      threads are, the sooner you are frozen. That is 7.2.\n";
  return 0;
}
