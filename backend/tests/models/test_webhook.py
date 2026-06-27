"""Unit tests for the :class:`Webhook` ORM model.

The tests assert the *shape* of the table: column names,
indexes, defaults and the Python-side UUID / HMAC-secret
generators. The schema migration that creates the table
lives in ``backend/alembic/versions/0004_webhooks.py``;
the tests here do not exercise it (that contract is
covered by :mod:`tests.test_alembic`).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.webhook import DEFAULT_EVENTS, Webhook, WebhookEvent


def test_webhooks_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "webhooks" in Base.metadata.tables
    table = Base.metadata.tables["webhooks"]
    expected_columns = {
        "id",
        "client_id",
        "url",
        "events",
        "secret",
        "active",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_webhooks_indexes_are_declared() -> None:
    """The model declares the indexes the delivery-receipt
    fan-out query actually uses: ``client_id`` (per-client
    listing on the dashboard) and the composite
    ``(client_id, active)`` (the worker's "find active
    subscriptions for this message" hot path). The test
    guards against a refactor that drops either of them,
    which would silently turn a cheap lookup into a
    table scan."""
    table = Base.metadata.tables["webhooks"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("client_id",),
        ("client_id", "active"),
    }
    assert expected.issubset(index_columns), index_columns


def test_webhook_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`Webhook` row (without persisting
    it) should still produce a UUID id – the autogenerate
    contract lets the service layer create a row, defer the
    flush, and only commit later without ever having to
    remember to assign the primary key by hand."""
    webhook = Webhook(
        client_id="00000000-0000-0000-0000-000000000000",
        url="https://example.com/hooks",
        events="message.delivered",
        secret="x" * 64,
    )
    assert webhook.id is not None
    assert len(webhook.id) == 36
    assert webhook.id.count("-") == 4


def test_webhook_secret_has_64_hex_chars_by_default() -> None:
    """The HMAC secret must be 256 bits of CSPRNG entropy
    (32 bytes hex-encoded = 64 hex characters) so the
    receiver can verify the signature with a key that
    meets the OWASP recommendation for HMAC-SHA256.
    A shorter default would weaken every signed receipt
    on the platform."""
    webhook = Webhook(
        client_id="00000000-0000-0000-0000-000000000000",
        url="https://example.com/hooks",
        events="message.delivered",
    )
    assert len(webhook.secret) == 64
    int(webhook.secret, 16)  # hex-decodable – no exception means OK


def test_webhook_default_events_include_all_known_events() -> None:
    """``DEFAULT_EVENTS`` must cover every event the
    platform can emit – a subscription that defaults to
    "send me everything important" must actually receive
    every event. A drift between this constant and
    :class:`WebhookEvent` would silently drop receipts."""
    known = {event.value for event in WebhookEvent}
    assert set(DEFAULT_EVENTS) == known


async def test_persisted_webhook_round_trips_through_database(async_session) -> None:
    """A row written through the ORM is read back with every
    field intact. This is the most basic "does the model
    behave" test and the canary for any silent column
    rename."""
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.GROWTH,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()

    webhook = Webhook(
        client_id=client.id,
        url="https://example.com/hooks",
        events="message.delivered,message.failed",
        secret="a" * 64,
        active=True,
    )
    async_session.add(webhook)
    await async_session.commit()
    await async_session.refresh(webhook)

    stmt = select(Webhook).where(Webhook.id == webhook.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.client_id == client.id
    assert loaded.url == "https://example.com/hooks"
    assert loaded.events == "message.delivered,message.failed"
    assert loaded.active is True
