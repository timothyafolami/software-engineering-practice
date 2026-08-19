"""
Layer 5 - Topic 6: fan-out tails, hedging, and coordinated omission (Python).

One process holds a gateway, up to 50 backends and BOTH load models, so the
only thing missing versus the containerised version is real network variance.
Everything else -- the arithmetic of percentiles under fan-out, the cost of
hedging, and the lie a closed-loop generator tells -- is here.

Python is the right language to lead with because `asyncio.gather` is exactly
"wait for all of them": a fan-out's end-to-end latency IS the max of its legs,
with no scheduler cleverness in between. The hedging primitive is
`asyncio.wait(..., return_when=FIRST_COMPLETED)` followed by an explicit
`.cancel()` on the loser, and forgetting that `.cancel()` is *the* classic bug
-- an uncancelled hedge still occupies its backend worker for the full
duration, which means the hedge you added to shave the tail has instead
doubled your load. Phase B measures that bug rather than describing it.
(`asyncio.TaskGroup`, 3.11+, cancels siblings for you when one raises; it is
the ergonomic version of the same discipline, not a different one.)

WHAT THIS DEMONSTRATES

  Phase A  A gateway fans out to K identical backends and waits for all of
           them, K in {1,2,5,10,20,50}, against two service-time
           distributions that share a p50 of 10ms and a p99 of 200ms:
           log-normal, and bimodal with a 1% slow mode. Backends are
           deliberately unsaturated here, so the only thing acting is the
           arithmetic.
  Phase B  Hedging at the MEASURED backend p95, under a 5% token bucket,
           run three ways: no hedge, hedge with the loser cancelled, and
           hedge with the loser left running.
  Phase C  The same server, the same nominal rate, measured twice: once by
           an open-model generator (arrivals on a fixed schedule) and once
           by a closed-loop one (a fixed number of virtual users, each
           waiting for a response before sending again).

WHAT TO LOOK FOR IN THE OUTPUT

  1. Phase A's `measured_tail` column against `predicted_tail`, which is
     1 - 0.99^K and is arithmetic, not measurement. If the two columns
     disagree badly, read the README's "what would mean the experiment is
     broken" list before you believe either.
  2. The two distributions' `e2e_p50` columns diverging as K grows while
     their `measured_tail` columns stay together. Same p50, same p99, same
     tail probability, different shape -- and the shape is what the user
     feels.
  3. Phase B's `svc_ms/req` and `+load` columns next to `e2e_p99`. Hedging is
     not free; the point of those columns is to quantify what it cost. Read
     the cancelled and uncancelled rows against each other in `svc_ms/req`,
     which is the backend service time actually consumed per request: they
     issue the same calls, and only one of them stops paying for the copy it
     threw away.
  4. Phase C's two p99s, and then the two histograms underneath them. The
     closed-loop row also prints a coordinated-omission-corrected p99,
     measured from when each request was DUE rather than when the generator
     got round to sending it. The gap between the closed loop's raw p99 and
     its corrected p99 is the size of the lie.

A NOTE ON THE TIMER FLOOR: `asyncio.sleep` on macOS resolves to roughly a
millisecond, and the p50 here is 10ms. Read the calibration block first --
it prints what the backend distribution actually measured as, not what it was
configured as, and every later table is relative to those measured numbers.

RUN
    python3 fanout.py

Standard library only. Takes roughly three minutes.
"""
from __future__ import annotations

import asyncio
import math
import random
import time

MS = 1000.0

# ------------------------------------------------------------------ config

BACKEND_P50_MS = 10.0
TAIL_RATIO = 20.0                  # p99 / p50, per the README's specification
Z99 = 2.3263478740408408           # standard normal 99th percentile
Z95 = 1.6448536269514722
LOGNORMAL_SIGMA = math.log(TAIL_RATIO) / Z99
TAIL_THRESHOLD_MS = BACKEND_P50_MS * TAIL_RATIO   # 200.0ms, by construction

K_VALUES = (1, 2, 5, 10, 20, 50)
SAMPLES_PER_CELL = 1500
MAX_RATE = 400.0                   # requests/s ceiling for a cell
MAX_BACKEND_CALLS_PER_S = 10_000.0 # keeps the event loop from being the tail
STAT_WORKERS = 512                 # phase A: backends must NOT queue

HEDGE_K = (10, 50)
HEDGE_BUDGET_RATIO = 0.05          # "at most 5% of backend calls may hedge"
HEDGE_BUCKET_CAPACITY = 20.0

CO_K = 10
CO_WORKERS = 4                     # phase C: backends that CAN saturate
CO_RHO = 0.90                      # offered load as a fraction of capacity
CO_SECONDS = 25.0

CALIB_SAMPLES = 20_000
SEED = 20260819


def pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation, no averaging of percentiles."""
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[idx]


# ----------------------------------------------------------- distributions

class LogNormal:
    """p50 = exp(mu); p99 = exp(mu + z99*sigma). sigma chosen so p99/p50 = 20."""

    name = "lognormal"

    def __init__(self, p50_ms: float, sigma: float) -> None:
        self.mu = math.log(p50_ms / MS)
        self.sigma = sigma

    def sample(self, rng: random.Random) -> float:
        return math.exp(self.mu + self.sigma * rng.gauss(0.0, 1.0))

    def p95_ms(self) -> float:
        return math.exp(self.mu + Z95 * self.sigma) * MS


class Bimodal:
    """99% fast and tight, 1% slow -- and the slow mode's FLOOR is the p99.

    Putting the slow mode's minimum exactly at 20x the p50 is what makes
    P(leg > 200ms) equal 1% on the nose, so the same tail threshold works for
    both distributions and `predicted_tail` stays honest. A slow mode centred
    on 200ms instead would put only half of 1% above the threshold, and the
    predicted/measured comparison would be comparing two different things.
    """

    name = "bimodal"

    def __init__(self, p50_ms: float, slow_floor_ms: float,
                 slow_extra_ms: float = 50.0, p_slow: float = 0.01) -> None:
        self.fast_mu = math.log(p50_ms / MS)
        self.fast_sigma = 0.15          # tight: the fast mode never reaches the floor
        self.slow_floor = slow_floor_ms / MS
        self.slow_extra = slow_extra_ms / MS
        self.p_slow = p_slow

    def sample(self, rng: random.Random) -> float:
        if rng.random() < self.p_slow:
            return self.slow_floor + rng.expovariate(1.0 / self.slow_extra)
        return math.exp(self.fast_mu + self.fast_sigma * rng.gauss(0.0, 1.0))

    def p95_ms(self) -> float:
        return math.exp(self.fast_mu + Z95 * self.fast_sigma) * MS


# --------------------------------------------------------------- the server

class Backend:
    """One backend: a fixed number of workers, a queue, and a service time.

    `workers` is what makes phase C possible. Set it high and the backend is
    a pure delay generator, which is what phase A wants; set it to 4 and the
    thing has a capacity, a queue in front of it, and therefore an opinion
    about how fast you are allowed to send.
    """

    __slots__ = ("sem", "started", "completed", "cancelled", "busy_s")

    def __init__(self, workers: int) -> None:
        self.sem = asyncio.Semaphore(workers)
        self.started = 0
        self.completed = 0
        self.cancelled = 0
        self.busy_s = 0.0

    async def call(self, dist, rng: random.Random) -> None:
        self.started += 1
        try:
            async with self.sem:                 # queueing, if there is any
                held = dist.sample(rng)
                t0 = time.perf_counter()
                try:
                    await asyncio.sleep(held)
                finally:
                    self.busy_s += time.perf_counter() - t0
            self.completed += 1
        except asyncio.CancelledError:
            # A cancelled call releases its worker on the way out, which is
            # the whole reason cancelling the loser of a hedge is cheap HERE
            # and expensive over a real network, where the far end never
            # hears about it. Topic 2's zombie requests, one layer down.
            self.cancelled += 1
            raise


class TokenBucket:
    """gRPC/Envoy-shaped retry throttle: earn fractional tokens on successes.

    Every primary call adds `ratio` of a token; every hedge spends a whole
    one. Steady state is therefore "hedges are at most `ratio` of primary
    calls", with `capacity` worth of burst. This is the difference between a
    hedge and a retry storm with better branding.
    """

    def __init__(self, ratio: float, capacity: float) -> None:
        self.ratio = ratio
        self.capacity = capacity
        self.tokens = capacity

    def on_primary(self) -> None:
        self.tokens = min(self.capacity, self.tokens + self.ratio)

    def take(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Gateway:
    """Fans out to K backends and waits for every one of them."""

    def __init__(self, backends: list[Backend], dist, rng: random.Random,
                 hedge_delay_s: float | None = None,
                 cancel_losers: bool = True) -> None:
        self.backends = backends
        self.dist = dist
        self.rng = rng
        self.hedge_delay_s = hedge_delay_s
        self.cancel_losers = cancel_losers
        self.bucket = TokenBucket(HEDGE_BUDGET_RATIO, HEDGE_BUCKET_CAPACITY)
        self.legs = 0
        self.legs_hedged = 0
        self.budget_denied = 0
        self.orphans: set[asyncio.Task] = set()

    async def _leg(self, backend: Backend) -> bool:
        """One leg. Returns True if this leg fired a hedge."""
        self.legs += 1
        if self.hedge_delay_s is None:
            await backend.call(self.dist, self.rng)
            return False

        self.bucket.on_primary()
        first = asyncio.create_task(backend.call(self.dist, self.rng))
        done, _ = await asyncio.wait({first}, timeout=self.hedge_delay_s)
        if first in done:
            first.result()
            return False

        # Past the measured p95 and still nothing. Hedge -- if the budget says so.
        if not self.bucket.take():
            self.budget_denied += 1
            await first
            return False

        self.legs_hedged += 1
        second = asyncio.create_task(backend.call(self.dist, self.rng))
        done, pending = await asyncio.wait(
            {first, second}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            if self.cancel_losers:
                task.cancel()                    # the line everybody forgets
            else:
                # The bug, made visible: the loser keeps its worker for its
                # full service time. Park it so nothing is garbage collected
                # out from under the measurement.
                self.orphans.add(task)
                task.add_done_callback(self.orphans.discard)
        for task in done:
            task.result()
        return True

    async def handle(self, k: int) -> bool:
        # gather() is "all of them", so e2e latency is the max of the legs.
        # That is not an implementation detail here; it is the experiment.
        hedged = await asyncio.gather(*(self._leg(self.backends[i]) for i in range(k)))
        return any(hedged)


# ------------------------------------------------------------- the harness

class Cell:
    """One measured configuration: latencies, generator lateness, backend load."""

    def __init__(self) -> None:
        self.lat_ms: list[float] = []
        self.late_ms: list[float] = []
        self.arrival_wall = 0.0
        self.hedged_requests = 0
        self.backend_started = 0
        self.backend_cancelled = 0
        self.backend_busy_ms = 0.0
        self.n = 0

    def summary(self) -> dict:
        lat = sorted(self.lat_ms)
        late = sorted(self.late_ms)
        over = sum(1 for v in lat if v > TAIL_THRESHOLD_MS)
        return {
            "n": len(lat),
            "p50": pct(lat, 0.50),
            "p95": pct(lat, 0.95),
            "p99": pct(lat, 0.99),
            "max": lat[-1] if lat else float("nan"),
            "tail": 100.0 * over / max(1, len(lat)),
            "late_p99": pct(late, 0.99),
            "backend_rps": self.backend_started / max(1e-9, self.arrival_wall),
            "svc_ms_per_req": self.backend_busy_ms / max(1, len(lat)),
            "calls_per_req": self.backend_started / max(1, len(lat)),
            "hedge_rate": 100.0 * self.hedged_requests / max(1, len(lat)),
        }


async def run_open_cell(k: int, dist, workers: int, rate: float, n: int,
                        hedge_delay_s: float | None = None,
                        cancel_losers: bool = True, seed: int = SEED) -> Cell:
    """Open model: arrivals happen on a precomputed schedule, full stop.

    The schedule is absolute and computed before the run, so the generator's
    own overhead cannot leak into it -- a generator that sleeps for
    `expovariate(rate)` BETWEEN dispatches slows down exactly when the server
    does, and has quietly become the closed-loop generator this topic is
    about. Latency is measured from each request's DUE time, not from when
    the dispatch loop got round to it, for the same reason.
    """
    rng_arr = random.Random(seed)
    rng_svc = random.Random(seed + 1)
    backends = [Backend(workers) for _ in range(k)]
    gw = Gateway(backends, dist, rng_svc, hedge_delay_s, cancel_losers)
    cell = Cell()

    async def one(due: float) -> None:
        hedged = await gw.handle(k)
        fin = time.perf_counter()
        cell.lat_ms.append((fin - due) * MS)
        if hedged:
            cell.hedged_requests += 1

    schedule = []
    acc = 0.0
    for _ in range(n):
        acc += rng_arr.expovariate(rate)
        schedule.append(acc)

    t0 = time.perf_counter()
    tasks = []
    for offset in schedule:
        due = t0 + offset
        now = time.perf_counter()
        if due > now:
            await asyncio.sleep(due - now)
        cell.late_ms.append((time.perf_counter() - due) * MS)
        tasks.append(asyncio.create_task(one(due)))
    cell.arrival_wall = time.perf_counter() - t0

    # Everything in flight at the end is counted. Dropping it would be its own
    # flavour of omission, and the requests still running are the slow ones.
    await asyncio.gather(*tasks)
    if gw.orphans:
        await asyncio.sleep(0.3)     # let uncancelled hedges finish being expensive

    cell.n = n
    cell.backend_started = sum(b.started for b in backends)
    cell.backend_cancelled = sum(b.cancelled for b in backends)
    cell.backend_busy_ms = sum(b.busy_s for b in backends) * MS
    cell.gw = gw
    return cell


async def run_closed_cell(k: int, dist, workers: int, vus: int,
                          nominal_rate: float, seconds: float,
                          seed: int = SEED) -> Cell:
    """Closed model: `vus` virtual users, each waiting before sending again.

    This is `ramping-vus`, the executor the rest of this layer forbids. It is
    permitted here and only here, because seeing it lie is the point.

    Two numbers are recorded per request. The raw one is what a closed-loop
    generator reports: finish minus send. The corrected one is finish minus
    the time the request was DUE under the nominal schedule -- because a VU
    stuck waiting on a slow response is not sending the requests it owed, and
    those unsent requests are exactly the ones that would have been slow.
    """
    rng_svc = random.Random(seed + 1)
    backends = [Backend(workers) for _ in range(k)]
    gw = Gateway(backends, dist, rng_svc)
    cell = Cell()
    cell.corrected_ms: list[float] = []
    per_vu_interval = vus / nominal_rate

    t0 = time.perf_counter()
    deadline = t0 + seconds

    async def vu(v: int) -> None:
        j = 0
        while True:
            start = time.perf_counter()
            if start >= deadline:
                return
            due = t0 + (v / nominal_rate) + j * per_vu_interval
            await gw.handle(k)
            fin = time.perf_counter()
            cell.lat_ms.append((fin - start) * MS)
            cell.corrected_ms.append((fin - min(start, due)) * MS)
            j += 1

    await asyncio.gather(*(vu(v) for v in range(vus)))
    cell.arrival_wall = time.perf_counter() - t0
    cell.n = len(cell.lat_ms)
    cell.backend_started = sum(b.started for b in backends)
    cell.backend_busy_ms = sum(b.busy_s for b in backends) * MS
    cell.gw = gw
    return cell


# ----------------------------------------------------------------- output

HIST_EDGES_MS = (0, 5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 5120)


def histogram(label: str, vals: list[float]) -> None:
    if not vals:
        return
    counts = [0] * (len(HIST_EDGES_MS))
    for v in vals:
        placed = False
        for i in range(len(HIST_EDGES_MS) - 1):
            if HIST_EDGES_MS[i] <= v < HIST_EDGES_MS[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    peak = max(counts) or 1
    print(f"  {label}   (n={len(vals)})")
    for i in range(len(HIST_EDGES_MS)):
        lo = HIST_EDGES_MS[i]
        hi = HIST_EDGES_MS[i + 1] if i + 1 < len(HIST_EDGES_MS) else None
        rng_label = f"{lo:>6} - {hi:>6} ms" if hi else f"{lo:>6} +{'':8}ms"
        bar = "#" * int(round(40.0 * counts[i] / peak))
        print(f"    {rng_label} |{bar:<40}| {counts[i]:>6}")


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def cell_rate(k: int) -> float:
    return min(MAX_RATE, MAX_BACKEND_CALLS_PER_S / k)


# ------------------------------------------------------------------- main

async def calibrate(dist, workers: int, n: int, seed: int) -> dict:
    """Measure ONE backend directly. Everything downstream is relative to this."""
    rng = random.Random(seed)
    b = Backend(workers)
    lat: list[float] = []

    async def one() -> None:
        t0 = time.perf_counter()
        await b.call(dist, rng)
        lat.append((time.perf_counter() - t0) * MS)

    # Batched so the loop is never asked to hold more than a few hundred timers.
    for start in range(0, n, 500):
        await asyncio.gather(*(one() for _ in range(min(500, n - start))))
    lat.sort()
    over = sum(1 for v in lat if v > TAIL_THRESHOLD_MS)
    return {
        "p50": pct(lat, 0.50),
        "p95": pct(lat, 0.95),
        "p99": pct(lat, 0.99),
        "mean": sum(lat) / len(lat),
        "over_pct": 100.0 * over / len(lat),
    }


async def main() -> None:
    lognormal = LogNormal(BACKEND_P50_MS, LOGNORMAL_SIGMA)
    bimodal = Bimodal(BACKEND_P50_MS, TAIL_THRESHOLD_MS)
    dists = (lognormal, bimodal)

    rule("Layer 5 - Topic 6: fan-out, hedging and coordinated omission (Python)")
    print(f"  backend p50 configured   {BACKEND_P50_MS:.1f} ms")
    print(f"  backend p99 configured   {TAIL_THRESHOLD_MS:.1f} ms   "
          f"(p99/p50 = {TAIL_RATIO:.0f}x, log-normal sigma = {LOGNORMAL_SIGMA:.4f})")
    print(f"  tail threshold t         {TAIL_THRESHOLD_MS:.1f} ms   "
          "chosen so P(one leg > t) = 1% for BOTH distributions, by construction")
    print("  predicted_tail below     1 - 0.99^K, arithmetic rather than measurement")

    # ------------------------------------------------------------ calibration
    rule("CALIBRATION: one backend, unsaturated, measured directly")
    print(f"  {'distribution':<12}{'p50':>9}{'p95':>9}{'p99':>9}{'mean':>9}"
          f"{'P(leg > t)':>13}")
    calib = {}
    for d in dists:
        c = await calibrate(d, STAT_WORKERS, CALIB_SAMPLES, SEED + 7)
        calib[d.name] = c
        print(f"  {d.name:<12}{c['p50']:>8.1f}ms{c['p95']:>8.1f}ms{c['p99']:>8.1f}ms"
              f"{c['mean']:>8.1f}ms{c['over_pct']:>12.2f}%")
    print()
    print("  P(leg > t) is the measured check on the configured 1%. The hedge delay")
    print("  in phase B is the MEASURED p95 above, not the analytic one.")

    # ---------------------------------------------------------------- phase A
    rule("PHASE A: fan-out to K backends, wait for all, no hedging")
    print(f"  backends have {STAT_WORKERS} workers each -- they do not queue, so the only")
    print("  mechanism acting on these numbers is the arithmetic of maxima.")
    print()
    header = (f"  {'dist':<11}{'K':>4}{'rate':>7}{'n':>7}{'e2e_p50':>10}{'e2e_p99':>10}"
              f"{'e2e_max':>10}{'predicted':>11}{'measured':>10}{'gen_late_p99':>14}")
    print(header)
    baseline: dict[tuple[str, int], dict] = {}
    for d in dists:
        for k in K_VALUES:
            rate = cell_rate(k)
            cell = await run_open_cell(k, d, STAT_WORKERS, rate, SAMPLES_PER_CELL)
            s = cell.summary()
            baseline[(d.name, k)] = s
            predicted = 100.0 * (1.0 - 0.99 ** k)
            print(f"  {d.name:<11}{k:>4}{rate:>7.0f}{s['n']:>7}"
                  f"{s['p50']:>9.1f}ms{s['p99']:>9.1f}ms{s['max']:>9.1f}ms"
                  f"{predicted:>10.1f}%{s['tail']:>9.1f}%{s['late_p99']:>12.2f}ms")
        print()

    # ---------------------------------------------------------------- phase B
    rule("PHASE B: hedging at the measured backend p95, under a 5% token bucket")
    print("  Three rows per configuration, identical except for what happens to the")
    print("  losing copy: nothing (no hedge), cancelled, or left running.")
    print()
    print("  svc_ms/req is the backend service time actually consumed per request. It is")
    print("  the column that separates the last two rows: they issue the same calls, and")
    print("  only one of them stops paying for the copy it threw away.")
    print()
    print(f"  {'dist':<10}{'K':>3} {'mode':<26}{'e2e_p50':>9}{'e2e_p99':>9}"
          f"{'be_rps':>11}{'+load':>7}{'svc_ms/req':>11}{'hedge%':>8}{'denied':>7}")
    for d in dists:
        hedge_delay_ms = calib[d.name]["p95"]
        for k in HEDGE_K:
            rate = cell_rate(k)
            base = baseline[(d.name, k)]
            print(f"  {d.name:<10}{k:>3} {'no hedge':<26}"
                  f"{base['p50']:>7.1f}ms{base['p99']:>7.1f}ms"
                  f"{base['backend_rps']:>10.0f}/s{'-':>7}"
                  f"{base['svc_ms_per_req']:>11.1f}{'-':>8}{'-':>7}")
            for cancel in (True, False):
                label = ("hedge @p95, cancelled" if cancel
                         else "hedge @p95, NOT cancelled")
                cell = await run_open_cell(k, d, STAT_WORKERS, rate,
                                           SAMPLES_PER_CELL,
                                           hedge_delay_s=hedge_delay_ms / MS,
                                           cancel_losers=cancel)
                s = cell.summary()
                load_pct = 100.0 * (s["backend_rps"] / base["backend_rps"] - 1.0)
                print(f"  {'':<10}{'':>3} {label:<26}"
                      f"{s['p50']:>7.1f}ms{s['p99']:>7.1f}ms"
                      f"{s['backend_rps']:>10.0f}/s{load_pct:>6.1f}%"
                      f"{s['svc_ms_per_req']:>11.1f}{s['hedge_rate']:>7.1f}%"
                      f"{cell.gw.budget_denied:>7}")
            print(f"  {'':<10}{'':>3}  hedge delay = measured p95 = "
                  f"{hedge_delay_ms:.1f} ms")
            print()

    # ---------------------------------------------------------------- phase C
    rule("PHASE C: the same server measured twice -- open model vs closed loop")
    mean_service_s = calib["lognormal"]["mean"] / MS
    capacity = CO_WORKERS / mean_service_s
    rate = CO_RHO * capacity
    base_mean_e2e_s = None
    print(f"  K = {CO_K}, log-normal, and this time each backend has only "
          f"{CO_WORKERS} workers.")
    print(f"  measured mean service time  {mean_service_s * MS:.1f} ms")
    print(f"  => capacity per backend     {capacity:.1f} rps "
          f"({CO_WORKERS} workers / mean service)")
    print(f"  => nominal offered rate     {rate:.1f} rps  (rho = {CO_RHO:.2f})")
    print()
    print("  rho is deliberately below 1. Above capacity the open model's queue grows")
    print("  without bound and its p99 becomes a statement about how long you ran,")
    print("  not about the server. Below capacity both numbers mean something.")

    # A short unsaturated pass to size the VU pool by Little's Law.
    warm = await run_open_cell(CO_K, lognormal, STAT_WORKERS, rate, 600)
    ws = warm.summary()
    base_mean_e2e_s = (sum(warm.lat_ms) / len(warm.lat_ms)) / MS
    vus = max(1, round(rate * base_mean_e2e_s))
    print()
    print(f"  unsaturated e2e mean at K={CO_K}: {base_mean_e2e_s * MS:.1f} ms "
          f"(p99 {ws['p99']:.1f} ms)")
    print(f"  => closed loop gets {vus} VUs, from Little's Law: "
          f"{rate:.1f} rps x {base_mean_e2e_s * MS:.1f} ms.")
    print("     At the healthy latency those VUs issue the nominal rate exactly. That")
    print("     is the whole trick: the generator is calibrated on a good day.")

    open_cell = await run_open_cell(CO_K, lognormal, CO_WORKERS, rate,
                                    int(rate * CO_SECONDS))
    os_ = open_cell.summary()
    closed_cell = await run_closed_cell(CO_K, lognormal, CO_WORKERS, vus,
                                        rate, CO_SECONDS)
    cs = closed_cell.summary()
    corrected = sorted(closed_cell.corrected_ms)

    print()
    print(f"  {'model':<34}{'n':>7}{'achieved':>11}{'p50':>10}{'p99':>10}{'max':>11}")
    print(f"  {'open  (arrival schedule)':<34}{os_['n']:>7}"
          f"{os_['n'] / open_cell.arrival_wall:>9.0f}/s"
          f"{os_['p50']:>9.1f}ms{os_['p99']:>9.1f}ms{os_['max']:>10.1f}ms")
    print(f"  {'closed (' + str(vus) + ' VUs), as reported':<34}{cs['n']:>7}"
          f"{cs['n'] / closed_cell.arrival_wall:>9.0f}/s"
          f"{cs['p50']:>9.1f}ms{cs['p99']:>9.1f}ms{cs['max']:>10.1f}ms")
    print(f"  {'closed, omission-corrected':<34}{len(corrected):>7}"
          f"{'':>11}{pct(corrected, 0.50):>9.1f}ms{pct(corrected, 0.99):>9.1f}ms"
          f"{corrected[-1]:>10.1f}ms")
    print()
    print(f"  open-model generator lateness p99: {os_['late_p99']:.2f} ms")
    print("  (if that number is large the generator itself fell behind and is now")
    print("   coordinating omission too, arrival schedule or not -- k6's warning")
    print("   about not being able to allocate enough VUs is the same tell.)")
    print()
    histogram("open model  ", open_cell.lat_ms)
    print()
    histogram("closed loop ", closed_cell.lat_ms)
    print()
    print("  Same server. Same nominal rate. Read the two histograms' right-hand")
    print("  ends against each other, then read the closed loop's raw p99 against")
    print("  its corrected p99.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
