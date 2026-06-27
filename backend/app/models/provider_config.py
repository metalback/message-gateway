"""Provider configuration and runtime health (issue #11).

A :class:`ProviderConfig` row is the persisted record of one
provider the platform knows about. It carries both the
*static* configuration (channel, priority, base URL) and the
*dynamic* health snapshot the periodic health-check worker
writes on every run (``health_status``,
``last_health_check``, ``consecutive_failures`` …).

The table is the source of truth for the admin dashboard's
"estado de proveedores" card (the green / yellow / red
indicator the operator uses to triage an outage). The
:mod:`app.services.provider_health` module owns the writes;
the rest of the codebase reads through
:func:`app.services.provider_health.list_provider_health`.

Why a dedicated table rather than a ``Settings`` flag?
-----------------------------------------------------

``Settings`` describes the *boot-time* state of the platform;
``ProviderConfig`` describes the *runtime* state of a
provider. Conflating the two would force every health-check
worker to re-read configuration, mutate a Pydantic model and
persist the diff through a config-only write path – error-
prone and untestable. A dedicated table is the simplest
representation of "the database is the system of record for
runtime health" and keeps ``Settings`` honest as a boot-time
input.

Schema
------

- ``name``            – the provider's registered
                        ``BaseProvider.name`` (e.g.
                        ``"meta_whatsapp"``). Unique because
                        two rows for the same upstream would
                        race the health-check worker.
- ``channel``         – the :class:`app.models.message.Channel`
                        the provider serves. Indexed because
                        the registry's per-channel lookup is
                        the most frequent access pattern.
- ``priority``        – the failover chain position
                        (``0`` = primary, ``1`` = first
                        fallback, …). Used by the routing
                        layer when an admin manually overrides
                        the chain order.
- ``base_url``        – the upstream's HTTP endpoint, kept
                        here so the admin dashboard can
                        render a clickable link without
                        having to resolve it from
                        ``Settings``.
- ``health_status``   – one of ``"healthy"`` / ``"degraded"``
                        / ``"unhealthy"`` / ``"unknown"``. A
                        fresh row defaults to ``"unknown"``
                        so a never-probed provider does not
                        silently count as healthy on the
                        dashboard.
- ``last_health_check`` – server-side timestamp of the most
                        recent probe; ``NULL`` until the
                        first health check runs.
- ``consecutive_failures`` / ``consecutive_successes`` –
                        counters the health worker increments
                        on each probe. The transition rules
                        (``N`` failures → ``degraded``,
                        ``M`` successes → ``healthy``) live
                        in :mod:`app.services.provider_health`.
- ``active``          – manual kill-switch the operator
                        flips from the admin dashboard.
                        ``False`` means "do not route
                        traffic to this provider" – the
                        failover router skips the row at
                        chain resolution time.
- ``created_at`` / ``updated_at`` – server-side timestamps.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.client import _StringEnum
from app.models.message import Channel


def _new_provider_config_id() -> str:
    """Default factory for :attr:`ProviderConfig.id`.

    Mirrors the rationale in
    :func:`app.models.client._new_client_id`: the id is
    populated at construction time so a freshly-built
    row can be referenced (logged, returned to the
    caller) before the row is flushed.
    """
    return str(uuid.uuid4())


class ProviderHealth(enum.StrEnum):
    """Health snapshot the periodic probe writes per row.

    The values are deliberately coarse ("healthy" /
    "degraded" / "unhealthy" / "unknown") because the
    dashboard's traffic-light indicator only needs three
    buckets. A future "include latency" iteration can land
    as a separate column without rewriting the enum.
    """

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ProviderConfig(Base):
    """Persisted configuration + health snapshot for one provider.

    The table is named ``provider_config`` (English – the
    same convention :class:`Webhook` follows; the
    customer-facing Spanish copy lives in the dashboard,
    not the database).
    """

    __tablename__ = "provider_config"

    # --- Identity -----------------------------------------------------
    # UUID primary key so the ``/v1/admin/providers/health``
    # endpoint can reference rows without leaking the
    # operator's internal id sequence.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_provider_config_id,
    )

    # --- Provider identification -------------------------------------
    # The provider's ``BaseProvider.name`` (e.g. ``"meta_whatsapp"``).
    # Unique because two rows for the same upstream would race
    # the health-check worker and silently double the rate of
    # outbound probes. The admin dashboard addresses a row by
    # ``(name, channel)`` rather than by the surrogate UUID.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # Channel the provider serves. Stored as a plain
    # ``String`` (via :class:`_StringEnum`) so a future
    # "voice" channel can land without rewriting the
    # column type.
    channel: Mapped[Channel] = mapped_column(
        _StringEnum(Channel, length=20),
        nullable=False,
        index=True,
    )

    # --- Failover order ----------------------------------------------
    # ``0`` is the primary, ``1`` is the first fallback, …
    # The routing layer (see
    # :mod:`app.adapters.failover`) reads the chain from
    # ``Settings.provider_failover_chains``; the column
    # here is the *admin override* the dashboard applies
    # when the operator rearranges the chain. The
    # ``Settings`` value wins at boot; a non-null
    # ``priority`` here wins at runtime.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Endpoint ----------------------------------------------------
    # The upstream's HTTP endpoint, surfaced on the admin
    # dashboard so the operator can click through to a
    # status page. Optional because some providers (the
    # local SMS aggregator) do not have a useful status
    # URL to render.
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Health snapshot --------------------------------------------
    # Current health bucket. The health-check worker is
    # the sole writer – the messaging service does not
    # touch this column at request time. ``unknown`` is
    # the column default so a never-probed provider
    # does not silently count as healthy on the
    # dashboard.
    health_status: Mapped[ProviderHealth] = mapped_column(
        _StringEnum(ProviderHealth, length=20),
        nullable=False,
        default=ProviderHealth.UNKNOWN,
        server_default=ProviderHealth.UNKNOWN.value,
    )

    # Server-side timestamp of the most recent probe.
    # ``NULL`` until the first health check runs. Indexed
    # so the dashboard's "stale data" alert can run a
    # single ``WHERE last_health_check < now() - 5m``
    # query without a full table scan.
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # --- Failure / success counters ---------------------------------
    # Counters the health worker increments on each probe.
    # Reset to zero on every status transition so a
    # long-running provider does not accumulate
    # meaningless failures after a recovery. Indexed
    # together (composite) so the dashboard's
    # "long-running failures" query is a single lookup.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    consecutive_successes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Latency in milliseconds of the most recent probe;
    # ``NULL`` until the first run. Surfaced on the
    # dashboard as the "latencia promedio" indicator.
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Operator kill-switch ---------------------------------------
    # ``True`` by default; flipping to ``False`` disables
    # the provider without losing the row. The routing
    # layer treats an inactive row as "skip me in the
    # chain" – the chain re-resolves on the next request
    # so the operator's action takes effect immediately.
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # --- Timestamps --------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ProviderConfig(id={self.id!r}, name={self.name!r}, "
            f"channel={self.channel!r}, health_status={self.health_status!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`ProviderConfig` and pre-fill the UUID pk.

        Same rationale as
        :meth:`app.models.client.Client.__init__`: the
        application code needs the ``id`` *before* the row
        is flushed so it can hand a freshly-built row back
        to the caller immediately.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_provider_config_id()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# Single-column index on ``(name)`` is implied by the
# ``unique=True`` constraint above; we add a composite
# index on ``(active, channel)`` so the registry's "list
# active providers for channel X" query is a single
# lookup. Declared on the table (rather than via
# ``__table_args__``) so Alembic's autogenerate picks it up
# automatically.


Index(
    "ix_provider_config_active_channel",
    ProviderConfig.__table__.c.active,
    ProviderConfig.__table__.c.channel,
)


__all__ = ("ProviderConfig", "ProviderHealth")
