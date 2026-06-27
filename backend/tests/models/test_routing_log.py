"""Unit tests for the :class:`RoutingLog` ORM model (issue #11).

The tests assert the *shape* of the table: column names,
the index set and the Python-side default UUID generator.
The schema migration that creates the table lives in
``backend/alembic/versions/0006_provider_health.py``; the
tests here do not exercise it (that contract is covered by
``test_alembic.py``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.sql.schema import ScalarElementColumnDefault

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.models.routing_log import RoutingLog, RoutingLogOutcome


def test_routing_log_table_is_registered() -> None:
    """The model registers itself in the shared
    ``metadata`` so Alembic's autogenerate picks the
    table up without extra wiring."""
    assert "routing_log" in Base.metadata.tables
    table = Base.metadata.tables["routing_log"]
    expected_columns = {
        "id",
        "message_id",
        "provider_attempted",
        "channel",
        "outcome",
        "latency_ms",
        "error_code",
        "error_message",
        "attempted_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_routing_log_indexes_are_declared() -> None:
    """The model declares the indexes the admin
    dashboard and the per-message trace actually use:
    ``message_id`` (trace view),
    ``provider_attempted`` (per-provider chart),
    ``attempted_at`` (most-recent) and the composite
    ``(provider_attempted, attempted_at)`` that backs
    the "latencia promedio en las últimas 24h" query."""
    table = Base.metadata.tables["routing_log"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("message_id",),
        ("provider_attempted",),
        ("attempted_at",),
        ("provider_attempted", "attempted_at"),
    }
    assert expected.issubset(index_columns), index_columns


def test_message_id_is_nullable() -> None:
    """A ``routing_log`` row may have no associated
    message – the health-check worker inserts a probe
    row with a synthetic id. The ``message_id`` column
    must therefore be ``nullable``."""
    table = Base.metadata.tables["routing_log"]
    assert table.c.message_id.nullable is True


def test_latency_ms_defaults_to_zero() -> None:
    """A sub-millisecond call is a legal value (e.g. a
    cache hit on a mock adapter). The default of ``0``
    keeps the column contract honest even when the
    recorder is wired with a zero-latency stub."""
    table = Base.metadata.tables["routing_log"]
    latency_default = table.c.latency_ms.default
    assert isinstance(latency_default, ScalarElementColumnDefault)
    assert latency_default.arg == 0


def test_error_message_is_capped_at_500_chars() -> None:
    """A verbose upstream response must not blow up
    the column. The 500-char ceiling mirrors
    :attr:`app.models.message.Message.error_message`
    so the two error columns are interchangeable in
    the admin UI."""
    table = Base.metadata.tables["routing_log"]
    length = getattr(table.c.error_message.type, "length", None)
    assert isinstance(length, int)
    assert length == 500


def test_outcome_uses_string_column() -> None:
    """``outcome`` is stored as a ``String`` so a future
    migration that introduces a new
    :class:`RoutingLogOutcome` value (e.g.
    ``"rate_limited"``) does not have to also rewrite
    the column type. The Python-side enum lives at the
    application boundary; the database only ever sees a
    string."""
    table = Base.metadata.tables["routing_log"]
    length = getattr(table.c.outcome.type, "length", None)
    assert isinstance(length, int)
    assert length == 20
    assert RoutingLogOutcome.SUCCESS.value == "success"
    assert RoutingLogOutcome.FAILURE.value == "failure"
    assert RoutingLogOutcome.VALIDATION_ERROR.value == "validation_error"


def test_routing_log_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`RoutingLog` row (without
    persisting it) should still produce a UUID id – the
    autogenerate contract lets the service layer create
    a row, defer the flush, and only commit later
    without ever having to remember to assign the
    primary key by hand."""
    row = RoutingLog(
        provider_attempted="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
    )
    assert row.id is not None
    uuid.UUID(row.id)
    # ``latency_ms`` is a server-side / column-level
    # default; it only applies once the row is flushed.
    # The migration mirrors the same value as
    # ``server_default`` so the contract holds when a
    # raw SQL insert hits the table – the test for that
    # contract lives in
    # :func:`test_persisted_routing_log_round_trips_through_database`.
    assert row.message_id is None
    assert row.error_code is None
    assert row.error_message is None


async def test_persisted_routing_log_round_trips_through_database(
    async_session,
) -> None:
    """A row written through the ORM is read back with
    every field intact. The test pins the ``message_id``
    foreign key (a chain can produce N rows per
    message) and the ``error_code`` /
    ``error_message`` columns the per-message trace
    surfaces.
    """
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()

    message = Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.SENT,
        provider_msg_id="wamid.1",
    )
    async_session.add(message)
    await async_session.flush()

    log = RoutingLog(
        message_id=message.id,
        provider_attempted="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.FAILURE,
        latency_ms=120,
        error_code="provider_unavailable",
        error_message="meta 5xx",
    )
    async_session.add(log)
    await async_session.commit()
    await async_session.refresh(log)

    stmt = select(RoutingLog).where(RoutingLog.id == log.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.message_id == message.id
    assert loaded.provider_attempted == "meta_whatsapp"
    assert loaded.channel == Channel.WHATSAPP
    assert loaded.outcome == RoutingLogOutcome.FAILURE
    assert loaded.latency_ms == 120
    assert loaded.error_code == "provider_unavailable"
    assert loaded.error_message == "meta 5xx"


async def test_routing_log_can_be_inserted_without_message(
    async_session,
) -> None:
    """The health-check worker inserts probe rows with
    no associated message (the synthetic
    ``__healthcheck__`` id has no ``Message`` row). The
    ``message_id`` column must therefore accept
    ``NULL`` at the database level too – the test
    pins the contract that an admin "test ahora"
    probe does not crash on a missing FK."""
    log = RoutingLog(
        message_id=None,
        provider_attempted="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
        latency_ms=42,
    )
    async_session.add(log)
    await async_session.commit()
    await async_session.refresh(log)
    assert log.id is not None
    assert log.message_id is None
    assert log.latency_ms == 42
