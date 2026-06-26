"""Database engine + session factory.

The engine is built lazily so importing this module does not
trigger a connection attempt (which would fail in unit tests
that don't have a Postgres instance available).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, building it on first use."""
    global _engine, _session_factory
    if _engine is None:
        # Imports kept inside the function so this module is
        # importable without an active database connection.
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.config import get_settings

        _engine = create_async_engine(get_settings().database_url, future=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, building the engine if needed."""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a transactional session.

    The session is closed automatically by the ``async with``
    context; route handlers are expected to ``await session.commit()``
    explicitly when they want the change to persist.
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session
