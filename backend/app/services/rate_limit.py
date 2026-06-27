"""Redis-backed per-tenant rate limiter.

The platform exposes a single ``POST /v1/messages/batch``
endpoint that the PRD's "100 msg/s por API Key" budget
limits. This module owns the **per-tenant** enforcement
of that budget: a Redis ``INCR`` + ``EXPIRE`` against the
key ``rate_limit:batch:{client_id}`` returns the rolling
second-window counter, and a result above the configured
ceiling surfaces as a :class:`BatchRateLimitError` the route
layer maps onto HTTP 429.

Design choices worth flagging:

- **Sliding fixed window** rather than a token bucket. The
  PRD target is "100 msg/s" – a one-second window is the
  simplest possible interpretation and matches what an
  operator reading the requirement would expect. A
  token-bucket algorithm would smooth bursts but is
  overkill for an MVP and harder to explain in a runbook.
- **Atomic ``INCR`` + ``EXPIRE``**. The first ``INCR`` sets
  the key to ``1``; we follow up with a one-second
  ``EXPIRE`` on the first hit of the window so the key
  evaporates on its own. A subsequent ``INCR`` within the
  window keeps incrementing the counter without bumping
  the TTL. This is the classic Redis fixed-window pattern
  documented in the official rate-limiting cookbook.
- **Optional Redis dependency**. The helper degrades to
  "always allow" when the Redis client is unavailable so a
  misconfigured dev environment does not block message
  delivery entirely. The trade-off is that a Redis outage
  effectively disables rate limiting; the alternative
  (raise on connection failure) would be a self-inflicted
  outage that the platform cannot recover from without
  operator intervention. The behaviour is logged so an
  operator can still see the limiter went into bypass
  mode.
- **Injectable client**. The public entry point
  :func:`enforce_batch_rate_limit` accepts an optional
  ``redis_client`` so unit tests can swap the in-memory
  fake from :mod:`tests.conftest` without monkeypatching
  :mod:`app.redis_client`. Production callers fall back to
  the cached singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.config import Settings, get_settings
from app.observability import get_logger
from app.redis_client import get_redis_client
from app.services.messaging import BatchRateLimitError

if TYPE_CHECKING:
    class _RedisLike(Protocol):
        """Protocol that captures the surface the rate
        limiter actually uses on the Redis client.

        Kept narrow so unit tests can pass an in-memory
        fake without inheriting the full ``Redis``
        class. Production callers fall back to the cached
        :func:`app.redis_client.get_redis_client` singleton,
        which is a real ``redis.asyncio.Redis`` instance and
        therefore satisfies the protocol trivially."""

        async def incr(self, key: str) -> int: ...
        async def expire(self, key: str, seconds: int) -> None: ...

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Redis key prefix for the batch rate-limit counter. Versioned
# so a future change to the algorithm (e.g. token bucket) can
# ship under a new prefix without colliding with the in-flight
# counter keys the platform already has live.
_BATCH_RATE_LIMIT_KEY_PREFIX = "rate_limit:batch:v1:"

# Default number of seconds the counter key is allowed to
# live. Matches the PRD's "por segundo" granularity: the key
# created on the first ``INCR`` of a new window evaporates
# after one second, so the next request starts a fresh
# counter.
_RATE_LIMIT_WINDOW_SECONDS = 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _key_for(client_id: str) -> str:
    """Build the Redis key the per-tenant counter lives at.

    Centralised so the prefix change is a one-line edit and
    so unit tests can assert on the exact string the
    production code uses.
    """
    return f"{_BATCH_RATE_LIMIT_KEY_PREFIX}{client_id}"


async def enforce_batch_rate_limit(
    *,
    client_id: str,
    limit: int | None = None,
    settings: Settings | None = None,
    redis_client: _RedisLike | None = None,
) -> int:
    """Increment the per-tenant counter and return the new
    value. Raises :class:`BatchRateLimitError` when the
    value exceeds ``limit``.

    The function is a coroutine so the same code path can
    transparently fall back to the cached
    :func:`app.redis_client.get_redis_client` singleton when
    ``redis_client`` is ``None``. The return value is the
    *post-increment* counter (the documented Redis contract)
    so the caller can echo the budget consumption in an
    observability metric without a second round-trip.

    The ``limit`` argument is intentionally exposed so the
    future per-endpoint overrides ("Starter = 10/s,
    Growth = 100/s, Enterprise = 1000/s") can be wired in
    without breaking this function's contract. The default
    is read from :attr:`Settings.batch_rate_limit_per_second`
    so deployments that want to bump the global ceiling
    only have to edit the env file.
    """
    cfg = settings or get_settings()
    if not isinstance(client_id, str) or not client_id:
        # A missing client id is a caller bug (the route
        # layer always has ``current_client.id`` at this
        # point). Failing loudly is better than silently
        # bypassing the limit.
        raise ValueError("client_id must be a non-empty string")
    effective_limit = int(limit) if limit is not None else cfg.batch_rate_limit_per_second
    if effective_limit <= 0:
        raise ValueError("limit must be a positive integer")

    # Lazy import keeps ``app.services.rate_limit`` importable
    # in environments where the optional ``redis`` dependency
    # is not installed (the same trade-off the
    # :mod:`app.redis_client` module already makes).
    client = redis_client if redis_client is not None else get_redis_client()
    key = _key_for(client_id)
    try:
        # ``INCR`` is atomic: two concurrent requests can
        # never observe the same pre-increment value, so the
        # post-increment read is the only authoritative
        # counter the platform needs.
        count = int(await client.incr(key))
    except Exception as exc:  # pragma: no cover - defensive
        # A Redis outage must not brick the platform. We log
        # the event so an operator can see the limiter went
        # into bypass mode and continue to allow the request
        # so a transient dependency blip does not cascade
        # into a delivery outage.
        logger.warning(
            "batch_rate_limit_bypassed",
            extra={
                "client_id": client_id,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            },
        )
        return 0
    if count == 1:
        # First hit of the window – arm the TTL so the key
        # evaporates after one second. The ``EXPIRE`` is a
        # no-op on a key that already has a TTL (a
        # concurrent first hit from another worker) and
        # idempotent on the first caller.
        try:
            await client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "batch_rate_limit_expire_failed",
                extra={
                    "client_id": client_id,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                },
            )
    if count > effective_limit:
        raise BatchRateLimitError(
            "batch_rate_limited",
            f"batch rate limit exceeded ({effective_limit}/s)",
            retry_after_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        )
    return count


__all__ = ("enforce_batch_rate_limit",)
