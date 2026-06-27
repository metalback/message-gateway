"""Admin endpoints (issue #10).

Implements the ``/v1/admin/*`` surface the platform-operator
dashboard consumes:

- ``GET  /v1/admin/clients``                – paginated,
  filterable read of every client.
- ``POST /v1/admin/clients``                – create a new
  customer on behalf of the operator. The plain API key
  is returned **once** in the response, mirroring
  :func:`app.routes.auth.register`.
- ``GET  /v1/admin/clients/{id}``           – fetch a single
  client (used by the "ver detalle" drawer on the
  dashboard).
- ``PATCH /v1/admin/clients/{id}``          – update
  ``name`` / ``plan`` / ``status`` /
  ``markup_percent`` / ``markup_fixed_clp``. Each field is
  optional; ``None`` is a no-op for that field.
- ``POST /v1/admin/clients/{id}/suspend``    – flip a
  client to :attr:`ClientStatus.SUSPENDED`. Idempotent.
- ``GET  /v1/admin/stats/overview``         – aggregate
  counters for the overview card.
- ``GET  /v1/admin/stats/by-provider``      – per-provider
  breakdown for the "desglose por proveedor" card.
- ``GET  /v1/admin/logs``                   – paginated
  read of the most recent failed messages.

All endpoints are guarded by :func:`require_admin`, which
verifies that the ``X-API-Key`` header resolves to a
:class:`Client` whose :attr:`Client.role` is
:attr:`ClientRole.ADMIN`. The dependency is the single
source of truth for admin authorisation: a future endpoint
that needs admin access simply takes ``Depends(require_admin)``
in its signature.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client, ClientPlan, ClientRole, ClientStatus
from app.routes.auth import require_api_key
from app.services import admin as admin_service
from app.services.admin import (
    AdminError,
    AdminOverview,
    ClientListPage,
    ProviderBreakdownRow,
)
from app.services.auth import AuthError

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AdminClientResponse(BaseModel):
    """Projection of a :class:`Client` row for the admin surface.

    The shape intentionally includes the auth-related fields
    (api_key_last4, role, markup) the operator needs to
    render the table, but never the bcrypt digests. The
    fields the regular ``ClientSummary`` (used by
    ``/v1/auth/login``) omits are added here so the
    dashboard does not have to issue a second round-trip
    to render the role / markup badges.
    """

    id: str
    name: str
    email: EmailStr
    rut: str
    plan: ClientPlan
    status: ClientStatus
    role: ClientRole
    api_key_last4: str
    markup_percent: float
    markup_fixed_clp: int
    created_at: datetime
    updated_at: datetime | None


class AdminClientListResponse(BaseModel):
    """Envelope for the paginated ``GET /v1/admin/clients`` response."""

    items: list[AdminClientResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class CreateClientRequest(BaseModel):
    """Body of ``POST /v1/admin/clients``.

    The shape is a strict superset of
    :class:`app.routes.auth.RegisterRequest`; the same
    validation (Pydantic for email, service layer for RUT
    / password) applies. ``plan`` defaults to ``starter``;
    the dashboard's "create client" form lets the operator
    pick from the public catalog.
    """

    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    rut: str = Field(..., min_length=1, max_length=20)
    password: str = Field(..., min_length=1, max_length=255)
    plan: ClientPlan | None = None


class CreateClientResponse(BaseModel):
    """Response of a successful ``POST /v1/admin/clients``.

    The plain API key is returned **once** – same contract
    as the public ``POST /v1/auth/register`` endpoint. The
    platform never stores the clear-text key, so the
    operator is expected to surface it to the new
    customer's onboarding flow before navigating away.
    """

    client: AdminClientResponse
    api_key: str
    api_key_last4: str


class UpdateClientRequest(BaseModel):
    """Body of ``PATCH /v1/admin/clients/{id}``.

    Every field is optional; ``None`` is a no-op for that
    field (the service uses an explicit ``is not None``
    check, so a falsy but valid value such as
    ``markup_percent=0.0`` is accepted).
    """

    name: str | None = Field(default=None, max_length=200)
    plan: ClientPlan | None = None
    status: ClientStatus | None = None
    markup_percent: float | None = Field(default=None, ge=0.0, le=10.0)
    markup_fixed_clp: int | None = Field(default=None, ge=0, le=1_000_000)


class SuspendClientResponse(BaseModel):
    """Response of a successful ``POST /v1/admin/clients/{id}/suspend``."""

    client: AdminClientResponse
    suspended_at: datetime


class AdminOverviewResponse(BaseModel):
    """Projection of :class:`AdminOverview` for the API."""

    period_start: date
    period_end: date
    total_clients: int
    active_clients: int
    suspended_clients: int
    pending_clients: int
    admin_users: int
    total_messages: int
    billable_messages: int
    delivered_messages: int
    failed_messages: int
    pending_messages: int
    total_revenue_clp: int


class ProviderBreakdownResponse(BaseModel):
    """Projection of :class:`ProviderBreakdownRow` for the API."""

    provider: str
    channel: str
    total: int
    delivered: int
    failed: int
    pending: int
    cost_clp: int
    fee_clp: int


class ErrorLogResponse(BaseModel):
    """Projection of :class:`ErrorLogEntry` for the API."""

    message_id: str
    client_id: str
    client_name: str
    client_email: EmailStr
    channel: str
    to_number: str
    provider: str
    error_code: str | None
    error_message: str | None
    created_at: datetime


class ErrorLogListResponse(BaseModel):
    """Envelope for the paginated ``GET /v1/admin/logs`` response."""

    items: list[ErrorLogResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(client: Client) -> AdminClientResponse:
    """Project a :class:`Client` row to the admin response shape."""
    return AdminClientResponse(
        id=client.id,
        name=client.name,
        email=client.email,
        rut=client.rut,
        plan=client.plan,
        status=client.status,
        role=client.role,
        api_key_last4=client.api_key_last4,
        markup_percent=client.markup_percent,
        markup_fixed_clp=client.markup_fixed_clp,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def _raise_admin_error(exc: AdminError) -> None:
    """Convert an :class:`AdminError` into the matching HTTPException.

    Mirrors the patterns in :mod:`app.routes.auth` and
    :mod:`app.routes.billing` so every route module uses
    the same exception-to-HTTP mapping.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def require_admin(
    current_client: Client = Depends(require_api_key),
) -> Client:
    """FastAPI dependency that returns the authenticated admin.

    Composition over inheritance: the function delegates
    the API-key validation to
    :func:`app.routes.auth.require_api_key` and then
    enforces the ``role == admin`` invariant. A non-admin
    caller gets a ``403`` with a stable error code; a
    missing / invalid API key gets the ``401`` from the
    upstream dependency.
    """
    if current_client.role != ClientRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_required",
                "message": "this endpoint requires an admin account",
            },
        )
    return current_client


# ---------------------------------------------------------------------------
# Client CRUD
# ---------------------------------------------------------------------------


@router.get(
    "/clients",
    response_model=AdminClientListResponse,
    responses={
        200: {"description": "One page of the admin clients table."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
    },
)
async def list_clients_endpoint(
    q: str | None = Query(
        default=None,
        description="Substring match against name, email or RUT (case-insensitive).",
    ),
    plan: ClientPlan | None = Query(default=None),
    status_filter: ClientStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=admin_service.DEFAULT_LIST_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminClientListResponse:
    """List every client (paginated, filterable)."""
    try:
        page: ClientListPage = await admin_service.list_clients(
            session,
            search=q,
            plan=plan,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
    except AdminError as exc:
        _raise_admin_error(exc)
    return AdminClientListResponse(
        items=[_to_response(client) for client in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.post(
    "/clients",
    response_model=CreateClientResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Client created; ``api_key`` is shown only once."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
        409: {"description": "A client with the same email or RUT already exists."},
        422: {"description": "One or more fields failed validation."},
    },
)
async def create_client_endpoint(
    payload: CreateClientRequest,
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> CreateClientResponse:
    """Create a new client and return the plain API key once.

    The endpoint reuses the registration helpers from
    :mod:`app.services.auth` so the bcrypt digests, RUT
    validation and uniqueness checks are shared with the
    public registration path.
    """
    try:
        client, api_key = await admin_service.create_client(
            session,
            name=payload.name,
            email=payload.email,
            rut=payload.rut,
            password=payload.password,
            plan=payload.plan or ClientPlan.STARTER,
        )
    except AdminError as exc:
        _raise_admin_error(exc)
    except AuthError as exc:
        # The :func:`app.services.auth.register_client` call
        # raises :class:`AuthError` subclasses (e.g.
        # :class:`DuplicateIdentityError`,
        # :class:`InvalidInputError`) for problems the
        # admin route does not have a domain mapping for.
        # We translate the exception using the same
        # helper the public ``POST /v1/auth/register``
        # route uses so the wire contract is identical.
        from app.routes.auth import _raise_auth_error

        _raise_auth_error(exc)
    return CreateClientResponse(
        client=_to_response(client),
        api_key=api_key,
        api_key_last4=client.api_key_last4,
    )


@router.get(
    "/clients/{client_id}",
    response_model=AdminClientResponse,
    responses={
        200: {"description": "The client."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
        404: {"description": "The client does not exist."},
    },
)
async def get_client_endpoint(
    client_id: str,
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminClientResponse:
    """Fetch a single client by id."""
    try:
        client = await admin_service.get_client(session, client_id=client_id)
    except AdminError as exc:
        _raise_admin_error(exc)
    return _to_response(client)


@router.patch(
    "/clients/{client_id}",
    response_model=AdminClientResponse,
    responses={
        200: {"description": "Client updated."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
        404: {"description": "The client does not exist."},
        422: {"description": "One or more fields failed validation."},
    },
)
async def update_client_endpoint(
    client_id: str,
    payload: UpdateClientRequest,
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminClientResponse:
    """Update the mutable fields of a single client.

    See :func:`app.services.admin.update_client` for the
    field-level semantics. The endpoint is the PATCH-style
    equivalent of the public ``POST /v1/billing/subscriptions``
    plan switcher.
    """
    try:
        client = await admin_service.update_client(
            session,
            client_id=client_id,
            name=payload.name,
            plan=payload.plan,
            status=payload.status,
            markup_percent=payload.markup_percent,
            markup_fixed_clp=payload.markup_fixed_clp,
        )
    except AdminError as exc:
        _raise_admin_error(exc)
    return _to_response(client)


@router.post(
    "/clients/{client_id}/suspend",
    response_model=SuspendClientResponse,
    responses={
        200: {"description": "Client suspended (idempotent)."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
        404: {"description": "The client does not exist."},
    },
)
async def suspend_client_endpoint(
    client_id: str,
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> SuspendClientResponse:
    """Flip a client to :attr:`ClientStatus.SUSPENDED`."""
    try:
        client = await admin_service.suspend_client(session, client_id=client_id)
    except AdminError as exc:
        _raise_admin_error(exc)
    return SuspendClientResponse(client=_to_response(client), suspended_at=datetime.utcnow())


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get(
    "/stats/overview",
    response_model=AdminOverviewResponse,
    responses={
        200: {"description": "Aggregate counters for the admin overview card."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
    },
)
async def admin_overview_endpoint(
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> AdminOverviewResponse:
    """Return the aggregate counters for the admin overview."""
    overview: AdminOverview = await admin_service.admin_overview(session)
    return AdminOverviewResponse(
        period_start=overview.period_start,
        period_end=overview.period_end,
        total_clients=overview.total_clients,
        active_clients=overview.active_clients,
        suspended_clients=overview.suspended_clients,
        pending_clients=overview.pending_clients,
        admin_users=overview.admin_users,
        total_messages=overview.total_messages,
        billable_messages=overview.billable_messages,
        delivered_messages=overview.delivered_messages,
        failed_messages=overview.failed_messages,
        pending_messages=overview.pending_messages,
        total_revenue_clp=overview.total_revenue_clp,
    )


@router.get(
    "/stats/by-provider",
    response_model=list[ProviderBreakdownResponse],
    responses={
        200: {"description": "Per-provider aggregates for the current period."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
    },
)
async def admin_provider_breakdown_endpoint(
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> list[ProviderBreakdownResponse]:
    """Return the per-(provider, channel) aggregates for the current period."""
    rows: Sequence[ProviderBreakdownRow] = await admin_service.admin_provider_breakdown(session)
    return [
        ProviderBreakdownResponse(
            provider=row.provider,
            channel=row.channel,
            total=row.total,
            delivered=row.delivered,
            failed=row.failed,
            pending=row.pending,
            cost_clp=row.cost_clp,
            fee_clp=row.fee_clp,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------


@router.get(
    "/logs",
    response_model=ErrorLogListResponse,
    responses={
        200: {"description": "Most recent failed messages, newest first."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        403: {"description": "The caller is not an admin."},
    },
)
async def admin_logs_endpoint(
    limit: int = Query(default=admin_service.DEFAULT_ERROR_LOG_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    _admin: Client = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> ErrorLogListResponse:
    """Return the most recent failed messages (paginated)."""
    try:
        items, total = await admin_service.list_recent_errors(
            session, limit=limit, offset=offset
        )
    except AdminError as exc:
        _raise_admin_error(exc)
    has_more = (offset + len(items)) < total
    return ErrorLogListResponse(
        items=[
            ErrorLogResponse(
                message_id=entry.message_id,
                client_id=entry.client_id,
                client_name=entry.client_name,
                client_email=entry.client_email,
                channel=entry.channel,
                to_number=entry.to_number,
                provider=entry.provider,
                error_code=entry.error_code,
                error_message=entry.error_message,
                created_at=entry.created_at,
            )
            for entry in items
        ],
        total=total,
        limit=min(limit, admin_service.DEFAULT_ERROR_LOG_LIMIT * 5),
        offset=offset,
        has_more=has_more,
    )
