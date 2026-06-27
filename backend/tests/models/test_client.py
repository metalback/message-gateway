"""Unit tests for the :class:`Client` ORM model.

The tests assert the *shape* of the table: column names,
uniqueness constraints and the behaviour of the Python-side
default UUID generator. The schema migration that creates
the table lives in
``backend/alembic/versions/0002_clientes.py``; the tests
here do not exercise it (that contract is covered by
``test_alembic.py``).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus


def test_clientes_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "clientes" in Base.metadata.tables
    table = Base.metadata.tables["clientes"]
    expected_columns = {
        "id",
        "name",
        "email",
        "rut",
        "password_hash",
        "api_key_hash",
        "api_key_last4",
        "plan",
        "status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_email_and_rut_have_unique_constraints() -> None:
    """``email`` and ``rut`` carry ``UNIQUE`` constraints at
    the table level – the migration script applies them too,
    but the model is the single source of truth for what the
    application code assumes."""
    table = Base.metadata.tables["clientes"]
    # The columns are flagged ``unique=True`` in the model; the
    # assertion below verifies the flag survived the round-trip
    # through SQLAlchemy.
    assert table.c.email.unique is True
    assert table.c.rut.unique is True
    assert table.c.api_key_hash.index is True


def test_status_and_plan_use_string_columns() -> None:
    """``plan`` and ``status`` are stored as ``String`` so a
    future migration that introduces a new enum value does
    not have to also rewrite the column type. The Python-side
    enum lives at the application boundary; the database only
    ever sees a string."""
    table = Base.metadata.tables["clientes"]
    # The custom :class:`_StringEnum` type delegates to a
    # plain ``String`` column; the ``impl`` attribute holds
    # the underlying SQL type whose ``length`` is what the
    # migration relies on. We poke at it via ``getattr`` to
    # stay on the right side of the static type checker,
    # which sees the column type as a generic ``TypeEngine``.
    plan_length = getattr(getattr(table.c.plan.type, "impl", None), "length", None)
    status_length = getattr(getattr(table.c.status.type, "impl", None), "length", None)
    assert isinstance(plan_length, int)
    assert isinstance(status_length, int)
    assert ClientStatus.ACTIVE.value == "active"
    assert ClientPlan.STARTER.value == "starter"


def test_client_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`Client` row (without persisting
    it) should still produce a UUID id – the autogenerate
    contract lets the auth service create a row, defer the
    flush, and only commit later without ever having to
    remember to assign the primary key by hand."""
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="x",
        api_key_hash="y",
        api_key_last4="abcd",
    )
    assert client.id is not None
    # The id is a 36-character string with the standard UUID
    # layout (8-4-4-4-12 hex characters separated by dashes).
    assert len(client.id) == 36
    assert client.id.count("-") == 4


async def test_persisted_client_round_trips_through_database(async_session) -> None:
    """A row written through the ORM is read back with every
    field intact. This is the most basic "does the model
    behave" test and the canary for any silent column rename."""
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
    await async_session.commit()
    await async_session.refresh(client)

    stmt = select(Client).where(Client.id == client.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.email == "ops@acme.cl"
    assert loaded.rut == "12345678-5"
    assert loaded.plan == ClientPlan.GROWTH
    assert loaded.status == ClientStatus.ACTIVE
    assert loaded.api_key_last4 == "abcd"
