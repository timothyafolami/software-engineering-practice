// Layer 1 - Thread vs process creation cost, C++ version.
// std::thread wraps pthread_create -- a real OS thread, same tier as
// Rust's std::thread. For "process," we go straight to the primitive every
// other language's process-spawn API is eventually built on: fork() +
// waitpid(). fork() duplicates the entire address space (copy-on-write, so
// it's cheaper than it sounds, but still has to duplicate page tables and
// the file descriptor table) -- this is the actual mechanism Python's
// multiprocessing.Process and Go's os/exec eventually bottom out on.
#include <chrono>
#include <cstdio>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

constexpr int N = 200;

double bench_threads() {
    auto start = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < N; i++) {
        std::thread t([] {});
        t.join();
    }
    auto end = std::chrono::high_resolution_clock::now();
    return std::chrono::duration<double>(end - start).count();
}

double bench_fork() {
    auto start = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < N; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            _exit(0); // child: do nothing, exit immediately -- no exec, still the SAME binary
        }
        int status;
        waitpid(pid, &status, 0);
    }
    auto end = std::chrono::high_resolution_clock::now();
    return std::chrono::duration<double>(end - start).count();
}

// fork() alone (above) just copy-on-write duplicates this process's
// address space -- cheap, because nothing is actually copied until
// written to. Every OTHER language's "spawn a process" API in this lab
// (Python's multiprocessing on some platforms, Go's os/exec, Java's
// ProcessBuilder) does fork()+exec(): duplicate, THEN throw the duplicate
// away and load a completely different binary from disk in its place.
// That's a fundamentally heavier operation, and this second benchmark
// isolates exactly how much heavier by doing the same fork()+exec("true")
// every one of those higher-level APIs eventually bottoms out on.
double bench_fork_exec() {
    auto start = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < N; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            execl("/usr/bin/true", "true", (char*)nullptr);
            _exit(127); // only reached if execl fails
        }
        int status;
        waitpid(pid, &status, 0);
    }
    auto end = std::chrono::high_resolution_clock::now();
    return std::chrono::duration<double>(end - start).count();
}

int main() {
    double t_thread = bench_threads();
    double t_fork = bench_fork();
    double t_fork_exec = bench_fork_exec();
    std::printf("N=%d\n", N);
    std::printf("std::thread spawn+join:      %6.3fs  (%7.1f us/thread)\n", t_thread, t_thread / N * 1e6);
    std::printf("fork()+waitpid (no exec):    %6.3fs  (%7.1f us/process)\n", t_fork, t_fork / N * 1e6);
    std::printf("fork()+exec(\"true\")+waitpid: %6.3fs  (%7.1f us/process)\n", t_fork_exec, t_fork_exec / N * 1e6);
    std::printf("bare fork is %.1fx a thread; fork+exec is %.1fx a thread (%.1fx a bare fork)\n",
                t_fork / t_thread, t_fork_exec / t_thread, t_fork_exec / t_fork);
    return 0;
}
