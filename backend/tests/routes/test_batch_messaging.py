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

import httpx
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
    flight" path, not the "partial failure" path.

    The ``summary`` block also carries the aggregated
    ``total_cost_clp`` / ``total_fee_clp`` values the
    dashboard renders as the campaign's "costo total";
    ``total_amount_clp`` is their sum, exposed as a
    convenience for the client. The block also carries
    the per-channel ``channels`` list so the dashboard
    can render the "desglose por canal" widget on the
    same screen."""
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
    # The rollup is the sum of the per-item cost / fee
    # values the Starter plan charges: 1 SMS at 25+5 and
    # 1 WhatsApp at 80+5.
    assert payload["summary"]["total_cost_clp"] == 25 + 80
    assert payload["summary"]["total_fee_clp"] == 5 + 5
    assert (
        payload["summary"]["total_amount_clp"]
        == payload["summary"]["total_cost_clp"]
        + payload["summary"]["total_fee_clp"]
    )
    assert len(payload["results"]) == 2
    # Per-channel rollup: one entry per channel,
    # ordered by ``channel`` (sms first, whatsapp
    # second) so the dashboard renders a stable list.
    channels = payload["summary"]["channels"]
    assert [c["channel"] for c in channels] == ["sms", "whatsapp"]
    sms, whatsapp = channels
    assert sms["count"] == 1
    assert sms["total_cost_clp"] == 25
    assert sms["total_fee_clp"] == 5
    assert sms["total_amount_clp"] == 30
    assert whatsapp["count"] == 1
    assert whatsapp["total_cost_clp"] == 80
    assert whatsapp["total_fee_clp"] == 5
    assert whatsapp["total_amount_clp"] == 85


def test_send_batch_summary_channels_is_empty_for_single_channel_batch(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A single-channel batch returns a ``channels``
    list with one entry (not zero, not all channels –
    just the channels the batch actually used). The
    dashboard can render the value directly without
    special-casing the one-channel case."""
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    channels = response.json()["summary"]["channels"]
    assert len(channels) == 1
    assert channels[0]["channel"] == "sms"
    assert channels[0]["count"] == 1


def test_send_batch_summary_channels_reflects_partial_failure(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A campaign with a mix of successes and failures
    reports the per-channel rollup that includes the
    failed items (the cost / fee was provisioned at
    dispatch time, so the rollup must include them).
    The ``failed`` counter of the matching channel
    records the failure so the dashboard can render
    "1 of 2 failed" without re-iterating the per-item
    results."""
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
    channels = response.json()["summary"]["channels"]
    assert len(channels) == 1
    sms = channels[0]
    assert sms["channel"] == "sms"
    assert sms["count"] == 2
    assert sms["failed"] == 1
    assert sms["pending"] == 1
    # Both items contribute to the rollup (CLP $25 + $5
    # for every SMS under the Starter plan).
    assert sms["total_cost_clp"] == 25 + 25
    assert sms["total_fee_clp"] == 5 + 5
    assert sms["total_amount_clp"] == 60


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
    row template.

    Each item carries the aggregated ``total_cost_clp`` /
    ``total_fee_clp`` / ``total_amount_clp`` rollup so
    the dashboard can render the "costo total" column
    without a second round-trip to the per-batch
    detail endpoint. Each item also carries the
    per-channel ``channels`` list so the dashboard can
    render the "desglose por canal" widget on every
    row of the table without a per-row fetch."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                {"channel": "whatsapp", "to": "+56944444444", "body": "dos"},
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
    assert payload["items"][0]["total_count"] == 2
    # 1 SMS at 25+5 + 1 WhatsApp at 80+5.
    assert payload["items"][0]["total_cost_clp"] == 25 + 80
    assert payload["items"][0]["total_fee_clp"] == 5 + 5
    assert payload["items"][0]["total_amount_clp"] == (25 + 80) + (5 + 5)
    # Per-channel rollup: one entry per channel the
    # batch actually used, ordered by ``channel``.
    channels = payload["items"][0]["channels"]
    assert [c["channel"] for c in channels] == ["sms", "whatsapp"]
    assert channels[0]["count"] == 1
    assert channels[0]["total_cost_clp"] == 25
    assert channels[1]["count"] == 1
    assert channels[1]["total_cost_clp"] == 80


def test_list_batches_returns_empty_channels_for_no_messages(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A freshly-sent batch with at least one message
    carries a non-empty ``channels`` list. A listing
    with no items carries no ``channels`` array (the
    per-item projection is skipped, so the response
    is an empty ``items`` array)."""
    body = _register(client)
    response = client.get(
        "/v1/messages/batch",
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["items"] == []


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
    second-round-trip layout.

    The response also carries the aggregated
    ``total_cost_clp`` / ``total_fee_clp`` values the
    dashboard renders as the campaign's "costo total";
    ``total_amount_clp`` is the customer-facing total
    (cost + fee), exposed as a convenience so the
    client does not have to sum the two fields itself.
    The response also carries the per-channel
    ``channels`` list so the dashboard can render the
    "desglose por canal" widget on the detail page
    without a second round-trip."""
    body = _register(client)
    sent = client.post(
        "/v1/messages/batch",
        json={
            "name": "Lanzamiento",
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
                {"channel": "whatsapp", "to": "+56944444444", "body": "dos"},
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
    assert payload["total_count"] == 2
    assert payload["pending_count"] == 2
    assert payload["status"] == "processing"
    # 1 SMS at 25+5 + 1 WhatsApp at 80+5.
    assert payload["total_cost_clp"] == 25 + 80
    assert payload["total_fee_clp"] == 5 + 5
    assert payload["total_amount_clp"] == (25 + 80) + (5 + 5)
    # Per-channel rollup: one entry per channel,
    # ordered by ``channel``.
    channels = payload["channels"]
    assert [c["channel"] for c in channels] == ["sms", "whatsapp"]
    assert channels[0]["count"] == 1
    assert channels[0]["total_cost_clp"] == 25
    assert channels[1]["count"] == 1
    assert channels[1]["total_cost_clp"] == 80


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


# ---------------------------------------------------------------------------
# POST /v1/messages/batch — completion webhook (issue #9)
# ---------------------------------------------------------------------------
#
# The "Webhook de batch completion funciona" acceptance
# criterion: when the caller supplies a ``webhook_url``,
# the route must fire one signed POST to that URL once
# the batch is dispatched. The test exercises the full
# end-to-end path (HTTP request -> service -> background
# task -> fake receiver) using FastAPI's ``BackgroundTasks``
# machinery and a transport-level stub.


class _CapturingReceiver:
    """HTTP receiver the background webhook task POSTs to.

    The test registers an httpx mock transport bound to
    this receiver, so the background task's outbound POST
    lands in ``captured`` without any real network I/O.
    """

    def __init__(self) -> None:
        self.captured: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.captured.append(
            {
                "url": str(request.url),
                "body": request.content,
                "headers": dict(request.headers),
            }
        )
        return httpx.Response(200, text="ok")


def _patch_webhook_transport(
    monkeypatch: pytest.MonkeyPatch, receiver: _CapturingReceiver
) -> None:
    """Install an httpx mock transport that captures the
    completion webhook POST.

    The transport is wired in at the
    :class:`app.services.webhook_delivery.WebhookDeliveryClient`
    level so the in-memory client used by the production
    code path picks it up. The test asserts the captured
    call so a regression in the helper (wrong URL, wrong
    body, missing signature) fails the test.
    """
    from app.services import webhook_delivery as wd_module

    transport = httpx.MockTransport(receiver.handler)

    class _PatchedClient(wd_module.WebhookDeliveryClient):
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            # Replace the production client with one that
            # uses the mock transport.
            self._client = httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(wd_module, "WebhookDeliveryClient", _PatchedClient)


def test_send_batch_accepts_webhook_url_and_returns_one_time_secret(
    client: TestClient,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed ``POST /v1/messages/batch`` with a
    ``webhook_url`` returns the URL alongside a one-time
    HMAC secret (32 bytes of CSPRNG entropy, hex-encoded).
    The dashboard surfaces the secret to the user so they
    can verify the completion body out-of-band; the
    platform also persists the value on the ``lotes`` row
    so a future re-fire does not have to mint a second
    one.
    """
    body = _register(client)
    receiver = _CapturingReceiver()
    _patch_webhook_transport(monkeypatch, receiver)

    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
            "webhook_url": "https://example.com/hook",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["webhook_url"] == "https://example.com/hook"
    secret = payload["webhook_secret"]
    assert isinstance(secret, str) and len(secret) == 64
    int(secret, 16)  # parses as hex


def test_send_batch_accepts_caller_supplied_webhook_secret(
    client: TestClient,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that supplies a ``webhook_secret`` gets
    the same value back in the response so the route
    layer can confirm the configuration the platform
    persisted.

    The test also asserts the signature header the
    background POST carries is the HMAC-SHA256 of the
    body keyed with the *caller-supplied* secret –
    flipping the secret in transit would defeat the
    purpose of the value, so a regression that uses
    the platform's one-time secret instead fails this
    test.
    """
    from app.services.webhooks import sign_payload

    body = _register(client)
    receiver = _CapturingReceiver()
    _patch_webhook_transport(monkeypatch, receiver)
    secret = "caller-supplied-secret"

    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
            "webhook_url": "https://example.com/hook",
            "webhook_secret": secret,
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["webhook_secret"] == secret

    # The TestClient runs background tasks after the
    # response, so we trigger them explicitly.
    with client.stream("GET", "/health") as _:
        pass  # drain pending background tasks

    # The receiver was hit exactly once with the
    # caller's secret.
    assert len(receiver.captured) == 1
    captured = receiver.captured[0]
    assert captured["url"] == "https://example.com/hook"
    body_bytes = captured["body"]
    assert isinstance(body_bytes, bytes)
    headers_raw = captured["headers"]
    assert isinstance(headers_raw, dict)
    headers = {str(key): str(value) for key, value in headers_raw.items()}
    assert headers["x-mgw-event"] == "batch.completed"
    assert headers["x-mgw-signature"] == sign_payload(
        body=body_bytes, secret=secret
    )


def test_send_batch_omits_webhook_fields_when_not_configured(
    client: TestClient,
    fake_provider: FakeProvider,
) -> None:
    """A request that does not opt-in to the completion
    webhook returns ``webhook_url=None`` and
    ``webhook_secret=None`` so the dashboard can branch
    on the presence / absence of the fields without
    having to inspect the request body.
    """
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["webhook_url"] is None
    assert payload["webhook_secret"] is None


def test_send_batch_rejects_http_webhook_url_with_422(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """A misconfigured ``http://`` (or any non-``https``)
    URL surfaces as 422 with a stable ``code`` so the
    dashboard can fix the input without a manual
    inspection of the server log.
    """
    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
            "webhook_url": "http://insecure.example.com/hook",
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_webhook_url"


# ---------------------------------------------------------------------------
# POST /v1/messages/batch — rate limit (issue #9)
# ---------------------------------------------------------------------------
#
# The "Rate limiting respeta el límite configurado"
# acceptance criterion: an over-the-limit caller
# receives HTTP 429 with a ``Retry-After`` header. The
# tests below drive the limit through the production
# code path by patching the Redis client the limiter
# uses, so the suite never needs a live Redis.


class _CountingRedis:
    """Tiny Redis substitute that tracks per-key counters
    and a one-second TTL, mirroring what the real
    Redis-backed limiter assumes.

    The class is shared between the rate-limit route
    tests below; the small surface area is intentional
    so the production helper can stay mock-free.
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._values[key] = self._values.get(key, 0) + 1
        return self._values[key]

    async def expire(self, key: str, seconds: int) -> None:  # noqa: ARG002
        return None


def test_send_batch_returns_429_when_rate_limited(
    client: TestClient,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The very first call succeeds; the second call,
    issued inside the same one-second window, is
    rejected with HTTP 429 and a ``Retry-After`` header
    so a well-behaved client backs off for the
    remainder of the window.

    The test configures
    :attr:`Settings.batch_rate_limit_per_second` to ``1``
    so the assertion is independent of the platform
    default.
    """
    body = _register(client)
    # Patch the ``get_redis_client`` accessor the rate
    # limiter uses so the fake in-memory counter is the
    # one the production code path picks up.
    fake_redis = _CountingRedis()
    import app.redis_client as redis_client_module
    import app.services.rate_limit as rl_module

    monkeypatch.setattr(redis_client_module, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(rl_module, "get_redis_client", lambda: fake_redis)
    # Tighten the ceiling to 1 for the duration of the
    # test so the assertion is independent of the
    # default (``100``) the production env ships.
    from app.config import get_settings

    real_settings = get_settings()
    monkeypatch.setattr(real_settings, "batch_rate_limit_per_second", 1)

    # First call goes through.
    first = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56911111111", "body": "uno"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert first.status_code == 202

    # Second call inside the same window is rejected.
    second = client.post(
        "/v1/messages/batch",
        json={
            "items": [
                {"channel": "sms", "to": "+56922222222", "body": "dos"},
            ],
        },
        headers={"X-API-Key": body["api_key"]},
    )
    assert second.status_code == 429
    assert second.headers.get("Retry-After") == "1"
    assert second.json()["detail"]["code"] == "batch_rate_limited"


def test_send_batch_does_not_consult_rate_limiter_for_failed_validation(
    client: TestClient,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that fails Pydantic validation is
    rejected by FastAPI **before** the rate limiter
    runs, so a runaway client cannot consume the
    budget with malformed payloads.

    The test submits a request with no ``items`` field
    and asserts the platform returns 422 *without*
    incrementing the counter the test fake tracks.
    """
    fake_redis = _CountingRedis()
    import app.redis_client as redis_client_module
    import app.services.rate_limit as rl_module

    monkeypatch.setattr(redis_client_module, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(rl_module, "get_redis_client", lambda: fake_redis)

    body = _register(client)
    response = client.post(
        "/v1/messages/batch",
        json={},
        headers={"X-API-Key": body["api_key"]},
    )
    assert response.status_code == 422
    # The fake counter saw no traffic.
    assert fake_redis._values == {}
