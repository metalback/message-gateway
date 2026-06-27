"""Unit tests for the webhook service layer (issue #5).

The tests cover:

- :func:`app.services.webhooks.sign_payload` – the
  HMAC-SHA256 signing helper. Pure-Python, no I/O.
- :func:`app.services.webhooks.create_webhook` /
  :func:`list_webhooks` / :func:`get_webhook` /
  :func:`update_webhook` / :func:`delete_webhook` –
  the CRUD operations, including tenant-isolation and
  input validation.
- :func:`app.services.webhooks.eligible_subscriptions` /
  :func:`event_for_status` – the receipt-fan-out
  filters.
- :func:`app.services.webhooks.deliver_receipt` – the
  end-to-end delivery flow with the
  :class:`WebhookDeliveryClient` stubbed out so the
  suite never opens a real TCP connection.

The HTTP layer is exercised separately by
:mod:`tests.routes.test_webhooks`.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.models.webhook import DEFAULT_EVENTS, WebhookEvent
from app.services.webhook_delivery import WebhookDeliveryClient, WebhookDeliveryResult
from app.services.webhooks import (
    WebhookNotFoundError,
    WebhookValidationError,
    build_receipt_payload,
    create_webhook,
    delete_webhook,
    deliver_receipt,
    eligible_subscriptions,
    event_for_status,
    get_webhook,
    list_webhooks,
    sign_payload,
    update_webhook,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeDeliveryClient(WebhookDeliveryClient):
    """An in-memory replacement for
    :class:`app.services.webhook_delivery.WebhookDeliveryClient`.

    The fake records every call and returns a
    pre-configured :class:`WebhookDeliveryResult` for the
    next ``deliver()`` invocation. The test suite uses two
    flavours of the fake: a "success" one for the happy
    path and a "fails every time" one for the auto-disable
    path.

    Inheriting from :class:`WebhookDeliveryClient` rather
    than duck-typing keeps the type checker honest – the
    service layer is annotated against the real class,
    and a plain ``class:`` would not satisfy
    ``delivery_client: WebhookDeliveryClient | None``.
    """

    def __init__(self, result: WebhookDeliveryResult | None = None) -> None:
        super().__init__(timeout_seconds=0.1, max_attempts=1, backoff_base_seconds=0.0)
        self.next_result = result or WebhookDeliveryResult(
            succeeded=True,
            attempts=1,
            status_code=200,
            response_body="ok",
            error=None,
        )
        self.calls: list[dict[str, Any]] = []

    async def deliver(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookDeliveryResult:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return self.next_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _make_client(
    async_session,
    *,
    email: str = "ops@acme.cl",
    rut: str = "12345678-5",
) -> Client:
    """Build + persist a :class:`Client` row for the test."""
    client = Client(
        name="Acme",
        email=email,
        rut=rut,
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


def _make_message(*, client: Client, status: MessageStatus) -> Message:
    """Build (not persist) a :class:`Message` row for delivery tests."""
    return Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=status,
        provider_msg_id="prov-1",
    )


# ---------------------------------------------------------------------------
# sign_payload
# ---------------------------------------------------------------------------


def test_sign_payload_returns_lowercase_hex_digest() -> None:
    """The signature is a lowercase hex string so the
    receiver can compare it against its own HMAC-SHA256
    calculation byte-for-byte without a case-folding
    step. The length matches the SHA-256 digest size
    (64 hex characters)."""
    sig = sign_payload(body=b'{"a":1}', secret="k")
    assert len(sig) == 64
    assert sig == sig.lower()
    int(sig, 16)  # hex-decodable – no exception means OK


def test_sign_payload_is_deterministic() -> None:
    """The same body + secret always produces the same
    digest so the receiver can re-derive the expected
    value. A non-deterministic signer would force the
    receiver to keep every signature in a database."""
    a = sign_payload(body=b'{"a":1}', secret="k")
    b = sign_payload(body=b'{"a":1}', secret="k")
    assert a == b


def test_sign_payload_changes_with_body() -> None:
    """A different body produces a different signature –
    a sanity check that the helper is actually hashing
    the body, not just returning a constant."""
    a = sign_payload(body=b'{"a":1}', secret="k")
    b = sign_payload(body=b'{"a":2}', secret="k")
    assert a != b


def test_sign_payload_changes_with_secret() -> None:
    """A different secret produces a different signature –
    a sanity check that the helper is actually keying the
    HMAC, not just hashing the body."""
    a = sign_payload(body=b'{"a":1}', secret="k1")
    b = sign_payload(body=b'{"a":1}', secret="k2")
    assert a != b


def test_sign_payload_rejects_non_bytes_body() -> None:
    """A non-bytes body would silently coerce to a
    different value in some Python versions; the helper
    rejects it eagerly so a misconfigured caller cannot
    ship a different signature to the receiver."""
    with pytest.raises(TypeError):
        sign_payload(body="not-bytes", secret="k")  # type: ignore[arg-type]


def test_sign_payload_rejects_empty_secret() -> None:
    """An empty secret would be a known-weak signer (any
    attacker can guess it). The helper refuses to compute
    a digest so a misconfigured caller cannot accidentally
    ship a low-entropy signature."""
    with pytest.raises(ValueError):
        sign_payload(body=b"{}", secret="")


# ---------------------------------------------------------------------------
# create_webhook
# ---------------------------------------------------------------------------


async def test_create_webhook_persists_and_returns_secret(async_session) -> None:
    """A successful create persists the row, mints a
    fresh secret, and returns both so the route layer
    can hand the secret back to the caller."""
    client = await _make_client(async_session)
    result = await create_webhook(
        async_session,
        client=client,
        url="https://example.com/hooks",
        events=[WebhookEvent.MESSAGE_DELIVERED.value],
    )
    assert result.webhook.client_id == client.id
    assert result.webhook.url == "https://example.com/hooks"
    assert len(result.plain_secret) == 64
    # The same secret must be stored on the row so the
    # delivery helper can sign future receipts.
    assert result.webhook.secret == result.plain_secret


async def test_create_webhook_defaults_to_all_events(async_session) -> None:
    """An empty ``events`` list falls back to the
    platform's "send me everything important" default
    so a typical onboarding request is a one-field
    payload (just the URL)."""
    client = await _make_client(async_session)
    result = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    stored = result.webhook.events.split(",")
    assert set(stored) == set(DEFAULT_EVENTS)


async def test_create_webhook_rejects_non_https_url(async_session) -> None:
    """A non-https URL is rejected with a stable
    :class:`WebhookValidationError` so the route layer
    can surface a 422. http:// URLs are rejected because
    the platform would otherwise ship HMAC-signed
    receipts over the public internet in clear text."""
    client = await _make_client(async_session)
    with pytest.raises(WebhookValidationError) as exc:
        await create_webhook(
            async_session,
            client=client,
            url="http://example.com/hooks",
        )
    assert exc.value.code == "invalid_url"


async def test_create_webhook_rejects_empty_url(async_session) -> None:
    """An empty URL is rejected – the field is required
    at Pydantic validation time (the test would never
    reach the service), but the service layer still
    asserts the invariant so a future direct caller
    (e.g. a worker that auto-creates a subscription on
    signup) cannot accidentally bypass it."""
    client = await _make_client(async_session)
    with pytest.raises(WebhookValidationError):
        await create_webhook(
            async_session, client=client, url="   "
        )


async def test_create_webhook_rejects_unknown_event(async_session) -> None:
    """An event the platform does not know about is
    rejected eagerly so a typo surfaces as a 422 at the
    service boundary, not a silent drop at delivery
    time."""
    client = await _make_client(async_session)
    with pytest.raises(WebhookValidationError) as exc:
        await create_webhook(
            async_session,
            client=client,
            url="https://example.com/hooks",
            events=["message.dance"],
        )
    assert exc.value.code == "unknown_event"


async def test_create_webhook_deduplicates_events(async_session) -> None:
    """A caller that lists the same event twice does not
    pay for the extra delivery attempts – the platform
    stores the de-duplicated list."""
    client = await _make_client(async_session)
    result = await create_webhook(
        async_session,
        client=client,
        url="https://example.com/hooks",
        events=[
            WebhookEvent.MESSAGE_DELIVERED.value,
            WebhookEvent.MESSAGE_DELIVERED.value,
        ],
    )
    stored = result.webhook.events.split(",")
    assert stored.count(WebhookEvent.MESSAGE_DELIVERED.value) == 1


# ---------------------------------------------------------------------------
# list_webhooks / get_webhook
# ---------------------------------------------------------------------------


async def test_list_webhooks_returns_newest_first(async_session) -> None:
    """The list endpoint advertises a newest-first order
    so the dashboard renders "most recent subscription
    on top" without a second query."""
    from datetime import timedelta

    client = await _make_client(async_session)
    first = await create_webhook(
        async_session, client=client, url="https://a.example.com/hooks"
    )
    second = await create_webhook(
        async_session, client=client, url="https://b.example.com/hooks"
    )
    # The two rows were created in the same wall-clock
    # second; nudge ``second``'s timestamp so the
    # ``ORDER BY created_at DESC`` test has a stable
    # tie-breaker.
    second.webhook.created_at = first.webhook.created_at + timedelta(seconds=1)
    await async_session.commit()
    rows = await list_webhooks(async_session, client=client)
    assert [row.id for row in rows] == [second.webhook.id, first.webhook.id]


async def test_list_webhooks_only_returns_callers_own(async_session) -> None:
    """Tenant isolation: the listing never returns
    another client's subscriptions. The test guards
    against a refactor that drops the ``client_id``
    filter and silently leaks the existence of other
    tenants' resources."""
    me = await _make_client(async_session, email="me@acme.cl", rut="12345678-5")
    other = await _make_client(async_session, email="other@acme.cl", rut="11111111-1")
    await create_webhook(
        async_session, client=me, url="https://me.example.com/hooks"
    )
    await create_webhook(
        async_session, client=other, url="https://other.example.com/hooks"
    )
    mine = await list_webhooks(async_session, client=me)
    assert len(mine) == 1
    assert mine[0].url == "https://me.example.com/hooks"


async def test_get_webhook_returns_match(async_session) -> None:
    """A known-good id resolves to the matching row."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    loaded = await get_webhook(
        async_session, client=client, webhook_id=created.webhook.id
    )
    assert loaded.id == created.webhook.id


async def test_get_webhook_rejects_other_clients_row(async_session) -> None:
    """A webhook that belongs to a different client is
    reported as not-found so the existence of another
    tenant's resource is not leaked. Same contract as
    :func:`app.services.messaging.get_message_status`."""
    me = await _make_client(async_session, email="me@acme.cl", rut="12345678-5")
    other = await _make_client(async_session, email="other@acme.cl", rut="11111111-1")
    foreign = await create_webhook(
        async_session, client=other, url="https://other.example.com/hooks"
    )
    with pytest.raises(WebhookNotFoundError):
        await get_webhook(
            async_session, client=me, webhook_id=foreign.webhook.id
        )


async def test_get_webhook_rejects_unknown_id(async_session) -> None:
    """An unknown id is a 404 at the route layer; the
    service layer surfaces a stable code so the
    mapping is obvious."""
    client = await _make_client(async_session)
    with pytest.raises(WebhookNotFoundError):
        await get_webhook(
            async_session,
            client=client,
            webhook_id="00000000-0000-0000-0000-000000000000",
        )


# ---------------------------------------------------------------------------
# update_webhook
# ---------------------------------------------------------------------------


async def test_update_webhook_changes_url(async_session) -> None:
    """A PATCH that targets ``url`` swaps the destination
    endpoint. The new value is validated the same way
    ``create_webhook`` validates it."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://old.example.com/hooks"
    )
    updated = await update_webhook(
        async_session,
        client=client,
        webhook_id=created.webhook.id,
        url="https://new.example.com/hooks",
    )
    assert updated.url == "https://new.example.com/hooks"


async def test_update_webhook_disables_subscription(async_session) -> None:
    """A PATCH that flips ``active`` to ``False`` is the
    canonical "stop the noise" flow the dashboard
    wires up next to a failing endpoint."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    updated = await update_webhook(
        async_session,
        client=client,
        webhook_id=created.webhook.id,
        active=False,
    )
    assert updated.active is False


async def test_update_webhook_rejects_invalid_url(async_session) -> None:
    """A PATCH that targets ``url`` re-validates the
    new value – a regression that only validates on
    create would let a customer migrate to a bad URL
    silently."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    with pytest.raises(WebhookValidationError):
        await update_webhook(
            async_session,
            client=client,
            webhook_id=created.webhook.id,
            url="ftp://example.com/hooks",
        )


# ---------------------------------------------------------------------------
# delete_webhook
# ---------------------------------------------------------------------------


async def test_delete_webhook_drops_row(async_session) -> None:
    """A DELETE removes the row, and a subsequent GET
    surfaces the same 404 the service layer surfaces
    for any unknown id."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    await delete_webhook(
        async_session, client=client, webhook_id=created.webhook.id
    )
    with pytest.raises(WebhookNotFoundError):
        await get_webhook(
            async_session, client=client, webhook_id=created.webhook.id
        )


# ---------------------------------------------------------------------------
# event_for_status / build_receipt_payload / eligible_subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MessageStatus.SENT, WebhookEvent.MESSAGE_SENT.value),
        (MessageStatus.DELIVERED, WebhookEvent.MESSAGE_DELIVERED.value),
        (MessageStatus.FAILED, WebhookEvent.MESSAGE_FAILED.value),
    ],
)
def test_event_for_status_maps_terminal_states(
    status: MessageStatus, expected: str
) -> None:
    """Every terminal / billable state maps to a known
    event. ``pending`` and ``queued`` are intentionally
    absent – the customer has not paid for the message
    yet, and the worker refreshes the status lazily
    rather than emitting an event."""
    assert event_for_status(status) == expected


def test_event_for_status_returns_none_for_pending() -> None:
    """A still-in-flight message does not emit a webhook
    – the receiver would see duplicate ``message.sent``
    events as the worker refreshes the status."""
    assert event_for_status(MessageStatus.PENDING) is None


def test_build_receipt_payload_contains_core_fields() -> None:
    """The receipt payload is the small, stable subset of
    the :class:`Message` row a receiver needs to update
    their own database. Every field is present so the
    receiver does not have to fall back to the public
    API for a re-fetch."""
    client = Client(
        id="c-1",
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="h",
        api_key_hash="h",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    message = _make_message(client=client, status=MessageStatus.DELIVERED)
    payload = build_receipt_payload(message)
    assert payload["id"] == message.id
    assert payload["client_id"] == client.id
    assert payload["status"] == "delivered"
    assert payload["channel"] == "whatsapp"
    assert payload["to_number"] == "+56912345678"


async def test_eligible_subscriptions_filters_inactive(
    async_session,
) -> None:
    """An inactive subscription is excluded from the
    fan-out list – the operator's "stop the noise"
    switch must take effect immediately."""
    client = await _make_client(async_session)
    active = await create_webhook(
        async_session, client=client, url="https://a.example.com/hooks"
    )
    inactive = await create_webhook(
        async_session, client=client, url="https://b.example.com/hooks"
    )
    await update_webhook(
        async_session,
        client=client,
        webhook_id=inactive.webhook.id,
        active=False,
    )
    message = _make_message(client=client, status=MessageStatus.DELIVERED)
    eligible = eligible_subscriptions(
        message, [active.webhook, inactive.webhook]
    )
    assert [w.id for w in eligible] == [active.webhook.id]


async def test_eligible_subscriptions_filters_unrelated_events(
    async_session,
) -> None:
    """A subscription that only opted in to
    ``message.sent`` is excluded when the message's
    current status is ``delivered`` – the
    per-subscription event filter is the customer's
    privacy contract, not a hint."""
    client = await _make_client(async_session)
    sent_only = await create_webhook(
        async_session,
        client=client,
        url="https://sent.example.com/hooks",
        events=[WebhookEvent.MESSAGE_SENT.value],
    )
    delivered_only = await create_webhook(
        async_session,
        client=client,
        url="https://delivered.example.com/hooks",
        events=[WebhookEvent.MESSAGE_DELIVERED.value],
    )
    message = _make_message(client=client, status=MessageStatus.DELIVERED)
    eligible = eligible_subscriptions(
        message, [sent_only.webhook, delivered_only.webhook]
    )
    assert [w.id for w in eligible] == [delivered_only.webhook.id]


async def test_eligible_subscriptions_excludes_other_clients(
    async_session,
) -> None:
    """Defensive: even if the caller forgets to filter
    by ``client_id`` the helper refuses to cross-tenant
    deliver a receipt."""
    me = await _make_client(async_session, email="me@acme.cl", rut="12345678-5")
    other = await _make_client(async_session, email="other@acme.cl", rut="11111111-1")
    my_hook = await create_webhook(
        async_session, client=me, url="https://me.example.com/hooks"
    )
    other_hook = await create_webhook(
        async_session, client=other, url="https://other.example.com/hooks"
    )
    message = _make_message(client=me, status=MessageStatus.DELIVERED)
    eligible = eligible_subscriptions(
        message, [my_hook.webhook, other_hook.webhook]
    )
    assert [w.id for w in eligible] == [my_hook.webhook.id]


# ---------------------------------------------------------------------------
# deliver_receipt
# ---------------------------------------------------------------------------


async def test_deliver_receipt_signs_and_posts(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end happy path: ``deliver_receipt`` looks
    up the subscriptions, signs the body, POSTs to every
    matching URL and returns one outcome per subscription."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session,
        client=client,
        url="https://example.com/hooks",
        events=[WebhookEvent.MESSAGE_DELIVERED.value],
    )
    fake = FakeDeliveryClient()
    async_session.add(
        _make_message(client=client, status=MessageStatus.DELIVERED)
    )
    await async_session.flush()
    message = (
        await async_session.execute(
            __import__("sqlalchemy").select(Message).order_by(Message.created_at.desc())
        )
    ).scalars().first()
    results = await deliver_receipt(
        async_session, message=message, delivery_client=fake
    )
    assert len(results) == 1
    assert results[0].succeeded is True
    # The fake recorded exactly one POST, to the right URL,
    # with the signature header set to the expected digest.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == created.webhook.url
    assert "X-Mgw-Signature" in call["headers"]
    expected = sign_payload(
        body=call["body"], secret=created.webhook.secret
    )
    assert call["headers"]["X-Mgw-Signature"] == expected
    assert call["headers"]["X-Mgw-Event"] == WebhookEvent.MESSAGE_DELIVERED.value
    # The matching subscription is left active after a
    # successful delivery.
    refreshed = await get_webhook(
        async_session, client=client, webhook_id=created.webhook.id
    )
    assert refreshed.active is True


async def test_deliver_receipt_skips_in_flight_messages(
    async_session,
) -> None:
    """``pending`` and ``queued`` messages do not emit a
    receipt – the receiver would see duplicates as the
    worker refreshes the status lazily."""
    client = await _make_client(async_session)
    await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    fake = FakeDeliveryClient()
    async_session.add(
        _make_message(client=client, status=MessageStatus.PENDING)
    )
    await async_session.flush()
    message = (
        await async_session.execute(
            __import__("sqlalchemy").select(Message).order_by(Message.created_at.desc())
        )
    ).scalars().first()
    results = await deliver_receipt(
        async_session, message=message, delivery_client=fake
    )
    assert results == []
    assert fake.calls == []


async def test_deliver_receipt_disables_failing_subscription(
    async_session,
) -> None:
    """When every attempt fails, the subscription is
    auto-disabled so a permanently-broken endpoint
    cannot keep consuming the worker's quota. The
    receiver's debugging surface ("why did the
    deliveries stop?") is the ``active=False`` flag
    the dashboard renders."""
    client = await _make_client(async_session)
    created = await create_webhook(
        async_session, client=client, url="https://example.com/hooks"
    )
    fake = FakeDeliveryClient(
        result=WebhookDeliveryResult(
            succeeded=False,
            attempts=5,
            status_code=502,
            response_body="bad gateway",
            error="http_502",
        )
    )
    async_session.add(
        _make_message(client=client, status=MessageStatus.DELIVERED)
    )
    await async_session.flush()
    message = (
        await async_session.execute(
            __import__("sqlalchemy").select(Message).order_by(Message.created_at.desc())
        )
    ).scalars().first()
    results = await deliver_receipt(
        async_session, message=message, delivery_client=fake
    )
    assert results[0].succeeded is False
    refreshed = await get_webhook(
        async_session, client=client, webhook_id=created.webhook.id
    )
    assert refreshed.active is False
