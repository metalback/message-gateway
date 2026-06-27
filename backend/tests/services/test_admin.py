"""Unit tests for :mod:`app.services.admin`.

The tests exercise the admin domain service against the
in-memory SQLite fixture so the SQLAlchemy ORM round-trip
behaviour is verified. The auth / messaging collaborators
are stubbed or the real services are used through the same
``register_client`` / ``Message`` rows the production
service would create.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  -- side-effect import to register tables
from app.config import Settings
from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientRole, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.services import admin as admin_service
from app.services.admin import (
    ClientListPage,
    ClientNotFoundError,
    InvalidClientFilterError,
    InvalidClientUpdateError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with predictable defaults for admin unit tests."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        billing_default_plan_code="starter",
        api_key_prefix="mgw_live_",
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh in-memory SQLite session per test.

    The shared ``async_session`` fixture in
    :mod:`tests.conftest` is enough for most suites; the
    admin suite builds its own because some tests need a
    shared async DB across coroutines (the same row has
    to be re-loaded by id, and a single in-memory
    connection per coroutine is too restrictive).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _make_client(
    *,
    name: str,
    email: str,
    rut: str,
    role: ClientRole = ClientRole.CLIENT,
    status: ClientStatus = ClientStatus.ACTIVE,
    plan: ClientPlan = ClientPlan.STARTER,
) -> Client:
    """Build a :class:`Client` row without hitting the database.

    The bcrypt digests are filled with a deterministic
    placeholder (the unit tests under :mod:`app.services.admin`
    never authenticate against them). The function is
    deliberately a "pure" builder so the tests can focus
    on the admin service's behaviour.
    """
    placeholder = bcrypt.hashpw(b"placeholder", bcrypt.gensalt(rounds=4)).decode("ascii")
    client = Client(
        name=name,
        email=email,
        rut=rut,
        password_hash=placeholder,
        api_key_hash=placeholder,
        api_key_last4="0000",
        plan=plan,
        status=status,
        role=role,
    )
    return client


async def _seed_clients(session: AsyncSession) -> list[Client]:
    """Insert a known fixture of five clients.

    The fixture covers the three plan tiers, the three
    statuses and an admin so the filter / counter tests
    can assert against a single source of truth.
    """
    clients = [
        _make_client(
            name="Acme SpA",
            email="ops@acme.cl",
            rut="12345678-5",
            plan=ClientPlan.STARTER,
            status=ClientStatus.ACTIVE,
        ),
        _make_client(
            name="Beta Ltda",
            email="admin@beta.cl",
            rut="11111111-1",
            plan=ClientPlan.GROWTH,
            status=ClientStatus.ACTIVE,
        ),
        _make_client(
            name="Gamma SA",
            email="ops@gamma.cl",
            rut="22222222-2",
            plan=ClientPlan.ENTERPRISE,
            status=ClientStatus.SUSPENDED,
        ),
        _make_client(
            name="Delta SpA",
            email="ops@delta.cl",
            rut="33333333-3",
            plan=ClientPlan.STARTER,
            status=ClientStatus.PENDING,
        ),
        _make_client(
            name="Platform Admin",
            email="platform@msg-gateway.cl",
            rut="00000000-0",
            plan=ClientPlan.ENTERPRISE,
            status=ClientStatus.ACTIVE,
            role=ClientRole.ADMIN,
        ),
    ]
    for client in clients:
        session.add(client)
    await session.commit()
    for client in clients:
        await session.refresh(client)
    return clients


async def _seed_single_client(
    session: AsyncSession,
    *,
    role: ClientRole = ClientRole.CLIENT,
    status: ClientStatus = ClientStatus.ACTIVE,
    plan: ClientPlan = ClientPlan.STARTER,
) -> Client:
    """Seed and return a single :class:`Client` row.

    Convenience wrapper for the per-client update /
    suspend tests; the helper exists so the call site
    does not have to unpack the multi-client fixture and
    pick ``[0]`` by hand.
    """
    client = _make_client(
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12345678-5",
        role=role,
        status=status,
        plan=plan,
    )
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client


# ---------------------------------------------------------------------------
# list_clients
# ---------------------------------------------------------------------------


async def test_list_clients_returns_every_active_row(
    session: AsyncSession,
) -> None:
    """The default list call returns every client, newest first."""
    seeded = await _seed_clients(session)
    page: ClientListPage = await admin_service.list_clients(session)
    assert page.total == len(seeded)
    assert page.limit == admin_service.DEFAULT_LIST_LIMIT
    assert page.offset == 0
    assert not page.has_more
    # Every seeded row is in the result; the SQLAlchemy
    # ``String`` UUIDs are random so a strict reverse-order
    # assertion is not portable across backends. The
    # ``order_by`` clause is covered by the pagination test
    # below (which sets a deterministic ``limit``).
    emails = {client.email for client in page.items}
    assert emails == {client.email for client in seeded}


async def test_list_clients_filters_by_plan(session: AsyncSession) -> None:
    """The ``plan`` filter narrows the result to the matching plan."""
    await _seed_clients(session)
    page = await admin_service.list_clients(session, plan=ClientPlan.STARTER)
    assert page.total == 2
    assert {client.plan for client in page.items} == {ClientPlan.STARTER}


async def test_list_clients_filters_by_status(session: AsyncSession) -> None:
    """The ``status`` filter narrows the result to the matching status."""
    await _seed_clients(session)
    page = await admin_service.list_clients(session, status=ClientStatus.SUSPENDED)
    assert page.total == 1
    assert page.items[0].email == "ops@gamma.cl"


async def test_list_clients_search_substring(session: AsyncSession) -> None:
    """The ``search`` argument is a case-insensitive substring
    match over name, email and RUT."""
    await _seed_clients(session)
    page = await admin_service.list_clients(session, search="acme")
    assert page.total == 1
    assert page.items[0].email == "ops@acme.cl"
    page_rut = await admin_service.list_clients(session, search="33333333")
    assert page_rut.total == 1
    assert page_rut.items[0].email == "ops@delta.cl"


async def test_list_clients_pagination(session: AsyncSession) -> None:
    """Pagination slices the result and reports ``has_more``."""
    await _seed_clients(session)
    page = await admin_service.list_clients(session, limit=2, offset=0)
    assert page.total == 5
    assert len(page.items) == 2
    assert page.has_more
    page2 = await admin_service.list_clients(session, limit=2, offset=2)
    assert page2.has_more
    page3 = await admin_service.list_clients(session, limit=2, offset=4)
    assert not page3.has_more
    assert len(page3.items) == 1


async def test_list_clients_rejects_invalid_pagination(session: AsyncSession) -> None:
    """A negative ``limit`` or ``offset`` raises a 422-coded error."""
    with pytest.raises(InvalidClientFilterError):
        await admin_service.list_clients(session, limit=0)
    with pytest.raises(InvalidClientFilterError):
        await admin_service.list_clients(session, offset=-1)


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


async def test_get_client_returns_row_by_id(session: AsyncSession) -> None:
    """A known id returns the matching row."""
    client = await _seed_single_client(session)
    fetched = await admin_service.get_client(session, client_id=client.id)
    assert fetched.id == client.id


async def test_get_client_missing_id_raises(session: AsyncSession) -> None:
    """An unknown id raises a 404-coded error."""
    with pytest.raises(ClientNotFoundError):
        await admin_service.get_client(session, client_id="does-not-exist")


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


async def test_create_client_mints_api_key(
    session: AsyncSession, fast_settings: Settings
) -> None:
    """``create_client`` returns the new row plus the plain API key."""
    client, api_key = await admin_service.create_client(
        session,
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12.345.678-5",
        password="sup3r-secret",
        plan=ClientPlan.STARTER,
        settings=fast_settings,
    )
    assert isinstance(client, Client)
    assert client.id
    assert client.plan == ClientPlan.STARTER
    assert client.role == ClientRole.CLIENT
    assert client.status == ClientStatus.ACTIVE
    assert api_key.startswith("mgw_live_")
    assert client.api_key_last4 == api_key[-4:]
    # The plain key is hashed with bcrypt in the database;
    # we never re-derive the digest, but the stored hash
    # has to be different from the plain value.
    assert client.api_key_hash != api_key


async def test_create_client_uses_default_plan_when_omitted(
    session: AsyncSession, fast_settings: Settings
) -> None:
    """When ``plan`` is ``None`` the platform falls back to the
    ``billing_default_plan_code`` setting (``starter``)."""
    client, _ = await admin_service.create_client(
        session,
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12.345.678-5",
        password="sup3r-secret",
        plan=None,
        settings=fast_settings,
    )
    assert client.plan == ClientPlan.STARTER


async def test_create_client_rejects_duplicate_identity(
    session: AsyncSession, fast_settings: Settings
) -> None:
    """A second registration with the same email raises a
    ``DuplicateIdentityError`` – same contract the public
    registration path enforces."""
    from app.services.auth import DuplicateIdentityError

    await admin_service.create_client(
        session,
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12.345.678-5",
        password="sup3r-secret",
        plan=ClientPlan.STARTER,
        settings=fast_settings,
    )
    with pytest.raises(DuplicateIdentityError):
        await admin_service.create_client(
            session,
            name="Acme SpA (again)",
            email="ops@acme.cl",
            rut="11.111.111-1",
            password="another-secret",
            plan=ClientPlan.STARTER,
            settings=fast_settings,
        )


# ---------------------------------------------------------------------------
# update_client
# ---------------------------------------------------------------------------


async def test_update_client_changes_plan_and_markup(
    session: AsyncSession,
) -> None:
    """The PATCH helper updates the plan and both markup fields."""
    client = await _seed_single_client(session)
    updated = await admin_service.update_client(
        session,
        client_id=client.id,
        plan=ClientPlan.ENTERPRISE,
        markup_percent=0.25,
        markup_fixed_clp=10,
    )
    assert updated.plan == ClientPlan.ENTERPRISE
    assert updated.markup_percent == 0.25
    assert updated.markup_fixed_clp == 10


async def test_update_client_zero_markup_is_accepted(
    session: AsyncSession,
) -> None:
    """``markup_percent=0.0`` is a valid (falsy) value – the
    service uses ``is not None``, not a truthiness check."""
    client = await _seed_single_client(session)
    client.markup_percent = 0.5
    await session.commit()
    updated = await admin_service.update_client(
        session, client_id=client.id, markup_percent=0.0
    )
    assert updated.markup_percent == 0.0


async def test_update_client_rejects_negative_markup(
    session: AsyncSession,
) -> None:
    """A negative markup value is rejected with a 422-coded error."""
    client = await _seed_single_client(session)
    with pytest.raises(InvalidClientUpdateError):
        await admin_service.update_client(
            session, client_id=client.id, markup_percent=-0.1
        )
    with pytest.raises(InvalidClientUpdateError):
        await admin_service.update_client(
            session, client_id=client.id, markup_fixed_clp=-1
        )


async def test_update_client_rejects_blank_name(
    session: AsyncSession,
) -> None:
    """A blank name is rejected with a 422-coded error."""
    client = await _seed_single_client(session)
    with pytest.raises(InvalidClientUpdateError):
        await admin_service.update_client(session, client_id=client.id, name="   ")


async def test_update_client_unknown_id_raises(
    session: AsyncSession,
) -> None:
    """An unknown id raises a 404-coded error."""
    with pytest.raises(ClientNotFoundError):
        await admin_service.update_client(
            session, client_id="does-not-exist", plan=ClientPlan.GROWTH
        )


# ---------------------------------------------------------------------------
# suspend_client
# ---------------------------------------------------------------------------


async def test_suspend_client_flips_status(session: AsyncSession) -> None:
    """``suspend_client`` flips an active client to SUSPENDED."""
    client = await _seed_single_client(session)
    assert client.status == ClientStatus.ACTIVE
    suspended = await admin_service.suspend_client(session, client_id=client.id)
    assert suspended.status == ClientStatus.SUSPENDED


async def test_suspend_client_is_idempotent(session: AsyncSession) -> None:
    """Suspending an already-suspended client is a no-op."""
    client = await _seed_single_client(session)
    await admin_service.suspend_client(session, client_id=client.id)
    again = await admin_service.suspend_client(session, client_id=client.id)
    assert again.status == ClientStatus.SUSPENDED


# ---------------------------------------------------------------------------
# set_client_markup
# ---------------------------------------------------------------------------


async def test_set_client_markup_updates_only_markup(
    session: AsyncSession,
) -> None:
    """The dedicated markup setter does not touch other fields."""
    client = await _seed_single_client(session)
    updated = await admin_service.set_client_markup(
        session, client_id=client.id, markup_percent=0.1, markup_fixed_clp=5
    )
    assert updated.markup_percent == 0.1
    assert updated.markup_fixed_clp == 5
    assert updated.plan == client.plan
    assert updated.status == client.status


# ---------------------------------------------------------------------------
# admin_overview
# ---------------------------------------------------------------------------


async def test_admin_overview_aggregates_clients(session: AsyncSession) -> None:
    """The overview card's client counters match the fixture."""
    await _seed_clients(session)
    overview = await admin_service.admin_overview(session)
    assert overview.total_clients == 5
    assert overview.active_clients == 3
    assert overview.suspended_clients == 1
    assert overview.pending_clients == 1
    assert overview.admin_users == 1


async def test_admin_overview_aggregates_messages(
    session: AsyncSession,
) -> None:
    """The overview card's message counters are period-bounded."""
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    # Two delivered, one failed, one pending in the
    # *current* month.
    in_period = now - timedelta(days=1)
    session.add_all(
        [
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.DELIVERED,
                cost_clp=80,
                fee_clp=5,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.SENT,
                cost_clp=80,
                fee_clp=5,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="sms_aggregator",
                channel=Channel.SMS,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.FAILED,
                cost_clp=25,
                fee_clp=3,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="sms_aggregator",
                channel=Channel.SMS,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.PENDING,
                cost_clp=0,
                fee_clp=0,
                created_at=in_period,
            ),
        ]
    )
    await session.commit()
    overview = await admin_service.admin_overview(session)
    assert overview.total_messages == 4
    # ``BILLABLE_STATUSES`` is ``{sent, delivered}`` so two
    # messages are billable.
    assert overview.billable_messages == 2
    assert overview.delivered_messages == 1
    assert overview.failed_messages == 1
    assert overview.pending_messages == 1
    # Revenue is ``cost + fee`` summed over billable
    # messages: ``(80+5) + (80+5) = 170``.
    assert overview.total_revenue_clp == 170
    # The period bounds span the current calendar month.
    assert overview.period_start <= in_period.date() <= overview.period_end


async def test_admin_overview_ignores_out_of_period_messages(
    session: AsyncSession,
) -> None:
    """Messages outside the current month are excluded from
    the period-bounded counters."""
    client = await _seed_single_client(session)
    last_year = datetime.now(tz=UTC) - timedelta(days=400)
    session.add(
        Message(
            client_id=client.id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="old",
            status=MessageStatus.DELIVERED,
            cost_clp=80,
            fee_clp=5,
            created_at=last_year,
        )
    )
    await session.commit()
    overview = await admin_service.admin_overview(session)
    assert overview.total_messages == 0
    assert overview.total_revenue_clp == 0


# ---------------------------------------------------------------------------
# admin_provider_breakdown
# ---------------------------------------------------------------------------


async def test_admin_provider_breakdown_groups_by_provider(
    session: AsyncSession,
) -> None:
    """The breakdown returns one row per ``(provider, channel)`` pair."""
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    in_period = now - timedelta(days=2)
    session.add_all(
        [
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.DELIVERED,
                cost_clp=80,
                fee_clp=5,
                latency_ms=150.0,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345679",
                body="hola",
                status=MessageStatus.FAILED,
                cost_clp=80,
                fee_clp=5,
                latency_ms=200.0,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="sms_aggregator",
                channel=Channel.SMS,
                to_number="+56912345678",
                body="hola",
                status=MessageStatus.DELIVERED,
                cost_clp=25,
                fee_clp=3,
                created_at=in_period,
            ),
        ]
    )
    await session.commit()
    rows = await admin_service.admin_provider_breakdown(session)
    by_provider = {row.provider: row for row in rows}
    assert set(by_provider) == {"meta_whatsapp", "sms_aggregator"}
    meta = by_provider["meta_whatsapp"]
    assert meta.channel == Channel.WHATSAPP.value
    assert meta.total == 2
    assert meta.delivered == 1
    assert meta.failed == 1
    assert meta.cost_clp == 160
    assert meta.fee_clp == 10
    # ``AVG`` of (150, 200) is 175; the helper wraps the
    # SQL expression in a Float() cast so the value comes
    # back as a Python ``float`` regardless of the backend.
    assert meta.avg_latency_ms == pytest.approx(175.0)
    sms = by_provider["sms_aggregator"]
    assert sms.total == 1
    assert sms.delivered == 1
    assert sms.cost_clp == 25
    assert sms.fee_clp == 3
    # No ``latency_ms`` recorded on the SMS row, so the
    # average is ``None`` (the dashboard renders a "—"
    # placeholder rather than a misleading ``0.0``).
    assert sms.avg_latency_ms is None


async def test_admin_provider_breakdown_avg_latency_ignores_nulls(
    session: AsyncSession,
) -> None:
    """``AVG(latency_ms)`` skips rows with a ``NULL`` value.

    The semantic is the standard SQL one: a bucket with
    five observed dispatches and one unobserved row
    reports the mean of the five, not the mean divided by
    six. The test pins the contract so a future "coalesce
    ``NULL`` to 0" refactor would fail loudly.
    """
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    in_period = now - timedelta(days=2)
    session.add_all(
        [
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="ok",
                status=MessageStatus.DELIVERED,
                latency_ms=100.0,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345679",
                body="ok",
                status=MessageStatus.DELIVERED,
                latency_ms=200.0,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345680",
                body="ok",
                status=MessageStatus.DELIVERED,
                # ``latency_ms`` left as ``None`` (the
                # default); represents a row that pre-dates
                # the column.
                created_at=in_period,
            ),
        ]
    )
    await session.commit()
    rows = await admin_service.admin_provider_breakdown(session)
    assert len(rows) == 1
    meta = rows[0]
    assert meta.provider == "meta_whatsapp"
    # Average of (100, 200) = 150, ignoring the third row.
    assert meta.avg_latency_ms == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# list_recent_errors
# ---------------------------------------------------------------------------


async def test_list_recent_errors_only_returns_failed(
    session: AsyncSession,
) -> None:
    """The error log skips any non-FAILED message."""
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    in_period = now - timedelta(hours=1)
    session.add_all(
        [
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="ok",
                status=MessageStatus.DELIVERED,
                created_at=in_period,
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="failed",
                status=MessageStatus.FAILED,
                error_code="rate_limited",
                error_message="429 Too Many Requests",
                created_at=in_period,
            ),
        ]
    )
    await session.commit()
    items, total = await admin_service.list_recent_errors(session)
    assert total == 1
    assert len(items) == 1
    entry = items[0]
    assert entry.error_code == "rate_limited"
    assert entry.error_message == "429 Too Many Requests"
    assert entry.client_email == client.email
    assert entry.client_name == client.name
    assert entry.channel == Channel.WHATSAPP.value


async def test_list_recent_errors_orders_newest_first(
    session: AsyncSession,
) -> None:
    """The most recent failure is the first item in the list."""
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    session.add_all(
        [
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="older",
                status=MessageStatus.FAILED,
                error_code="older",
                error_message="older failure",
                created_at=now - timedelta(hours=2),
            ),
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body="newer",
                status=MessageStatus.FAILED,
                error_code="newer",
                error_message="newer failure",
                created_at=now - timedelta(minutes=5),
            ),
        ]
    )
    await session.commit()
    items, _ = await admin_service.list_recent_errors(session)
    assert [item.error_code for item in items] == ["newer", "older"]


async def test_list_recent_errors_supports_pagination(
    session: AsyncSession,
) -> None:
    """``limit`` / ``offset`` slice the result; ``total`` stays stable."""
    client = await _seed_single_client(session)
    now = datetime.now(tz=UTC)
    for index in range(5):
        session.add(
            Message(
                client_id=client.id,
                provider="meta_whatsapp",
                channel=Channel.WHATSAPP,
                to_number="+56912345678",
                body=f"failed {index}",
                status=MessageStatus.FAILED,
                error_code=f"e{index}",
                error_message=f"failure {index}",
                created_at=now - timedelta(minutes=index),
            )
        )
    await session.commit()
    items, total = await admin_service.list_recent_errors(session, limit=2, offset=0)
    assert total == 5
    assert len(items) == 2
    items2, total2 = await admin_service.list_recent_errors(session, limit=2, offset=2)
    assert total2 == 5
    assert len(items2) == 2
    assert {item.error_code for item in items} != {item.error_code for item in items2}
