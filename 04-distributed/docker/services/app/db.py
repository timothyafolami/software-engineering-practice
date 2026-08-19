"""Shared Postgres helpers for the layer-4 lab services."""
import os
from psycopg_pool import ConnectionPool

_pools: dict[str, ConnectionPool] = {}


def pool(dsn_env: str, default: str = "", size: int = 8) -> ConnectionPool:
    dsn = os.environ.get(dsn_env) or default
    if not dsn:
        raise RuntimeError(f"{dsn_env} is not set and there is no default")
    if dsn not in _pools:
        _pools[dsn] = ConnectionPool(dsn, min_size=1, max_size=size, open=True,
                                     kwargs={"autocommit": True})
    return _pools[dsn]
