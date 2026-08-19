"""
Layer 1 - The fix: run_in_executor moves the blocking call off the event
loop's thread and onto a thread-pool worker, so the loop is free to keep
processing the ticker's timers while it waits for the executor's Future.
This is exactly what asyncpg/aiohttp-style async drivers do for you
automatically -- this script does it by hand to show the mechanism.
"""
import asyncio
import time

TICK_INTERVAL = 0.1
BLOCK_DURATION = 1.0
LEAD_IN = 0.2
LEAD_OUT = 0.2


async def ticker(counter, stop_event):
    while not stop_event.is_set():
        await asyncio.sleep(TICK_INTERVAL)
        counter[0] += 1


def blocking_work():
    time.sleep(BLOCK_DURATION)


async def main():
    counter = [0]
    stop_event = asyncio.Event()
    tick_task = asyncio.create_task(ticker(counter, stop_event))
    loop = asyncio.get_running_loop()

    start = time.perf_counter()
    await asyncio.sleep(LEAD_IN)
    # <-- the only change from bad_blocking.py: hand the blocking call to
    # the default ThreadPoolExecutor and await its completion instead of
    # calling it inline.
    await loop.run_in_executor(None, blocking_work)
    await asyncio.sleep(LEAD_OUT)

    stop_event.set()
    await tick_task
    elapsed = time.perf_counter() - start
    expected = elapsed / TICK_INTERVAL
    print(f"[good] ticks counted: {counter[0]}  over {elapsed:.2f}s  "
          f"(expected ~{expected:.0f} if the ticker were never blocked)")


if __name__ == "__main__":
    asyncio.run(main())
