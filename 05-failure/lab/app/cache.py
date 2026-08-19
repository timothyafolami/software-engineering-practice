"""
Layer 5 lab - the cache whose loss is topic 4's trigger.

WHAT THIS DEMONSTRATES
  A cache is a capacity multiplier, and capacity multipliers are load-
  bearing. At a 95% hit rate the database sees 5% of traffic; empty the
  cache and it sees 100% of it, instantly. That is why `redis-cli FLUSHALL`
  is the trigger in topic 4: one command, instantaneous, fully reversible,
  and the cache is already refilling by the time you have finished typing
  it. If the system does not recover, nothing is still broken - which is
  the entire definition of a metastable failure.

WHAT TO LOOK FOR
  `cache_hits` / (`cache_hits` + `cache_misses`) in /admin/counters. Watch
  it recover to near its old value while goodput does not. The hit rate
  coming back and goodput not coming back is the moment the topic lands:
  the trigger is gone and the amplification is now sustaining itself.

  Redis unreachable is treated as a miss, not an error. That is the honest
  production behaviour and it is also the worse one - a cache that fails
  open converts a cache outage into a database overload.
"""
from __future__ import annotations

import time

import redis.asyncio as aioredis

from .config import config
from .metrics import counters

_client: aioredis.Redis | None = None
_url: str | None = None


def client() -> aioredis.Redis:
    global _client, _url
    url = config.get("REDIS_URL")
    if _client is None or url != _url:
        _client = aioredis.from_url(url, socket_timeout=2.0,
                                    socket_connect_timeout=2.0,
                                    decode_responses=True)
        _url = url
    return _client


async def get(key: str) -> str | None:
    try:
        value = await client().get(key)
    except Exception:
        counters.inc("cache_misses")
        return None
    if value is None:
        counters.inc("cache_misses")
        return None
    counters.inc("cache_hits")
    return value


async def setex(key: str, value: str) -> None:
    try:
        await client().setex(key, int(config.get("CACHE_TTL_S")), value)
    except Exception:
        pass  # a cache that cannot be written is still a cache that can be missed


async def ping() -> bool:
    try:
        await client().ping()
        return True
    except Exception:
        return False


def hit_rate() -> float:
    snap = counters.snapshot()
    hits, misses = snap["cache_hits"], snap["cache_misses"]
    total = hits + misses
    return (hits / total * 100.0) if total else 0.0


def now_value(key: str) -> str:
    return f"{key}@{time.time():.3f}"
