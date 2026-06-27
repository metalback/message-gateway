"""HTTP-level tests for the webhook subscription routes (issue #5).

The tests mount the real :class:`FastAPI` app on a vanilla
:class:`TestClient` and exercise ``/v1/webhooks/*``
end-to-end against an in-memory SQLite database. The
point is to assert the *observable* HTTP contract: status
codes, response shapes and header-driven dependencies –
not the internals of the service layer (covered by
:mod:`tests.services.test_webhooks`).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.config import Settings
from app.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with a low bcrypt cost so the suite stays fast."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        jwt_ttl_minutes=15,
        api_key_prefix="mgw_live_",
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
    )


@pytest.fixture
def app_with_db(monkeypatch, fast_settings):  # noqa: ANN001
    """Return a :class:`FastAPI` app whose DB engine points at
    an isolated in-memory SQLite database.

    Same pattern as the messages / billing tests: the
    fixture rebuilds the cached engine in :mod:`app.db`
    so the application's ``get_db`` dependency opens a
    fresh database for every test.
    """
    import app.models  # noqa: F401
    from app.models.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

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
def client(app_with_db) -> TestClient:  # noqa: ANN001
    return TestClient(app_with_db)


def _register(
    client: TestClient,
    *,
    email: str = "ops@acme.cl",
    rut: str = "12.345.678-5",
) -> dict[str, Any]:
    """Register a known-good client and return the parsed body."""
    response = client.post(
        "/v1/auth/register",
        json={
            "name": "Acme SpA",
            "email": email,
            "rut": rut,
            "password": "sup3r-secret",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# POST /v1/webhooks
# ---------------------------------------------------------------------------


def test_create_webhook_returns_201_with_secret(
    client: TestClient,
) -> None:
    """A well-formed request lands in the database and
    returns the freshly-minted HMAC secret exactly once."""
    body = _register(client)
    response = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["webhook"]["url"] == "https://example.com/hooks"
    assert payload["webhook"]["active"] is True
    # The default event set is "send me everything important".
    assert set(payload["webhook"]["events"]) == {
        "message.sent",
        "message.delivered",
        "message.failed",
    }
    # The secret is 64 hex characters (32 bytes) – the
    # OWASP minimum for HMAC-SHA256.
    assert len(payload["secret"]) == 64


def test_create_webhook_accepts_explicit_events(client: TestClient) -> None:
    """A caller can opt in to a subset of the event
    vocabulary; the response shape mirrors the request
    shape so round-tripping a value through the API is
    trivial."""
    body = _register(client)
    response = client.post(
        "/v1/webhooks",
        json={
            "url": "https://example.com/hooks",
            "events": ["message.delivered"],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 201
    assert response.json()["webhook"]["events"] == ["message.delivered"]


def test_create_webhook_422_on_non_https_url(client: TestClient) -> None:
    """A non-https URL is rejected at the service layer
    with a stable code – the receiver's TLS
    configuration is a hard requirement, not a hint."""
    body = _register(client)
    response = client.post(
        "/v1/webhooks",
        json={"url": "http://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_url"


def test_create_webhook_422_on_unknown_event(client: TestClient) -> None:
    """An event the platform does not know about is
    rejected at the service layer with a stable
    ``unknown_event`` code so a typo surfaces as a 422,
    not a silent drop at delivery time."""
    body = _register(client)
    response = client.post(
        "/v1/webhooks",
        json={
            "url": "https://example.com/hooks",
            "events": ["message.undelivered"],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_event"


def test_create_webhook_401_when_api_key_missing(client: TestClient) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable
    code so the caller can branch on ``code`` rather
    than parsing free-text ``detail``."""
    response = client.post(
        "/v1/webhooks", json={"url": "https://example.com/hooks"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


# ---------------------------------------------------------------------------
# GET /v1/webhooks
# ---------------------------------------------------------------------------


def test_list_webhooks_returns_empty_array_initially(
    client: TestClient,
) -> None:
    """A freshly-registered client has no subscriptions;
    the endpoint returns ``[]`` (not 404) so the
    dashboard can render an empty state without a
    special-case branch."""
    body = _register(client)
    response = client.get(
        "/v1/webhooks", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200
    assert response.json() == []


def test_list_webhooks_returns_created_subscriptions(
    client: TestClient,
) -> None:
    """A subscription created via ``POST`` is visible in
    the list immediately afterwards."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert create.status_code == 201
    response = client.get(
        "/v1/webhooks", headers={"X-API-Key": body["api_key"]}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == create.json()["webhook"]["id"]


def test_list_webhooks_only_returns_callers_own(
    client: TestClient,
) -> None:
    """Tenant isolation at the HTTP level: the listing
    never returns another client's subscriptions."""
    me = _register(client, email="me@acme.cl", rut="12.345.678-5")
    other = _register(client, email="other@acme.cl", rut="11.111.111-1")
    client.post(
        "/v1/webhooks",
        json={"url": "https://me.example.com/hooks"},
        headers={"X-API-Key": me["api_key"]},
    )
    client.post(
        "/v1/webhooks",
        json={"url": "https://other.example.com/hooks"},
        headers={"X-API-Key": other["api_key"]},
    )
    response = client.get(
        "/v1/webhooks", headers={"X-API-Key": me["api_key"]}
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["url"] == "https://me.example.com/hooks"


# ---------------------------------------------------------------------------
# GET /v1/webhooks/{id}
# ---------------------------------------------------------------------------


def test_get_webhook_returns_match(client: TestClient) -> None:
    """A known-good id resolves to the matching row."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    response = client.get(
        f"/v1/webhooks/{webhook_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["id"] == webhook_id


def test_get_webhook_does_not_expose_secret(client: TestClient) -> None:
    """The HMAC secret is returned by the POST response
    exactly once; the GET endpoint must not echo it
    back, so a stolen API key cannot recover the
    secret through a read-only path."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    original_secret = create.json()["secret"]
    response = client.get(
        f"/v1/webhooks/{webhook_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert "secret" not in response.json()
    # And the response shape does not collide with the
    # POST envelope (a regression that returned the
    # ``{"webhook": …}`` wrapper on GET would mask the
    # secret-omission property).
    assert isinstance(response.json()["id"], str)
    assert original_secret  # sanity – the POST did return a secret


def test_get_webhook_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 with a stable code."""
    body = _register(client)
    response = client.get(
        "/v1/webhooks/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_found"


def test_get_webhook_404_for_other_clients_row(
    client: TestClient,
) -> None:
    """A webhook that belongs to a different client is
    reported as not-found (not forbidden) so the
    existence of another tenant's resource is not
    leaked."""
    me = _register(client, email="me@acme.cl", rut="12.345.678-5")
    other = _register(client, email="other@acme.cl", rut="11.111.111-1")
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://other.example.com/hooks"},
        headers={"X-API-Key": other["api_key"]},
    )
    foreign_id = create.json()["webhook"]["id"]
    response = client.get(
        f"/v1/webhooks/{foreign_id}",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_found"


# ---------------------------------------------------------------------------
# PATCH /v1/webhooks/{id}
# ---------------------------------------------------------------------------


def test_patch_webhook_disables_subscription(
    client: TestClient,
) -> None:
    """A PATCH that flips ``active`` to ``False`` is the
    canonical "stop the noise" flow the dashboard
    wires up next to a failing endpoint."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    response = client.patch(
        f"/v1/webhooks/{webhook_id}",
        json={"active": False},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["active"] is False


def test_patch_webhook_changes_url(client: TestClient) -> None:
    """A PATCH that targets ``url`` swaps the destination
    endpoint; the new value is validated the same way
    the POST validates it."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://old.example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    response = client.patch(
        f"/v1/webhooks/{webhook_id}",
        json={"url": "https://new.example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["url"] == "https://new.example.com/hooks"


def test_patch_webhook_422_on_invalid_url(client: TestClient) -> None:
    """A PATCH that targets ``url`` re-validates the new
    value – a regression that only validates on create
    would let a customer migrate to a bad URL
    silently."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    response = client.patch(
        f"/v1/webhooks/{webhook_id}",
        json={"url": "http://insecure.example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_patch_webhook_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 with a stable code."""
    body = _register(client)
    response = client.patch(
        "/v1/webhooks/00000000-0000-0000-0000-000000000000",
        json={"active": False},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_found"


# ---------------------------------------------------------------------------
# DELETE /v1/webhooks/{id}
# ---------------------------------------------------------------------------


def test_delete_webhook_returns_204(client: TestClient) -> None:
    """A successful DELETE returns 204 (no body) and
    removes the row, so a subsequent GET surfaces the
    same 404 the service layer surfaces for any
    unknown id."""
    body = _register(client)
    create = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hooks"},
        headers={"X-API-Key": body["api_key"]},
    )
    webhook_id = create.json()["webhook"]["id"]
    response = client.delete(
        f"/v1/webhooks/{webhook_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 204
    # The row is gone.
    follow_up = client.get(
        f"/v1/webhooks/{webhook_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert follow_up.status_code == 404


def test_delete_webhook_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 with a stable code (the
    service layer is idempotent only at the
    application layer; a re-DELETE returns the same
    404 the GET does)."""
    body = _register(client)
    response = client.delete(
        "/v1/webhooks/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404


def test_delete_webhook_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code."""
    response = client.delete("/v1/webhooks/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"
