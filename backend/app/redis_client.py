"""Async Redis client (singleton).

Mirrors the lazy / cached pattern used by :mod:`app.db`: the
client is built on first use and reused for the life of the
process, so request handlers, the Arq worker and the health
probe all share a single connection pool.

Why a centralised module?

- The :class:`redis.asyncio.Redis` client is fully thread-safe
  and reuses a connection pool internally; constructing a new
  client per call (as the original health check did) wastes the
  pool and adds handshake latency on every probe.
- The settings (host / port / db) live in :class:`app.config.Settings`
  and are read through the same ``get_settings()`` entry point the
  rest of the codebase uses, so a single :func:`get_settings.cache_clear`
  in tests propagates everywhere.
- Tests can swap the client out by patching
  :func:`app.redis_client.get_redis_client` – no need to mock
  ``redis.asyncio.Redis`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Module-level cache. ``None`` means "not built yet"; the first
# call to :func:`get_redis_client` populates it.
_client: Redis | None = None


def _build_client() -> Redis:
    """Construct a new :class:`redis.asyncio.Redis` from the settings.

    Kept as a private helper so tests can monkeypatch the *factory*
    (``Redis.from_url``) without touching the cached singleton.
    Imports are local so this module is importable in environments
    where the optional ``redis`` dependency is not installed.
    """
    from redis.asyncio import Redis

    settings = get_settings()
    # ``decode_responses=False`` so binary payloads (Blobs, JSON
    # strings) round-trip unchanged; callers that want strings
    # can decode explicitly. ``socket_connect_timeout`` is kept
    # low so a misconfigured Redis URL fails the request fast
    # instead of stalling on a TCP handshake.
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def get_redis_client() -> Redis:
    """Return the process-wide :class:`redis.asyncio.Redis` instance.

    The client is built lazily on the first call so importing
    :mod:`app.redis_client` (and the application that depends on
    it) does not require a live Redis. The same instance is
    returned on every subsequent call so the underlying
    connection pool is shared.

    Tests that need a clean slate call :func:`reset_redis_client`
    after monkeypatching the factory.
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def reset_redis_client() -> None:
    """Drop the cached client.

    Intended for tests: after monkeypatching
    :func:`_build_client` (or the underlying ``Redis.from_url``)
    a test must reset the cache so the next :func:`get_redis_client`
    call rebuilds the client with the patched factory.
    """
    global _client
    _client = None
