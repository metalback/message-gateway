"""Provider health + routing log service (issue #11).

This module owns the *runtime* health of the provider fleet –
the periodic probes the issue calls for ("Worker that every
60s makes a ping to each provider"), the
:class:`~app.models.routing_log.RoutingLog` rows the
messaging service emits, and the read API the admin
dashboard consumes.

Public functions:

- :func:`build_attempt_recorder` – construct a recorder
  callback for the
  :class:`~app.adapters.failover.FailoverProvider` so every
  per-attempt event the router emits lands in
  ``routing_log``.
- :func:`record_routing_attempt` – single-row insert helper
  used both by the recorder and by the periodic health
  worker (which has no associated message).
- :func:`probe_provider`         – run a single health check
  against a provider, persist the outcome, return the
  :class:`ProviderConfig` row that was updated.
- :func:`run_health_checks`      – fan the probe out across
  every provider the registry knows about.
- :func:`list_provider_health`   – read API for the admin
  endpoint (the green / yellow / red card).
- :func:`list_recent_routing_attempts` – read API for the
  "logs de routing" view the operator uses to trace a
  message through the chain.

Health-state transitions
------------------------

The probe counts successes / failures in
``ProviderConfig.consecutive_failures`` /
``consecutive_successes`` and applies these rules on every
run:

- ``failure_threshold`` consecutive failures
  (``HEALTH_FAILURE_THRESHOLD``, default ``3``) →
  ``ProviderHealth.DEGRADED`` and the row is recorded.
- ``recovery_threshold`` consecutive successes
  (``HEALTH_RECOVERY_THRESHOLD``, default ``2``) →
  ``ProviderHealth.HEALTHY`` and the counters are reset.

The counters are reset on every status transition so a
provider that bounced between healthy and degraded does
not accumulate stale failures. The current "healthy"
status is also implicitly returned by the probe even when
the threshold has not been crossed: a single successful
probe leaves ``consecutive_failures`` at zero and the
status at ``HEALTHY`` (the conservative default that keeps
the dashboard from flapping on a single blip).

The probe is intentionally cheap: a
``BaseProvider.get_status`` call against a synthetic
``"__healthcheck__"`` id. Concrete adapters already
handle that id gracefully (returning the upstream's
status endpoint), so a single call exercises the
provider's HTTP path without actually sending a
message.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.adapters.registry as registry
from app.adapters.base import BaseProvider
from app.adapters.errors import (
    ProviderError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.failover import (
    RETRYABLE_ERRORS,
    AttemptCallback,
    FailoverProvider,
)
from app.adapters.registry import get_provider, supported_channels
from app.config import Settings, get_settings
from app.models.message import Channel
from app.models.provider_config import ProviderConfig, ProviderHealth
from app.models.routing_log import RoutingLog, RoutingLogOutcome
from app.observability import get_logger

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Synthetic ``provider_msg_id`` the health probe uses. The
#: concrete adapters treat it as a no-op status query, so a
#: single call exercises the upstream's HTTP path without
#: sending a real message.
HEALTHCHECK_PROVIDER_MSG_ID = "__healthcheck__"

#: Number of consecutive failures that flip a row to
#: :attr:`ProviderHealth.DEGRADED`. Tuned for the 60 s probe
#: cadence (issue #11): 3 failures = ~3 minutes of trouble
#: before the dashboard starts showing the yellow indicator.
HEALTH_FAILURE_THRESHOLD = 3

#: Number of consecutive successes required to flip a row
#: back to :attr:`ProviderHealth.HEALTHY`. Lower than the
#: failure threshold so a recovering provider is back to
#: green quickly, but high enough to avoid a flap on a
#: single lucky probe.
HEALTH_RECOVERY_THRESHOLD = 2

#: Hard cap on the "logs de routing" admin view. 200 rows
#: covers a "this hour" trace and prevents a runaway
#: dashboard from asking for an unbounded slice of the
#: audit history.
DEFAULT_ROUTING_LOG_LIMIT = 100
_ROUTING_LOG_HARD_LIMIT = 500

#: Hard cap on the per-channel health probe concurrency. A
#: platform that ships 6+ providers should not fan out 6
#: HTTP probes at once and saturate the worker pod; 4
#: keeps the probe cost bounded while still completing
#: the cycle in seconds.
HEALTHCHECK_CONCURRENCY = 4


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderHealthRow:
    """Read API projection of a :class:`ProviderConfig` row.

    The shape is intentionally narrow – only the columns
    the admin dashboard renders – so a future iteration
    that adds bookkeeping columns to the model does not
    silently expose them on the public surface.
    """

    name: str
    channel: str
    health_status: str
    last_health_check: datetime | None
    last_latency_ms: int | None
    consecutive_failures: int
    consecutive_successes: int
    active: bool
    priority: int


@dataclass(frozen=True)
class RoutingAttemptRow:
    """Read API projection of a :class:`RoutingLog` row."""

    id: str
    message_id: str | None
    provider: str
    channel: str
    outcome: str
    latency_ms: int
    error_code: str | None
    error_message: str | None
    attempted_at: datetime


# ---------------------------------------------------------------------------
# Helpers – error → outcome mapping
# ---------------------------------------------------------------------------


def _outcome_from_exception(exc: ProviderError | None) -> RoutingLogOutcome:
    """Map a :class:`ProviderError` to the matching :class:`RoutingLogOutcome`.

    Kept as a module-level helper (rather than inlined in
    :func:`record_routing_attempt`) so the :class:`FailoverProvider`
    callback and the health-check worker both reach the same
    conclusion. ``None`` (no error) maps to ``SUCCESS``;
    :class:`ProviderValidationError` is treated as a permanent
    failure (``VALIDATION_ERROR``) so the dashboard can chart
    bad-input errors separately from upstreams that are merely
    unavailable.
    """
    if exc is None:
        return RoutingLogOutcome.SUCCESS
    if isinstance(exc, ProviderValidationError):
        return RoutingLogOutcome.VALIDATION_ERROR
    return RoutingLogOutcome.FAILURE


# ---------------------------------------------------------------------------
# Routing log
# ---------------------------------------------------------------------------


async def record_routing_attempt(
    session: AsyncSession,
    *,
    provider_name: str,
    channel: Channel,
    outcome: RoutingLogOutcome,
    latency_ms: int,
    error_code: str | None = None,
    error_message: str | None = None,
    message_id: str | None = None,
) -> RoutingLog:
    """Insert a single :class:`RoutingLog` row and return it.

    The function is intentionally tiny: the messaging
    service builds the recorder once per request and
    calls it from inside the
    :class:`~app.adapters.failover.FailoverProvider`
    callback, so a single ``INSERT`` is all the API
    needs.

    ``latency_ms`` is clamped at ``0`` (a sub-millisecond
    call) so the column contract is honoured even when a
    mock adapter returns synchronously. ``error_message``
    is truncated to 500 chars (the column ceiling) so a
    verbose upstream response cannot blow up the row.
    """
    safe_latency = max(int(latency_ms or 0), 0)
    safe_error_message = (
        error_message[:500] if error_message is not None else None
    )
    row = RoutingLog(
        message_id=message_id,
        provider_attempted=provider_name,
        channel=channel,
        outcome=outcome,
        latency_ms=safe_latency,
        error_code=error_code,
        error_message=safe_error_message,
    )
    session.add(row)
    await session.flush()
    return row


def build_attempt_recorder(
    session: AsyncSession,
    *,
    channel: Channel,
    message_id: str,
) -> AttemptCallback:
    """Build a recorder callback for the failover router.

    The returned callable matches the
    :data:`~app.adapters.failover.AttemptCallback` signature
    and persists one
    :class:`~app.models.routing_log.RoutingLog` row per
    provider attempt the router makes. The
    :class:`~app.adapters.failover.FailoverProvider` invokes
    it after every attempt (success or failure) so the
    audit log captures the *whole chain* the messaging
    service walked, not just the final outcome.

    The recorder is bound to a single message_id and
    channel so the per-row ``INSERT`` does not have to
    re-pass them. The session is the *request* session
    (the same one the messaging service uses) so the
    routing_log rows commit atomically with the
    :class:`~app.models.message.Message` row – a
    crash between the dispatch and the commit does not
    leave the message "sent" without an audit trail.

    Implementation note: the callback runs from inside
    the synchronous ``send`` coroutine of the failover
    router. Calling ``asyncio.create_task`` from a
    coroutine is fine but the task would not be awaited
    before the messaging service commits the parent
    transaction, racing the INSERT. Instead we
    stage the row with :meth:`AsyncSession.add` (which
    is synchronous) and let the next ``flush`` /
    ``commit`` write it. SQLAlchemy keeps the staged
    row in the unit of work until the next ``await
    session.flush()`` boundary, so the row lands in
    the same transaction as the parent ``Message`` row
    – the audit log and the message either both commit
    or both roll back.
    """

    def _record(
        provider_name: str,
        outcome: RoutingLogOutcome | str,
        latency_ms: int,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        outcome_value = (
            outcome
            if isinstance(outcome, RoutingLogOutcome)
            else RoutingLogOutcome(str(outcome))
        )
        safe_latency = max(int(latency_ms or 0), 0)
        safe_error_message = (
            error_message[:500] if error_message is not None else None
        )
        row = RoutingLog(
            message_id=message_id,
            provider_attempted=provider_name,
            channel=channel,
            outcome=outcome_value,
            latency_ms=safe_latency,
            error_code=error_code,
            error_message=safe_error_message,
        )
        # ``add`` is synchronous: the row is staged in
        # the session's unit of work and will be written
        # by the next ``flush`` boundary (the messaging
        # service commits before returning, which flushes
        # the staged rows).
        session.add(row)

    return _record


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def _resolve_by_name(provider_name: str, *, settings: Settings) -> BaseProvider | None:
    """Look up a provider by ``BaseProvider.name`` through the registry.

    The normal :func:`app.adapters.registry.get_provider`
    call returns the *primary* of a channel. The health
    worker needs to probe a specific upstream by name –
    including fallbacks – so this helper consults the
    failover factory map directly. ``None`` is returned
    when the name is not registered (configuration
    drift); the caller treats that as a probe failure
    so the dashboard surfaces the misconfiguration.
    """
    factory = registry._FAILOVER_BUILDERS.get(provider_name)
    if factory is None:
        return None
    return factory(settings)


# ---------------------------------------------------------------------------
# Health probe (continued)
# ---------------------------------------------------------------------------


async def _probe_one(
    session: AsyncSession,
    *,
    provider_name: str,
    channel: Channel,
    settings: Settings,
) -> tuple[ProviderConfig, ProviderHealth, int, ProviderError | None]:
    """Run a single health probe and update the row.

    Returns a ``(row, status, latency_ms, error)`` tuple
    so the caller can format the response without
    re-querying the database. The ``row`` is the
    post-update ORM object (the same instance the
    caller passed in – flushed in place).
    """
    started = time.monotonic()
    try:
        # Prefer the channel's primary (the registry
        # shortcut). Fall back to the name-keyed factory
        # the failover builder map exposes so a probe
        # of a *fallback* provider name does not get
        # silently redirected to the primary.
        try:
            provider = get_provider(channel, settings=settings)
        except Exception:
            provider = None
        target: BaseProvider | None = None
        if isinstance(provider, FailoverProvider):
            target = (
                provider.primary
                if provider.primary.name == provider_name
                else None
            )
        elif provider is not None and provider.name == provider_name:
            target = provider
        if target is None:
            target = _resolve_by_name(provider_name, settings=settings)
        if target is None:
            raise RuntimeError(
                f"provider {provider_name!r} is not registered for channel {channel.value}"
            )
    except Exception as exc:  # noqa: BLE001 - registry must not crash probe
        # The registry can raise ``UnsupportedChannelError``
        # (configuration drift) or a downstream import
        # error. Treat as a failure so the dashboard
        # surfaces the misconfiguration.
        _logger.warning(
            "provider registry failed to resolve %s/%s: %s",
            provider_name,
            channel,
            exc,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        row = await _upsert_probe_result(
            session,
            provider_name=provider_name,
            channel=channel,
            success=False,
            latency_ms=latency_ms,
            error_code="registry_error",
            error_message=str(exc)[:500],
        )
        return row, row.health_status, latency_ms, None

    error: ProviderError | None = None
    try:
        await target.get_status(HEALTHCHECK_PROVIDER_MSG_ID)
    except RETRYABLE_ERRORS as exc:
        error = exc
    except ProviderValidationError as exc:
        # A validation error on a status query is
        # unexpected (the synthetic id is well-formed) –
        # but treat as a failure so the dashboard sees
        # the misconfiguration.
        error = exc
    except ProviderError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - probe must not crash the worker
        # An unexpected exception (a programming error
        # in a concrete adapter) is logged but still
        # treated as a probe failure: the worker
        # survives, the row reflects the failure, and
        # the dashboard surfaces the misconfiguration.
        # A bare :class:`Exception` has neither
        # ``code`` nor ``message`` attributes so we
        # synthesise a stable code for the audit log.
        _logger.warning(
            "health probe raised for %s/%s: %s",
            provider_name,
            channel,
            exc,
        )
        error = ProviderUnavailableError(
            str(exc)[:200] or "probe raised an unexpected exception",
            provider=provider_name,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    success = error is None
    row = await _upsert_probe_result(
        session,
        provider_name=provider_name,
        channel=channel,
        success=success,
        latency_ms=latency_ms,
        error_code=(error.code if error is not None else None),
        error_message=(error.message if error is not None else None),
    )
    return row, row.health_status, latency_ms, error


async def _upsert_probe_result(
    session: AsyncSession,
    *,
    provider_name: str,
    channel: Channel,
    success: bool,
    latency_ms: int,
    error_code: str | None,
    error_message: str | None,
) -> ProviderConfig:
    """Persist the outcome of a single probe and return the row.

    The probe row is either updated (the provider was
    already known) or created with a ``health_status``
    of ``UNKNOWN`` (the very first probe for a brand-
    new provider). Either way the returned row carries
    the post-update state so the caller can format the
    response without re-querying.
    """
    stmt = select(ProviderConfig).where(ProviderConfig.name == provider_name)
    row = (await session.execute(stmt)).scalar_one_or_none()
    now = datetime.now(tz=UTC)
    if row is None:
        row = ProviderConfig(
            name=provider_name,
            channel=channel,
            priority=0,
            health_status=ProviderHealth.UNKNOWN,
        )
        session.add(row)
        await session.flush()

    row.last_health_check = now
    row.last_latency_ms = latency_ms
    if success:
        row.consecutive_failures = 0
        row.consecutive_successes = (row.consecutive_successes or 0) + 1
        # A brand-new row (the first probe for a
        # previously-unseen provider) starts at
        # ``unknown``. The first successful probe
        # promotes it straight to ``healthy`` so the
        # dashboard does not render an "unknown" badge
        # for a provider the worker has already
        # successfully contacted. A previously-degraded
        # provider needs ``HEALTH_RECOVERY_THRESHOLD``
        # consecutive successes to be promoted back –
        # a single success is not enough to overturn a
        # known-bad signal.
        if row.health_status == ProviderHealth.UNKNOWN:
            row.health_status = ProviderHealth.HEALTHY
            row.consecutive_successes = 0
        elif (
            row.health_status != ProviderHealth.HEALTHY
            and row.consecutive_successes >= HEALTH_RECOVERY_THRESHOLD
        ):
            row.health_status = ProviderHealth.HEALTHY
            row.consecutive_successes = 0
    else:
        row.consecutive_successes = 0
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        # Mark as degraded once the failure threshold is
        # met. The provider stays ``active`` (the
        # operator can still manually disable) but the
        # dashboard renders a yellow indicator.
        if row.consecutive_failures >= HEALTH_FAILURE_THRESHOLD:
            if row.health_status != ProviderHealth.UNHEALTHY:
                row.health_status = ProviderHealth.DEGRADED
    await session.flush()
    return row


async def probe_provider(
    session: AsyncSession,
    *,
    provider_name: str,
    channel: Channel,
    settings: Settings | None = None,
) -> tuple[ProviderConfig, ProviderHealth, int]:
    """Run a single health probe and return the (row, status, latency) tuple.

    Thin wrapper over :func:`_probe_one` exposed at module
    level so the periodic worker and the admin "test
    ahora" button can share the same code path.
    """
    cfg = settings or get_settings()
    row, status, latency_ms, _ = await _probe_one(
        session,
        provider_name=provider_name,
        channel=channel,
        settings=cfg,
    )
    return row, status, latency_ms


async def run_health_checks(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    providers: Iterable[tuple[str, Channel]] | None = None,
) -> list[ProviderConfig]:
    """Run the probe across every provider the platform knows about.

    ``providers`` is the explicit allow-list the worker
    uses so the probe does not have to introspect the
    registry; tests pass a hand-rolled list to avoid
    touching the live ``Settings``. When ``providers``
    is ``None`` the function walks the registry's
    failover chain map (the chain's primary is the
    only entry that needs probing – the chain is a
    request-time concern).

    Probes run with bounded concurrency
    (:data:`HEALTHCHECK_CONCURRENCY`) so a deployment
    with many providers does not flood the worker pod.
    """
    cfg = settings or get_settings()
    targets: list[tuple[str, Channel]] = list(providers) if providers is not None else []
    if not targets:
        # Walk the chain map. The primary is the only
        # entry the probe needs to touch – a failover
        # chain is a request-time concern, not a
        # health-snapshot concern.
        for channel in supported_channels():
            for name in cfg.provider_failover_chains.get(channel.value, []):
                if name not in {n for n, _ in targets}:
                    targets.append((name, channel))
            # If no chain is configured, the registry's
            # primary is the only entry to probe. The
            # primary name lives on the provider instance,
            # so we resolve it through the registry.
            if not cfg.provider_failover_chains.get(channel.value):
                provider = get_provider(channel, settings=cfg)
                primary_name = (
                    provider.primary.name
                    if isinstance(provider, FailoverProvider)
                    else provider.name
                )
                if primary_name not in {n for n, _ in targets}:
                    targets.append((primary_name, channel))
    semaphore = asyncio.Semaphore(HEALTHCHECK_CONCURRENCY)

    async def _run(name: str, channel: Channel) -> ProviderConfig:
        async with semaphore:
            row, _status, _latency, _ = await _probe_one(
                session,
                provider_name=name,
                channel=channel,
                settings=cfg,
            )
            return row

    # The probes run *sequentially* even though we
    # wrap them with a semaphore: SQLAlchemy's
    # ``AsyncSession`` is not safe to share across
    # concurrent coroutines (a ``flush`` in flight
    # blocks the next ``add``). The semaphore is
    # retained so a future iteration that wants to
    # open per-probe sessions (the production
    # deployment's worker uses a session pool)
    # can re-introduce parallelism without
    # re-reading the helper.
    rows: list[ProviderConfig] = []
    for name, channel in targets:
        rows.append(await _run(name, channel))
    await session.commit()
    return list(rows)


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------


async def list_provider_health(
    session: AsyncSession,
    *,
    channel: Channel | None = None,
) -> Sequence[ProviderHealthRow]:
    """Return the live health snapshot for every provider.

    The query is sorted by ``(channel, priority, name)`` so
    the dashboard renders the providers in the same order
    the routing layer will use them – a sanity check the
    operator can do at a glance.
    """
    stmt = select(ProviderConfig).order_by(
        ProviderConfig.channel,
        ProviderConfig.priority,
        ProviderConfig.name,
    )
    if channel is not None:
        stmt = stmt.where(ProviderConfig.channel == channel)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return tuple(
        ProviderHealthRow(
            name=row.name,
            channel=str(row.channel),
            health_status=str(row.health_status),
            last_health_check=row.last_health_check,
            last_latency_ms=row.last_latency_ms,
            consecutive_failures=row.consecutive_failures,
            consecutive_successes=row.consecutive_successes,
            active=row.active,
            priority=row.priority,
        )
        for row in rows
    )


async def list_recent_routing_attempts(
    session: AsyncSession,
    *,
    message_id: str | None = None,
    limit: int = DEFAULT_ROUTING_LOG_LIMIT,
    offset: int = 0,
) -> tuple[Sequence[RoutingAttemptRow], int]:
    """Return the most recent :class:`RoutingLog` rows.

    The default ordering is ``attempted_at`` descending so
    the dashboard's "logs de routing" view shows the
    freshest attempt first. ``message_id`` is an optional
    filter so the per-message trace view can be served
    from the same endpoint.

    Returns ``(items, total)`` so the route layer can
    compute ``has_more`` without a second query.
    """
    page_limit = min(max(int(limit or 0), 1), _ROUTING_LOG_HARD_LIMIT)
    page_offset = max(int(offset or 0), 0)

    base = select(RoutingLog)
    count_stmt = select(func.count(RoutingLog.id))
    if message_id is not None:
        base = base.where(RoutingLog.message_id == message_id)
        count_stmt = count_stmt.where(RoutingLog.message_id == message_id)
    base = base.order_by(RoutingLog.attempted_at.desc()).limit(page_limit).offset(
        page_offset
    )
    items = (await session.execute(base)).scalars().all()
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = tuple(
        RoutingAttemptRow(
            id=row.id,
            message_id=row.message_id,
            provider=row.provider_attempted,
            channel=str(row.channel),
            outcome=str(row.outcome),
            latency_ms=row.latency_ms,
            error_code=row.error_code,
            error_message=row.error_message,
            attempted_at=row.attempted_at,
        )
        for row in items
    )
    return rows, total


__all__ = (
    "DEFAULT_ROUTING_LOG_LIMIT",
    "HEALTHCHECK_PROVIDER_MSG_ID",
    "HEALTH_FAILURE_THRESHOLD",
    "HEALTH_RECOVERY_THRESHOLD",
    "ProviderHealthRow",
    "RoutingAttemptRow",
    "build_attempt_recorder",
    "list_provider_health",
    "list_recent_routing_attempts",
    "probe_provider",
    "record_routing_attempt",
    "run_health_checks",
)
