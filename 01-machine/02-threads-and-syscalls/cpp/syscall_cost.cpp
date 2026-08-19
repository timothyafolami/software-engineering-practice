// Layer 1 - What a syscall actually costs, C++ version.
// read(2) directly via POSIX <unistd.h> -- there is no wrapper at all
// between this code and the kernel, which makes it the most direct
// measurement of raw syscall cost in this whole lab.
#include <chrono>
#include <cstdio>
#include <fcntl.h>
#include <unistd.h>

constexpr long N = 500'000;

double bench_syscall() {
    int fd = open("/dev/zero", O_RDONLY);
    char buf[1];
    auto start = std::chrono::high_resolution_clock::now();
    for (long i = 0; i < N; i++) {
        read(fd, buf, 1);
    }
    auto end = std::chrono::high_resolution_clock::now();
    close(fd);
    return std::chrono::duration<double>(end - start).count();
}

double bench_pure_cpp(volatile long& sink) {
    long total = 0;
    auto start = std::chrono::high_resolution_clock::now();
    for (long i = 0; i < N; i++) {
        total += i & 0xFF;
    }
    auto end = std::chrono::high_resolution_clock::now();
    sink = total; // defeat dead-code elimination, same issue Rust's version hit
    return std::chrono::duration<double>(end - start).count();
}

int main() {
    volatile long sink = 0;
    double t_sys = bench_syscall();
    double t_pure = bench_pure_cpp(sink);
    std::printf("N=%ld\n", N);
    std::printf("read(/dev/zero) x%ld:  %6.3fs  (%6.1f ns/call)\n", N, t_sys, t_sys / N * 1e9);
    std::printf("pure C++ loop:         %6.3fs  (%6.1f ns/iter)\n", t_pure, t_pure / N * 1e9);
    std::printf("syscall is %.1fx the cost of an equivalent pure-C++ step\n", t_sys / t_pure);
    return 0;
}
