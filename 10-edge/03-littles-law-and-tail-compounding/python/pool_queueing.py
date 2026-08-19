"""
Layer 10 - Topic 3: the pool is the concurrency limit, and Kingman is the
shape of the approach to it. (Python / asyncio)

What this demonstrates
    Three things, measured rather than asserted, against a backend whose
    service time this program sets exactly -- because Little's Law and
    Kingman are statements about arrival and service distributions, and you
    cannot check them against a backend whose duration you do not control.

      Part 1  L = λW as a wall. c pool slots and a mean service time W pin
              the maximum throughput at c/W. Below it, acquire wait is
              near zero. Above it, the queue grows without bound and the
              service time never changes.
      Part 2  Kingman. Same ρ, same mean service time, two service-time
              distributions: fixed (c_s = 0) and exponential (c_s = 1).
              Wq ~= (ρ/(1-ρ)) x ((c_a^2 + c_s^2)/2) x τ says the second
              queue is twice as deep. That factor is the whole reason LLM
              serving needs admission control that CRUD serving did not.
      Part 3  The Python-specific trap: SQLAlchemy's effective c is
              pool_size PLUS max_overflow, so a service configured with
              "pool_size 5" saturates at 3x where its owner predicted.

What to look for
    - Part 1: `service p50` stays flat while `acquire p99` explodes. That
      split is the graph the topic is about, and it is why acquire wait has
      to be timed separately from query time.
    - Part 2: the ratio of measured Wq between the two rows, against the
      predicted 2.0. Kingman is an approximation for GI/G/1, so treat
      agreement within tens of percent as confirmation, not the digits.
    - Part 3: the measured wall is at c = pool_size + max_overflow. Both
      halves count.

Arrivals are Poisson (c_a = 1) and OPEN-LOOP: the generator issues on a
clock and does not wait for responses. A closed-loop generator cannot
produce an unbounded queue at all, because it throttles itself exactly
when the system is in trouble.

No dependencies. Runs with no arguments (about 25 seconds):
    python3 python/pool_queueing.py
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field

SEED = 20260818


@dataclass
class Sample:
    acquire: float
    service: float
    total: float


@dataclass
class Run:
    label: str
    lam: float
    slots: int
    mean_service: float
    cs: float
    samples: list[Sample] = field(default_factory=list)
    issued: int = 0
    completed: int = 0
    # Completions that landed INSIDE the arrival window. Throughput must be
    # counted over the same interval as `wall`: `completed` keeps rising
    # during the drain, and dividing the post-drain total by the arrival
    # window reports a rate above c/W -- i.e. above the wall this whole
    # section says cannot be crossed.
    completed_in_window: int = 0
    area: float = 0.0        # integral of in-system count dt, for measured L
    wall: float = 0.0

    @property
    def rho(self) -> float:
        return self.lam * self.mean_service / self.slots

    @property
    def lambda_max(self) -> float:
        return self.slots / self.mean_service

    def pct(self, attr: str, q: float) -> float:
        vals = sorted(getattr(s, attr) for s in self.samples)
        if not vals:
            return float("nan")
        idx = min(len(vals) - 1, int(q * len(vals)))
        return vals[idx]

    def mean(self, attr: str) -> float:
        vals = [getattr(s, attr) for s in self.samples]
        return statistics.fmean(vals) if vals else float("nan")

    def cv(self, attr: str) -> float:
        """Coefficient of variation, sigma/mu, of an observed series."""
        vals = [getattr(s, attr) for s in self.samples]
        if len(vals) < 2:
            return float("nan")
        return statistics.stdev(vals) / statistics.fmean(vals)


def kingman_wq(rho: float, ca: float, cs: float, tau: float) -> float:
    """Wq ~= (rho/(1-rho)) x ((ca^2 + cs^2)/2) x tau, for a single server."""
    if rho >= 1.0:
        return float("inf")
    return (rho / (1 - rho)) * ((ca ** 2 + cs ** 2) / 2) * tau


async def drive(label: str, lam: float, slots: int, mean_service: float,
                cs: float, duration: float) -> Run:
    """Open-loop Poisson arrivals against a `slots`-wide pool."""
    rng = random.Random(SEED)
    run = Run(label, lam, slots, mean_service, cs)
    pool = asyncio.Semaphore(slots)
    in_system = 0
    last_change = time.perf_counter()

    def track(delta: int) -> None:
        # Integrate in-system count over time, so measured L can be checked
        # against λW rather than assumed equal to it.
        nonlocal in_system, last_change
        now = time.perf_counter()
        run.area += in_system * (now - last_change)
        last_change = now
        in_system += delta

    def service_time() -> float:
        # cs = 0 -> deterministic; cs = 1 -> exponential, same mean.
        if cs == 0:
            return mean_service
        return rng.expovariate(1.0 / mean_service)

    async def one_request() -> None:
        track(+1)
        arrived = time.perf_counter()
        async with pool:
            acquired = time.perf_counter()
            await asyncio.sleep(service_time())
            done = time.perf_counter()
        track(-1)
        run.completed += 1
        run.samples.append(Sample(acquired - arrived, done - acquired, done - arrived))

    tasks: list[asyncio.Task] = []
    start = time.perf_counter()
    next_arrival = start
    while True:
        now = time.perf_counter()
        if now - start >= duration:
            break
        next_arrival += rng.expovariate(lam)
        delay = next_arrival - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        run.issued += 1
        tasks.append(asyncio.create_task(one_request()))

    run.wall = time.perf_counter() - start
    run.completed_in_window = run.completed
    # Drain, but do not wait forever on a queue that is growing: past the
    # wall it never drains, and that IS the result.
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                               timeout=duration)
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
    track(0)
    return run


async def calibrate(mean_service: float, cs: float, n: int = 200) -> float:
    """Mean service time actually achieved, with no contention.

    An event loop's sleep granularity is a fixed cost added to every
    service time. Computing ρ from the NOMINAL service time therefore puts
    the two arms of Part 2 at different real utilisations, and 1/(1-ρ)
    swamps the variance factor you were trying to isolate. Measure first,
    then set λ. This is the same discipline the topic asks for in
    production: ρ computed from an assumed W is a guess.
    """
    rng = random.Random(SEED + 1)
    total = 0.0
    for _ in range(n):
        d = mean_service if cs == 0 else rng.expovariate(1.0 / mean_service)
        t0 = time.perf_counter()
        await asyncio.sleep(d)
        total += time.perf_counter() - t0
    return total / n


def header(title: str) -> None:
    print(f"\n{title}")
    print("-" * 78)


def row(run: Run) -> None:
    measured_l = run.area / run.wall if run.wall else float("nan")
    print(f"  {run.label:<22} {run.rho:>5.2f} "
          f"{run.pct('acquire', 0.5) * 1000:>9.1f} {run.pct('acquire', 0.99) * 1000:>9.1f} "
          f"{run.pct('service', 0.5) * 1000:>9.1f} {run.pct('total', 0.99) * 1000:>9.1f} "
          f"{run.completed_in_window / run.wall:>9.0f} {measured_l:>7.1f}")


async def main() -> None:
    print("Python / asyncio - pool queueing, Little's Law and Kingman")
    print(f"  arrivals: Poisson (c_a = 1), open loop, seed {SEED}")

    # ---------------- Part 1: the wall -----------------------------------
    slots, service = 20, 0.050
    header(f"Part 1 - L = λW. c = {slots} slots, W = {service * 1000:.0f}ms fixed, "
           f"so λ_max = c/W = {slots / service:.0f} req/s")
    print(f"  {'run':<22} {'ρ':>5} {'acq p50':>9} {'acq p99':>9} "
          f"{'svc p50':>9} {'tot p99':>9} {'done/s':>9} {'L':>7}")
    for lam in (200, 360, 400, 440):
        row(await drive(f"λ={lam}", lam, slots, service, cs=0.0, duration=3.0))
    print("\n  Service time is identical in every row. Everything that moved is")
    print("  queueing for a slot -- which is why acquire wait has to be its own")
    print("  timer. A single 'request duration' histogram hides the mechanism.")

    # ---------------- Part 2: Kingman ------------------------------------
    #
    # τ is 20ms rather than something smaller on purpose: an event loop's
    # sleep granularity is a fixed cost added to every service time, and at
    # τ = 1ms it is most of the service time. That inflates the real mean,
    # pushes real ρ past the nominal one, and produces an unbounded queue
    # that looks like Kingman being wrong. Everything below is therefore
    # computed from the MEASURED service distribution, not the nominal one.
    slots, service, rho_target = 1, 0.020, 0.85
    header(f"Part 2 - Kingman. c = {slots}, nominal τ = {service * 1000:.0f}ms, "
           f"target ρ = {rho_target:.2f} for both arms")
    print("  λ is calibrated per arm from the MEASURED service time, so both")
    print("  rows sit at the same real ρ and the only difference left is c_s.")
    print(f"  {'service dist':<16} {'c_s meas':>9} {'τ meas':>9} {'ρ meas':>8} "
          f"{'Wq pred':>9} {'Wq meas':>9} {'ratio':>7}")
    results = {}
    for name, cs in (("fixed", 0.0), ("exponential", 1.0)):
        tau_cal = await calibrate(service, cs)
        lam = rho_target * slots / tau_cal
        # 30s, not 10s. The mean wait of a c_s = 1 queue at rho = 0.85 is a
        # heavy-tailed average: a 10-second window does not contain enough of
        # the rare deep excursions to estimate it, so a short run reports a
        # ratio near 1 and makes Kingman look wrong. Run length is the whole
        # difference. This is the same reason a five-minute load test
        # understates your production p99.
        run = await drive(name, lam, slots, service, cs=cs, duration=30.0)
        tau_measured = run.mean("service")
        cs_measured = run.cv("service")
        rho_measured = lam * tau_measured / slots
        predicted = kingman_wq(rho_measured, ca=1.0, cs=cs_measured, tau=tau_measured)
        measured = run.mean("acquire")
        results[name] = (predicted, measured)
        print(f"  {name:<16} {cs_measured:>9.2f} {tau_measured * 1000:>8.1f}ms "
              f"{rho_measured:>8.2f} {predicted * 1000:>8.1f}ms "
              f"{measured * 1000:>8.1f}ms {measured / predicted:>7.2f}")
    pred_ratio = results["exponential"][0] / results["fixed"][0]
    meas_ratio = results["exponential"][1] / results["fixed"][1]
    print(f"\n  exponential/fixed queueing: predicted {pred_ratio:.2f}x, "
          f"measured {meas_ratio:.2f}x")
    # State the verdict from the numbers actually obtained rather than
    # asserting it. Kingman is an approximation for GI/G/1, and this is a
    # finite-length simulation, so the honest reading is a band.
    gap = abs(meas_ratio - pred_ratio) / pred_ratio
    if meas_ratio > 1.0 and gap <= 0.35:
        verdict = ("  Measured tracks predicted: same utilisation, same mean "
                   "service\n  time, and the variable-service queue is the "
                   "deeper one.")
    elif meas_ratio > 1.0:
        verdict = ("  The direction holds -- the variable-service queue is the "
                   "deeper\n  one -- but the measured ratio falls short of the "
                   "prediction. That\n  gap is run length, not a refutation: "
                   "re-run and watch it move.")
    else:
        verdict = ("  BROKEN RUN: the variable-service queue came out no deeper "
                   "than\n  the fixed one. Check rho meas is equal in both rows "
                   "before\n  reading anything else -- 1/(1-rho) swamps the "
                   "variance factor.")
    print(verdict)
    print("  That is the difference between CRUD traffic and traffic where one")
    print("  request emits 20 tokens and the next emits 2000. Kingman is an")
    print("  approximation for GI/G/1 and this is a finite sample: read the")
    print("  RATIO rather than the digits, and read rho meas first -- the two")
    print("  arms must sit at the same rho or the comparison is not one.")

    # ---------------- Part 3: the Python-specific trap --------------------
    header("Part 3 - the effective c is pool_size + max_overflow")
    print("  SQLAlchemy hands out max_overflow extra connections beyond")
    print("  pool_size. Both count in L = λW, and only one of them is in the")
    print("  variable most people read.")
    print(f"\n  {'config':<26} {'c':>3} {'λ_max':>7} {'ρ at λ=250':>11} "
          f"{'acq p99':>10} {'done/s':>9}")
    for label, c in (("pool_size=5", 5), ("pool_size=5, overflow=10", 15)):
        run = await drive(label, 250, c, 0.050, cs=0.0, duration=3.0)
        print(f"  {label:<26} {c:>3} {run.lambda_max:>7.0f} {run.rho:>11.2f} "
              f"{run.pct('acquire', 0.99) * 1000:>8.1f}ms "
              f"{run.completed_in_window / run.wall:>9.0f}")
    print("\n  Predicting the knee from pool_size alone puts it at 100 req/s.")
    print("  The service actually falls over near 300. Same code, same load,")
    print("  and the arithmetic was right -- the input was wrong.")


if __name__ == "__main__":
    asyncio.run(main())
