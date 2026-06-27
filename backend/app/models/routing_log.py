"""Routing attempt audit log (issue #11).

A :class:`RoutingLog` row records a *single* provider attempt
the messaging service made while dispatching a message. The
table is the source of truth for:

- The admin dashboard's "intentos por proveedor" chart
  (how many calls each upstream took, how many failed, …
  the operator can spot a noisy fallback before it tips
  into a customer-visible outage).
- The "logs de routing" view the operator uses to trace
  a single message through the chain (who tried first, who
  failed, who finally accepted it, how long each leg took).

Schema
------

- ``message_id``        – FK to :class:`app.models.message.Message`.
                          ``NULL`` allowed for health-check
                          probes (the worker pings a
                          provider with a synthetic id
                          that has no associated message).
                          Indexed because the dashboard's
                          "trace this message" query groups
                          the table by ``message_id``.
- ``provider_attempted`` – the ``BaseProvider.name`` the
                          attempt targeted (e.g.
                          ``"meta_whatsapp"``). Indexed
                          because the per-provider chart
                          groups by this column.
- ``channel``            – the channel the message was
                          bound for. Duplicated from the
                          parent ``Message`` so the
                          per-channel chart can run
                          without a join.
- ``outcome``            – one of ``"success"`` /
                          ``"failure"`` /
                          ``"validation_error"``. The
                          exact taxonomy lives in
                          :class:`RoutingLogOutcome`.
- ``latency_ms``         – wall-clock time the attempt
                          took, in milliseconds. The
                          dashboard's "latencia promedio
                          por proveedor" widget averages
                          this column. ``0`` is a legal
                          value (a sub-millisecond
                          cache hit) so the column is
                          ``NOT NULL`` with a default of
                          ``0``.
- ``error_code``         – the
                          :attr:`app.adapters.errors.ProviderError.code`
                          on failure; ``NULL`` on success.
- ``error_message``      – free-text provider response
                          on failure; ``NULL`` on success.
                          Capped at 500 chars (same
                          ceiling ``Message.error_message``
                          uses) so a verbose upstream
                          cannot blow up the column.
- ``attempted_at``       – server-side timestamp. Indexed
                          so the "most recent" admin view
                          does not need to sort the
                          whole table.

Why a dedicated table rather than columns on ``Message``?
---------------------------------------------------------

A multi-provider chain produces *N* attempts per
message; storing them as a JSON blob on the message
row would block the per-provider aggregates the
dashboard needs (the "GROUP BY provider" query would
have to unnest the JSON on every request). A separate
table is the simplest representation of "one row per
attempt" and keeps ``Message`` narrow (the column
it actually needs – the *final* provider that handled
the call – already lives there).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.client import _StringEnum
from app.models.message import Channel


def _new_routing_log_id() -> str:
    """Default factory for :attr:`RoutingLog.id`.

    Mirrors the rationale in
    :func:`app.models.message._new_message_id`: the id
    is populated at construction time so the
    application code can reference a freshly-built row
    (e.g. when handing the audit id back to a log line)
    before the row is flushed.
    """
    return str(uuid.uuid4())


class RoutingLogOutcome(enum.StrEnum):
    """Outcome of a single provider attempt.

    The set is the same one the
    :mod:`app.adapters.failover` module uses to decide
    whether to advance the chain: ``success`` means
    the upstream accepted the message, ``failure``
    means a retryable error (5xx / 429), and
    ``validation_error`` means a permanent error
    (bad number, template rejected). Storing the
    three buckets explicitly lets the dashboard
    chart failures and permanent errors separately
    – a provider that constantly 422s is a
    configuration problem, not an outage.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    VALIDATION_ERROR = "validation_error"


class RoutingLog(Base):
    """A single provider attempt in a message's dispatch chain.

    The table is named ``routing_log`` (English, singular –
    the existing tables follow the same convention; the
    customer-facing Spanish copy lives in the dashboard,
    not the database).
    """

    __tablename__ = "routing_log"

    # --- Identity -----------------------------------------------------
    # UUID primary key for the same reason
    # :attr:`app.models.message.Message.id` is: the id
    # never leaks business meaning and is safe to embed
    # in log lines.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_routing_log_id,
    )

    # --- Foreign key --------------------------------------------------
    # ``mensajes.id`` so the dashboard can join the
    # audit log against the message. ``NULL`` is
    # allowed for *health-check* attempts (the
    # periodic worker probes a provider with a
    # synthetic id that has no associated message).
    # The foreign key is declared ``nullable`` because
    # the health-check path needs to insert without a
    # message; the index covers both reads.
    message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("mensajes.id"),
        nullable=True,
        index=True,
    )

    # --- Provider identification -------------------------------------
    # The ``BaseProvider.name`` the attempt targeted.
    # Not a foreign key because providers are
    # configured through ``Settings`` + the
    # :class:`ProviderConfig` table; the routing log
    # is a passive recorder, not a constraint
    # enforcer. Indexed because the per-provider
    # chart groups by this column.
    provider_attempted: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    # Channel the message was bound for. Duplicated
    # from the parent ``Message`` so the per-channel
    # chart can run without a join.
    channel: Mapped[Channel] = mapped_column(
        _StringEnum(Channel, length=20),
        nullable=False,
    )

    # --- Outcome -----------------------------------------------------
    # The single taxonomy the rest of the platform
    # uses (success / failure / validation_error).
    # Stored as a plain ``String`` so a future
    # ``"rate_limited"`` bucket can land without a
    # column-rewrite migration.
    outcome: Mapped[RoutingLogOutcome] = mapped_column(
        _StringEnum(RoutingLogOutcome, length=20),
        nullable=False,
    )

    # --- Latency -----------------------------------------------------
    # Wall-clock time the attempt took, in
    # milliseconds. ``0`` is a legal value
    # (a sub-millisecond cache hit) so the column
    # is ``NOT NULL`` with a default of ``0``.
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # --- Error context ----------------------------------------------
    # Stable error code from the
    # :class:`app.adapters.errors.ProviderError` (e.g.
    # ``"provider_unavailable"``). ``NULL`` on
    # success; non-null on failure. Sized to match
    # the same ceiling ``Message.error_code`` uses.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Human-readable error message. Capped at 500
    # chars (same ceiling ``Message.error_message``
    # uses) so a verbose upstream response cannot
    # blow up the column.
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Timestamps --------------------------------------------------
    # Server-side timestamp of the attempt. Indexed
    # so the dashboard's "most recent" view does not
    # need to sort the whole table.
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"RoutingLog(id={self.id!r}, provider={self.provider_attempted!r}, "
            f"outcome={self.outcome!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`RoutingLog` and pre-fill the UUID pk.

        Same rationale as
        :meth:`app.models.message.Message.__init__`: the
        application code needs the ``id`` *before* the
        row is flushed so it can pass the id back to a
        log line.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_routing_log_id()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# Single-column indexes on ``message_id``,
# ``provider_attempted`` and ``attempted_at`` are
# declared on the columns above. We add a composite
# index on ``(provider_attempted, attempted_at)`` so
# the dashboard's "latencia promedio por proveedor en
# las últimas 24h" query is a single lookup without a
# full table scan. Declared on the table (rather than
# via ``__table_args__``) so Alembic's autogenerate
# picks it up automatically.


Index(
    "ix_routing_log_provider_attempted_at",
    RoutingLog.__table__.c.provider_attempted,
    RoutingLog.__table__.c.attempted_at,
)


__all__ = ("RoutingLog", "RoutingLogOutcome")
