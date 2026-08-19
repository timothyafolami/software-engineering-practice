"""
Layer 5 lab - runtime-mutable configuration.

WHAT THIS IS
  Every knob the lab needs, read once from the environment at startup and
  writable afterwards through POST /admin/config. That is what makes
  "change ONLY pool_size and rerun" an honest instruction rather than a
  rebuild - the sweep scripts in ../scripts/ set their own parameters in
  setup() and the image never changes.

THE CONTRACT
  The names, defaults and meanings of everything in CONTRACT_FIELDS below
  are fixed by ../README.md. Topic code depends on them. Do not rename
  them here; change them there or not at all.

EXTRA FIELDS
  EXTRA_FIELDS are wiring the topic READMEs do not name but the container
  stack needs (where the next hop lives, which latency distribution a
  fan-out backend draws from, and so on). They are settable exactly the
  same way. They are listed separately so the contract stays visible.

TYPES
  Values arrive from the environment as strings and from /admin/config as
  JSON. Both paths go through the same coercion, so `{"POOL_SIZE": "8"}`
  and `{"POOL_SIZE": 8}` mean the same thing, and an unset optional value
  is None rather than the string "unset".
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable


def _int(v: Any) -> int:
    return int(str(v).strip())


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("none", "null", "unset"):
        return None
    return int(s)


def _float(v: Any) -> float:
    return float(str(v).strip())


def _str(v: Any) -> str:
    return str(v)


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _choice(*allowed: str) -> Callable[[Any], str]:
    def coerce(v: Any) -> str:
        s = str(v).strip()
        if s not in allowed:
            raise ValueError(f"expected one of {allowed}, got {s!r}")
        return s
    return coerce


# name -> (coerce, default). Default None means "unset" and is meaningful.
CONTRACT_FIELDS: dict[str, tuple[Callable[[Any], Any], Any]] = {
    "ROLE": (_choice("app", "gateway", "service_b", "service_c", "backend"), "app"),
    "SERVICE_MS": (_int, 40),
    "POOL_SIZE": (_int, 5),
    "MAX_OVERFLOW": (_int, 10),
    "POOL_TIMEOUT_S": (_float, 30.0),
    "STATEMENT_TIMEOUT_MS": (_opt_int, None),
    "CLIENT_TIMEOUT_MS": (_int, 500),
    "DEADLINE_HEADER": (_str, "X-Request-Deadline"),
    "DEADLINE_SLACK_MS": (_int, 20),
    "PROPAGATE_DEADLINE": (_bool, False),
    "RETRY_ATTEMPTS": (_int, 3),
    "RETRY_BASE_MS": (_int, 50),
    "RETRY_JITTER": (_choice("none", "full"), "none"),
    "RETRY_BUDGET_PCT": (_float, 0.0),
    "SHED_MODE": (_choice("none", "static", "priority", "adaptive"), "none"),
    "SHED_LIMIT": (_opt_int, None),
    "SHED_WAIT_MS": (_int, 50),
    "CACHE_TTL_S": (_int, 300),
    "IDEMPOTENCY_MODE": (_choice("naive", "correct"), "correct"),
    "UVICORN_BACKLOG": (_int, 2048),
    "UVICORN_LIMIT_CONCURRENCY": (_opt_int, None),
}

# Wiring the README's table does not name, because it is about where things
# live rather than about how the system behaves under load.
EXTRA_FIELDS: dict[str, tuple[Callable[[Any], Any], Any]] = {
    # Chain (topics 2, 3): who this hop calls next. Empty means "I am the leaf,
    # talk to Postgres instead".
    "DOWNSTREAM_URL": (_str, ""),
    # Fan-out (topic 6): DNS name that resolves to every `backend` replica,
    # and the distribution each replica draws its service time from.
    "BACKEND_HOST": (_str, "backend"),
    "BACKEND_PORT": (_int, 8000),
    "LATENCY_DIST": (_choice("fixed", "lognormal", "bimodal"), "fixed"),
    "LATENCY_P50_MS": (_int, 10),
    "LATENCY_TAIL_RATIO": (_float, 20.0),   # p99 / p50, per the topic 6 spec
    "BIMODAL_SLOW_PCT": (_float, 1.0),      # % of requests in the slow mode
    "HEDGE": (_bool, False),
    "HEDGE_AFTER_MS": (_opt_int, None),     # None -> measured backend p95
    "HEDGE_BUDGET_PCT": (_float, 5.0),
    # Storage. DATABASE_URL wins if set; otherwise assembled from the parts.
    "DATABASE_URL": (_str, ""),
    "PGHOST": (_str, "postgres"),
    "PGPORT": (_int, 5432),
    "PGUSER": (_str, "app"),
    "PGPASSWORD": (_str, "app"),
    "PGDATABASE": (_str, "failure_lab"),
    "REDIS_URL": (_str, "redis://redis:6379/0"),
    # Topic 5 bulkhead: /report gets its own small engine when this is on.
    "BULKHEAD": (_bool, False),
    "REPORT_POOL_SIZE": (_int, 3),
    "REPORT_SERVICE_MS": (_int, 2000),
    # Topic 4: how many distinct cache keys the workload rotates through.
    "CACHE_KEYS": (_int, 500),
    # Topic 7: how long an in_progress idempotency key may block a retry.
    "IDEMPOTENCY_TTL_S": (_int, 60),
}

FIELDS = {**CONTRACT_FIELDS, **EXTRA_FIELDS}

# Changing any of these has to rebuild the SQLAlchemy engine.
POOL_FIELDS = frozenset({"POOL_SIZE", "MAX_OVERFLOW", "POOL_TIMEOUT_S", "DATABASE_URL",
                         "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"})


class Config:
    """A dict of coerced values with a lock, and a record of what changed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        for name, (coerce, default) in FIELDS.items():
            raw = os.environ.get(name)
            if raw is None or raw == "":
                self._values[name] = default
            else:
                self._values[name] = coerce(raw)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not found normally, i.e. config keys.
        try:
            return object.__getattribute__(self, "_values")[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, name: str) -> Any:
        with self._lock:
            return self._values[name]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def apply(self, patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Coerce and apply.

        Returns (applied, unknown_keys); raises ValueError on a bad value, in
        which case nothing is applied. A JSON `null` resets the field to its
        startup default, which for the optional fields means genuinely unset.
        """
        unknown = [k for k in patch if k not in FIELDS]
        staged: dict[str, Any] = {}
        for key, value in patch.items():
            if key in unknown:
                continue
            coerce, default = FIELDS[key]
            staged[key] = default if value is None else coerce(value)
        applied: dict[str, Any] = {}
        with self._lock:
            for key, coerced in staged.items():
                if self._values[key] != coerced:
                    applied[key] = coerced
                self._values[key] = coerced
        return applied, unknown


config = Config()
