"""HTTP-level tests for the batch messaging endpoints (issue #9).

The tests mount the real :class:`FastAPI` app on a vanilla
:class:`TestClient` and exercise ``/v1/messages/batch`` end-to-end
against an in-memory SQLite database. The point is to assert the
*observable* HTTP contract: status codes, response shapes, header-
driven dependencies, and the cross-tenant 404 guard – not the
internals of the service layer (covered by
:mod:`tests.services.test_batch_messaging`).

The provider HTTP layer is stubbed by patching the registry to
return :class:`FakeProvider` instances, so the suite never opens
a real TCP connection. The fixture set is shared with
:mod:`tests.routes.test_messages` so a future change to the
``register -> send`` flow keeps the two suites in sync.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import ProviderUnavailableError
from app.config import Settings
from app.main import create_app
from app.models.message import Channel


class FakeProvider(BaseProvider):
    """A controllable :class:`BaseProvider` for the route tests.

    Mirrors the helper in :mod:`tests.routes.test_messages` but
    kept local so the batch tests can swap ``_fail_suffix`` for
    the per-recipient failure path the partial-success case
    exercises.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        fail_suffix: str | None = "2",
    ) -> None:
        self.name = name
        self._fail_suffix = fail_suffix

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        if self._fail_suffix is not None and to.endswith(self._fail_suffix):
            raise ProviderUnavailableError("down", provider=self.name)
        return SendResult(provider_msg_id=f"fake-{to}", raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        return "sent"


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
    """Patch the registry so :func:`get_provider` returns a
    single :class:`FakeProvider` for every channel."""
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

    Same pattern as the existing route tests: a fresh engine +
    session factory per test so a failed transaction does not
    leak into the next case.
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
# POST /v1/messages/batch
# ---------------------------------------------------------------------------


def test_send_batch_returns_202_with_batch_id_and_summary(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A well-formed batch returns 202 with a ``batch_id``
    the caller can poll through
    ``GET /v1/messages/batch/{batch_id}`` plus a per-item
    ``results`` list and a ``summary`` block carrying the
    rollup counters the dashboard surfaces on the
    "Campañas" view.

    The two destinations end in ``1`` and ``3`` so the
    :class:`FakeProvider`'s ``fail_suffix="2"`` does not
    trip; this test exercises the "all messages in
    flight" path, not the "partial failure" path."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                {"channel": "whatsapp", "to": "+56933333333", "body": "dos"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["batch_id"]
    assert len(payload["batch_id"]) == 36
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["delivered"] == 0
    assert payload["summary"]["failed"] == 0
    # Both messages are in ``sent`` state (in flight).
    assert payload["summary"]["pending"] == 2
    assert len(payload["results"]) == 2


def test_send_batch_persists_optional_name(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The optional ``name`` field is persisted on the
    :class:`Batch` row so the dashboard can render it on
    the "Campañas" view. The endpoint mirrors the
    persistence: a subsequent ``GET /v1/messages/batch/
    {batch_id}`` returns the same name."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "name": "Black Friday 2026",
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]

    detail = client.get(
        f"/v1/messages/batch/{batch_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert detail.status_code == 200
    assert detail.json()["batch"]["name"] == "Black Friday 2026"


def test_send_batch_records_partial_failure_in_summary(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A campaign with a mix of successes and failures
    surfaces both numbers in the ``summary`` block – the
    caller can render "1 of 2 failed" without
    re-iterating the results."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                # ends in ``2`` -> the fake provider raises
                {"channel": "sms", "to": "+56911111112", "body": "dos"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    summary = response.json()["summary"]
    assert summary["total"] == 2
    assert summary["failed"] == 1
    assert summary["pending"] == 1


# ---------------------------------------------------------------------------
# GET /v1/messages/batch
# ---------------------------------------------------------------------------


def test_list_batches_returns_200_with_batches(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The listing endpoint returns 200 with the
    customer's batches, newest first. The shape is the
    same as the per-batch detail so the dashboard's
    "Campañas" view can iterate the items with a single
    row template."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert sent.status_code == 202

    response = client.get(
        "/v1/messages/batch",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == sent.json()["batch_id"]
    assert payload["items"][0]["total_count"] == 1


def test_list_batches_supports_pagination(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``limit`` / ``offset`` query parameters
    paginate the result. The response carries a
    ``has_more`` flag the dashboard's "Campañas" view
    uses to render the "cargar más" button."""
    body = _register(client)
    for i in range(3):
        sent = client.post(
            "/v1/messages/batch",
            json={
                "items": [
                    {"channel": "sms", "to": "+56911111111", "body": f"msg-{i}"},
                ],
            },
            headers={"X-API-Key": body["api_key"]},
        )
        assert sent.status_code == 202

    page = client.get(
        "/v1/messages/batch?limit=2",
        headers={"X-API-Key": body["api_key"]},
    )
    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 2
    assert payload["has_more"] is True
    assert payload["limit"] == 2


def test_list_batches_filters_by_status(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``status`` query parameter narrows the result
    to a single lifecycle state. A freshly-sent batch
    sits in ``processing`` (delivery receipts have not
    arrived yet), so the filter is the path the
    dashboard's "En curso" view uses."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert sent.status_code == 202

    response = client.get(
        "/v1/messages/batch?status=processing",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1

    empty = client.get(
        "/v1/messages/batch?status=completed",
        headers={"X-API-Key": body["api_key"]},
    )
    assert empty.status_code == 200
    assert empty.json()["total"] == 0


def test_list_batches_422_on_unknown_status(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """An unknown ``status`` value is a 422 with a
    useful Pydantic-validated error message so the
    dashboard can render an inline error without
    inspecting the upstream's vocabulary. The
    response is the same shape FastAPI produces for
    every other ``Query`` enum filter (e.g. the
    ``?channel=`` / ``?status=`` knobs on
    ``GET /v1/messages``), so the dashboard can use
    the same error-rendering path."""
    body = _register(client)
    response = client.get(
        "/v1/messages/batch?status=scheduled",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["query", "status"]
    assert "processing" in detail["msg"]


def test_list_batches_401_when_api_key_missing(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A missing ``X-API-Key`` header is a 401 with a
    stable code, not a 403 (the API key is the only auth
    surface this endpoint exposes)."""
    response = client.get("/v1/messages/batch")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/messages/batch/{batch_id}
# ---------------------------------------------------------------------------


def test_get_batch_returns_200_with_counters(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A successful ``GET /v1/messages/batch/{batch_id}``
    returns the batch with its current counters. The
    contract is the same ``items[0]`` shape the listing
    endpoint uses, so the dashboard can re-render the
    detail page from the listing data without a
    second-round-trip layout."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "name": "Lanzamiento",
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert sent.status_code == 202
    batch_id = sent.json()["batch_id"]

    response = client.get(
        f"/v1/messages/batch/{batch_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()["batch"]
    assert payload["id"] == batch_id
    assert payload["name"] == "Lanzamiento"
    assert payload["total_count"] == 1
    assert payload["pending_count"] == 1
    assert payload["status"] == "processing"


def test_get_batch_404_for_unknown_id(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """An unknown batch id is a 404 with a stable code –
    the caller can retry with a different id without
    having to inspect the upstream's error vocabulary."""
    body = _register(client)
    response = client.get(
        "/v1/messages/batch/00000000-0000-0000-0000-000000000000",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "batch_not_found"


def test_get_batch_404_for_other_clients_batch(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A batch that belongs to a different customer is
    reported as 404 (the same response an unauthenticated
    caller would see) so the existence of another
    tenant's campaign is not leaked."""
    owner = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": owner["api_key"]},
    )
    assert sent.status_code == 202
    batch_id = sent.json()["batch_id"]

    intruder = client.post(
        "/v1/auth/register",
        json={
            "name": "Other Co",
            "email": "ops@other.cl",
            "rut": "22.222.222-2",
            "password": "sup3r-secret",
        },
    )
    assert intruder.status_code == 201

    response = client.get(
        f"/v1/messages/batch/{batch_id}",
        headers={"X-API-Key": intruder.json()["api_key"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "batch_not_found"


def test_get_batch_401_when_api_key_missing(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A missing ``X-API-Key`` header is a 401 with a
    stable code."""
    response = client.get("/v1/messages/batch/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Route ordering
# ---------------------------------------------------------------------------


def test_batch_routes_take_priority_over_catch_all(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """The ``/batch`` and ``/batch/{batch_id}`` routes
    must win over the ``/{message_id}`` catch-all so a
    call to ``GET /v1/messages/batch`` does not get
    resolved as ``GET /v1/messages/{message_id}`` with
    ``message_id="batch"``. The contract is verified by
    submitting a batch and then looking the row up
    through the dedicated endpoint; a regression that
    reversed the order would surface as a 404 with a
    ``message_not_found`` code instead of a 200 with
    the batch payload."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert sent.status_code == 202
    batch_id = sent.json()["batch_id"]

    detail = client.get(
        f"/v1/messages/batch/{batch_id}",
        headers={"X-API-Key": body["api_key"]},
    )
    assert detail.status_code == 200
    assert "batch" in detail.json()
    assert detail.json()["batch"]["id"] == batch_id

    listing = client.get(
        "/v1/messages/batch",
        headers={"X-API-Key": body["api_key"]},
    )
    assert listing.status_code == 200
    assert "items" in listing.json()
