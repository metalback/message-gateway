"""Invoice ORM model.

A :class:`Invoice` row represents a single electronic invoice
(``factura electrónica``) the platform has issued to a
customer. The shape mirrors the ``facturas`` table documented
in the PRD (see ``PRD.md`` -> "Modelo de datos") and adds the
fields the SII's DTE specification requires:

- :attr:`Invoice.dte_number` – the folio number the SII
  assigns to the document (the platform increments a per-issuer
  counter on every emission).
- :attr:`Invoice.dte_url`    – the public URL where the
  customer (or the SII) can fetch the DTE PDF / XML.

The model also splits the total into the **net** amount
(``subtotal_clp``) and the **IVA** amount (``iva_clp``) so
the SII line items match what the customer sees on the
invoice; the DTE service is the only consumer of those two
columns.

The ``status`` enum tracks the payment lifecycle:

- :attr:`InvoiceStatus.DRAFT`    – the row exists but the DTE
  has not been emitted yet (the cron / job is still computing
  usage).
- :attr:`InvoiceStatus.ISSUED`   – the DTE has been emitted
  and the customer has been notified. The row is now
  read-only from the platform's point of view.
- :attr:`InvoiceStatus.PAID`     – the customer paid in full.
- :attr:`InvoiceStatus.OVERDUE`  – the due date passed without
  payment; the platform may downgrade the customer to the
  free tier (out of scope for this task).
- :attr:`InvoiceStatus.VOIDED`   – the DTE was annulled (a
  refund, a billing error, etc.). A voided invoice keeps the
  row for audit purposes; the DTE service emits a "nota de
  crédito" referencing the original folio.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_invoice_id() -> str:
    """Default factory for :attr:`Invoice.id`."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InvoiceStatus(enum.StrEnum):
    """Lifecycle states an :class:`Invoice` row can be in.

    Stored as a ``String`` so adding a state (e.g.
    ``"partially_refunded"``) does not require a column type
    migration. The billing service is the only writer; route
    handlers / background jobs are the only consumers.
    """

    DRAFT = "draft"
    ISSUED = "issued"
    PAID = "paid"
    OVERDUE = "overdue"
    VOIDED = "voided"


class InvoiceType(enum.StrEnum):
    """Tipo de documento tributario (SII codification).

    The MVP only emits :attr:`FACTURA_ELECTRONICA` (DTE 33)
    which is the standard "factura" used for B2B sales. A
    future "boleta electrónica" (DTE 39) for B2C customers is
    a one-line addition – the SII's structure is the same
    modulo a few mandatory fields.
    """

    FACTURA_ELECTRONICA = "factura_electronica"  # DTE 33


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Invoice(Base):
    """A single electronic invoice (factura) the platform has issued.

    The table is named ``facturas`` to match the PRD's
    vocabulary and the rest of the customer-facing Spanish
    copy in the dashboard.
    """

    __tablename__ = "facturas"

    # --- Identity -----------------------------------------------------
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_invoice_id,
    )

    # Public, sequential invoice number the platform shows to
    # the customer (e.g. ``"F-2024-000123"``). Unique so a
    # second emission can never collide with a prior one.
    number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    # --- Foreign keys -------------------------------------------------
    # ``client_id`` and ``plan_id`` are denormalised as string
    # UUIDs (no ``ForeignKey``) so the unit tests can build
    # invoices without a real client / plan row. The
    # billing service joins on the column directly.
    client_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Snapshot of the plan code at the time of issue. Stored
    # so the invoice renders correctly even if the plan row
    # is later renamed / re-priced.
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Period -------------------------------------------------------
    # The billing period the invoice covers. ``period_start``
    # is inclusive; ``period_end`` is inclusive too (i.e. the
    # last day of the month). Stored as a ``Date`` (not a
    # ``DateTime``) because the SII only cares about the day.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # --- Usage / economics --------------------------------------------
    # Total messages sent in the period (billable + non-billable).
    # The billing service breaks it down further internally
    # but the customer only needs the headline number.
    total_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Number of messages included in the plan's monthly fee
    # that the customer actually used. Cannot exceed
    # :attr:`Plan.msg_limit`.
    included_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Messages beyond the plan's limit, billed per-message.
    overage_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Net amount (the platform's fee for the month, before
    # IVA). Integer CLP – the SII does not allow sub-peso
    # amounts on a DTE.
    subtotal_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # IVA (19% by default; configured via Settings). Stored
    # separately so the DTE line items match the SII's
    # expectations.
    iva_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Total billed (``subtotal + iva``). Convenience column –
    # the SII line items are computed from ``subtotal`` /
    # ``iva``; this column is what the customer sees.
    total_clp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- DTE ----------------------------------------------------------
    # Folio number assigned by the SII (the platform's
    # per-issuer counter). ``None`` until the DTE has been
    # emitted – the DTE service is the only writer.
    dte_number: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    # Public URL of the DTE PDF / XML. The platform stores it
    # for the customer's convenience; the SII keeps the
    # canonical copy in its own repository.
    dte_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Flow integration ---------------------------------------------
    # Flow's invoice / order id. The Flow platform returns a
    # string id (``"F-12345"``) which the platform echoes
    # back in the confirmation webhook. ``None`` until the
    # payment has been initiated.
    flow_invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Status / lifecycle -------------------------------------------
    status: Mapped[InvoiceStatus] = mapped_column(
        String(20),
        nullable=False,
        default=InvoiceStatus.DRAFT,
        index=True,
    )
    tipo: Mapped[InvoiceType] = mapped_column(
        String(32),
        nullable=False,
        default=InvoiceType.FACTURA_ELECTRONICA,
    )

    # --- Dates --------------------------------------------------------
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Timestamps ---------------------------------------------------
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

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Invoice(id={self.id!r}, number={self.number!r}, "
            f"client_id={self.client_id!r}, total_clp={self.total_clp!r}, "
            f"status={self.status!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise an :class:`Invoice` and pre-fill the UUID primary key."""
        if "id" not in kwargs:
            kwargs["id"] = _new_invoice_id()
        super().__init__(**kwargs)
