"""
Layer 1 - Thread creation vs process creation cost.

A thread shares its address space with its parent: the OS still has to set
up a kernel scheduling entity and a stack, but no new page tables, no new
file descriptor table copy, no new memory mappings. A process (fork/exec)
gets all of that duplicated or freshly built. This times "spawn N, join N"
for both, using the smallest unit of work in each so we're timing creation
overhead, not the work itself.
"""
import multiprocessing
import threading
import time

N = 200  # process creation is slow; keep this small so the demo finishes quickly


def noop():
    pass


def bench_threads():
    start = time.perf_counter()
    for _ in range(N):
        t = threading.Thread(target=noop)
        t.start()
        t.join()
    return time.perf_counter() - start


def bench_processes():
    start = time.perf_counter()
    for _ in range(N):
        p = multiprocessing.Process(target=noop)
        p.start()
        p.join()
    return time.perf_counter() - start


if __name__ == "__main__":
    t_thread = bench_threads()
    t_proc = bench_processes()
    print(f"N={N}")
    print(f"thread spawn+join:   {t_thread:6.3f}s  ({t_thread/N*1e6:7.1f} us/thread)")
    print(f"process spawn+join:  {t_proc:6.3f}s  ({t_proc/N*1e6:7.1f} us/process)")
    print(f"process is {t_proc/t_thread:.1f}x the cost of a thread")
