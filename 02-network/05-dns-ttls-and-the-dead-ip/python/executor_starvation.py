"""
Layer 2 · Topic 5 - "The DNS lookup timed out", with no DNS problem anywhere.

asyncio has no async resolver. `loop.getaddrinfo()` is `socket.getaddrinfo`
handed to the loop's DEFAULT ThreadPoolExecutor -- the same executor every
`run_in_executor(None, ...)` call in your codebase uses. Its default size is
min(32, cpu_count + 4). Fill it with blocking work and name resolution joins
a queue behind that work.

The symptom is a service that reports DNS timeouts while every DNS metric you
own -- query rate, response time at the resolver, NXDOMAIN rate, CoreDNS
latency -- looks perfect. Because it is. The queue is inside your process.

  cpython#112169: https://github.com/python/cpython/issues/112169

This is Layer 1 Topic 3's blocking-in-async failure wearing a network costume,
and it is nastier here because the metric that would show it is one nobody
graphs: the depth of your own default executor's queue.

Three measurements of the SAME lookup:
  1. idle          -- nothing else running
  2. starved       -- the default executor filled with blocking work
  3. fixed         -- the same blocking work sent to a SEPARATE executor

What to look for in the output:
  - the starved column, against the idle one. That difference is entirely
    queueing inside this process.
  - the fixed column returning to idle-like numbers with no change to the
    resolver, the network, or the DNS configuration.

Run: python3 executor_starvation.py
"""
import asyncio
import concurrent.futures
import os
import socket
import statistics
import time

NAME = "localhost"          # resolves from /etc/hosts: no network involved at all
LOOKUPS = 12
BLOCK_SECONDS = 1.0


def default_executor_size() -> int:
    # The value CPython uses when you never configure one.
    return min(32, (os.cpu_count() or 1) + 4)


def blocking_work(seconds: float) -> None:
    """A synchronous database driver, an image resize, a `requests` call, a
    big `json.loads`. Anything you sent to run_in_executor because someone
    told you that makes it non-blocking. It does not; it moves the block."""
    time.sleep(seconds)


async def time_lookups(loop, label: str) -> list[float]:
    """CONCURRENT lookups, because that is what a service does: many requests
    in flight, each needing a name. Measuring them one at a time would let the
    executor drain between measurements and hide the queue entirely -- which is
    also how you would accidentally write a benchmark that proves this bug does
    not exist."""
    async def one() -> float:
        t0 = time.perf_counter()
        await loop.getaddrinfo(NAME, 80, proto=socket.IPPROTO_TCP)
        return (time.perf_counter() - t0) * 1000

    return list(await asyncio.gather(*(one() for _ in range(LOOKUPS))))


def report(label: str, times: list[float], note: str = ""):
    print(f"    {label:<10} p50 {statistics.median(times):8.2f} ms   "
          f"max {max(times):8.2f} ms   {note}")


async def main():
    loop = asyncio.get_running_loop()
    size = default_executor_size()

    print("=" * 78)
    print("A DNS timeout with no DNS problem: the resolver is a thread pool")
    print("=" * 78)
    print(f"  cpu_count {os.cpu_count()}  ->  default ThreadPoolExecutor size "
          f"min(32, cpu+4) = {size}")
    print(f"  resolving '{NAME}' {LOOKUPS} times per measurement (from /etc/hosts:")
    print("  there is no network in this experiment at all, which is the point)")
    print()

    # 1. Idle.
    idle = await time_lookups(loop, "idle")

    # 2. Starved: fill the DEFAULT executor, which is also the resolver's.
    # Three times as many blocking tasks as there are workers, so the pool
    # stays saturated for the whole measurement rather than clearing after the
    # first lookup. A momentary spike moves your max; a sustained one moves
    # your p50, and only the second looks like "DNS is down".
    saturating = [loop.run_in_executor(None, blocking_work, BLOCK_SECONDS)
                  for _ in range(size * 3)]
    await asyncio.sleep(0.05)          # let them all be picked up
    starved = await time_lookups(loop, "starved")
    await asyncio.gather(*saturating)

    # 3. Fixed: the same blocking work, on an executor of its own.
    with concurrent.futures.ThreadPoolExecutor(max_workers=size,
                                               thread_name_prefix="blocking") as own:
        elsewhere = [loop.run_in_executor(own, blocking_work, BLOCK_SECONDS)
                     for _ in range(size * 3)]
        await asyncio.sleep(0.05)
        fixed = await time_lookups(loop, "fixed")
        await asyncio.gather(*elsewhere)

    print("  Resolution latency for the same name, three ways:")
    report("idle", idle, "nothing else running")
    report("starved", starved, f"{size * 3} blocking tasks on the DEFAULT executor")
    report("fixed", fixed, f"the same {size * 3} tasks on a SEPARATE executor")
    print()

    p50x = statistics.median(starved) / max(statistics.median(idle), 1e-6)
    maxx = max(starved) / max(max(idle), 1e-6)
    print(f"  starved / idle:  {p50x:.0f}x at p50,  {maxx:.0f}x at max")
    print()
    print("  Nothing about DNS changed between those three rows. Same name, same")
    print("  /etc/hosts, no packets. The entire difference is queueing time inside")
    print("  this process, in a thread pool you did not know your resolver shared.")
    print()
    print("  What to graph, so you can tell this apart from a real DNS problem:")
    print("    - resolution latency measured IN YOUR PROCESS (this number), against")
    print("      resolution latency measured at the resolver. If yours is high and the")
    print("      resolver's is flat, the queue is yours.")
    print("    - the count of tasks you have in flight on the default executor. There")
    print("      is no built-in metric for it, so wrap run_in_executor and count.")
    print()
    print("  The fixes, in order of how much they actually help:")
    print("    1. Do not put blocking work on the default executor. Give it its own,")
    print("       sized on purpose -- the `fixed` row above, and one line of code.")
    print("    2. Then reduce the blocking work, because a separate executor is a")
    print("       wider queue, not the absence of a queue (Topic 2, again).")
    print("    3. loop.set_default_executor() if you want the resolver to have a pool")
    print("       nothing else can reach.")
    print()
    print("  Rust is the interesting contrast: tokio pushes getaddrinfo onto its")
    print("  DEDICATED blocking pool rather than onto the reactor's workers, which is")
    print("  fix 1 applied by the runtime, by default, for you. Python does not, and")
    print("  that single difference is why an asyncio service degrades so much less")
    print("  gracefully than a tokio one during a DNS stall.")


if __name__ == "__main__":
    asyncio.run(main())
