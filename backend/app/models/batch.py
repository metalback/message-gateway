"""Batch ORM model.

A :class:`Batch` row groups a set of outbound :class:`Message` rows
that the customer submitted together through ``POST /v1/messages/batch``
(PRD user story #7: "enviar mensajes en lote con ``POST
/v1/messages/batch``, para campañas masivas"). The row carries the
lifecycle metadata the dashboard / API needs to surface a campaign-
shaped summary ("10 000 / 9 800 delivered / 200 failed") without
forcing the caller to re-aggregate the underlying ``mensajes`` table.

The shape mirrors the ``lotes`` table documented in the PRD
("Modelo de datos"):

- ``id``               – UUIDv4 primary key, generated client-side.
- ``client_id``        – FK to ``clientes.id`` (the customer that
                         submitted the batch).
- ``name``             – optional human-readable label
                         (e.g. "Black Friday 2026"). Optional so
                         a one-off campaign can land without a
                         label.
- ``total_count``      – number of items the caller submitted.
                         Frozen at creation time so the dashboard
                         can render "X of Y delivered" without
                         having to re-derive the denominator.
- ``pending_count``    – items still in ``pending`` /
                         ``queued`` / ``sent`` state. Always
                         ``>= 0``; recomputed on every
                         :func:`update_counters` call.
- ``delivered_count``  – items in ``delivered`` state.
- ``failed_count``     – items in ``failed`` state.
- ``status``           – batch-level lifecycle
                         (``processing`` / ``completed`` /
                         ``failed``). ``processing`` while at
                         least one item is still in flight;
                         ``completed`` when every item has
                         reached a terminal state
                         (``delivered`` or ``failed``).
- ``created_at`` / ``updated_at`` – server-side timestamps.
- ``completed_at``     – set the first time ``status`` flips to
                         ``completed``; ``None`` while at least
                         one item is still in flight.

The model is intentionally narrow: a batch never owns a
"successful" boolean because the contract is "every item has its
own row + its own status". A campaign-level rollup is the
sum of the per-item statuses, kept up to date by
:func:`app.services.messaging.recompute_batch_counters`.

Security notes:

- :attr:`Batch.name` is free-form text the customer can choose.
  It is the same field the dashboard renders as a campaign name,
  so it is a candidate for PII scrub before it reaches the log
  stream (the redaction helpers in :mod:`app.observability.redact`
  are the right tool for that).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_batch_id() -> str:
    """Default factory for :attr:`Batch.id`.

    Mirrors the client-side UUID generator used by every other
    model so the row is referenceable (logged, returned to the
    caller) before the flush.
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BatchStatus(enum.StrEnum):
    """Lifecycle states a :class:`Batch` row can be in.

    The values are deliberately conservative and stored as a
    ``String`` so a future release can introduce a new state
    (``"cancelled"``, ``"partial"`` …) without rewriting the
    column type.

    Transitions:

    - ``processing``  – at least one item is still in flight
                        (``pending`` / ``queued`` / ``sent``).
    - ``completed``   – every item has reached a terminal state
                        (``delivered`` or ``failed``). The
                        :attr:`Batch.completed_at` timestamp is
                        set on this transition.
    - ``failed``      – every item ended up ``failed`` (no
                        partial success). Set when ``pending_count``
                        drops to zero and ``delivered_count`` is
                        also zero. Useful for the dashboard's
                        "campaña fallida" filter.
    """

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Batch(Base):
    """A campaign of outbound messages submitted together.

    The table is named ``lotes`` (Spanish for "batches") to match
    the rest of the customer-facing Spanish copy in the dashboard
    and the ``mensajes`` → ``lotes_mensajes`` → ``lotes`` join
    documented in the PRD.
    """

    __tablename__ = "lotes"

    # --- Identity --------------------------------------------------------
    # UUIDs so the primary key never leaks business meaning
    # (sequential ids would let an attacker enumerate a
    # customer's campaigns). The value is generated client-side
    # so the application code can return the id to the caller
    # before the row is flushed.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_batch_id,
    )

    # --- Foreign keys ----------------------------------------------------
    # ``client_id`` links the batch to the customer that
    # submitted it. The FK is a no-cascade reference (matching
    # the ``clientes`` <-> ``mensajes`` relationship) so a
    # suspended customer keeps its batch history available for
    # audit and so an operator can answer "what was the last
    # campaign that customer X ran?".
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )

    # --- Profile ---------------------------------------------------------
    # Human-readable label. Optional so a one-off campaign can
    # land without forcing the caller to pick a name. Stored as
    # ``String(200)`` to match the same ceiling the customer
    # ``name`` column uses.
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # --- Counters --------------------------------------------------------
    # The counters are denormalised: a customer-facing summary
    # ("10 000 / 9 800 delivered / 200 failed") does not have
    # to re-aggregate the ``mensajes`` table on every read. The
    # values are kept in sync by
    # :func:`app.services.messaging.recompute_batch_counters`,
    # which runs after every batch send and after every
    # delivery-receipt update.

    # Total number of items the caller submitted. Frozen at
    # creation time so the dashboard can render "X of Y" without
    # having to re-derive the denominator.
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Items in ``pending`` / ``queued`` / ``sent`` state.
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Items in ``delivered`` state.
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Items in ``failed`` state.
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Lifecycle -------------------------------------------------------
    # Batch-level lifecycle. ``processing`` while at least one
    # item is still in flight; ``completed`` when every item has
    # reached a terminal state; ``failed`` when every item
    # ended up ``failed`` (no partial success).
    status: Mapped[BatchStatus] = mapped_column(
        String(20),
        nullable=False,
        default=BatchStatus.PROCESSING,
        index=True,
    )

    # --- Timestamps ------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )
    # Set the first time ``status`` flips to ``completed``;
    # ``None`` while at least one item is still in flight.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Batch(id={self.id!r}, client_id={self.client_id!r}, "
            f"status={self.status!r}, total={self.total_count!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Batch` and pre-fill the UUID PK.

        Same rationale as
        :meth:`app.models.client.Client.__init__`: the
        application code needs the ``id`` *before* the row is
        flushed so it can return the id to the caller in the
        ``POST /v1/messages/batch`` response and so the row can
        be linked to its :class:`Message` siblings in the same
        transaction.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_batch_id()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# The queries the batch routes actually use are:
#
# - "list my batches" – ``WHERE client_id = ? ORDER BY created_at DESC``
# - "find a batch by id" – ``WHERE id = ? AND client_id = ?``
#   (cross-tenant guard means the lookup is always scoped)
# - "recompute counters for batch X" – ``WHERE batch_id = ?`` on the
#   ``mensajes`` table (composite index added in the migration).
#
# The composite index below covers the dashboard's "list my recent
# batches" query so the listing endpoint never has to fall back to
# a full table scan.

Index(
    "ix_lotes_client_created",
    Batch.__table__.c.client_id,
    Batch.__table__.c.created_at,
)


__all__ = ("Batch", "BatchStatus")
