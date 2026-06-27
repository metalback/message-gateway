"""Webhook subscription & delivery service.

This module owns the domain logic behind the platform's
delivery-receipt webhooks (issue #5):

- :func:`create_webhook`     – persist a new subscription and
                                return the freshly-minted HMAC
                                secret to the caller (shown
                                exactly once, the same flow
                                :func:`app.services.auth.register_client`
                                uses for API keys).
- :func:`list_webhooks`      – return the client's
                                subscriptions, newest first.
- :func:`get_webhook`        – fetch a single subscription,
                                enforcing tenant isolation.
- :func:`update_webhook`     – flip the ``active`` flag, swap
                                the URL, or change the event
                                subscription.
- :func:`delete_webhook`     – drop a subscription.

- :func:`sign_payload`       – HMAC-SHA256 helper the delivery
                                helper uses to sign every
                                outbound POST.
- :func:`deliver_receipt`    – the actual outbound POST: pick
                                the subscriptions the message's
                                owning client has registered,
                                sign the body, fire the HTTP
                                request (with bounded retry),
                                and return the per-subscription
                                outcome so the worker can log
                                / persist it.

Design choices worth flagging:

- The service does **not** do any HTTP I/O on the request
  path: the CRUD operations are pure-async wrappers around
  the database. The outbound delivery lives behind
  :class:`WebhookDeliveryClient`, an injectable seam that
  the unit tests replace with an in-memory fake.
- HMAC-SHA256 (over the request body, with the secret as
  the key) is the signing scheme. The signature is sent as
  an ``X-Mgw-Signature`` header so the receiver can verify
  the body before parsing it. The header is documented in
  the route's OpenAPI description.
- The retry policy is bounded exponential back-off (1s, 2s,
  4s, 8s, 16s) capped at
  :attr:`Settings.webhook_max_delivery_attempts`. After the
  cap is reached, the subscription is auto-disabled so a
  failing endpoint cannot consume the worker's quota
  indefinitely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import Client
from app.models.message import Message, MessageStatus
from app.models.webhook import DEFAULT_EVENTS, Webhook, WebhookEvent
from app.observability import get_logger

if TYPE_CHECKING:
    from app.services.webhook_delivery import WebhookDeliveryClient, WebhookDeliveryResult

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Maximum length (in characters) of a webhook URL. The
# database column already enforces ``<=500``; the service
# layer asserts the same value eagerly so a request that
# would have failed at the INSERT round-trip fails at the
# service boundary instead.
_MAX_URL_LENGTH = 500

# The HTTP status codes the delivery helper treats as
# "successful". 2xx is the obvious bucket; 3xx is rare for
# a webhook receiver but some teams deliberately return
# ``308 Permanent Redirect`` to migrate endpoints, so the
# helper honours the redirect itself and treats the
# eventual 2xx as success.
_SUCCESS_STATUS_RANGE = range(200, 400)

# Header name the platform uses to sign every outbound
# receipt. Dotted-lowercase to match the convention the
# other public webhooks (Stripe, GitHub) use.
_SIGNATURE_HEADER = "X-Mgw-Signature"
# Event-name header – the receiver can branch on the event
# without parsing the body.
_EVENT_HEADER = "X-Mgw-Event"
# Delivery id header – unique per delivery attempt; lets
# the receiver deduplicate retries.
_DELIVERY_ID_HEADER = "X-Mgw-Delivery"
# Content-Type the delivery helper sets on the outbound
# POST. The receiver can branch on the body shape.
_CONTENT_TYPE_HEADER = "Content-Type"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WebhookError(Exception):
    """Base class for every webhook-domain exception.

    Mirrors :class:`app.services.auth.AuthError`: a stable
    ``code`` for the front-end, a human ``message`` and a
    ``http_status`` the route layer maps onto a
    :class:`fastapi.HTTPException`.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WebhookNotFoundError(WebhookError):
    """The webhook does not exist (or belongs to another client)."""

    http_status = 404


class WebhookValidationError(WebhookError):
    """The request body did not pass validation."""

    http_status = 422


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookCreationResult:
    """The output of :func:`create_webhook`.

    ``webhook``   – the persisted :class:`Webhook` row.
    ``plain_secret`` – the HMAC secret in clear text. Returned
                       exactly once; the caller is expected to
                       surface it to the user and discard it
                       from memory as soon as the user has
                       confirmed they have stored it.
    """

    webhook: Webhook
    plain_secret: str


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> str:
    """Return a clean ``https://`` URL or raise."""
    if not isinstance(url, str):
        raise WebhookValidationError("invalid_url", "url must be a string")
    cleaned = url.strip()
    if not cleaned:
        raise WebhookValidationError("invalid_url", "url is required")
    if len(cleaned) > _MAX_URL_LENGTH:
        raise WebhookValidationError(
            "invalid_url",
            f"url must be at most {_MAX_URL_LENGTH} characters",
        )
    parsed = urlparse(cleaned)
    # ``scheme`` and ``netloc`` are both required; we also
    # reject ``javascript:`` / ``file:`` / ``data:`` URLs by
    # virtue of the scheme check.
    if parsed.scheme != "https":
        raise WebhookValidationError(
            "invalid_url",
            "url must use the https scheme",
        )
    if not parsed.netloc:
        raise WebhookValidationError("invalid_url", "url is missing the host")
    return cleaned


def _validate_events(events: list[str] | None) -> list[str]:
    """Return the canonical list of subscribed events.

    An empty / missing ``events`` falls back to the default
    set (:data:`app.models.webhook.DEFAULT_EVENTS`) so a
    "subscribe me to everything important" caller does not
    have to spell it out. An event the platform does not
    know about is rejected eagerly so a typo surfaces as a
    422 at the service boundary, not a silent drop at
    delivery time.
    """
    if not events:
        return list(DEFAULT_EVENTS)
    if not isinstance(events, list):
        raise WebhookValidationError(
            "invalid_events", "events must be a list of strings"
        )
    known = {event.value for event in WebhookEvent}
    out: list[str] = []
    seen: set[str] = set()
    for raw in events:
        if not isinstance(raw, str):
            raise WebhookValidationError(
                "invalid_events", "events must be a list of strings"
            )
        event = raw.strip()
        if not event:
            continue
        if event not in known:
            raise WebhookValidationError(
                "unknown_event",
                f"event {event!r} is not a known webhook event",
            )
        if event in seen:
            # De-duplicate so a "send me duplicates" caller
            # does not pay for the extra signed POSTs.
            continue
        seen.add(event)
        out.append(event)
    if not out:
        return list(DEFAULT_EVENTS)
    return out


def _encode_events(events: list[str]) -> str:
    """Serialise ``events`` to the comma-separated string the
    column stores.

    Kept in a helper so the format is owned by the service
    layer (and can change – e.g. to JSON – without touching
    the route handlers).
    """
    return ",".join(events)


def _decode_events(raw: str) -> list[str]:
    """Inverse of :func:`_encode_events`.

    Tolerates a missing / empty value (returns the default
    set) so a row that landed in the database before the
    field was non-nullable can still be read.
    """
    if not raw:
        return list(DEFAULT_EVENTS)
    parts = [chunk.strip() for chunk in raw.split(",")]
    return [part for part in parts if part]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_payload(*, body: bytes, secret: str) -> str:
    """Return the HMAC-SHA256 signature of ``body`` keyed with ``secret``.

    The output is the lowercase hex digest the ``X-Mgw-Signature``
    header carries. The function is pure-Python (no I/O) so
    unit tests can verify the contract without spinning up
    an HTTP server.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    digest = hmac.new(
        secret.encode("utf-8"),
        bytes(body),
        hashlib.sha256,
    ).hexdigest()
    return digest


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_webhook(
    session: AsyncSession,
    *,
    client: Client,
    url: str,
    events: list[str] | None = None,
    active: bool = True,
) -> WebhookCreationResult:
    """Persist a new :class:`Webhook` subscription.

    The plain HMAC secret is returned alongside the
    persisted row so the route layer can return it to the
    caller in the same response. The secret is also stored
    on the row – the platform needs it to sign future
    receipts – so a caller who loses the value cannot
    recover it (the contract is the same as the API-key
    flow in :mod:`app.services.auth`).
    """
    canonical_url = _validate_url(url)
    canonical_events = _validate_events(events)
    webhook = Webhook(
        client_id=client.id,
        url=canonical_url,
        events=_encode_events(canonical_events),
        active=bool(active),
    )
    session.add(webhook)
    await session.flush()
    await session.commit()
    await session.refresh(webhook)
    return WebhookCreationResult(webhook=webhook, plain_secret=webhook.secret)


async def list_webhooks(
    session: AsyncSession,
    *,
    client: Client,
) -> list[Webhook]:
    """Return the client's subscriptions, newest first."""
    stmt = (
        select(Webhook)
        .where(Webhook.client_id == client.id)
        .order_by(Webhook.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_webhook(
    session: AsyncSession,
    *,
    client: Client,
    webhook_id: str,
) -> Webhook:
    """Fetch a single webhook, enforcing tenant isolation.

    A webhook that belongs to a different client is
    reported as :class:`WebhookNotFoundError` (the same
    response an unauthenticated caller would see) so the
    existence of another tenant's resource is not leaked.
    """
    if not isinstance(webhook_id, str) or not webhook_id:
        raise WebhookNotFoundError("webhook_not_found", "webhook id is required")
    stmt = select(Webhook).where(Webhook.id == webhook_id)
    result = await session.execute(stmt)
    webhook = result.scalar_one_or_none()
    if webhook is None or webhook.client_id != client.id:
        raise WebhookNotFoundError(
            "webhook_not_found", "webhook does not exist"
        )
    return webhook


async def update_webhook(
    session: AsyncSession,
    *,
    client: Client,
    webhook_id: str,
    url: str | None = None,
    events: list[str] | None = None,
    active: bool | None = None,
) -> Webhook:
    """Apply a partial update to a webhook subscription.

    ``None`` means "do not change". Empty / missing
    payloads are accepted (the route layer rejects an
    entirely-empty body at Pydantic validation time) so a
    "just flip the active flag" call is a one-field
    payload.
    """
    webhook = await get_webhook(session, client=client, webhook_id=webhook_id)
    if url is not None:
        webhook.url = _validate_url(url)
    if events is not None:
        webhook.events = _encode_events(_validate_events(events))
    if active is not None:
        webhook.active = bool(active)
    await session.commit()
    await session.refresh(webhook)
    return webhook


async def delete_webhook(
    session: AsyncSession,
    *,
    client: Client,
    webhook_id: str,
) -> None:
    """Drop a webhook subscription.

    Idempotent: a second call for the same id raises
    :class:`WebhookNotFoundError` so a retried DELETE
    surfaces a 404 the way every other resource does.
    """
    webhook = await get_webhook(session, client=client, webhook_id=webhook_id)
    await session.delete(webhook)
    await session.commit()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def build_receipt_payload(message: Message) -> dict[str, object]:
    """Project a :class:`Message` row to the JSON the
    delivery helper POSTs to the customer's endpoint.

    The shape is deliberately small: only the fields the
    receiver needs to update their own database. The full
    :class:`Message` row stays on the platform side; the
    receiver can always poll ``GET /v1/messages/{id}`` to
    pull the canonical state.
    """
    channel_value = (
        message.channel.value
        if hasattr(message.channel, "value")
        else str(message.channel)
    )
    status_value = (
        message.status.value
        if hasattr(message.status, "value")
        else str(message.status)
    )
    return {
        "id": message.id,
        "client_id": message.client_id,
        "channel": channel_value,
        "status": status_value,
        "to_number": message.to_number,
        "provider": message.provider,
        "provider_msg_id": message.provider_msg_id,
        "error_code": message.error_code,
        "error_message": message.error_message,
        "cost_clp": message.cost_clp,
        "fee_clp": message.fee_clp,
    }


def event_for_status(status: MessageStatus) -> str | None:
    """Map a :class:`MessageStatus` to the matching
    :class:`WebhookEvent` value, or ``None`` when no
    webhook should fire.

    Terminal ``delivered`` and ``failed`` states are the
    most important for a customer waiting on a receipt;
    ``sent`` is exposed too (the customer wants to know
    the upstream accepted the message even before the
    final state is known).
    """
    if status == MessageStatus.DELIVERED:
        return WebhookEvent.MESSAGE_DELIVERED.value
    if status == MessageStatus.FAILED:
        return WebhookEvent.MESSAGE_FAILED.value
    if status == MessageStatus.SENT:
        return WebhookEvent.MESSAGE_SENT.value
    return None


def eligible_subscriptions(
    message: Message,
    webhooks: list[Webhook],
) -> list[Webhook]:
    """Filter ``webhooks`` down to the ones the message
    should be delivered to.

    The rules are deliberately simple:

    - The subscription must be active.
    - The subscription's event list must include the
      event the message's current status maps to.
    """
    event = event_for_status(message.status)
    if event is None:
        return []
    out: list[Webhook] = []
    for webhook in webhooks:
        if webhook.client_id != message.client_id:
            # Defensive: callers are expected to filter by
            # ``client_id`` already, but a misconfigured
            # worker cannot accidentally cross-tenant
            # deliver a receipt through this code path.
            continue
        if not webhook.active:
            continue
        subscribed = _decode_events(webhook.events)
        if event not in subscribed:
            continue
        out.append(webhook)
    return out


async def deliver_receipt(
    session: AsyncSession,
    *,
    message: Message,
    settings: Settings | None = None,
    delivery_client: WebhookDeliveryClient | None = None,
) -> list[WebhookDeliveryResult]:
    """Sign + POST a delivery receipt to every eligible
    subscription for ``message``.

    Returns the per-subscription outcome so the caller
    (the future worker) can persist the result for
    audit / retry. The function never raises on a
    transport error – a failing customer endpoint must
    not crash the worker.
    """
    # Local import to avoid a circular dependency between
    # the service module and the delivery client (the
    # client imports the dataclasses declared here).
    from app.services.webhook_delivery import WebhookDeliveryClient  # noqa: PLC0415

    cfg = settings or get_settings()
    event = event_for_status(message.status)
    if event is None:
        return []
    stmt = select(Webhook).where(
        Webhook.client_id == message.client_id, Webhook.active.is_(True)
    )
    result = await session.execute(stmt)
    webhooks = list(result.scalars().all())
    eligible = eligible_subscriptions(message, webhooks)
    if not eligible:
        return []

    payload = build_receipt_payload(message)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    client = delivery_client or WebhookDeliveryClient(
        timeout_seconds=cfg.webhook_delivery_timeout_seconds,
        max_attempts=cfg.webhook_max_delivery_attempts,
    )
    deliveries: list[WebhookDeliveryResult] = []
    for webhook in eligible:
        signature = sign_payload(body=body, secret=webhook.secret)
        delivery = await client.deliver(
            url=webhook.url,
            body=body,
            headers={
                _SIGNATURE_HEADER: signature,
                _EVENT_HEADER: event,
                _CONTENT_TYPE_HEADER: "application/json",
            },
        )
        if not delivery.succeeded:
            # After the retry budget is exhausted, disable
            # the subscription so a permanently-broken
            # endpoint cannot keep consuming the worker's
            # quota. The platform's contract is "failing
            # endpoints are visible to the operator" –
            # the dashboard renders the disabled flag
            # alongside the failure reason.
            webhook.active = False
        deliveries.append(delivery)
    await session.commit()
    return deliveries


# Re-exports
# ---------------------------------------------------------------------------

__all__ = (
    "WebhookCreationResult",
    "WebhookError",
    "WebhookNotFoundError",
    "WebhookValidationError",
    "build_receipt_payload",
    "create_webhook",
    "delete_webhook",
    "deliver_receipt",
    "eligible_subscriptions",
    "event_for_status",
    "get_webhook",
    "list_webhooks",
    "sign_payload",
    "update_webhook",
)
