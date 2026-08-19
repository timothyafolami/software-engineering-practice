"""
lab/tools/fake_upstream.py - a stand-in for the host model server.

THIS IS NOT A MODEL. It generates no text, it has no weights, and every
latency it produces is one this file computed from its own constants. Its
only job is to make the *plumbing* runnable without a 16 GB download:

    k6  ->  gateway (container)  ->  host.docker.internal  ->  this

so that `scripts/arrival_rate.js`, the gateway's cancellation counter, the
Prometheus scrape and the prompt-layout switch can all be exercised and a
wiring regression can be caught. Numbers it produces belong in a row
labelled "stub upstream"; they are not measurements of any engine.

What it does model, deliberately and crudely:

  * a fixed number of concurrent decode SLOTS. Requests past that queue,
    which is what puts a knee in TTFT as arrival rate rises. Capacity is
    SLOTS / (prefill + decode) requests per second and it is printed at
    startup, so you can predict the knee before generating load.
  * block-level prefix caching, BLOCK tokens per block. A prompt whose
    first k blocks match a previously seen prompt pays no prefill for
    them. This is why PROMPT_VOLATILE=head and =tail produce different
    TTFT here -- because this file was written to charge for it, not
    because anything was measured. A real engine's cache is evicted,
    shared across sequences and bounded by KV memory; none of that is here.
  * client disconnect. If the gateway closes the stream, the generator
    stops and the slot is freed, and the count of those shows up in
    /metrics as stub_cancelled_total. That is the property topic 2's
    cancellation experiment checks on the gateway side.

Run it on the HOST (see lab/README.md - no Metal in Docker):

    python3 lab/tools/fake_upstream.py --port 8085

then point the gateway at it:

    MODEL_URL=http://host.docker.internal:8085/v1 \
      docker compose up -d gateway prom grafana

The lab's own default is port 8081 for a real model server. Use a
different port for this so the two can never be confused in a scrape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

BLOCK = 16

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8085)
parser.add_argument("--slots", type=int, default=8,
                    help="concurrent decode slots; the knee is SLOTS/service_time")
parser.add_argument("--prefill-us-per-token", type=float, default=200.0)
parser.add_argument("--decode-tokens-per-s", type=float, default=200.0)
args, _ = parser.parse_known_args()

app = FastAPI(title="fake-upstream (NOT A MODEL)")
_slots = asyncio.Semaphore(args.slots)

_prefix_cache: set[tuple[int, ...]] = set()   # hashes of cached block prefixes
_stats = {"requests": 0, "cancelled": 0, "cached_blocks": 0, "prefill_blocks": 0,
          "queued": 0, "running": 0}


def _blocks(text: str) -> list[int]:
    """Chop into BLOCK-token blocks the same way prompt_layout counts them:
    ~4 chars per token, which is the same rough rule the gateway uses."""
    approx_tokens = max(1, len(text) // 4)
    step = BLOCK * 4
    return [hash(text[: (i + 1) * step]) for i in range(approx_tokens // BLOCK)]


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "stub", "object": "model"}]}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    lines = [
        "# HELP stub_num_requests_total Requests accepted (NOT a model server)",
        f"stub_num_requests_total {_stats['requests']}",
        f"stub_cancelled_total {_stats['cancelled']}",
        f"stub_prefix_cache_blocks_hit_total {_stats['cached_blocks']}",
        f"stub_prefix_cache_blocks_miss_total {_stats['prefill_blocks']}",
        f"stub_num_requests_waiting {_stats['queued']}",
        f"stub_num_requests_running {_stats['running']}",
        f"stub_slots {args.slots}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 128))
    stream = bool(body.get("stream", False))

    blocks = _blocks(prompt)
    hit = 0
    for b in blocks:                      # prefix match: stop at first miss
        if b in _prefix_cache:
            hit += 1
        else:
            break
    miss = len(blocks) - hit
    for b in blocks:
        _prefix_cache.add(b)

    _stats["requests"] += 1
    _stats["cached_blocks"] += hit
    _stats["prefill_blocks"] += miss
    prefill_s = miss * BLOCK * args.prefill_us_per_token / 1e6
    per_token_s = 1.0 / args.decode_tokens_per_s

    async def gen():
        _stats["queued"] += 1
        async with _slots:
            _stats["queued"] -= 1
            _stats["running"] += 1
            try:
                await asyncio.sleep(prefill_s)
                for i in range(max_tokens):
                    await asyncio.sleep(per_token_s)
                    chunk = {"id": "stub", "object": "text_completion",
                             "choices": [{"text": " tok", "index": 0,
                                          "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"
            except asyncio.CancelledError:
                # The gateway closed the stream. Free the slot immediately;
                # a server that does not is the failure topic 2 is about.
                _stats["cancelled"] += 1
                raise
            except (BrokenPipeError, ConnectionResetError):
                _stats["cancelled"] += 1
            finally:
                _stats["running"] -= 1

    if not stream:                        # shadow traffic uses this path
        async with _slots:
            await asyncio.sleep(prefill_s + max_tokens * per_token_s)
        return {"id": "stub", "choices": [{"text": " tok" * max_tokens}]}
    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    service_s = 128 * (1.0 / args.decode_tokens_per_s)
    print(f"fake_upstream on :{args.port} - NOT A MODEL")
    print(f"  slots={args.slots}  decode={args.decode_tokens_per_s} tok/s  "
          f"prefill={args.prefill_us_per_token} us/token")
    print(f"  service time for a 128-token completion with a COLD prefix is "
          f"prefill + {service_s:.2f}s")
    print(f"  so capacity with a warm prefix is about "
          f"{args.slots / service_s:.1f} req/s - predict the knee there")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
