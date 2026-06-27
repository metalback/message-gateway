"""Unit tests for the :class:`WhatsAppTemplate` ORM model.

The tests assert the *shape* of the table: column names,
uniqueness / index constraints and the behaviour of the
Python-side default UUID generator. The schema migration that
creates the table lives in
``backend/alembic/versions/0004_plantillas_whatsapp.py``; the
tests here do not exercise it (that contract is covered by
``test_alembic.py``).
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.whatsapp_template import (
    WhatsAppTemplate,
    WhatsAppTemplateCategory,
    WhatsAppTemplateStatus,
)


def test_plantillas_whatsapp_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "plantillas_whatsapp" in Base.metadata.tables
    table = Base.metadata.tables["plantillas_whatsapp"]
    expected_columns = {
        "id",
        "client_id",
        "name",
        "language",
        "category",
        "status",
        "meta_template_id",
        "rejection_reason",
        "description",
        "components",
        "created_at",
        "updated_at",
        "submitted_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_plantillas_whatsapp_indexes_are_declared() -> None:
    """The model declares the indexes the templates routes
    actually use: ``client_id`` (per-customer history),
    ``status`` (filter by lifecycle state), ``name`` (lookup
    by Meta-side identifier) and ``meta_template_id`` (lookup
    by Meta's id). The composite ``(client_id, created_at)``
    covers the "list my recent templates" query the
    dashboard runs.

    The unique index on ``(client_id, name, language)`` is
    also asserted because it backs the duplicate-template
    detection the service layer relies on."""
    table = Base.metadata.tables["plantillas_whatsapp"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("client_id",),
        ("name",),
        ("status",),
        ("meta_template_id",),
        ("client_id", "created_at"),
        ("client_id", "name", "language"),
    }
    assert expected.issubset(index_columns), index_columns


def test_unique_index_on_client_name_language() -> None:
    """The composite unique index on
    ``(client_id, name, language)`` is the database-side
    duplicate-template guard. The assertion guards against a
    refactor that drops the constraint and lets two
    customers register the same ``(name, language)`` pair
    (Meta's WABA endpoint would reject the second
    submission, but a clean error at the platform layer is
    friendlier to the customer)."""
    table = Base.metadata.tables["plantillas_whatsapp"]
    unique_indexes = [
        idx for idx in table.indexes if idx.unique
    ]
    matching = [
        idx
        for idx in unique_indexes
        if tuple(idx.c.keys()) == ("client_id", "name", "language")
    ]
    assert matching, "expected a unique index on (client_id, name, language)"


def test_category_and_status_use_string_columns() -> None:
    """``category`` and ``status`` are stored as ``String`` so
    a future release that introduces a new enum value does
    not have to also rewrite the column type. The
    Python-side enum lives at the application boundary; the
    database only ever sees a string."""
    table = Base.metadata.tables["plantillas_whatsapp"]
    assert isinstance(getattr(table.c.category.type, "length", None), int)
    assert isinstance(getattr(table.c.status.type, "length", None), int)
    # Spot-check the canonical values match Meta's vocabulary.
    assert WhatsAppTemplateCategory.UTILITY.value == "utility"
    assert WhatsAppTemplateCategory.MARKETING.value == "marketing"
    assert WhatsAppTemplateCategory.AUTHENTICATION.value == "authentication"
    assert WhatsAppTemplateStatus.DRAFT.value == "draft"
    assert WhatsAppTemplateStatus.PENDING.value == "pending"
    assert WhatsAppTemplateStatus.APPROVED.value == "approved"
    assert WhatsAppTemplateStatus.REJECTED.value == "rejected"


def test_template_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`WhatsAppTemplate` row (without
    persisting it) should still produce a UUID id – the
    autogenerate contract lets the service layer create a
    row, defer the flush, and only commit later without ever
    having to remember to assign the primary key by hand."""
    template = WhatsAppTemplate(
        client_id="00000000-0000-0000-0000-000000000000",
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components="[]",
    )
    assert template.id is not None
    assert len(template.id) == 36
    assert template.id.count("-") == 4
    # The customer-facing fields default to ``None`` until
    # the platform sets them; the test pins that contract so
    # a future refactor that eagerly fills them does not
    # silently change the customer-visible lifecycle.
    assert template.meta_template_id is None
    assert template.rejection_reason is None


def test_components_default_is_empty_json_array() -> None:
    """The ``components`` column defaults to ``"[]"`` so a
    row that has not been filled in yet is a valid JSON
    array. The default lives at the column level so the
    database is the single source of truth."""
    table = Base.metadata.tables["plantillas_whatsapp"]
    # The default is applied on flush, not at construction;
    # assert the column-level default.
    default = table.c.components.default
    assert default is not None
    # ``DefaultGenerator.arg`` is the literal value the
    # migration also wires (see
    # ``0004_plantillas_whatsapp.py`` -> ``server_default="[]"``).
    assert getattr(default, "arg", None) == "[]"


async def test_persisted_template_round_trips_through_database(async_session) -> None:
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

    components = json.dumps(
        [
            {"type": "HEADER", "format": "TEXT", "text": "Hola"},
            {"type": "BODY", "text": "Tu pedido {{1}} ya está en camino."},
            {"type": "FOOTER", "text": "Message Gateway"},
        ]
    )
    template = WhatsAppTemplate(
        client_id=client.id,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=components,
        description="Confirmación de pedido",
        status=WhatsAppTemplateStatus.DRAFT,
    )
    async_session.add(template)
    await async_session.commit()
    await async_session.refresh(template)

    stmt = select(WhatsAppTemplate).where(WhatsAppTemplate.id == template.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.client_id == client.id
    assert loaded.name == "order_confirmation"
    assert loaded.language == "es_CL"
    assert loaded.category == WhatsAppTemplateCategory.UTILITY
    assert loaded.status == WhatsAppTemplateStatus.DRAFT
    assert loaded.components == components
    assert loaded.description == "Confirmación de pedido"


async def test_unique_index_rejects_duplicate_name_language(async_session) -> None:
    """A second template with the same
    ``(client_id, name, language)`` is rejected by the
    database's unique index – the canary for a refactor
    that drops the constraint and lets the platform store
    two rows the upstream will reject later.

    A different customer can still register the same
    ``(name, language)`` pair – the index is scoped to
    ``client_id`` so the platform does not police
    cross-customer collisions (which is Meta's job)."""
    owner = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.GROWTH,
        status=ClientStatus.ACTIVE,
    )
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="11.111.111-1",
        password_hash="hashed",
        api_key_hash="also-hashed-2",
        api_key_last4="wxyz",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add_all([owner, other])
    await async_session.flush()
    other_id = other.id
    owner_id = owner.id

    first = WhatsAppTemplate(
        client_id=owner_id,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components="[]",
    )
    async_session.add(first)
    await async_session.commit()

    # A duplicate ``(name, language)`` is rejected by the
    # unique index. The test catches the exception
    # explicitly (rather than wrapping the commit in
    # ``pytest.raises``) so the rollback can run inside the
    # same async-context manager and a follow-up
    # assertion can be checked on the still-valid session.
    duplicate = WhatsAppTemplate(
        client_id=owner_id,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components="[]",
    )
    async_session.add(duplicate)
    raised = False
    try:
        await async_session.commit()
    except IntegrityError:
        raised = True
        await async_session.rollback()
    assert raised, "expected IntegrityError on duplicate (name, language)"

    # Cross-customer collisions are allowed at the platform
    # level (Meta's WABA endpoint is the source of truth for
    # "the same name on two different WABAs").
    other_template = WhatsAppTemplate(
        client_id=other_id,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components="[]",
    )
    async_session.add(other_template)
    await async_session.commit()
    assert other_template.id is not None
