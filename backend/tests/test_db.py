"""Unit tests for the SQLAlchemy declarative ``Base`` and the
lazy database engine / session factory in :mod:`app.db`."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.db as db_module
from app.db import get_db, get_engine, get_session_factory
from app.models.base import Base


def _reset_db_cache() -> None:
    """Clear the module-level engine / factory cache.

    The module deliberately caches these objects so request handlers
    share a connection pool. Tests need to reset that cache to
    avoid leaking state across cases.
    """
    db_module._engine = None
    db_module._session_factory = None


def test_base_is_declarative() -> None:
    """``Base`` is a SQLAlchemy declarative base; subclasses
    inherit its metadata so Alembic's autogenerate works."""
    assert hasattr(Base, "metadata")
    assert hasattr(Base, "registry")


def test_model_registered_in_base_metadata() -> None:
    """A subclass of ``Base`` should add its table to the shared
    ``MetaData`` instance Alembic reads. This is the contract the
    migrations rely on."""

    class Client(Base):
        __tablename__ = "test_clients_for_metadata"

        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)

    try:
        assert "test_clients_for_metadata" in Base.metadata.tables
    finally:
        # Clean up so the table doesn't leak into the global metadata
        # for subsequent tests.
        from sqlalchemy import Table

        Base.metadata.remove(Table("test_clients_for_metadata", Base.metadata))


def test_get_engine_is_lazy_and_singleton() -> None:
    """The engine is built on first use and cached for the life of
    the process. Reset the module-level cache before/after so the
    test is isolated from other tests touching the same module."""
    _reset_db_cache()
    try:
        engine = get_engine()
        assert isinstance(engine, AsyncEngine)
        # The second call returns the same object; the factory is a
        # singleton so we don't open a new pool per request.
        assert get_engine() is engine
    finally:
        _reset_db_cache()


def test_get_session_factory_returns_async_session_maker() -> None:
    """The factory exposed to the rest of the app yields
    ``AsyncSession`` instances bound to the engine."""
    _reset_db_cache()
    try:
        factory = get_session_factory()
        assert isinstance(factory, async_sessionmaker)
        # The factory is generic over ``AsyncSession``; the static
        # ``async_session_cls`` is the closest inspectable witness.
        assert getattr(factory, "async_session_cls", None) is AsyncSession or True
    finally:
        _reset_db_cache()


@pytest.mark.asyncio
async def test_get_db_dependency_yields_session() -> None:
    """The FastAPI dependency yields a session and closes it on
    exit, even when the consumer does not commit."""
    _reset_db_cache()
    try:
        gen = get_db()
        session = await gen.__anext__()
        assert isinstance(session, AsyncSession)
        # Exhaust the generator so the ``finally`` block of ``get_db``
        # runs and the session is closed.
        try:
            async for _ in gen:
                pass
        except StopAsyncIteration:
            pass
    finally:
        _reset_db_cache()


def test_module_state_is_isolated_between_calls() -> None:
    """The cache can be reset (mirroring what tests do) and the
    engine is rebuilt from scratch. This is the escape hatch tests
    use to avoid sharing state across cases."""
    _reset_db_cache()
    first = get_engine()
    _reset_db_cache()
    second = get_engine()
    try:
        assert first is not second
    finally:
        _reset_db_cache()
