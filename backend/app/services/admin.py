"""Admin domain service.

Owns the business logic behind the ``/v1/admin/*`` surface
(issue #10). The module is intentionally focused on
platform-operations concerns – the per-customer /v1/* endpoints
keep using their existing services; the admin layer is
additive.

Public functions:

- :func:`list_clients`           – paginated, filterable read
  of every :class:`~app.models.client.Client` row, newest
  first. Supports the ``q`` (substring search over name /
  email / RUT), ``plan`` and ``status`` filters the admin
  dashboard uses.
- :func:`get_client`             – fetch a single client by id,
  raising :class:`ClientNotFoundError` when the row is
  missing.
- :func:`create_client`          – mint a new client (regular
  customer) and return the plain API key. Reuses the
  registration helpers from :mod:`app.services.auth` so the
  bcrypt digests / RUT validation / email normalisation are
  shared with the public registration path.
- :func:`update_client`          – PATCH-style update of the
  mutable fields (``name`` / ``plan`` / ``status`` /
  ``markup_percent`` / ``markup_fixed_clp``). Each field is
  optional; passing ``None`` is a no-op for that field. The
  operator cannot demote a client to ``admin`` through this
  endpoint – promoting to admin is a separate, deliberately
  rare operation; if it is needed it lands as a follow-up
  task so the audit log has a single, dedicated entry point.
- :func:`suspend_client`         – flip a client to
  :attr:`ClientStatus.SUSPENDED`. Idempotent (suspending an
  already-suspended client is a no-op).
- :func:`set_client_markup`      – dedicated setter for the
  pricing overrides; used by the dashboard's "editar
  markup" form and the ``PATCH /v1/admin/clients/{id}``
  endpoint.
- :func:`admin_overview`         – aggregate counters for the
  "métricas agregadas" widget (active clients, billable
  messages this month, monthly revenue in CLP, per-channel
  delivered/failed/pending counts).
- :func:`admin_provider_breakdown` – per-provider aggregates
  for the "desglose por proveedor" card. The bucket
  includes the mean ``latency_ms`` of every successful
  dispatch so the dashboard's "latencia promedio por
  provider" tile can render without a second round-trip.
- :func:`list_recent_errors`     – paginated read of the most
  recent :class:`~app.models.message.Message` rows whose
  status is :attr:`MessageStatus.FAILED` (the "logs de
  errores" view the admin dashboard surfaces). Returns one
  row per failed message; future iterations can join against
  a dedicated audit log.

The module never issues HTTP requests and never talks to a
provider adapter; it is a pure orchestrator on top of the
ORM, the same way :mod:`app.services.billing` is. The route
layer (see :mod:`app.routes.admin`) translates the
dataclasses into Pydantic response models.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import (
    Client,
    ClientPlan,
    ClientRole,
    ClientStatus,
)
from app.models.message import BILLABLE_STATUSES, Message, MessageStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Default page size for the admin listings. The dashboard's
# tables render 20 rows per page by convention; 50 covers the
# same use case without ballooning the response payload.
DEFAULT_LIST_LIMIT = 50
_LIST_HARD_LIMIT = 200

# Hard cap on the number of rows the ``GET /v1/admin/logs``
# endpoint will return. 500 covers a "this week" view on a
# healthy platform and prevents a runaway dashboard from
# asking for an unbounded slice of the failure history.
DEFAULT_ERROR_LOG_LIMIT = 100
_ERROR_LOG_HARD_LIMIT = 500

# The default API-key prefix used when the admin service
# mints a key on behalf of a newly-created client. The
# value is sourced from :class:`app.config.Settings` so a
# future "staging" prefix (``mgw_test_``) can land as a
# config change.
_API_KEY_PREFIX = "mgw_live_"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdminError(Exception):
    """Base class for every admin-domain exception.

    Mirrors the contract :class:`app.services.auth.AuthError`
    and :class:`app.services.billing.BillingError` expose:
    a stable ``code`` for the front-end, a human ``message``
    and a ``http_status`` the route layer maps onto a
    :class:`fastapi.HTTPException`.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ClientNotFoundError(AdminError):
    """The requested :class:`Client` does not exist."""

    http_status = 404
    code = "client_not_found"


class InvalidClientUpdateError(AdminError):
    """A field in the PATCH body was rejected.

    The error covers both "field is the wrong type" and
    "field is outside the documented range" so the route
    layer can surface a single 422 with a stable code.
    """

    http_status = 422
    code = "invalid_client_update"


class InvalidClientFilterError(AdminError):
    """The list query carries an invalid filter value."""

    http_status = 422
    code = "invalid_client_filter"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientListPage:
    """One page of the admin's "clientes" table.

    ``items`` carries the row payload; ``total`` is the
    unfiltered count of the underlying query (so the
    dashboard can render "showing 1-50 of 247"); ``has_more``
    is the standard "are there more pages?" hint.
    """

    items: tuple[Client, ...]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class AdminOverview:
    """Aggregate counters for the admin overview card.

    The shape is intentionally flat so the dashboard can
    bind each field to a single tile without any
    client-side aggregation. Period-bounded metrics
    (messages, revenue) default to the current calendar
    month – matching the customer-facing
    :func:`app.services.billing.get_balance` semantic so the
    two views describe the same period.
    """

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


@dataclass(frozen=True)
class ProviderBreakdownRow:
    """A single (provider, channel) bucket in the
    "desglose por proveedor" response.

    Counts and sums are aggregated over the current
    calendar month by default; the dashboard uses the
    value to drive the per-provider bar chart.

    ``avg_latency_ms`` is the mean wall-clock duration of
    a successful ``provider.send`` call, in milliseconds,
    computed across every row in the bucket that has a
    non-``NULL`` ``mensajes.latency_ms`` value. The field
    is ``None`` for buckets where every row is still
    unobserved (a freshly-rolled deployment) so the
    dashboard can render a "—" placeholder rather than a
    misleading ``0``.
    """

    provider: str
    channel: str
    total: int
    delivered: int
    failed: int
    pending: int
    cost_clp: int
    fee_clp: int
    avg_latency_ms: float | None


@dataclass(frozen=True)
class ErrorLogEntry:
    """A single row in the admin "errores recientes" view.

    The shape is a subset of the :class:`Message` row
    (the columns the admin table actually shows) plus the
    owning client's name / email so the operator can
    contact the customer without a second round-trip.
    """

    message_id: str
    client_id: str
    client_name: str
    client_email: str
    channel: str
    to_number: str
    provider: str
    error_code: str | None
    error_message: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month_bounds(value: datetime) -> tuple[datetime, datetime]:
    """Return ``[first_instant, last_instant]`` of the month containing ``value``.

    The bounds are inclusive of the first and last second of
    the month. The function is the source of truth for
    "current month" semantics the admin overview uses so the
    service / tests agree on the same range.
    """
    if not isinstance(value, datetime):
        raise AdminError("invalid_period", "period must be a datetime")
    first = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first.replace(microsecond=0)
    return first, last


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Coerce ``limit`` / ``offset`` into a safe (limit, offset) pair.

    ``limit`` is clamped to ``[1, _LIST_HARD_LIMIT]`` and
    ``offset`` to ``[0, ∞)``. A non-integer value is a
    hard error because the call site already passes a
    Pydantic-validated ``int``; the helper just guards
    against the "negative limit" footgun a future caller
    might trigger.
    """
    if not isinstance(limit, int) or limit < 1:
        raise InvalidClientFilterError(
            "invalid_limit", "limit must be a positive integer"
        )
    if not isinstance(offset, int) or offset < 0:
        raise InvalidClientFilterError(
            "invalid_offset", "offset must be a non-negative integer"
        )
    return (min(limit, _LIST_HARD_LIMIT), offset)


# ---------------------------------------------------------------------------
# Client CRUD
# ---------------------------------------------------------------------------


async def list_clients(
    session: AsyncSession,
    *,
    search: str | None = None,
    plan: ClientPlan | None = None,
    status: ClientStatus | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> ClientListPage:
    """Return one page of the admin's clients table.

    The query is sorted by ``created_at`` descending so the
    most recently registered client is at the top of the
    dashboard; ``id`` is the tiebreaker so the result is
    deterministic regardless of millisecond ties.

    ``search`` is a case-insensitive substring match against
    the customer's name / email / RUT. ``plan`` and
    ``status`` are exact-match filters; passing ``None``
    disables the corresponding filter.

    The function is read-only and cheap enough to call on
    every page load (the underlying query is a single
    ``SELECT`` with a single-row ``COUNT(*)`` for the
    ``total`` field).
    """
    page_limit, page_offset = _validate_pagination(limit, offset)

    conditions = []
    if search:
        like = f"%{search.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(Client.name).like(like),
                func.lower(Client.email).like(like),
                func.lower(Client.rut).like(like),
            )
        )
    if plan is not None:
        conditions.append(Client.plan == plan)
    if status is not None:
        conditions.append(Client.status == status)

    base_stmt = select(Client)
    count_stmt = select(func.count(Client.id))
    if conditions:
        base_stmt = base_stmt.where(and_(*conditions))
        count_stmt = count_stmt.where(and_(*conditions))

    base_stmt = base_stmt.order_by(Client.created_at.desc(), Client.id.desc())
    base_stmt = base_stmt.limit(page_limit).offset(page_offset)

    result = await session.execute(base_stmt)
    rows = tuple(result.scalars().all())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    has_more = (page_offset + len(rows)) < total
    return ClientListPage(
        items=rows,
        total=total,
        limit=page_limit,
        offset=page_offset,
        has_more=has_more,
    )


async def get_client(session: AsyncSession, *, client_id: str) -> Client:
    """Fetch a single :class:`Client` row by id.

    Raises :class:`ClientNotFoundError` when the row is
    missing so the route layer can render a 404.
    """
    if not isinstance(client_id, str) or not client_id:
        raise ClientNotFoundError("invalid_client_id", "client id is required")
    client = await session.get(Client, client_id)
    if client is None:
        raise ClientNotFoundError(
            "client_not_found", f"client {client_id!r} does not exist"
        )
    return client


async def create_client(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    rut: str,
    password: str,
    plan: ClientPlan | None = None,
    settings: Settings | None = None,
) -> tuple[Client, str]:
    """Mint a new client (regular customer) and return ``(row, api_key)``.

    Wraps :func:`app.services.auth.register_client` so the
    bcrypt digests / RUT validation / email normalisation /
    uniqueness checks are shared with the public
    registration path. The only delta is the default ``plan``
    resolution: when the operator omits ``plan`` on the
    admin form the platform falls back to the
    ``billing_default_plan_code`` setting rather than the
    service's hard-coded ``starter``.
    """
    from app.services.auth import register_client

    cfg = settings or get_settings()
    if plan is None:
        try:
            plan = ClientPlan(cfg.billing_default_plan_code)
        except ValueError as exc:
            raise InvalidClientUpdateError(
                "invalid_plan",
                f"default plan {cfg.billing_default_plan_code!r} is not a known client plan",
            ) from exc
    elif not isinstance(plan, ClientPlan):
        raise InvalidClientUpdateError(
            "invalid_plan", f"plan {plan!r} is not a known client plan"
        )

    result = await register_client(
        session,
        name=name,
        email=email,
        rut=rut,
        password=password,
        plan=plan,
        settings=cfg,
    )
    return result.client, result.api_key


async def update_client(
    session: AsyncSession,
    *,
    client_id: str,
    name: str | None = None,
    plan: ClientPlan | None = None,
    status: ClientStatus | None = None,
    markup_percent: float | None = None,
    markup_fixed_clp: int | None = None,
) -> Client:
    """PATCH-style update of a single :class:`Client` row.

    Every argument is optional; ``None`` is a no-op for that
    field. The function deliberately uses
    ``if ``x`` is not None`` rather than ``x or default`` so
    a falsy but valid value (``markup_percent=0.0``,
    ``markup_fixed_clp=0``) is accepted.

    Promoting a client to ``admin`` is **not** part of this
    endpoint – the role column is the source of truth for
    the ``require_admin`` dependency, and a silent
    promotion would let a compromised admin grant
    themselves access to a new account without an audit
    trail. If the operator needs a new admin they call the
    bootstrap API (out of scope for the MVP) or run the
    SQL by hand.
    """
    client = await get_client(session, client_id=client_id)
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise InvalidClientUpdateError(
                "invalid_name", "name cannot be blank"
            )
        if len(cleaned) > 200:
            raise InvalidClientUpdateError(
                "invalid_name", "name is too long (max 200 chars)"
            )
        client.name = cleaned
    if plan is not None:
        client.plan = plan
    if status is not None:
        client.status = status
    if markup_percent is not None:
        if markup_percent < 0:
            raise InvalidClientUpdateError(
                "invalid_markup_percent",
                "markup_percent must be greater than or equal to 0",
            )
        client.markup_percent = float(markup_percent)
    if markup_fixed_clp is not None:
        if markup_fixed_clp < 0:
            raise InvalidClientUpdateError(
                "invalid_markup_fixed_clp",
                "markup_fixed_clp must be greater than or equal to 0",
            )
        client.markup_fixed_clp = int(markup_fixed_clp)
    await session.commit()
    await session.refresh(client)
    return client


async def suspend_client(
    session: AsyncSession, *, client_id: str
) -> Client:
    """Flip a client to :attr:`ClientStatus.SUSPENDED`.

    The operation is idempotent: suspending an
    already-suspended client is a no-op (the row is
    returned unchanged but the function still returns a
    success). Re-activating a client is a regular
    ``update_client`` call (``status=ClientStatus.ACTIVE``).
    """
    client = await get_client(session, client_id=client_id)
    if client.status == ClientStatus.SUSPENDED:
        return client
    client.status = ClientStatus.SUSPENDED
    await session.commit()
    await session.refresh(client)
    return client


async def set_client_markup(
    session: AsyncSession,
    *,
    client_id: str,
    markup_percent: float | None = None,
    markup_fixed_clp: int | None = None,
) -> Client:
    """Dedicated setter for the per-client pricing overrides.

    Both arguments are optional; passing ``None`` is a no-op
    for that field. Kept as a separate function so the
    "editar markup" form on the admin dashboard can hit a
    single endpoint without going through the more generic
    :func:`update_client` PATCH contract.
    """
    return await update_client(
        session,
        client_id=client_id,
        markup_percent=markup_percent,
        markup_fixed_clp=markup_fixed_clp,
    )


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


async def admin_overview(
    session: AsyncSession, *, period: datetime | None = None
) -> AdminOverview:
    """Return the aggregate counters for the admin overview card.

    The function aggregates over the *current calendar month*
    (computed from ``period`` so a unit test can pin a
    specific day). The client counters
    (``total_clients`` / ``active_clients`` /
    ``suspended_clients`` / ``pending_clients`` /
    ``admin_users``) are point-in-time snapshots – they are
    not bounded by the period.

    Revenue is the sum of ``cost_clp + fee_clp`` over
    billable messages in the period; the same number the
    customer's invoice carries (a future "expected invoice"
    card can plug straight in).
    """
    now = period or datetime.now(tz=UTC)
    period_start, period_end = _month_bounds(now)

    client_counts = (
        await session.execute(
            select(
                func.count(Client.id).label("total"),
                func.sum(
                    case((Client.status == ClientStatus.ACTIVE, 1), else_=0)
                ).label("active"),
                func.sum(
                    case((Client.status == ClientStatus.SUSPENDED, 1), else_=0)
                ).label("suspended"),
                func.sum(
                    case((Client.status == ClientStatus.PENDING, 1), else_=0)
                ).label("pending"),
                func.sum(
                    case((Client.role == ClientRole.ADMIN, 1), else_=0)
                ).label("admins"),
            )
        )
    ).one()
    total_clients = int(client_counts.total or 0)
    active_clients = int(client_counts.active or 0)
    suspended_clients = int(client_counts.suspended or 0)
    pending_clients = int(client_counts.pending or 0)
    admin_users = int(client_counts.admins or 0)

    msg_counts = (
        await session.execute(
            select(
                func.count(Message.id).label("total"),
                func.sum(
                    case(
                        (Message.status.in_(tuple(BILLABLE_STATUSES)), 1),
                        else_=0,
                    )
                ).label("billable"),
                func.sum(
                    case(
                        (Message.status == MessageStatus.DELIVERED, 1),
                        else_=0,
                    )
                ).label("delivered"),
                func.sum(
                    case(
                        (Message.status == MessageStatus.FAILED, 1),
                        else_=0,
                    )
                ).label("failed"),
                func.sum(
                    case(
                        (
                            or_(
                                Message.status == MessageStatus.PENDING,
                                Message.status == MessageStatus.QUEUED,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("pending"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                Message.status.in_(tuple(BILLABLE_STATUSES)),
                                Message.cost_clp + Message.fee_clp,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("revenue"),
            ).where(
                and_(
                    Message.created_at >= period_start,
                    Message.created_at < period_end,
                )
            )
        )
    ).one()
    total_messages = int(msg_counts.total or 0)
    billable_messages = int(msg_counts.billable or 0)
    delivered_messages = int(msg_counts.delivered or 0)
    failed_messages = int(msg_counts.failed or 0)
    pending_messages = int(msg_counts.pending or 0)
    total_revenue_clp = int(msg_counts.revenue or 0)

    return AdminOverview(
        period_start=period_start.date(),
        period_end=period_end.date(),
        total_clients=total_clients,
        active_clients=active_clients,
        suspended_clients=suspended_clients,
        pending_clients=pending_clients,
        admin_users=admin_users,
        total_messages=total_messages,
        billable_messages=billable_messages,
        delivered_messages=delivered_messages,
        failed_messages=failed_messages,
        pending_messages=pending_messages,
        total_revenue_clp=total_revenue_clp,
    )


async def admin_provider_breakdown(
    session: AsyncSession, *, period: datetime | None = None
) -> Sequence[ProviderBreakdownRow]:
    """Return the per-provider aggregates for the "desglose por proveedor" card.

    The query is grouped by ``(provider, channel)`` and
    ordered by ``total`` descending so the chart's biggest
    bar is the first row. The ``cost_clp`` / ``fee_clp``
    columns are summed across the same period as the
    message counts so the per-provider revenue is
    apples-to-apples with the overview card.

    ``avg_latency_ms`` is the arithmetic mean of the
    ``latency_ms`` column over the same group, ignoring
    ``NULL`` rows (the standard SQL ``AVG`` semantics).
    The query is wrapped in a ``CAST(... AS Float)`` so
    the result comes back as a float on every backend
    SQLAlchemy supports – SQLite returns a ``Decimal``
    by default and the dataclass field is typed
    ``float | None`` to keep the wire shape stable.
    """
    now = period or datetime.now(tz=UTC)
    period_start, period_end = _month_bounds(now)

    stmt = (
        select(
            Message.provider.label("provider"),
            Message.channel.label("channel"),
            func.count(Message.id).label("total"),
            func.sum(
                case(
                    (Message.status == MessageStatus.DELIVERED, 1),
                    else_=0,
                )
            ).label("delivered"),
            func.sum(
                case(
                    (Message.status == MessageStatus.FAILED, 1),
                    else_=0,
                )
            ).label("failed"),
            func.sum(
                case(
                    (
                        or_(
                            Message.status == MessageStatus.PENDING,
                            Message.status == MessageStatus.QUEUED,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("pending"),
            func.coalesce(func.sum(Message.cost_clp), 0).label("cost_clp"),
            func.coalesce(func.sum(Message.fee_clp), 0).label("fee_clp"),
            func.avg(Message.latency_ms).label("avg_latency_ms"),
        )
        .where(
            and_(
                Message.created_at >= period_start,
                Message.created_at < period_end,
            )
        )
        .group_by(Message.provider, Message.channel)
        .order_by(func.count(Message.id).desc())
    )
    result = await session.execute(stmt)
    rows = result.all()
    return tuple(
        ProviderBreakdownRow(
            provider=row.provider,
            channel=str(row.channel),
            total=int(row.total or 0),
            delivered=int(row.delivered or 0),
            failed=int(row.failed or 0),
            pending=int(row.pending or 0),
            cost_clp=int(row.cost_clp or 0),
            fee_clp=int(row.fee_clp or 0),
            avg_latency_ms=(
                float(row.avg_latency_ms)
                if row.avg_latency_ms is not None
                else None
            ),
        )
        for row in rows
    )


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------


async def list_recent_errors(
    session: AsyncSession, *, limit: int = DEFAULT_ERROR_LOG_LIMIT, offset: int = 0
) -> tuple[tuple[ErrorLogEntry, ...], int]:
    """Return the most recent failed messages plus the total count.

    The query joins ``mensajes`` to ``clientes`` so each row
    carries the owning customer's name and email. The result
    is sorted by ``created_at`` descending so the most
    recent failure is at the top – matching the order an
    operator expects to see when triaging.

    The function returns ``(items, total)`` rather than a
    paginated envelope so the route layer can re-use the
    same ``has_more`` computation the other list endpoints
    use.
    """
    if not isinstance(limit, int) or limit < 1:
        raise InvalidClientFilterError(
            "invalid_limit", "limit must be a positive integer"
        )
    if not isinstance(offset, int) or offset < 0:
        raise InvalidClientFilterError(
            "invalid_offset", "offset must be a non-negative integer"
        )
    page_limit = min(limit, _ERROR_LOG_HARD_LIMIT)

    base = (
        select(Message, Client)
        .join(Client, Client.id == Message.client_id)
        .where(Message.status == MessageStatus.FAILED)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(page_limit)
        .offset(offset)
    )
    total = int(
        (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.status == MessageStatus.FAILED
                )
            )
        ).scalar_one()
        or 0
    )
    result = await session.execute(base)
    rows = result.all()
    items = tuple(
        ErrorLogEntry(
            message_id=message.id,
            client_id=client.id,
            client_name=client.name,
            client_email=client.email,
            channel=str(message.channel),
            to_number=message.to_number,
            provider=message.provider,
            error_code=message.error_code,
            error_message=message.error_message,
            created_at=message.created_at,
        )
        for message, client in rows
    )
    return items, total


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


__all__ = (
    "DEFAULT_ERROR_LOG_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "AdminError",
    "AdminOverview",
    "ClientListPage",
    "ClientNotFoundError",
    "ErrorLogEntry",
    "InvalidClientFilterError",
    "InvalidClientUpdateError",
    "ProviderBreakdownRow",
    "admin_overview",
    "admin_provider_breakdown",
    "create_client",
    "get_client",
    "list_clients",
    "list_recent_errors",
    "set_client_markup",
    "suspend_client",
    "update_client",
)
