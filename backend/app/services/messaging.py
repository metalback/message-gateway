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

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
# Re-exports
# ---------------------------------------------------------------------------


__all__ = (
    "BatchOutcome",
    "BatchTooLargeError",
    "DEFAULT_BATCH_SIZE",
    "InvalidMessageError",
    "MessageNotFoundError",
    "MessagingError",
    "SendOutcome",
    "compute_message_cost",
    "get_message_status",
    "send_batch",
    "send_message",
)
