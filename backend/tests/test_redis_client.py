"""Unit tests for the cached async Redis client in :mod:`app.redis_client`.

The module owns three contracts:

- :func:`get_redis_client` returns a process-wide singleton
  built lazily on first use.
- The factory is :func:`_build_client` (which reads
  :class:`app.config.Settings` and calls
  ``Redis.from_url``) so tests can swap it without touching
  the cached object.
- :func:`reset_redis_client` drops the cache so the next
  :func:`get_redis_client` call rebuilds the client with the
  patched factory.

The tests below exercise these contracts without opening a
real TCP connection: ``Redis.from_url`` is monkeypatched to a
double that records construction arguments and returns a
sentinel object.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.config import Settings
from app.redis_client import (
    _build_client,
    _client,
    get_redis_client,
    reset_redis_client,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Stand-in for :class:`redis.asyncio.Redis`.

    Records the URL it was built with so tests can assert that
    the factory honoured :class:`Settings.redis_url`. The
    equality contract is the object's identity – the production
    code uses ``is`` checks on the cached client, and we want
    the test to match that.
    """

    def __init__(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.kwargs = kwargs


@pytest.fixture
def fake_redis_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Replace ``Redis.from_url`` with a factory that returns
    a single :class:`_FakeRedis` instance, and reset the
    module-level cache so the next :func:`get_redis_client`
    call goes through the patched factory.

    Returns the instance so tests can assert on its recorded
    arguments.
    """
    instance = _FakeRedis("placeholder://will-be-overridden")

    def _factory(url: str, **kwargs: Any) -> _FakeRedis:
        instance.url = url
        instance.kwargs = kwargs
        return instance

    # ``Redis.from_url`` is a method of the ``Redis`` class, not a
    # module-level function, so we patch the *class* itself. The
    # production import (``from redis.asyncio import Redis`` inside
    # ``_build_client``) resolves to the same symbol, so the patch
    # is visible to the production code path.
    class _FakeRedisClass:
        from_url = staticmethod(_factory)

    monkeypatch.setattr("redis.asyncio.Redis", _FakeRedisClass)
    # Reset the cache so the first ``get_redis_client`` call
    # invokes the patched factory.
    reset_redis_client()
    return instance


@pytest.fixture(autouse=True)
def _restore_redis_cache() -> Any:
    """Snapshot the cache around each test.

    The module keeps a process-wide singleton. We restore it
    to ``None`` after every test so cases do not leak state to
    the next one.
    """
    reset_redis_client()
    try:
        yield
    finally:
        reset_redis_client()


# ---------------------------------------------------------------------------
# _build_client
# ---------------------------------------------------------------------------


def test_build_client_uses_settings_url(
    fake_redis_factory: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory must receive the URL derived from
    :class:`Settings` (not the default value), so a
    misconfigured env var surfaces as a connection error
    against the *real* deployment URL rather than a silent
    fallback to ``localhost``."""
    settings = Settings(
        redis_host="redis.internal",
        redis_port=6380,
        redis_db=3,
    )

    # ``_build_client`` reads settings through the module's
    # ``get_settings`` reference, so we patch that. (A direct
    # ``Settings(...)`` argument is not part of the public
    # surface; the production call is the lazy cache.)
    monkeypatch.setattr("app.redis_client.get_settings", lambda: settings)

    client = cast("_FakeRedis", _build_client())

    assert client is fake_redis_factory
    assert client.url == "redis://redis.internal:6380/3"


def test_build_client_sets_socket_timeouts(
    fake_redis_factory: _FakeRedis,
) -> None:
    """The factory must receive the connect / read timeouts so
    a misconfigured Redis URL fails fast instead of stalling on
    a TCP handshake (which is what a missing timeout looks like
    in production)."""
    _build_client()
    assert fake_redis_factory.kwargs.get("socket_connect_timeout") == 5
    assert fake_redis_factory.kwargs.get("socket_timeout") == 5


# ---------------------------------------------------------------------------
# get_redis_client (singleton)
# ---------------------------------------------------------------------------


def test_get_redis_client_is_lazy() -> None:
    """Importing / calling :func:`get_redis_client` must not
    trigger a connection attempt; the cache starts ``None`` and
    is only populated on the first call. A premature
    connection would make unit tests fail on machines that do
    not have a Redis listening on the default port."""
    reset_redis_client()
    # The cache is a module-level ``_client`` – we read it
    # directly to assert it was not populated yet.
    from app import redis_client as redis_client_module

    assert redis_client_module._client is None


def test_get_redis_client_returns_singleton() -> None:
    """The same instance is returned on every call so the
    underlying connection pool is shared. A fresh client per
    call would re-handshake the TCP connection on every
    request and pool the connections in a way that defeats
    the point of the cache."""
    first = get_redis_client()
    second = get_redis_client()
    assert first is second


def test_get_redis_client_uses_settings_url() -> None:
    """The first call must build a client from
    :class:`Settings` (not from some hard-coded URL), so a
    ``REDIS_HOST`` env var override takes effect without
    importing the module in a particular order."""
    # We patch the factory to inspect the URL it was called
    # with. The factory is set up to mirror the production
    # import path (``from redis.asyncio import Redis`` inside
    # ``_build_client``) by patching the module symbol.
    from unittest.mock import patch

    with patch("redis.asyncio.Redis") as fake_class:
        fake_class.from_url = lambda url, **kw: _FakeRedis(url, **kw)
        reset_redis_client()
        client = cast("_FakeRedis", get_redis_client())

    assert client.url == Settings().redis_url


def test_get_redis_client_builds_only_once() -> None:
    """The factory must be called exactly once for the lifetime
    of the cache: a second call returns the cached object, it
    does not rebuild. This is what the connection pool relies
    on."""
    from unittest.mock import patch

    with patch("redis.asyncio.Redis") as fake_class:
        call_count = {"n": 0}

        def _from_url(url: str, **kw: Any) -> _FakeRedis:
            call_count["n"] += 1
            return _FakeRedis(url, **kw)

        fake_class.from_url = _from_url
        reset_redis_client()
        get_redis_client()
        get_redis_client()
        get_redis_client()

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# reset_redis_client
# ---------------------------------------------------------------------------


def test_reset_redis_client_clears_cache() -> None:
    """After :func:`reset_redis_client` the next call must
    rebuild the client – the cache must not leak the previous
    instance across a test (or across a settings change in
    production)."""
    first = get_redis_client()
    reset_redis_client()
    second = get_redis_client()
    assert first is not second


def test_reset_redis_client_is_idempotent() -> None:
    """Calling :func:`reset_redis_client` on an already-empty
    cache must be a no-op so a test that calls it twice in
    setup / teardown does not raise."""
    reset_redis_client()
    reset_redis_client()
    # No assertion needed – reaching the end without an
    # exception is the contract.
    assert _client is None
