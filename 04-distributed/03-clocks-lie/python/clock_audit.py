"""
Layer 4 Topic 3 (Part A) -- Python's clocks, audited rather than assumed.

WHAT THIS DEMONSTRATES: four things, in order.
  1. the clock inventory: which of Python's time functions is settable, which is
     monotonic, and the resolution each one actually delivers on THIS machine;
  2. a span timed twice -- once through the application's own now(), which reads
     the wall clock, and once through time.perf_counter() -- while an NTP-style
     backwards step is applied mid-run;
  3. the footgun specific to this runtime: datetime.utcnow() is deprecated from
     3.12, and time.CLOCK_BOOTTIME does not exist on Darwin;
  4. the summary line for the README's record table.

WHAT TO LOOK FOR IN THE OUTPUT: the NEGATIVE DURATIONS count in section 2. Every
one of those is a span whose wall-clock arithmetic produced a duration that runs
backwards through time. The monotonic column beside it stays sane through the
exact same step. Nothing about the workload changed; only which clock was read.

  python3 python/clock_audit.py
"""
from __future__ import annotations

import datetime
import platform
import threading
import time
import warnings

STEP_SECONDS = -40.0   # an NTP correction, applied backwards, mid-run
SPANS = 400
SPAN_WORK_US = 200


# --------------------------------------------------------------- 1. inventory

def measure_resolution(read, trials: int = 20) -> float:
    """Smallest non-zero delta this clock will report, in seconds.

    Not the documented resolution -- the one you get. A clock can advertise
    nanoseconds and tick in microseconds.
    """
    deltas = []
    for _ in range(trials):
        a = read()
        while True:
            b = read()
            if b != a:
                deltas.append(abs(b - a))
                break
    return min(deltas)


def inventory() -> None:
    print("-" * 78)
    print("1. the clocks Python offers, and what each one is for")
    print("-" * 78)
    print(f"  {'function':<34}{'kind':<12}{'settable':<10}{'measured resolution'}")

    rows = [
        ("time.time()", "realtime", "YES", time.time),
        ("time.monotonic()", "monotonic", "no", time.monotonic),
        ("time.perf_counter()", "monotonic", "no", time.perf_counter),
        ("time.time_ns()/1e9", "realtime", "YES", lambda: time.time_ns() / 1e9),
        ("time.clock_gettime(MONOTONIC_RAW)", "monotonic", "no",
         lambda: time.clock_gettime(time.CLOCK_MONOTONIC_RAW)),
    ]
    for name, kind, settable, read in rows:
        res = measure_resolution(read)
        print(f"  {name:<34}{kind:<12}{settable:<10}{res * 1e9:12.0f} ns")

    # What the interpreter itself claims, which is not always what you measure.
    print()
    for name in ("time", "monotonic", "perf_counter"):
        info = time.get_clock_info(name)
        print(f"  get_clock_info({name!r}):  adjustable={info.adjustable}  "
              f"monotonic={info.monotonic}  claimed resolution={info.resolution * 1e9:.0f} ns")


# ------------------------------------------------- 2. one span, two clocks

class AppClock:
    """The application's own now(). Every service has one; most read the wall clock.

    The offset stands in for an NTP step. We do not touch the system clock --
    lab/README.md explains why that is not even possible per-container on this
    machine, and doing it at the application layer makes the independent variable
    explicit instead of magic.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return time.time() + self.offset

    def step(self, seconds: float) -> None:
        with self._lock:
            self.offset += seconds


def burn(micros: int) -> None:
    end = time.perf_counter() + micros / 1e6
    while time.perf_counter() < end:
        pass


def span_comparison(clock: AppClock) -> tuple[list[float], list[float]]:
    """Time the same work twice, and step the app clock INSIDE two of the spans.

    The steps are applied at fixed span indices rather than from a timer, and
    deliberately so: a timer racing an 80ms loop is how you get a run where the
    step lands between spans and the experiment silently proves nothing. The
    README lists that exact outcome under "what would mean the experiment is
    broken". Determinism here is worth more than realism, because the realism is
    in *what* the step does, not in when it arrives.
    """
    wall_ms: list[float] = []
    mono_ms: list[float] = []
    step_back_at = SPANS // 3
    step_fwd_at = (2 * SPANS) // 3

    for i in range(SPANS):
        w0, m0 = clock.now(), time.perf_counter()
        burn(SPAN_WORK_US)
        if i == step_back_at:
            clock.step(STEP_SECONDS)        # NTP decides we are ahead: jump back
        elif i == step_fwd_at:
            clock.step(-STEP_SECONDS)       # and back again: jump forward
        w1, m1 = clock.now(), time.perf_counter()
        wall_ms.append((w1 - w0) * 1000.0)
        mono_ms.append((m1 - m0) * 1000.0)

    return wall_ms, mono_ms


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(q * len(s) + 0.5) - 1))
    return s[i]


def span_report(wall_ms: list[float], mono_ms: list[float]) -> int:
    print()
    print("-" * 78)
    print(f"2. {SPANS} identical spans, timed twice, with a {STEP_SECONDS:+.0f}s step "
          f"and a {-STEP_SECONDS:+.0f}s step landing INSIDE two of them")
    print("-" * 78)
    print(f"  {'clock':<26}{'p50':>10}{'p99':>12}{'max':>14}{'min':>14}{'negative':>10}")
    negatives = 0
    for name, v in (("wall (app now())", wall_ms), ("monotonic (perf_counter)", mono_ms)):
        neg = sum(1 for x in v if x < 0)
        if name.startswith("wall"):
            negatives = neg
        print(f"  {name:<26}{pct(v, 0.50):10.3f}{pct(v, 0.99):12.3f}"
              f"{max(v):14.1f}{min(v):14.1f}{neg:10d}")
    print("  (milliseconds; 'negative' counts spans that finished before they started)")
    print()
    hot = wall_ms.index(max(wall_ms))
    window = wall_ms[max(0, hot - 19):hot + 21]
    print(f"  Two samples out of {SPANS} were touched: {min(wall_ms):.0f} ms and "
          f"{max(wall_ms):.0f} ms,")
    print(f"  against a p50 of {pct(wall_ms, 0.50):.3f} ms. Over all {SPANS} spans that is only "
          f"the max --")
    print(f"  one sample in {SPANS} cannot move a p99 by rank. But dashboards aggregate")
    print(f"  windows, not runs: over the {len(window)} spans around the step the wall-clock")
    print(f"  p99 is {pct(window, 0.99):.1f} ms against a monotonic p99 of "
          f"{pct(mono_ms[max(0, hot - 19):hot + 21], 0.99):.3f} ms.")
    print("  Same workload, same machine, same instant. Only the clock differed.")
    print("  Rule this out BEFORE you go looking for the spike in application code.")
    return negatives


# ------------------------------------------------------ 3. the Python footgun

def footguns() -> bool:
    print()
    print("-" * 78)
    print("3. the footguns specific to this runtime")
    print("-" * 78)
    reproduced = True

    # (a) datetime.utcnow() -- deprecated in 3.12 because it returns a NAIVE
    # datetime that claims to be UTC, so every comparison with an aware datetime
    # is either an error or, worse, silently wrong by your local offset.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        naive = datetime.datetime.utcnow()  # noqa: DTZ003 - the point of the demo
        aware = datetime.datetime.now(datetime.UTC)
    warned = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    print(f"  datetime.utcnow()      -> {naive.isoformat()}   tzinfo={naive.tzinfo}")
    print(f"  datetime.now(UTC)      -> {aware.isoformat()}   tzinfo={aware.tzinfo}")
    print(f"  deprecation warning    -> {'YES: ' + str(warned[0].message) if warned else 'none raised'}")
    try:
        naive - aware
        print("  naive - aware          -> subtracted cleanly (this build allows it)")
    except TypeError as exc:
        print(f"  naive - aware          -> TypeError: {exc}")
    reproduced &= bool(warned)

    # (b) CLOCK_BOOTTIME does not exist on Darwin. The equivalents are
    # CLOCK_MONOTONIC_RAW (unslewed) and CLOCK_UPTIME_RAW (excludes sleep).
    print()
    print(f"  platform               -> {platform.system()} {platform.release()} "
          f"{platform.machine()}")
    for const in ("CLOCK_MONOTONIC", "CLOCK_MONOTONIC_RAW", "CLOCK_UPTIME_RAW",
                  "CLOCK_BOOTTIME", "CLOCK_REALTIME"):
        if hasattr(time, const):
            value = time.clock_gettime(getattr(time, const))
            print(f"  time.{const:<22}-> present, reads {value:.6f}")
        else:
            print(f"  time.{const:<22}-> AttributeError: not on this platform")

    # CLOCK_MONOTONIC vs CLOCK_MONOTONIC_RAW: RAW is not slewed by NTP. On a
    # machine NTP is actively correcting, the two drift apart; that difference is
    # the correction itself, which is the thing this whole topic is about.
    a = time.clock_gettime(time.CLOCK_MONOTONIC)
    b = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    print(f"  MONOTONIC - MONOTONIC_RAW -> {(a - b):+.6f} s "
          f"(the slew NTP has applied since boot, if any)")
    return reproduced


def main() -> int:
    print("=" * 78)
    print("Layer 4 Topic 3 -- Python clock audit")
    print("=" * 78)
    print(f"  Python {platform.python_version()} on {platform.system()} "
          f"{platform.machine()}")
    print()
    inventory()
    clock = AppClock()
    negatives = span_report(*span_comparison(clock))
    reproduced = footguns()

    print()
    print("-" * 78)
    print("4. one line for the record table in the README")
    print("-" * 78)
    res = measure_resolution(time.perf_counter)
    print(f"  | Python | time.perf_counter() | {res * 1e9:.0f} ns | "
          f"{'yes' if reproduced else 'NO -- investigate'} "
          f"({negatives} negative wall-clock span{'' if negatives == 1 else 's'}) |")
    print()
    print("  The table in the README stays blank until you fill it in. This line is")
    print("  the measurement, not the answer -- copy it across yourself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
