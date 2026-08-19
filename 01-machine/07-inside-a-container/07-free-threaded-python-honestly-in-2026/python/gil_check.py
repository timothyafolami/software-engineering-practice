"""
7.7 -- which interpreter am I on, is the GIL actually off, and does it matter
here?

WHAT THIS DEMONSTRATES
  Three things, in the order you should care about them.

  1. THE CHECK YOU MUST DO FIRST, EVERY TIME. Any C extension not marked
     free-thread-safe causes the interpreter to RE-ENABLE the GIL at import,
     with a warning that is easy to miss in a container's log stream. So a
     free-threading benchmark showing no difference is far more often a
     silently re-enabled GIL than a real null result. This script imports
     the modules a real service imports and re-checks after each one, which
     is the only way to find the culprit.

  2. WHETHER THE GIL IS EVEN YOUR PROBLEM. It measures the same work three
     ways -- one thread, N threads CPU-bound, N threads IO-bound -- and
     reports the parallel speedup. On a GIL build the CPU-bound row is ~1x
     no matter how many threads you use; on a free-threaded build it scales.
     The IO-bound row scales on BOTH, which is the point: if your latency is
     a database wait, the GIL was never in the way and removing it will move
     nothing.

  3. WHAT IT COSTS. Single-threaded overhead and per-object memory
     overhead are both real, both version-dependent, and both measurable on
     your own machine in minutes -- so this prints measurements rather than
     quoting a number from a blog post. Run it on both interpreters and
     compare; do not take either figure from a README, including this one.

  What this script deliberately does NOT tell you: whether to migrate. That
  answer depends on which of 7.4's four ceilings currently binds, and the
  honest version of the argument is a process-count argument, not a
  performance one -- one free-threaded process with a thread pool collapses
  N interpreters, N connection pools and N copies of every in-process cache
  into one. Removing the GIL does not remove the quota.

WHAT TO LOOK FOR IN THE OUTPUT
  1. The GIL line at the top, and then the same line again after imports.
     If they differ, an extension re-enabled it, and the culprit is named.
  2. The CPU-bound speedup. On 3.13/3.14 it is ~1x by design. Anything
     dramatically above 1x on a GIL build means the work is not actually
     holding the GIL -- hashlib releases it around large buffers, which is
     exactly why the harness's /cpu endpoint uses hashlib and why "Python
     can't use threads" is too simple a sentence.
  3. The single-threaded cost row, which is what you pay on every request
     whether or not anything is parallel.

RUN
    python3 gil_check.py
    python3.14t gil_check.py           # if you have the free-threaded build
    PYTHON_GIL=0 python3.14t gil_check.py

  This is the one script in this topic that is genuinely useful on the Mac
  host: which interpreter you are on and whether the GIL is enabled is not
  container-specific. Everything with a `cpus:` or a throttle ratio in it
  must run inside the Linux container -- see ../docker/run_7_7.sh.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
import sysconfig
import threading
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "00-harness" / "local"
sys.path.insert(0, str(HARNESS))

import cgroup  # noqa: E402
from openloop import table  # noqa: E402

BLOCK = os.urandom(256 * 1024)


def gil_enabled() -> bool | None:
    """None on an interpreter with no such concept (any build before 3.13)."""
    checker = getattr(sys, "_is_gil_enabled", None)
    return checker() if checker else None


def is_freethreaded_build() -> bool:
    """The BUILD, which is a different question from whether the GIL is on.

    A free-threaded build can be running with the GIL re-enabled -- by an
    extension at import, or by PYTHON_GIL=1 -- and that combination is
    exactly the one that produces a benchmark showing "no difference".
    """
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


# --------------------------------------------------------------- workloads

def pure_python_work(iterations: int) -> int:
    """CPU-bound work that genuinely HOLDS the GIL.

    Pure bytecode: arithmetic on ints, no C library underneath. This is the
    workload the GIL actually serialises, and it is the only honest way to
    measure whether removing it changed anything.
    """
    total = 0
    for i in range(iterations):
        total = (total * 31 + i) & 0xFFFFFFFF
    return total


def hashlib_work(rounds: int) -> str:
    """CPU-bound work that RELEASES the GIL.

    hashlib drops the GIL around each update() of a large buffer, so this
    scales with threads even on a GIL build. Included because it is the
    single most common reason people conclude the GIL is not a problem, or
    that they have already removed it.
    """
    digest = hashlib.sha256()
    for _ in range(rounds):
        digest.update(BLOCK)
    return digest.hexdigest()[:16]


def io_work(seconds: float) -> None:
    """Waiting. The GIL is released across a sleep or a socket read, so this
    has always scaled with threads. If your service's time goes here, the
    GIL was never your bottleneck."""
    time.sleep(seconds)


def measure_scaling(work, threads: int, repeat: int = 1) -> float:
    """Wall time for `threads` threads each doing one unit of `work`."""
    started = time.perf_counter()
    pool = [threading.Thread(target=work) for _ in range(threads * repeat)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    return time.perf_counter() - started


# ----------------------------------------------------------------- imports

# The modules a real FastAPI service in this lab actually imports. Each one
# is checked separately, because the useful output is not "the GIL came
# back" -- it is WHICH import brought it back.
CANDIDATE_IMPORTS = [
    "hashlib",       # stdlib C, always fine
    "json",
    "asyncio",
    "psycopg2",      # C extension. The one to watch in this lab's stack
    "asyncpg",       # C extension (Cython)
    "pydantic_core", # Rust extension
    "uvloop",
    "numpy",
]


def import_audit() -> list[list[str]]:
    rows = []
    for name in CANDIDATE_IMPORTS:
        before = gil_enabled()
        try:
            __import__(name)
            status = "imported"
        except ImportError:
            rows.append([name, "not installed", "-", "-"])
            continue
        except Exception as exc:  # a broken extension is also a result
            rows.append([name, f"failed: {exc.__class__.__name__}", "-", "-"])
            continue
        after = gil_enabled()
        verdict = "ok"
        if before is False and after is True:
            verdict = "*** RE-ENABLED THE GIL ***"
        rows.append([name, status, str(after), verdict])
    return rows


def main() -> None:
    build_freethreaded = is_freethreaded_build()
    gil_before = gil_enabled()

    print("7.7 -- free-threaded Python, honestly")
    print(f"  interpreter        : CPython {sys.version.split()[0]} on "
          f"{platform.system()} {platform.machine()}")
    print(f"  executable         : {sys.executable}")
    print(f"  free-threaded BUILD: {build_freethreaded}"
          f"   (Py_GIL_DISABLED in sysconfig -- the binary, not the state)")
    print(f"  GIL enabled NOW    : {gil_before}"
          f"   (sys._is_gil_enabled() -- the state, not the binary)")
    print(f"  PYTHON_GIL env     : {os.environ.get('PYTHON_GIL', '<unset>')}")
    print()

    if gil_before is None:
        print("  This interpreter predates sys._is_gil_enabled() (3.13+). There is")
        print("  no free-threading question to ask it, and no way for it to answer.")
        print()
    elif build_freethreaded and gil_before:
        print("  !! Free-threaded BUILD with the GIL currently ENABLED.")
        print("  !! Something turned it back on. Any benchmark you run in this state")
        print("  !! measures a GIL build wearing a 't' in its filename -- which is by")
        print("  !! far the most common way a free-threading result comes out null.")
        print()
    elif not build_freethreaded:
        print("  This is a standard GIL build. The CPU-bound row below will be ~1x")
        print("  no matter how many threads it uses, and that is correct, not broken.")
        print("  For the comparison, install python3.14t and run this file again.")
        print()

    # ---- the check that must come first, every time ---------------------
    print("  import audit -- the check to do before believing ANY free-threading")
    print("  benchmark, including your own:")
    print()
    rows = import_audit()
    print(table(rows, ["module", "result", "GIL after", "verdict"]))
    print()

    gil_after = gil_enabled()
    if gil_before is False and gil_after is True:
        print("  *** THE GIL CAME BACK during imports. The row marked above is the")
        print("      culprit. Assert this at startup in real code, or you will not")
        print("      notice -- the warning goes to stderr in a container's log stream:")
        print()
        print("        import sys")
        print('        assert not sys._is_gil_enabled(), "GIL re-enabled -- find the extension"')
        print()
    elif gil_before is False:
        print("  The GIL stayed off through every import above. That is the state a")
        print("  free-threading benchmark has to be in before its numbers mean")
        print("  anything -- assert it at startup anyway, because the set of modules")
        print("  imported in production is not the set imported here.")
        print()

    # ---- does the GIL even matter for THIS workload ---------------------
    threads = min(4, os.cpu_count() or 1)
    quota = cgroup.cpu_quota()

    print(f"  scaling test: 1 thread vs {threads} threads, three workloads")
    # Each thread runs the SAME workload, so the N-thread column does N times
    # the total work in the wall time shown. The speedup is therefore
    # (1-thread ms x N) / (N-thread ms) -- a THROUGHPUT ratio, not a ratio of
    # the two wall times. Say so, because 1.00x next to a 4x-longer wall time
    # reads like a contradiction otherwise.
    print(f"  Each thread does the same work, so the {threads}-thread column does")
    print(f"  {threads}x the total work in the time shown. Speedup below is")
    print(f"  (1-thread ms x {threads}) / ({threads}-thread ms): 1.00x means the extra")
    print("  threads bought no throughput at all, however the wall times compare.")
    print()

    # Calibrate each workload to roughly the same single-threaded duration,
    # so the three rows are comparable. Measured here, never hardcoded.
    mark = time.perf_counter()
    pure_python_work(200_000)
    pure_iterations = int(200_000 * 0.3 / max(1e-6, time.perf_counter() - mark))

    mark = time.perf_counter()
    hashlib_work(8)
    hash_rounds = max(1, int(8 * 0.3 / max(1e-6, time.perf_counter() - mark)))

    workloads = [
        ("pure Python bytecode (HOLDS the GIL)",
         lambda: pure_python_work(pure_iterations)),
        ("hashlib (RELEASES the GIL around each update)",
         lambda: hashlib_work(hash_rounds)),
        ("time.sleep (IO-shaped: always released)",
         lambda: io_work(0.3)),
    ]

    scaling_rows = []
    for label, work in workloads:
        one = measure_scaling(work, 1)
        many = measure_scaling(work, threads)
        speedup = one * threads / many if many > 0 else float("nan")
        scaling_rows.append([label, f"{one * 1000:.0f}", f"{many * 1000:.0f}",
                             f"{speedup:.2f}x"])

    print(table(scaling_rows,
                [f"workload", "1 thread ms", f"{threads} threads ms",
                 "parallel speedup"]))
    print()
    print("  Read row 1 first. That is the only row the GIL was ever in the way of.")
    print("  Row 2 scales on a GIL build too, because hashlib releases the GIL --")
    print("  which is why 'we tested it and threads work fine' is such a common and")
    print("  such a wrong conclusion. Row 3 has always scaled and always will.")
    print()

    # ---- and what it costs ----------------------------------------------
    print("  what free-threading costs, measured here rather than quoted:")
    mark = time.perf_counter()
    pure_python_work(2_000_000)
    single_ms = (time.perf_counter() - mark) * 1000
    print(f"    single-threaded pure-Python work : {single_ms:.0f} ms for 2M iterations")
    print(f"    sys.getsizeof(object())          : {sys.getsizeof(object())} bytes")
    print(f"    sys.getsizeof(1)                 : {sys.getsizeof(1)} bytes")
    print(f"    sys.getsizeof([])                : {sys.getsizeof([])} bytes")
    print("    Run the same line on the other interpreter and subtract. Larger")
    print("    object headers, immortalised interned strings, mimalloc and deferred")
    print("    reclamation all cost memory on a free-threaded build, and both this")
    print("    cost and the single-threaded one have moved between releases -- so")
    print("    take them from your own machine and your own version, never from a")
    print("    blog post, and never from this file.")
    print()

    # ---- the part that is not about the GIL at all -----------------------
    print("  and the number free-threading does NOT change:")
    print(f"    cpu.max enforced : "
          f"{'none (no cgroupfs on this host)' if quota is None else f'{quota:.2f} CPU'}")
    print("    Removing the GIL changes how you SPEND your CPU allowance. It does")
    print("    not enlarge it. A frozen cgroup freezes a free-threaded interpreter")
    print("    exactly as thoroughly, and by putting more runnable threads in one")
    print("    cgroup it can make the throttle ratio WORSE for the same work (7.2).")
    print()
    print("  So the question 'should we move to free-threaded Python' is really the")
    print("  question 'which of 7.4's four ceilings currently binds':")
    print("    * CFS throttling or a Postgres wait -> the GIL was never in the way.")
    print("    * connection count or RSS x N workers -> free-threading helps, and")
    print("      it helps by collapsing PROCESS COUNT, not by being faster.")
    print("    * pure-Python CPU inside one process -> this is the one case the hype")
    print("      is actually about, and it is the rarest of the three in a web service.")


if __name__ == "__main__":
    main()
