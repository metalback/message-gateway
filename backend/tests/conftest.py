"""Shared pytest fixtures for the backend test suite.

Currently exposes a single helper – ``async_session`` – that
yields an in-memory SQLite :class:`AsyncSession` bound to a
fresh database. The fixture is intentionally narrow: any test
that needs a different connection (e.g. to assert the cache
state of the engine) should build it locally rather than
parameterise the global fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  -- side-effect import to register tables
from app.models.base import Base


@pytest_asyncio.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh in-memory SQLite session for unit tests.

    The fixture creates a brand-new engine + session factory per
    test so a transaction that fails halfway through does not
    leak into the next case. ``Base.metadata.create_all`` runs
    once at fixture setup; the connection is closed (and the
    database thrown away) on teardown.

    Why SQLite and not PostgreSQL? The unit tests are
    intentionally pure-Python: we want them to run in CI on a
    vanilla Python image without spinning up a database
    container. SQLAlchemy abstracts over both backends; the
    features the auth flow uses (UUIDs, ``String`` columns,
    unique constraints) work identically in either engine.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()
