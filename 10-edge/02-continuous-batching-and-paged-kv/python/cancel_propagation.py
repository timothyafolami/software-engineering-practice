"""
Layer 10 - Topic 2: does hanging up actually free the KV blocks? (Python)

What this demonstrates
    One program, both halves of the failure. It stands up a stub model
    server that streams 40 tokens at 100ms each while watching for its
    caller to go away, puts a FastAPI gateway in front of it, then fires a
    client that hangs up after 500ms -- twice, at two gateway handlers:

      /naive      buffers the upstream response before returning anything.
                  It is the handler almost everyone writes first, and it
                  never learns the client left.
      /cancelling streams, polls `await request.is_disconnected()`, and
                  leaves the `async with client.stream(...)` block on
                  disconnect, which closes the upstream socket.

    The stub server reports what it saw. That report is the experiment:
    under load, a request whose client is gone but whose decode continues
    is holding KV blocks the scheduler could be giving to someone real.

What to look for
    - /naive: the upstream runs to completion, ~4s, roughly 3.5s of it
      after the client stopped listening.
    - /cancelling: the upstream sees EOF within a few tokens of the hang-up
      and stops.
    - The mechanism is not "FastAPI cancels for you". Starlette does cancel
      the handler task on http.disconnect, but that only surfaces at the
      next `await` -- so a handler that is inside a blocking call, or that
      awaits nothing until the upstream finishes, learns nothing. The
      polling in /cancelling is what makes the behaviour the same either
      way.

Dependencies (declared in requirements.txt): fastapi, uvicorn, httpx.
Runs with no arguments, binds only to 127.0.0.1 on ephemeral ports:

    python3 python/cancel_propagation.py
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

TOKENS = 40
TOKEN_INTERVAL = 0.1          # 4.0s of "decode" in total
CLIENT_HANGS_UP_AFTER = 0.5   # the client gives up here

ledger: list[dict] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


UPSTREAM_PORT = free_port()
GATEWAY_PORT = free_port()
UPSTREAM = f"http://127.0.0.1:{UPSTREAM_PORT}"


# --------------------------------------------------------------------------
# The stub model server. Streams tokens while concurrently watching its own
# socket for EOF, which is how a real engine learns that the consumer of a
# generation has gone away.
# --------------------------------------------------------------------------
async def upstream_handler(reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
    while await reader.readline() not in (b"\r\n", b"", b"\n"):
        pass  # drain request headers; the body is irrelevant to the point

    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                 b"Transfer-Encoding: chunked\r\n\r\n")
    await writer.drain()

    peer_gone = asyncio.Event()

    async def watch_for_eof() -> None:
        # A read that returns b"" means the peer closed its end. This is the
        # only signal the engine gets, and it only arrives if somebody
        # upstream actually closes the socket.
        if await reader.read(1) == b"":
            peer_gone.set()

    watcher = asyncio.create_task(watch_for_eof())
    started = time.perf_counter()
    sent = 0
    try:
        for i in range(TOKENS):
            if peer_gone.is_set():
                break
            body = f"data: token {i}\n\n".encode()
            writer.write(b"%x\r\n" % len(body) + body + b"\r\n")
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                peer_gone.set()
                break
            sent = i + 1
            await asyncio.sleep(TOKEN_INTERVAL)
        writer.write(b"0\r\n\r\n")
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        peer_gone.set()
    finally:
        watcher.cancel()
        ledger.append({
            "aborted": peer_gone.is_set(),
            "tokens": sent,
            "seconds": time.perf_counter() - started,
        })
        writer.close()


# --------------------------------------------------------------------------
# The gateway: the same upstream call written two ways.
# --------------------------------------------------------------------------
app = FastAPI()

# One long-lived client, as a real service would have. Note that this alone
# is enough to defeat cancellation in /naive: nothing closes it, so nothing
# closes the upstream socket.
shared_client = httpx.AsyncClient(timeout=60.0)


@app.post("/naive")
async def naive() -> Response:
    """Await the whole upstream response, then return it. The shape almost
    everyone writes first, and it is not obviously wrong on the page.

    Starlette does not cancel a plain (non-streaming) handler when the
    client disconnects -- the http.disconnect message simply sits in the
    receive queue while this coroutine runs to completion. So the upstream
    generation runs to its end, holding KV blocks, for a response that is
    thrown away at the last line of this function."""
    r = await shared_client.post(f"{UPSTREAM}/completions", json={})
    return Response(content=r.content, media_type="text/event-stream")


@app.post("/cancelling")
async def cancelling(request: Request) -> StreamingResponse:
    """Stream, and hold the upstream response inside a scope that ends when
    this generator does.

    Two mechanisms are at work and it is worth knowing which is load-
    bearing. The explicit one is the `is_disconnected()` poll. The implicit
    one -- the one that actually fires first here -- is that Starlette
    throws CancelledError into an in-progress StreamingResponse generator
    when the client goes away, which unwinds `async with client.stream(...)`,
    which closes the upstream socket. Structure does the work; the poll is
    the backstop for the case where the upstream produces nothing for a long
    time and the generator is parked on a chunk that never arrives."""
    async def body():
        async with shared_client.stream("POST", f"{UPSTREAM}/completions",
                                        json={}) as upstream:
            async for chunk in upstream.aiter_bytes():
                if await request.is_disconnected():
                    return
                yield chunk
    return StreamingResponse(body(), media_type="text/event-stream")


async def hang_up_on(path: str) -> None:
    """A client that starts reading and then hangs up.

    Deliberately a raw socket rather than an HTTP client with a timeout.
    A timeout in httpx (and in most client libraries) raises in your code
    without necessarily closing the TCP connection promptly -- so the server
    sees nothing, and the experiment silently measures the wrong thing. A
    browser tab closing, or a proxy giving up, closes the socket. That close
    is the only signal the server ever gets, so it is the one to send."""
    reader, writer = await asyncio.open_connection("127.0.0.1", GATEWAY_PORT)
    writer.write(f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
                 f"Content-Type: application/json\r\nContent-Length: 2\r\n"
                 f"\r\n{{}}".encode())
    await writer.drain()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(reader.read(), CLIENT_HANGS_UP_AFTER)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def main() -> None:
    upstream_server = await asyncio.start_server(upstream_handler, "127.0.0.1",
                                                 UPSTREAM_PORT)
    config = uvicorn.Config(app, host="127.0.0.1", port=GATEWAY_PORT,
                            log_level="error")
    gateway = uvicorn.Server(config)
    gateway_task = asyncio.create_task(gateway.serve())
    while not gateway.started:
        await asyncio.sleep(0.02)

    print("Python / FastAPI - cancellation on client disconnect")
    print(f"  upstream streams {TOKENS} tokens x {TOKEN_INTERVAL * 1000:.0f}ms "
          f"= {TOKENS * TOKEN_INTERVAL:.1f}s of decode")
    print(f"  client hangs up after {CLIENT_HANGS_UP_AFTER:.1f}s\n")
    print(f"  {'handler':<14} {'upstream saw':<16} {'tokens decoded':>14} "
          f"{'upstream ran':>13} {'wasted':>8}")
    print("  " + "-" * 70)

    for path in ("/naive", "/cancelling"):
        ledger.clear()
        await hang_up_on(path)
        # Give the stub a moment to finish or notice, whichever it does.
        deadline = time.perf_counter() + TOKENS * TOKEN_INTERVAL + 1.0
        while not ledger and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        entry = ledger[0] if ledger else {"aborted": False, "tokens": -1,
                                          "seconds": float("nan")}
        wasted = max(0.0, entry["seconds"] - CLIENT_HANGS_UP_AFTER)
        print(f"  {path:<14} {'cancelled' if entry['aborted'] else 'nothing':<16} "
              f"{entry['tokens']:>14} {entry['seconds']:>12.2f}s "
              f"{wasted:>7.2f}s")

    print()
    print("  'wasted' is decode time spent on a response nobody read. On a")
    print("  loaded server that is not just wasted compute: those KV blocks")
    print("  stayed allocated the whole time, so the scheduler could not")
    print("  admit somebody who was still waiting.")

    gateway.should_exit = True
    await gateway_task
    upstream_server.close()
    await upstream_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
