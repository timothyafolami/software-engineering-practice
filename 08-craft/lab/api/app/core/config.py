"""Every environment variable in lab/README.md, read in exactly one place.

WHAT THIS DEMONSTRATES: the topic READMEs flip behaviour by setting env vars
(ERROR_MODE, PAGINATION_STRATEGY, POOL_TIMEOUT_S, ...). If those names are read
in six modules, changing one becomes an archaeology exercise. They are read
here and nowhere else.

WHAT TO LOOK FOR: `Settings.describe()` prints the active configuration at
startup, so every recorded measurement has the configuration that produced it
sitting next to it in the log.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict


def _opt_float(name: str) -> float | None:
    raw = os.environ.get(name)
    return None if raw in (None, "") else float(raw)


def _opt_int(name: str) -> int | None:
    raw = os.environ.get(name)
    return None if raw in (None, "") else int(raw)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://app:app@toxiproxy:5433/craft_lab"
        )
    )
    pool_size: int = field(default_factory=lambda: int(os.environ.get("POOL_SIZE", "5")))
    max_overflow: int = field(default_factory=lambda: int(os.environ.get("MAX_OVERFLOW", "10")))
    # unset means "wait forever for a connection", which is topic 7's baseline
    # and the reason its baseline ladder shows unbounded latency rather than errors
    pool_timeout_s: float | None = field(default_factory=lambda: _opt_float("POOL_TIMEOUT_S"))
    statement_timeout_ms: int | None = field(default_factory=lambda: _opt_int("STATEMENT_TIMEOUT_MS"))
    request_deadline_ms: int | None = field(default_factory=lambda: _opt_int("REQUEST_DEADLINE_MS"))
    retry_attempts: int = field(default_factory=lambda: int(os.environ.get("RETRY_ATTEMPTS", "0")))
    retry_budget_pct: float = field(default_factory=lambda: float(os.environ.get("RETRY_BUDGET_PCT", "0")))
    breaker_latency_ms: int | None = field(default_factory=lambda: _opt_int("BREAKER_LATENCY_MS"))
    error_mode: str = field(default_factory=lambda: os.environ.get("ERROR_MODE", "swallow"))
    pagination_strategy: str = field(default_factory=lambda: os.environ.get("PAGINATION_STRATEGY", "narrow"))
    seed_orders: int = field(default_factory=lambda: int(os.environ.get("SEED_ORDERS", "50000")))

    def __post_init__(self):
        if self.error_mode not in {"swallow", "none", "correct"}:
            raise ValueError(
                f"ERROR_MODE={self.error_mode!r}; expected swallow, none or correct. "
                "Failing at startup rather than defaulting silently -- a typo'd "
                "ERROR_MODE that quietly means 'swallow' would invalidate a whole "
                "row of topic 3's table without telling you."
            )
        if self.pagination_strategy not in {"narrow", "wide"}:
            raise ValueError(f"PAGINATION_STRATEGY={self.pagination_strategy!r}; expected narrow or wide")

    def describe(self) -> str:
        return " ".join(f"{k}={v}" for k, v in asdict(self).items())


settings = Settings()
