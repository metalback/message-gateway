"""Message ORM model.

A :class:`Message` row represents a single outbound SMS or WhatsApp
message that the platform has accepted for delivery. The shape mirrors
the ``mensajes`` table documented in the PRD (see ``PRD.md`` ->
"Modelo de datos").

The lifecycle:

- ``PENDING``   – the request was accepted; the worker has not yet
                  asked the provider to send the message.
- ``QUEUED``    – the message was successfully handed to the
                  provider and is sitting in their queue.
- ``SENT``      – the provider acknowledged receipt.
- ``DELIVERED`` – the recipient's handset confirmed delivery.
- ``FAILED``    – the provider rejected the message or reported an
                  unrecoverable error. The ``error_code`` /
                  ``error_message`` columns capture the reason.
- ``UNKNOWN``   – the status could not be determined (the
                  :func:`app.services.messaging.get_message_status`
                  helper maps provider-specific values to one of
                  the above).

Security notes:

- :attr:`Message.to_number` stores the destination in the canonical
  ``+56…`` form, but the redaction helpers in
  :mod:`app.observability.redact` are still the right tool to scrub
  this column before it reaches the log stream.
- The :attr:`Message.body` is plain text. End-to-end encryption is
  out of scope for the MVP; a future iteration will integrate
  client-side encryption.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.client import _StringEnum


def _new_message_id() -> str:
    """Default factory for :attr:`Message.id`.

    Mirrors the client-side UUID generator so a freshly-built
    message can be referenced (logged, returned to the caller)
    before the row is flushed.
    """
    return str(uuid.uuid4())


class Channel(enum.StrEnum):
    """Delivery channel the message is bound for.

    Stored as a :class:`String` column (the ``Enum`` *value*) so a
    future migration that introduces a new channel does not have
    to also rewrite the column type.
    """

    SMS = "sms"
    WHATSAPP = "whatsapp"


class MessageStatus(enum.StrEnum):
    """Lifecycle states a :class:`Message` row can be in.

    The values are deliberately conservative: the provider adapter
    is responsible for mapping its own internal state
    (``queued``, ``accepted``, ``read`` …) onto one of these
    strings so the rest of the platform can reason about a single
    vocabulary.
    """

    PENDING = "pending"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Channel / status values that count towards the monthly invoice.
BILLABLE_STATUSES: frozenset[str] = frozenset(
    {MessageStatus.SENT, MessageStatus.DELIVERED}
)


class Message(Base):
    """A single outbound SMS / WhatsApp message.

    The table is named ``mensajes`` (Spanish for "messages") to
    match the PRD and the rest of the customer-facing Spanish
    copy in the dashboard.
    """

    __tablename__ = "mensajes"

    # --- Identity --------------------------------------------------------
    # UUIDs so the primary key never leaks business meaning
    # (sequential ids would let an attacker enumerate the
    # customer's traffic by walking the sequence).
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_message_id,
    )

    # Foreign key to the owning client. ``ondelete=`` is left as
    # the default (``NO ACTION``) rather than ``CASCADE`` because
    # the platform keeps a soft-delete semantic (clients are
    # marked ``suspended`` rather than deleted) so historical
    # messages survive account closure.
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )

    # --- Routing ---------------------------------------------------------
    # The provider name (``"meta_whatsapp"`` / ``"sms_aggregator"`` …)
    # is recorded so an operator can investigate a misroute without
    # having to reproduce the request. Sized to match the longest
    # provider name in :mod:`app.adapters.registry`.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    channel: Mapped[Channel] = mapped_column(
        _StringEnum(Channel, length=20),
        nullable=False,
        index=True,
    )

    # --- Payload ---------------------------------------------------------
    # Destination number, stored normalised (``+56…``). The
    # normalisation is enforced at the service layer because the
    # provider adapters already accept the canonical form, and a
    # bare ``String(20)`` is enough for the Chilean market.
    to_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Plain-text body. ``Text`` (no length cap at the DB level) so
    # the platform can ship longer WhatsApp templates; the
    # per-channel character limit is enforced at the API edge
    # instead.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Status ----------------------------------------------------------
    status: Mapped[MessageStatus] = mapped_column(
        _StringEnum(MessageStatus, length=20),
        nullable=False,
        default=MessageStatus.PENDING,
        index=True,
    )

    # Identifier the upstream assigned to the message; we persist
    # it so subsequent status checks can be correlated. Optional
    # because the value is only known *after* the provider has
    # accepted the message.
    provider_msg_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    # Short error code from the provider (e.g. ``"rate_limited"``).
    # Kept separate from the human-readable ``error_message`` so
    # an operator can filter on a stable token without parsing
    # free text.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Billing ---------------------------------------------------------
    # Cost the upstream charged the platform, in CLP cents (the
    # ``*_clp`` columns live in *centavos* so the database never
    # has to deal with floating-point currency). The integer
    # mapping is what the future billing service consumes.
    cost_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Markup the platform charges the client. The sum
    # ``cost_clp + fee_clp`` is what shows up on the monthly
    # factura.
    fee_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Message(id={self.id!r}, client_id={self.client_id!r}, "
            f"channel={self.channel!r}, status={self.status!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Message` and pre-fill the UUID primary key.

        Same rationale as :meth:`Client.__init__`: the application
        code needs the ``id`` *before* the row is flushed so it
        can return it to the caller immediately.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_message_id()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# Composite indexes that the message-sending routes actually use
# are declared on the table (rather than via ``__table_args__``) so
# Alembic's autogenerate picks them up automatically. The two
# queries we expect most often are:

# - "list my recent messages" – ``WHERE client_id = ? ORDER BY created_at DESC``
# - "list pending deliveries for the worker" – ``WHERE status = ?``


Index(
    "ix_mensajes_client_created",
    Message.__table__.c.client_id,
    Message.__table__.c.created_at,
)


__all__ = ("BILLABLE_STATUSES", "Channel", "Message", "MessageStatus")
