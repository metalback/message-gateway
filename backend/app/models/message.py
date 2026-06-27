"""Message ORM model.

A :class:`Message` row represents a single SMS or WhatsApp
message the customer has sent through the platform. The shape
mirrors the ``mensajes`` table documented in the PRD (see
``PRD.md`` -> "Modelo de datos") with a couple of
billing-oriented extensions:

- :attr:`Message.cost_clp` and :attr:`Message.fee_clp` split
  the per-message economics into the cost the platform pays
  the provider and the fee the customer is charged. The
  difference is the platform's margin; the invoice line item
  uses the ``fee_clp`` so the customer only sees what they
  paid.
- The phone number is stored as a **hash** (:attr:`to_hash`)
  rather than the clear text. PII is never persisted in
  clear text per CODING_STANDARDS.md §9 and the PRD's
  "Seguridad" section.

This model is the single source of truth for "how many
messages did customer X send this month?" – the billing
service aggregates over it to compute the overage and emit the
monthly invoice. The actual *send* logic (provider adapter,
queue, webhook delivery) is out of scope for this task and
lands in the messaging feature.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_message_id() -> str:
    """Default factory for :attr:`Message.id`."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MessageChannel(enum.StrEnum):
    """Channel a message was sent through.

    Stored as a ``String`` so a future channel (RCS, email,
    …) does not require a column type migration.
    """

    SMS = "sms"
    WHATSAPP = "whatsapp"


class MessageStatus(enum.StrEnum):
    """Lifecycle states a :class:`Message` row can be in.

    Only the states relevant to billing are modelled here:

    - :attr:`QUEUED`   – accepted by the platform, not yet
      sent. Excluded from the invoice (the customer is not
      billed for messages the platform never sent).
    - :attr:`SENT`     – accepted by the provider, awaiting
      delivery confirmation. Billed.
    - :attr:`DELIVERED`– delivered to the handset. Billed.
    - :attr:`FAILED`   – the provider rejected the message.
      Billed at zero – the platform eats the cost, but the
      fee is also zero so the customer is not penalised.
    - :attr:`UNDELIVERED` – the provider confirmed the
      handset rejected the message. Not billed.
    """

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNDELIVERED = "undelivered"


# Channel / status values that count towards the monthly
# invoice. Centralised here so a future "bill on QUEUED too"
# change is a one-line edit; the billing service imports the
# constant instead of inlining the strings.
BILLABLE_STATUSES: frozenset[str] = frozenset({MessageStatus.SENT, MessageStatus.DELIVERED.value})


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Message(Base):
    """A single SMS or WhatsApp message sent by a customer.

    The table is named ``mensajes`` to match the PRD's
    vocabulary and the rest of the customer-facing Spanish
    copy in the dashboard.
    """

    __tablename__ = "mensajes"

    # --- Identity -----------------------------------------------------
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_message_id,
    )

    # Foreign key to :class:`~app.models.client.Client`. Stored
    # as the string UUID – no ``ForeignKey`` constraint is
    # declared so the unit tests can build messages without a
    # real client row. The billing service joins on the column
    # directly.
    client_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # --- Channel / recipient ------------------------------------------
    channel: Mapped[MessageChannel] = mapped_column(String(16), nullable=False, index=True)

    # Hashed (SHA-256) recipient phone number. The clear-text
    # number is **never** stored: the log stream gets the
    # ``hash_phone`` token and the database gets the raw
    # digest so the two can be correlated without the clear
    # value ever hitting a query result.
    to_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # --- Content ------------------------------------------------------
    # Message body. Truncated to 1000 chars by the API edge;
    # the column is sized to fit the SII's 1000-char soft
    # limit on free-text invoice references.
    body: Mapped[str] = mapped_column(String(1000), nullable=False)

    # --- Status / provider info ---------------------------------------
    status: Mapped[MessageStatus] = mapped_column(
        String(20),
        nullable=False,
        default=MessageStatus.QUEUED,
        index=True,
    )
    # Provider-side identifier (e.g. Meta's
    # ``wamid.<…>``). ``None`` while the message is still in
    # the queue.
    provider_msg_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Economics ----------------------------------------------------
    # Per-message cost the platform paid the provider, in CLP.
    # The value is the provider's wholesale rate at the time
    # of send; the billing service sums it to compute the
    # platform's total cost (an internal metric; not shown to
    # the customer).
    cost_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-message fee the customer is charged, in CLP. Equals
    # ``cost_clp + (markup)``; the invoice line item reads
    # this column directly. The integer is required by the
    # SII – sub-peso amounts are not legal on an electronic
    # invoice.
    fee_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Timestamps ---------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Message(id={self.id!r}, client_id={self.client_id!r}, "
            f"channel={self.channel!r}, status={self.status!r}, fee_clp={self.fee_clp!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Message` and pre-fill the UUID primary key."""
        if "id" not in kwargs:
            kwargs["id"] = _new_message_id()
        super().__init__(**kwargs)
