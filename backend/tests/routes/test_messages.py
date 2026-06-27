"""HTTP-level tests for the message-sending routes (issue #4).

The tests mount the real :class:`FastAPI` app on a vanilla
``TestClient` and exercise ``/v1/messages/*`` end-to-end
against an in-memory SQLite database. The point is to assert
the *observable* HTTP contract: status codes, response
shapes and header-driven dependencies – not the internals of
the service layer (covered by :mod:`tests.services.test_messaging`).

The provider HTTP layer is stubbed by patching the registry
to return :class:`FakeProvider` instances, so the suite never
opens a real TCP connection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.config import Settings
from app.main import create_app
from app.models.message import Channel

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    """A controllable :class:`BaseProvider` for the route tests.

    Mirrors the helper in
    :mod:`tests.services.test_messaging` but kept local
    because the route tests need to control ``raise`` /
    ``return`` separately for each test (the service tests
    drive a richer interaction through a shared fake).
    """

    def __init__(
        self,
        *,
        name: str,
        provider_msg_id: str = "fake-1",
        status: str = "sent",
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._provider_msg_id = provider_msg_id
        self._status = status
        self._error = error

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        if self._error is not None:
            raise self._error
        return SendResult(provider_msg_id=self._provider_msg_id, raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        return self._status


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
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """Patch the registry so :func:`get_provider` returns
    a single :class:`FakeProvider` for every channel.

    Tests can swap ``fake_provider._error`` /
    ``fake_provider._provider_msg_id`` between calls to
    drive the route layer through different branches.
    """
    import app.adapters.registry as registry

    instance = FakeProvider(name="fake")
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.WHATSAPP,
        lambda settings: instance,
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.SMS,
        lambda settings: instance,
    )
    return instance


@pytest.fixture
def app_with_db(monkeypatch, fast_settings):  # noqa: ANN001
    """Return a :class:`FastAPI` app whose DB engine points at
    an isolated in-memory SQLite database.

    Same pattern as the auth tests: the fixture rebuilds the
    cached engine in :mod:`app.db` so the application's
    ``get_db`` dependency opens a fresh database for every
    test.
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


def _register(client: TestClient) -> dict[str, Any]:
    """Register a known-good client and return the parsed body."""
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
    return response.json()


# ---------------------------------------------------------------------------
# POST /v1/messages
# ---------------------------------------------------------------------------


def test_send_message_returns_202_with_message(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A well-formed request lands in the database, calls the
    provider and returns the persisted row."""
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["message"]["status"] == "sent"
    assert payload["message"]["to_number"] == "+56912345678"
    assert payload["message"]["provider_msg_id"] == "fake-1"
    assert payload["message"]["channel"] == "sms"
    assert payload["message"]["cost_clp"] == 25
    assert payload["message"]["fee_clp"] == 5


def test_send_message_401_when_api_key_missing(client: TestClient) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code so
    the caller can branch on ``code`` rather than parsing
    free-text ``detail``."""
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_send_message_401_for_invalid_api_key(client: TestClient) -> None:
    """A bogus key is rejected with the same 401 contract as
    the auth endpoints – the test guards against a
    regression that would differentiate the two error
    responses and accidentally leak which key prefix is
    valid."""
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": "mgw_live_bogus"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


def test_send_message_422_on_invalid_destination(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A non-Chilean number is rejected at the service layer
    with a stable code, surfacing as a 422 to the caller."""
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "not-a-number", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_destination"


def test_send_message_422_on_unknown_channel(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A channel the platform does not know about is rejected
    by Pydantic validation with a 422 before the service
    layer is involved."""
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "carrier-pigeon", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_send_message_records_failure_on_provider_unavailable(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A :class:`ProviderUnavailableError` from the upstream
    is recorded on the row as ``failed`` but the API still
    returns ``202 Accepted`` – the contract is "the
    message was accepted, the upstream rejected it". The
    caller can read the failure through
    ``GET /v1/messages/{id}``.

    Returning ``502`` here would mislead the caller: the
    platform itself is healthy, the row is durable, and a
    retry would create a duplicate. The 202-with-status
    pattern is what the PRD user story #16 asks for
    ("recibir una respuesta inmediata con un message_id").
    """
    fake_provider._error = ProviderUnavailableError("down", provider="fake")
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["message"]["status"] == "failed"
    assert payload["message"]["error_code"] == "provider_unavailable"
    assert payload["message"]["error_message"] == "down"
    assert payload["message"]["provider_msg_id"] is None


def test_send_message_records_failure_on_provider_validation(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A :class:`ProviderValidationError` is recorded on the
    row as ``failed`` with the upstream's error code – the
    same contract as the ``unavailable`` path so the caller
    can rely on a single error-handling shape."""
    fake_provider._error = ProviderValidationError("bad number", provider="fake")
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["message"]["status"] == "failed"
    assert payload["message"]["error_code"] == "provider_validation"
    assert payload["message"]["error_message"] == "bad number"


def test_send_message_records_failure_on_provider_rate_limited(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A :class:`ProviderRateLimitError` follows the same
    202-with-failed-status contract as the unavailable /
    validation paths: the row is durable, the caller gets a
    ``message_id`` immediately and a worker can read the
    rate-limit hint from ``error_code`` to back off. The
    test guards against a refactor that would surface the
    429 to the caller (a worse outcome: the row is durable,
    surfacing 429 would mislead the dashboard)."""
    from app.adapters.errors import ProviderRateLimitError

    fake_provider._error = ProviderRateLimitError(
        "throttled", provider="fake", retry_after=1.5
    )
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["message"]["status"] == "failed"
    assert payload["message"]["error_code"] == "provider_rate_limited"
    assert payload["message"]["error_message"] == "throttled"


def test_send_message_422_on_body_too_long(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A body longer than the channel's per-message cap is
    rejected at the service layer with a stable ``body_too_long``
    code. Pydantic lets the payload through (it accepts up to
    4096 chars) so the per-channel cap (1600 for SMS) is
    enforced by the service layer – the test pins the
    end-to-end contract so a refactor cannot regress it."""
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={
            "channel": "sms",
            "to": "+56912345678",
            "body": "a" * 2000,  # > SMS cap (1600), < Pydantic cap (4096)
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "body_too_long"
    # The provider must not be called: the service layer
    # rejects the request before it ever reaches the upstream.
    assert not getattr(fake_provider, "send_calls", [])


def test_send_message_works_for_whatsapp_channel(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``POST /v1/messages`` endpoint must accept every
    channel the platform supports. SMS is exercised by
    most of the suite; this test pins the WhatsApp path
    so a future regression that special-cases SMS does not
    break the second channel silently."""
    body = _register(client)
    response = client.post(
        "/v1/messages",
        json={"channel": "whatsapp", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["message"]["channel"] == "whatsapp"
    assert payload["message"]["status"] == "sent"
    # WhatsApp has a higher base cost than SMS in the MVP fee
    # engine; the assertion pins the cost / fee pair so a
    # refactor of ``compute_message_cost`` does not silently
    # change the customer-facing price.
    assert payload["message"]["cost_clp"] == 80
    assert payload["message"]["fee_clp"] == 5


def test_send_message_persists_failure_and_status_reflects_it(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A failed dispatch is durable: ``GET /v1/messages/{id}``
    echoes the ``failed`` status and the error code so a
    subsequent poll sees the same state the POST produced."""
    fake_provider._error = ProviderUnavailableError("down", provider="fake")
    body = _register(client)
    send = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert send.status_code == 202
    message_id = send.json()["message"]["id"]

    status_response = client.get(
        f"/v1/messages/{message_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["status"] == "failed"
    assert payload["error_code"] == "provider_unavailable"


# ---------------------------------------------------------------------------
# POST /v1/messages/batch
# ---------------------------------------------------------------------------


def test_send_batch_returns_202_with_per_item_results(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A well-formed batch returns one result per item, in
    the same order as the request."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                {"channel": "whatsapp", "to": "+56922222222", "body": "dos"},
            ]
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert len(payload["results"]) == 2
    assert [r["to_number"] for r in payload["results"]] == [
        "+56911111111",
        "+56922222222",
    ]


def test_send_batch_422_on_empty_list(client: TestClient) -> None:
    """An empty batch is rejected by Pydantic validation with
    a 422 before the service layer is involved."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={"items": []},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_send_batch_422_on_invalid_item(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A batch with an invalid item is a 422 with a stable
    code."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56912345678", "body": "uno"},
                {"channel": "sms", "to": "not-a-number", "body": "dos"},
            ]
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_destination"


def test_send_batch_422_on_oversized_item_body(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A batch with an item that violates the per-channel body
    cap is rejected at the service layer with the same
    ``body_too_long`` code the single-message endpoint uses,
    so the caller can build a single retry policy around one
    error code. The whole batch is rejected – partial
    persistence would leave the caller guessing which items
    made it through."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56912345678", "body": "ok"},
                {"channel": "sms", "to": "+56912345678", "body": "a" * 2000},
            ]
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "body_too_long"


def test_send_batch_401_when_api_key_missing(client: TestClient) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code."""
    response = client.post(
        "/v1/messages/batch",
        json={"items": [{"channel": "sms", "to": "+56912345678", "body": "uno"}]},
    )
    assert response.status_code == 401


def test_send_batch_records_partial_failure(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A batch with a mix of successes and failures returns
    one result per item; failures are recorded on the row
    but the batch as a whole is still ``202 Accepted``."""

    # Use a counter so the first call succeeds and the second
    # fails: this exercises the per-item outcome shape.
    class _CountingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(name="counting")

        async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
            if to.endswith("2"):
                raise ProviderUnavailableError("down", provider="counting")
            return SendResult(provider_msg_id=f"ok-{to}", raw={})

    import app.adapters.registry as registry

    instance = _CountingProvider()
    registry._BUILDERS[Channel.WHATSAPP] = lambda settings: instance
    registry._BUILDERS[Channel.SMS] = lambda settings: instance

    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                {"channel": "sms", "to": "+56911111112", "body": "dos"},
            ]
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert len(payload["results"]) == 2
    assert payload["results"][0]["status"] == "sent"
    assert payload["results"][1]["status"] == "failed"
    assert payload["results"][1]["error_code"] == "provider_unavailable"


# ---------------------------------------------------------------------------
# GET /v1/messages/{id}
# ---------------------------------------------------------------------------


def test_get_message_status_returns_row(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """After a successful send, ``GET /v1/messages/{id}``
    echoes the persisted row."""
    body = _register(client)
    send = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56912345678", "body": "hola"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert send.status_code == 202, send.text
    message_id = send.json()["message"]["id"]

    response = client.get(
        f"/v1/messages/{message_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == message_id
    assert payload["status"] == "sent"
    assert payload["provider_msg_id"] == "fake-1"


def test_get_message_status_404_for_unknown_id(client: TestClient) -> None:
    """An unknown id is a 404 with a stable code, not a 403 –
    the existence of another tenant's resource is not
    leaked."""
    body = _register(client)
    response = client.get(
        "/v1/messages/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "message_not_found"


def test_get_message_status_404_for_other_clients_message(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A message that belongs to a different client is
    reported as ``not_found`` (not ``forbidden``) so the
    existence of another tenant's resource is not
    leaked."""
    me = _register(client)
    # Register a second client to own the message.
    other_response = client.post(
        "/v1/auth/register",
        json={
            "name": "Other",
            "email": "other@acme.cl",
            "rut": "11.111.111-1",
            "password": "another-secret",
        },
    )
    assert other_response.status_code == 201
    other_key = other_response.json()["api_key"]
    # Send a message as the other client.
    other_send = client.post(
        "/v1/messages",
        json={"channel": "sms", "to": "+56933333333", "body": "private"},
        headers={"X-API-Key": other_key},
    )
    assert other_send.status_code == 202
    foreign_id = other_send.json()["message"]["id"]

    # `me` should not be able to read the foreign message.
    response = client.get(
        f"/v1/messages/{foreign_id}",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "message_not_found"


def test_get_message_status_401_when_api_key_missing(client: TestClient) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code."""
    response = client.get("/v1/messages/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"
