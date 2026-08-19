"""
7.6 -- Python: there is no heap ceiling, so the OOM killer is your heap limit.

WHAT THIS DEMONSTRATES
  CPython's allocator keeps asking the OS for arenas until the OS stops
  answering, and inside a container the OS stops answering by killing you.
  Nothing in the interpreter reads memory.max. There is no MemoryError to
  catch, no atexit hook, no shutdown log line, no last-gasp metric --
  SIGKILL cannot be caught, blocked or handled, and the process is gone
  between one bytecode and the next.

  This program installs, on purpose, every piece of error handling a
  careful engineer would reach for:

    * try/except MemoryError around the allocation
    * a SIGTERM handler that logs
    * an atexit hook
    * a finally block

  and then allocates until the cgroup kills it. NONE of them run. Reading
  that list of things that did not happen is the point of the exercise --
  the language has the error paths, and the kernel's policy means you never
  reach them.

  Two Python specifics worth knowing while you watch RSS climb:
    * Under Linux's default overcommit, the allocation SUCCEEDS. The charge
      happens when you touch the page, so the failure lands on a memory
      write, arbitrarily far from the allocation.
    * Freed objects go back to pymalloc's pools and arenas, and arenas are
      returned to the OS only when completely empty. RSS is sticky: a burst
      that briefly needed 300MB leaves a process that looks like it needs
      300MB. The --free phase demonstrates that.

WHAT TO LOOK FOR IN THE OUTPUT
  1. The last line printed before the process disappears. There is no
     traceback after it, because there is no exception -- compare that with
     what nodejs/oom.js and java/Oom.java print at the same moment.
  2. `memory.events`' oom_kill counter, read from the shell afterwards. The
     evidence exists; it is just not in your application's logs.
  3. The exit code: 137. Derive it rather than memorise it -- a shell
     reports a signal-terminated process as 128 + signal, and SIGKILL is 9.
  4. In the --free phase, RSS after freeing everything. It does not return
     to where it started.

RUN
    # the real thing: inside a Linux container, with a real cgroup
    docker run --rm --memory=256m -v "$PWD:/w" -w /w python:3.13-slim python oom.py
    echo "exit code: $?"        # 137

    # on this Mac, where there is no cgroup memory controller at all
    python3 oom.py

  On macOS there is no memory.max, no memory.events and no cgroup OOM
  killer. Allocating until something happens would page, swap, and
  eventually annoy you -- a different experiment with a different lesson.
  So on a host with no cgroup limit this program imposes its OWN ceiling
  (--limit-mb, default 512) and stops there, saying clearly that it stopped
  itself and was not killed. That distinction is the whole sub-topic.
"""
from __future__ import annotations

import argparse
import atexit
import os
import platform
import resource
import signal
import sys
import time
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "00-harness" / "local"
sys.path.insert(0, str(HARNESS))

import cgroup  # noqa: E402

CHUNK_MB = 8


HAVE_CURRENT_RSS = Path("/proc/self/status").exists()


def rss_mb() -> float:
    """Resident set size in MiB -- and NOT the same reading on both platforms.

    /proc/self/status VmRSS is the CURRENT resident set. Darwin has no
    /proc, and getrusage's ru_maxrss is the PEAK, which never goes down.
    Two different questions wearing one function name is exactly the sort of
    thing that produces a confidently wrong memory dashboard, so
    HAVE_CURRENT_RSS is exported and the --free phase below refuses to draw
    a conclusion from a peak.

    (ru_maxrss is also in KiB on Linux and BYTES on macOS. That units
    difference has produced its own share of wrong dashboards.)
    """
    if HAVE_CURRENT_RSS:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def report_and_die(signum, _frame):
    """A SIGTERM handler that logs. It will run for SIGTERM and it will
    never run for SIGKILL, which is the entire difference between a
    graceful shutdown and an OOM kill."""
    print(f"  [signal handler] caught {signal.Signals(signum).name} -- "
          "shutting down cleanly", flush=True)
    raise SystemExit(128 + signum)


@atexit.register
def farewell() -> None:
    """An atexit hook. Runs on a normal exit, on SystemExit, on an uncaught
    exception. Does not run on SIGKILL. If you see this line after an OOM
    kill, you were not OOM-killed."""
    print(f"  [atexit] final RSS {rss_mb():.0f} MiB -- process exiting normally",
          flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="7.6 -- the limit that kills without a traceback")
    parser.add_argument("--limit-mb", type=int, default=512,
                        help="self-imposed ceiling when there is no cgroup (default 512)")
    parser.add_argument("--free", action="store_true",
                        help="also demonstrate that RSS does not come back down")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, report_and_die)

    limit = cgroup.memory_limit()
    high = cgroup.memory_high()
    host = cgroup.host_memory_bytes()
    events_before = cgroup.memory_events()

    print("7.6 -- memory: Python")
    print(f"  interpreter          : CPython {sys.version.split()[0]} on "
          f"{platform.system()} {platform.machine()}")
    print(f"  memory.max           : "
          f"{'no limit / no cgroupfs' if limit is None else f'{limit / 2**20:.0f} MiB'}")
    print(f"  memory.high          : "
          f"{'unset' if high is None else f'{high / 2**20:.0f} MiB'}"
          "   <- the version you can debug, and Compose has no key for it")
    print(f"  /proc/meminfo says   : "
          f"{'?' if host is None else f'{host / 2**20:.0f} MiB'}"
          "   <- the HOST's memory. Not namespaced, so not yours")
    print(f"  Python's heap ceiling: none. There is no such thing.")
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    print(f"  RLIMIT_AS            : "
          f"{'unlimited' if soft == resource.RLIM_INFINITY else f'{soft / 2**20:.0f} MiB'}"
          "   <- a DIFFERENT limit, enforced differently")
    print(f"  starting RSS         : {rss_mb():.0f} MiB"
          f"   ({'current, /proc/self/status' if HAVE_CURRENT_RSS else 'PEAK, getrusage -- no /proc here'})")
    print()

    if limit is None:
        print(f"  !! No cgroup memory limit on this host, so nothing can OOM-kill this")
        print(f"  !! process. It will stop ITSELF at {args.limit_mb} MiB and say so.")
        print(f"  !! That is not the experiment -- it is the shape of the experiment")
        print(f"  !! with the lethal part removed. For the real thing:")
        print(f"  !!   docker run --rm --memory=256m -v \"$PWD:/w\" -w /w \\")
        print(f"  !!     python:3.13-slim python oom.py")
        print()
        ceiling_mb = args.limit_mb
    else:
        # Aim past the limit. If the kernel can reclaim page cache fast
        # enough it will, so this has to allocate ANONYMOUS pages and touch
        # them -- reclaimable memory does not get you killed.
        ceiling_mb = int(limit / 2**20 * 1.5)
        print(f"  Allocating toward {ceiling_mb} MiB against a {limit / 2**20:.0f} MiB limit.")
        print("  Every chunk is touched, because under Linux's default overcommit the")
        print("  allocation itself is free -- the cgroup charge lands on the WRITE.")
        print()

    print("  installed, and about to be shown useless:")
    print("    * try/except MemoryError around the allocation")
    print("    * a SIGTERM handler that logs")
    print("    * an atexit hook")
    print("    * a finally block")
    print()

    blocks: list[bytearray] = []
    try:
        while len(blocks) * CHUNK_MB < ceiling_mb:
            # bytearray, not a list of ints: this is a single contiguous
            # allocation whose pages we then touch, which is what a big
            # query result or a JSON serialisation actually looks like.
            block = bytearray(CHUNK_MB * 2**20)
            # Touch every page. One byte per 4 KiB page is enough -- the
            # charge is per page, not per byte.
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            blocks.append(block)

            allocated = len(blocks) * CHUNK_MB
            if allocated % 32 == 0 or (limit and allocated * 2**20 > limit * 0.8):
                events = cgroup.memory_events() or {}
                print(f"    allocated {allocated:5d} MiB   RSS {rss_mb():6.0f} MiB"
                      f"   oom_kill={events.get('oom_kill', 'n/a')}"
                      f"  high={events.get('high', 'n/a')}", flush=True)
            time.sleep(0.01)

    except MemoryError:
        # This runs only if you hit an RLIMIT_AS or the allocator genuinely
        # returned NULL. A cgroup OOM kill NEVER produces this. If you see
        # this traceback in a container, you were not OOM-killed -- you hit
        # a different limit, and the fix is a different fix.
        print()
        print("  MemoryError CAUGHT.", flush=True)
        print("  Read this carefully: a cgroup OOM kill cannot produce this exception.")
        print("  You hit an RLIMIT_AS, or the allocator returned NULL for another")
        print("  reason. Different mechanism, different fix. Check `ulimit -v`.")
        raise SystemExit(1)
    finally:
        # A finally block. Runs on exceptions and on SystemExit. Does not
        # run on SIGKILL.
        print(f"  [finally] reached, with {len(blocks) * CHUNK_MB} MiB allocated",
              flush=True)

    print()
    print(f"  Reached {len(blocks) * CHUNK_MB} MiB without being killed.")
    if limit is None:
        print("  Expected: there is no cgroup on this host to kill anything. The")
        print("  self-imposed ceiling stopped the loop; nothing was enforced.")
    else:
        print("  NOT expected under a memory limit. The kernel reclaimed enough to")
        print("  keep up -- allocate faster, or check that memory.max is what you")
        print("  think it is. If memory.high is set below max, that is the answer:")
        print("  the high counter above should be climbing, and this is the")
        print("  degrade-instead-of-die behaviour working exactly as advertised.")

    if args.free:
        print()
        print("  --free: dropping every reference and asking for a full GC.")
        before = rss_mb()
        blocks.clear()
        import gc

        gc.collect()
        time.sleep(0.5)
        after = rss_mb()
        if not HAVE_CURRENT_RSS:
            print(f"    peak RSS {after:.0f} MiB -- and PEAK is all this platform")
            print("    offers. There is no /proc/self/status here, so there is no")
            print("    current-RSS reading to compare against, and a peak that did")
            print("    not fall proves nothing. Run this in a Linux container to see")
            print("    the real before/after.")
        else:
            print(f"    RSS before {before:.0f} MiB -> after {after:.0f} MiB "
                  f"(returned {before - after:.0f} MiB)")
        print("    Freed objects go back to pymalloc's pools and arenas, and an")
        print("    arena returns to the OS only when it is COMPLETELY empty. So RSS")
        print("    is sticky: a burst that briefly needed this much leaves a process")
        print("    that looks like it needs this much, and your memory limit has to")
        print("    be sized for the peak rather than the average.")

    print()
    print("  Where the evidence lives when this DOES get killed:")
    print("    docker inspect <c> --format '{{.State.OOMKilled}}'   -> true")
    print("    exit code                                           -> 137 (128 + SIGKILL)")
    print("    cat /sys/fs/cgroup/memory.events                    -> oom_kill incremented")
    print("    dmesg on the host                                   -> the kill decision")
    print("    your application logs                               -> nothing at all")
    if events_before:
        print()
        print(f"  memory.events at start: {events_before}")
        print(f"  memory.events now     : {cgroup.memory_events()}")


if __name__ == "__main__":
    main()
