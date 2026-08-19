"""
Layer 5 lab - deadline propagation and zombie accounting (topic 2).

WHAT THIS DEMONSTRATES
  A timeout is a local decision; a deadline is a shared fact. The gateway
  gives up at CLIENT_TIMEOUT_MS either way. The difference is whether the
  hops behind it are told when the caller stops caring:

    PROPAGATE_DEADLINE=0  every hop starts a fresh CLIENT_TIMEOUT_MS clock,
                          so work continues after the gateway has already
                          returned 504. That work is a zombie: it holds a
                          pool slot, it burns a connection, and nobody is
                          left to receive it.

    PROPAGATE_DEADLINE=1  every hop reads the absolute deadline, refuses
                          immediately if less than DEADLINE_SLACK_MS
                          remains, sets its outbound timeout to
                          remaining - slack, and derives statement_timeout
                          from the same number.

WHAT TO LOOK FOR
  `zombies` in /admin/counters, and C's pool utilisation while it climbs.
  Those two together are the topic: zombie work is not wasted CPU, it is
  occupied SLOTS, which is topic 1's bound.

HOW ZOMBIES ARE COUNTED IN THE NAIVE VARIANT
  The gateway stamps the deadline header on every request in BOTH variants.
  In the naive variant no hop reads it to make a decision - only the
  counter reads it, on the way out, to ask "did I finish after the caller
  gave up?". Measuring the naive variant otherwise would mean guessing.
  The flag decides whether the header is OBEYED, not whether it is SENT.
"""
from __future__ import annotations

import time

from .config import config
from .metrics import counters

OBSERVED_HEADER = "X-Observed-Deadline"


def now_ms() -> float:
    return time.time() * 1000.0


def read_deadline(headers) -> float | None:
    """Absolute deadline in unix millis, from the configured header."""
    name = config.get("DEADLINE_HEADER")
    raw = headers.get(name) or headers.get(OBSERVED_HEADER)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def remaining_ms(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - now_ms()


def should_reject(deadline: float | None) -> bool:
    """True when there is not enough budget left to be worth starting.

    Only consulted when PROPAGATE_DEADLINE is on. DEADLINE_SLACK_MS is the
    floor: below it the answer cannot arrive in time, so returning 504 now
    is strictly better than occupying a slot to produce a late answer.
    """
    if not config.get("PROPAGATE_DEADLINE"):
        return False
    left = remaining_ms(deadline)
    return left is not None and left < config.get("DEADLINE_SLACK_MS")


def outbound_timeout_ms(deadline: float | None) -> int:
    """What to give the next hop: the remaining budget, minus one hop of slack."""
    configured = int(config.get("CLIENT_TIMEOUT_MS"))
    if not config.get("PROPAGATE_DEADLINE"):
        return configured
    left = remaining_ms(deadline)
    if left is None:
        return configured
    return max(1, int(min(configured, left - config.get("DEADLINE_SLACK_MS"))))


def statement_timeout_ms(deadline: float | None) -> int | None:
    """SET LOCAL statement_timeout for this request.

    Explicit STATEMENT_TIMEOUT_MS wins. Otherwise, under propagation, it is
    derived from the same remaining budget - which is the point: one
    number, honoured all the way down to the query planner, instead of four
    independent timeouts that happen to be in the same file.
    """
    explicit = config.get("STATEMENT_TIMEOUT_MS")
    if explicit is not None:
        return int(explicit)
    if not config.get("PROPAGATE_DEADLINE"):
        return None
    left = remaining_ms(deadline)
    if left is None:
        return None
    return max(1, int(left - config.get("DEADLINE_SLACK_MS")))


def headers_for_next_hop(deadline: float | None, incoming) -> dict[str, str]:
    """Carry the deadline downward, plus the observational copy."""
    out: dict[str, str] = {}
    if deadline is not None:
        out[config.get("DEADLINE_HEADER")] = f"{deadline:.0f}"
        out[OBSERVED_HEADER] = f"{deadline:.0f}"
    rid = incoming.get("X-Request-Id")
    if rid:
        out["X-Request-Id"] = rid
    return out


def record_completion(deadline: float | None, did_work: bool = True) -> bool:
    """Count this completion, and say whether it was a zombie.

    A zombie is work that finished after its caller's deadline: the answer
    is correct, complete, and worthless. Called on the way out of the leaf.

    `did_work` is what keeps the metric honest. A request that was refused
    on arrival, abandoned at the pool, or cancelled by statement_timeout
    also returns after the deadline - but it returns having consumed
    nothing, which is the OPPOSITE of a zombie. Counting those would make
    the propagated variant report more zombies than the naive one purely
    because it gives up out loud, and topic 2's headline comparison would
    come out backwards.
    """
    if not did_work:
        return False
    left = remaining_ms(deadline)
    if left is not None and left <= 0:
        counters.inc("zombies")
        return True
    return False
