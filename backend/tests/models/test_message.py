"""Unit tests for the :class:`Message` ORM model.

The tests assert the *shape* of the table: column names,
uniqueness / index constraints and the behaviour of the
Python-side default UUID generator. The schema migration that
creates the table lives in
``backend/alembic/versions/0003_mensajes.py``; the tests here
do not exercise it (that contract is covered by
``test_alembic.py``).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus


def test_mensajes_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "mensajes" in Base.metadata.tables
    table = Base.metadata.tables["mensajes"]
    expected_columns = {
        "id",
        "client_id",
        "provider",
        "channel",
        "to_number",
        "body",
        "status",
        "provider_msg_id",
        "error_code",
        "error_message",
        "cost_clp",
        "fee_clp",
        "created_at",
        "updated_at",
        # ``latency_ms`` was added with the admin
        # dashboard's "latencia promedio por provider" tile
        # (issue #10). The column is nullable and lives
        # next to ``cost_clp`` / ``fee_clp`` in the model
        # so a future column rename would surface here.
        "latency_ms",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_mensajes_indexes_are_declared() -> None:
    """The model declares the indexes the message-sending
    routes actually use: ``client_id`` (per-client history),
    ``status`` (worker picking up pending deliveries),
    ``channel`` and ``to_number`` (debug / audit) and
    ``provider_msg_id`` (status refresh). The composite
    ``(client_id, created_at)`` covers the "list my recent
    messages" query the dashboard runs."""
    table = Base.metadata.tables["mensajes"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("client_id",),
        ("status",),
        ("channel",),
        ("to_number",),
        ("provider_msg_id",),
        ("client_id", "created_at"),
    }
    assert expected.issubset(index_columns), index_columns


def test_channel_and_status_use_string_columns() -> None:
    """``channel`` and ``status`` are stored as ``String`` so
    a future migration that introduces a new enum value does
    not have to also rewrite the column type. The Python-side
    enum lives at the application boundary; the database
    only ever sees a string."""
    table = Base.metadata.tables["mensajes"]
    channel_length = getattr(getattr(table.c.channel.type, "impl", None), "length", None)
    status_length = getattr(getattr(table.c.status.type, "impl", None), "length", None)
    assert isinstance(channel_length, int)
    assert isinstance(status_length, int)
    assert Channel.SMS.value == "sms"
    assert Channel.WHATSAPP.value == "whatsapp"
    assert MessageStatus.PENDING.value == "pending"
    assert MessageStatus.SENT.value == "sent"
    assert MessageStatus.DELIVERED.value == "delivered"
    assert MessageStatus.FAILED.value == "failed"


def test_message_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`Message` row (without persisting
    it) should still produce a UUID id – the autogenerate
    contract lets the service layer create a row, defer the
    flush, and only commit later without ever having to
    remember to assign the primary key by hand."""
    message = Message(
        client_id="00000000-0000-0000-0000-000000000000",
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
    )
    assert message.id is not None
    assert len(message.id) == 36
    assert message.id.count("-") == 4


async def test_persisted_message_round_trips_through_database(async_session) -> None:
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

    message = Message(
        client_id=client.id,
        provider="sms_aggregator",
        channel=Channel.SMS,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.SENT,
        provider_msg_id="agg-1234",
        cost_clp=25,
        fee_clp=3,
        latency_ms=185.0,
    )
    async_session.add(message)
    await async_session.commit()
    await async_session.refresh(message)

    stmt = select(Message).where(Message.id == message.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.client_id == client.id
    assert loaded.channel == Channel.SMS
    assert loaded.status == MessageStatus.SENT
    assert loaded.provider_msg_id == "agg-1234"
    assert loaded.cost_clp == 25
    assert loaded.fee_clp == 3
    assert loaded.to_number == "+56912345678"
    # The wall-clock duration of the dispatch round-trip
    # round-trips through the ORM. ``None`` for a row
    # that pre-dates the column.
    assert loaded.latency_ms == 185.0
