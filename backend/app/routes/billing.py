"""Billing & payment routes.

Implements the public surface the PRD documents for the
billing / Flow / DTE integration:

- ``GET  /v1/billing/plans`` – list the public plan
  catalog.
- ``POST /v1/billing/subscriptions`` – switch the
  authenticated client to a different plan.
- ``GET  /v1/billing/balance`` – headline counters for
  the dashboard ("X of Y messages this month").
- ``POST /v1/billing/invoices`` – compute + issue a new
  monthly invoice for the authenticated client. The
  request carries the target period (defaults to the
  current month).
- ``GET  /v1/billing/invoices`` – invoice history.
- ``GET  /v1/billing/invoices/{id}`` – single invoice
  detail.
- ``POST /v1/billing/invoices/{id}/pay`` – mint a Flow
  payment order; the response carries the redirect URL
  the dashboard sends the customer to.
- ``GET  /v1/billing/payments`` – payment history.
- ``GET  /v1/billing/payments/{id}`` – poll Flow for the
  current status of a payment.
- ``POST /v1/billing/webhook/flow`` – Flow's asynchronous
  ``payment/confirm`` webhook. Validates the payload,
  reconciles the local :class:`Payment` row and flips
  the matching invoice to ``paid`` if appropriate.

All endpoints (except the Flow webhook) require a valid
``X-API-Key`` header – the
:func:`app.routes.auth.require_api_key` dependency is the
single source of truth. The webhook is open: Flow
authenticates itself through the signed payload, not
through the API key, so the route cannot use the
``require_api_key`` dependency.
"""

from __future__ import annotations

import itertools
import threading
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.flow import FlowClient, FlowError
from app.db import get_db
from app.models.client import Client
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentStatus
from app.models.plan import Plan, PlanBillingPeriod
from app.routes.auth import require_api_key
from app.services.billing import (
    BalanceSummary,
    BillingError,
    apply_flow_status,
    compute_invoice,
    create_payment,
    finalize_invoice,
    get_balance,
    get_invoice,
    list_invoices,
    list_payments,
    list_plans,
    persist_invoice_draft,
    refresh_payment_status,
    switch_subscription,
)
from app.services.dte import DteService

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PlanResponse(BaseModel):
    """Public projection of a :class:`Plan` row."""

    code: str
    name: str
    description: str | None
    price_clp: int
    msg_limit: int | None
    extra_msg_price: int | None
    billing_period: PlanBillingPeriod


class SubscriptionRequest(BaseModel):
    """Body of ``POST /v1/billing/subscriptions``."""

    plan_code: str = Field(..., min_length=1, max_length=32)


class SubscriptionResponse(BaseModel):
    """Response of a successful plan switch."""

    plan: PlanResponse
    switched_at: datetime


class BalanceResponse(BaseModel):
    """Projection of a :class:`BalanceSummary` for the API."""

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


class ComputeInvoiceRequest(BaseModel):
    """Body of ``POST /v1/billing/invoices``."""

    period: date | None = Field(default=None, description="First day of the target month")


class InvoiceLineItemResponse(BaseModel):
    description: str
    quantity: int
    unit_price_clp: int
    total_clp: int


class InvoiceResponse(BaseModel):
    """Projection of an :class:`Invoice` row for the API."""

    id: str
    number: str
    plan_code: str
    period_start: date
    period_end: date
    issue_date: date
    due_date: date
    total_msgs: int
    included_msgs: int
    overage_msgs: int
    subtotal_clp: int
    iva_clp: int
    total_clp: int
    status: InvoiceStatus
    dte_number: int | None
    dte_url: str | None
    flow_invoice_id: str | None
    paid_at: datetime | None


class IssueInvoiceResponse(BaseModel):
    """Response of a successful ``POST /v1/billing/invoices``."""

    invoice: InvoiceResponse
    dte_number: int
    dte_url: str


class PaymentResponse(BaseModel):
    """Projection of a :class:`Payment` row for the API."""

    id: str
    invoice_id: str | None
    plan_code: str
    amount_clp: int
    flow_token: str
    flow_order: str
    flow_redirect_url: str | None
    status: PaymentStatus
    created_at: datetime
    confirmed_at: datetime | None


class CreatePaymentResponse(BaseModel):
    """Response of a successful ``POST /v1/billing/invoices/{id}/pay``."""

    payment: PaymentResponse
    redirect_url: str


class FlowWebhookRequest(BaseModel):
    """Payload Flow POSTs to the asynchronous ``payment/confirm`` endpoint.

    The real Flow integration signs the payload with the
    merchant's secret key; the MVP accepts the unsigned
    payload and trusts the IP allow-list at the load
    balancer. A future "signed webhook" hardening is
    tracked in the follow-up list.
    """

    token: str
    status: int = Field(..., description="Flow's numeric status (1=pending, 2=paid, ...)")
    payment_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_response(plan: Plan) -> PlanResponse:
    return PlanResponse(
        code=plan.code,
        name=plan.name,
        description=plan.description,
        price_clp=plan.price_clp,
        msg_limit=plan.msg_limit,
        extra_msg_price=plan.extra_msg_price,
        billing_period=plan.billing_period,
    )


def _invoice_response(invoice: Invoice) -> InvoiceResponse:
    return InvoiceResponse(
        id=invoice.id,
        number=invoice.number,
        plan_code=invoice.plan_code,
        period_start=invoice.period_start,
        period_end=invoice.period_end,
        issue_date=invoice.issue_date,
        due_date=invoice.due_date,
        total_msgs=invoice.total_msgs,
        included_msgs=invoice.included_msgs,
        overage_msgs=invoice.overage_msgs,
        subtotal_clp=invoice.subtotal_clp,
        iva_clp=invoice.iva_clp,
        total_clp=invoice.total_clp,
        status=invoice.status,
        dte_number=invoice.dte_number,
        dte_url=invoice.dte_url,
        flow_invoice_id=invoice.flow_invoice_id,
        paid_at=invoice.paid_at,
    )


def _payment_response(payment: Payment) -> PaymentResponse:
    return PaymentResponse(
        id=payment.id,
        invoice_id=payment.invoice_id,
        plan_code=payment.plan_code,
        amount_clp=payment.amount_clp,
        flow_token=payment.flow_token,
        flow_order=payment.flow_order,
        flow_redirect_url=payment.flow_redirect_url,
        status=payment.status,
        created_at=payment.created_at,
        confirmed_at=payment.confirmed_at,
    )


def _balance_response(summary: BalanceSummary) -> BalanceResponse:
    return BalanceResponse(
        plan_code=summary.plan_code,
        plan_name=summary.plan_name,
        period_start=summary.period_start,
        period_end=summary.period_end,
        msg_limit=summary.msg_limit,
        used_msgs=summary.used_msgs,
        billable_msgs=summary.billable_msgs,
        overage_msgs=summary.overage_msgs,
        overage_cost_clp=summary.overage_cost_clp,
        estimated_total_clp=summary.estimated_total_clp,
    )


def _raise_billing_error(exc: BillingError) -> None:
    """Convert a :class:`BillingError` into the matching HTTPException.

    Mirrors the pattern in :mod:`app.routes.auth` so the
    caller does not have to know which HTTP status each
    domain error maps to.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


def _raise_flow_error(exc: FlowError) -> None:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# Plans / subscriptions
# ---------------------------------------------------------------------------


@router.get(
    "/plans",
    response_model=list[PlanResponse],
    responses={200: {"description": "Active plans the customer can subscribe to."}},
)
async def list_plans_endpoint(
    session: AsyncSession = Depends(get_db),
) -> list[PlanResponse]:
    """List the public plan catalog."""
    plans = await list_plans(session)
    return [_plan_response(plan) for plan in plans]


@router.post(
    "/subscriptions",
    response_model=SubscriptionResponse,
    responses={
        200: {"description": "Subscription switched to the requested plan."},
        404: {"description": "The plan code does not exist or is no longer active."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def switch_subscription_endpoint(
    payload: SubscriptionRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    """Switch the authenticated client to a different plan."""
    try:
        plan = await switch_subscription(
            session,
            client=current_client,
            plan_code=payload.plan_code,
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    return SubscriptionResponse(plan=_plan_response(plan), switched_at=datetime.utcnow())


@router.get(
    "/balance",
    response_model=BalanceResponse,
    responses={
        200: {"description": "Headline counters for the current billing period."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def get_balance_endpoint(
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> BalanceResponse:
    """Return the live usage counters for the current period."""
    summary = await get_balance(session, client=current_client)
    return _balance_response(summary)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@router.get(
    "/invoices",
    response_model=list[InvoiceResponse],
    responses={200: {"description": "Invoice history, newest first."}},
)
async def list_invoices_endpoint(
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> list[InvoiceResponse]:
    """List the authenticated customer's invoices."""
    invoices = await list_invoices(session, client=current_client)
    return [_invoice_response(invoice) for invoice in invoices]


@router.post(
    "/invoices",
    response_model=IssueInvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Invoice computed and issued; DTE emitted."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The requested period is not a valid calendar month."},
    },
)
async def create_invoice_endpoint(
    payload: ComputeInvoiceRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> IssueInvoiceResponse:
    """Compute and issue a monthly invoice for the authenticated client.

    The endpoint is the customer-facing entry point for
    the "generar factura del mes" button on the
    dashboard. It computes the usage, persists the
    draft, asks the DTE service to emit the document,
    and returns the issued invoice plus the DTE
    metadata.
    """
    try:
        draft = await compute_invoice(
            session,
            client=current_client,
            period=payload.period,
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    invoice = await persist_invoice_draft(session, draft=draft)
    folio_provider = _build_folio_provider(session)
    dte_service = DteService(folio_provider=folio_provider)
    invoice = await finalize_invoice(
        session,
        invoice=invoice,
        dte_service=dte_service,
    )
    return IssueInvoiceResponse(
        invoice=_invoice_response(invoice),
        dte_number=invoice.dte_number or 0,
        dte_url=invoice.dte_url or "",
    )


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    responses={
        200: {"description": "Invoice detail."},
        404: {"description": "Invoice does not exist for the authenticated client."},
    },
)
async def get_invoice_endpoint(
    invoice_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> InvoiceResponse:
    """Fetch a single invoice, ensuring the caller owns it."""
    try:
        invoice = await get_invoice(
            session,
            client=current_client,
            invoice_id=invoice_id,
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    return _invoice_response(invoice)


@router.post(
    "/invoices/{invoice_id}/pay",
    response_model=CreatePaymentResponse,
    responses={
        200: {"description": "Payment order created; redirect the customer to ``redirect_url``."},
        404: {"description": "Invoice does not exist for the authenticated client."},
        409: {"description": "Invoice is in a non-payable status."},
        502: {"description": "Flow rejected the request or is unreachable."},
    },
)
async def pay_invoice_endpoint(
    invoice_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> CreatePaymentResponse:
    """Create a Flow order for an invoice and return the redirect URL.

    The dashboard polls
    :func:`refresh_payment_status_endpoint` after the
    Flow checkout completes (or relies on the
    asynchronous ``payment/confirm`` webhook) to learn
    the final status.
    """
    try:
        invoice = await get_invoice(
            session,
            client=current_client,
            invoice_id=invoice_id,
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    try:
        payment = await create_payment(
            session,
            client=current_client,
            invoice=invoice,
            flow_client=FlowClient(),
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    except FlowError as exc:
        _raise_flow_error(exc)
    redirect_url = payment.flow_redirect_url or ""
    return CreatePaymentResponse(
        payment=_payment_response(payment),
        redirect_url=redirect_url,
    )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@router.get(
    "/payments",
    response_model=list[PaymentResponse],
    responses={200: {"description": "Payment history, newest first."}},
)
async def list_payments_endpoint(
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> list[PaymentResponse]:
    """List the authenticated customer's payment attempts."""
    payments = await list_payments(session, client=current_client)
    return [_payment_response(payment) for payment in payments]


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentResponse,
    responses={
        200: {"description": "Payment detail (refreshed from Flow if still pending)."},
        404: {"description": "Payment does not exist for the authenticated client."},
        502: {"description": "Flow is unreachable."},
    },
)
async def refresh_payment_status_endpoint(
    payment_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    """Poll Flow for the current status of a previously created payment."""
    try:
        payment = await refresh_payment_status(
            session,
            client=current_client,
            payment_id=payment_id,
            flow_client=FlowClient(),
        )
    except BillingError as exc:
        _raise_billing_error(exc)
    except FlowError as exc:
        _raise_flow_error(exc)
    return _payment_response(payment)


# ---------------------------------------------------------------------------
# Flow webhook
# ---------------------------------------------------------------------------


@router.post(
    "/webhook/flow",
    response_model=PaymentResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Webhook accepted; payment reconciled."},
        404: {"description": "The flow_token does not match a known payment."},
    },
    include_in_schema=True,
)
async def flow_webhook(
    payload: FlowWebhookRequest,
    session: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    """Reconcile an asynchronous ``payment/confirm`` notification from Flow.

    The endpoint is intentionally **not** guarded by
    :func:`require_api_key`: Flow does not send the
    ``X-API-Key`` header, it signs the request body
    with the merchant secret. The MVP trusts the
    payload verbatim; the next hardening pass will
    validate the signature.
    """
    from sqlalchemy import select

    from app.adapters.flow import FlowPaymentStatus

    stmt = select(Payment).where(Payment.flow_token == payload.token)
    result = await session.execute(stmt)
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "payment_not_found",
                "message": "flow_token does not match a known payment",
            },
        )
    flow_status = FlowPaymentStatus(
        status=payload.status,
        payment_id=payload.payment_id,
        raw_json="",
    )
    payment = await apply_flow_status(
        session,
        payment=payment,
        flow_status=flow_status,
    )
    return _payment_response(payment)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Process-wide folio counter. The MVP ships a single
# counter; the production implementation will back it
# with a row in a ``dte_folios`` table that uses
# ``SELECT ... FOR UPDATE`` so concurrent cron jobs
# never allocate the same folio.
_folio_counter: itertools.count | None = None
_folio_lock = threading.Lock()


def _next_folio() -> int:
    """Allocate the next SII folio number for the running process.

    The base value is derived from the current time so
    two emissions on different days sit on different
    ranges. The ``itertools.count`` instance is
    initialised lazily so the first call also picks up
    the timestamp.
    """
    global _folio_counter
    with _folio_lock:
        if _folio_counter is None:
            base = int(datetime.utcnow().strftime("%y%m%d%H%M%S"))
            _folio_counter = itertools.count(base + 1)
        return next(_folio_counter)


def _build_folio_provider(session: AsyncSession):
    """Return a callable that allocates the next DTE folio.

    Thin wrapper around :func:`_next_folio` that
    preserves the function signature
    :class:`app.services.dte.DteService` expects. The
    closure makes the dependency injectable – unit
    tests can supply their own counter.
    """
    return _next_folio
