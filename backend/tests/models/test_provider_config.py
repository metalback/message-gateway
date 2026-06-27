"""Unit tests for the :class:`ProviderConfig` ORM model (issue #11).

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
from app.models.message import Channel
from app.models.provider_config import ProviderConfig, ProviderHealth


def test_provider_config_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``
    so Alembic's autogenerate picks the table up without
    extra wiring."""
    assert "provider_config" in Base.metadata.tables
    table = Base.metadata.tables["provider_config"]
    expected_columns = {
        "id",
        "name",
        "channel",
        "priority",
        "base_url",
        "health_status",
        "last_health_check",
        "consecutive_failures",
        "consecutive_successes",
        "last_latency_ms",
        "active",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_provider_config_indexes_are_declared() -> None:
    """The model declares the indexes the admin dashboard
    actually uses: ``channel`` (per-channel filter),
    ``last_health_check`` (stale-data alert) and the
    composite ``(active, channel)`` that backs the
    registry's "list active providers for channel X"
    query."""
    table = Base.metadata.tables["provider_config"]
    index_columns = {tuple(idx.c.keys()) for idx in table.indexes}
    expected = {
        ("channel",),
        ("last_health_check",),
        ("active", "channel"),
    }
    assert expected.issubset(index_columns), index_columns


def test_name_is_unique() -> None:
    """Two rows for the same upstream would race the
    health-check worker and silently double the rate of
    outbound probes. The unique constraint catches a
    duplicate at INSERT time."""
    table = Base.metadata.tables["provider_config"]
    assert table.c.name.unique is True


def test_health_status_defaults_to_unknown() -> None:
    """A never-probed provider must default to
    :attr:`ProviderHealth.UNKNOWN` so the dashboard does
    not render a green indicator for a row the worker
    has not touched yet."""
    table = Base.metadata.tables["provider_config"]
    health_default = table.c.health_status.default
    assert isinstance(health_default, ScalarElementColumnDefault)
    assert health_default.arg == ProviderHealth.UNKNOWN
    # The column length matches the longest health
    # value (``"unhealthy"`` is 9 chars) plus a margin
    # for a future "quarantined" / "disabled" value.
    length = getattr(table.c.health_status.type, "length", None)
    assert isinstance(length, int)
    assert length == 20


def test_consecutive_counters_default_to_zero() -> None:
    """The failure / success counters the health worker
    increments start at zero so the first probe is the
    first signal (no stale history from a prior run)."""
    table = Base.metadata.tables["provider_config"]
    failures_default = table.c.consecutive_failures.default
    successes_default = table.c.consecutive_successes.default
    assert isinstance(failures_default, ScalarElementColumnDefault)
    assert failures_default.arg == 0
    assert isinstance(successes_default, ScalarElementColumnDefault)
    assert successes_default.arg == 0


def test_active_defaults_to_true() -> None:
    """A freshly-built :class:`ProviderConfig` row is
    *active* by default – the operator must explicitly
    flip the kill-switch to disable a provider."""
    table = Base.metadata.tables["provider_config"]
    active_default = table.c.active.default
    assert isinstance(active_default, ScalarElementColumnDefault)
    assert active_default.arg is True


def test_provider_config_id_is_a_uuid_string_by_default() -> None:
    """Constructing a :class:`ProviderConfig` row
    (without persisting it) should still produce a UUID
    id – the autogenerate contract lets the service
    layer create a row, defer the flush, and only
    commit later without ever having to remember to
    assign the primary key by hand."""
    config = ProviderConfig(
        name="meta_whatsapp",
        channel=Channel.WHATSAPP,
    )
    assert config.id is not None
    # The id parses as a UUID – a malformed default
    # would surface here before the row is flushed.
    uuid.UUID(config.id)
    # The remaining defaults (``health_status``,
    # ``consecutive_*``) are server-side / column-level
    # defaults that only apply on flush. The migration
    # mirrors the same values as ``server_default``,
    # so the contract holds when a raw SQL insert hits
    # the table. The test for that contract lives in
    # :func:`test_persisted_provider_config_round_trips_through_database`;
    # here we only assert the eagerly-populated
    # identity.
    assert config.id is not None


async def test_persisted_provider_config_round_trips_through_database(
    async_session,
) -> None:
    """A row written through the ORM is read back with
    every field intact. This is the most basic "does
    the model behave" test and the canary for any
    silent column rename.

    The :class:`Client` parent is not strictly required
    (the table has no foreign key to ``clientes``) but
    we build one anyway so the test mirrors the shape
    a future iteration that adds per-client provider
    overrides would use.
    """
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

    config = ProviderConfig(
        name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        priority=0,
        base_url="https://graph.facebook.com",
        health_status=ProviderHealth.HEALTHY,
        last_latency_ms=120,
    )
    async_session.add(config)
    await async_session.commit()
    await async_session.refresh(config)

    stmt = select(ProviderConfig).where(ProviderConfig.id == config.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.name == "meta_whatsapp"
    assert loaded.channel == Channel.WHATSAPP
    assert loaded.priority == 0
    assert loaded.base_url == "https://graph.facebook.com"
    assert loaded.health_status == ProviderHealth.HEALTHY
    assert loaded.last_latency_ms == 120
    assert loaded.active is True
    assert loaded.consecutive_failures == 0
    assert loaded.consecutive_successes == 0
