"""HTTP-level tests for the admin routes (issue #10).

The tests mount the real :class:`FastAPI` app on a vanilla
:class:`TestClient` and exercise the ``/v1/admin/*`` surface
end-to-end against an in-memory SQLite database. The point
is to assert the *observable* HTTP contract: status codes,
response shapes, query-string handling and the
admin-only authorisation guard. The domain service
behaviour is covered by :mod:`tests.services.test_admin`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.client import Client, ClientPlan, ClientRole, ClientStatus
from app.models.message import Channel, Message, MessageStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """``bcrypt_rounds=4`` keeps the suite under a second."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        billing_default_plan_code="starter",
        api_key_prefix="mgw_live_",
    )


@pytest.fixture
def app_with_db(monkeypatch, fast_settings):  # noqa: ANN001
    """Return a :class:`FastAPI` app whose DB engine points at
    an isolated in-memory SQLite database.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.db as db_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import app.models  # noqa: F401
    from app.models.base import Base

    async def _setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.run(_setup())

    async def _override_session():
        async with factory() as session:
            yield session

    test_app = create_app(fast_settings)
    test_app.dependency_overrides[db_module.get_db] = _override_session

    db_module._engine = None
    db_module._session_factory = None
    monkeypatch.setattr(db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    yield test_app

    async def _teardown() -> None:
        await engine.dispose()

    asyncio.run(_teardown())


@pytest.fixture
def client(app_with_db):  # noqa: ANN001
    return TestClient(app_with_db)


def _make_client_row(
    *,
    name: str,
    email: str,
    rut: str,
    role: ClientRole = ClientRole.CLIENT,
    status: ClientStatus = ClientStatus.ACTIVE,
    plan: ClientPlan = ClientPlan.STARTER,
    markup_percent: float = 0.0,
    markup_fixed_clp: int = 0,
) -> Client:
    """Build a :class:`Client` row in memory.

    The bcrypt digests are placeholders – the admin
    endpoints do not authenticate against them.
    """
    placeholder = bcrypt.hashpw(b"placeholder", bcrypt.gensalt(rounds=4)).decode("ascii")
    return Client(
        name=name,
        email=email,
        rut=rut,
        password_hash=placeholder,
        api_key_hash=placeholder,
        api_key_last4="0000",
        plan=plan,
        status=status,
        role=role,
        markup_percent=markup_percent,
        markup_fixed_clp=markup_fixed_clp,
    )


async def _seed_admin(async_session_factory) -> tuple[Client, str]:
    """Insert a fresh admin and return ``(client, plain_api_key)``.

    The plain key is the bcrypt hash of the ``api_key_hash``
    column concatenated with the prefix – the test treats
    the placeholder as opaque. We use the
    :func:`app.services.admin.create_client` helper for
    the regular customer and a hand-rolled insert for
    the admin (the public registration path defaults to
    :attr:`ClientRole.CLIENT`).
    """
    from app.services.auth import _bcrypt_hash, _generate_api_key

    cfg = Settings(bcrypt_rounds=4, api_key_prefix="mgw_live_")
    api_key = _generate_api_key(cfg)
    admin = Client(
        name="Platform Admin",
        email="platform@msg-gateway.cl",
        rut="00000000-0",
        password_hash=_bcrypt_hash("admin-pass", settings=cfg),
        api_key_hash=_bcrypt_hash(api_key, settings=cfg),
        api_key_last4=api_key[-4:],
        plan=ClientPlan.ENTERPRISE,
        status=ClientStatus.ACTIVE,
        role=ClientRole.ADMIN,
    )
    regular = _make_client_row(
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12345678-5",
    )
    suspended = _make_client_row(
        name="Gamma SA",
        email="ops@gamma.cl",
        rut="22222222-2",
        status=ClientStatus.SUSPENDED,
    )
    async with async_session_factory() as session:
        session.add_all([admin, regular, suspended])
        await session.commit()
    return admin, api_key


async def _seed_messages(async_session_factory, *, client_id: str) -> None:
    """Insert two FAILED messages and one DELIVERED message."""
    now = datetime.now(tz=UTC)
    rows = [
        Message(
            client_id=client_id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="failed-1",
            status=MessageStatus.FAILED,
            error_code="rate_limited",
            error_message="429 Too Many Requests",
            created_at=now - timedelta(minutes=5),
        ),
        Message(
            client_id=client_id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="failed-2",
            status=MessageStatus.FAILED,
            error_code="invalid_number",
            error_message="+56999999999 is not a valid Chilean mobile",
            created_at=now - timedelta(minutes=10),
        ),
        Message(
            client_id=client_id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="ok",
            status=MessageStatus.DELIVERED,
            created_at=now - timedelta(minutes=15),
        ),
    ]
    async with async_session_factory() as session:
        session.add_all(rows)
        await session.commit()


@pytest.fixture
def admin_headers(client, monkeypatch):  # noqa: ANN001
    """Yield a dict with the ``X-API-Key`` header for the seeded admin.

    The fixture seeds one admin + two regular customers so
    the list / get / update endpoints have rows to work
    against. The plain API key is also stored on the test
    client as ``client.api_key`` so a test that needs the
    value directly (rather than as a header) can read it
    off the fixture object.
    """

    import app.db as db_module

    factory = db_module.get_session_factory()

    import asyncio

    admin, api_key = asyncio.run(_seed_admin(factory))
    asyncio.run(_seed_messages(factory, client_id=admin.id))
    client.admin_api_key = api_key
    return {"X-API-Key": api_key}


@pytest.fixture
def regular_headers(client):  # noqa: ANN001
    """Yield the headers for a freshly-registered non-admin client.

    The fixture uses the public ``/v1/auth/register`` flow
    so the test exercises the same code path production
    goes through.
    """
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Acme SpA",
            "email": "ops@acme.cl",
            "rut": "12.345.678-5",
            "password": "sup3r-secret",
        },
    )
    assert response.status_code == 201, response.text
    api_key = response.json()["api_key"]
    return {"X-API-Key": api_key}


# ---------------------------------------------------------------------------
# Authorisation guard
# ---------------------------------------------------------------------------


def test_admin_endpoints_require_api_key(client) -> None:
    """A request with no API key is a 401 (same contract as
    the rest of the platform)."""
    response = client.get("/v1/admin/clients")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_admin_endpoints_reject_non_admin(client, regular_headers) -> None:
    """A regular customer gets a 403 with the ``admin_required`` code."""
    response = client.get("/v1/admin/clients", headers=regular_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


# ---------------------------------------------------------------------------
# GET /v1/admin/clients
# ---------------------------------------------------------------------------


def test_list_clients_returns_paginated_payload(client, admin_headers) -> None:
    """The list endpoint returns the seeded clients and the
    pagination envelope."""
    response = client.get("/v1/admin/clients", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3  # admin + 2 regulars
    assert body["limit"] == admin_headers_default_limit()
    assert body["offset"] == 0
    assert not body["has_more"]
    assert {item["email"] for item in body["items"]} == {
        "platform@msg-gateway.cl",
        "ops@acme.cl",
        "ops@gamma.cl",
    }
    # The admin row carries ``role == admin`` and the
    # customer rows carry ``role == client``.
    roles = {item["email"]: item["role"] for item in body["items"]}
    assert roles["platform@msg-gateway.cl"] == "admin"
    assert roles["ops@acme.cl"] == "client"


def test_list_clients_filters_by_status(client, admin_headers) -> None:
    """The ``status`` query parameter narrows the result."""
    response = client.get(
        "/v1/admin/clients", params={"status": "suspended"}, headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "ops@gamma.cl"


def test_list_clients_filters_by_plan(client, admin_headers) -> None:
    """The ``plan`` query parameter narrows the result."""
    response = client.get(
        "/v1/admin/clients", params={"plan": "enterprise"}, headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "platform@msg-gateway.cl"


def test_list_clients_search_substring(client, admin_headers) -> None:
    """The ``q`` query parameter is a substring match over name/email/rut."""
    response = client.get(
        "/v1/admin/clients", params={"q": "gamma"}, headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "ops@gamma.cl"


def test_list_clients_pagination(client, admin_headers) -> None:
    """``limit`` / ``offset`` slice the result; ``has_more``
    reports whether another page is available."""
    page1 = client.get(
        "/v1/admin/clients", params={"limit": 2, "offset": 0}, headers=admin_headers
    ).json()
    assert page1["total"] == 3
    assert page1["has_more"]
    page2 = client.get(
        "/v1/admin/clients", params={"limit": 2, "offset": 2}, headers=admin_headers
    ).json()
    assert not page2["has_more"]


def test_list_clients_invalid_limit_returns_422(client, admin_headers) -> None:
    """A non-positive ``limit`` is rejected by Pydantic
    before the handler runs."""
    response = client.get(
        "/v1/admin/clients", params={"limit": 0}, headers=admin_headers
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/admin/clients/{id}
# ---------------------------------------------------------------------------


def test_get_client_returns_row(client, admin_headers) -> None:
    """A known id returns the matching client payload."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@acme.cl"
    )
    response = client.get(
        f"/v1/admin/clients/{target['id']}", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "ops@acme.cl"
    assert body["plan"] == "starter"
    assert body["status"] == "active"
    assert body["role"] == "client"
    assert body["markup_percent"] == 0.0
    assert body["markup_fixed_clp"] == 0


def test_get_client_unknown_id_returns_404(client, admin_headers) -> None:
    """An unknown id is a 404 with a stable error code."""
    response = client.get(
        "/v1/admin/clients/does-not-exist", headers=admin_headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "client_not_found"


# ---------------------------------------------------------------------------
# POST /v1/admin/clients
# ---------------------------------------------------------------------------


def test_create_client_returns_201_and_api_key(client, admin_headers) -> None:
    """The create endpoint returns the new row plus the
    plain API key exactly once."""
    response = client.post(
        "/v1/admin/clients",
        json={
            "name": "New Customer",
            "email": "new@example.cl",
            "rut": "11.111.111-1",
            "password": "sup3r-secret",
            "plan": "growth",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["client"]["email"] == "new@example.cl"
    assert body["client"]["plan"] == "growth"
    assert body["client"]["role"] == "client"
    assert body["api_key"].startswith("mgw_live_")
    assert body["api_key_last4"] == body["api_key"][-4:]


def test_create_client_uses_default_plan_when_omitted(client, admin_headers) -> None:
    """Without a ``plan`` the platform falls back to the
    ``billing_default_plan_code`` setting (``starter``)."""
    response = client.post(
        "/v1/admin/clients",
        json={
            "name": "Default Plan Customer",
            "email": "default@example.cl",
            "rut": "33.333.333-3",
            "password": "sup3r-secret",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["client"]["plan"] == "starter"


def test_create_client_409_on_duplicate_email(client, admin_headers) -> None:
    """A duplicate email surfaces a 409 with a stable code."""
    payload = {
        "name": "Acme SpA",
        "email": "ops@acme.cl",
        "rut": "12.345.678-5",
        "password": "sup3r-secret",
    }
    response = client.post(
        "/v1/admin/clients", json=payload, headers=admin_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate_identity"


# ---------------------------------------------------------------------------
# PATCH /v1/admin/clients/{id}
# ---------------------------------------------------------------------------


def test_update_client_changes_plan_and_markup(client, admin_headers) -> None:
    """The PATCH endpoint updates the plan and markup fields."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@acme.cl"
    )
    response = client.patch(
        f"/v1/admin/clients/{target['id']}",
        json={
            "plan": "enterprise",
            "markup_percent": 0.25,
            "markup_fixed_clp": 10,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"] == "enterprise"
    assert body["markup_percent"] == 0.25
    assert body["markup_fixed_clp"] == 10


def test_update_client_partial_payload(client, admin_headers) -> None:
    """A PATCH with only one field leaves the other fields untouched."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@acme.cl"
    )
    response = client.patch(
        f"/v1/admin/clients/{target['id']}",
        json={"status": "suspended"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "suspended"
    assert body["plan"] == target["plan"]


def test_update_client_rejects_blank_name(client, admin_headers) -> None:
    """A blank name is a 422 with a stable code."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@acme.cl"
    )
    response = client.patch(
        f"/v1/admin/clients/{target['id']}",
        json={"name": "   "},
        headers=admin_headers,
    )
    assert response.status_code == 422
    # The service emits the more specific ``invalid_name``
    # code so the dashboard can render a targeted error
    # without parsing the message.
    assert response.json()["detail"]["code"] == "invalid_name"


# ---------------------------------------------------------------------------
# POST /v1/admin/clients/{id}/suspend
# ---------------------------------------------------------------------------


def test_suspend_client_flips_status(client, admin_headers) -> None:
    """The suspend endpoint flips an active client to suspended."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@acme.cl"
    )
    response = client.post(
        f"/v1/admin/clients/{target['id']}/suspend", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["client"]["status"] == "suspended"


def test_suspend_client_is_idempotent(client, admin_headers) -> None:
    """Suspending an already-suspended client is a 200 (no-op)."""
    list_response = client.get("/v1/admin/clients", headers=admin_headers).json()
    target = next(
        item for item in list_response["items"] if item["email"] == "ops@gamma.cl"
    )
    response = client.post(
        f"/v1/admin/clients/{target['id']}/suspend", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json()["client"]["status"] == "suspended"


# ---------------------------------------------------------------------------
# GET /v1/admin/stats/overview
# ---------------------------------------------------------------------------


def test_admin_overview_returns_counters(client, admin_headers) -> None:
    """The overview endpoint exposes the aggregate counters."""
    response = client.get("/v1/admin/stats/overview", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total_clients"] == 3
    assert body["active_clients"] == 2
    assert body["suspended_clients"] == 1
    assert body["pending_clients"] == 0
    assert body["admin_users"] == 1
    # The seeded admin row carries two FAILED messages and
    # one DELIVERED message in the current month.
    assert body["failed_messages"] == 2
    assert body["delivered_messages"] == 1
    assert body["total_messages"] == 3
    assert body["billable_messages"] == 1


# ---------------------------------------------------------------------------
# GET /v1/admin/stats/by-provider
# ---------------------------------------------------------------------------


def test_admin_provider_breakdown_groups_by_provider(
    client, admin_headers
) -> None:
    """The provider breakdown returns one row per ``(provider, channel)``."""
    response = client.get(
        "/v1/admin/stats/by-provider", headers=admin_headers
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "meta_whatsapp"
    assert row["channel"] == "whatsapp"
    assert row["total"] == 3
    assert row["delivered"] == 1
    assert row["failed"] == 2
    # The ``avg_latency_ms`` field is part of the wire
    # contract; the seeded messages do not record a
    # latency (the fixture predates the column), so the
    # field is exposed as ``null`` rather than omitted.
    assert "avg_latency_ms" in row
    assert row["avg_latency_ms"] is None


# ---------------------------------------------------------------------------
# GET /v1/admin/logs
# ---------------------------------------------------------------------------


def test_admin_logs_returns_only_failures(client, admin_headers) -> None:
    """The error log only includes FAILED messages."""
    response = client.get("/v1/admin/logs", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    error_codes = {item["error_code"] for item in body["items"]}
    assert error_codes == {"rate_limited", "invalid_number"}
    # Every entry carries the owning client's metadata
    # so the operator does not have to do a second lookup.
    assert all(item["client_name"] == "Platform Admin" for item in body["items"])
    assert all(item["provider"] == "meta_whatsapp" for item in body["items"])


def test_admin_logs_pagination(client, admin_headers) -> None:
    """``limit`` / ``offset`` slice the result; ``has_more``
    reports whether another page is available."""
    page1 = client.get(
        "/v1/admin/logs", params={"limit": 1, "offset": 0}, headers=admin_headers
    ).json()
    assert page1["total"] == 2
    assert page1["has_more"]
    assert len(page1["items"]) == 1
    page2 = client.get(
        "/v1/admin/logs", params={"limit": 1, "offset": 1}, headers=admin_headers
    ).json()
    assert not page2["has_more"]


# ---------------------------------------------------------------------------
# GET /v1/admin/providers/health
# ---------------------------------------------------------------------------


def test_admin_providers_health_returns_empty_list_for_no_rows(
    client, admin_headers
) -> None:
    """A fresh deployment with no provider health
    snapshots returns an empty list – the dashboard
    renders "no data yet" until the periodic worker
    has had a chance to probe at least one upstream.
    The endpoint is admin-only and surfaces the
    empty state explicitly rather than 404-ing."""
    response = client.get(
        "/v1/admin/providers/health", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json() == []


def test_admin_providers_health_returns_seeded_rows(
    client, admin_headers
) -> None:
    """After seeding two :class:`ProviderConfig` rows
    the endpoint returns both, sorted by
    ``(channel, priority, name)`` so the dashboard
    renders the providers in routing order. The test
    pins the column-projection contract: every
    field the admin dashboard needs is present in
    the response, no extra columns leak."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig, ProviderHealth

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add_all(
                [
                    ProviderConfig(
                        name="meta_whatsapp",
                        channel=Channel.WHATSAPP,
                        priority=0,
                        health_status=ProviderHealth.HEALTHY,
                        last_latency_ms=120,
                    ),
                    ProviderConfig(
                        name="sms_aggregator",
                        channel=Channel.SMS,
                        priority=0,
                        health_status=ProviderHealth.DEGRADED,
                        last_latency_ms=350,
                    ),
                ]
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.get(
        "/v1/admin/providers/health", headers=admin_headers
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    # SMS sorts before WhatsApp alphabetically; the
    # two WhatsApp-style providers at priority 0
    # would tiebreak by name (only one here).
    assert rows[0]["channel"] == "sms"
    assert rows[1]["channel"] == "whatsapp"
    for row in rows:
        assert set(row.keys()) == {
            "name",
            "channel",
            "health_status",
            "last_health_check",
            "last_latency_ms",
            "consecutive_failures",
            "consecutive_successes",
            "active",
            "priority",
        }


def test_admin_providers_health_rejects_non_admin(
    client, regular_headers
) -> None:
    """A non-admin caller gets the standard 403 – the
    endpoint does not leak provider health to a
    regular customer."""
    response = client.get(
        "/v1/admin/providers/health", headers=regular_headers
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/admin/providers/routing-log
# ---------------------------------------------------------------------------


def test_admin_routing_log_returns_empty_list_for_no_rows(
    client, admin_headers
) -> None:
    """A fresh deployment with no audit history returns
    an empty list (not 404). The dashboard treats the
    empty state as "the worker has not logged anything
    yet" rather than an error."""
    response = client.get(
        "/v1/admin/providers/routing-log", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_admin_routing_log_filters_by_message_id(
    client, admin_headers
) -> None:
    """The optional ``message_id`` query parameter is
    the per-message trace view: only the attempts the
    chain made for a single message come back. The
    test seeds two :class:`RoutingLog` rows for one
    message and one for another, then asserts the
    filter narrows the response to the right subset."""
    import asyncio
    import uuid

    import app.db as db_module
    from app.models.message import Channel
    from app.models.routing_log import RoutingLog, RoutingLogOutcome

    factory = db_module.get_session_factory()
    target = str(uuid.uuid4())
    other = str(uuid.uuid4())

    async def _seed() -> None:
        async with factory() as session:
            session.add_all(
                [
                    RoutingLog(
                        message_id=target,
                        provider_attempted="meta_whatsapp",
                        channel=Channel.WHATSAPP,
                        outcome=RoutingLogOutcome.FAILURE,
                        latency_ms=10,
                        error_code="provider_unavailable",
                        error_message="meta 5xx",
                    ),
                    RoutingLog(
                        message_id=target,
                        provider_attempted="twilio_whatsapp",
                        channel=Channel.WHATSAPP,
                        outcome=RoutingLogOutcome.SUCCESS,
                        latency_ms=15,
                    ),
                    RoutingLog(
                        message_id=other,
                        provider_attempted="sms_aggregator",
                        channel=Channel.SMS,
                        outcome=RoutingLogOutcome.SUCCESS,
                        latency_ms=8,
                    ),
                ]
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.get(
        "/v1/admin/providers/routing-log",
        params={"message_id": target},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    providers = {item["provider"] for item in body["items"]}
    assert providers == {"meta_whatsapp", "twilio_whatsapp"}
    for item in body["items"]:
        assert item["message_id"] == target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def admin_headers_default_limit() -> int:
    """Return the default page size the ``list_clients`` endpoint uses.

    Mirrors :data:`app.services.admin.DEFAULT_LIST_LIMIT`
    without importing the value (the constant is internal
    to the service layer).
    """
    from app.services import admin as admin_service

    return admin_service.DEFAULT_LIST_LIMIT


# ---------------------------------------------------------------------------
# POST /v1/admin/providers/{name}/active (issue #11)
# ---------------------------------------------------------------------------
#
# The endpoint is the operator's "desactivar / activar"
# button on the admin dashboard. The tests below pin the
# observable HTTP contract: status code, response body
# shape, idempotence and the admin-only authorisation
# guard.


def test_set_provider_active_disables_existing_provider(
    client, admin_headers
) -> None:
    """Posting ``{"active": false}`` flips a previously
    enabled provider to disabled and returns the
    post-update snapshot. The dashboard uses the
    response to refresh the row in place without
    re-issuing the ``GET /v1/admin/providers/health``
    call."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.post(
        "/v1/admin/providers/meta_whatsapp/active",
        json={"active": False},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "meta_whatsapp"
    assert body["active"] is False


def test_set_provider_active_re_enables_provider(client, admin_headers) -> None:
    """Posting ``{"active": true}`` on a previously
    disabled provider flips the flag back. The round-
    trip pins the contract that a disabled provider
    can always be re-enabled through the same
    endpoint (no separate "re-enable" route)."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=False,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.post(
        "/v1/admin/providers/meta_whatsapp/active",
        json={"active": True},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "meta_whatsapp"
    assert body["active"] is True


def test_set_provider_active_auto_creates_row_when_missing(
    client, admin_headers
) -> None:
    """A provider that has never been probed (no
    :class:`ProviderConfig` row) is auto-created on
    the first toggle so the operator does not have
    to race the health worker. The dashboard sees a
    fresh row with ``health_status="unknown"`` and
    the requested target state."""
    response = client.post(
        "/v1/admin/providers/twilio_whatsapp/active",
        json={"active": False},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "twilio_whatsapp"
    assert body["active"] is False
    assert body["health_status"] == "unknown"


def test_set_provider_active_rejects_non_admin(
    client, regular_headers
) -> None:
    """A regular customer gets the standard 403 –
    only admins can flip the kill-switch. The test
    pins the authorisation guard so a future
    refactor does not accidentally leak the
    endpoint to a non-admin."""
    response = client.post(
        "/v1/admin/providers/meta_whatsapp/active",
        json={"active": False},
        headers=regular_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


def test_set_provider_active_rejects_missing_body_field(
    client, admin_headers
) -> None:
    """A body without the ``active`` key is a 422 –
    the helper does not have to guess whether the
    operator meant "turn it on" or "leave it alone"."""
    response = client.post(
        "/v1/admin/providers/meta_whatsapp/active",
        json={},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_set_provider_active_is_idempotent(client, admin_headers) -> None:
    """Posting the same target state twice is a no-op:
    the response shape is identical, the database
    holds exactly one row for the provider, and no
    duplicate is created. The idempotence keeps the
    operator's "re-click" actions cheap."""
    import asyncio

    from sqlalchemy import func, select

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    for _ in range(2):
        response = client.post(
            "/v1/admin/providers/meta_whatsapp/active",
            json={"active": False},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is False

    # Exactly one row in the table for the provider.
    async def _count() -> int:
        async with factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(ProviderConfig.id)).where(
                            ProviderConfig.name == "meta_whatsapp"
                        )
                    )
                ).scalar_one()
            )

    assert asyncio.run(_count()) == 1


# ---------------------------------------------------------------------------
# POST /v1/admin/providers/{name}/toggle (issue #11)
# ---------------------------------------------------------------------------
#
# The toggle endpoint is the dashboard's "switch" button:
# it reads the current state and POSTs the opposite. The
# tests below pin the read-then-write contract and the
# same authorisation / idempotence guarantees the
# target-state endpoint provides.


def test_toggle_provider_flips_inactive_to_active(client, admin_headers) -> None:
    """A provider that starts as ``active=False`` is
    flipped to ``active=True`` after a single POST.
    The response carries the post-update snapshot
    so the dashboard can refresh the row in place."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=False,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.post(
        "/v1/admin/providers/meta_whatsapp/toggle",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "meta_whatsapp"
    assert body["active"] is True


def test_toggle_provider_flips_active_to_inactive(client, admin_headers) -> None:
    """A provider that starts as ``active=True`` is
    flipped to ``active=False`` after a single POST.
    The asymmetry with the previous test pins the
    read-then-write contract: the helper reads the
    current value and POSTs the opposite, regardless
    of which direction the operator starts from."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    response = client.post(
        "/v1/admin/providers/meta_whatsapp/toggle",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "meta_whatsapp"
    assert body["active"] is False


def test_toggle_provider_auto_creates_row_when_missing(client, admin_headers) -> None:
    """A never-seen provider is auto-created in the
    *disabled* state on the first toggle (the
    read-then-write helper sees no row and
    treats "no row" as "currently active"). The
    dashboard can then re-toggle to enable the
    provider without further setup."""
    response = client.post(
        "/v1/admin/providers/twilio_whatsapp/toggle",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "twilio_whatsapp"
    assert body["active"] is False


def test_toggle_provider_rejects_non_admin(client, regular_headers) -> None:
    """The toggle endpoint shares the admin-only
    authorisation guard. A regular customer gets
    a 403 with the standard ``admin_required``
    code."""
    response = client.post(
        "/v1/admin/providers/meta_whatsapp/toggle",
        headers=regular_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


def test_set_provider_active_response_shape_matches_health_endpoint(
    client, admin_headers
) -> None:
    """The kill-switch response is the same
    :class:`ProviderHealthResponse` shape the
    health endpoint returns. A test that
    exercises both endpoints and asserts the
    keys are equal pins the contract so the
    dashboard can render the row with a single
    component."""
    import asyncio

    import app.db as db_module
    from app.models.message import Channel
    from app.models.provider_config import ProviderConfig

    factory = db_module.get_session_factory()

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                ProviderConfig(
                    name="meta_whatsapp",
                    channel=Channel.WHATSAPP,
                    active=True,
                )
            )
            await session.commit()

    asyncio.run(_seed())

    health = client.get(
        "/v1/admin/providers/health", headers=admin_headers
    ).json()
    toggled = client.post(
        "/v1/admin/providers/meta_whatsapp/active",
        json={"active": True},
        headers=admin_headers,
    ).json()
    assert set(toggled.keys()) == set(health[0].keys())
