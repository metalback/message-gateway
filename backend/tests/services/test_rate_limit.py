"""Service-level tests for the Redis-backed rate limiter (issue #9).

The platform exposes a single ``POST /v1/messages/batch``
endpoint that the PRD's "100 msg/s por API Key" budget
limits. The tests below cover the
:func:`app.services.rate_limit.enforce_batch_rate_limit`
helper that owns the per-tenant enforcement.

The Redis client is swapped for an in-memory fake so the
suite never opens a real TCP connection. The fake
implements the two Redis primitives the helper uses
(``INCR`` + ``EXPIRE``) plus a ``time``-shaped helper
that lets a test fast-forward the second-window TTL.
"""

from __future__ import annotations

import pytest

from app.services.messaging import BatchRateLimitError
from app.services.rate_limit import enforce_batch_rate_limit

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _InMemoryRedis:
    """A minimal in-memory Redis substitute.

    Implements only the two methods the rate limiter uses
    (``incr`` + ``expire``) and a tiny ``time`` helper so a
    test can fast-forward the window. A ``Counter`` keeps
    track of the calls so the tests can assert the helper
    issued exactly one ``INCR`` and one ``EXPIRE`` per
    fresh window.
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}
        self._expiry: dict[str, float] = {}
        self._now: float = 0.0
        self.incr_calls: list[str] = []
        self.expire_calls: list[tuple[str, int]] = []

    def advance(self, seconds: float) -> None:
        """Move the in-memory clock forward by ``seconds``."""
        self._now += seconds
        for key in list(self._expiry):
            if self._expiry[key] <= self._now:
                self._values.pop(key, None)
                self._expiry.pop(key, None)

    async def incr(self, key: str) -> int:
        self.incr_calls.append(key)
        self._values[key] = self._values.get(key, 0) + 1
        return self._values[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expire_calls.append((key, seconds))
        if key in self._values:
            self._expiry[key] = self._now + seconds


# ---------------------------------------------------------------------------
# enforcement
# ---------------------------------------------------------------------------


async def test_enforce_batch_rate_limit_allows_under_limit() -> None:
    """The first ``limit`` calls within a single second
    succeed silently and return the running counter so
    the caller can echo the budget consumption in an
    observability metric without a second round-trip.
    """
    redis = _InMemoryRedis()
    for count in range(1, 4):
        result = await enforce_batch_rate_limit(
            client_id="client-1",
            limit=10,
            redis_client=redis,
        )
        assert result == count


async def test_enforce_batch_rate_limit_blocks_over_limit() -> None:
    """The ``limit + 1``-th call inside a single window
    raises :class:`BatchRateLimitError` with a
    ``retry_after_seconds`` of ``1`` so the route layer
    can surface the value as a ``Retry-After`` header.
    """
    redis = _InMemoryRedis()
    for _ in range(3):
        await enforce_batch_rate_limit(
            client_id="client-1",
            limit=3,
            redis_client=redis,
        )
    with pytest.raises(BatchRateLimitError) as excinfo:
        await enforce_batch_rate_limit(
            client_id="client-1",
            limit=3,
            redis_client=redis,
        )
    assert excinfo.value.http_status == 429
    assert excinfo.value.code == "batch_rate_limited"
    assert excinfo.value.retry_after_seconds == 1


async def test_enforce_batch_rate_limit_resets_after_window() -> None:
    """A new window (simulated by advancing the in-memory
    clock past the TTL) lets the customer resume. The
    counter starts fresh and ``EXPIRE`` is re-armed on
    the first hit of the new window.
    """
    redis = _InMemoryRedis()
    for _ in range(3):
        await enforce_batch_rate_limit(
            client_id="client-1",
            limit=3,
            redis_client=redis,
        )
    # Exhaust the budget.
    with pytest.raises(BatchRateLimitError):
        await enforce_batch_rate_limit(
            client_id="client-1",
            limit=3,
            redis_client=redis,
        )
    # Advance the clock past the 1-second window so the
    # counter evaporates.
    redis.advance(2.0)
    # The next call succeeds and re-arms the TTL.
    result = await enforce_batch_rate_limit(
        client_id="client-1",
        limit=3,
        redis_client=redis,
    )
    assert result == 1
    # ``EXPIRE`` was issued twice (once on the original
    # first hit, once on the new first hit). The
    # assertions are on the *count*, not on the order, so
    # a future refactor that arms the TTL on a different
    # path keeps the suite green.
    assert len(redis.expire_calls) == 2


async def test_enforce_batch_rate_limit_isolates_clients() -> None:
    """Two customers share the same Redis but have
    independent counters – an over-the-limit call from
    ``client-a`` does not consume ``client-b``'s
    budget."""
    redis = _InMemoryRedis()
    # Exhaust ``client-a``'s budget.
    for _ in range(2):
        await enforce_batch_rate_limit(
            client_id="client-a",
            limit=2,
            redis_client=redis,
        )
    with pytest.raises(BatchRateLimitError):
        await enforce_batch_rate_limit(
            client_id="client-a",
            limit=2,
            redis_client=redis,
        )
    # ``client-b`` is unaffected.
    result = await enforce_batch_rate_limit(
        client_id="client-b",
        limit=2,
        redis_client=redis,
    )
    assert result == 1


async def test_enforce_batch_rate_limit_uses_settings_default() -> None:
    """When ``limit`` is ``None``, the helper reads the
    ceiling from
    :attr:`Settings.batch_rate_limit_per_second` so a
    deployment that wants to bump the global ceiling
    only has to edit the env file.
    """
    from app.config import Settings

    redis = _InMemoryRedis()
    settings = Settings(batch_rate_limit_per_second=2)
    for _ in range(2):
        await enforce_batch_rate_limit(
            client_id="client-x",
            settings=settings,
            redis_client=redis,
        )
    with pytest.raises(BatchRateLimitError):
        await enforce_batch_rate_limit(
            client_id="client-x",
            settings=settings,
            redis_client=redis,
        )


async def test_enforce_batch_rate_limit_rejects_empty_client_id() -> None:
    """A missing ``client_id`` is a caller bug (the
    route layer always has ``current_client.id`` at
    this point) and surfaces as ``ValueError`` so the
    silent "always allow" fallback does not mask a
    wiring mistake.
    """
    redis = _InMemoryRedis()
    with pytest.raises(ValueError):
        await enforce_batch_rate_limit(
            client_id="",
            limit=10,
            redis_client=redis,
        )


async def test_enforce_batch_rate_limit_bypasses_on_redis_failure() -> None:
    """A Redis outage must not brick the platform: the
    helper logs the event and returns ``0`` so the
    caller proceeds with the request. The trade-off is
    that a Redis outage effectively disables rate
    limiting; the alternative (raise on connection
    failure) would be a self-inflicted outage the
    platform cannot recover from without operator
    intervention.
    """
    import pytest as _pytest

    class _BrokenRedis:
        async def incr(self, key: str) -> int:  # pragma: no cover - def. path
            raise RuntimeError("redis down")

        async def expire(self, key: str, seconds: int) -> None:  # pragma: no cover
            raise RuntimeError("redis down")

    # Use ``caplog`` so the bypass log line is not
    # silently dropped.
    _pytest.MonkeyPatch().setattr(
        "app.services.rate_limit.logger",
        _pytest.MonkeyPatch(),  # placeholder so the import below works
    )
    from app.services import rate_limit as rl_module

    class _CaptureLog:
        def __init__(self) -> None:
            self.warnings: list[tuple[str, dict[str, object]]] = []

        def warning(self, event: str, extra: dict[str, object] | None = None) -> None:
            self.warnings.append((event, extra or {}))

    capture = _CaptureLog()
    rl_module.logger = capture  # type: ignore[assignment]
    result = await enforce_batch_rate_limit(
        client_id="client-1",
        limit=10,
        redis_client=_BrokenRedis(),
    )
    assert result == 0
    assert any(event == "batch_rate_limit_bypassed" for event, _ in capture.warnings)
