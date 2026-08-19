"""
Layer 4 Topic 7 (part 5) -- what makes THIS runtime stop renewing its lease.

WHAT THIS DEMONSTRATES: a lease holder whose renewal loop targets a fixed
interval, while Python's characteristic hazard is applied: one synchronous call
on the event loop. The program records the ACTUAL gap between consecutive
renewals, measured with a monotonic clock (Topic 3 -- a wall clock here would let
an NTP step masquerade as a pause, and the whole point is to know which it was).

The interesting result is not that a blocked loop misses renewals. It is that
this process is perfectly healthy the entire time: no GC pause, no crash, no
network fault, nothing to see in any dashboard. The lease expired because the
service was WORKING.

WHAT TO LOOK FOR IN THE OUTPUT: the longest renewal gap against the 10s TTL, and
the "would have lost the lease" verdict. Then run the fix and note that the
blocking work still takes exactly as long -- it is just no longer on the loop.

  python3 python/pause_audit.py
  python3 python/pause_audit.py --block-seconds 3      # under the TTL
  python3 python/pause_audit.py --fixed                # run only the fixed half
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import platform
import time

LEASE_TTL = 10.0
RENEW_INTERVAL = 1.0


class Renewals:
    """Renewal gaps, measured with a monotonic clock.

    time.time() would work right up until an NTP correction, at which point a
    'pause' appears that never happened -- or a real one vanishes. Topic 3 is
    where that costs a p99; here it would cost you the answer to 'did we lose the
    lease?', which is a correctness question.
    """

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self.last = time.monotonic()

    def tick(self) -> None:
        now = time.monotonic()
        self.gaps.append(now - self.last)
        self.last = now

    def longest(self) -> float:
        return max(self.gaps) if self.gaps else 0.0


async def renew_loop(r: Renewals, stop: asyncio.Event) -> None:
    """The keepalive. In production this is an UPDATE on a leases row or an etcd
    lease KeepAlive; the network call is not the interesting part and is left
    out so that nothing but scheduling can explain a missed renewal."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=RENEW_INTERVAL)
        except asyncio.TimeoutError:
            pass
        r.tick()


def expensive_work(seconds: float) -> int:
    """The hazard. A synchronous call: a blocking DB driver, a big json.loads, a
    `requests` call somebody left in, a password hash. Nothing exotic, and
    nothing that looks wrong in review."""
    end = time.monotonic() + seconds
    digest = b"seed"
    rounds = 0
    while time.monotonic() < end:
        digest = hashlib.sha256(digest).digest()
        rounds += 1
    return rounds


async def run(blocking: bool, block_seconds: float) -> tuple[float, int, float]:
    r = Renewals()
    stop = asyncio.Event()
    task = asyncio.create_task(renew_loop(r, stop))
    await asyncio.sleep(2 * RENEW_INTERVAL)

    t0 = time.monotonic()
    if blocking:
        # Called directly inside a coroutine. Never awaited, never yields.
        rounds = expensive_work(block_seconds)
    else:
        # The fix, and it is the same fix in every language in this topic: get
        # the blocking call off the resource everything else needs.
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            rounds = await loop.run_in_executor(pool, expensive_work, block_seconds)
    work_took = time.monotonic() - t0

    await asyncio.sleep(2 * RENEW_INTERVAL)
    stop.set()
    await task
    return r.longest(), rounds, work_took


def report(label: str, longest: float, rounds: int, work_took: float) -> bool:
    lost = longest > LEASE_TTL
    print(f"  {label:<26}{longest:9.2f}s{'':4}{work_took:8.2f}s{'':4}"
          f"{'LOST THE LEASE' if lost else 'held':<16}{rounds:>12} rounds")
    return lost


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--block-seconds", type=float, default=12.0,
                    help="how long the synchronous call runs (default 12, above "
                         "the 10s TTL). Try 3 and watch the verdict change while "
                         "the mechanism does not.")
    ap.add_argument("--fixed", action="store_true", help="run only the fixed half")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("Layer 4 Topic 7 -- Python pause audit")
    print("=" * 78)
    print(f"  Python {platform.python_version()} on {platform.system()} "
          f"{platform.machine()}")
    print(f"  lease TTL {LEASE_TTL:.0f}s, renewal every {RENEW_INTERVAL:.0f}s, "
          f"hazard {args.block_seconds:.0f}s")
    print( "  hazard: ONE synchronous call inside a coroutine -- the whole hazard")
    print( "  clock : time.monotonic(), so an NTP step cannot be mistaken for a pause")
    print()
    print(f"  {'run':<26}{'longest gap':>10}{'':4}{'work took':>9}{'':4}"
          f"{'verdict':<16}{'':>12}")

    lost = False
    if not args.fixed:
        lost = report("blocking on the loop", *asyncio.run(run(True, args.block_seconds)))
    report("run_in_executor", *asyncio.run(run(False, args.block_seconds)))

    print()
    print("  The renewal coroutine was never cancelled, never errored and never")
    print("  lost a connection. It simply did not get to run, because one function")
    print("  that does not await owns the single thread asyncio schedules on.")
    print()
    print("  This is by far the most common way a Python service loses a lease --")
    print("  much more common than a GC pause. And note what the fix does NOT do:")
    print("  the work takes the same time either way. You did not make anything")
    print("  faster, you moved it off the resource the renewal needs.")
    print()
    print("  Fencing is what makes this survivable. A stale holder that resumes")
    print("  must be REJECTED BY THE RESOURCE -- `AND fence < $epoch` in the")
    print("  UPDATE -- because no amount of renewal tuning removes the pause.")
    return 1 if lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
