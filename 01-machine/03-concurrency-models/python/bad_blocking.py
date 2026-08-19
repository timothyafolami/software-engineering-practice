"""
Layer 1 - Python's concurrency model: asyncio is single-threaded.

A ticker coroutine increments a counter every 100ms. While it's running, we
make one "synchronous" call that looks innocent -- a database driver
without an async version, a synchronous requests.get(), a hash computation
-- anything that doesn't `await`. Because asyncio's event loop lives on a
single OS thread, ANY code that runs without yielding (without hitting an
`await`) owns that thread completely. The ticker cannot run a single tick
while the blocking call is executing, no matter how many "concurrent" tasks
you gathered it with.
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
    # This is the "one synchronous call inside an async handler" from the
    # roadmap's Layer 1 test question. Nothing exotic -- just a function
    # that doesn't await anything.
    time.sleep(BLOCK_DURATION)


async def main():
    counter = [0]
    stop_event = asyncio.Event()
    tick_task = asyncio.create_task(ticker(counter, stop_event))

    start = time.perf_counter()
    await asyncio.sleep(LEAD_IN)
    blocking_work()  # <-- called directly inside a coroutine, never awaited
    await asyncio.sleep(LEAD_OUT)

    stop_event.set()
    await tick_task
    elapsed = time.perf_counter() - start
    expected = elapsed / TICK_INTERVAL
    print(f"[bad] ticks counted: {counter[0]}  over {elapsed:.2f}s  "
          f"(expected ~{expected:.0f} if the ticker were never blocked)")


if __name__ == "__main__":
    asyncio.run(main())
