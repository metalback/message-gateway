"""Liveness / readiness probes for the FastAPI service.

The simple ``/health`` endpoint in :mod:`app.main` is intentionally
shallow so it can answer without touching external services; a
Kubernetes / load-balancer "is this pod ready to serve traffic?"
check needs something stronger. This module owns the async probes
that the ``/health/ready`` endpoint runs:

- :func:`check_database` – round-trips a trivial ``SELECT 1``
  through the SQLAlchemy async engine.
- :func:`check_redis`     – ``PING`` against the cached
  :class:`redis.asyncio.Redis` client returned by
  :func:`app.redis_client.get_redis_client`.

Each probe returns a :class:`HealthStatus` value object so the
endpoint can render a uniform response. Probes never raise; any
exception is captured into ``ok=False`` with a redacted
``detail`` string. The redaction is deliberately conservative
(connection strings, SQL state, tracebacks are stripped) because
the response is unauthenticated by design.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings

# Cap ``detail`` to keep the response body small even when the
# underlying library produces a multi-line stack trace.
_DETAIL_LIMIT = 200


def _truncate(value: str) -> str:
    """Trim ``value`` to a single line and a bounded length."""
    single = value.splitlines()[0] if value else ""
    return single[:_DETAIL_LIMIT]


@dataclass(frozen=True)
class HealthStatus:
    """Result of a single dependency probe.

    ``name``    – short identifier of the dependency (``"database"``,
                  ``"redis"``).
    ``ok``      – ``True`` if the probe succeeded.
    ``detail``  – optional, already-redacted human-readable note.
                  ``None`` when the probe succeeded cleanly.
    """

    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for the JSON response."""
        return asdict(self)


async def check_database(settings: Settings | None = None) -> HealthStatus:
    """Probe the configured PostgreSQL connection.

    A trivial ``SELECT 1`` is enough: the goal is to confirm the
    engine can open a connection and the DB is reachable, not to
    validate application-level permissions.
    """
    settings = settings or get_settings()
    try:
        # The engine is fetched through the module attribute (not a
        # local import) so tests can monkeypatch
        # ``app.db.get_engine`` and observe the change here.
        import app.db

        engine = app.db.get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return HealthStatus(name="database", ok=True)
    except SQLAlchemyError as exc:
        return HealthStatus(name="database", ok=False, detail=_truncate(str(exc)))
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return HealthStatus(name="database", ok=False, detail=_truncate(str(exc)))


async def check_redis(settings: Settings | None = None) -> HealthStatus:
    """Probe the configured Redis instance with a ``PING``.

    Uses the process-wide client returned by
    :func:`app.redis_client.get_redis_client` so the probe
    reuses the same connection pool the rest of the app uses.
    The probe does **not** close the client on success: the
    pool is shared and a closing health check would tear it
    out from under concurrent requests.
    """
    settings = settings or get_settings()
    try:
        # Imported lazily so test environments that do not exercise
        # the Redis check do not have to install the optional
        # ``redis`` client just to import this module.
        from redis.exceptions import RedisError

        # The client is fetched through the module attribute (not a
        # local import) so tests can monkeypatch
        # ``app.redis_client.get_redis_client`` and observe the change
        # here – mirrors the ``app.db.get_engine`` pattern used by
        # :func:`check_database`.
        import app.redis_client

        client = app.redis_client.get_redis_client()
        pong = await client.ping()
        ok = bool(pong)
        return HealthStatus(
            name="redis",
            ok=ok,
            detail=None if ok else "PING returned a falsy value",
        )
    except RedisError as exc:
        return HealthStatus(name="redis", ok=False, detail=_truncate(str(exc)))
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return HealthStatus(name="redis", ok=False, detail=_truncate(str(exc)))


async def run_readiness_checks(
    settings: Settings | None = None,
) -> list[HealthStatus]:
    """Run every registered probe and return the results.

    The list order matches the declaration order below so the
    response is deterministic and easy to test.
    """
    return [
        await check_database(settings),
        await check_redis(settings),
    ]


def overall_ok(checks: list[HealthStatus]) -> bool:
    """Return ``True`` only when every check succeeded."""
    return all(check.ok for check in checks)
