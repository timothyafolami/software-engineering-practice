"""Fixtures shared by every level. The container fixture is session-scoped.

WHAT TO LOOK FOR: `pg_engine` is `scope="session"` and each test runs inside a
transaction that is rolled back. Topic 4's broken-experiment note says an
integration suite more than roughly an order of magnitude slower than the mocked
one warm means you started a container per test -- this is the fixture shape
that avoids it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def pytest_collection_modifyitems(config, items):
    """Skip container tests with a message that names the unblock command."""
    import importlib.util
    import shutil
    import subprocess

    # A running daemon is only half the prerequisite. `pg_url` imports
    # `testcontainers` INSIDE the fixture, so without the package every
    # container test ERRORS with a bare ModuleNotFoundError during setup --
    # after collection, with no unblock message anywhere near it. Check the
    # import here, where the skip reason can still name the fix.
    if importlib.util.find_spec("testcontainers") is None:
        _skip_container(
            items,
            "the `testcontainers` package is not installed, so the Postgres 18 "
            "fixture cannot start one. Unblock: python3 -m pip install "
            "'testcontainers[postgres]'",
        )
        return

    docker_up = False
    if shutil.which("docker"):
        # BOUNDED. `docker info` talks to a socket, and a Docker Desktop that is
        # mid-start, mid-stop or wedged answers slowly or never -- which would
        # hang pytest COLLECTION, before a single test runs, with no output to
        # explain it. A probe for "is it up" that can block forever is worse
        # than no probe: treat a timeout as down and let the skip message send
        # the reader to Docker Desktop.
        try:
            docker_up = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10
            ).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            docker_up = False
    if docker_up:
        return
    _skip_container(
        items,
        "Docker daemon is not running. Start Docker Desktop, then: "
        "cd 08-craft/lab && docker compose up -d postgres",
    )


def _skip_container(items, reason):
    """Mark every `container` test skipped, with a reason that names the fix."""
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "container" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def pg_url():
    """Postgres 18 in a container, started once for the whole session."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
async def pg_engine(pg_url):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def pg_session(pg_engine):
    """One transaction per test, rolled back. Real semantics at unit-test speed."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with pg_engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            yield session
        await trans.rollback()
