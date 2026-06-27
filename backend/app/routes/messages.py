"""Message sending / status endpoints.

Implements the public surface documented in the PRD:

- ``GET  /v1/messages``        – list the authenticated
                                  customer's message history
                                  (used by the dashboard's
                                  "Historial y consumo" view).
- ``GET  /v1/messages/summary`` – per-status aggregate of the
                                  customer's traffic for the
                                  "desglose por estado" card
                                  on the dashboard.
- ``GET  /v1/messages/daily``  – per-day, per-channel counts
                                  for the "gráfico de barras"
                                  the dashboard renders above
                                  the history table.
- ``GET  /v1/messages/export`` – CSV download of the same
                                  history (issue #6 follow-up).
- ``POST /v1/messages``        – send a single message.
- ``POST /v1/messages/batch``  – send a batch of messages
                                  (issue #9). Returns the
                                  new ``batch_id`` plus a
                                  per-item result list and a
                                  summary rollup.
- ``GET  /v1/messages/batch``  – list the authenticated
                                  customer's batches
                                  (dashboard's "Campañas"
                                  view), newest first.
- ``GET  /v1/messages/batch/{id}`` – read the current state
                                       of a single batch
                                       (counters + lifecycle).
- ``GET  /v1/messages/{id}``   – read the current status of a
                                  message (refreshing the
                                  provider's view if the row
                                  is still in flight).

All endpoints require a valid ``X-API-Key`` header (the
``require_api_key`` dependency in :mod:`app.routes.auth` is
the single source of truth for API-key authentication). The
domain logic lives in :mod:`app.services.messaging`; this
module only translates the HTTP request into a service call
and renders the response.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, conlist
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.db import get_db
from app.models.batch import Batch, BatchStatus
from app.models.client import Client
from app.models.message import Channel, Message, MessageStatus
from app.routes.auth import require_api_key
from app.services.messaging import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LIST_LIMIT,
    BatchListPage,
    BatchNotFoundError,
    BatchOutcome,
    BatchSummary,
    BatchTooLargeError,
    DailyUsagePage,
    InvalidListFilterError,
    InvalidMessageError,
    MessageExport,
    MessageListPage,
    MessageNotFoundError,
    MessageStatusSummary,
    MessagingError,
    SendOutcome,
    daily_message_counts,
    get_batch,
    get_message_status,
    iter_messages_for_export,
    list_batches,
    list_messages,
    message_status_summary,
    render_messages_csv,
    send_batch,
    send_message,
)

router = APIRouter(prefix="/messages", tags=["messages"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SendMessageRequest(BaseModel):
    """Body of ``POST /v1/messages``.

    The platform accepts a single destination per request; the
    batch endpoint covers the "many recipients" use case. The
    body length is bounded at the per-channel limit by the
    service layer.
    """

    channel: Channel
    to: str = Field(..., min_length=1, max_length=20)
    body: str = Field(..., min_length=1, max_length=4096)


class MessageResponse(BaseModel):
    """Projection of a :class:`Message` row for the public API.

    PII fields (``to_number``, ``body``) are included in the
    response so the dashboard can show "what did I just send?".
    Future iterations may switch to a stricter projection if
    the legal team flags the body as sensitive.
    """

    id: str
    channel: Channel
    status: MessageStatus
    to_number: str
    body: str
    provider: str
    provider_msg_id: str | None
    error_code: str | None
    error_message: str | None
    cost_clp: int
    fee_clp: int
    created_at: datetime


class SendMessageResponse(BaseModel):
    """Response of a successful ``POST /v1/messages``.

    The single ``message`` field is what most clients need;
    we wrap it in an envelope so a future iteration can
    surface auxiliary data (rate-limit counters, delivery
    ETA) without breaking the existing contract.
    """

    message: MessageResponse


class BatchItem(BaseModel):
    """A single entry in a ``POST /v1/messages/batch`` request."""

    channel: Channel
    to: str = Field(..., min_length=1, max_length=20)
    body: str = Field(..., min_length=1, max_length=4096)


class BatchRequest(BaseModel):
    """Body of ``POST /v1/messages/batch``.

    The default cap is :data:`DEFAULT_BATCH_SIZE`; the
    service layer also enforces a hard upper bound so a
    malicious client cannot enqueue thousands of rows by
    accident.

    ``name`` is an optional human-readable label
    (e.g. "Black Friday 2026") the dashboard renders
    on the "Campañas" view. Optional so a one-off
    campaign can land without forcing the caller to
    pick a name.
    """

    items: conlist(  # type: ignore[valid-type]
        BatchItem, min_length=1, max_length=DEFAULT_BATCH_SIZE
    )
    name: str | None = Field(default=None, max_length=200)


class BatchSummaryResponse(BaseModel):
    """The headline counters of a :class:`Batch`.

    Mirrors :class:`app.services.messaging.BatchSummary`
    so the dashboard can render the values directly on
    the "campañas" widget without re-iterating the
    per-item results.

    ``succeeded`` is the number of items that reached a
    terminal state (``delivered + failed``); a caller
    that just wants "how many made it through?" can
    read the single field instead of summing the other
    two.
    """

    total: int
    pending: int
    delivered: int
    failed: int
    succeeded: int


class BatchResponse(BaseModel):
    """Response of a successful ``POST /v1/messages/batch``.

    ``batch_id`` – the id of the
                   :class:`app.models.batch.Batch` row
                   the call created. The caller can
                   poll it through
                   ``GET /v1/messages/batch/{batch_id}``
                   to check progress.
    ``summary``  – rollup counters the dashboard
                   surfaces on the "campañas" view.
    ``results``  – per-item outcome in the same order
                   as the request. A failed item
                   (``status=failed``) does not abort
                   the rest of the batch; the caller can
                   retry just the failures.
    """

    batch_id: str
    summary: BatchSummaryResponse
    results: list[MessageResponse]


class BatchListItemResponse(BaseModel):
    """Projection of a :class:`app.models.batch.Batch` row
    for the public API.

    The shape is the same as the ``BatchResponse`` summary
    block plus the lifecycle fields the dashboard needs to
    render the "Campañas" view.
    """

    id: str
    name: str | None
    status: BatchStatus
    total_count: int
    pending_count: int
    delivered_count: int
    failed_count: int
    created_at: datetime
    updated_at: datetime | None
    completed_at: datetime | None


class BatchListResponse(BaseModel):
    """Response of a successful ``GET /v1/messages/batch``.

    The shape mirrors :class:`MessageListResponse` so the
    dashboard's "Campañas" view can reuse the same
    pagination component the "Historial" view already uses.
    """

    items: list[BatchListItemResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class BatchDetailResponse(BaseModel):
    """Response of a successful ``GET /v1/messages/batch/{id}``.

    Wraps a :class:`BatchListItemResponse` so a future
    iteration can add envelope metadata (rate-limit
    counters, the per-item results of a fresh
    re-compute, …) without breaking the existing
    client contract.
    """

    batch: BatchListItemResponse


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------


def _batch_to_response(batch: Batch) -> BatchListItemResponse:
    """Project a :class:`Batch` row to a :class:`BatchListItemResponse`."""
    return BatchListItemResponse(
        id=batch.id,
        name=batch.name,
        status=batch.status,
        total_count=batch.total_count,
        pending_count=batch.pending_count,
        delivered_count=batch.delivered_count,
        failed_count=batch.failed_count,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
    )


def _summary_to_response(summary: BatchSummary) -> BatchSummaryResponse:
    """Project a :class:`BatchSummary` to a :class:`BatchSummaryResponse`."""
    return BatchSummaryResponse(
        total=summary.total,
        pending=summary.pending,
        delivered=summary.delivered,
        failed=summary.failed,
        succeeded=summary.succeeded,
    )


class MessageListResponse(BaseModel):
    """Response of a successful ``GET /v1/messages``.

    The shape is a thin wrapper over the
    :class:`MessageListPage` dataclass the service layer
    returns: the same fields, the same names. The wrapper
    exists so a future iteration can add envelope metadata
    (rate-limit counters, a "next page" URL, …) without
    breaking the existing client contract.
    """

    items: list[MessageResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class DailyUsageBucket(BaseModel):
    """A single (day, channel, count) bucket in the daily
    usage response.

    Mirrors the :class:`DailyMessageCount` dataclass the
    service layer returns. The route layer projects the
    value directly; the dashboard does not have to know
    about the dataclass type.
    """

    day: date
    channel: str
    count: int


class DailyUsageResponse(BaseModel):
    """Response of a successful ``GET /v1/messages/daily``.

    The endpoint returns the raw aggregation buckets plus
    the window the service picked (so the dashboard can
    render the chart axis without having to mirror the
    default-window logic on the client).
    """

    since: datetime
    until: datetime
    items: list[DailyUsageBucket]


class StatusSummaryBucket(BaseModel):
    """A single ``(status, count)`` row in the status
    summary response.

    Mirrors the :class:`MessageStatusCount` dataclass the
    service layer returns. The route layer projects the
    value directly; the dashboard does not have to know
    about the dataclass type.
    """

    status: MessageStatus
    count: int


class StatusSummaryResponse(BaseModel):
    """Response of a successful ``GET /v1/messages/summary``.

    The endpoint carries the per-status buckets, the
    headline counters the dashboard surfaces in the
    "desglose por estado" card, the summed cost / fee
    amounts, the resolved ``since`` / ``until`` window
    and the ``delivery_rate`` the widget renders as a
    progress bar. ``delivery_rate`` is in the closed
    interval ``[0.0, 1.0]``; the client multiplies by
    ``100`` to render a percentage.
    """

    items: list[StatusSummaryBucket]
    total: int
    delivered: int
    failed: int
    pending: int
    cost_clp: int
    fee_clp: int
    delivery_rate: float
    since: datetime
    until: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(message: Message) -> MessageResponse:
    """Project a :class:`Message` row to a :class:`MessageResponse`."""
    return MessageResponse(
        id=message.id,
        channel=message.channel,
        status=message.status,
        to_number=message.to_number,
        body=message.body,
        provider=message.provider,
        provider_msg_id=message.provider_msg_id,
        error_code=message.error_code,
        error_message=message.error_message,
        cost_clp=message.cost_clp,
        fee_clp=message.fee_clp,
        created_at=message.created_at,
    )


def _raise_messaging_error(exc: MessagingError) -> None:
    """Convert a :class:`MessagingError` into the matching HTTPException.

    Centralised so the handlers do not have to know which
    HTTP status each domain error maps to. Adding a new
    :class:`MessagingError` subclass is a one-line change
    (override ``http_status`` / ``code``) and no edits here.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


def _raise_provider_error(exc: ProviderError) -> None:
    """Convert a :class:`ProviderError` into the matching HTTPException.

    The mapping mirrors the values declared on
    :mod:`app.adapters.errors` so the HTTP contract is
    independent of the specific provider that surfaced the
    failure.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message, "provider": exc.provider},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Message accepted and dispatched to the provider."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The request body failed validation or the provider rejected it."},
        429: {"description": "The provider rate-limited the request."},
        502: {"description": "The provider is unreachable."},
    },
)
async def send(
    payload: SendMessageRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    """Send a single message.

    The endpoint is intentionally asynchronous: the platform
    accepts the message, persists the row, calls the provider
    inline, and returns ``202 Accepted`` once the row is
    durable. A delivery receipt arrives later through the
    webhook (added in a follow-up task) or can be polled via
    ``GET /v1/messages/{id}``.
    """
    try:
        outcome: SendOutcome = await send_message(
            session,
            client=current_client,
            channel=payload.channel,
            to=payload.to,
            body=payload.body,
        )
    except InvalidMessageError as exc:
        _raise_messaging_error(exc)
    except ProviderValidationError as exc:
        _raise_provider_error(exc)
    except ProviderRateLimitError as exc:
        _raise_provider_error(exc)
    except ProviderUnavailableError as exc:
        _raise_provider_error(exc)
    except ProviderError as exc:
        _raise_provider_error(exc)
    return SendMessageResponse(message=_to_response(outcome.message))


@router.post(
    "/batch",
    response_model=BatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Batch accepted; one row per item in ``results``."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The batch is empty, too large, or contains invalid items."},
        429: {"description": "The provider rate-limited the request."},
        502: {"description": "The provider is unreachable."},
    },
)
async def send_batch_endpoint(
    payload: BatchRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> BatchResponse:
    """Send a batch of messages.

    The endpoint dispatches the items sequentially and
    returns one result per item plus a rollup summary
    (total / pending / delivered / failed / succeeded).
    A failure on item ``N`` does not abort the batch:
    each item has its own row, and a failed row carries
    the error code in ``error_code`` /
    ``error_message`` so the caller can retry just the
    failures.

    The response carries a ``batch_id`` the caller can
    poll through ``GET /v1/messages/batch/{batch_id}``
    to check progress. The id is also visible in the
    dashboard's "Campañas" view.
    """
    try:
        outcome: BatchOutcome = await send_batch(
            session,
            client=current_client,
            items=[item.model_dump() for item in payload.items],
            name=payload.name,
        )
    except (InvalidMessageError, BatchTooLargeError) as exc:
        _raise_messaging_error(exc)
    except ProviderValidationError as exc:
        _raise_provider_error(exc)
    except ProviderRateLimitError as exc:
        _raise_provider_error(exc)
    except ProviderUnavailableError as exc:
        _raise_provider_error(exc)
    except ProviderError as exc:
        _raise_provider_error(exc)
    return BatchResponse(
        batch_id=outcome.batch_id,
        summary=_summary_to_response(outcome.summary),
        results=[_to_response(item.message) for item in outcome.results],
    )


@router.get(
    "/batch",
    response_model=BatchListResponse,
    responses={
        200: {"description": "A page of the customer's batch history, newest first."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The filter values failed validation."},
    },
)
async def list_batches_endpoint(
    batch_status: BatchStatus | None = Query(
        default=None,
        alias="status",
        description="Restrict the listing to a single batch lifecycle state.",
    ),
    limit: int = Query(
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        description="Page size; capped server-side at the list hard limit.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of rows to skip (for pagination).",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> BatchListResponse:
    """List the authenticated customer's batch history.

    The endpoint is the backend for the dashboard's
    "Campañas" view: a paginated, filterable list of
    every campaign the customer has submitted through
    ``POST /v1/messages/batch``. The result is ordered
    newest first, so the dashboard does not have to
    re-sort on the client.

    The endpoint never crosses the tenant boundary: a
    customer can only see their own batches.
    """
    try:
        page: BatchListPage = await list_batches(
            session,
            client=current_client,
            status=batch_status,
            limit=limit,
            offset=offset,
        )
    except InvalidListFilterError as exc:
        _raise_messaging_error(exc)
    return BatchListResponse(
        items=[_batch_to_response(batch) for batch in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.get(
    "/batch/{batch_id}",
    response_model=BatchDetailResponse,
    responses={
        200: {"description": "The current state of the batch."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The batch does not exist (or belongs to another client)."},
    },
)
async def get_batch_endpoint(
    batch_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> BatchDetailResponse:
    """Read the current state of a single batch.

    The endpoint recomputes the batch's counters on the
    way out so a campaign that has been receiving
    delivery receipts asynchronously through the webhook
    loop still shows up-to-date numbers when the
    customer opens the dashboard.

    Cross-tenant access is blocked by the
    ``client_id`` WHERE clause in the service layer: a
    batch that belongs to a different client is reported
    as 404 (the same response an unauthenticated caller
    would see) so the existence of another tenant's
    campaign is not leaked.
    """
    try:
        batch = await get_batch(
            session,
            client=current_client,
            batch_id=batch_id,
        )
    except BatchNotFoundError as exc:
        _raise_messaging_error(exc)
    return BatchDetailResponse(batch=_batch_to_response(batch))


@router.get(
    "",
    response_model=MessageListResponse,
    responses={
        200: {"description": "A page of the customer's message history, newest first."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The filter values failed validation."},
    },
)
async def list_history(
    channel: Channel | None = Query(
        default=None,
        description="Restrict the history to a single delivery channel.",
    ),
    message_status: MessageStatus | None = Query(
        default=None,
        alias="status",
        description="Restrict the history to a single message status.",
    ),
    since: datetime | None = Query(
        default=None,
        description="Only return messages created on or after this ISO-8601 instant.",
    ),
    until: datetime | None = Query(
        default=None,
        description="Only return messages created on or before this ISO-8601 instant.",
    ),
    limit: int = Query(
        default=DEFAULT_LIST_LIMIT,
        ge=1,
        description="Page size; capped server-side at the list hard limit.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of rows to skip (for pagination).",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> MessageListResponse:
    """List the authenticated customer's message history.

    The endpoint is the backend for the dashboard's
    "Historial y consumo" view: a paginated, filterable list
    of every message the customer has dispatched through
    the platform. The result is ordered newest first, so
    the dashboard does not have to re-sort on the client.

    Filters are all optional; a request with no query
    parameters returns the first 50 messages of the full
    history. The dashboard's "filtrar por canal" /
    "filtrar por estado" controls set the matching query
    parameters; the date range picker sets ``since`` /
    ``until``.

    The endpoint never crosses the tenant boundary: a
    customer can only see their own messages, and the
    existence of another tenant's history is not even
    hinted at (an unknown channel / status is the same
    422 a misspelled filter would return).
    """
    try:
        page: MessageListPage = await list_messages(
            session,
            client=current_client,
            channel=channel,
            status=message_status,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except InvalidListFilterError as exc:
        _raise_messaging_error(exc)
    return MessageListResponse(
        items=[_to_response(message) for message in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


def _export_filename(now: datetime) -> str:
    """Build the ``Content-Disposition`` filename for an export.

    The filename is the same the dashboard downloads: a
    snake-case noun + the export date. Including the date
    keeps multiple exports (one per month) in the same
    downloads folder without a name collision, and the
    ``mensajes-`` prefix matches the underlying table name so
    an operator can ``grep`` a backup archive.
    """
    return f"mensajes-{now.strftime('%Y-%m-%d')}.csv"


@router.get(
    "/export",
    responses={
        200: {
            "description": (
                "A CSV file with every message matching the filter, "
                "newest first. The response carries a "
                "``X-Export-Truncated`` header set to ``true`` when "
                "the export hit the server-side hard cap."
            ),
            "content": {"text/csv": {}},
        },
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The filter values failed validation."},
    },
)
async def export_history(
    channel: Channel | None = Query(
        default=None,
        description="Restrict the export to a single delivery channel.",
    ),
    message_status: MessageStatus | None = Query(
        default=None,
        alias="status",
        description="Restrict the export to a single message status.",
    ),
    since: datetime | None = Query(
        default=None,
        description="Only export messages created on or after this ISO-8601 instant.",
    ),
    until: datetime | None = Query(
        default=None,
        description="Only export messages created on or before this ISO-8601 instant.",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """Download the authenticated customer's history as a CSV file.

    The endpoint is the "Descargar CSV" button on the
    dashboard: a single shot download that respects the
    same filter set the on-screen list uses (channel /
    status / date range) so a customer exporting "this
    month, only WhatsApp, only failed" gets the same rows
    the table is showing. The result is a ``text/csv``
    payload with a ``Content-Disposition`` attachment
    header, ready to save to disk.

    The route is mounted at ``/messages/export`` (rather
    than the more natural ``/messages/export.csv``) so it
    sits cleanly above the ``/messages/{message_id}``
    path the rest of the API uses. The literal segment
    keeps FastAPI's route matcher from trying to resolve
    ``export`` as a message id.
    """
    try:
        export: MessageExport = await iter_messages_for_export(
            session,
            client=current_client,
            channel=channel,
            status=message_status,
            since=since,
            until=until,
        )
    except InvalidListFilterError as exc:
        _raise_messaging_error(exc)
    body = render_messages_csv(export.items)
    headers = {
        "Content-Disposition": f'attachment; filename="{_export_filename(datetime.now())}"',
        # ``X-Export-Truncated`` lets a script detect a
        # partial export without re-running the count. The
        # value is the literal string ``"true"`` / ``"false"``
        # because HTTP headers are text by definition.
        "X-Export-Truncated": "true" if export.truncated else "false",
        "X-Export-Total": str(export.total),
    }
    return Response(content=body, media_type="text/csv; charset=utf-8", headers=headers)


@router.get(
    "/daily",
    response_model=DailyUsageResponse,
    responses={
        200: {"description": "Per-day, per-channel message counts."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The filter values failed validation."},
    },
)
async def daily_usage(
    channel: Channel | None = Query(
        default=None,
        description="Restrict the aggregation to a single delivery channel.",
    ),
    since: datetime | None = Query(
        default=None,
        description="Lower bound on ``created_at``. Defaults to a 31-day rolling window.",
    ),
    until: datetime | None = Query(
        default=None,
        description="Upper bound on ``created_at``. Defaults to the current instant.",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> DailyUsageResponse:
    """Return the per-day, per-channel message counts the
    dashboard's bar chart renders.

    The endpoint is the backend for the issue #6 acceptance
    criterion "gráfico de uso diario se renderiza
    correctamente". A request with no query parameters
    returns the trailing 31 days; the dashboard can narrow
    the window by pinning ``since`` / ``until`` (e.g. a
    "este mes" toggle that sets ``since`` to the first day
    of the month).

    The response carries the resolved ``since`` / ``until``
    so the chart axis can be drawn without mirroring the
    service's default-window logic on the client.
    """
    try:
        page: DailyUsagePage = await daily_message_counts(
            session,
            client=current_client,
            channel=channel,
            since=since,
            until=until,
        )
    except InvalidListFilterError as exc:
        _raise_messaging_error(exc)
    return DailyUsageResponse(
        since=page.since,
        until=page.until,
        items=[
            DailyUsageBucket(day=row.day, channel=row.channel, count=row.count)
            for row in page.items
        ],
    )


@router.get(
    "/summary",
    response_model=StatusSummaryResponse,
    responses={
        200: {
            "description": (
                "Per-status aggregate of the customer's traffic "
                "for the resolved window. Drives the "
                "'desglose por estado' card on the dashboard."
            ),
        },
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The filter values failed validation."},
    },
)
async def status_summary(
    channel: Channel | None = Query(
        default=None,
        description="Restrict the aggregation to a single delivery channel.",
    ),
    since: datetime | None = Query(
        default=None,
        description="Lower bound on ``created_at``. Defaults to a 31-day rolling window.",
    ),
    until: datetime | None = Query(
        default=None,
        description="Upper bound on ``created_at``. Defaults to the current instant.",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> StatusSummaryResponse:
    """Return the per-status message counts the dashboard's
    "desglose por estado" card renders.

    The endpoint pairs the per-status buckets with the
    headline counters (``total`` / ``delivered`` /
    ``failed`` / ``pending``), the summed ``cost_clp`` /
    ``fee_clp`` amounts, the ``delivery_rate`` the widget
    renders as a progress bar and the resolved ``since`` /
    ``until`` window. A request with no query parameters
    returns the trailing 31 days (same default as
    ``/v1/messages/daily`` so the two widgets describe the
    same period by default).

    The route is mounted at ``/messages/summary`` so it
    sits cleanly above the ``/messages/{message_id}``
    path the rest of the API uses. The literal segment
    keeps FastAPI's route matcher from trying to resolve
    ``summary`` as a message id (the same pattern the
    ``/messages/export`` route uses).
    """
    try:
        summary: MessageStatusSummary = await message_status_summary(
            session,
            client=current_client,
            channel=channel,
            since=since,
            until=until,
        )
    except InvalidListFilterError as exc:
        _raise_messaging_error(exc)
    return StatusSummaryResponse(
        items=[
            StatusSummaryBucket(status=row.status, count=row.count)
            for row in summary.items
        ],
        total=summary.total,
        delivered=summary.delivered,
        failed=summary.failed,
        pending=summary.pending,
        cost_clp=summary.cost_clp,
        fee_clp=summary.fee_clp,
        delivery_rate=summary.delivery_rate,
        since=summary.since,
        until=summary.until,
    )


@router.get(
    "/{message_id}",
    response_model=MessageResponse,
    responses={
        200: {"description": "The current state of the message."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The message does not exist (or belongs to another client)."},
    },
)
async def get_one(
    message_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Read the current state of a single message.

    The endpoint refreshes the row from the provider when the
    row is still in flight. A successful read is cheap (one
    database hit) and the upstream is only consulted when the
    row is in a non-terminal state.
    """
    try:
        message = await get_message_status(
            session,
            client=current_client,
            message_id=message_id,
        )
    except MessageNotFoundError as exc:
        _raise_messaging_error(exc)
    return _to_response(message)
