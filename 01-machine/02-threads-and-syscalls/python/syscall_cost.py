"""
Layer 1 - What a syscall actually costs.

read(2) on /dev/zero is about as cheap as a real syscall gets: no disk, no
network, the kernel just memsets your buffer and returns. Comparing it to a
pure userspace loop isolates the cost of the user->kernel->user round trip
itself (mode switch, argument validation, return) from any "real work" the
syscall does.
"""
import os
import time

N = 500_000


def bench_syscall():
    fd = os.open("/dev/zero", os.O_RDONLY)
    buf_size = 10
    start = time.perf_counter()
    for _ in range(N):
        os.read(fd, buf_size)
    elapsed = time.perf_counter() - start
    os.close(fd)
    return elapsed


def bench_pure_python():
    total = 0
    start = time.perf_counter()
    for i in range(N):
        total += i & 0xFF  # comparable amount of "work" per iteration, zero syscalls
    elapsed = time.perf_counter() - start
    return elapsed, total


if __name__ == "__main__":
    t_sys = bench_syscall()
    t_pure, _ = bench_pure_python()
    print(f"N={N:,}")
    print(f"read(/dev/zero) x{N}:  {t_sys:6.3f}s  ({t_sys/N*1e9:6.1f} ns/call)")
    print(f"pure python loop:      {t_pure:6.3f}s  ({t_pure/N*1e9:6.1f} ns/iter)")
    print(f"syscall is {t_sys/t_pure:.1f}x the cost of an equivalent pure-python step")
