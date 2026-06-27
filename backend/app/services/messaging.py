"""Messaging service.

This module orchestrates the message-sending flow: it picks the
right provider for a given channel, calls the provider, persists
the outcome and translates the result into a domain object the
route layer can serialise.

The service deliberately **does not** do any HTTP I/O – that
lives in the route handlers – and it **does not** enqueue
background work: the worker (added in a follow-up task) will
take the persisted message and dispatch it. The service is
synchronous from the caller's perspective: the provider is
``await``-ed inline, the row is committed, and the function
returns. A future iteration can swap the inline ``await`` for
"persist with status=``pending`` and let the worker pick it
up" without changing the route handler.

Public functions:

- :func:`send_message`  – persist + dispatch a single message.
- :func:`send_batch`    – persist + dispatch up to N messages
                          in one call (the hard cap is enforced
                          here so the route layer does not have
                          to care).
- :func:`get_message_status` – read a message's current state
                                from the database, refreshing
                                the provider status if the
                                row is still ``pending`` /
                                ``queued``.
- :func:`list_messages` – paginated read of the authenticated
                          customer's message history. Supports
                          the ``channel`` / ``status`` /
                          ``since`` / ``until`` filters the
                          usage dashboard uses to drive its
                          "historial" view.
- :func:`compute_message_cost` – pure helper that maps a
                                  channel + plan to the cost /
                                  fee the message should be
                                  billed at. Kept separate from
                                  :func:`send_message` so the
                                  billing service can use the
                                  same logic without having to
                                  fire a real provider call.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.adapters.base import SendResult
from app.adapters.errors import ProviderError
from app.adapters.registry import get_provider
from app.config import Settings, get_settings
from app.models.client import Client, ClientPlan
from app.models.message import Channel, Message, MessageStatus
from app.observability import normalise_phone

if TYPE_CHECKING:
    from app.adapters.base import BaseProvider


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Default cap on a single ``POST /v1/messages/batch`` request. The
# PRD targets campaigns of a few hundred messages at a time, so
# 100 leaves plenty of headroom while still preventing a single
# customer from monopolising a worker.
DEFAULT_BATCH_SIZE = 100
_BATCH_HARD_LIMIT = 500

# Default page size for the ``GET /v1/messages`` history endpoint.
# 50 rows covers a "last few days" view on a typical Starter-tier
# customer without rendering a multi-megabyte payload. The hard
# cap is enforced at the service layer so the route handler does
# not have to.
DEFAULT_LIST_LIMIT = 50
_LIST_HARD_LIMIT = 200

# Hard cap on the number of rows the CSV export endpoint will
# stream. A monthly report on a Growth-tier customer is well under
# this number; 10,000 leaves room for a multi-year archive dump
# while still preventing a malicious or runaway client from
# asking the API to render an unbounded CSV.
_EXPORT_HARD_LIMIT = 10_000

# Channel-specific character limits. The numbers match the
# operational ceilings the upstream providers enforce: WhatsApp
# text messages are capped at 4096 characters, GSM-7 SMS at
# 160. A stricter ``concatenated`` SMS goes up to 1530
# characters (10 segments of 153); we leave the segmenting to
# the provider and only enforce the conservative cap.
_CHANNEL_LIMITS: dict[Channel, int] = {
    Channel.SMS: 1600,
    Channel.WHATSAPP: 4096,
}

# Minimum acceptable length for a message body. We reject
# empty / whitespace-only payloads at the service layer so the
# provider never sees a body that would just burn quota.
_MIN_BODY_LENGTH = 1

# Provider base cost (in CLP cents) per channel. The MVP uses
# flat numbers so a deployment can configure the plan markup
# through ``Settings`` without forking the service. A future
# iteration will read these from a ``planes`` table.
_BASE_COST_CLP: dict[Channel, int] = {
    Channel.SMS: 25,  # CLP $25
    Channel.WHATSAPP: 80,  # CLP $0.80 per Meta conversation
}

# Plan-level markup (CLP cents per message). The numbers mirror
# the PRD's "Starter / Growth / Enterprise" plans: a flat
# markup of CLP $5 / $3 / $1 respectively.
_PLAN_MARKUP_CLP: dict[ClientPlan, int] = {
    ClientPlan.STARTER: 5,
    ClientPlan.GROWTH: 3,
    ClientPlan.ENTERPRISE: 1,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MessagingError(Exception):
    """Base class for every messaging-domain exception.

    The route layer converts subclasses of this exception into a
    uniform HTTP response so the rest of the platform does not
    need to know which provider surfaced the failure.
    """

    http_status: int = 400
    code: str = "messaging_error"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidMessageError(MessagingError):
    """The request body did not pass validation."""

    http_status = 422
    code = "invalid_message"


class MessageNotFoundError(MessagingError):
    """The requested message id does not exist (or belongs to a
    different client – the error is the same so we do not leak
    the existence of someone else's resource)."""

    http_status = 404
    code = "message_not_found"


class BatchTooLargeError(MessagingError):
    """The batch request exceeded the hard cap."""

    http_status = 422
    code = "batch_too_large"


class InvalidListFilterError(MessagingError):
    """The history query carries an invalid filter value.

    Raised when the dashboard sends a ``channel`` / ``status``
    value the platform does not know about, or a malformed
    ``since`` / ``until`` timestamp. Surfaced as a 422 so the
    caller can fix the input without having to back the
    history view out of the URL bar.
    """

    http_status = 422
    code = "invalid_list_filter"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """Outcome of a single :func:`send_message` call.

    ``message``         – the persisted :class:`Message` row.
    ``provider_msg_id`` – the identifier the upstream assigned;
                          ``None`` if the provider did not
                          acknowledge the message (the row is
                          then marked ``failed``).
    """

    message: Message
    provider_msg_id: str | None


@dataclass(frozen=True)
class BatchOutcome:
    """Outcome of a :func:`send_batch` call.

    ``results`` – per-item outcome in the same order as the
                  request. The route layer uses this to render
                  the ``results`` array in the response.
    """

    results: list[SendOutcome]


@dataclass(frozen=True)
class MessageListPage:
    """A single page of a customer's message history.

    ``items``      – messages on this page, newest first.
    ``total``      – total number of rows matching the filter
                     across the full history (not just the
                     current page). The dashboard uses the
                     value to render the "showing N of M"
                     counter.
    ``limit``      – the page size that was applied; useful
                     so the UI can echo the limit back to
                     the user.
    ``offset``     – the offset that was applied; same
                     rationale as ``limit``.
    ``has_more``   – ``True`` when there is at least one more
                     row after the current page, so the
                     dashboard can render a "cargar más"
                     button without having to do a count
                     query of its own.
    """

    items: list[Message]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class MessageExport:
    """A bulk slice of the customer's message history for export.

    The shape mirrors :class:`MessageListPage` minus the
    pagination knobs (a CSV export is single-shot, not a
    paged read) plus a ``truncated`` flag so the caller can
    warn the user when the result was capped at
    :data:`_EXPORT_HARD_LIMIT`. ``truncated=True`` means
    "the full history is longer than what you got back";
    a UI that ignores the flag will silently drop the older
    rows.
    """

    items: list[Message]
    total: int
    truncated: bool


@dataclass(frozen=True)
class DailyMessageCount:
    """The number of messages dispatched on a single calendar day.

    The dashboard uses the value to drive the "gráfico de
    barras" of daily usage (PRD user story #8: "ver en un
    dashboard cuántos mensajes envié en el mes"). The
    ``channel`` field is ``"all"`` for the totals row and
    one of :class:`Channel` for a per-channel breakdown; the
    service layer returns both shapes from the same query
    so the route handler does not have to issue a second
    round-trip just to colour-code the bars.
    """

    day: date
    channel: str
    count: int


@dataclass(frozen=True)
class DailyUsagePage:
    """The output of :func:`daily_message_counts`.

    The dataclass pairs the per-day aggregation buckets
    with the resolved ``since`` / ``until`` window the
    service picked. The route layer echoes the window in
    the response so the dashboard can render the chart
    axis without having to mirror the default-window
    logic on the client.
    """

    items: list[DailyMessageCount]
    since: datetime
    until: datetime


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


# A loose body-shape check: at least one non-whitespace
# character. The Pydantic model on the route layer already
# rejects empty strings, but the service layer is the one that
# gets called from the worker (or any future in-process
# caller), so the check has to live here too.
_BODY_RE = re.compile(r"\S")


def _validate_body(channel: Channel, body: str) -> str:
    """Return the cleaned body or raise :class:`InvalidMessageError`."""
    if not isinstance(body, str):
        raise InvalidMessageError("invalid_body", "body must be a string")
    cleaned = body.strip()
    if len(cleaned) < _MIN_BODY_LENGTH or not _BODY_RE.search(cleaned):
        raise InvalidMessageError("invalid_body", "body cannot be empty")
    limit = _CHANNEL_LIMITS.get(channel, 4096)
    if len(cleaned) > limit:
        raise InvalidMessageError(
            "body_too_long",
            f"body exceeds the {channel.value} limit of {limit} characters",
        )
    return cleaned


def _validate_destination(channel: Channel, to: str) -> str:
    """Return the canonical ``+56…`` destination or raise."""
    if not isinstance(to, str) or not to.strip():
        raise InvalidMessageError("invalid_destination", "destination is required")
    canonical = normalise_phone(to)
    if canonical is None:
        raise InvalidMessageError(
            "invalid_destination",
            "destination must be a valid Chilean mobile number",
        )
    return canonical


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


def compute_message_cost(*, channel: Channel, plan: ClientPlan) -> tuple[int, int]:
    """Return ``(cost_clp, fee_clp)`` for a single message.

    The cost is the upstream's flat per-message price (the
    values are placeholders – the real numbers will land in a
    ``planes`` table). The fee is the markup the platform
    adds on top, indexed by the client's plan.

    Exposed as a public helper so the billing service can use
    the same logic without firing a real provider call.
    """
    cost = _BASE_COST_CLP.get(channel, 0)
    fee = _PLAN_MARKUP_CLP.get(plan, 0)
    return cost, fee


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _persist_message(
    session: AsyncSession,
    *,
    client: Client,
    channel: Channel,
    to: str,
    body: str,
    settings: Settings,
) -> Message:
    """Create and persist a :class:`Message` row in ``pending`` state.

    The caller is responsible for the actual provider dispatch;
    this helper just makes sure the row exists with the right
    defaults so the worker's "pick up pending messages" query
    can find it.
    """
    cost, fee = compute_message_cost(channel=channel, plan=client.plan)
    message = Message(
        client_id=client.id,
        provider=_provider_name_for(channel, settings=settings),
        channel=channel,
        to_number=to,
        body=body,
        status=MessageStatus.PENDING,
        cost_clp=cost,
        fee_clp=fee,
    )
    session.add(message)
    await session.flush()  # populate server defaults
    return message


def _provider_name_for(channel: Channel, *, settings: Settings) -> str:
    """Return the registered provider's name for ``channel``.

    Looking the name up through the registry (rather than
    hard-coding ``"meta_whatsapp"`` / ``"sms_aggregator"``)
    keeps the persistence layer in sync with the routing
    layer: a future swap of the SMS provider would only need
    a registry change, not a database migration.
    """
    provider = get_provider(channel, settings=settings)
    return provider.name


async def _dispatch(
    provider: BaseProvider,
    message: Message,
) -> SendResult:
    """Call ``provider.send`` and translate the result.

    The helper is kept thin: it returns the provider's
    :class:`SendResult` on success and converts
    :class:`ProviderError` into a :class:`MessagingError` so
    the route layer only has to know about one exception
    hierarchy.
    """
    try:
        return await provider.send(to=message.to_number, body=message.body)
    except ProviderError:
        # The provider layer already classifies the failure
        # (unavailable / validation / rate limit). We re-raise
        # so the route layer can surface the correct HTTP
        # status without re-deriving it from a string.
        raise


def _map_provider_status(raw: str) -> MessageStatus:
    """Translate a provider-specific status string into our enum.

    The mapping is deliberately conservative: anything we do
    not recognise becomes :class:`MessageStatus.UNKNOWN` so a
    future provider status does not silently downgrade a
    message to ``delivered`` just because the string changed.
    """
    if not raw:
        return MessageStatus.UNKNOWN
    normalised = raw.strip().lower()
    if normalised in {"delivered", "read"}:
        return MessageStatus.DELIVERED
    if normalised in {"sent", "accepted", "submitted"}:
        return MessageStatus.SENT
    if normalised in {"queued", "accepted_by_carrier"}:
        return MessageStatus.QUEUED
    if normalised in {"failed", "rejected", "undelivered", "error"}:
        return MessageStatus.FAILED
    if normalised in {"pending", "accepted_by_provider"}:
        return MessageStatus.PENDING
    return MessageStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


async def send_message(
    session: AsyncSession,
    *,
    client: Client,
    channel: Channel,
    to: str,
    body: str,
    settings: Settings | None = None,
) -> SendOutcome:
    """Persist + dispatch a single message and return the outcome.

    The function commits the row before returning so a
    successful ``POST /v1/messages`` is durable even if the
    worker that picks up the delivery receipt crashes in
    parallel.
    """
    cfg = settings or get_settings()
    canonical_to = _validate_destination(channel, to)
    canonical_body = _validate_body(channel, body)

    message = await _persist_message(
        session,
        client=client,
        channel=channel,
        to=canonical_to,
        body=canonical_body,
        settings=cfg,
    )
    # Flush + commit before the network call: a failed
    # dispatch must not roll back the row (the worker / ops
    # team still needs to be able to see *what* was attempted).
    await session.commit()
    await session.refresh(message)

    provider = get_provider(channel, settings=cfg)
    try:
        result: SendResult = await _dispatch(provider, message)
    except ProviderError as exc:
        message.status = MessageStatus.FAILED
        message.error_code = exc.code
        message.error_message = exc.message[:500]
        await session.commit()
        await session.refresh(message)
        return SendOutcome(message=message, provider_msg_id=None)

    message.status = MessageStatus.SENT
    message.provider_msg_id = result.provider_msg_id
    await session.commit()
    await session.refresh(message)
    return SendOutcome(message=message, provider_msg_id=result.provider_msg_id)


async def send_batch(
    session: AsyncSession,
    *,
    client: Client,
    items: list[dict[str, str]],
    settings: Settings | None = None,
) -> BatchOutcome:
    """Persist + dispatch a batch of messages.

    ``items`` is a list of dicts with the same shape as the
    :class:`SendMessageRequest` model the route layer accepts
    (``channel``, ``to``, ``body``). The hard cap is enforced
    before any persistence work so a malicious client cannot
    enqueue thousands of rows by accident.
    """
    cfg = settings or get_settings()
    if not isinstance(items, list):
        raise InvalidMessageError("invalid_batch", "items must be a list")
    if not items:
        raise InvalidMessageError("invalid_batch", "items cannot be empty")
    if len(items) > _BATCH_HARD_LIMIT:
        raise BatchTooLargeError(
            "batch_too_large",
            f"batch size {len(items)} exceeds the hard limit of {_BATCH_HARD_LIMIT}",
        )

    outcomes: list[SendOutcome] = []
    for item in items:
        # We dispatch sequentially so a single bad item does
        # not consume the upstream's rate-limit budget. The
        # worker (added in a follow-up task) will run batches
        # in parallel; the synchronous path here is the
        # "one-shot" behaviour the API edge advertises.
        try:
            channel = Channel(item.get("channel", ""))
        except ValueError as exc:
            raise InvalidMessageError("invalid_channel", str(exc)) from exc
        outcome = await send_message(
            session,
            client=client,
            channel=channel,
            to=item.get("to", ""),
            body=item.get("body", ""),
            settings=cfg,
        )
        outcomes.append(outcome)
    return BatchOutcome(results=outcomes)


async def get_message_status(
    session: AsyncSession,
    *,
    client: Client,
    message_id: str,
    settings: Settings | None = None,
) -> Message:
    """Return the current :class:`Message` row, refreshing the
    provider status when the row is still in flight.

    A row that belongs to a different client is reported as
    :class:`MessageNotFoundError` (the same response an
    unauthenticated caller would see) so the existence of
    another tenant's message is not leaked.
    """
    cfg = settings or get_settings()
    if not isinstance(message_id, str) or not message_id:
        raise MessageNotFoundError("message_not_found", "message id is required")
    stmt = select(Message).where(Message.id == message_id)
    result = await session.execute(stmt)
    message = result.scalar_one_or_none()
    if message is None or message.client_id != client.id:
        raise MessageNotFoundError("message_not_found", "message does not exist")

    # No provider status to refresh if we never reached the
    # upstream or if the upstream already confirmed the
    # terminal state. This keeps the hot path cheap (one DB
    # read) for the common "I just got a delivery receipt"
    # case.
    if message.status in {MessageStatus.DELIVERED, MessageStatus.FAILED}:
        return message
    if not message.provider_msg_id:
        return message

    provider = get_provider(message.channel, settings=cfg)
    try:
        raw = await provider.get_status(message.provider_msg_id)
    except ProviderError:
        # If the upstream is down we return the cached row
        # rather than failing the whole call – the platform's
        # contract is "best-effort status refresh", not
        # "guaranteed up-to-date".
        return message
    new_status = _map_provider_status(raw)
    if new_status != message.status:
        message.status = new_status
        await session.commit()
        await session.refresh(message)
    return message


# ---------------------------------------------------------------------------
# History listing
# ---------------------------------------------------------------------------


def _normalise_channel_filter(value: object) -> Channel | None:
    """Return the :class:`Channel` matching ``value`` or ``None``.

    ``None`` means "no filter" (the SQL query omits the WHERE
    clause). The function accepts a string (the typical case
    from the API edge) or a :class:`Channel` instance (handy
    for unit tests that want to skip the str → enum dance).
    An unknown value is reported as
    :class:`InvalidListFilterError` so the caller surfaces a
    422 instead of silently returning an empty list.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Channel):
        return value
    if isinstance(value, str):
        try:
            return Channel(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_channel",
                f"unknown channel filter: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_channel",
        "channel filter must be a string",
    )


def _normalise_status_filter(value: object) -> MessageStatus | None:
    """Return the :class:`MessageStatus` matching ``value`` or ``None``.

    Same contract as :func:`_normalise_channel_filter`: a
    string-typed enum (``"sent"`` / ``"failed"`` / …) is
    converted, an unknown value raises
    :class:`InvalidListFilterError`. A non-string,
    non-enum value is rejected for symmetry.
    """
    if value is None or value == "":
        return None
    if isinstance(value, MessageStatus):
        return value
    if isinstance(value, str):
        try:
            return MessageStatus(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_status",
                f"unknown status filter: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_status",
        "status filter must be a string",
    )


def _coerce_int(value: object, *, field: str, minimum: int) -> int:
    """Return ``value`` clamped to ``[minimum, infinity)``.

    Used for ``limit`` / ``offset`` query parameters that the
    caller might submit as strings (the FastAPI ``Query``
    annotation does not coerce ``int`` when the input comes
    from a manually-built request) or that a dashboard
    could send as ``0`` / negative. The function rejects
    anything that is not an ``int`` so a typo (e.g. a stray
    string) does not silently degrade into a 200 with
    empty results.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python; we
        # treat a ``True``/``False`` here as a caller bug and
        # surface it as 422 rather than coercing.
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be a positive integer",
        )
    coerced: int
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                f"invalid_{field}",
                f"{field} must be a positive integer",
            ) from exc
    else:
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be a positive integer",
        )
    if coerced < minimum:
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be >= {minimum}",
        )
    return coerced


async def list_messages(
    session: AsyncSession,
    *,
    client: Client,
    channel: object | None = None,
    status: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    settings: Settings | None = None,
) -> MessageListPage:
    """Return a paginated slice of the customer's message history.

    The endpoint backs the dashboard's "Historial" view: a
    chronological list of every message the authenticated
    customer has dispatched, optionally narrowed down by
    channel, status, and a date range. The result is ordered
    by ``created_at`` descending so the most recent message
    is the first row of the response – the dashboard does
    not have to sort client-side.

    Filtering:

    - ``channel`` and ``status`` accept either a string
      (``"sms"`` / ``"sent"`` …) or the matching enum
      member. An unknown value is a 422.
    - ``since`` and ``until`` are inclusive lower / upper
      bounds on :attr:`Message.created_at`. They default to
      "no bound" (a NULL comparison is omitted from the
      WHERE clause) so the dashboard can fetch the full
      history with no parameters.
    - ``limit`` is clamped to :data:`_LIST_HARD_LIMIT` so a
      curious operator cannot ask the API for a million
      rows in one call.
    - ``offset`` is the standard SQL offset; the platform
      does not yet expose cursor-based pagination (the
      dashboard can switch to "cargar más" by bumping the
      offset by ``limit``).

    The function **does not** talk to the upstream provider:
    a history list is a database-only operation, and the
    delivery status has already been reconciled by the time
    the row is in the table (the worker / webhook loop keeps
    it fresh).
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    # ``settings`` is accepted for symmetry with the other
    # service entry points (so a caller can pass a custom
    # ``Settings`` instance in tests) but the current
    # implementation does not read any value from it – the
    # list is a pure database query. Keeping the parameter
    # in the signature means a future change (e.g. a config
    # flag that caps the page size per customer) does not
    # have to break the contract.
    _ = settings
    where: list[ColumnElement[bool]]
    where, channel_filter, status_filter = _build_history_filters(
        client=client,
        channel=channel,
        status=status,
        since=since,
        until=until,
    )
    limit = _coerce_int(limit, field="limit", minimum=1)
    offset = _coerce_int(offset, field="offset", minimum=0)
    if limit > _LIST_HARD_LIMIT:
        limit = _LIST_HARD_LIMIT

    from sqlalchemy import and_, func

    list_stmt = (
        select(Message)
        .where(and_(*where))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = select(func.count(Message.id)).where(and_(*where))

    list_result = await session.execute(list_stmt)
    items = list(list_result.scalars().all())

    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)

    # ``has_more`` is derived from the count (not by querying
    # the next page) so the dashboard can render the "cargar
    # más" button even when the current page fills the
    # window. The cost is one extra ``SELECT COUNT(*)`` per
    # call; on a composite (client_id, created_at) index
    # that is the same cost as the page query itself.
    has_more = (offset + len(items)) < total

    return MessageListPage(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# History export (CSV)
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard lets the customer download
# their history as a CSV file (PRD user story #18: "descargar un
# reporte mensual con detalle de mensajes, para auditoría"). The
# implementation is split in two: a thin database reader that
# returns every row matching the filter, and a pure function that
# projects those rows onto a CSV byte string. The route handler
# stitches the two together so the service layer is the only
# place that knows the export's hard cap and the column shape.


def _build_history_filters(
    *,
    client: Client,
    channel: object | None,
    status: object | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[ColumnElement[bool]], Channel | None, MessageStatus | None]:
    """Validate + normalise the history filters shared by the
    list and the export endpoints.

    Returns the SQLAlchemy ``WHERE`` clause plus the typed
    enum values so the caller can use them in the COUNT query
    without having to re-parse the raw input. The function
    raises :class:`InvalidListFilterError` on any malformed
    input (unknown channel, inverted date range, …) so both
    endpoints surface the same 422 contract.
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    if since is not None and until is not None and since > until:
        raise InvalidListFilterError(
            "invalid_date_range",
            "since must be earlier than or equal to until",
        )
    channel_filter = _normalise_channel_filter(channel)
    status_filter = _normalise_status_filter(status)

    where: list[ColumnElement[bool]] = [Message.client_id == client.id]
    if channel_filter is not None:
        where.append(Message.channel == channel_filter)
    if status_filter is not None:
        where.append(Message.status == status_filter)
    if since is not None:
        where.append(Message.created_at >= since)
    if until is not None:
        where.append(Message.created_at <= until)
    return where, channel_filter, status_filter


async def iter_messages_for_export(
    session: AsyncSession,
    *,
    client: Client,
    channel: object | None = None,
    status: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    settings: Settings | None = None,
) -> MessageExport:
    """Return every message matching the filter, capped at
    :data:`_EXPORT_HARD_LIMIT`.

    The function is the backend for the
    ``GET /v1/messages/export`` CSV endpoint: it returns
    the full result set (newest first) so a flat-file export
    is a single round-trip. The ``truncated`` flag tells the
    caller whether the cap was reached; the route handler
    forwards it as a response header so a script can detect
    a partial export without re-running the count.

    The query mirrors :func:`list_messages` (same WHERE
    shape, same ordering) so the two endpoints never disagree
    on what "the history" means.
    """
    _ = settings  # kept for symmetry with the rest of the service
    where: list[ColumnElement[bool]]
    where, _, _ = _build_history_filters(
        client=client,
        channel=channel,
        status=status,
        since=since,
        until=until,
    )

    from sqlalchemy import and_, func

    # Fetch ``_EXPORT_HARD_LIMIT + 1`` rows so we can tell
    # whether the result was capped without a second query
    # against the table.
    list_stmt = (
        select(Message)
        .where(and_(*where))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(_EXPORT_HARD_LIMIT + 1)
    )
    count_stmt = select(func.count(Message.id)).where(and_(*where))

    list_result = await session.execute(list_stmt)
    raw_items = list(list_result.scalars().all())
    truncated = len(raw_items) > _EXPORT_HARD_LIMIT
    if truncated:
        raw_items = raw_items[:_EXPORT_HARD_LIMIT]

    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)

    return MessageExport(items=raw_items, total=total, truncated=truncated)


# CSV column order for the export. The order is part of the
# public contract: a script that ingests ``mensajes-YYYY-MM.csv``
# expects the same column layout across releases. Append new
# columns at the end so a parser written against the v1 layout
# keeps working.
_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "channel",
    "status",
    "to_number",
    "body",
    "provider",
    "provider_msg_id",
    "error_code",
    "error_message",
    "cost_clp",
    "fee_clp",
)


def _message_to_csv_row(message: Message) -> dict[str, str]:
    """Project a :class:`Message` row onto a CSV-friendly dict.

    Datetimes are serialised in the ISO-8601 form the rest of
    the platform uses (``2026-06-15T10:00:00+00:00``) so the
    CSV stays a flat file: a spreadsheet can sort / filter
    by the column without having to first parse a localised
    format. ``None`` values are written as empty strings
    (the standard csv module handles this), not the literal
    ``"None"`` that ``str(None)`` would produce.
    """
    created_at = message.created_at
    iso = created_at.isoformat() if created_at is not None else ""
    channel_value = (
        message.channel.value
        if isinstance(message.channel, Channel)
        else message.channel
    )
    status_value = (
        message.status.value
        if isinstance(message.status, MessageStatus)
        else message.status
    )
    return {
        "id": message.id,
        "created_at": iso,
        "channel": str(channel_value),
        "status": str(status_value),
        "to_number": message.to_number,
        "body": message.body,
        "provider": message.provider,
        "provider_msg_id": message.provider_msg_id or "",
        "error_code": message.error_code or "",
        "error_message": message.error_message or "",
        "cost_clp": str(message.cost_clp),
        "fee_clp": str(message.fee_clp),
    }


def render_messages_csv(messages: Iterable[Message]) -> str:
    """Render an iterable of :class:`Message` rows as a CSV string.

    The function is a pure helper (no database, no FastAPI
    dependency) so the unit tests can assert on the exact
    wire format without spinning up a session. The
    :class:`csv.DictWriter` is configured for ``utf-8`` with
    explicit quoting so an apostrophe in a message body does
    not break the row layout. The output uses ``\\r\\n`` line
    endings (the RFC-4180 default) so it loads cleanly into
    Excel on both Windows and macOS.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(_CSV_COLUMNS),
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    writer.writeheader()
    for message in messages:
        writer.writerow(_message_to_csv_row(message))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Daily usage aggregation
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard renders a bar chart of
# daily message volume (PRD user story #8). The chart is a
# simple per-day count; a per-channel breakdown is included
# in the same response so the renderer can colour the stacked
# bars without a second round-trip.


# Default window for ``daily_message_counts`` when the caller
# does not pin a ``since`` value. 31 days covers the longest
# month in the year and the previous month when the dashboard
# is opened on the 1st.
_DAILY_DEFAULT_DAYS = 31

# Hard cap on the date range a single call can cover. A
# curious operator could otherwise ask the API for the entire
# history and force a full-table aggregation; the cap is the
# same number the chart's "todo el año" view would need.
_DAILY_HARD_LIMIT_DAYS = 366


def _daily_default_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``[since, until]`` for the default daily window.

    The window covers the trailing 31 days, ending at the
    current instant. Tests pass an explicit ``now`` so the
    result is deterministic; production relies on the
    ``datetime.now(tz=UTC)`` default.

    The function is a thin wrapper so the route layer and the
    tests share the same definition of "default window".
    """
    anchor = now or datetime.now(tz=UTC)
    start_day = anchor.date() - timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
    return start, anchor


async def daily_message_counts(
    session: AsyncSession,
    *,
    client: Client,
    since: datetime | None = None,
    until: datetime | None = None,
    channel: object | None = None,
    settings: Settings | None = None,
) -> DailyUsagePage:
    """Aggregate the customer's messages per day, per channel.

    The function backs the dashboard's "gráfico de barras"
    (issue #6 acceptance criteria). It returns a
    :class:`DailyUsagePage` whose ``items`` field is a flat
    list of :class:`DailyMessageCount` rows; the route layer
    forwards the list to the wire verbatim. Days with no
    traffic are **not** filled in – the dashboard's chart
    component handles the zero-filling so a sparse history
    does not inflate the response.

    Filtering mirrors :func:`list_messages`:

    - ``since`` / ``until`` bound ``created_at`` (inclusive).
      An inverted range is a 422; the function does not
      silently return an empty list.
    - ``channel`` accepts either a :class:`Channel` enum
      member or the matching string. An unknown value is a
      422; the dashboard never sends one, but the validator
      guards against a future caller mistake.

    The function also returns the resolved window so the
    route layer can echo ``since`` / ``until`` in the
    response – the dashboard needs the value to draw the
    chart axis labels.

    Cross-tenant access is blocked by the ``client_id``
    WHERE clause – the function never returns another
    customer's rows.
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    _ = settings  # kept for symmetry with the rest of the service
    if since is not None and until is not None and since > until:
        raise InvalidListFilterError(
            "invalid_date_range",
            "since must be earlier than or equal to until",
        )
    if since is None and until is None:
        since, until = _daily_default_range()
    elif since is None:
        assert until is not None  # narrow the union for mypy
        since = until - timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    elif until is None:
        assert since is not None  # narrow the union for mypy
        until = since + timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    # ``since`` and ``until`` are non-None at this point:
    # every branch above either leaves the argument alone
    # (already non-None) or assigns a fresh value. The
    # explicit assertion is a safety net so a future
    # refactor that re-introduces a ``None`` branch fails
    # loudly here rather than in the SQL builder.
    assert since is not None and until is not None
    if (until - since).days > _DAILY_HARD_LIMIT_DAYS:
        raise InvalidListFilterError(
            "invalid_date_range",
            f"date range cannot exceed {_DAILY_HARD_LIMIT_DAYS} days",
        )
    channel_filter = _normalise_channel_filter(channel)

    where: list[ColumnElement[bool]] = [
        Message.client_id == client.id,
        Message.created_at >= since,
        Message.created_at <= until,
    ]
    if channel_filter is not None:
        where.append(Message.channel == channel_filter)

    # ``func.date`` is the cross-dialect way to truncate a
    # ``TIMESTAMP WITH TIME ZONE`` to the calendar day in
    # UTC. SQLite (the test backend) and PostgreSQL (the
    # production backend) both understand the syntax; the
    # function returns a ``DATE`` value that SQLAlchemy
    # hands back as a :class:`datetime.date`.
    day_expr = func.date(Message.created_at).label("day")
    stmt = (
        select(
            day_expr,
            Message.channel.label("channel"),
            func.count(Message.id).label("count"),
        )
        .where(and_(*where))
        .group_by(day_expr, Message.channel)
        .order_by(day_expr.asc(), Message.channel.asc())
    )
    result = await session.execute(stmt)
    rows = list(result.all())
    # ``_mapping`` returns the row as a typed mapping; the
    # alternative ``row.day`` / ``row.channel`` syntax
    # collides with the built-in :class:`Row` methods
    # (``.count``) at the type-checker level, so we read
    # the labelled columns through the mapping interface.
    items = [
        DailyMessageCount(
            day=_coerce_day(mapping["day"]),
            channel=str(mapping["channel"]),
            count=int(mapping["count"]),
        )
        for mapping in (row._mapping for row in rows)
    ]
    return DailyUsagePage(items=items, since=since, until=until)


def _coerce_day(value: object) -> date:
    """Normalise a ``GROUP BY date(...)`` result to a :class:`date`.

    SQLite (the test backend) returns the truncated value as
    a string; PostgreSQL (the production backend) returns a
    real :class:`datetime.date`. The function is the single
    point of normalisation so the route layer can rely on a
    stable type regardless of the active database engine.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_date_range",
                f"unparseable day value: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_date_range",
        f"unexpected day value: {value!r}",
    )


__all__ = (
    "BatchOutcome",
    "BatchTooLargeError",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_LIST_LIMIT",
    "DailyMessageCount",
    "DailyUsagePage",
    "InvalidListFilterError",
    "InvalidMessageError",
    "MessageExport",
    "MessageListPage",
    "MessageNotFoundError",
    "MessagingError",
    "SendOutcome",
    "compute_message_cost",
    "daily_message_counts",
    "get_message_status",
    "iter_messages_for_export",
    "list_messages",
    "render_messages_csv",
    "send_batch",
    "send_message",
)
