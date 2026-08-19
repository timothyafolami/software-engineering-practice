"""
Layer 1 - Why `i += 1` is not atomic, even under the GIL.

The GIL guarantees only one thread executes Python bytecode at a time. It
does NOT guarantee that a *statement* like `counter += 1` runs as a single
uninterruptible unit. That statement compiles to several bytecode
instructions (roughly: LOAD_GLOBAL, LOAD_CONST 1, BINARY_OP add,
STORE_GLOBAL), and in principle the interpreter can switch to another
thread between any two of them, losing an update the same way it would in
Go or Rust.

Two experiments below:

1. `increments` -- the textbook version. Worth knowing: on this machine's
   CPython 3.11, it turns out to be very hard to trigger naturally, even
   with an aggressive switch interval. That's not because the race is
   impossible -- it's a real property of the byte-code sequence -- it's
   because CPython's eval-breaker (the "should I release the GIL now?"
   check) tends to land at the loop's backward jump rather than mid
   statement for a loop this tiny, so the read-modify-write triplet
   usually completes as a unit by coincidence of timing, not by guarantee.
   Don't take dependency on that coincidence in real code.

2. `cache_stampede` -- the version that WILL reliably race, because it
   contains an actual function call (and therefore a real GIL-release
   point) between the read and the write. This is the shape almost every
   real Python race takes: "check if cached, and if not, go compute it,"
   with two threads both passing the check before either writes back.
"""
import sys
import threading
import time

N_THREADS = 8
INCREMENTS = 300_000


def run_increments(use_lock: bool) -> int:
    counter = 0
    lock = threading.Lock()

    def worker_unsafe():
        nonlocal counter
        for _ in range(INCREMENTS):
            counter += 1  # LOAD, ADD, STORE -- several separate steps

    def worker_safe():
        nonlocal counter
        for _ in range(INCREMENTS):
            with lock:
                counter += 1

    target = worker_safe if use_lock else worker_unsafe
    threads = [threading.Thread(target=target) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return counter


def run_cache_stampede(use_lock: bool):
    cache = {}
    call_count = [0]
    lock = threading.Lock()

    def compute():
        call_count[0] += 1
        time.sleep(0.002)  # stands in for a real DB query / API call
        return 42

    def get_unsafe(key):
        if key not in cache:
            cache[key] = compute()
        return cache[key]

    def get_safe(key):
        with lock:
            if key not in cache:
                cache[key] = compute()
        return cache[key]

    target = get_safe if use_lock else get_unsafe
    threads = [threading.Thread(target=target, args=("shared-key",)) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return call_count[0]


if __name__ == "__main__":
    sys.setswitchinterval(0.00001)

    expected = N_THREADS * INCREMENTS
    unsafe = run_increments(use_lock=False)
    safe = run_increments(use_lock=True)
    print("-- experiment 1: bare increments --")
    print(f"expected:              {expected}")
    print(f"unsafe (no lock):      {unsafe}  (lost {expected - unsafe})")
    print(f"safe (threading.Lock): {safe}")

    print()
    print("-- experiment 2: check-then-act cache fill (the race that actually bites in practice) --")
    unsafe_calls = run_cache_stampede(use_lock=False)
    safe_calls = run_cache_stampede(use_lock=True)
    print(f"{N_THREADS} threads all requesting the same uncached key")
    print(f"unsafe (no lock): compute() ran {unsafe_calls} times (should be 1)")
    print(f"safe (with lock): compute() ran {safe_calls} times (should be 1)")
