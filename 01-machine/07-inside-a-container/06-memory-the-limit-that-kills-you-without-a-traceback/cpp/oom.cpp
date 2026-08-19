// 7.6 -- C++: the sharpest illustration on this page.
//
// WHAT THIS DEMONSTRATES
//   `new` is specified to throw std::bad_alloc on failure, so the obvious
//   defensive code is a try/catch around the allocation. This program writes
//   exactly that code -- and then prints, at the end, how many times the
//   catch block ran.
//
//   Under Linux's default overcommit it never runs. malloc returns a valid
//   pointer, the kernel commits nothing, and the cgroup charge lands when
//   you first WRITE to the page. So the failure arrives on a memory store,
//   arbitrarily far from the allocation, delivered as SIGKILL -- and you are
//   dead inside a memcpy, not inside your catch.
//
//   The language HAS the error path. The kernel's policy means you never
//   reach it. Reading that empty catch block is the most direct way to
//   internalise "the limit that kills you without a traceback", and it is
//   why C++ is the version worth writing this experiment in even though
//   nothing about it is C++-specific.
//
//   To make the contrast concrete, the program separates the two moments:
//
//     --reserve-only   allocate 4x the container limit and DO NOT touch it.
//                      Watch it succeed. Nothing is charged; nothing dies.
//     (default)        allocate and touch. Watch it die on the touch.
//
//   That pair is the entire difference between virtual address space and
//   resident memory, and it is the reason `top`'s VIRT column has misled a
//   generation of engineers.
//
//   (`vm.overcommit_memory=2` changes the policy and is worth knowing
//   exists. It is host-wide, with host-wide consequences, and it is not
//   something you set to fix one container.)
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. In --reserve-only mode: gigabytes "allocated" with RSS flat. The
//      allocation genuinely succeeded and cost nothing.
//   2. In the default mode: the last line before the process disappears,
//      and the fact that "bad_alloc caught: 0" is never printed at all,
//      because the process does not survive to print its own summary.
//   3. The atexit hook and the destructor. Both will run on a normal exit.
//      Neither runs on SIGKILL.
//
// PORTABILITY
//   Builds and runs on Linux and macOS. Darwin has no cgroup memory
//   controller, no memory.events and no cgroup OOM killer, so with no limit
//   to read the program imposes its own ceiling and says clearly that it
//   stopped itself rather than being killed. The RSS reading uses
//   /proc/self/status on Linux and mach's task_info on Darwin.
//
// RUN
//   g++ -O2 -std=c++17 -o /tmp/oom oom.cpp && /tmp/oom
//
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w gcc:14 \
//     sh -c 'g++ -O2 -std=c++17 -o /tmp/oom /w/oom.cpp && /tmp/oom'
//   echo "exit code: $?"      # 137
//   docker run --rm --memory=256m -v "$PWD:/w" -w /w gcc:14 \
//     sh -c 'g++ -O2 -std=c++17 -o /tmp/oom /w/oom.cpp && /tmp/oom --reserve-only'

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <new>
#include <string>
#include <vector>

#if defined(__APPLE__)
#include <mach/mach.h>
#endif

namespace {

constexpr size_t kChunkMB = 8;

// ---------------------------------------------------------------- the kernel

// Bytes the cgroup will let this container charge, or 0 for no limit.
long long memory_max() {
  std::ifstream v2("/sys/fs/cgroup/memory.max");
  if (v2) {
    std::string raw;
    v2 >> raw;
    if (raw != "max" && !raw.empty()) return std::stoll(raw);
    return 0;
  }
  std::ifstream v1("/sys/fs/cgroup/memory/memory.limit_in_bytes");
  long long value = 0;
  // v1 spells "unlimited" as a number near 2^63, not as a word.
  if (v1 >> value && value < (1LL << 62)) return value;
  return 0;
}

std::string read_or(const char* path, const char* fallback) {
  std::ifstream file(path);
  if (!file) return fallback;
  std::string all, line;
  while (std::getline(file, line)) {
    if (!all.empty()) all += " ";
    all += line;
  }
  return all.empty() ? fallback : all;
}

// Current RSS in MiB. /proc does not exist on Darwin, so the two branches
// use genuinely different mechanisms rather than one that quietly returns a
// wrong number on the second platform.
double rss_mb() {
#if defined(__linux__)
  std::ifstream status("/proc/self/status");
  std::string line;
  while (std::getline(status, line)) {
    if (line.rfind("VmRSS:", 0) == 0)
      return std::stod(line.substr(6)) / 1024.0;
  }
  return -1.0;
#elif defined(__APPLE__)
  mach_task_basic_info info;
  mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
  if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                reinterpret_cast<task_info_t>(&info), &count) != KERN_SUCCESS)
    return -1.0;
  return static_cast<double>(info.resident_size) / (1024.0 * 1024.0);
#else
  return -1.0;
#endif
}

int g_bad_alloc_caught = 0;

// A destructor, so you can watch it not run.
struct Farewell {
  ~Farewell() {
    std::cout << "  [destructor] returning from main normally -- RSS "
              << std::fixed << std::setprecision(0) << rss_mb() << " MiB\n";
  }
};

void at_exit_hook() {
  std::cout << "  [atexit] process exiting normally. bad_alloc caught: "
            << g_bad_alloc_caught << " time(s)\n";
}

// A signal handler that logs. It will run for SIGTERM. It will never run for
// SIGKILL, and that one sentence is the whole of this sub-topic.
void on_signal(int signum) {
  std::cout << "  [signal handler] caught signal " << signum
            << " -- shutting down cleanly\n";
  std::exit(128 + signum);
}

}  // namespace

int main(int argc, char** argv) {
  Farewell farewell;
  std::atexit(at_exit_hook);
  std::signal(SIGTERM, on_signal);
  std::signal(SIGINT, on_signal);

  bool reserve_only = false;
  size_t self_limit_mb = 512;
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--reserve-only") reserve_only = true;
    if (arg == "--limit-mb" && i + 1 < argc) self_limit_mb = std::stoul(argv[++i]);
  }

  long long limit = memory_max();

  std::cout << "7.6 -- memory: C++\n";
#if defined(__linux__)
  std::cout << "  platform     : Linux\n";
#elif defined(__APPLE__)
  std::cout << "  platform     : Darwin (macOS) -- no cgroup memory controller\n";
#endif
  std::cout << "  memory.max   : "
            << (limit == 0 ? std::string("no limit / no cgroupfs")
                           : std::to_string(limit / (1 << 20)) + " MiB")
            << "\n";
  std::cout << "  memory.high  : " << read_or("/sys/fs/cgroup/memory.high", "unset")
            << "   <- degrades instead of killing; no Compose key\n";
  std::cout << "  heap ceiling : none. new goes to the system allocator and that is all.\n";
  std::cout << "  starting RSS : " << std::fixed << std::setprecision(0) << rss_mb()
            << " MiB\n\n";

  std::cout << "  installed, and about to be shown useless:\n";
  std::cout << "    * try/catch (std::bad_alloc) around every allocation\n";
  std::cout << "    * a SIGTERM handler that logs\n";
  std::cout << "    * an atexit hook\n";
  std::cout << "    * a destructor on a stack object in main\n\n";

  size_t ceiling_mb;
  if (limit == 0) {
    std::cout << "  !! No cgroup memory limit on this host, so nothing can OOM-kill\n";
    std::cout << "  !! this process. It will stop ITSELF at " << self_limit_mb
              << " MiB and say so.\n";
    std::cout << "  !! For the kill:\n";
    std::cout << "  !!   docker run --rm --memory=256m -v \"$PWD:/w\" -w /w gcc:14 \\\n";
    std::cout << "  !!     sh -c 'g++ -O2 -std=c++17 -o /tmp/oom /w/oom.cpp && /tmp/oom'\n\n";
    ceiling_mb = self_limit_mb;
  } else {
    ceiling_mb = static_cast<size_t>(limit / (1 << 20) * (reserve_only ? 4 : 1.5));
    std::cout << "  Allocating toward " << ceiling_mb << " MiB against a "
              << (limit / (1 << 20)) << " MiB limit.\n\n";
  }

  if (reserve_only) {
    std::cout << "  mode: --reserve-only. Allocating and NOT touching.\n";
    std::cout << "  Under default overcommit the kernel commits nothing until a page\n";
    std::cout << "  is written, so this should sail past the limit with RSS flat.\n\n";
  } else {
    std::cout << "  mode: allocate AND touch. The touch is what gets charged, and\n";
    std::cout << "  therefore what gets you killed.\n\n";
  }

  std::vector<char*> blocks;
  size_t allocated_mb = 0;

  while (allocated_mb < ceiling_mb) {
    char* block = nullptr;
    try {
      // The defensive code every careful C++ engineer writes. Count how
      // many times the handler below actually runs.
      block = new char[kChunkMB << 20];
    } catch (const std::bad_alloc& err) {
      ++g_bad_alloc_caught;
      std::cout << "\n  bad_alloc CAUGHT after " << allocated_mb << " MiB: "
                << err.what() << "\n";
      std::cout << "  The ALLOCATOR refused. That is a different event from a cgroup\n";
      std::cout << "  OOM kill, which never gives the allocator a chance to refuse.\n";
      std::cout << "  If you are in a container and reached this line, check for an\n";
      std::cout << "  RLIMIT_AS (ulimit -v) or vm.overcommit_memory=2.\n";
      break;
    }

    if (!reserve_only) {
      // Touch every page. One byte per 4 KiB is enough -- the charge is per
      // page, not per byte. THIS is the line the kernel kills you on.
      for (size_t offset = 0; offset < (kChunkMB << 20); offset += 4096)
        block[offset] = 1;
    }
    blocks.push_back(block);
    allocated_mb += kChunkMB;

    if (allocated_mb % 64 == 0 ||
        (limit != 0 && static_cast<long long>(allocated_mb) << 20 > limit * 8 / 10)) {
      std::cout << "    allocated " << std::setw(5) << allocated_mb << " MiB   RSS "
                << std::setw(6) << std::fixed << std::setprecision(0) << rss_mb()
                << " MiB   memory.events: "
                << read_or("/sys/fs/cgroup/memory.events", "n/a") << std::endl;
    }
  }

  std::cout << "\n  Reached " << allocated_mb << " MiB. bad_alloc caught "
            << g_bad_alloc_caught << " time(s).\n";

  if (reserve_only) {
    std::cout << "  Note the RSS column against the allocated column. The gap between\n";
    std::cout << "  them is memory that exists in your address space and nowhere else.\n";
    std::cout << "  That gap is why `top`'s VIRT column has misled a generation of\n";
    std::cout << "  engineers, and why an allocation succeeding tells you nothing\n";
    std::cout << "  about whether you have the memory.\n";
  } else if (limit == 0) {
    std::cout << "  Expected: no cgroup here to kill anything, and the self-imposed\n";
    std::cout << "  ceiling stopped the loop. Nothing was enforced.\n";
  } else {
    std::cout << "  NOT expected under a memory limit while touching every page. The\n";
    std::cout << "  kernel reclaimed enough to keep up, or memory.high is set.\n";
  }

  std::cout << "\n  If bad_alloc was caught 0 times and the process is still alive, the\n";
  std::cout << "  catch block above is decoration. In a container it is decoration\n";
  std::cout << "  ALWAYS: under default overcommit the allocator does not fail, the\n";
  std::cout << "  kernel kills you on a store instruction, and SIGKILL cannot be\n";
  std::cout << "  caught, blocked or handled by anything -- including everything this\n";
  std::cout << "  program installed at the top.\n";

  for (char* block : blocks) delete[] block;
  return 0;
}
