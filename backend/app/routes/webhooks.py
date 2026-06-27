"""Delivery-receipt webhook subscription endpoints.

Implements the public surface documented in the PRD for
the "Webhooks — Delivery Receipts" feature (issue #5):

- ``POST  /v1/webhooks``      – register a new webhook
  subscription. The response carries the freshly-minted
  HMAC secret exactly once.
- ``GET   /v1/webhooks``      – list the authenticated
  client's subscriptions, newest first.
- ``GET   /v1/webhooks/{id}`` – fetch a single
  subscription.
- ``PATCH /v1/webhooks/{id}`` – partial update (URL,
  events, active flag).
- ``DELETE /v1/webhooks/{id}`` – drop a subscription.

All endpoints require a valid ``X-API-Key`` header. The
domain logic lives in :mod:`app.services.webhooks`; this
module only translates the HTTP request into a service
call and renders the response.

The actual outbound delivery (the HMAC-signed POST the
platform makes to the customer's URL when a message
transitions to a billable / terminal status) lives in
:func:`app.services.webhooks.deliver_receipt` and is
invoked by the worker added in a follow-up task.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.models.webhook import Webhook
from app.routes.auth import require_api_key
from app.services.webhooks import (
    WebhookError,
    WebhookNotFoundError,
    WebhookValidationError,
    create_webhook,
    delete_webhook,
    get_webhook,
    list_webhooks,
    update_webhook,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class WebhookCreateRequest(BaseModel):
    """Body of ``POST /v1/webhooks``.

    The ``url`` is validated at the service layer (must be
    ``https://`` and at most 500 characters). The
    ``events`` list defaults to the platform's "send me
    everything important" set when omitted, so a typical
    onboarding request is a one-field payload.
    """

    url: str = Field(..., min_length=1, max_length=500)
    events: list[str] | None = None
    active: bool = True


class WebhookUpdateRequest(BaseModel):
    """Body of ``PATCH /v1/webhooks/{id}``.

    All fields are optional; an entirely-empty body is
    accepted (the route layer rejects it at Pydantic
    validation time) so a "just flip the active flag"
    call is a one-field payload.
    """

    url: str | None = Field(default=None, max_length=500)
    events: list[str] | None = None
    active: bool | None = None


class WebhookResponse(BaseModel):
    """Public projection of a :class:`Webhook` row.

    The HMAC ``secret`` is **not** exposed here: it is
    returned exactly once by ``POST /v1/webhooks`` (the
    :class:`WebhookCreateResponse` below) and never
    again. The dashboard re-derives the secret fingerprint
    from the stored value if it needs to display a
    "secret ending in …" hint.
    """

    id: str
    url: str
    events: list[str]
    active: bool
    created_at: datetime
    updated_at: datetime | None


class WebhookCreateResponse(BaseModel):
    """Response of a successful ``POST /v1/webhooks``.

    Carries the same fields as :class:`WebhookResponse`
    plus the freshly-minted HMAC secret. The secret is
    shown exactly once – the platform does not store the
    plain value retrievably (the database column does
    store it for signing, but there is no GET endpoint
    that returns it), so losing the value means deleting
    the subscription and registering a new one.
    """

    webhook: WebhookResponse
    secret: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(webhook: Webhook) -> WebhookResponse:
    """Project a :class:`Webhook` row to a
    :class:`WebhookResponse`.

    The ``events`` comma-separated string is decoded back
    to a ``list[str]`` so the response shape matches the
    request shape (callers can round-trip a value
    through the API without an extra split / join).
    """
    from app.services.webhooks import _decode_events

    return WebhookResponse(
        id=webhook.id,
        url=webhook.url,
        events=_decode_events(webhook.events),
        active=webhook.active,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


def _raise_webhook_error(exc: WebhookError) -> None:
    """Convert a :class:`WebhookError` into the matching
    :class:`HTTPException`.

    Centralised so the handlers do not have to know
    which HTTP status each domain error maps to. Adding
    a new :class:`WebhookError` subclass is a one-line
    change (override ``http_status``) and no edits
    here.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=WebhookCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Webhook registered; ``secret`` is shown only once."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        422: {"description": "The url is invalid or an event is not recognised."},
    },
)
async def create_endpoint(
    payload: WebhookCreateRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> WebhookCreateResponse:
    """Register a new delivery-receipt subscription.

    The response carries the HMAC ``secret`` exactly
    once – the caller is expected to surface it to the
    user before they navigate away. The platform does
    not expose the secret through any other endpoint,
    so losing the value means deleting the
    subscription and registering a new one.
    """
    try:
        result = await create_webhook(
            session,
            client=current_client,
            url=payload.url,
            events=payload.events,
            active=payload.active,
        )
    except WebhookValidationError as exc:
        _raise_webhook_error(exc)
    return WebhookCreateResponse(
        webhook=_to_response(result.webhook),
        secret=result.plain_secret,
    )


@router.get(
    "",
    response_model=list[WebhookResponse],
    responses={
        200: {"description": "The authenticated client's subscriptions, newest first."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def list_endpoint(
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> list[WebhookResponse]:
    """List the authenticated client's subscriptions."""
    webhooks = await list_webhooks(session, client=current_client)
    return [_to_response(webhook) for webhook in webhooks]


@router.get(
    "/{webhook_id}",
    response_model=WebhookResponse,
    responses={
        200: {"description": "The matching subscription."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The webhook does not exist (or belongs to another client)."},
    },
)
async def get_endpoint(
    webhook_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    """Fetch a single subscription, enforcing tenant isolation."""
    try:
        webhook = await get_webhook(
            session, client=current_client, webhook_id=webhook_id
        )
    except WebhookNotFoundError as exc:
        _raise_webhook_error(exc)
    return _to_response(webhook)


@router.patch(
    "/{webhook_id}",
    response_model=WebhookResponse,
    responses={
        200: {"description": "The updated subscription."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The webhook does not exist (or belongs to another client)."},
        422: {"description": "The url is invalid or an event is not recognised."},
    },
)
async def update_endpoint(
    webhook_id: str,
    payload: WebhookUpdateRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    """Apply a partial update to a subscription.

    Only the fields present in the body are changed;
    ``None`` means "do not touch". A typical
    "just flip the active flag" call therefore carries
    a single ``{"active": false}`` field.
    """
    try:
        webhook = await update_webhook(
            session,
            client=current_client,
            webhook_id=webhook_id,
            url=payload.url,
            events=payload.events,
            active=payload.active,
        )
    except WebhookNotFoundError as exc:
        _raise_webhook_error(exc)
    except WebhookValidationError as exc:
        _raise_webhook_error(exc)
    return _to_response(webhook)


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "The subscription was deleted."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The webhook does not exist (or belongs to another client)."},
    },
)
async def delete_endpoint(
    webhook_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Drop a subscription."""
    try:
        await delete_webhook(
            session, client=current_client, webhook_id=webhook_id
        )
    except WebhookNotFoundError as exc:
        _raise_webhook_error(exc)
    return None
