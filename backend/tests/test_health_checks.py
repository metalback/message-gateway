"""Unit tests for the dependency probes in :mod:`app.health`.

The probes never raise; any exception from the underlying driver
is captured into a :class:`HealthStatus` with ``ok=False``. These
tests pin that contract and exercise both the success and failure
paths of each probe without touching a real Postgres or Redis
instance.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app import health
from app.config import Settings

# ---------------------------------------------------------------------------
# HealthStatus value object
# ---------------------------------------------------------------------------


def test_health_status_to_dict_includes_none_detail() -> None:
    """Successful probes serialise with an explicit ``detail: null``
    so the JSON response shape is consistent across probes (clients
    can rely on the key being present)."""
    status = health.HealthStatus(name="database", ok=True)
    assert status.to_dict() == {"name": "database", "ok": True, "detail": None}


def test_health_status_to_dict_includes_failure_detail() -> None:
    status = health.HealthStatus(name="redis", ok=False, detail="timeout")
    assert status.to_dict() == {
        "name": "redis",
        "ok": False,
        "detail": "timeout",
    }


def test_health_status_is_immutable() -> None:
    """``HealthStatus`` is a frozen dataclass so a probe result can
    be passed around (e.g. into a logger) without risk of mutation."""
    status = health.HealthStatus(name="x", ok=True)
    with pytest.raises((AttributeError, Exception)):
        status.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# overall_ok
# ---------------------------------------------------------------------------


def test_overall_ok_true_for_empty_list() -> None:
    """An empty check list is vacuously ready. The aggregation logic
    must not special-case the empty list, but the result should
    still be ``True`` so a future probe that fails to register does
    not silently flip the service to "degraded"."""
    assert health.overall_ok([]) is True


def test_overall_ok_true_when_all_pass() -> None:
    checks = [
        health.HealthStatus(name="a", ok=True),
        health.HealthStatus(name="b", ok=True),
    ]
    assert health.overall_ok(checks) is True


def test_overall_ok_false_when_any_fails() -> None:
    checks = [
        health.HealthStatus(name="a", ok=True),
        health.HealthStatus(name="b", ok=False, detail="nope"),
    ]
    assert health.overall_ok(checks) is False


# ---------------------------------------------------------------------------
# check_database
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Async context-manager double for ``AsyncConnection``."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    async def execute(self, _statement: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return None

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeEngine:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.connect_calls = 0

    def connect(self) -> _FakeConnection:
        self.connect_calls += 1
        return _FakeConnection(self._exc)


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    """Patch :func:`app.db.get_engine` to return a controllable
    double. Tests can inspect ``fake.connect_calls`` and inject
    exceptions via ``fake = _FakeEngine(exc=...)``."""
    engine = _FakeEngine()
    monkeypatch.setattr("app.db.get_engine", lambda: engine)
    return engine


async def test_check_database_returns_ok_on_success(fake_engine: _FakeEngine) -> None:
    status = await health.check_database(Settings())
    assert status.name == "database"
    assert status.ok is True
    assert status.detail is None
    assert fake_engine.connect_calls == 1


async def test_check_database_returns_failure_on_sqlalchemy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``SQLAlchemyError`` (the public base for every asyncpg /
    driver error) must be captured into ``ok=False`` with a
    non-empty ``detail`` string."""
    engine = _FakeEngine(exc=OperationalError("select 1", {}, Exception("could not connect")))
    monkeypatch.setattr("app.db.get_engine", lambda: engine)

    status = await health.check_database(Settings())
    assert status.ok is False
    assert status.detail is not None
    assert status.detail != ""


async def test_check_database_returns_failure_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected exceptions (e.g. an ``ImportError`` from a broken
    driver) must also be captured, never raised."""
    engine = _FakeEngine(exc=RuntimeError("boom"))
    monkeypatch.setattr("app.db.get_engine", lambda: engine)

    status = await health.check_database(Settings())
    assert status.ok is False
    assert status.detail == "boom"


# ---------------------------------------------------------------------------
# check_redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(
        self,
        *,
        ping_value: Any = True,
        ping_exc: Exception | None = None,
    ) -> None:
        self._ping_value = ping_value
        self._ping_exc = ping_exc
        self.aclose_calls = 0
        self.ping_calls = 0

    async def ping(self) -> Any:
        self.ping_calls += 1
        if self._ping_exc is not None:
            raise self._ping_exc
        return self._ping_value

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Patch :func:`app.redis_client.get_redis_client` to return a
    controllable double.

    The production code path is ``health.check_redis`` → ``get_redis_client``
    → cached :class:`redis.asyncio.Redis` instance. Patching the
    accessor (rather than ``redis.asyncio.Redis.from_url``) keeps
    the test in lock-step with the actual call site, so a future
    refactor that drops the cache cannot silently invalidate the
    test.
    """
    client = _FakeRedis()
    monkeypatch.setattr("app.redis_client.get_redis_client", lambda: client)
    return client


async def test_check_redis_returns_ok_on_pong(fake_redis: _FakeRedis) -> None:
    status = await health.check_redis(Settings())
    assert status.name == "redis"
    assert status.ok is True
    assert status.detail is None
    assert fake_redis.ping_calls == 1
    # The probe must NOT close the shared client: the connection
    # pool is reused across the app, and closing it from the
    # health check would race with concurrent requests.
    assert fake_redis.aclose_calls == 0


async def test_check_redis_returns_failure_on_redis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRedis(ping_exc=RedisConnectionError("redis is down"))
    monkeypatch.setattr("app.redis_client.get_redis_client", lambda: client)

    status = await health.check_redis(Settings())
    assert status.ok is False
    assert status.detail is not None
    assert "redis is down" in status.detail


async def test_check_redis_returns_failure_on_falsy_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PING that returns a falsy value is treated as a failure –
    the Redis protocol is supposed to reply with ``True``."""
    client = _FakeRedis(ping_value=False)
    monkeypatch.setattr("app.redis_client.get_redis_client", lambda: client)

    status = await health.check_redis(Settings())
    assert status.ok is False
    assert status.detail is not None


async def test_check_redis_catches_unexpected_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected non-Redis exceptions (e.g. ``OSError``) must also
    be captured so the probe never lets an exception escape."""
    client = _FakeRedis(ping_exc=OSError("network is unreachable"))
    monkeypatch.setattr("app.redis_client.get_redis_client", lambda: client)

    status = await health.check_redis(Settings())
    assert status.ok is False
    assert status.detail is not None
    assert "network is unreachable" in status.detail


# ---------------------------------------------------------------------------
# run_readiness_checks
# ---------------------------------------------------------------------------


async def test_run_readiness_checks_runs_every_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregator must call both probes in declaration order
    so the response payload is deterministic across runs."""
    calls: list[str] = []

    async def _fake_db(_settings: Any) -> health.HealthStatus:
        calls.append("database")
        return health.HealthStatus(name="database", ok=True)

    async def _fake_redis(_settings: Any) -> health.HealthStatus:
        calls.append("redis")
        return health.HealthStatus(name="redis", ok=True)

    monkeypatch.setattr(health, "check_database", _fake_db)
    monkeypatch.setattr(health, "check_redis", _fake_redis)

    result = await health.run_readiness_checks(Settings())
    assert calls == ["database", "redis"]
    assert [c.name for c in result] == ["database", "redis"]


async def test_run_readiness_checks_continues_when_a_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing probe must not abort the aggregator: the load
    balancer needs to see the full picture, not just the first
    broken dependency."""

    async def _failing_db(_settings: Any) -> health.HealthStatus:
        return health.HealthStatus(name="database", ok=False, detail="x")

    async def _ok_redis(_settings: Any) -> health.HealthStatus:
        return health.HealthStatus(name="redis", ok=True)

    monkeypatch.setattr(health, "check_database", _failing_db)
    monkeypatch.setattr(health, "check_redis", _ok_redis)

    result = await health.run_readiness_checks(Settings())
    assert len(result) == 2
    assert result[0].ok is False
    assert result[1].ok is True
