"""Message sending / status endpoints.

Implements the public surface documented in the PRD:

- ``POST /v1/messages``        – send a single message.
- ``POST /v1/messages/batch``  – send a batch of messages.
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

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, conlist
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.db import get_db
from app.models.client import Client
from app.models.message import Channel, Message, MessageStatus
from app.routes.auth import require_api_key
from app.services.messaging import (
    DEFAULT_BATCH_SIZE,
    BatchOutcome,
    BatchTooLargeError,
    InvalidMessageError,
    MessageNotFoundError,
    MessagingError,
    SendOutcome,
    get_message_status,
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
    """

    items: conlist(  # type: ignore[valid-type]
        BatchItem, min_length=1, max_length=DEFAULT_BATCH_SIZE
    )


class BatchResponse(BaseModel):
    """Response of a successful ``POST /v1/messages/batch``."""

    results: list[MessageResponse]


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
    returns one result per item. A failure on item ``N`` does
    not abort the batch: each item has its own row, and a
    failed row carries the error code in ``error_code`` /
    ``error_message`` so the caller can retry just the
    failures.
    """
    try:
        outcome: BatchOutcome = await send_batch(
            session,
            client=current_client,
            items=[item.model_dump() for item in payload.items],
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
    return BatchResponse(results=[_to_response(item.message) for item in outcome.results])


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
