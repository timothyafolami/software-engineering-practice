"""
Layer 10 lab - the `gateway` service. Topics 2 and 6.

What it is
    FastAPI in front of the model server running on the HOST (Docker
    Desktop on macOS has no Metal passthrough -- see lab/README.md). It
    owns three things the model server does not:

      1. prompt layout, via the mounted prompt_layout module, selected by
         PROMPT_VOLATILE=head|tail                            [topic 2]
      2. cancellation on client disconnect -- whether an abandoned request
         keeps holding KV blocks upstream                     [topic 2]
      3. shadow mirroring to a candidate model at SHADOW_TARGET, which
         must never be able to affect the primary response    [topic 6]

What to look for in /metrics
    gateway_requests_total{outcome=...}   ok | client_disconnect | upstream_error
    gateway_ttft_seconds                  time to first token, the metric
                                          topic 2's knee shows up in
    gateway_inflight                      requests the gateway is holding
    gateway_upstream_cancelled_total      disconnects where the gateway
                                          actually closed the upstream
                                          stream. If this stays at 0 while
                                          client_disconnect climbs, your
                                          cancellation is not propagating
                                          and the engine is still decoding
                                          into a socket nobody is reading.
    gateway_shadow_total{outcome=...}     topic 6; never blocks the primary

Environment (all defined once in lab/README.md):
    MODEL_URL         primary model server, default host.docker.internal:8081/v1
    PROMPT_VOLATILE   head | tail
    SHADOW_TARGET     candidate server; unset disables shadowing
    MODEL_NAME        model id passed upstream
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)

from prompt_layout import BLOCK_SIZE, count_tokens, render

MODEL_URL = os.environ.get("MODEL_URL", "http://host.docker.internal:8081/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "local")
PROMPT_VOLATILE = os.environ.get("PROMPT_VOLATILE", "tail")
SHADOW_TARGET = os.environ.get("SHADOW_TARGET") or None

REQUESTS = Counter("gateway_requests_total", "Requests by outcome", ["outcome"])
TTFT = Histogram(
    "gateway_ttft_seconds", "Time to first token",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
)
TOTAL_LATENCY = Histogram(
    "gateway_request_seconds", "Full response latency",
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)
INFLIGHT = Gauge("gateway_inflight", "Requests the gateway is currently holding")
CANCELLED = Counter("gateway_upstream_cancelled_total",
                    "Disconnects where the upstream stream was closed early")
PROMPT_TOKENS = Counter("gateway_prompt_tokens_total",
                        "Prompt tokens sent upstream (approximate)")
SHADOW = Counter("gateway_shadow_total", "Shadow requests by outcome", ["outcome"])

_client: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global _client
    # No connection-limit games here on purpose: the queue this topic is
    # about lives in the engine, not in the gateway's connection pool.
    # Topic 3 is where a deliberately-sized pool is the subject.
    _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0))
    yield
    await _client.aclose()


app = FastAPI(title="layer10-gateway", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "model_url": MODEL_URL,
        "prompt_volatile": PROMPT_VOLATILE,
        "shadow_target": SHADOW_TARGET,
        "block_size": BLOCK_SIZE,
    })


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)


@app.get("/debug/prompt")
async def debug_prompt(chars: int = 256) -> JSONResponse:
    """First `chars` characters of the rendered prompt for two different
    requests, so you can diff them by eye before believing any cache-hit
    number. Topic 2's broken-experiment checklist starts here."""
    a = render(_volatile_string(), layout=PROMPT_VOLATILE)
    b = render(_volatile_string(), layout=PROMPT_VOLATILE)
    return JSONResponse({
        "layout": PROMPT_VOLATILE,
        "identical_head": a.text[:chars] == b.text[:chars],
        "request_a_head": a.text[:chars],
        "request_b_head": b.text[:chars],
        "approx_tokens": count_tokens(a.text),
    })


def _volatile_string() -> str:
    """The per-request unique content. Real gateways inject exactly this
    kind of thing -- a clock and a correlation id -- and where it lands in
    the prompt is the entire experiment."""
    return (f"Current time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"| request_id: {uuid.uuid4().hex[:8]}")


async def _mirror_to_shadow(payload: dict) -> None:
    """Topic 6: fire-and-forget mirror to the candidate model.

    Three properties this must have, and all three are the point:
    it never blocks the primary response, its failures never surface to the
    caller, and its result is discarded. A shadow that can break production
    is not a shadow, it is a canary with no rollback."""
    assert _client is not None
    try:
        r = await _client.post(f"{SHADOW_TARGET}/completions", json=payload,
                               timeout=httpx.Timeout(60.0, connect=2.0))
        SHADOW.labels(outcome="ok" if r.status_code < 400 else "http_error").inc()
    except Exception:
        SHADOW.labels(outcome="error").inc()


@app.post("/generate")
async def generate(request: Request) -> StreamingResponse:
    body = await request.json() if await request.body() else {}
    user_message = body.get("user_message", "How do I roll back a bad deploy?")
    max_tokens = int(body.get("max_tokens", 128))

    prompt = render(_volatile_string(), layout=PROMPT_VOLATILE,
                    user_message=user_message)
    PROMPT_TOKENS.inc(count_tokens(prompt.text))

    payload = {
        "model": MODEL_NAME,
        "prompt": prompt.text,
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0.0,
    }

    if SHADOW_TARGET:
        shadow_payload = dict(payload, stream=False)
        # Not awaited: the primary path must not wait on the candidate.
        asyncio.create_task(_mirror_to_shadow(shadow_payload))

    started = time.perf_counter()
    INFLIGHT.inc()

    async def stream():
        first_token_seen = False
        outcome = "ok"
        assert _client is not None
        try:
            async with _client.stream("POST", f"{MODEL_URL}/completions",
                                      json=payload) as upstream:
                if upstream.status_code >= 400:
                    outcome = "upstream_error"
                    yield b""
                    return
                async for chunk in upstream.aiter_bytes():
                    if not first_token_seen:
                        TTFT.observe(time.perf_counter() - started)
                        first_token_seen = True
                    # The explicit half of cancellation. Starlette also
                    # cancels this task on http.disconnect, which raises
                    # CancelledError at the next await -- but only if there
                    # IS a next await. Polling makes the behaviour the same
                    # whether or not the upstream is currently producing.
                    if await request.is_disconnected():
                        outcome = "client_disconnect"
                        CANCELLED.inc()
                        # Leaving the `async with` closes the upstream
                        # response, which closes the socket, which is what
                        # makes the engine free the KV blocks.
                        return
                    yield chunk
        except asyncio.CancelledError:
            # Task cancellation from the ASGI layer. Count it, then let it
            # propagate: swallowing CancelledError is how a "graceful"
            # gateway ends up holding upstream work forever.
            outcome = "client_disconnect"
            CANCELLED.inc()
            raise
        except httpx.HTTPError:
            outcome = "upstream_error"
        finally:
            INFLIGHT.dec()
            REQUESTS.labels(outcome=outcome).inc()
            TOTAL_LATENCY.observe(time.perf_counter() - started)

    return StreamingResponse(stream(), media_type="text/event-stream")
