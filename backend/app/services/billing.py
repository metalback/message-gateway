"""Billing & invoicing service.

This module owns the domain logic behind the platform's
monthly billing cycle:

- :func:`compute_invoice`    – sum a customer's billable
  messages over a billing period, apply the plan's monthly
  fee + per-message overage rate, add IVA, and return the
  line items the caller will persist on an
  :class:`~app.models.invoice.Invoice` row.
- :func:`finalize_invoice`   – take the :class:`Invoice`
  persisted by :func:`compute_invoice` and ask the DTE
  service to emit a ``DTE 33`` (factura electrónica), then
  flip the row's status to ``issued``. The DTE folio and
  PDF URL are written back so the dashboard can show
  "factura emitida" with a download link.
- :func:`list_invoices`      – return the customer's invoice
  history, ordered from newest to oldest.
- :func:`get_invoice`        – fetch a single invoice,
  ensuring the requesting client owns it.
- :func:`switch_subscription` – move a client to a different
  :class:`~app.models.plan.Plan` (the next invoice will be
  computed against the new plan).
- :func:`get_balance`        – return the headline counters
  the dashboard needs ("you've used 1,234 of 1,000
  messages this month"). The endpoint is read-only and
  cheap to call on every page load.

The module never issues HTTP requests to the Flow API – that
lives in :mod:`app.adapters.flow` – and never emits a DTE –
that lives in :mod:`app.services.dte`. The service is a
pure orchestrator on top of those two collaborators, so
unit tests can swap either for a fake and assert the
end-to-end billing math without touching the network.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import Client
from app.models.invoice import Invoice, InvoiceStatus
from app.models.message import BILLABLE_STATUSES, Message
from app.models.payment import Payment
from app.models.plan import Plan
from app.observability import get_logger

if TYPE_CHECKING:
    from app.adapters.flow import FlowClient, FlowPaymentStatus
    from app.services.dte import DteService

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BillingError(Exception):
    """Base class for every billing-domain exception.

    Mirrors the contract :class:`app.services.auth.AuthError`
    exposes: a stable ``code`` for the front-end, a human
    ``message`` and a ``http_status`` the route layer maps
    onto an :class:`fastapi.HTTPException`.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PlanNotFoundError(BillingError):
    """The requested :class:`Plan` does not exist or is not active."""

    http_status = 404


class InvoiceNotFoundError(BillingError):
    """The :class:`Invoice` does not exist (or belongs to another client)."""

    http_status = 404


class InvoiceAlreadyIssuedError(BillingError):
    """The :class:`Invoice` is already past the DRAFT stage."""

    http_status = 409


class InvalidBillingPeriodError(BillingError):
    """The requested period is not a valid calendar month."""

    http_status = 422


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceLineItem:
    """A single line on the invoice's subtotal breakdown.

    The SII DTE spec accepts multiple ``Detalle`` rows; the
    MVP ships one row per "tier" (monthly fee, included
    messages, overage) so the customer can audit the math
    without having to reverse-engineer a single lump sum.
    """

    description: str
    quantity: int
    unit_price_clp: int
    total_clp: int


@dataclass(frozen=True)
class InvoiceDraft:
    """The output of :func:`compute_invoice`.

    The caller (:func:`finalize_invoice` in production, the
    unit tests in dev) persists every field of
    :attr:`invoice` on the corresponding
    :class:`~app.models.invoice.Invoice` row.
    """

    invoice: Invoice
    line_items: tuple[InvoiceLineItem, ...]
    included_msgs: int
    overage_msgs: int
    subtotal_clp: int
    iva_clp: int
    total_clp: int


@dataclass(frozen=True)
class BalanceSummary:
    """The output of :func:`get_balance`.

    ``used_msgs`` includes both billable and non-billable
    messages; ``billable_msgs`` is the subset the platform
    will actually charge for. The two numbers differ when
    a message was rejected by the provider and ends up
    in :attr:`MessageStatus.UNDELIVERED` (or any of the
    non-billable terminal states).
    """

    plan_code: str
    plan_name: str
    period_start: date
    period_end: date
    msg_limit: int | None
    used_msgs: int
    billable_msgs: int
    overage_msgs: int
    overage_cost_clp: int
    estimated_total_clp: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month_bounds(value: date) -> tuple[date, date]:
    """Return ``[first_day, last_day]`` of the month containing ``value``.

    Both bounds are inclusive. The function is the source
    of truth for "what does *this month* mean?" so the
    cron job, the dashboard and the tests agree on the
    same range.
    """
    if not isinstance(value, date):
        raise InvalidBillingPeriodError(
            "invalid_period",
            "billing period must be a calendar date",
        )
    first = value.replace(day=1)
    last_day = calendar.monthrange(value.year, value.month)[1]
    last = value.replace(day=last_day)
    return first, last


def _next_invoice_number(plan_code: str, period: date) -> str:
    """Build the public invoice number ``F-YYYY-NNNNNN``.

    The format is what the SII expects in the
    ``Folio`` field of a DTE 33 and what the customer
    sees on the dashboard. The number is purely
    informational – the database is the source of truth
    for uniqueness; the migration adds a ``UNIQUE``
    constraint on :attr:`Invoice.number`.

    The trailing six digits are derived from a UUID
    prefix so two emissions in the same second still
    collide on different values. The production
    sequence lives in the ``facturas_secuencia`` table
    (a follow-up task); the UUID-based placeholder is
    what the MVP uses.
    """
    import uuid

    if not isinstance(plan_code, str) or not plan_code:
        raise InvalidBillingPeriodError(
            "invalid_plan_code",
            "plan code is required to build an invoice number",
        )
    if not isinstance(period, date):
        raise InvalidBillingPeriodError(
            "invalid_period",
            "billing period must be a calendar date",
        )
    return f"F-{period.year}-{uuid.uuid4().hex[:6].upper()}"


def _compute_iva(subtotal_clp: int, iva_rate: float) -> int:
    """Return the IVA (in CLP) for a given subtotal and rate.

    The SII requires integer amounts on a DTE, so the
    function rounds to the nearest peso (using banker
    semantics, matching the rest of the platform's
    accounting helpers). A future "half-up" tweak is a
    one-line change here.
    """
    if subtotal_clp < 0:
        raise BillingError("invalid_subtotal", "subtotal cannot be negative")
    if iva_rate < 0:
        raise BillingError("invalid_iva_rate", "iva rate cannot be negative")
    return int(round(subtotal_clp * iva_rate))


# ---------------------------------------------------------------------------
# Plan / subscription operations
# ---------------------------------------------------------------------------


async def list_plans(session: AsyncSession) -> list[Plan]:
    """Return every active :class:`Plan`, ordered for the pricing page."""
    stmt = (
        select(Plan)
        .where(Plan.active.is_(True))
        .order_by(Plan.sort_order.asc(), Plan.price_clp.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_plan_by_code(session: AsyncSession, code: str) -> Plan:
    """Resolve a plan code to the matching :class:`Plan` row.

    Raises :class:`PlanNotFoundError` when the plan does
    not exist or is marked ``active=False`` (retired
    plans cannot be subscribed to).
    """
    if not isinstance(code, str) or not code:
        raise PlanNotFoundError("invalid_plan", "plan code is required")
    stmt = select(Plan).where(Plan.code == code, Plan.active.is_(True))
    result = await session.execute(stmt)
    plan = result.scalar_one_or_none()
    if plan is None:
        raise PlanNotFoundError(
            "plan_not_found",
            f"plan {code!r} does not exist or is no longer available",
        )
    return plan


async def switch_subscription(
    session: AsyncSession,
    *,
    client: Client,
    plan_code: str,
    settings: Settings | None = None,
) -> Plan:
    """Move ``client`` to the plan identified by ``plan_code``.

    The change takes effect immediately: the next invoice
    is computed against the new plan, and the dashboard
    shows the new price from the next page load. The
    function does **not** retroactively re-price messages
    already billed; that is an out-of-band operation
    (refund / credit) that lives outside the MVP scope.

    Returns the freshly loaded :class:`Plan` row so the
    caller can echo the new price in the response.
    """
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    plan = await get_plan_by_code(session, plan_code)
    # The string→enum coercion lives in
    # :class:`app.models.client._StringEnum`; we set the
    # enum member directly so the change is round-trip safe
    # regardless of the database column type.
    from app.models.client import ClientPlan

    try:
        new_plan = ClientPlan(plan.code)
    except ValueError as exc:
        # The ``planes.code`` column is the source of truth
        # for "is this a valid plan code?". A drift between
        # ``planes.code`` and :class:`ClientPlan` would
        # surface here as a 422.
        raise PlanNotFoundError(
            "plan_not_in_client_enum",
            f"plan {plan.code!r} is not a known client plan",
        ) from exc
    client.plan = new_plan
    await session.commit()
    await session.refresh(client)
    return plan


# ---------------------------------------------------------------------------
# Invoice computation
# ---------------------------------------------------------------------------


async def _count_messages(
    session: AsyncSession,
    *,
    client_id: str,
    period_start: date,
    period_end: date,
    statuses: frozenset[str] | None = None,
) -> int:
    """Count :class:`Message` rows for ``client_id`` in the period.

    ``created_at`` is the filter key (the PRD documents
    that the SII cares about the day the message was
    *dispatched*, not the day it was *queued*). The
    function takes an optional ``statuses`` filter so the
    same code path can answer both "all messages sent
    this month" and "billable messages sent this month".
    """
    if not isinstance(client_id, str) or not client_id:
        raise BillingError("invalid_client", "client id is required")
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(period_end, datetime.max.time(), tzinfo=UTC)
    stmt = select(func.count(Message.id)).where(
        and_(
            Message.client_id == client_id,
            Message.created_at >= start_dt,
            Message.created_at <= end_dt,
        )
    )
    if statuses is not None:
        stmt = stmt.where(Message.status.in_(tuple(statuses)))
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


async def compute_invoice(
    session: AsyncSession,
    *,
    client: Client,
    period: date | None = None,
    settings: Settings | None = None,
) -> InvoiceDraft:
    """Aggregate a customer's usage over a billing period.

    The function reads the customer's current plan, sums
    the billable messages over the period, and returns an
    :class:`InvoiceDraft` the caller can persist and finalise.
    The draft is **not** committed – the caller decides
    when to durably store it.
    """
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    cfg = settings or get_settings()
    if period is None:
        period = datetime.now(tz=UTC).date()
    period_start, period_end = _month_bounds(period)

    plan = await get_plan_by_code(session, client.plan.value)
    total_msgs = await _count_messages(
        session,
        client_id=client.id,
        period_start=period_start,
        period_end=period_end,
    )
    billable_msgs = await _count_messages(
        session,
        client_id=client.id,
        period_start=period_start,
        period_end=period_end,
        statuses=BILLABLE_STATUSES,
    )

    if plan.msg_limit is None:
        # Enterprise: no per-message overage, the customer
        # is billed the negotiated monthly fee only.
        included_msgs = 0
        overage_msgs = 0
    else:
        included_msgs = min(billable_msgs, plan.msg_limit)
        overage_msgs = max(0, billable_msgs - plan.msg_limit)

    line_items: list[InvoiceLineItem] = []
    if plan.price_clp > 0:
        line_items.append(
            InvoiceLineItem(
                description=f"Plan {plan.name} (mensual)",
                quantity=1,
                unit_price_clp=plan.price_clp,
                total_clp=plan.price_clp,
            )
        )
    if plan.msg_limit is not None and overage_msgs > 0 and plan.extra_msg_price is not None:
        line_items.append(
            InvoiceLineItem(
                description="Mensajes adicionales",
                quantity=overage_msgs,
                unit_price_clp=plan.extra_msg_price,
                total_clp=overage_msgs * plan.extra_msg_price,
            )
        )

    subtotal_clp = sum(item.total_clp for item in line_items)
    iva_clp = _compute_iva(subtotal_clp, cfg.billing_iva_rate)
    total_clp = subtotal_clp + iva_clp

    issue_date = datetime.now(tz=UTC).date()
    due_date = issue_date + timedelta(days=cfg.billing_due_days)

    invoice = Invoice(
        number=_next_invoice_number(plan.code, period),
        client_id=client.id,
        plan_id=plan.id,
        plan_code=plan.code,
        period_start=period_start,
        period_end=period_end,
        total_msgs=total_msgs,
        included_msgs=included_msgs,
        overage_msgs=overage_msgs,
        subtotal_clp=subtotal_clp,
        iva_clp=iva_clp,
        total_clp=total_clp,
        status=InvoiceStatus.DRAFT,
        issue_date=issue_date,
        due_date=due_date,
    )

    logger.info(
        "billing.invoice.computed",
        extra={
            "client_id": client.id,
            "plan_code": plan.code,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "billable_msgs": billable_msgs,
            "overage_msgs": overage_msgs,
            "subtotal_clp": subtotal_clp,
            "iva_clp": iva_clp,
            "total_clp": total_clp,
        },
    )

    return InvoiceDraft(
        invoice=invoice,
        line_items=tuple(line_items),
        included_msgs=included_msgs,
        overage_msgs=overage_msgs,
        subtotal_clp=subtotal_clp,
        iva_clp=iva_clp,
        total_clp=total_clp,
    )


async def persist_invoice_draft(
    session: AsyncSession,
    *,
    draft: InvoiceDraft,
) -> Invoice:
    """Persist the :class:`Invoice` carried by ``draft``.

    Helper split out of :func:`finalize_invoice` so the
    route layer can persist the draft before asking the
    DTE service to emit (the DTE service needs a stable
    invoice id to embed in the XML).
    """
    if not isinstance(draft, InvoiceDraft):
        raise BillingError("invalid_draft", "draft is required")
    session.add(draft.invoice)
    await session.commit()
    await session.refresh(draft.invoice)
    return draft.invoice


async def finalize_invoice(
    session: AsyncSession,
    *,
    invoice: Invoice,
    dte_service: DteService | None = None,
    settings: Settings | None = None,
) -> Invoice:
    """Mark ``invoice`` as issued and emit the DTE.

    The DTE service is injected so unit tests can supply a
    stub. Production wires the real implementation from
    :mod:`app.services.dte`.
    """
    if not isinstance(invoice, Invoice):
        raise BillingError("invalid_invoice", "invoice is required")
    if invoice.status != InvoiceStatus.DRAFT:
        raise InvoiceAlreadyIssuedError(
            "invoice_not_draft",
            f"invoice {invoice.number} is in status {invoice.status} and cannot be re-issued",
        )
    if dte_service is None:
        from app.services.dte import DteService

        dte_service = DteService(settings=settings)
    dte = await dte_service.emit(invoice=invoice)
    invoice.dte_number = dte.folio
    invoice.dte_url = dte.url
    invoice.status = InvoiceStatus.ISSUED
    await session.commit()
    await session.refresh(invoice)
    logger.info(
        "billing.invoice.issued",
        extra={
            "invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "dte_number": invoice.dte_number,
        },
    )
    return invoice


# ---------------------------------------------------------------------------
# Invoice listing / retrieval
# ---------------------------------------------------------------------------


async def list_invoices(
    session: AsyncSession,
    *,
    client: Client,
) -> list[Invoice]:
    """Return the customer's invoice history, newest first.

    The order is by ``period_start`` descending – the
    billing period is the meaningful sort key for the
    dashboard. ``id`` is the tiebreaker: it is unique
    (UUID) so the result is deterministic regardless of
    the underlying database's ``DateTime`` precision.
    """
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    stmt = (
        select(Invoice)
        .where(Invoice.client_id == client.id)
        .order_by(Invoice.period_start.desc(), Invoice.id.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_invoice(
    session: AsyncSession,
    *,
    client: Client,
    invoice_id: str,
) -> Invoice:
    """Fetch a single invoice, ensuring the caller owns it."""
    if not isinstance(invoice_id, str) or not invoice_id:
        raise InvoiceNotFoundError("invalid_invoice_id", "invoice id is required")
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    stmt = select(Invoice).where(Invoice.id == invoice_id)
    result = await session.execute(stmt)
    invoice = result.scalar_one_or_none()
    if invoice is None or invoice.client_id != client.id:
        raise InvoiceNotFoundError(
            "invoice_not_found",
            f"invoice {invoice_id} does not exist",
        )
    return invoice


async def list_payments(
    session: AsyncSession,
    *,
    client: Client,
) -> list[Payment]:
    """Return the customer's payment history, newest first."""
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    stmt = (
        select(Payment)
        .where(Payment.client_id == client.id)
        .order_by(Payment.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Balance / dashboard counters
# ---------------------------------------------------------------------------


async def get_balance(
    session: AsyncSession,
    *,
    client: Client,
    settings: Settings | None = None,
) -> BalanceSummary:
    """Return the headline counters for the dashboard.

    The function does not generate an invoice; it just
    aggregates the live :class:`Message` rows so the
    dashboard can show "you've used X of Y messages this
    month" on every page load without paying the cost of
    a full DTE emission.
    """
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    cfg = settings or get_settings()
    today = datetime.now(tz=UTC).date()
    period_start, period_end = _month_bounds(today)
    plan = await get_plan_by_code(session, client.plan.value)
    total_msgs = await _count_messages(
        session,
        client_id=client.id,
        period_start=period_start,
        period_end=period_end,
    )
    billable_msgs = await _count_messages(
        session,
        client_id=client.id,
        period_start=period_start,
        period_end=period_end,
        statuses=BILLABLE_STATUSES,
    )
    if plan.msg_limit is None:
        overage_msgs = 0
    else:
        overage_msgs = max(0, billable_msgs - plan.msg_limit)
    overage_cost = (
        overage_msgs * (plan.extra_msg_price or 0)
        if plan.extra_msg_price is not None
        else 0
    )
    estimated_subtotal = plan.price_clp + overage_cost
    estimated_iva = _compute_iva(estimated_subtotal, cfg.billing_iva_rate)
    return BalanceSummary(
        plan_code=plan.code,
        plan_name=plan.name,
        period_start=period_start,
        period_end=period_end,
        msg_limit=plan.msg_limit,
        used_msgs=total_msgs,
        billable_msgs=billable_msgs,
        overage_msgs=overage_msgs,
        overage_cost_clp=overage_cost,
        estimated_total_clp=estimated_subtotal + estimated_iva,
    )


# ---------------------------------------------------------------------------
# Flow payment orchestration
# ---------------------------------------------------------------------------


async def create_payment(
    session: AsyncSession,
    *,
    client: Client,
    invoice: Invoice,
    flow_client: FlowClient | None = None,
    settings: Settings | None = None,
) -> Payment:
    """Create a Flow order for ``invoice`` and persist a :class:`Payment`.

    The Flow API call is delegated to the injected
    :class:`FlowClient` (a thin wrapper around ``httpx``
    that the :mod:`app.adapters.flow` module owns) so
    unit tests can supply a stub that returns a canned
    response without touching the network.

    The function also writes
    :attr:`Invoice.flow_invoice_id` on the invoice row
    so the platform can correlate the asynchronous
    ``payment/confirm`` webhook with the originating
    invoice.
    """
    if not isinstance(client, Client):
        raise BillingError("invalid_client", "client is required")
    if not isinstance(invoice, Invoice):
        raise BillingError("invalid_invoice", "invoice is required")
    if invoice.client_id != client.id:
        raise BillingError(
            "invoice_mismatch",
            "invoice does not belong to the requesting client",
        )
    if invoice.status not in {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED, InvoiceStatus.OVERDUE}:
        raise InvoiceAlreadyIssuedError(
            "invoice_not_payable",
            f"invoice in status {invoice.status.value} cannot be paid",
        )
    cfg = settings or get_settings()
    if flow_client is None:
        from app.adapters.flow import FlowClient

        flow_client = FlowClient(settings=cfg)
    commerce_order = f"INV-{invoice.id}"
    order = await flow_client.create_order(
        commerce_order=commerce_order,
        subject=f"Factura {invoice.number}",
        amount_clp=invoice.total_clp,
        email=client.email,
    )
    payment = Payment(
        client_id=client.id,
        invoice_id=invoice.id,
        plan_code=invoice.plan_code,
        amount_clp=invoice.total_clp,
        flow_token=order.token,
        flow_order=commerce_order,
        flow_payment_id=None,
        flow_redirect_url=order.redirect_url,
        flow_response=order.raw_json,
    )
    invoice.flow_invoice_id = commerce_order
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    logger.info(
        "billing.payment.created",
        extra={
            "payment_id": payment.id,
            "invoice_id": invoice.id,
            "flow_token": payment.flow_token,
            "amount_clp": payment.amount_clp,
        },
    )
    return payment


async def refresh_payment_status(
    session: AsyncSession,
    *,
    client: Client,
    payment_id: str,
    flow_client: FlowClient | None = None,
    settings: Settings | None = None,
) -> Payment:
    """Poll Flow for the current status of ``payment``.

    The platform's primary notification path is the
    asynchronous ``payment/confirm`` webhook, but the
    dashboard can also call this endpoint to refresh a
    payment the user is staring at. The function
    delegates to :func:`apply_flow_status` so the
    webhook / poll paths share the same status-mapping
    logic.
    """
    if not isinstance(payment_id, str) or not payment_id:
        raise BillingError("invalid_payment", "payment id is required")
    stmt = select(Payment).where(Payment.id == payment_id)
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()
    if payment is None or payment.client_id != client.id:
        raise BillingError("payment_not_found", "payment does not exist")
    # The model stores ``status`` as a ``String``; we
    # compare the string value rather than the enum to
    # avoid a deserialisation round-trip.
    if payment.status in {"paid", "cancelled", "expired"}:
        # Terminal states never need a refresh; the local
        # row is the source of truth.
        return payment
    cfg = settings or get_settings()
    if flow_client is None:
        from app.adapters.flow import FlowClient

        flow_client = FlowClient(settings=cfg)
    status = await flow_client.get_status(token=payment.flow_token)
    await apply_flow_status(
        session,
        payment=payment,
        flow_status=status,
    )
    return payment


async def apply_flow_status(
    session: AsyncSession,
    *,
    payment: Payment,
    flow_status: FlowPaymentStatus,
) -> Payment:
    """Reconcile a Flow status payload against the local :class:`Payment`.

    Used by both the webhook handler and the poll endpoint
    so the two paths never disagree on the mapping
    (Flow's ``status=2`` is paid, ``3`` is rejected, etc.).
    """
    from app.models.payment import PaymentStatus

    if not isinstance(payment, Payment):
        raise BillingError("invalid_payment", "payment is required")
    if flow_status.status == 2:
        payment.status = PaymentStatus.PAID
        payment.flow_payment_id = flow_status.payment_id
        payment.confirmed_at = datetime.now(tz=UTC)
        if payment.invoice_id is not None:
            invoice = await session.get(Invoice, payment.invoice_id)
            if invoice is not None and invoice.status != InvoiceStatus.PAID:
                invoice.status = InvoiceStatus.PAID
                invoice.paid_at = payment.confirmed_at
        await session.commit()
        await session.refresh(payment)
    elif flow_status.status == 3:
        payment.status = PaymentStatus.FAILED
        await session.commit()
        await session.refresh(payment)
    elif flow_status.status == 4:
        payment.status = PaymentStatus.CANCELLED
        await session.commit()
        await session.refresh(payment)
    elif flow_status.status == 5:
        # Flow uses ``5`` to mark an order that expired
        # without being paid. The platform's enum does not
        # have a separate ``expired`` value, but it is
        # exposed for clarity in the audit log.
        payment.status = PaymentStatus.EXPIRED
        await session.commit()
        await session.refresh(payment)
    return payment


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


__all__ = (
    "BalanceSummary",
    "BillingError",
    "InvoiceAlreadyIssuedError",
    "InvoiceDraft",
    "InvoiceLineItem",
    "InvoiceNotFoundError",
    "InvalidBillingPeriodError",
    "PlanNotFoundError",
    "apply_flow_status",
    "compute_invoice",
    "create_payment",
    "finalize_invoice",
    "get_balance",
    "get_invoice",
    "get_plan_by_code",
    "list_invoices",
    "list_payments",
    "list_plans",
    "persist_invoice_draft",
    "refresh_payment_status",
    "switch_subscription",
)
