"""
Layer 2 lab - the service under test.

One FastAPI app, four endpoints, each owned by one topic. Which code path runs
is decided entirely by environment variables read once at startup, so that
nothing except the variable changes between two runs you intend to compare:

    /fanout    topic 1 (VARIANT) and topic 6 (PROTO)
    /checkout  topic 2 (POOL_PROFILE)
    /order     topic 3 (TIMEOUT_PROFILE)
    /stats     every topic - in-flight, pool waits, upstream calls, errors

Everything upstream goes through Toxiproxy (UPSTREAM_URL points at toxi:8475,
never at upstream:9000). If you ever see a topic produce "no difference at
all", check that first: a toxic attached to a proxy nobody talks through is
the single most common way to get a null result that looks like a disproved
prediction.
"""
import asyncio
import contextlib
import os
import random
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

VARIANT = os.environ.get("VARIANT", "WARM").upper()
POOL_PROFILE = os.environ.get("POOL_PROFILE", "default")
TIMEOUT_PROFILE = os.environ.get("TIMEOUT_PROFILE", "none")
PROTO = os.environ.get("PROTO", "h1")
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://toxi:8475")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://app:app@db:5432/app")

FANOUT = int(os.environ.get("FANOUT", "5"))


# --------------------------------------------------------------------------
# Counters. Deliberately plain integers rather than a metrics library: the
# point of several topics is that the interesting number (pool waits, queue
# depth) is one nobody exports by default, so exporting it here is the lesson.
# --------------------------------------------------------------------------
@dataclass
class Stats:
    inflight: int = 0
    inflight_max: int = 0
    upstream_calls: int = 0
    upstream_errors: int = 0
    upstream_timeouts: int = 0
    retries: int = 0
    retries_denied_by_budget: int = 0
    breaker_open_rejections: int = 0
    shed: int = 0
    db_queries: int = 0
    pool_wait_seconds: float = 0.0
    pool_waits: int = 0
    clients_constructed: int = 0
    started: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict:
        d = self.__dict__.copy()
        d["uptime_s"] = round(time.monotonic() - self.started, 1)
        d.pop("started")
        return d


STATS = Stats()

# --------------------------------------------------------------------------
# Topic 2: pool profiles. Each is one line of configuration and a completely
# different shape of failure at the same arrival rate.
# --------------------------------------------------------------------------
POOL_PROFILES = {
    # SQLAlchemy and httpx defaults, untouched. pool_timeout=30 is why the
    # incident is reported as "slow", never as "erroring".
    "default":      dict(pool_size=5,  max_overflow=10, pool_timeout=30.0, shed_at=None),
    "wide":         dict(pool_size=20, max_overflow=10, pool_timeout=30.0, shed_at=None),
    "fast_timeout": dict(pool_size=5,  max_overflow=10, pool_timeout=2.0,  shed_at=None),
    # The point of the topic: the first three move the knee, only this one
    # changes the shape of the failure.
    "shed":         dict(pool_size=5,  max_overflow=10, pool_timeout=30.0, shed_at=int(os.environ.get("SHED_AT", "40"))),
}

# --------------------------------------------------------------------------
# Topic 3: timeout profiles.
# --------------------------------------------------------------------------
BUDGET_TOTAL = float(os.environ.get("BUDGET_TOTAL_S", "3.0"))
BUDGET_RESERVE = float(os.environ.get("BUDGET_RESERVE_S", "0.2"))


class Deadline:
    """A deadline is a value you propagate and re-check, not a constant you
    configure once. Every outgoing call gets min(remaining - reserve, cap);
    if that is <= 0 we fail now rather than starting a call whose answer is
    already too late to use."""

    def __init__(self, total: float, reserve: float = BUDGET_RESERVE):
        self.expires_at = time.monotonic() + total
        self.reserve = reserve

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def for_call(self, cap: float) -> float:
        return min(self.remaining() - self.reserve, cap)


class RetryBudget:
    """Token bucket. Backoff and jitter spread retries out; only a budget
    reduces how many there are, which is the part that stops a storm."""

    def __init__(self, ratio: float = 0.1, burst: int = 20):
        self.ratio, self.tokens, self.burst = ratio, float(burst), float(burst)

    def on_request(self) -> None:
        self.tokens = min(self.burst, self.tokens + self.ratio)

    def try_retry(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Breaker:
    """Closed -> open on consecutive failures, half-open after cool_down."""

    def __init__(self, threshold: int = 20, cool_down: float = 5.0):
        self.threshold, self.cool_down = threshold, cool_down
        self.consecutive_failures = 0
        self.opened_at = 0.0

    def allows(self) -> bool:
        if self.consecutive_failures < self.threshold:
            return True
        if time.monotonic() - self.opened_at > self.cool_down:
            self.consecutive_failures = self.threshold - 1   # half-open: let one through
            return True
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.consecutive_failures = 0
        else:
            if self.consecutive_failures == 0:
                self.opened_at = time.monotonic()
            self.consecutive_failures += 1
            if self.consecutive_failures == self.threshold:
                self.opened_at = time.monotonic()


BUDGET = RetryBudget()
BREAKER = Breaker()

app = FastAPI()
STATE: dict = {}


# Topic 5's variant table asks for the SAME client under several connection
# lifetime policies -- httpx's default 5 s idle expiry, an expiry of None
# ("this socket is immortal"), and a bounded maximum lifetime. Without a knob
# the lab can only ever run the first, which is the one that accidentally
# recovers, so the alias move produces a three-request blip instead of the
# outage the topic is named after.
#
#   KEEPALIVE_EXPIRY=default   httpx's own 5.0 s
#   KEEPALIVE_EXPIRY=none      never expire an idle connection  <- the bug
#   KEEPALIVE_EXPIRY=<seconds> e.g. 60, the bounded-lifetime fix
_KA_EXPIRY_RAW = os.environ.get("KEEPALIVE_EXPIRY", "default").strip().lower()


def _keepalive_expiry():
    if _KA_EXPIRY_RAW in ("", "default"):
        return 5.0            # httpx's own default, stated rather than implied
    if _KA_EXPIRY_RAW in ("none", "null", "never"):
        return None
    return float(_KA_EXPIRY_RAW)


def build_client() -> httpx.AsyncClient:
    """Topic 1's three variants, Topic 5's lifetimes, Topic 6's two protocols."""
    STATS.clients_constructed += 1
    if VARIANT == "WARM_TUNED" or PROTO == "h2":
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=100,
            keepalive_expiry=30.0 if _KA_EXPIRY_RAW == "default" else _keepalive_expiry(),
        )
        return httpx.AsyncClient(
            limits=limits,
            http2=(PROTO == "h2"),
            timeout=httpx.Timeout(10.0, connect=2.0),
        )
    return httpx.AsyncClient(
        limits=httpx.Limits(keepalive_expiry=_keepalive_expiry()),
        timeout=httpx.Timeout(10.0, connect=2.0),
    )


@contextlib.asynccontextmanager
async def lifespan_impl(_: FastAPI):
    prof = POOL_PROFILES[POOL_PROFILE]
    # WARM / WARM_TUNED: one client, built here, alive for the process. COLD
    # builds one per call inside the handler instead -- the actual bug.
    STATE["client"] = None if VARIANT == "COLD" else build_client()
    STATE["engine"] = create_async_engine(
        DATABASE_URL,
        pool_size=prof["pool_size"],
        max_overflow=prof["max_overflow"],
        pool_timeout=prof["pool_timeout"],
        pool_pre_ping=False,
    )
    STATE["session"] = async_sessionmaker(STATE["engine"], expire_on_commit=False)
    try:
        yield
    finally:
        if STATE["client"] is not None:
            await STATE["client"].aclose()
        await STATE["engine"].dispose()


app.router.lifespan_context = lifespan_impl


@app.middleware("http")
async def track_inflight(request: Request, call_next):
    STATS.inflight += 1
    STATS.inflight_max = max(STATS.inflight_max, STATS.inflight)
    try:
        shed_at = POOL_PROFILES[POOL_PROFILE]["shed_at"]
        # /stats and /healthz are exempt. They are the two endpoints you read
        # DURING the incident, and POOL_PROFILE=shed drives in-flight straight
        # through the threshold and holds it there -- so an unexempted
        # admission controller answers `{"error":"shed"}` to every attempt to
        # read the counters that explain what it is doing. Load shedding that
        # sheds your own telemetry first is a real production failure, and the
        # exemption is the real fix: keep the observability path off the queue.
        if request.url.path in ("/stats", "/healthz"):
            return await call_next(request)
        if shed_at is not None and STATS.inflight > shed_at:
            # Admission control. The error rate is the feature: it is the only
            # signal that says "at capacity" while there is still time to act.
            STATS.shed += 1
            return JSONResponse({"error": "shed"}, status_code=503,
                                headers={"retry-after": "1"})
        return await call_next(request)
    finally:
        STATS.inflight -= 1


async def call_upstream(client: httpx.AsyncClient, path: str, timeout=None) -> httpx.Response:
    STATS.upstream_calls += 1
    kwargs = {} if timeout is None else {"timeout": timeout}
    return await client.get(f"{UPSTREAM_URL}{path}", **kwargs)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "variant": VARIANT, "pool_profile": POOL_PROFILE,
        "timeout_profile": TIMEOUT_PROFILE, "proto": PROTO,
        "keepalive_expiry": _KA_EXPIRY_RAW,
        "upstream": UPSTREAM_URL,
    }


@app.get("/stats")
async def stats():
    return STATS.snapshot()


# ---------------------------------------------------------------- topic 1/6
@app.get("/fanout")
async def fanout():
    """FANOUT calls to upstream per request.

    COLD constructs the client inside the handler: a pool of one, used once,
    then discarded along with its socket. That is the bug this topic exists
    for, and it is three characters different from the code beside it.
    """
    t0 = time.perf_counter()
    if VARIANT == "COLD":
        async with build_client() as client:
            rs = await asyncio.gather(*(call_upstream(client, "/work") for _ in range(FANOUT)))
    else:
        client = STATE["client"]
        rs = await asyncio.gather(*(call_upstream(client, "/work") for _ in range(FANOUT)))
    return {"n": len(rs), "ms": round((time.perf_counter() - t0) * 1000, 2)}


# ------------------------------------------------------------------ topic 2
@app.get("/checkout")
async def checkout():
    """One upstream call and two database queries -- the stacked-pool shape.

    The measurement that matters is pool_wait_seconds: requests do not fail
    when the pool is full, they wait, in a place your application metrics do
    not look. So we look.
    """
    client = STATE["client"] or build_client()
    await call_upstream(client, "/work")

    # engine.connect(), not sessionmaker(). An AsyncSession is lazy: entering
    # `async with STATE["session"]()` allocates a Python object and touches the
    # connection pool exactly not at all -- the checkout happens later, inside
    # the first execute(), where its cost is indistinguishable from the query's.
    # Timing the session's __aenter__ therefore reports ~0.05 ms no matter how
    # exhausted the pool is, and pool_waits stays at zero through an incident
    # that is entirely pool waiting. AsyncConnection.__aenter__ does acquire,
    # so this timer measures the queue and nothing else.
    waited = time.perf_counter()
    async with STATE["engine"].connect() as conn:
        wait = time.perf_counter() - waited
        if wait > 0.005:                       # anything above noise was a queue
            STATS.pool_waits += 1
            STATS.pool_wait_seconds += wait
        await conn.execute(text("SELECT count(*) FROM orders"))
        await conn.execute(text("SELECT sku, sum(qty) FROM orders GROUP BY sku LIMIT 10"))
        STATS.db_queries += 2
    return {"ok": True, "pool_wait_ms": round(wait * 1000, 2)}


# ------------------------------------------------------------------ topic 3
async def _attempt(client, timeout) -> bool:
    try:
        r = await call_upstream(client, "/work", timeout=timeout)
        return r.status_code < 500
    except httpx.TimeoutException:
        STATS.upstream_timeouts += 1
        return False
    except httpx.HTTPError:
        STATS.upstream_errors += 1
        return False


@app.get("/order")
async def order(request: Request, response: Response):
    """Three sequential upstream calls under one of four timeout policies.

    The measurement that matters here is NOT p99 during the fault. It is time
    to recovery after the fault is removed; `none` is the profile that should
    fail that test.
    """
    client = STATE["client"] or build_client()

    if TIMEOUT_PROFILE == "none":
        # The `requests` default, in async form: no timeout is not a call, it
        # is a promise to hang forever.
        #
        # httpx.Timeout(None), not the Python literal None. Passing None here
        # means "no argument given", and httpx then applies the client's own
        # default -- which build_client() sets to 10 s. That made this profile
        # a 10-second timeout wearing the name `none`: the fault window ended
        # in bounded 500s, the service recovered by itself, and scenario 1
        # demonstrated the opposite of its point. It is exactly the bullet in
        # this topic's own broken-experiment list ("you have a timeout you did
        # not know about ... find it"), found in the lab's own code.
        for _ in range(3):
            await call_upstream(client, "/work", timeout=httpx.Timeout(None))
        return {"profile": "none"}

    if TIMEOUT_PROFILE == "flat":
        # 5 s everywhere regardless of depth. Better -- and still lets three
        # inner calls total 15 s while the caller gave up long ago.
        for _ in range(3):
            if not await _attempt(client, 5.0):
                response.status_code = 504
                return {"profile": "flat", "error": "upstream"}
        return {"profile": "flat"}

    # budget / full: the deadline arrives from the caller when it exists.
    hdr = request.headers.get("x-request-deadline")
    total = BUDGET_TOTAL
    with contextlib.suppress(ValueError, TypeError):
        if hdr:
            total = min(BUDGET_TOTAL, float(hdr))
    dl = Deadline(total)

    for hop in range(3):
        slice_s = dl.for_call(cap=1.0)
        if slice_s <= 0:
            response.status_code = 504
            return {"profile": TIMEOUT_PROFILE, "error": "deadline exceeded before hop %d" % hop}

        if TIMEOUT_PROFILE == "budget":
            if not await _attempt(client, slice_s):
                response.status_code = 504
                return {"profile": "budget", "error": "upstream", "hop": hop}
            continue

        # full: budget + retry with FULL jitter + retry budget + breaker
        BUDGET.on_request()
        attempt, backoff, ok = 0, 0.05, False
        while True:
            if not BREAKER.allows():
                STATS.breaker_open_rejections += 1
                response.status_code = 503
                return {"profile": "full", "error": "breaker open", "hop": hop}
            ok = await _attempt(client, dl.for_call(cap=1.0))
            BREAKER.record(ok)
            if ok or attempt >= 2 or dl.remaining() <= BUDGET_RESERVE:
                break
            if not BUDGET.try_retry():
                STATS.retries_denied_by_budget += 1
                break
            STATS.retries += 1
            # full jitter: uniform in [0, backoff], not backoff +/- noise.
            await asyncio.sleep(random.uniform(0, min(backoff, max(0.0, dl.remaining()))))
            backoff, attempt = backoff * 2, attempt + 1
        if not ok:
            response.status_code = 504
            return {"profile": "full", "error": "upstream", "hop": hop}

    return {"profile": TIMEOUT_PROFILE, "remaining_ms": round(dl.remaining() * 1000, 1)}
