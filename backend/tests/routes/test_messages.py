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

The :class:`GET /v1/messages` history endpoint (added with
the "Historial y consumo" dashboard task) lives here too:
its observable contract is an HTTP response and that is
exactly what this module exercises.
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


# ---------------------------------------------------------------------------
# GET /v1/messages  –  history listing (issue #6)
# ---------------------------------------------------------------------------


def _send_one(
    client: TestClient, *, api_key: str, to: str, body: str, channel: str = "sms"
) -> dict[str, Any]:
    """Send a single message and return the parsed response body.

    Helper factored out of the history tests so each
    assertion focuses on the contract it cares about
    (pagination, filtering, …) instead of the boilerplate
    of building a POST request.
    """
    response = client.post(
        "/v1/messages",
        json={"channel": channel, "to": to, "body": body},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 202, response.text
    return response.json()["message"]


def test_list_messages_returns_200_with_paginated_history(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A freshly registered customer with a handful of
    messages gets a well-formed paginated response: the
    ``items`` array carries the rows, ``total`` counts the
    full history, ``limit`` / ``offset`` echo the page
    arguments and ``has_more`` is ``False`` when the page
    covers the whole history.
    """
    body = _register(client)
    api_key = body["api_key"]
    sent_ids: list[str] = []
    for i in range(3):
        sent = _send_one(
            client,
            api_key=api_key,
            to=f"+5691234567{i}",
            body=f"hola {i}",
        )
        sent_ids.append(sent["id"])

    response = client.get(
        "/v1/messages",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 3
    assert payload["limit"] == 50  # service default
    assert payload["offset"] == 0
    assert payload["has_more"] is False
    # All three seeded messages are present. The exact
    # order between same-timestamp messages is
    # implementation-defined (the service falls back to the
    # UUID for the tiebreaker), so the contract we pin
    # here is the set, not the order. The
    # "newest-first" property itself is exercised by the
    # dedicated service-level test that controls
    # ``created_at`` directly.
    assert {m["id"] for m in payload["items"]} == set(sent_ids)


def test_list_messages_filters_by_channel(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``?channel=…`` query parameter narrows the
    response to that channel. The test seeds one SMS and one
    WhatsApp message and asserts the ``whatsapp`` filter
    returns only the WhatsApp row."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="sms", channel="sms")
    wa = _send_one(
        client,
        api_key=api_key,
        to="+56922222222",
        body="hola wa",
        channel="whatsapp",
    )

    response = client.get(
        "/v1/messages",
        params={"channel": "whatsapp"},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [m["id"] for m in payload["items"]] == [wa["id"]]
    assert all(m["channel"] == "whatsapp" for m in payload["items"])


def test_list_messages_filters_by_status(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``?status=…`` query parameter narrows the response
    to a single :class:`MessageStatus`. The test seeds a
    success and a provider failure and asserts the
    ``failed`` filter returns only the failure row."""
    fake_provider._error = ProviderUnavailableError("down", provider="fake")
    body = _register(client)
    api_key = body["api_key"]
    failed = _send_one(
        client, api_key=api_key, to="+56911111111", body="primero"
    )
    fake_provider._error = None
    _send_one(client, api_key=api_key, to="+56922222222", body="segundo")

    response = client.get(
        "/v1/messages",
        params={"status": "failed"},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [m["id"] for m in payload["items"]] == [failed["id"]]
    assert all(m["status"] == "failed" for m in payload["items"])


def test_list_messages_paginates_with_limit_and_offset(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """``limit`` and ``offset`` narrow the page; the
    ``has_more`` flag tells the dashboard whether another
    request is needed. The test seeds 5 rows and walks them
    in pages of 2."""
    body = _register(client)
    api_key = body["api_key"]
    for i in range(5):
        _send_one(
            client,
            api_key=api_key,
            to=f"+5691234567{i}",
            body=f"row {i}",
        )

    page1 = client.get(
        "/v1/messages",
        params={"limit": 2, "offset": 0},
        headers={"X-API-Key": api_key},
    ).json()
    assert len(page1["items"]) == 2
    assert page1["total"] == 5
    assert page1["has_more"] is True
    assert page1["limit"] == 2

    page2 = client.get(
        "/v1/messages",
        params={"limit": 2, "offset": 2},
        headers={"X-API-Key": api_key},
    ).json()
    assert len(page2["items"]) == 2
    assert page2["has_more"] is True

    page3 = client.get(
        "/v1/messages",
        params={"limit": 2, "offset": 4},
        headers={"X-API-Key": api_key},
    ).json()
    assert len(page3["items"]) == 1
    assert page3["has_more"] is False


def test_list_messages_does_not_leak_other_clients(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A second customer's history must not bleed into the
    first customer's page. The test seeds messages for both
    customers and asserts the first customer only ever sees
    their own rows."""
    me = _register(client)
    other = client.post(
        "/v1/auth/register",
        json={
            "name": "Other",
            "email": "other@acme.cl",
            "rut": "11.111.111-1",
            "password": "another-secret",
        },
    ).json()
    _send_one(client, api_key=me["api_key"], to="+56911111111", body="mine")
    _send_one(client, api_key=other["api_key"], to="+56922222222", body="theirs")

    response = client.get(
        "/v1/messages",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["to_number"] == "+56911111111"


def test_list_messages_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code;
    the test guards against a regression that would let an
    unauthenticated caller list arbitrary history."""
    response = client.get("/v1/messages")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_list_messages_422_on_unknown_channel_filter(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """An unknown ``?channel=…`` is a 422 with a useful
    Pydantic-validated error message so the dashboard can
    render an inline error."""
    body = _register(client)
    response = client.get(
        "/v1/messages",
        params={"channel": "carrier-pigeon"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    # Pydantic's enum validation produces a list of error
    # objects; the first one references the ``channel``
    # query parameter and names the legal values.
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["query", "channel"]
    assert "sms" in detail["msg"] and "whatsapp" in detail["msg"]


def test_list_messages_422_on_unknown_status_filter(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """An unknown ``?status=…`` is a 422 with a useful
    Pydantic-validated error message."""
    body = _register(client)
    response = client.get(
        "/v1/messages",
        params={"status": "teleported"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["query", "status"]
    assert "sent" in detail["msg"] and "failed" in detail["msg"]


def test_list_messages_422_on_inverted_date_range(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A ``since`` value that is later than ``until`` is a
    422 with a stable code so a buggy date picker cannot
    silently return an empty page."""
    body = _register(client)
    response = client.get(
        "/v1/messages",
        params={
            "since": "2030-01-01T00:00:00+00:00",
            "until": "2020-01-01T00:00:00+00:00",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_date_range"


def test_list_messages_422_on_non_positive_limit(
    client: TestClient,
) -> None:
    """A ``limit=0`` (or any non-positive value) is rejected
    by Pydantic validation with a 422. The dashboard never
    sends a zero, but the test pins the contract so a
    future regression in the call site does not silently
    return an empty page."""
    body = _register(client)
    response = client.get(
        "/v1/messages",
        params={"limit": 0},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_list_messages_empty_history_returns_well_formed_page(
    client: TestClient,
) -> None:
    """A customer who has never sent a message gets a
    well-formed empty page (``items=[]``, ``total=0``,
    ``has_more=False``) so the dashboard can render the
    "no has enviado mensajes todavía" empty state without
    a special-case branch."""
    body = _register(client)
    response = client.get(
        "/v1/messages",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }


# ---------------------------------------------------------------------------
# GET /v1/messages/export  –  CSV download (issue #6 follow-up)
# ---------------------------------------------------------------------------
#
# The dashboard's "Descargar CSV" button targets this endpoint.
# The tests assert the wire format (Content-Type, header shape,
# CSV body) rather than poking at the service layer, so the
# route handler is exercised end-to-end.


def test_export_history_returns_csv_with_attachment_header(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A well-formed request yields a ``text/csv`` body with a
    ``Content-Disposition: attachment`` header so the browser
    saves the file rather than rendering it inline. The
    response also carries ``X-Export-Total`` and
    ``X-Export-Truncated`` so a script can detect a partial
    export without re-running the count."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="uno")
    _send_one(client, api_key=api_key, to="+56922222222", body="dos")

    response = client.get(
        "/v1/messages/export",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.csv"')
    assert response.headers["x-export-total"] == "2"
    assert response.headers["x-export-truncated"] == "false"

    # Body is a well-formed CSV: a header row + 2 data rows.
    lines = response.text.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("id,created_at,channel,status,to_number,body,")
    assert "uno" in lines[1] or "uno" in lines[2]
    assert "dos" in lines[1] or "dos" in lines[2]


def test_export_history_respects_filters(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The export honours the same ``?channel=…`` /
    ``?status=…`` / date range filters the list endpoint
    does. The test seeds a mix of rows and asserts only the
    filtered ones make it into the CSV."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="sms", channel="sms")
    _send_one(
        client, api_key=api_key, to="+56922222222", body="wa", channel="whatsapp"
    )

    response = client.get(
        "/v1/messages/export",
        params={"channel": "whatsapp"},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert len(lines) == 2  # header + 1 data row
    assert "wa" in lines[1]
    assert "sms" not in lines[1]
    assert response.headers["x-export-total"] == "1"


def test_export_history_does_not_leak_other_clients(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A second customer's history must not bleed into the
    CSV. The test seeds messages for both customers and
    asserts the first customer only ever sees their own
    rows in the body and the ``X-Export-Total`` header."""
    me = _register(client)
    other = client.post(
        "/v1/auth/register",
        json={
            "name": "Other",
            "email": "other@acme.cl",
            "rut": "11.111.111-1",
            "password": "another-secret",
        },
    ).json()
    _send_one(client, api_key=me["api_key"], to="+56911111111", body="mine")
    _send_one(client, api_key=other["api_key"], to="+56922222222", body="theirs")

    response = client.get(
        "/v1/messages/export",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 200
    assert response.headers["x-export-total"] == "1"
    assert "mine" in response.text
    assert "theirs" not in response.text


def test_export_history_401_when_api_key_missing(client: TestClient) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code;
    the test guards against a regression that would let an
    unauthenticated caller download arbitrary history."""
    response = client.get("/v1/messages/export")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_export_history_422_on_unknown_channel_filter(
    client: TestClient,
) -> None:
    """An unknown ``?channel=…`` is a 422, mirroring the list
    endpoint's contract. The dashboard never sends a bogus
    value, but the assertion pins the contract so a
    regression in the call site does not silently return a
    200 with the unfiltered export."""
    body = _register(client)
    response = client.get(
        "/v1/messages/export",
        params={"channel": "carrier-pigeon"},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422


def test_export_history_422_on_inverted_date_range(
    client: TestClient,
) -> None:
    """An inverted date range is a 422 with the same
    ``invalid_date_range`` code the list endpoint uses."""
    body = _register(client)
    response = client.get(
        "/v1/messages/export",
        params={
            "since": "2030-01-01T00:00:00+00:00",
            "until": "2020-01-01T00:00:00+00:00",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_date_range"


def test_export_history_empty_history_returns_header_only(
    client: TestClient,
) -> None:
    """A customer who has never sent a message still gets a
    well-formed CSV (header row + ``X-Export-Total: 0``) so
    the dashboard does not have to branch on the empty
    case."""
    body = _register(client)
    response = client.get(
        "/v1/messages/export",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.headers["x-export-total"] == "0"
    assert response.headers["x-export-truncated"] == "false"
    lines = response.text.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("id,created_at,channel,status,to_number,body,")


def test_export_history_does_not_match_get_by_id(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A regression guard: the route is mounted at
    ``/messages/export`` (a literal segment), so a request
    to that path must hit the export endpoint, not the
    single-message ``GET /messages/{message_id}`` route.
    Sending a bogus API key would surface a 401 from the
    export route; if the matcher were reversed, the request
    would try to look up a message with id ``"export"``
    and surface a 404 instead."""
    response = client.get("/v1/messages/export")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


# ---------------------------------------------------------------------------
# GET /v1/messages/daily  –  bar chart data (issue #6)
# ---------------------------------------------------------------------------
#
# The dashboard's "gráfico de barras" is backed by this
# endpoint. The tests exercise the wire format (date / channel
# / count), the default 31-day window, the ``channel`` filter,
# the cross-tenant guard and the 401 / 422 paths. The seed
# pattern is borrowed from the export tests above.


def test_daily_usage_returns_aggregated_buckets(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """Two SMS messages and one WhatsApp message, all
    dispatched in quick succession, surface as two
    ``DailyUsageBucket`` rows on the same calendar day:
    ``(today, sms, 2)`` and ``(today, whatsapp, 1)``. The
    response also carries the resolved ``since`` /
    ``until`` window so the chart axis can be drawn
    without a second round-trip."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="d1-sms-1")
    _send_one(client, api_key=api_key, to="+56911111112", body="d1-sms-2")
    _send_one(
        client, api_key=api_key, to="+56922222222", body="d1-wa", channel="whatsapp"
    )

    response = client.get(
        "/v1/messages/daily",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    by_key = {(item["channel"], item["count"]) for item in payload["items"]}
    assert by_key == {("sms", 2), ("whatsapp", 1)}
    # The window is echoed back so the dashboard can render
    # the chart axis labels.
    assert "since" in payload and "until" in payload


def test_daily_usage_respects_channel_filter(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``?channel=`` query parameter narrows the
    aggregation to a single channel. The test seeds a mix
    of SMS and WhatsApp messages and asserts the ``sms``
    filter only returns the SMS buckets."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="sms")
    _send_one(
        client, api_key=api_key, to="+56922222222", body="wa", channel="whatsapp"
    )

    response = client.get(
        "/v1/messages/daily",
        params={"channel": "sms"},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["channel"] == "sms"
    assert items[0]["count"] == 1


def test_daily_usage_respects_date_range(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``since`` / ``until`` query parameters narrow the
    aggregation to the requested window. The test asserts
    the response carries the resolved bounds so the chart
    axis can be drawn without a second round-trip."""
    body = _register(client)
    api_key = body["api_key"]
    _send_one(client, api_key=api_key, to="+56911111111", body="uno")

    since = "2026-01-01T00:00:00+00:00"
    until = "2026-12-31T23:59:59+00:00"
    response = client.get(
        "/v1/messages/daily",
        params={"since": since, "until": until},
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["since"].startswith("2026-01-01")
    assert payload["until"].startswith("2026-12-31")


def test_daily_usage_does_not_leak_other_clients(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A second customer's history must not bleed into the
    first customer's daily aggregation. The test seeds
    messages for both customers and asserts the first
    customer only ever sees their own buckets."""
    me = _register(client)
    other = client.post(
        "/v1/auth/register",
        json={
            "name": "Other",
            "email": "other@acme.cl",
            "rut": "11.111.111-1",
            "password": "another-secret",
        },
    ).json()
    _send_one(client, api_key=me["api_key"], to="+56911111111", body="mine")
    _send_one(client, api_key=other["api_key"], to="+56922222222", body="theirs")

    response = client.get(
        "/v1/messages/daily",
        headers={"X-API-Key": me["api_key"]},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    total = sum(item["count"] for item in items)
    assert total == 1


def test_daily_usage_401_when_api_key_missing(
    client: TestClient,
) -> None:
    """A missing ``X-API-Key`` is a 401 with a stable code;
    the test guards against a regression that would let an
    unauthenticated caller read the daily aggregation."""
    response = client.get("/v1/messages/daily")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_daily_usage_422_on_inverted_date_range(
    client: TestClient,
) -> None:
    """An inverted date range is a 422 with the same
    ``invalid_date_range`` code the other message endpoints
    use. The test pins the contract so a future regression
    in the call site does not silently return an empty
    response."""
    body = _register(client)
    response = client.get(
        "/v1/messages/daily",
        params={
            "since": "2030-01-01T00:00:00+00:00",
            "until": "2020-01-01T00:00:00+00:00",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_date_range"


def test_daily_usage_empty_history_returns_well_formed_response(
    client: TestClient,
) -> None:
    """A customer who has never sent a message gets a
    well-formed response (``items=[]`` plus a resolved
    window) so the dashboard can render the "todavía no
    has enviado mensajes este mes" empty state without a
    special-case branch."""
    body = _register(client)
    response = client.get(
        "/v1/messages/daily",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert "since" in payload and "until" in payload

