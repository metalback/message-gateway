"""Payment ORM model.

A :class:`Payment` row tracks a single attempt to charge a
customer through the Flow gateway. The model is the
authoritative record of "did the customer pay invoice X?" –
the platform's billing flow is:

1. The cron / scheduled job generates an
   :class:`~app.models.invoice.Invoice` and stamps it
   ``ISSUED``.
2. ``POST /v1/billing/payments`` (the dashboard's "Pay now"
   button) mints a :class:`Payment` row and asks Flow to
   create a Webpay order. The response carries a
   ``flow_token`` and a ``flow_redirect_url`` the customer
   is redirected to.
3. Flow POSTs the asynchronous ``payment/confirm`` webhook
   to the platform. The billing service looks the payment up
   by ``flow_token`` and flips its status to ``PAID`` or
   ``FAILED`` (and the related invoice to ``PAID`` if the
   amount matches).
4. The customer can also poll ``GET /v1/billing/payments/{id}``
   – the route handler calls Flow's ``/payment/getStatus``
   endpoint to refresh the local row if it is still pending.

The status enum mirrors Flow's own state machine so the
billing service can re-use Flow's status codes without an
extra translation layer.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_payment_id() -> str:
    """Default factory for :attr:`Payment.id`."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaymentStatus(enum.StrEnum):
    """Lifecycle states a :class:`Payment` row can be in.

    Mirrors Flow's status codes (``1`` = pending,
    ``2`` = paid, ``3`` = rejected, ``4`` = cancelled) so the
    billing service can store Flow's response verbatim. The
    ``FAILED`` / ``CANCELLED`` values are platform-side
    convenience labels that map to Flow's ``3`` / ``4``.
    """

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Payment(Base):
    """A single attempt to charge a customer through Flow.

    The table is named ``pagos`` to match the PRD's
    vocabulary.
    """

    __tablename__ = "pagos"

    # --- Identity -----------------------------------------------------
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_payment_id,
    )

    # --- Foreign keys -------------------------------------------------
    # ``client_id`` and ``invoice_id`` are stored as string
    # UUIDs (no ``ForeignKey``) so unit tests can build
    # payments without a real client / invoice row.
    client_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    invoice_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Economics ----------------------------------------------------
    # Amount the customer is being charged, in CLP. Mirrors
    # the invoice's ``total_clp``; the platform refuses the
    # payment if the two diverge (an out-of-band change to
    # the invoice invalidates the Flow order).
    amount_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Flow integration ---------------------------------------------
    # Flow's per-payment token. Returned by
    # ``POST /payment/create``; the customer is redirected to
    # ``https://sandbox.flow.cl/payment/<token>``. Also the
    # lookup key for the asynchronous ``payment/confirm``
    # webhook and the ``/payment/getStatus`` poll.
    flow_token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    # Flow's per-customer order id (``commerce_order``). Echoed
    # back by the webhook and the status poll; stored so a
    # future "match by order id" audit is one query away.
    flow_order: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Flow's per-payment internal id. ``None`` until the
    # payment has been created on Flow's side.
    flow_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The URL the customer should be redirected to. Computed
    # by the Flow adapter; ``None`` until the order has been
    # created.
    flow_redirect_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Raw JSON payload of the last Flow response (creation
    # or confirmation). Kept for audit / debugging; never
    # exposed to the customer.
    flow_response: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # --- Status / lifecycle -------------------------------------------
    status: Mapped[PaymentStatus] = mapped_column(
        String(20),
        nullable=False,
        default=PaymentStatus.PENDING,
        index=True,
    )

    # --- Timestamps ---------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Payment(id={self.id!r}, client_id={self.client_id!r}, "
            f"amount_clp={self.amount_clp!r}, status={self.status!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Payment` and pre-fill the UUID primary key."""
        if "id" not in kwargs:
            kwargs["id"] = _new_payment_id()
        super().__init__(**kwargs)
