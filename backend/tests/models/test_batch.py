"""Unit tests for the :class:`Batch` ORM model (issue #9).

The tests assert the *shape* of the table: column names,
the index set and the Python-side default UUID generator.
The schema migration that creates the table lives in
``backend/alembic/versions/0005_lotes_mensajes.py``; the
tests here do not exercise it (that contract is covered by
``test_alembic.py``).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.base import Base
from app.models.batch import Batch, BatchStatus
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus


def test_lotes_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "lotes" in Base.metadata.tables
    table = Base.metadata.tables["lotes"]
    expected_columns = {
        "id",
        "client_id",
        "name",
        "total_count",
        "pending_count",
        "delivered_count",
        "failed_count",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_lotes_indexes_are_declared() -> None:
    """The model declares the indexes the batch routes
    actually use: ``client_id`` (per-client history),
    ``status`` (filter by lifecycle state) and the
    composite ``(client_id, created_at)`` that covers
    the dashboard's "list my recent batches" query."""
    table = Base.metadata.tables["lotes"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("client_id",),
        ("status",),
        ("client_id", "created_at"),
    }
    assert expected.issubset(index_columns), index_columns


def test_status_uses_string_column() -> None:
    """``status`` is stored as a ``String`` so a future
    migration that introduces a new
    :class:`BatchStatus` value (e.g. ``"cancelled"``)
    does not have to also rewrite the column type. The
    Python-side enum lives at the application boundary;
    the database only ever sees a string."""
    table = Base.metadata.tables["lotes"]
    status_length = getattr(table.c.status.type, "length", None)
    assert isinstance(status_length, int)
    assert status_length == 20
    assert BatchStatus.PROCESSING.value == "processing"
    assert BatchStatus.COMPLETED.value == "completed"
    assert BatchStatus.FAILED.value == "failed"


def test_batch_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`Batch` row (without
    persisting it) should still produce a UUID id – the
    autogenerate contract lets the service layer create
    a row, defer the flush, and only commit later
    without ever having to remember to assign the
    primary key by hand."""
    batch = Batch(
        client_id="00000000-0000-0000-0000-000000000000",
    )
    assert batch.id is not None
    assert len(batch.id) == 36
    assert batch.id.count("-") == 4


def test_batch_defaults_to_processing_status() -> None:
    """A freshly-built :class:`Batch` row starts in
    :class:`BatchStatus.PROCESSING` with the counters
    zeroed. The defaults are column-level
    (``default=BatchStatus.PROCESSING`` /
    ``default=0``) and the migration mirrors them with
    ``server_default`` so the contract holds even when
    the row is inserted through a raw SQL path.

    The test pins the column-level defaults so a future
    refactor that drops them does not silently change
    the dashboard-visible lifecycle.
    """
    from sqlalchemy.sql.schema import ScalarElementColumnDefault

    table = Base.metadata.tables["lotes"]
    # The model declares the defaults at the column
    # level so the service layer does not have to set
    # them by hand. The migration mirrors the same
    # values as ``server_default`` so a raw SQL
    # ``INSERT`` (e.g. a future analytics pipeline)
    # inherits the same contract.
    status_default = table.c.status.default
    total_default = table.c.total_count.default
    pending_default = table.c.pending_count.default
    delivered_default = table.c.delivered_count.default
    failed_default = table.c.failed_count.default

    assert isinstance(status_default, ScalarElementColumnDefault)
    assert status_default.arg == BatchStatus.PROCESSING
    assert isinstance(total_default, ScalarElementColumnDefault)
    assert total_default.arg == 0
    assert isinstance(pending_default, ScalarElementColumnDefault)
    assert pending_default.arg == 0
    assert isinstance(delivered_default, ScalarElementColumnDefault)
    assert delivered_default.arg == 0
    assert isinstance(failed_default, ScalarElementColumnDefault)
    assert failed_default.arg == 0
    assert table.c.name.nullable is True
    assert table.c.completed_at.nullable is True


async def test_persisted_batch_round_trips_through_database(async_session) -> None:
    """A row written through the ORM is read back with
    every field intact. This is the most basic "does
    the model behave" test and the canary for any
    silent column rename."""
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

    batch = Batch(
        client_id=client.id,
        name="Black Friday 2026",
        total_count=10,
        pending_count=10,
        delivered_count=0,
        failed_count=0,
        status=BatchStatus.PROCESSING,
    )
    async_session.add(batch)
    await async_session.commit()
    await async_session.refresh(batch)

    stmt = select(Batch).where(Batch.id == batch.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.client_id == client.id
    assert loaded.name == "Black Friday 2026"
    assert loaded.total_count == 10
    assert loaded.pending_count == 10
    assert loaded.delivered_count == 0
    assert loaded.failed_count == 0
    assert loaded.status == BatchStatus.PROCESSING
    assert loaded.completed_at is None


async def test_message_carries_batch_id(async_session) -> None:
    """A :class:`Message` row created as part of a
    :class:`Batch` carries the ``batch_id`` foreign key
    so the counter-recompute query can group the
    per-item statuses under the campaign. The single-
    message path leaves the column ``NULL``."""
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

    batch = Batch(
        client_id=client.id,
        name="Lanzamiento",
        total_count=1,
        pending_count=1,
        status=BatchStatus.PROCESSING,
    )
    async_session.add(batch)
    await async_session.flush()

    batched = Message(
        client_id=client.id,
        batch_id=batch.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.SENT,
        provider_msg_id="wamid.1",
    )
    single = Message(
        client_id=client.id,
        batch_id=None,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56987654321",
        body="chau",
        status=MessageStatus.SENT,
        provider_msg_id="wamid.2",
    )
    async_session.add_all([batched, single])
    await async_session.commit()

    loaded = (
        await async_session.execute(select(Message).where(Message.id == batched.id))
    ).scalar_one()
    assert loaded.batch_id == batch.id

    solo = (
        await async_session.execute(select(Message).where(Message.id == single.id))
    ).scalar_one()
    assert solo.batch_id is None
